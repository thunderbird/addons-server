#!/usr/bin/env python3
"""
ATN Stage Infrastructure Smoke Test (Read-Only)

This just validates connectivity from ECS tasks to all backend services
without writing any data. Designed to run as a one-off ECS task before
enabling application containers

Basic usage:
    python smoke_test.py                    # Run all checks
    python smoke_test.py --check secrets    # Run a specific check
    python smoke_test.py --json             # Output as JSON

Configuration:
    This script is intended to be committed to a public repo so it does not
    embed any environment-specific endpoints or private IP addresses

    Provide targets via environment variables; checks are SKIPped when their
    required variables are not set.

    Required (per check)
      - RDS_HOST
      - REDIS_NEW_HOST
      - REDIS_EXISTING_HOST
      - RABBITMQ_HOST
      - ES_HOST
      - DNS_HOSTNAMES (comma-separated list)
      - REQUIRED_SECRETS (comma-separated list of Secrets Manager names)

    Optional
      - AWS_REGION (default: us-west-2)
      - RDS_PORT (default: 3306)
      - REDIS_PORT (default: 6379)
      - RABBITMQ_PORT (default: 5672)
      - ES_PORT (default: 443)
      - NAT_EGRESS_URL (default: https://httpbin.org/status/200)

Exit codes:
    0 - All checks passed
    1 - One or more checks failed
"""

import argparse
import json
import os
import socket
import sys
import time
from urllib.request import urlopen
from urllib.error import URLError


# ---------------------------------------------------------------------------
# Configuration -- sourced from environment or defaults
# ---------------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

def _csv_env(var_name: str):
    value = os.environ.get(var_name, "").strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# Backend endpoints (provided via environment; no embedded targets)
