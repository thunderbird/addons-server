#!/usr/bin/env python3
"""
Thunderbird Add-ons Server Infra

This Pulumi program aims to define the AWS infra for the Thunderbird Add-ons
server (ATN), migrating from EC2/Ansible to ECS Fargate

Architecture:
    - VPC with public/private subnets
    - ECR repository for container images
    - Fargate services: web, worker, versioncheck
    - ElastiCache Redis for Celery
    - (Future) RDS MySQL, OpenSearch, EFS

Usage:
    pulumi preview  # See planned changes
    pulumi up       # Apply changes

Configuration is defined in config.{stack}.yaml files
"""

import json

import pulumi
import pulumi_aws as aws
import tb_pulumi
import tb_pulumi.autoscale
import tb_pulumi.elasticache
import tb_pulumi.fargate
import tb_pulumi.network


def main():
    # Create a ThunderbirdPulumiProject to aggregate resources
    # This loads config.{stack}.yaml automatically
    project = tb_pulumi.ThunderbirdPulumiProject()

    # =========================================================================
    # Extended resource tags
    # =========================================================================
    # tb_pulumi sets 4 default tags: environment, project, pulumi_project,
    # pulumi_stack. We extend with operational and FinOps tags BEFORE creating
    # any resources so all ThunderbirdComponentResources inherit them via
    # their __init__ copy of common_tags. Resources created directly via
    # aws.* also pick these up through project.common_tags spread
    project.common_tags.update(
        {
            "managed_by": "pulumi",
            "repository": "thunderbird/addons-server",
            "repository_url": "https://github.com/thunderbird/addons-server",
            "owner": "thunderbird",
            "service": "addons",
            "lifecycle": "ephemeral" if project.stack == "stage" else "persistent",
        }
    )

    # Pull the resources configuration
    resources = project.config.get("resources", {})

    # =========================================================================
    # VPC - Multi-tier network with public/private subnets
    # =========================================================================
    vpc_config = resources.get("tb:network:MultiTierVpc", {}).get("vpc", {})

    if vpc_config:
        vpc = tb_pulumi.network.MultiTierVpc(
            name=f"{project.name_prefix}-vpc",
            project=project,
            **vpc_config,
        )

        # Extract subnets for use by other resources
        private_subnets = vpc.resources.get("private_subnets", [])
        public_subnets = vpc.resources.get("public_subnets", [])
        vpc_resource = vpc.resources.get("vpc")

        # -----------------------------------------------------------------
        # VPC Peering to default VPC (RDS, Redis, ES, EFS)
        # -----------------------------------------------------------------
        # We handle peering manually (not via MultiTierVpc config) because
        # MultiTierVpc places peering routes on vpc.default_route_table_id,
        # but our subnets use custom public/private route tables created by
        # egress_via_internet_gateway / egress_via_nat_gateway. Routes on
        # the default table would/should be unreachable

        # Create the peering connection
        default_vpc_peer = aws.ec2.VpcPeeringConnection(
            f"{project.name_prefix}-pcx-default-vpc",
            vpc_id=vpc_resource.id,
            peer_vpc_id="vpc-441e5e22",
            auto_accept=True,
            tags={
                **project.common_tags,
                "Name": f"{project.name_prefix}-to-default-vpc",
            },
            opts=pulumi.ResourceOptions(depends_on=[vpc_resource]),
        )

        # Enable DNS resolution across the peering connection
        aws.ec2.PeeringConnectionOptions(
            f"{project.name_prefix}-pcx-requester-dns",
            vpc_peering_connection_id=default_vpc_peer.id,
            requester=aws.ec2.PeeringConnectionOptionsRequesterArgs(
                allow_remote_vpc_dns_resolution=True,
            ),
            opts=pulumi.ResourceOptions(depends_on=[default_vpc_peer]),
        )

        # Add peering route to the PRIVATE route table (ECS tasks need
        # to reach RDS/Redis/ES/EFS in 172.31.0.0/16)
        # Extract route table ID from the route table associations that
        # MultiTierVpc exposes (the actual RouteTable is a local variable
        # inside the component and not directly accessible)
        private_rt_assocs = vpc.resources.get(
            "private_route_table_subnet_associations", []
        )
        if private_rt_assocs:
            aws.ec2.Route(
                f"{project.name_prefix}-private-rt-pcx-route",
                route_table_id=private_rt_assocs[0].route_table_id,
                destination_cidr_block="172.31.0.0/16",
                vpc_peering_connection_id=default_vpc_peer.id,
                opts=pulumi.ResourceOptions(
                    depends_on=[default_vpc_peer, private_rt_assocs[0]]
                ),
            )

        # Add peering route to the PUBLIC route table (ALB health checks
        # may need to reach backends in the default VPC)
        public_rt_assocs = vpc.resources.get(
            "public_route_table_subnet_associations", []
        )
        if public_rt_assocs:
            aws.ec2.Route(
                f"{project.name_prefix}-public-rt-pcx-route",
                route_table_id=public_rt_assocs[0].route_table_id,
                destination_cidr_block="172.31.0.0/16",
                vpc_peering_connection_id=default_vpc_peer.id,
                opts=pulumi.ResourceOptions(
                    depends_on=[default_vpc_peer, public_rt_assocs[0]]
                ),
            )

        # Return route: default VPC -> our VPC via peering
        aws.ec2.Route(
            f"{project.name_prefix}-default-vpc-return-route",
            # Default VPC's sole route table (overrideable via config below)
            route_table_id=resources.get("tb:network:DefaultVpcIngressRules", {}).get(
                "default_vpc_route_table_id",
                "rtb-0657e07f",
            ),
            destination_cidr_block="10.100.0.0/16",
            vpc_peering_connection_id=default_vpc_peer.id,
            opts=pulumi.ResourceOptions(depends_on=[default_vpc_peer]),
        )

        # SG rules on existing security groups in the default VPC
        # Smoke test revealed that different services use different SGs:
        #
        # sg-d5539ea9 (amo-services-prod-tb):
        #   Redis, Memcached, ES/OpenSearch, EFS
        # sg-5133b52c (default VPC SG):
        #   RDS MySQL (and self-referencing for internal comms)
        #
        # We add our VPC CIDR to both SGs for the relevant ports

        # --- sg-d5539ea9: services SG (Redis, Memcached, ES, EFS) ---
        default_vpc_ingress_cfg = resources.get("tb:network:DefaultVpcIngressRules", {})
        stage_vpc_cidr = default_vpc_ingress_cfg.get("stage_vpc_cidr", "10.100.0.0/16")

        services_sg_ids = default_vpc_ingress_cfg.get(
            "services_sg_ids",
            ["sg-d5539ea9"],
        )
        services_sg_ports = {
            "redis": 6379,
            "memcached": 11211,
            "elasticsearch": 9200,
            "elasticsearch-https": 443,  # Managed AWS ES speaks HTTPS
            "efs": 2049,
        }
        for sg_id in services_sg_ids:
            for svc_name, port in services_sg_ports.items():
                aws.ec2.SecurityGroupRule(
                    f"{project.name_prefix}-default-vpc-sg-{svc_name}-{sg_id[-4:]}",
                    type="ingress",
                    security_group_id=sg_id,
                    from_port=port,
                    to_port=port,
                    protocol="tcp",
                    cidr_blocks=[stage_vpc_cidr],
                    description=f"Allow {svc_name} from ATN stage VPC",
                    opts=pulumi.ResourceOptions(depends_on=[default_vpc_peer]),
                )

        # --- sg-5133b52c: default VPC SG (RDS) ---
        # Note: RabbitMQ (5672) was removed after the broker isolation
        # incident (issue #375). The stage broker secret pointed elsewhere;
        # the SG rule gave ECS tasks a clean path to it
        # We should NOT re-add 5672 until a dedicated stage broker exists
        # and the secret is verified to point to it via the preflight check
        default_sg_ids = default_vpc_ingress_cfg.get(
            "default_sg_ids",
            ["sg-5133b52c"],
        )
        default_sg_ports = {
            "mysql": 3306,
        }
        for sg_id in default_sg_ids:
            for svc_name, port in default_sg_ports.items():
                aws.ec2.SecurityGroupRule(
                    f"{project.name_prefix}-default-vpc-defsg-{svc_name}-{sg_id[-4:]}",
                    type="ingress",
                    security_group_id=sg_id,
                    from_port=port,
                    to_port=port,
                    protocol="tcp",
                    cidr_blocks=[stage_vpc_cidr],
                    description=f"Allow {svc_name} from ATN stage VPC",
                    opts=pulumi.ResourceOptions(depends_on=[default_vpc_peer]),
                )

    else:
        private_subnets = []
        public_subnets = []
        vpc_resource = None

    # =========================================================================
    # ECR Repository
    # =========================================================================
    # ECR is not part of tb_pulumi, so we use the AWS provider directly
    # This creates a private repository for the addons-server container images
    ecr_config = resources.get("aws:ecr:Repository", {})
    ecr_repositories = {}

    for repo_name, repo_config in ecr_config.items():
        # Create ECR repository
        # force_delete allows pulumi destroy to succeed even if images exist
        # (safe for staging; prod should set this to False)
        ecr_repo = aws.ecr.Repository(
            f"{project.name_prefix}-{repo_name}",
            name=repo_config.get("name", f"{project.name_prefix}-{repo_name}"),
            image_tag_mutability=repo_config.get("image_tag_mutability", "MUTABLE"),
            force_delete=repo_config.get("force_delete", False),
            image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
                scan_on_push=repo_config.get("scan_on_push", True),
            ),
            encryption_configurations=[
                aws.ecr.RepositoryEncryptionConfigurationArgs(
                    encryption_type=repo_config.get("encryption_type", "AES256"),
                )
            ],
            tags={
                **project.common_tags,
                "Name": f"{project.name_prefix}-{repo_name}",
            },
        )

        # Lifecycle policy to manage image retention
        lifecycle_policy = repo_config.get("lifecycle_policy")
        if lifecycle_policy:
            aws.ecr.LifecyclePolicy(
                f"{project.name_prefix}-{repo_name}-lifecycle",
                repository=ecr_repo.name,
                policy=lifecycle_policy,
                opts=pulumi.ResourceOptions(parent=ecr_repo),
            )

        ecr_repositories[repo_name] = ecr_repo

        # Export repository URL for CI/CD pipelines
        pulumi.export(f"ecr_{repo_name}_url", ecr_repo.repository_url)

    # =========================================================================
    # GitHub Actions OIDC Role for ECR publishing
    # =========================================================================
    # This role allows GH Actions to push images to ECR via OIDC
    #
    # Prerequisites
    #   - OIDC provider exists: token.actions.githubusercontent.com
    #   - After deployment we set AWS_ROLE_ARN as GitHub repo variable
    #
    # Trust policy restricts to
    #   - this specific repository
    #   - the stage branch only
    #   - only the build-and-push.yml workflow
    gha_oidc_config = resources.get("aws:iam:GitHubActionsOIDCRole", {})
    addons_repo = ecr_repositories.get("addons-server")

    if gha_oidc_config and not addons_repo:
        pulumi.log.warn(
            "OIDC role config present but aws:ecr:Repository.addons-server not defined "
            "in this stack; so skipping OIDC role creation"
        )

    if gha_oidc_config and addons_repo:
        github_org = gha_oidc_config.get("github_org", "thunderbird")
        github_repo = gha_oidc_config.get("github_repo", "addons-server")
        allowed_branches = gha_oidc_config.get("allowed_branches", ["stage"])

        # Build the subject conditions for allowed branches
        sub_conditions = [
            f"repo:{github_org}/{github_repo}:ref:refs/heads/{branch}"
            for branch in allowed_branches
        ]

        gha_trust_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Federated": f"arn:aws:iam::{project.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
                        },
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {
                                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                            },
                            "StringLike": {
                                "token.actions.githubusercontent.com:sub": sub_conditions
                                if len(sub_conditions) > 1
                                else sub_conditions[0],
                            },
                        },
                    }
                ],
            }
        )

        gha_ecr_publish_role = aws.iam.Role(
            f"{project.name_prefix}-gha-ecr-publish",
            name=f"{project.name_prefix}-gha-ecr-publish",
            description=f"GitHub Actions OIDC role for ECR publishing ({github_org}/{github_repo})",
            assume_role_policy=gha_trust_policy,
            tags=project.common_tags,
        )

        # ECR push permissions derive ARN from actual repo to avoid drifts
        gha_ecr_policy_doc = addons_repo.arn.apply(
            lambda arn: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "ECRAuth",
                            "Effect": "Allow",
                            "Action": "ecr:GetAuthorizationToken",
                            "Resource": "*",
                        },
                        {
                            "Sid": "ECRPush",
                            "Effect": "Allow",
                            "Action": [
                                "ecr:BatchCheckLayerAvailability",
                                "ecr:BatchGetImage",
                                "ecr:CompleteLayerUpload",
                                "ecr:DescribeImages",
                                "ecr:DescribeRepositories",
                                "ecr:GetDownloadUrlForLayer",
                                "ecr:InitiateLayerUpload",
                                "ecr:ListImages",
                                "ecr:PutImage",
                                "ecr:UploadLayerPart",
                            ],
                            "Resource": arn,
                        },
                    ],
                }
            )
        )

        gha_ecr_policy = aws.iam.Policy(
            f"{project.name_prefix}-gha-ecr-push-policy",
            name=f"{project.name_prefix}-gha-ecr-push",
            description="Allows GitHub Actions to push images to ECR",
            policy=gha_ecr_policy_doc,
            tags=project.common_tags,
        )

        aws.iam.RolePolicyAttachment(
            f"{project.name_prefix}-gha-ecr-policy-attachment",
            role=gha_ecr_publish_role.name,
            policy_arn=gha_ecr_policy.arn,
        )

        # Export the role ARN for GitHub repo variable setup
        pulumi.export("gha_ecr_publish_role_arn", gha_ecr_publish_role.arn)

    # =========================================================================
    # Security Groups (accounts-repo pattern)
    # =========================================================================
    # Pattern is: separate load_balancers and containers sections
    # For each service, matching entries in both. Workers with no LB set to
    # null. Code dynamically wires source_security_group_id from LB SG to
    # container ingress
    sg_configs = resources.get("tb:network:SecurityGroupWithRules", {})
    lb_sg_configs = sg_configs.get("load_balancers", {})
    container_sg_configs = sg_configs.get("containers", {})

    # Build security groups for load balancers
    lb_sgs = {}
    for service, sg_config in lb_sg_configs.items():
        if sg_config is None:
            lb_sgs[service] = None
            continue
        if vpc_resource:
            sg_config["vpc_id"] = vpc_resource.id
        lb_sgs[service] = tb_pulumi.network.SecurityGroupWithRules(
            name=f"{project.name_prefix}-sg-lb-{service}",
            project=project,
            opts=pulumi.ResourceOptions(depends_on=[vpc] if vpc_config else None),
            **sg_config,
        )

    # Build security groups for containers
    # Wire source_security_group_id from LB SG to container ingress rules
    container_sgs = {}
    for service, sg_config in container_sg_configs.items():
        if service not in lb_sg_configs:
            pulumi.log.warn(
                f"Container SG '{service}' has no matching load_balancers entry"
            )
        # Dynamically set source_security_group_id for ingress rules
        if lb_sgs.get(service) is not None:
            for rule in sg_config.get("rules", {}).get("ingress", []):
                if "self" not in rule or not rule.get("self"):
                    rule["source_security_group_id"] = (
                        lb_sgs[service].resources["sg"].id
                    )
        if vpc_resource:
            sg_config["vpc_id"] = vpc_resource.id
        depends_on = []
        if lb_sgs.get(service):
            depends_on.append(lb_sgs[service].resources["sg"])
        if vpc_config:
            depends_on.append(vpc)
        container_sgs[service] = tb_pulumi.network.SecurityGroupWithRules(
            name=f"{project.name_prefix}-sg-cont-{service}",
            project=project,
            opts=pulumi.ResourceOptions(depends_on=depends_on) if depends_on else None,
            **sg_config,
        )

    # =========================================================================
    # EFS Mount Targets (addons shared storage)
    # =========================================================================
    # The addons EFS filesystem hosts add-on files, uploads, and media
    # (legacy NFS share from the EC2 era). Mount targets in the ATN VPC
    # private subnets give Fargate tasks a local-VPC ENI for NFS so they
    # don't need to route through VPC peering for every file I/O
    #
    # The filesystem retains its existing mount targets in the default VPC
    # for the EC2 fleet; multi-VPC mount targets (Sep 2024) allow both
    # fleets to coexist during migration
    #
    # NFS SG: allows TCP 2049 inbound only from the container SGs that
    # actually need filesystem access (web + worker; versioncheck excluded
    # per existing Ansible config efs: false)
    efs_config = resources.get("aws:efs:MountTargets", {})
    efs_mount_targets = []
    efs_filesystem_id = None

    if efs_config and private_subnets and vpc_resource:
        efs_secret_name = efs_config["efs_filesystem_id_secret_name"]
        efs_secret = aws.secretsmanager.get_secret_version(
            secret_id=efs_secret_name,
        )
        efs_filesystem_id = pulumi.Output.secret(efs_secret.secret_string)

        # NFS security group for mount target ENIs
        efs_sg = aws.ec2.SecurityGroup(
            f"{project.name_prefix}-efs-mt-sg",
            name=f"{project.name_prefix}-efs-mt",
            description="NFS access to EFS mount targets from Fargate containers",
            vpc_id=vpc_resource.id,
            tags={
                **project.common_tags,
                "Name": f"{project.name_prefix}-efs-mt",
            },
        )

        # Allow NFS (TCP 2049) from each container SG that needs EFS
        efs_ingress_services = efs_config.get(
            "ingress_from_services", ["web", "worker"]
        )
        for svc_name in efs_ingress_services:
            cont_sg = container_sgs.get(svc_name)
            if cont_sg:
                aws.ec2.SecurityGroupRule(
                    f"{project.name_prefix}-efs-nfs-from-{svc_name}",
                    type="ingress",
                    security_group_id=efs_sg.id,
                    from_port=2049,
                    to_port=2049,
                    protocol="tcp",
                    source_security_group_id=cont_sg.resources["sg"].id,
                    description=f"NFS from {svc_name} containers",
                )

        # Mount target in each private subnet
        for i, subnet in enumerate(private_subnets):
            mt = aws.efs.MountTarget(
                f"{project.name_prefix}-efs-mt-{i}",
                file_system_id=efs_filesystem_id,
                subnet_id=subnet.id,
                security_groups=[efs_sg.id],
                opts=pulumi.ResourceOptions(depends_on=[efs_sg, subnet]),
            )
            efs_mount_targets.append(mt)

        pulumi.export("efs_mount_target_ids", [mt.id for mt in efs_mount_targets])

    # =========================================================================
    # Fargate App Task Role
    # =========================================================================
    # tb_pulumi creates a task_role per FargateClusterWithLogging but only
    # sets it as execution_role_arn (image pulls, log writes, ECS-injected
    # secrets). It does NOT set task_role_arn on the ECS task definition, so
    # the container has no IAM identity at runtime -- boto3 calls (e.g., the
    # app fetching secrets directly from Secrets Manager) would fail
    #
    # Approach: create a shared app-level task role with runtime permissions,
    # inject its ARN into each service task_definition config dict before
    # passing to FargateClusterWithLogging. The dict gets splatted into
    # aws.ecs.TaskDefinition(**task_def), so task_role_arn propagates cleanly
    fargate_app_task_role = None
    if vpc_resource:
        app_task_assume_role = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )

        fargate_app_task_role = aws.iam.Role(
            f"{project.name_prefix}-fargate-app-task-role",
            name=f"{project.name_prefix}-fargate-app-task-role",
            description="Runtime IAM role for Fargate containers (boto3 / SDK calls)",
            assume_role_policy=app_task_assume_role,
            tags=project.common_tags,
        )

        # Attach the atn/{stack}/* secrets policy so the app can fetch secrets
        # at runtime via boto3 (settings_local.py reads from Secrets Manager)
        # NOTE: here if any secret uses a customer-managed KMS key, kms:Decrypt
        # will also be needed here -- add as a follow-up if GetSecretValue
        # returns AccessDenied
        app_task_secrets_policy_doc = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AllowATNSecretsAccess",
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": f"arn:aws:secretsmanager:{project.aws_region}:{project.aws_account_id}:secret:atn/{project.stack}/*",
                    }
                ],
            }
        )

        app_task_secrets_policy = aws.iam.Policy(
            f"{project.name_prefix}-app-task-secrets-policy",
            name=f"{project.name_prefix}-app-task-secrets",
            description="Allows Fargate app containers to read atn secrets at runtime",
            policy=app_task_secrets_policy_doc,
            tags=project.common_tags,
        )

        aws.iam.RolePolicyAttachment(
            f"{project.name_prefix}-app-task-secrets-attachment",
            role=fargate_app_task_role.name,
            policy_arn=app_task_secrets_policy.arn,
        )

    # =========================================================================
    # Fargate Services
    # =========================================================================
    fargate_configs = resources.get("tb:fargate:FargateClusterWithLogging", {})
    fargate_services = {}

    for service_name, service_config in fargate_configs.items():
        is_internal = service_config.get("internal", True)
        # Internet-facing ALBs require public subnets and tb_pulumi uses a single
        # subnet list for both ALB and tasks, so external services must land in
        # public subnets. To compensate this we'd force assign_public_ip=True so
        # tasks can reach ECR/internet via IGW (private subnet tasks use NAT)
        # TODO: consider tb_pulumi proposal to support separate ALB/task subnets
        subnets = private_subnets if is_internal else public_subnets
        if not is_internal:
            service_config["assign_public_ip"] = True

        if subnets:
            # Get security groups for this service
            lb_sg = lb_sgs.get(service_name)
            container_sg = container_sgs.get(service_name)

            # Extract SG IDs
            lb_sg_ids = [lb_sg.resources["sg"].id] if lb_sg else []
            container_sg_ids = [container_sg.resources["sg"].id] if container_sg else []

            # Inject task_role_arn into the task definition so containers
            # have an IAM identity at runtime (cf. Fargate App Task Role
            # section above for why this is needed)
            # setdefault ensures the dict is on service_config even if
            # task_definition was absent, so the ARN isn't dropped when
            # **service_config is spread into the constructor.
            task_def = service_config.setdefault("task_definition", {})
            if fargate_app_task_role and "task_role_arn" not in task_def:
                task_def["task_role_arn"] = fargate_app_task_role.arn

            # Inject EFS filesystem ID from Secrets Manager into any
            # volume configs that declare an efs_volume_configuration
            # The YAML carries the volume structure
            if efs_filesystem_id is not None:
                for vol in task_def.get("volumes", []):
                    efs_vol_cfg = vol.get("efs_volume_configuration")
                    if efs_vol_cfg and "file_system_id" not in efs_vol_cfg:
                        efs_vol_cfg["file_system_id"] = efs_filesystem_id

            # Build depends_on list
            depends_on = [*subnets]
            if container_sg:
                depends_on.append(container_sg.resources["sg"])
            if lb_sg:
                depends_on.append(lb_sg.resources["sg"])
            if fargate_app_task_role:
                depends_on.append(fargate_app_task_role)
            # EFS mount targets must exist before tasks that mount them
            if efs_mount_targets and service_name in efs_config.get(
                "ingress_from_services", []
            ):
                depends_on.extend(efs_mount_targets)

            fargate_services[service_name] = (
                tb_pulumi.fargate.FargateClusterWithLogging(
                    name=f"{project.name_prefix}-{service_name}",
                    project=project,
                    subnets=subnets,  # Pass subnet objects; tb_pulumi extracts .id internally
                    container_security_groups=container_sg_ids,
                    load_balancer_security_groups=lb_sg_ids if not is_internal else [],
                    opts=pulumi.ResourceOptions(depends_on=depends_on),
                    **service_config,
                )
            )

    # =========================================================================
    # Additional Secrets Manager access for Fargate execution roles
    # =========================================================================
    # tb_pulumi scopes its auto-created secrets policy to
    # {project}/{stack}/* = thunderbird-addons/stage/*, but the app expects
    # atn/stage/* (existing convention). We attach an additional policy to
    # each tb_pulumi-managed execution role so the ECS agent can inject
    # atn secrets into containers at launch time.
    #
    # Note: runtime boto3 access is here handled by the separate app task role
    # (fargate_app_task_role) created above, which has its own secrets policy.
    atn_exec_secrets_policy_doc = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowATNSecretsAccess",
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": f"arn:aws:secretsmanager:{project.aws_region}:{project.aws_account_id}:secret:atn/{project.stack}/*",
                }
            ],
        }
    )

    atn_exec_secrets_policy = aws.iam.Policy(
        f"{project.name_prefix}-atn-secrets-policy",
        name=f"{project.name_prefix}-atn-secrets",
        description=f"Allows ECS execution role to access atn/{project.stack}/* secrets",
        policy=atn_exec_secrets_policy_doc,
        tags=project.common_tags,
    )

    for service_name, fargate_service in fargate_services.items():
        task_role = fargate_service.resources.get("task_role")
        if task_role:
            aws.iam.RolePolicyAttachment(
                f"{project.name_prefix}-{service_name}-atn-secrets",
                role=task_role.name,
                policy_arn=atn_exec_secrets_policy.arn,
            )

    # =========================================================================
    # ECS Service Autoscaling
    # =========================================================================
    # Target-tracking policies for CPU and memory. Thresholds are sensible
    # defaults based on the thunderbird-accounts pattern; to be tuned after
    # observing real workload performance
    #
    # Config-driven: each service can optionally have an "autoscaling" key in
    # config.stage.yaml. If absent, no autoscaler is created for that service
    autoscaling_configs = resources.get("tb:autoscale:EcsServiceAutoscaler", {})

    for service_name, scaling_config in autoscaling_configs.items():
        fargate_svc = fargate_services.get(service_name)
        if not fargate_svc:
            pulumi.log.warn(
                f"Autoscaling config for '{service_name}' but no matching Fargate service"
            )
            continue

        ecs_service = fargate_svc.resources.get("service")
        if ecs_service:
            tb_pulumi.autoscale.EcsServiceAutoscaler(
                name=f"{project.name_prefix}-{service_name}-autoscaler",
                project=project,
                service=ecs_service,
                **scaling_config,
            )

    # =========================================================================
    # ElastiCache - Redis
    # =========================================================================
    elasticache_configs = resources.get(
        "tb:elasticache:ElastiCacheReplicationGroup", {}
    )
    elasticache_clusters = {}

    for cluster_name, cluster_config in elasticache_configs.items():
        if private_subnets:
            # Add source access from private subnets
            if "source_cidrs" not in cluster_config:
                cluster_config["source_cidrs"] = ["10.100.0.0/16"]  # VPC CIDR

            elasticache_clusters[cluster_name] = (
                tb_pulumi.elasticache.ElastiCacheReplicationGroup(
                    name=f"{project.name_prefix}-{cluster_name}",
                    project=project,
                    subnets=private_subnets,
                    **cluster_config,
                )
            )

    # =========================================================================
    # ECS Scheduled Tasks (Cron Jobs)
    # =========================================================================
    # Uses EventBridge Scheduler to run management commands on schedule
    # Each scheduled task runs as a Fargate task with command override
    scheduled_tasks_config = resources.get("aws:scheduler:ScheduledTasks", {})
    addons_ecr_repo = ecr_repositories.get("addons-server")

    if scheduled_tasks_config and private_subnets and addons_ecr_repo:
        # ---------------------------------------------------------------------
        # Task Execution Role (ECS to pull images and write logs)
        # ---------------------------------------------------------------------
        task_execution_assume_role = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )

        cron_execution_role = aws.iam.Role(
            f"{project.name_prefix}-cron-execution-role",
            name=f"{project.name_prefix}-cron-execution-role",
            assume_role_policy=task_execution_assume_role,
            tags=project.common_tags,
        )

        # Attach AWS managed policy for ECS task execution
        aws.iam.RolePolicyAttachment(
            f"{project.name_prefix}-cron-execution-policy",
            role=cron_execution_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        )

        # Additional policy for Secrets Manager access
        cron_secrets_policy_doc = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": [
                            f"arn:aws:secretsmanager:{project.aws_region}:{project.aws_account_id}:secret:atn/{project.stack}/*"
                        ],
                    }
                ],
            }
        )

        cron_secrets_policy = aws.iam.Policy(
            f"{project.name_prefix}-cron-secrets-policy",
            name=f"{project.name_prefix}-cron-secrets-policy",
            policy=cron_secrets_policy_doc,
            tags=project.common_tags,
        )

        aws.iam.RolePolicyAttachment(
            f"{project.name_prefix}-cron-secrets-attachment",
            role=cron_execution_role.name,
            policy_arn=cron_secrets_policy.arn,
        )

        # ---------------------------------------------------------------------
        # Task Role (container to access AWS resources)
        # ---------------------------------------------------------------------
        cron_task_role = aws.iam.Role(
            f"{project.name_prefix}-cron-task-role",
            name=f"{project.name_prefix}-cron-task-role",
            assume_role_policy=task_execution_assume_role,
            tags=project.common_tags,
        )

        # Also attach secrets policy to the cron TASK role (not just
        # execution role). The execution role is used by ECS to pull
        # images/inject secrets; the task role is used by the container
        # at runtime for boto3 calls (e.g. fetching secrets directly).
        aws.iam.RolePolicyAttachment(
            f"{project.name_prefix}-cron-task-secrets-attachment",
            role=cron_task_role.name,
            policy_arn=cron_secrets_policy.arn,
        )

        # ---------------------------------------------------------------------
        # CloudWatch Log Group for cron tasks
        # ---------------------------------------------------------------------
        aws.cloudwatch.LogGroup(
            f"{project.name_prefix}-cron-logs",
            name=f"/ecs/{project.name_prefix}-cron",
            retention_in_days=30,
            tags=project.common_tags,
        )

        # ---------------------------------------------------------------------
        # Cron Task Definition
        # ---------------------------------------------------------------------
        # Lightweight task definition for management commands
        # Command here is overridden per schedule via container overrides
        cron_container_def = addons_ecr_repo.repository_url.apply(
            lambda url: json.dumps(
                [
                    {
                        "name": "cron",
                        "image": f"{url}:stage-latest",
                        "essential": True,
                        "command": [
                            "manage",
                            "help",
                        ],  # Default; again overridden per schedule
                        "mountPoints": [
                            {
                                "sourceVolume": "addons-efs",
                                "containerPath": "/var/addons",
                                "readOnly": False,
                            }
                        ],
                        "environment": [
                            {
                                "name": "DJANGO_SETTINGS_MODULE",
                                "value": "settings_local_stage",
                            },
                            {"name": "BOOTSTRAP_SAFE", "value": "true"},
                            {"name": "NETAPP_STORAGE_ROOT", "value": "/tmp/storage"},
                        ],
                        "logConfiguration": {
                            "logDriver": "awslogs",
                            "options": {
                                "awslogs-group": f"/ecs/{project.name_prefix}-cron",
                                "awslogs-region": project.aws_region,
                                "awslogs-stream-prefix": "cron",
                            },
                        },
                    }
                ]
            )
        )

        cron_task_definition = aws.ecs.TaskDefinition(
            f"{project.name_prefix}-cron",
            family=f"{project.name_prefix}-cron",
            cpu="512",  # 0.5 vCPU - probably sufficient for management commands
            memory="1024",  # 1 GB
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            execution_role_arn=cron_execution_role.arn,
            task_role_arn=cron_task_role.arn,
            container_definitions=cron_container_def,
            volumes=[
                aws.ecs.TaskDefinitionVolumeArgs(
                    name="addons-efs",
                    efs_volume_configuration=aws.ecs.TaskDefinitionVolumeEfsVolumeConfigurationArgs(
                        file_system_id=efs_filesystem_id,
                        root_directory="/",
                        transit_encryption="ENABLED",
                    ),
                )
            ],
            tags=project.common_tags,
            opts=pulumi.ResourceOptions(
                depends_on=efs_mount_targets if efs_mount_targets else None,
            ),
        )

        # ---------------------------------------------------------------------
        # EventBridge Scheduler IAM Role
        # ---------------------------------------------------------------------
        scheduler_assume_role_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "scheduler.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )

        scheduler_role = aws.iam.Role(
            f"{project.name_prefix}-scheduler-role",
            name=f"{project.name_prefix}-scheduler-role",
            assume_role_policy=scheduler_assume_role_policy,
            tags=project.common_tags,
        )

        # Policy for Scheduler to run ECS tasks and pass roles.
        # PassRole is scoped to only the cron execution and task roles
        # (not Resource: * which would allow privilege escalation)
        scheduler_policy_doc = pulumi.Output.all(
            cron_task_definition.arn,
            cron_execution_role.arn,
            cron_task_role.arn,
        ).apply(
            lambda args: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "RunTask",
                            "Effect": "Allow",
                            "Action": ["ecs:RunTask"],
                            "Resource": [args[0]],
                            "Condition": {
                                "ArnLike": {
                                    "ecs:cluster": f"arn:aws:ecs:{project.aws_region}:{project.aws_account_id}:cluster/{project.name_prefix}-worker-cluster"
                                }
                            },
                        },
                        {
                            "Sid": "PassRole",
                            "Effect": "Allow",
                            "Action": ["iam:PassRole"],
                            "Resource": [args[1], args[2]],
                            "Condition": {
                                "StringLike": {
                                    "iam:PassedToService": "ecs-tasks.amazonaws.com"
                                }
                            },
                        },
                    ],
                }
            )
        )

        scheduler_policy = aws.iam.Policy(
            f"{project.name_prefix}-scheduler-policy",
            name=f"{project.name_prefix}-scheduler-policy",
            policy=scheduler_policy_doc,
            tags=project.common_tags,
        )

        aws.iam.RolePolicyAttachment(
            f"{project.name_prefix}-scheduler-policy-attachment",
            role=scheduler_role.name,
            policy_arn=scheduler_policy.arn,
        )

        # ---------------------------------------------------------------------
        # Schedule Group (organises all cron schedules)
        # ---------------------------------------------------------------------
        schedule_group = aws.scheduler.ScheduleGroup(
            f"{project.name_prefix}-cron-group",
            name=f"{project.name_prefix}-cron",
            tags=project.common_tags,
        )

        # ---------------------------------------------------------------------
        # Create EventBridge Schedules per each cron job
        # ---------------------------------------------------------------------
        # Get worker security group for network config
        worker_sg = container_sgs.get("worker")
        worker_sg_id = worker_sg.resources["sg"].id if worker_sg else None

        # Get private subnet IDs
        private_subnet_ids = [s.id for s in private_subnets]

        for task_name, task_config in scheduled_tasks_config.items():
            schedule_expr = task_config.get("schedule_expression", "rate(1 day)")
            command = task_config.get("command", ["manage", "help"])
            description = task_config.get("description", f"Scheduled task: {task_name}")

            # Create the schedule
            aws.scheduler.Schedule(
                f"{project.name_prefix}-{task_name}",
                name=f"{project.name_prefix}-{task_name}",
                group_name=schedule_group.name,
                schedule_expression=schedule_expr,
                schedule_expression_timezone="UTC",
                description=description,
                flexible_time_window=aws.scheduler.ScheduleFlexibleTimeWindowArgs(
                    mode="OFF"  # Would run exactly at scheduled time
                ),
                target=aws.scheduler.ScheduleTargetArgs(
                    arn=f"arn:aws:ecs:{project.aws_region}:{project.aws_account_id}:cluster/{project.name_prefix}-worker-cluster",
                    role_arn=scheduler_role.arn,
                    ecs_parameters=aws.scheduler.ScheduleTargetEcsParametersArgs(
                        task_definition_arn=cron_task_definition.arn,
                        task_count=1,
                        launch_type="FARGATE",
                        platform_version="LATEST",
                        network_configuration=aws.scheduler.ScheduleTargetEcsParametersNetworkConfigurationArgs(
                            subnets=private_subnet_ids,
                            security_groups=[worker_sg_id] if worker_sg_id else [],
                            assign_public_ip=False,
                        ),
                    ),
                    input=json.dumps(
                        {"containerOverrides": [{"name": "cron", "command": command}]}
                    ),
                ),
                state=task_config.get("state", "DISABLED"),
                opts=pulumi.ResourceOptions(
                    parent=schedule_group,
                    depends_on=[cron_task_definition, scheduler_role],
                ),
            )

            pulumi.log.info(f"Scheduled task: {task_name} - {schedule_expr}")

        # Export scheduled task info
        pulumi.export("scheduled_tasks_count", len(scheduled_tasks_config))
        pulumi.export("cron_task_definition_arn", cron_task_definition.arn)
        pulumi.export("cron_schedule_group", schedule_group.name)

    # =========================================================================
    # Outputs
    # =========================================================================
    # Export useful values for reference
    if vpc_resource:
        pulumi.export("vpc_id", vpc_resource.id)
    if private_subnets:
        pulumi.export("private_subnet_ids", [s.id for s in private_subnets])
    if public_subnets:
        pulumi.export("public_subnet_ids", [s.id for s in public_subnets])


if __name__ == "__main__":
    main()
