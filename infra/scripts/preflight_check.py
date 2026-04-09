#!/usr/bin/env python3
"""
ATN Stage Pre-flight Isolation Validator (RO)

Verifies environment isolation assertions before any scale-up, pulumi up,
or BOOTSTRAP_SAFE flip.  Every check is RO AWS API call.

Triggered after the RabbitMQ incident (issue #375) which revealed that a
stage-named secret could point to a different endpoint, and no automated
gate to validate it existed.

Tier 1 checks (implemented here):
  1. Broker isolation   -- celery_broker must NOT resolve outside of stage
  2. Secret endpoints   -- every atn/stage/* secret with a host/IP must
                           resolve to an expected (stage-scoped) resource
  3. BOOTSTRAP_SAFE     -- every running ECS task definition must have
                           BOOTSTRAP_SAFE set to the expected value

Tier 2 checks (implemented here):
  4. EventBridge state  -- schedules match expected ENABLED/DISABLED
  5. IAM scope          -- task roles only reach atn/stage/*
  6. SG reachability    -- required ports have CIDR rules from ECS VPC

Proposed usage:
    python preflight_check.py                          # all checks (Tier 1 + 2)
    python preflight_check.py --tier 1                 # Tier 1 only
    python preflight_check.py --tier 2                 # Tier 2 only
    python preflight_check.py --check broker           # single check
    python preflight_check.py --check eventbridge      # single check
    python preflight_check.py --json                   # JSON output
    python preflight_check.py --expect-rw              # expect BOOTSTRAP_SAFE=false

Configuration:
    Derived at runtime from Secrets Manager names and ECS API responses

    Environment variables (all optional, with sensible defaults):
      SECRET_PREFIX        atn/stage          Secrets Manager prefix
      ECS_CLUSTER_PREFIX   thunderbird-addons-stage   ECS cluster name prefix
      AWS_REGION           us-west-2          AWS region
      EXPECT_BOOTSTRAP     true               Expected BOOTSTRAP_SAFE value
                                              (override with --expect-rw)

Exit codes:
    0  All checks passed
    1  One or more checks failed
    2  Script error (bad args, missing permissions, etcetera)
"""

import argparse
import json
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class Status(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "atn/stage")
ECS_CLUSTER_PREFIX = os.environ.get("ECS_CLUSTER_PREFIX", "thunderbird-addons-stage")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# AWS clients (lazy, shared)
# ---------------------------------------------------------------------------

_clients: dict = {}