CHECKS = {
    "rds_stage": {
        "description": "RDS MySQL (stage)",
        "type": "tcp",
        "host_env": "RDS_HOST",
        "port": int(os.environ.get("RDS_PORT", "3306")),
    },
    "redis_new": {
        "description": "ElastiCache Redis (new, this stack)",
        "type": "tcp",
        "host_env": "REDIS_NEW_HOST",
        "port": int(os.environ.get("REDIS_PORT", "6379")),
    },
    "redis_existing": {
        "description": "ElastiCache Redis (existing, default VPC)",
        "type": "tcp",
        "host_env": "REDIS_EXISTING_HOST",
        "port": int(os.environ.get("REDIS_PORT", "6379")),
    },
    "rabbitmq": {
        "description": "RabbitMQ (default VPC)",
        "type": "tcp",
        "host_env": "RABBITMQ_HOST",
        "port": int(os.environ.get("RABBITMQ_PORT", "5672")),
    },
    "elasticsearch": {
        "description": "Elasticsearch/OpenSearch (managed endpoint, HTTPS)",
        "type": "tcp",
        "host_env": "ES_HOST",
        "port": int(os.environ.get("ES_PORT", "443")),
    },
    "secrets_manager": {
        "description": "Secrets Manager (read access)",
        "type": "secrets",
        "required_secrets": _csv_env("REQUIRED_SECRETS"),
    },
    "dns_resolution": {
        "description": "DNS resolution (cross-VPC)",
        "type": "dns",
        "hostnames": _csv_env("DNS_HOSTNAMES"),
    },
    "nat_egress": {
        "description": "NAT Gateway egress (internet connectivity)",
        "type": "http",
        "url": os.environ.get("NAT_EGRESS_URL", "https://httpbin.org/status/200"),
    },
}


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------
def check_tcp(host, port, timeout=5):
    """Test TCP connectivity to a host:port."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        start = time.time()
        sock.connect((host, port))
        latency_ms = (time.time() - start) * 1000
        sock.close()
        return {
            "status": "PASS",
            "message": f"Connected to {host}:{port} ({latency_ms:.0f}ms)",
        }
    except socket.timeout:
        return {"status": "FAIL", "message": f"Timeout connecting to {host}:{port}"}
    except socket.error as e:
        return {"status": "FAIL", "message": f"Connection failed: {host}:{port} - {e}"}


def check_dns(hostnames):
    """Test DNS resolution for a list of hostnames."""
    results = []
    all_pass = True
    for hostname in hostnames:
        try:
            ip = socket.gethostbyname(hostname)
            results.append(f"{hostname} -> {ip}")
        except socket.gaierror as e:
            results.append(f"{hostname} -> FAILED ({e})")
            all_pass = False
    return {
        "status": "PASS" if all_pass else "FAIL",
        "message": "; ".join(results),
    }


def check_http(url, timeout=10):
    """Test HTTP(S) connectivity."""
    try:
        start = time.time()
        response = urlopen(url, timeout=timeout)
        latency_ms = (time.time() - start) * 1000
        return {
            "status": "PASS",
            "message": f"HTTP {response.status} from {url} ({latency_ms:.0f}ms)",
        }
    except (URLError, OSError) as e:
        return {"status": "FAIL", "message": f"HTTP request failed: {url} - {e}"}


def check_secrets(required_secrets=None):
    """Test Secrets Manager read access without listing or printing values.

    Attempts GetSecretValue on each required secret and reports
    accessible vs denied vs not-found, without printing any values.
    """
    if not required_secrets:
        return {
            "status": "SKIP",
            "message": "Set REQUIRED_SECRETS (comma-separated) to enable this check",
        }

    try:
        import boto3

        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        accessible = []
        denied = []
        not_found = []

        for secret_name in required_secrets:
            try:
                client.get_secret_value(SecretId=secret_name)
                accessible.append(secret_name)
            except client.exceptions.AccessDeniedException:
                denied.append(secret_name)
            except client.exceptions.ResourceNotFoundException:
                not_found.append(secret_name)

        parts = [f"{len(accessible)}/{len(required_secrets)} accessible"]
        if denied:
            parts.append(f"{len(denied)} denied: {', '.join(denied)}")
        if not_found:
            parts.append(f"{len(not_found)} not found: {', '.join(not_found)}")

        has_failures = len(denied) > 0 or len(not_found) > 0
        return {
            "status": "FAIL" if has_failures else "PASS",
            "message": "; ".join(parts),
        }
    except ImportError:
        return {"status": "SKIP", "message": "boto3 not available"}
    except Exception as e:
        return {"status": "FAIL", "message": f"Secrets Manager error: {e}"}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_checks(filter_check=None):
    """Run all or a specific check and return results."""
    results = {}
    for name, config in CHECKS.items():
        if filter_check and name != filter_check:
            continue

        check_type = config["type"]
        description = config["description"]

        if check_type == "tcp":
            host_env = config.get("host_env")
            host = os.environ.get(host_env) if host_env else None
            if not host:
                result = {
                    "status": "SKIP",
                    "message": f"Set {host_env} to enable this check",
                }
            else:
                result = check_tcp(host, config["port"])
        elif check_type == "dns":
            hostnames = config.get("hostnames") or []
            if not hostnames:
                result = {
                    "status": "SKIP",
                    "message": "Set DNS_HOSTNAMES (comma-separated) to enable this check",
                }
            else:
                result = check_dns(hostnames)
        elif check_type == "http":
            result = check_http(config["url"])
        elif check_type == "secrets":
            result = check_secrets(config.get("required_secrets"))
        else:
            result = {"status": "SKIP", "message": f"Unknown check type: {check_type}"}

        results[name] = {
            "description": description,
            **result,
        }

    return results


def print_results(results, as_json=False):
    """Print results in human-readable or JSON format."""
    if as_json:
        print(json.dumps(results, indent=2))
        return

    print("\n" + "=" * 70)
    print("ATN Stage Infrastructure Smoke Test")
    print("=" * 70)

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results.items():
        status = result["status"]
        icon = {"PASS": "[OK]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}.get(
            status, "[??]"
        )

        if status == "PASS":
            passed += 1
        elif status == "FAIL":
            failed += 1
        else:
            skipped += 1

        print(f"\n  {icon} {result['description']}")
        print(f"       {result['message']}")

    print("\n" + "-" * 70)
    print(f"  Results: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="ATN Stage Smoke Test (Read-Only)")
    parser.add_argument("--check", help="Run a specific check only")
    parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )
    args = parser.parse_args()

    results = run_checks(filter_check=args.check)
    print_results(results, as_json=args.json)

    # Exit 1 if any check failed
    has_failures = any(r["status"] == "FAIL" for r in results.values())
    sys.exit(1 if has_failures else 0)


if __name__ == "__main__":
    main()