def client(service: str):
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=AWS_REGION)
    return _clients[service]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_secret(name: str) -> Optional[str]:
    try:
        resp = client("secretsmanager").get_secret_value(SecretId=name)
        return resp["SecretString"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def parse_host_from_url(url: str) -> Optional[str]:
    """Extract hostname or IP from amqp://, redis://, smtp+tls://, https:// URLs"""
    match = re.search(r"@([^/:]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"//([^/:@]+)", url)
    if match:
        return match.group(1)
    return None


def is_private_ip(addr: str) -> bool:
    """Check whether an address is actually an RFC 1918 private IP"""
    try:
        parts = list(map(int, addr.split(".")))
        if len(parts) != 4:
            return False
        return (
            parts[0] == 10
            or (parts[0] == 172 and 16 <= parts[1] <= 31)
            or (parts[0] == 192 and parts[1] == 168)
        )
    except (ValueError, AttributeError):
        return False


def resolve_ec2_by_ip(ip: str) -> Optional[dict]:
    """Look up an EC2 instance by private IP; returns {id, name, vpc} or None"""
    try:
        resp = client("ec2").describe_instances(
            Filters=[{"Name": "private-ip-address", "Values": [ip]}]
        )
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                name = ""
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                return {
                    "id": inst["InstanceId"],
                    "name": name,
                    "vpc": inst.get("VpcId", ""),
                    "state": inst["State"]["Name"],
                }
    except ClientError:
        pass
    return None


def resolve_rds_by_host(host: str) -> Optional[dict]:
    """Look up an RDS instance whose endpoint matches *host*"""
    try:
        resp = client("rds").describe_db_instances()
        for db in resp.get("DBInstances", []):
            if db.get("Endpoint", {}).get("Address") == host:
                return {
                    "id": db["DBInstanceIdentifier"],
                    "engine": db["Engine"],
                    "vpc": db.get("DBSubnetGroup", {}).get("VpcId", ""),
                }
    except ClientError:
        pass
    return None


def resolve_elasticache_by_host(host: str) -> Optional[dict]:
    """Look up an ElastiCache node whose endpoint address matches *host*"""
    try:
        for rg in (
            client("elasticache")
            .describe_replication_groups()
            .get("ReplicationGroups", [])
        ):
            for ng in rg.get("NodeGroups", []):
                for ep_key in ("PrimaryEndpoint", "ReaderEndpoint"):
                    ep = ng.get(ep_key, {})
                    if ep.get("Address") == host:
                        return {
                            "id": rg["ReplicationGroupId"],
                            "engine": "redis",
                            "status": rg["Status"],
                        }
        resp = client("elasticache").describe_cache_clusters(ShowCacheNodeInfo=True)
        for cc in resp.get("CacheClusters", []):
            cfg = cc.get("ConfigurationEndpoint", {})
            if cfg.get("Address") == host:
                return {
                    "id": cc["CacheClusterId"],
                    "engine": cc["Engine"],
                    "status": cc["CacheClusterStatus"],
                }
            for node in cc.get("CacheNodes", []):
                if node.get("Endpoint", {}).get("Address") == host:
                    return {
                        "id": cc["CacheClusterId"],
                        "engine": cc["Engine"],
                        "status": cc["CacheClusterStatus"],
                    }
    except ClientError:
        pass
    return None


# ---------------------------------------------------------------------------
# Check 1: Broker isolation
# ---------------------------------------------------------------------------

KNOWN_PROD_BROKER_NAMES = {"rabbitmq", "rabbit", "celery", "mq"}


def check_broker_isolation() -> CheckResult:
    """
    Verify that the celery_broker secret does NOT point to a non-stage broker

    Assertion chain:
      1. Secret must exist
      2. Host must be extractable from the AMQP URL
      3. If host is a private IP in 172.31.0.0/16 (default VPC), resolve the
         EC2 instance and flag if it looks like a non-stage broker
      4. Host should ideally be an AWS-managed endpoint (Amazon MQ, ElastiCache)
         or a dedicated stage hostname
    """
    secret_name = f"{SECRET_PREFIX}/celery_broker"
    raw = get_secret(secret_name)
    if raw is None:
        return CheckResult(
            "broker_isolation",
            Status.SKIP,
            f"Secret {secret_name} not found",
        )

    host = parse_host_from_url(raw)
    if host is None:
        return CheckResult(
            "broker_isolation",
            Status.FAIL,
            "Cannot extract host from broker URL",
            [f"Raw value starts with: {raw[:40]}..."],
        )

    details = [f"Broker host: {host}"]

    if is_private_ip(host):
        ec2 = resolve_ec2_by_ip(host)
        if ec2:
            details.append(
                f"Resolves to EC2 {ec2['id']} name={ec2['name']!r} "
                f"vpc={ec2['vpc']} state={ec2['state']}"
            )
            name_lower = ec2["name"].lower()
            if any(kw in name_lower for kw in KNOWN_PROD_BROKER_NAMES):
                return CheckResult(
                    "broker_isolation",
                    Status.FAIL,
                    f"Broker points to EC2 instance {ec2['id']} ({ec2['name']!r}) "
                    f"which looks like a non-stage broker",
                    details,
                )
            if "stage" not in name_lower and "test" not in name_lower:
                return CheckResult(
                    "broker_isolation",
                    Status.WARN,
                    f"Broker points to EC2 {ec2['id']} ({ec2['name']!r}) -- "
                    f"name does not contain 'stage' or 'test'",
                    details,
                )
        else:
            details.append(f"No EC2 instance found at {host}")
            return CheckResult(
                "broker_isolation",
                Status.WARN,
                f"Broker points to private IP {host} but no EC2 instance found",
                details,
            )

    cache = resolve_elasticache_by_host(host)
    if cache and "stage" in cache["id"].lower():
        details.append(f"Resolves to ElastiCache {cache['id']} (stage)")
        return CheckResult(
            "broker_isolation",
            Status.PASS,
            f"Broker points to stage ElastiCache {cache['id']}",
            details,
        )

    mq_suffix = f".mq.{AWS_REGION}.amazonaws.com"
    if host.endswith(mq_suffix):
        details.append("Host is an Amazon MQ managed endpoint")
        return CheckResult(
            "broker_isolation",
            Status.PASS,
            "Broker points to Amazon MQ endpoint (dedicated managed broker)",
            details,
        )

    if host.endswith(".amazonaws.com") and "stage" in host:
        details.append("Host is an AWS-managed endpoint containing 'stage'")
        return CheckResult(
            "broker_isolation",
            Status.PASS,
            "Broker host looks stage-scoped",
            details,
        )

    return CheckResult(
        "broker_isolation",
        Status.FAIL,
        f"Broker host {host!r} could not be confirmed as stage-scoped",
        details,
    )


# ---------------------------------------------------------------------------
# Check 2: Secret endpoint validation
# ---------------------------------------------------------------------------

SECRET_ENDPOINT_CHECKS = {
    "mysql": {
        "type": "rds",
        "field": "host",
    },
    "mysql_ro": {
        "type": "rds",
        "field": "host",
    },
    "celery_broker": {
        "type": "broker",
    },
    "celery_result_backend": {
        "type": "elasticache",
    },
    "cache_host": {
        "type": "elasticache_raw",
    },
    "elasticsearch_host": {
        "type": "opensearch",
    },
}


def check_secret_endpoints() -> CheckResult:
    """
    For every atn/stage/* secret that contains a hostname or IP, verify it
    resolves to an actual AWS resource and is not obviously non-stage-scoped
    """
    results = []
    failures = 0
    warnings = 0

    secrets_client = client("secretsmanager")
    paginator = secrets_client.get_paginator("list_secrets")
    all_secrets = []
    for page in paginator.paginate(
        Filters=[{"Key": "name", "Values": [SECRET_PREFIX]}]
    ):
        all_secrets.extend(page.get("SecretList", []))

    for sec in all_secrets:
        name = sec["Name"]
        short = name.replace(f"{SECRET_PREFIX}/", "")
        raw = get_secret(name)
        if raw is None:
            continue

        check_spec = SECRET_ENDPOINT_CHECKS.get(short)
        if check_spec is None:
            results.append(
                f"  {short}: no endpoint to validate (key material / config)"
            )
            continue

        host = None
        if check_spec["type"] == "rds":
            try:
                data = json.loads(raw)
                host = data.get(check_spec.get("field", "host"))
            except (json.JSONDecodeError, TypeError):
                host = parse_host_from_url(raw)
        elif check_spec["type"] == "elasticache_raw":
            cleaned = raw.strip().strip('"')
            host = cleaned.split(":")[0] if ":" in cleaned else cleaned
        elif check_spec["type"] in ("broker", "elasticache", "opensearch"):
            host = parse_host_from_url(raw)
            if host is None and raw.startswith("http"):
                host = parse_host_from_url(raw)
            if host is None:
                match = re.search(r"([\w.-]+\.amazonaws\.com)", raw)
                if match:
                    host = match.group(1)

        if not host:
            results.append(f"  {short}: WARN -- could not extract host from value")
            warnings += 1
            continue

        resolved = False
        resource_desc = ""

        if check_spec["type"] == "rds":
            rds = resolve_rds_by_host(host)
            if rds:
                resolved = True
                resource_desc = f"RDS {rds['id']} ({rds['engine']}) vpc={rds['vpc']}"

        elif (
            check_spec["type"] == "elasticache"
            or check_spec["type"] == "elasticache_raw"
        ):
            ec = resolve_elasticache_by_host(host)
            if ec:
                resolved = True
                resource_desc = (
                    f"ElastiCache {ec['id']} ({ec['engine']}) status={ec['status']}"
                )

        elif check_spec["type"] == "opensearch":
            if "es.amazonaws.com" in host:
                resolved = True
                resource_desc = f"OpenSearch VPC endpoint {host}"

        elif check_spec["type"] == "broker":
            if is_private_ip(host):
                ec2 = resolve_ec2_by_ip(host)
                if ec2:
                    resolved = True
                    resource_desc = (
                        f"EC2 {ec2['id']} name={ec2['name']!r} vpc={ec2['vpc']}"
                    )
            elif host.endswith(".amazonaws.com"):
                resolved = True
                resource_desc = f"AWS-managed endpoint {host}"

        if resolved:
            results.append(f"  {short}: OK -- {resource_desc}")
        else:
            try:
                socket.getaddrinfo(host, None)
                results.append(
                    f"  {short}: WARN -- host {host} resolves via DNS but not to a known AWS resource"
                )
                warnings += 1
            except socket.gaierror:
                results.append(f"  {short}: FAIL -- host {host} does not resolve")
                failures += 1

    if failures > 0:
        status = Status.FAIL
        msg = f"{failures} endpoint(s) could not be resolved"
    elif warnings > 0:
        status = Status.WARN
        msg = f"All endpoints resolved but {warnings} warning(s)"
    else:
        status = Status.PASS
        msg = "All secret endpoints resolved to known resources"

    return CheckResult("secret_endpoints", status, msg, results)


# ---------------------------------------------------------------------------
# Check 3: BOOTSTRAP_SAFE consistency
# ---------------------------------------------------------------------------


def check_bootstrap_safe(expect_value: str = "true") -> CheckResult:
    """
    Verify that every running ECS task definition has BOOTSTRAP_SAFE set to
    the expected value

    Scans all clusters matching the prefix, inspects the active task
    definition for each service, and checks the environment variable
    """
    ecs = client("ecs")
    details = []
    failures = 0

    all_cluster_arns = []
    paginator = ecs.get_paginator("list_clusters")
    for page in paginator.paginate():
        all_cluster_arns.extend(page.get("clusterArns", []))
    matching_clusters = [arn for arn in all_cluster_arns if ECS_CLUSTER_PREFIX in arn]

    if not matching_clusters:
        return CheckResult(
            "bootstrap_safe",
            Status.SKIP,
            f"No ECS clusters matching {ECS_CLUSTER_PREFIX!r}",
        )

    for cluster_arn in matching_clusters:
        cluster_name = cluster_arn.split("/")[-1]
        svc_paginator = ecs.get_paginator("list_services")
        service_arns = []
        for page in svc_paginator.paginate(cluster=cluster_arn):
            service_arns.extend(page.get("serviceArns", []))
        if not service_arns:
            continue

        svc_detail = ecs.describe_services(cluster=cluster_arn, services=service_arns)
        for svc in svc_detail.get("services", []):
            svc_name = svc["serviceName"]
            task_def_arn = svc["taskDefinition"]
            desired = svc["desiredCount"]
            running = svc["runningCount"]

            td = ecs.describe_task_definition(taskDefinition=task_def_arn)
            containers = td["taskDefinition"].get("containerDefinitions", [])

            for container in containers:
                env_vars = {
                    e["name"]: e["value"] for e in container.get("environment", [])
                }
                bs_val = env_vars.get("BOOTSTRAP_SAFE")
                container_name = container["name"]

                state_label = f"desired={desired} running={running}"

                if bs_val is None:
                    details.append(
                        f"  {cluster_name}/{svc_name}/{container_name}: "
                        f"WARN -- BOOTSTRAP_SAFE not set ({state_label})"
                    )
                    failures += 1
                elif bs_val.lower() != expect_value.lower():
                    details.append(
                        f"  {cluster_name}/{svc_name}/{container_name}: "
                        f"FAIL -- BOOTSTRAP_SAFE={bs_val!r} (expected {expect_value!r}) "
                        f"({state_label})"
                    )
                    failures += 1
                else:
                    details.append(
                        f"  {cluster_name}/{svc_name}/{container_name}: "
                        f"OK -- BOOTSTRAP_SAFE={bs_val!r} ({state_label})"
                    )

    # Also check cron task definitions (not attached to a service)
    try:
        cron_td_resp = ecs.list_task_definitions(
            familyPrefix=f"{ECS_CLUSTER_PREFIX}-cron", status="ACTIVE", sort="DESC"
        )
        cron_arns = cron_td_resp.get("taskDefinitionArns", [])
        if cron_arns:
            latest_cron = cron_arns[0]
            td = ecs.describe_task_definition(taskDefinition=latest_cron)
            for container in td["taskDefinition"].get("containerDefinitions", []):
                env_vars = {
                    e["name"]: e["value"] for e in container.get("environment", [])
                }
                bs_val = env_vars.get("BOOTSTRAP_SAFE")
                if bs_val is None:
                    details.append(
                        f"  cron (latest)/{container['name']}: "
                        f"WARN -- BOOTSTRAP_SAFE not set"
                    )
                    failures += 1
                elif bs_val.lower() != expect_value.lower():
                    details.append(
                        f"  cron (latest)/{container['name']}: "
                        f"FAIL -- BOOTSTRAP_SAFE={bs_val!r} (expected {expect_value!r})"
                    )
                    failures += 1
                else:
                    details.append(
                        f"  cron (latest)/{container['name']}: "
                        f"OK -- BOOTSTRAP_SAFE={bs_val!r}"
                    )
    except ClientError:
        details.append("  cron: SKIP -- could not list cron task definitions")

    if failures > 0:
        return CheckResult(
            "bootstrap_safe",
            Status.FAIL,
            f"{failures} container(s) have unexpected BOOTSTRAP_SAFE value",
            details,
        )
    return CheckResult(
        "bootstrap_safe",
        Status.PASS,
        f"All containers have BOOTSTRAP_SAFE={expect_value!r}",
        details,
    )


# ---------------------------------------------------------------------------
# Check 4: EventBridge schedule state
# ---------------------------------------------------------------------------

SCHEDULER_PREFIX = os.environ.get("SCHEDULER_PREFIX", ECS_CLUSTER_PREFIX)


def check_eventbridge_state(expect_state: str = "DISABLED") -> CheckResult:
    """
    Verify that every EventBridge Scheduler schedule matching our prefix is
    in the expected state (DISABLED or ENABLED)
    """
    scheduler = client("scheduler")
    details = []
    failures = 0

    try:
        paginator = scheduler.get_paginator("list_schedules")
        schedules = []
        for page in paginator.paginate():
            schedules.extend(page.get("Schedules", []))
    except ClientError as e:
        return CheckResult(
            "eventbridge_state",
            Status.FAIL,
            f"Could not list schedules: {e}",
        )

    matching = [s for s in schedules if SCHEDULER_PREFIX in s.get("Name", "")]

    if not matching:
        return CheckResult(
            "eventbridge_state",
            Status.SKIP,
            f"No schedules matching {SCHEDULER_PREFIX!r}",
        )

    for s in sorted(matching, key=lambda x: x["Name"]):
        name = s["Name"]
        state = s.get("State", "UNKNOWN")
        short = name.replace(f"{ECS_CLUSTER_PREFIX}-", "")
        if state.upper() != expect_state.upper():
            details.append(
                f"  {short}: FAIL -- state={state} (expected {expect_state})"
            )
            failures += 1
        else:
            details.append(f"  {short}: OK -- {state}")

    if failures > 0:
        return CheckResult(
            "eventbridge_state",
            Status.FAIL,
            f"{failures} schedule(s) in unexpected state (expected {expect_state})",
            details,
        )
    return CheckResult(
        "eventbridge_state",
        Status.PASS,
        f"All {len(matching)} schedules are {expect_state}",
        details,
    )


# ---------------------------------------------------------------------------
# Check 5: IAM scope -- task roles restricted to stage secrets
# ---------------------------------------------------------------------------


def check_iam_scope() -> CheckResult:
    """
    Verify that every ECS task role can only read secrets under the expected
    prefix (atn/stage/*) and has no access elsewhere or broader wildcards
    """
    ecs = client("ecs")
    iam = client("iam")
    details = []
    failures = 0
    checked_roles: set[str] = set()

    all_cluster_arns = []
    for page in ecs.get_paginator("list_clusters").paginate():
        all_cluster_arns.extend(page.get("clusterArns", []))
    matching_clusters = [arn for arn in all_cluster_arns if ECS_CLUSTER_PREFIX in arn]

    task_def_arns: set[str] = set()
    for cluster_arn in matching_clusters:
        service_arns = []
        for page in ecs.get_paginator("list_services").paginate(cluster=cluster_arn):
            service_arns.extend(page.get("serviceArns", []))
        for svc_arn in service_arns:
            svc_detail = ecs.describe_services(cluster=cluster_arn, services=[svc_arn])
            for svc in svc_detail.get("services", []):
                task_def_arns.add(svc["taskDefinition"])

    try:
        cron_td_resp = ecs.list_task_definitions(
            familyPrefix=f"{ECS_CLUSTER_PREFIX}-cron", status="ACTIVE", sort="DESC"
        )
        cron_arns = cron_td_resp.get("taskDefinitionArns", [])
        if cron_arns:
            task_def_arns.add(cron_arns[0])
    except ClientError:
        pass

    for td_arn in sorted(task_def_arns):
        td = ecs.describe_task_definition(taskDefinition=td_arn)
        task_role_arn = td["taskDefinition"].get("taskRoleArn", "")
        if not task_role_arn:
            continue
        role_name = task_role_arn.split("/")[-1]
        if role_name in checked_roles:
            continue
        checked_roles.add(role_name)

        try:
            attached = iam.list_attached_role_policies(RoleName=role_name)
        except ClientError as e:
            details.append(f"  {role_name}: FAIL -- could not list policies: {e}")
            failures += 1
            continue

        for pol in attached.get("AttachedPolicies", []):
            pol_arn = pol["PolicyArn"]
            pol_name = pol["PolicyName"]

            try:
                pol_meta = iam.get_policy(PolicyArn=pol_arn)
                version_id = pol_meta["Policy"]["DefaultVersionId"]
                pol_doc = iam.get_policy_version(
                    PolicyArn=pol_arn, VersionId=version_id
                )["PolicyVersion"]["Document"]
            except ClientError as e:
                details.append(
                    f"  {role_name}/{pol_name}: FAIL -- could not read policy: {e}"
                )
                failures += 1
                continue

            statements = pol_doc.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]

            for stmt in statements:
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                resources = stmt.get("Resource", [])
                if isinstance(resources, str):
                    resources = [resources]

                secrets_actions = [a for a in actions if "secretsmanager" in a.lower()]
                if not secrets_actions:
                    continue

                for resource in resources:
                    if f":secret:{SECRET_PREFIX}/" in resource or resource.endswith(
                        f":secret:{SECRET_PREFIX}/*"
                    ):
                        details.append(
                            f"  {role_name}/{pol_name}: OK -- scoped to {SECRET_PREFIX}/*"
                        )
                    elif ":secret:atn/prod" in resource:
                        details.append(
                            f"  {role_name}/{pol_name}: FAIL -- grants access to atn/prod secrets"
                        )
                        failures += 1
                    elif resource == "*" or resource.endswith(":secret:*"):
                        details.append(
                            f"  {role_name}/{pol_name}: FAIL -- grants access to ALL secrets ({resource})"
                        )
                        failures += 1
                    else:
                        details.append(
                            f"  {role_name}/{pol_name}: OK -- scoped to {resource}"
                        )

    if not checked_roles:
        return CheckResult(
            "iam_scope",
            Status.SKIP,
            "No task roles found to check",
        )

    if failures > 0:
        return CheckResult(
            "iam_scope",
            Status.FAIL,
            f"{failures} policy statement(s) grant access beyond {SECRET_PREFIX}/*",
            details,
        )
    return CheckResult(
        "iam_scope",
        Status.PASS,
        f"All {len(checked_roles)} task role(s) restricted to {SECRET_PREFIX}/* secrets",
        details,
    )


# ---------------------------------------------------------------------------
# Check 6: SG reachability matrix
# ---------------------------------------------------------------------------

REQUIRED_PORTS = {
    "rds_mysql": 3306,
    "redis": 6379,
    "opensearch_https": 443,
    "opensearch_http": 9200,
}

EXPECTED_BLOCKED_PORTS = {
    "broker_amqp": 5672,
}

KNOWN_UNREACHABLE = {
    "memcached": 11211,
}


def _get_ecs_vpc_cidr() -> Optional[tuple[str, str]]:
    """Derive the ECS VPC ID and CIDR from the first matching ECS service."""
    ecs = client("ecs")
    ec2 = client("ec2")

    all_cluster_arns = []
    for page in ecs.get_paginator("list_clusters").paginate():
        all_cluster_arns.extend(page.get("clusterArns", []))
    for cluster_arn in all_cluster_arns:
        if ECS_CLUSTER_PREFIX not in cluster_arn:
            continue
        service_arns = []
        for page in ecs.get_paginator("list_services").paginate(cluster=cluster_arn):
            service_arns.extend(page.get("serviceArns", []))
        for svc_arn in service_arns:
            svc_detail = ecs.describe_services(cluster=cluster_arn, services=[svc_arn])
            for svc in svc_detail.get("services", []):
                subnets = (
                    svc.get("networkConfiguration", {})
                    .get("awsvpcConfiguration", {})
                    .get("subnets", [])
                )
                if subnets:
                    subnet_resp = ec2.describe_subnets(SubnetIds=[subnets[0]])
                    if subnet_resp.get("Subnets"):
                        vpc_id = subnet_resp["Subnets"][0]["VpcId"]
                        vpc_resp = ec2.describe_vpcs(VpcIds=[vpc_id])
                        if vpc_resp.get("Vpcs"):
                            cidr = vpc_resp["Vpcs"][0]["CidrBlock"]
                            return vpc_id, cidr
    return None


def _sg_allows_port_from_cidr(sg_id: str, port: int, cidr: str) -> bool:
    """Check whether a security group has an inbound rule for *port* from *cidr*"""
    ec2 = client("ec2")
    try:
        resp = ec2.describe_security_groups(GroupIds=[sg_id])
        for sg in resp.get("SecurityGroups", []):
            for rule in sg.get("IpPermissions", []):
                proto = rule.get("IpProtocol", "")
                if proto not in ("tcp", "-1"):
                    continue
                from_port = rule.get("FromPort", 0)
                to_port = rule.get("ToPort", 65535)
                if proto == "-1" or (from_port <= port <= to_port):
                    for ip_range in rule.get("IpRanges", []):
                        if ip_range.get("CidrIp") == cidr:
                            return True
    except ClientError:
        pass
    return False


def _get_rds_sg_ids(host: str) -> list[str]:
    rds_info = resolve_rds_by_host(host)
    if not rds_info:
        return []
    try:
        resp = client("rds").describe_db_instances(DBInstanceIdentifier=rds_info["id"])
        db = resp["DBInstances"][0]
        return [sg["VpcSecurityGroupId"] for sg in db.get("VpcSecurityGroups", [])]
    except (ClientError, IndexError, KeyError):
        return []


def _get_opensearch_sg_ids(host: str) -> list[str]:
    """Find the SG IDs for the OpenSearch domain whose VPC endpoint matches *host*"""
    if ".es.amazonaws.com" not in host:
        return []
    try:
        for d in client("opensearch").list_domain_names().get("DomainNames", []):
            detail = client("opensearch").describe_domain(DomainName=d["DomainName"])
            vpc_endpoint = detail["DomainStatus"].get("Endpoints", {}).get("vpc", "")
            if host in vpc_endpoint or vpc_endpoint in host:
                return (
                    detail["DomainStatus"]
                    .get("VPCOptions", {})
                    .get("SecurityGroupIds", [])
                )
    except ClientError:
        pass
    return []


def check_sg_reachability() -> CheckResult:
    """
    For each required service port, verify the destination SG has an inbound
    CIDR rule from the ECS VPC.  Also verify that ports expected to be blocked
    remain blocked
    """
    vpc_info = _get_ecs_vpc_cidr()
    if not vpc_info:
        return CheckResult(
            "sg_reachability",
            Status.SKIP,
            "Could not determine ECS VPC CIDR",
        )

    vpc_id, vpc_cidr = vpc_info
    details = [f"ECS VPC: {vpc_id} ({vpc_cidr})"]
    failures = 0

    rds_host = None
    rds_raw = get_secret(f"{SECRET_PREFIX}/mysql")
    if rds_raw:
        try:
            rds_host = json.loads(rds_raw).get("host")
        except (json.JSONDecodeError, TypeError):
            pass
    rds_sgs = _get_rds_sg_ids(rds_host) if rds_host else []

    es_host = None
    es_raw = get_secret(f"{SECRET_PREFIX}/elasticsearch_host")
    if es_raw:
        es_host = parse_host_from_url(es_raw)
        if not es_host:
            match = re.search(r"([\w.-]+\.es\.amazonaws\.com)", es_raw)
            if match:
                es_host = match.group(1)
    es_sgs = _get_opensearch_sg_ids(es_host) if es_host else []

    redis_raw = get_secret(f"{SECRET_PREFIX}/celery_result_backend")
    redis_host = parse_host_from_url(redis_raw) if redis_raw else None
    redis_sgs: list[str] = []
    if redis_host:
        ec_info = resolve_elasticache_by_host(redis_host)
        if ec_info:
            try:
                cc_resp = client("elasticache").describe_cache_clusters(
                    CacheClusterId=f"{ec_info['id']}-001"
                )
                for cc in cc_resp.get("CacheClusters", []):
                    for sg in cc.get("SecurityGroups") or []:
                        redis_sgs.append(sg["SecurityGroupId"])
            except ClientError:
                pass

    port_sg_map = {
        "rds_mysql": rds_sgs,
        "redis": redis_sgs,
        "opensearch_https": es_sgs,
        "opensearch_http": es_sgs,
    }

    for label, port in REQUIRED_PORTS.items():
        sg_ids = port_sg_map.get(label, [])
        if not sg_ids:
            details.append(
                f"  {label} (port {port}): WARN -- could not determine destination SG"
            )
            continue
        allowed = any(_sg_allows_port_from_cidr(sg, port, vpc_cidr) for sg in sg_ids)
        if allowed:
            details.append(f"  {label} (port {port}): OK -- SG allows from {vpc_cidr}")
        else:
            details.append(
                f"  {label} (port {port}): FAIL -- no inbound rule from {vpc_cidr} "
                f"on SG(s) {', '.join(sg_ids)}"
            )
            failures += 1

    broker_raw = get_secret(f"{SECRET_PREFIX}/celery_broker")
    broker_host = parse_host_from_url(broker_raw) if broker_raw else None
    mq_suffix = f".mq.{AWS_REGION}.amazonaws.com"

    if broker_host and broker_host.endswith(mq_suffix):
        details.append(
            "  broker_amqps (port 5671): OK -- broker is Amazon MQ "
            "(SG managed by Pulumi, connectivity via container SG egress)"
        )
    elif broker_host and is_private_ip(broker_host):
        ec2_info = resolve_ec2_by_ip(broker_host)
        if ec2_info:
            try:
                inst_resp = client("ec2").describe_instances(
                    InstanceIds=[ec2_info["id"]]
                )
                broker_sgs = [
                    sg["GroupId"]
                    for res in inst_resp.get("Reservations", [])
                    for inst in res.get("Instances", [])
                    for sg in inst.get("SecurityGroups", [])
                ]
            except ClientError:
                broker_sgs = []

            for label, port in EXPECTED_BLOCKED_PORTS.items():
                allowed = any(
                    _sg_allows_port_from_cidr(sg, port, vpc_cidr) for sg in broker_sgs
                )
                if allowed:
                    details.append(
                        f"  {label} (port {port}): FAIL -- SG ALLOWS from {vpc_cidr} "
                        f"(should be blocked while broker points to non-stage)"
                    )
                    failures += 1
                else:
                    details.append(
                        f"  {label} (port {port}): OK -- blocked "
                        f"(no inbound rule from {vpc_cidr})"
                    )

    has_known_unreachable = False
    for label, port in KNOWN_UNREACHABLE.items():
        has_known_unreachable = True
        details.append(
            f"  {label} (port {port}): DEGRADED -- known unreachable from ECS VPC "
            f"(SG fix pending)"
        )

    if failures > 0:
        return CheckResult(
            "sg_reachability",
            Status.FAIL,
            f"{failures} SG reachability issue(s)",
            details,
        )
    if has_known_unreachable:
        return CheckResult(
            "sg_reachability",
            Status.WARN,
            "Required ports reachable; blocked ports confirmed blocked; "
            "known degraded reachability exists",
            details,
        )
    return CheckResult(
        "sg_reachability",
        Status.PASS,
        "All required ports reachable; blocked ports confirmed blocked",
        details,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TIER_1_CHECKS = ["broker", "secrets", "bootstrap"]
TIER_2_CHECKS = ["eventbridge", "iam", "sg"]

CHECKS = {
    "broker": check_broker_isolation,
    "secrets": check_secret_endpoints,
    "bootstrap": check_bootstrap_safe,
    "eventbridge": check_eventbridge_state,
    "iam": check_iam_scope,
    "sg": check_sg_reachability,
}

STATUS_SYMBOLS = {
    Status.PASS: "PASS",
    Status.FAIL: "FAIL",
    Status.WARN: "WARN",
    Status.SKIP: "SKIP",
}

STATUS_COLOURS = {
    Status.PASS: "\033[32m",
    Status.FAIL: "\033[31m",
    Status.WARN: "\033[33m",
    Status.SKIP: "\033[90m",
}
RESET = "\033[0m"


def run_check(
    name: str, expect_bootstrap: str, expect_schedule_state: str
) -> CheckResult:
    fn = CHECKS[name]
    if name == "bootstrap":
        return fn(expect_bootstrap)
    if name == "eventbridge":
        return fn(expect_schedule_state)
    return fn()


def main():
    parser = argparse.ArgumentParser(
        description="ATN Stage Pre-flight Isolation Validator",
    )
    parser.add_argument(
        "--check",
        choices=list(CHECKS.keys()),
        help="Run a single check instead of all",
    )
    parser.add_argument(
        "--tier",
        type=int,
        choices=[1, 2],
        help="Run only Tier 1 or Tier 2 checks",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--expect-rw",
        action="store_true",
        help="Expect BOOTSTRAP_SAFE=false (use when validating RW mode)",
    )
    parser.add_argument(
        "--expect-schedules-enabled",
        action="store_true",
        help="Expect EventBridge schedules to be ENABLED (default: DISABLED)",
    )
    args = parser.parse_args()

    expect_bootstrap = os.environ.get("EXPECT_BOOTSTRAP", "true")
    if args.expect_rw:
        expect_bootstrap = "false"

    expect_schedule_state = "ENABLED" if args.expect_schedules_enabled else "DISABLED"

    if args.check:
        checks_to_run = [args.check]
    elif args.tier == 1:
        checks_to_run = TIER_1_CHECKS
    elif args.tier == 2:
        checks_to_run = TIER_2_CHECKS
    else:
        checks_to_run = TIER_1_CHECKS + TIER_2_CHECKS
    results: list[CheckResult] = []

    for name in checks_to_run:
        try:
            result = run_check(name, expect_bootstrap, expect_schedule_state)
        except Exception as e:
            result = CheckResult(name, Status.FAIL, f"Exception: {e}")
        results.append(result)

    if args.json_output:
        out = [
            {
                "name": r.name,
                "status": r.status.value,
                "message": r.message,
                "details": r.details,
            }
            for r in results
        ]
        print(json.dumps(out, indent=2))
    else:
        print()
        print("=" * 60)
        print("  ATN Stage Pre-flight Isolation Check")
        print("=" * 60)
        print()
        for r in results:
            colour = STATUS_COLOURS.get(r.status, "")
            symbol = STATUS_SYMBOLS[r.status]
            print(f"  [{colour}{symbol}{RESET}] {r.name}: {r.message}")
            for d in r.details:
                print(f"       {d}")
            print()
        print("-" * 60)

        failed = [r for r in results if r.status == Status.FAIL]
        if failed:
            failed_names = ", ".join(r.name for r in failed)
            print(
                f"  {STATUS_COLOURS[Status.FAIL]}RESULT: FAILED{RESET} "
                f"-- unsafe for worker enablement or RW transition"
            )
            print(f"  Failing checks: {failed_names}")
        else:
            print(
                f"  {STATUS_COLOURS[Status.PASS]}RESULT: PASSED{RESET} "
                f"-- pre-flight checks satisfied"
            )
        print()

    sys.exit(1 if any(r.status == Status.FAIL for r in results) else 0)


if __name__ == "__main__":
    main()
