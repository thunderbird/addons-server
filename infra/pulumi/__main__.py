#!/usr/bin/env python3
"""
Thunderbird Add-ons Server Infra

This Pulumi program aims to define the AWS infra for the Thunderbird Add-ons
server (ATN), migrating from EC2/Ansible to ECS Fargate

Architecture:
    - VPC with public/private subnets
    - ECR repository for container images
    - Fargate services: web, worker, versioncheck
    - ElastiCache Redis for Celery result backend
    - Amazon MQ RabbitMQ for Celery broker
    - EFS for add-on storage (dedicated)
    - (Future) RDS MySQL, OpenSearch

Usage:
    pulumi preview  # See planned changes
    pulumi up       # Apply changes

    The AWS region is pinned in Pulumi.stage.yaml (required by
    pulumi-aws 6.65.0 to avoid a provider diff bug, pulumi/pulumi-aws#5652).
    Ensure the correct stack is selected before running commands.

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
        #   Redis, ES/OpenSearch
        # sg-5133b52c (default VPC SG):
        #   RDS MySQL, Memcached (ENI lookup confirmed cluster uses this SG)
        #
        # We add our VPC CIDR to both SGs for the relevant ports.
        # EFS was originally in this list but moved to a dedicated stage
        # filesystem with its own SG in the ECS VPC (see aws:efs:FileSystem).

        # --- sg-d5539ea9: services SG (Redis, ES) ---
        default_vpc_ingress_cfg = resources.get("tb:network:DefaultVpcIngressRules", {})
        stage_vpc_cidr = default_vpc_ingress_cfg.get("stage_vpc_cidr", "10.100.0.0/16")

        services_sg_ids = default_vpc_ingress_cfg.get(
            "services_sg_ids",
            ["sg-d5539ea9"],
        )
        services_sg_ports = {
            "redis": 6379,
            "elasticsearch": 9200,
            "elasticsearch-https": 443,  # Managed AWS ES speaks HTTPS
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

        # --- sg-5133b52c: default VPC SG (RDS, Memcached) ---
        # Note: RabbitMQ (5672) was removed after the broker isolation
        # incident (issue #375). The stage broker is now a dedicated
        # Amazon MQ instance in the ECS VPC with its own SG
        default_sg_ids = default_vpc_ingress_cfg.get(
            "default_sg_ids",
            ["sg-5133b52c"],
        )
        default_sg_ports = {
            "mysql": 3306,
            "memcached": 11211,
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
    # EFS Filesystem (dedicated stage storage)
    # =========================================================================
    # Dedicated stage filesystem, replacing the original plan to mount the
    # shared filesystem. AWS EFS restricts mount targets to a single VPC
    # per filesystem, so sharing fs-55e85afc with Fargate tasks (ECS VPC)
    # is actually not possible.
    #
    # This follows the isolation model from issue #375: stage gets dedicated
    # resources rather than sharing infrastructure. Non-stage data is in fact
    # available on demand via AWS DataSync (one-way, prod -> stage)
    #
    # NFS SG: allows TCP 2049 inbound only from the container SGs that
    # actually need filesystem access (web + worker; versioncheck excluded
    # per existing Ansible config efs: false)
    efs_config = resources.get("aws:efs:FileSystem", {})
    efs_mount_targets = []
    efs_filesystem_id = None

    if efs_config and private_subnets and vpc_resource:
        efs_filesystem = aws.efs.FileSystem(
            f"{project.name_prefix}-efs",
            encrypted=efs_config.get("encrypted", True),
            performance_mode=efs_config.get("performance_mode", "generalPurpose"),
            throughput_mode=efs_config.get("throughput_mode", "bursting"),
            lifecycle_policies=[
                aws.efs.FileSystemLifecyclePolicyArgs(
                    transition_to_ia=lp["transition_to_ia"],
                )
                for lp in efs_config.get("lifecycle_policies", [])
            ],
            tags={
                **project.common_tags,
                "Name": f"{project.name_prefix}-efs",
            },
        )
        efs_filesystem_id = efs_filesystem.id

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

        pulumi.export("efs_filesystem_id", efs_filesystem_id)
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

            # Inject EFS filesystem ID into any volume configs that declare
            # an efs_volume_configuration without a file_system_id
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
    # Amazon MQ - RabbitMQ (stage-only Celery broker)
    # =========================================================================
    # Dedicated stage broker replacing the production EC2 RabbitMQ that
    # atn/stage/celery_broker previously pointed to (issue #375)
    mq_config = resources.get("aws:mq:RabbitMQBroker", {})
    mq_broker = None

    if mq_config and private_subnets and vpc_resource:
        mq_creds_secret_name = mq_config.get("credentials_secret_name")
        mq_creds_raw = aws.secretsmanager.get_secret_version(
            secret_id=mq_creds_secret_name,
        )
        mq_creds = json.loads(mq_creds_raw.secret_string)
        mq_username = mq_creds["username"]
        mq_password = pulumi.Output.secret(mq_creds["password"])

        # SG for the broker: AMQPS (5671) from container SGs,
        # management API (15671) from VPC CIDR for post-deploy bootstrap
        mq_sg = aws.ec2.SecurityGroup(
            f"{project.name_prefix}-mq-sg",
            name=f"{project.name_prefix}-mq",
            description="Amazon MQ RabbitMQ broker - AMQPS from Fargate containers",
            vpc_id=vpc_resource.id,
            tags={
                **project.common_tags,
                "Name": f"{project.name_prefix}-mq",
            },
        )

        mq_ingress_services = mq_config.get("ingress_from_services", ["web", "worker"])
        for svc_name in mq_ingress_services:
            cont_sg = container_sgs.get(svc_name)
            if cont_sg:
                aws.ec2.SecurityGroupRule(
                    f"{project.name_prefix}-mq-amqps-from-{svc_name}",
                    type="ingress",
                    security_group_id=mq_sg.id,
                    from_port=5671,
                    to_port=5671,
                    protocol="tcp",
                    source_security_group_id=cont_sg.resources["sg"].id,
                    description=f"AMQPS from {svc_name} containers",
                )

        aws.ec2.SecurityGroupRule(
            f"{project.name_prefix}-mq-mgmt-from-vpc",
            type="ingress",
            security_group_id=mq_sg.id,
            from_port=15671,
            to_port=15671,
            protocol="tcp",
            cidr_blocks=[vpc_config.get("cidr_block", "10.100.0.0/16")],
            description="RabbitMQ management API from VPC (post-deploy bootstrap)",
        )

        aws.ec2.SecurityGroupRule(
            f"{project.name_prefix}-mq-egress",
            type="egress",
            security_group_id=mq_sg.id,
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            description="Allow all outbound",
        )

        mq_broker = aws.mq.Broker(
            f"{project.name_prefix}-mq-broker",
            broker_name=mq_config.get("broker_name", f"{project.name_prefix}-rabbitmq"),
            engine_type="RabbitMQ",  # AWS here returns mixed case; must match to avoid perpetual diff
            engine_version=mq_config.get("engine_version", "3.13"),
            host_instance_type=mq_config.get("host_instance_type", "mq.t3.micro"),
            deployment_mode=mq_config.get("deployment_mode", "SINGLE_INSTANCE"),
            publicly_accessible=mq_config.get("publicly_accessible", False),
            auto_minor_version_upgrade=mq_config.get(
                "auto_minor_version_upgrade", True
            ),
            security_groups=[mq_sg.id],
            subnet_ids=[private_subnets[0].id],
            maintenance_window_start_time=aws.mq.BrokerMaintenanceWindowStartTimeArgs(
                day_of_week=mq_config.get("maintenance_day", "SUNDAY"),
                time_of_day=mq_config.get("maintenance_hour", "06:00"),
                time_zone="UTC",
            ),
            users=[
                aws.mq.BrokerUserArgs(
                    username=mq_username,
                    password=mq_password,
                    console_access=True,
                ),
            ],
            tags={
                **project.common_tags,
                "Name": mq_config.get("broker_name", f"{project.name_prefix}-rabbitmq"),
            },
            opts=pulumi.ResourceOptions(depends_on=[mq_sg]),
        )

        pulumi.export("mq_broker_id", mq_broker.id)
        pulumi.export("mq_broker_arn", mq_broker.arn)
        pulumi.export(
            "mq_broker_amqps_endpoints",
            mq_broker.instances.apply(
                lambda instances: [
                    ep
                    for inst in (instances or [])
                    for ep in (inst.endpoints or [])
                    if "amqps" in ep
                ]
            ),
        )
        pulumi.export(
            "mq_broker_console_url",
            mq_broker.instances.apply(
                lambda instances: [
                    ep
                    for inst in (instances or [])
                    for ep in (inst.endpoints or [])
                    if "https" in ep
                ]
            ),
        )

    # =========================================================================
    # Monitoring and Alarms (prod-gating baseline)
    # =========================================================================
    # Phase 1 observability: SNS notification path, CloudWatch alarms for
    # ALB/TG/ECS/MQ/Redis, and one operational dashboard
    #
    # All alarms are written explicitly (not via CloudWatchMonitoringGroup)
    # for a single SNS topic, full control over alarm descriptions, and
    # correct metric names (upstream tb_pulumi has a target_5xx metric bug)
    #
    # Thresholds live in config.stage.yaml under resources.monitoring.alarms
    monitoring_cfg = resources.get("monitoring", {})
    alarm_cfg = monitoring_cfg.get("alarms", {})

    if monitoring_cfg and fargate_services:
        notify_secret_name = monitoring_cfg.get("notify_emails_secret_name")
        notify_emails = []
        if notify_secret_name:
            notify_emails_raw = aws.secretsmanager.get_secret_version(
                secret_id=notify_secret_name,
            )
            notify_emails = [
                e.strip()
                for e in notify_emails_raw.secret_string.split(",")
                if e.strip()
            ]

        # -----------------------------------------------------------------
        # SNS topic + email subscriptions
        # -----------------------------------------------------------------
        alarm_topic = aws.sns.Topic(
            f"{project.name_prefix}-alarm-topic",
            name=f"{project.name_prefix}-alarms",
            tags={
                **project.common_tags,
                "Name": f"{project.name_prefix}-alarms",
            },
        )

        for idx, email in enumerate(notify_emails):
            aws.sns.TopicSubscription(
                f"{project.name_prefix}-alarm-sub-{idx}",
                protocol="email",
                endpoint=email,
                topic=alarm_topic.arn,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic]),
            )

        # -----------------------------------------------------------------
        # ALB alarms (web, versioncheck)
        # -----------------------------------------------------------------
        alb_cfg = alarm_cfg.get("alb", {})
        alb_error_threshold = alb_cfg.get("error_threshold", 10)
        alb_error_period = alb_cfg.get("error_period", 60)
        alb_rt_threshold = alb_cfg.get("response_time_threshold", 1)
        alb_rt_period = alb_cfg.get("response_time_period", 60)
        alb_eval_periods = alb_cfg.get("evaluation_periods", 2)

        for svc_name in ["web", "versioncheck"]:
            fargate_svc = fargate_services.get(svc_name)
            if not fargate_svc:
                continue
            svc_alb = fargate_svc.resources.get("fargate_service_alb")
            if not svc_alb:
                continue
            alb = svc_alb.resources["albs"].get(svc_name)
            if not alb:
                continue

            lb_suffix = alb.arn_suffix

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-{svc_name}-alb-5xx",
                name=f"{project.name_prefix}-{svc_name}-alb-5xx",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"LoadBalancer": lb_suffix},
                metric_name="HTTPCode_ELB_5XX_Count",
                namespace="AWS/ApplicationELB",
                statistic="Sum",
                threshold=alb_error_threshold,
                period=alb_error_period,
                evaluation_periods=alb_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Elevated 5xx errors on the {svc_name} ALB. "
                    "Check: ECS task health in console, then "
                    "application logs in CloudWatch for stack traces."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, alb]),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-{svc_name}-target-5xx",
                name=f"{project.name_prefix}-{svc_name}-target-5xx",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"LoadBalancer": lb_suffix},
                metric_name="HTTPCode_Target_5XX_Count",
                namespace="AWS/ApplicationELB",
                statistic="Sum",
                threshold=alb_error_threshold,
                period=alb_error_period,
                evaluation_periods=alb_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Elevated 5xx errors from {svc_name} application targets. "
                    "Check: application logs for exceptions, database "
                    "connectivity, and upstream dependency health."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, alb]),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-{svc_name}-response-time",
                name=f"{project.name_prefix}-{svc_name}-response-time",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"LoadBalancer": lb_suffix},
                metric_name="TargetResponseTime",
                namespace="AWS/ApplicationELB",
                statistic="Average",
                threshold=alb_rt_threshold,
                period=alb_rt_period,
                evaluation_periods=alb_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Average response time above {alb_rt_threshold}s on {svc_name}. "
                    "Check: is traffic elevated? Are database queries slow? "
                    "Is Memcached reachable?"
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, alb]),
            )

        # -----------------------------------------------------------------
        # Target group alarms (web, versioncheck)
        # -----------------------------------------------------------------
        tg_cfg = alarm_cfg.get("target_group", {})
        tg_unhealthy_threshold = tg_cfg.get("unhealthy_threshold", 1)
        tg_period = tg_cfg.get("period", 60)
        tg_eval_periods = tg_cfg.get("evaluation_periods", 2)

        for svc_name in ["web", "versioncheck"]:
            fargate_svc = fargate_services.get(svc_name)
            if not fargate_svc:
                continue
            svc_alb = fargate_svc.resources.get("fargate_service_alb")
            if not svc_alb:
                continue
            alb = svc_alb.resources["albs"].get(svc_name)
            tg = svc_alb.resources["target_groups"].get(svc_name)
            if not alb or not tg:
                continue

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-{svc_name}-unhealthy-hosts",
                name=f"{project.name_prefix}-{svc_name}-unhealthy-hosts",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={
                    "TargetGroup": tg.arn_suffix,
                    "LoadBalancer": alb.arn_suffix,
                },
                metric_name="UnHealthyHostCount",
                namespace="AWS/ApplicationELB",
                statistic="Average",
                threshold=tg_unhealthy_threshold,
                period=tg_period,
                evaluation_periods=tg_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Unhealthy hosts detected in {svc_name} target group. "
                    "Check: ECS task status, health check endpoint "
                    "(/services/monitor.json), container logs."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, tg]),
            )

        # -----------------------------------------------------------------
        # ECS service alarms (web, worker, versioncheck)
        # -----------------------------------------------------------------
        ecs_cfg = alarm_cfg.get("ecs", {})
        ecs_cpu_threshold = ecs_cfg.get("cpu_threshold", 80)
        ecs_mem_threshold = ecs_cfg.get("memory_threshold", 80)
        ecs_period = ecs_cfg.get("period", 300)
        ecs_eval_periods = ecs_cfg.get("evaluation_periods", 2)

        for svc_name, fargate_svc in fargate_services.items():
            ecs_service = fargate_svc.resources.get("service")
            ecs_cluster = fargate_svc.resources.get("cluster")
            if not ecs_service or not ecs_cluster:
                continue

            cluster_name = ecs_cluster.arn.apply(lambda arn: arn.split("/")[-1])
            service_name = ecs_service.name

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-{svc_name}-ecs-cpu",
                name=f"{project.name_prefix}-{svc_name}-ecs-cpu",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={
                    "ClusterName": cluster_name,
                    "ServiceName": service_name,
                },
                metric_name="CPUUtilization",
                namespace="AWS/ECS",
                statistic="Average",
                threshold=ecs_cpu_threshold,
                period=ecs_period,
                evaluation_periods=ecs_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"CPU utilisation above {ecs_cpu_threshold}% on {svc_name} service. "
                    "Check: is traffic elevated? Are tasks stuck? "
                    "Consider scaling if sustained."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, ecs_service]),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-{svc_name}-ecs-memory",
                name=f"{project.name_prefix}-{svc_name}-ecs-memory",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={
                    "ClusterName": cluster_name,
                    "ServiceName": service_name,
                },
                metric_name="MemoryUtilization",
                namespace="AWS/ECS",
                statistic="Average",
                threshold=ecs_mem_threshold,
                period=ecs_period,
                evaluation_periods=ecs_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Memory utilisation above {ecs_mem_threshold}% on {svc_name} service. "
                    "Check: application memory leaks, task resource limits, "
                    "consider scaling."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, ecs_service]),
            )

        # -----------------------------------------------------------------
        # Amazon MQ alarms
        # -----------------------------------------------------------------
        mq_cfg = alarm_cfg.get("mq", {})

        if mq_broker is not None:
            mq_queue_name = mq_cfg.get("queue_name", "olympia")
            mq_vhost = mq_cfg.get("virtual_host", "/")
            mq_msg_threshold = mq_cfg.get("message_ready_threshold", 1000)
            mq_consumer_alarm_enabled = mq_cfg.get("consumer_alarm_enabled", False)
            mq_consumer_threshold = mq_cfg.get("consumer_count_threshold", 1)
            mq_cpu_threshold = mq_cfg.get("cpu_threshold", 80)
            mq_mem_threshold = mq_cfg.get("memory_bytes_threshold", 512000000)
            mq_period = mq_cfg.get("period", 300)
            mq_eval_periods = mq_cfg.get("evaluation_periods", 2)

            broker_id = mq_broker.id

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-mq-message-ready",
                name=f"{project.name_prefix}-mq-message-ready",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={
                    "Broker": broker_id,
                    "VirtualHost": mq_vhost,
                    "Queue": mq_queue_name,
                },
                metric_name="MessageReadyCount",
                namespace="AWS/AmazonMQ",
                statistic="Average",
                threshold=mq_msg_threshold,
                period=mq_period,
                evaluation_periods=mq_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Queue '{mq_queue_name}' has over {mq_msg_threshold} "
                    "ready messages. Check: is the worker consuming? "
                    "Are tasks backing up? Check worker logs."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, mq_broker]),
            )

            if mq_consumer_alarm_enabled:
                aws.cloudwatch.MetricAlarm(
                    f"{project.name_prefix}-mq-consumer-count",
                    name=f"{project.name_prefix}-mq-consumer-count",
                    alarm_actions=[alarm_topic.arn],
                    ok_actions=[alarm_topic.arn],
                    comparison_operator="LessThanThreshold",
                    dimensions={
                        "Broker": broker_id,
                        "VirtualHost": mq_vhost,
                        "Queue": mq_queue_name,
                    },
                    metric_name="ConsumerCount",
                    namespace="AWS/AmazonMQ",
                    statistic="Minimum",
                    threshold=mq_consumer_threshold,
                    period=mq_period,
                    evaluation_periods=mq_eval_periods,
                    treat_missing_data="breaching",
                    alarm_description=(
                        f"No consumers connected to the '{mq_queue_name}' queue. "
                        "Check: is the worker service running? "
                        "Check worker logs for connection errors."
                    ),
                    tags=project.common_tags,
                    opts=pulumi.ResourceOptions(depends_on=[alarm_topic, mq_broker]),
                )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-mq-cpu",
                name=f"{project.name_prefix}-mq-cpu",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"Broker": broker_id},
                metric_name="SystemCpuUtilization",
                namespace="AWS/AmazonMQ",
                statistic="Average",
                threshold=mq_cpu_threshold,
                period=mq_period,
                evaluation_periods=mq_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Broker CPU above {mq_cpu_threshold}%. Check: "
                    "queue depth, message throughput, consider "
                    "upgrading instance type if sustained."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, mq_broker]),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-mq-memory",
                name=f"{project.name_prefix}-mq-memory",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"Broker": broker_id},
                metric_name="RabbitMQMemUsed",
                namespace="AWS/AmazonMQ",
                statistic="Average",
                threshold=mq_mem_threshold,
                period=mq_period,
                evaluation_periods=mq_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Broker memory above {mq_mem_threshold} bytes. "
                    "Check: queue depth and message sizes, consider "
                    "purging stale queues or upgrading instance."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic, mq_broker]),
            )

        # -----------------------------------------------------------------
        # Redis alarms
        # -----------------------------------------------------------------
        redis_cfg = alarm_cfg.get("redis", {})
        redis_cluster = elasticache_clusters.get("redis")

        if redis_cluster:
            redis_mem_threshold = redis_cfg.get("memory_pct_threshold", 80)
            redis_eviction_threshold = redis_cfg.get("eviction_threshold", 100)
            redis_cpu_threshold = redis_cfg.get("cpu_threshold", 80)
            redis_host_cpu_threshold = redis_cfg.get("host_cpu_threshold", 90)
            redis_conn_threshold = redis_cfg.get("connection_threshold", 500)
            redis_period = redis_cfg.get("period", 300)
            redis_eval_periods = redis_cfg.get("evaluation_periods", 2)

            replication_group = redis_cluster.resources["replication_group"]
            cache_cluster_id = replication_group.id.apply(lambda rg_id: f"{rg_id}-001")

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-redis-memory",
                name=f"{project.name_prefix}-redis-memory",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"CacheClusterId": cache_cluster_id},
                metric_name="DatabaseMemoryUsagePercentage",
                namespace="AWS/ElastiCache",
                statistic="Average",
                threshold=redis_mem_threshold,
                period=redis_period,
                evaluation_periods=redis_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Redis memory usage above {redis_mem_threshold}%. "
                    "Check: eviction count, key count growth, "
                    "potential memory leak in application cache usage."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(
                    depends_on=[alarm_topic, replication_group]
                ),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-redis-evictions",
                name=f"{project.name_prefix}-redis-evictions",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"CacheClusterId": cache_cluster_id},
                metric_name="Evictions",
                namespace="AWS/ElastiCache",
                statistic="Sum",
                threshold=redis_eviction_threshold,
                period=redis_period,
                evaluation_periods=redis_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Redis evictions above {redis_eviction_threshold} "
                    "per period. Check: memory usage, maxmemory-policy, "
                    "whether the application is over-caching."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(
                    depends_on=[alarm_topic, replication_group]
                ),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-redis-cpu",
                name=f"{project.name_prefix}-redis-cpu",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"CacheClusterId": cache_cluster_id},
                metric_name="EngineCPUUtilization",
                namespace="AWS/ElastiCache",
                statistic="Average",
                threshold=redis_cpu_threshold,
                period=redis_period,
                evaluation_periods=redis_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Redis engine CPU above {redis_cpu_threshold}%. "
                    "Check: command complexity (KEYS, SORT), "
                    "connection count, consider node upgrade."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(
                    depends_on=[alarm_topic, replication_group]
                ),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-redis-connections",
                name=f"{project.name_prefix}-redis-connections",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"CacheClusterId": cache_cluster_id},
                metric_name="CurrConnections",
                namespace="AWS/ElastiCache",
                statistic="Average",
                threshold=redis_conn_threshold,
                period=redis_period,
                evaluation_periods=redis_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Redis connections above {redis_conn_threshold}. "
                    "Check: connection pool settings, task/service "
                    "count, potential connection leaks."
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(
                    depends_on=[alarm_topic, replication_group]
                ),
            )

            aws.cloudwatch.MetricAlarm(
                f"{project.name_prefix}-redis-host-cpu",
                name=f"{project.name_prefix}-redis-host-cpu",
                alarm_actions=[alarm_topic.arn],
                ok_actions=[alarm_topic.arn],
                comparison_operator="GreaterThanOrEqualToThreshold",
                dimensions={"CacheClusterId": cache_cluster_id},
                metric_name="CPUUtilization",
                namespace="AWS/ElastiCache",
                statistic="Average",
                threshold=redis_host_cpu_threshold,
                period=redis_period,
                evaluation_periods=redis_eval_periods,
                treat_missing_data="notBreaching",
                alarm_description=(
                    f"Redis host CPU above {redis_host_cpu_threshold}%. "
                    "This monitors the underlying host, not just the Redis "
                    "engine. On nodes with <= 2 vCPUs, EngineCPUUtilization "
                    "alone can miss host overload. Check: background processes, "
                    "node type, consider upgrading"
                ),
                tags=project.common_tags,
                opts=pulumi.ResourceOptions(
                    depends_on=[alarm_topic, replication_group]
                ),
            )

        # -----------------------------------------------------------------
        # CloudWatch Dashboard
        # -----------------------------------------------------------------
        dash_cfg = monitoring_cfg.get("dashboard", {})
        dash_period = dash_cfg.get("period", 300)

        dashboard_outputs = {}
        for svc_name in ["web", "versioncheck"]:
            fargate_svc = fargate_services.get(svc_name)
            if fargate_svc:
                svc_alb = fargate_svc.resources.get("fargate_service_alb")
                if svc_alb:
                    alb = svc_alb.resources["albs"].get(svc_name)
                    if alb:
                        dashboard_outputs[f"{svc_name}_alb_suffix"] = alb.arn_suffix
                svc_res = fargate_svc.resources.get("service")
                cluster_res = fargate_svc.resources.get("cluster")
                if svc_res:
                    dashboard_outputs[f"{svc_name}_svc_name"] = svc_res.name
                if cluster_res:
                    dashboard_outputs[f"{svc_name}_cluster"] = cluster_res.arn.apply(
                        lambda arn: arn.split("/")[-1]
                    )

        worker_svc = fargate_services.get("worker")
        if worker_svc:
            svc_res = worker_svc.resources.get("service")
            cluster_res = worker_svc.resources.get("cluster")
            if svc_res:
                dashboard_outputs["worker_svc_name"] = svc_res.name
            if cluster_res:
                dashboard_outputs["worker_cluster"] = cluster_res.arn.apply(
                    lambda arn: arn.split("/")[-1]
                )

        if mq_broker is not None:
            dashboard_outputs["mq_broker_id"] = mq_broker.id

        if redis_cluster:
            dashboard_outputs["redis_cluster_id"] = redis_cluster.resources[
                "replication_group"
            ].id.apply(lambda rg_id: f"{rg_id}-001")

        mq_queue = alarm_cfg.get("mq", {}).get("queue_name", "olympia")
        mq_vhost_dash = alarm_cfg.get("mq", {}).get("virtual_host", "/")
        region = project.aws_region

        if dashboard_outputs:
            dashboard_body = pulumi.Output.all(**dashboard_outputs).apply(
                lambda o: json.dumps(
                    {
                        "widgets": [
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 0,
                                        "y": 0,
                                        "width": 12,
                                        "height": 6,
                                        "properties": {
                                            "title": "Web ALB - Requests and Errors",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ApplicationELB",
                                                    "RequestCount",
                                                    "LoadBalancer",
                                                    o["web_alb_suffix"],
                                                    {"stat": "Sum"},
                                                ],
                                                [
                                                    "AWS/ApplicationELB",
                                                    "HTTPCode_ELB_5XX_Count",
                                                    "LoadBalancer",
                                                    o["web_alb_suffix"],
                                                    {"stat": "Sum"},
                                                ],
                                                [
                                                    "AWS/ApplicationELB",
                                                    "HTTPCode_Target_5XX_Count",
                                                    "LoadBalancer",
                                                    o["web_alb_suffix"],
                                                    {"stat": "Sum"},
                                                ],
                                                [
                                                    "AWS/ApplicationELB",
                                                    "TargetResponseTime",
                                                    "LoadBalancer",
                                                    o["web_alb_suffix"],
                                                    {
                                                        "stat": "Average",
                                                        "yAxis": "right",
                                                    },
                                                ],
                                            ],
                                            "yAxis": {
                                                "right": {
                                                    "label": "Seconds",
                                                    "showUnits": False,
                                                }
                                            },
                                        },
                                    }
                                ]
                                if "web_alb_suffix" in o
                                else []
                            ),
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 12,
                                        "y": 0,
                                        "width": 12,
                                        "height": 6,
                                        "properties": {
                                            "title": "Versioncheck ALB - Requests and Errors",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ApplicationELB",
                                                    "RequestCount",
                                                    "LoadBalancer",
                                                    o["versioncheck_alb_suffix"],
                                                    {"stat": "Sum"},
                                                ],
                                                [
                                                    "AWS/ApplicationELB",
                                                    "HTTPCode_ELB_5XX_Count",
                                                    "LoadBalancer",
                                                    o["versioncheck_alb_suffix"],
                                                    {"stat": "Sum"},
                                                ],
                                                [
                                                    "AWS/ApplicationELB",
                                                    "HTTPCode_Target_5XX_Count",
                                                    "LoadBalancer",
                                                    o["versioncheck_alb_suffix"],
                                                    {"stat": "Sum"},
                                                ],
                                                [
                                                    "AWS/ApplicationELB",
                                                    "TargetResponseTime",
                                                    "LoadBalancer",
                                                    o["versioncheck_alb_suffix"],
                                                    {
                                                        "stat": "Average",
                                                        "yAxis": "right",
                                                    },
                                                ],
                                            ],
                                            "yAxis": {
                                                "right": {
                                                    "label": "Seconds",
                                                    "showUnits": False,
                                                }
                                            },
                                        },
                                    }
                                ]
                                if "versioncheck_alb_suffix" in o
                                else []
                            ),
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 0,
                                        "y": 6,
                                        "width": 8,
                                        "height": 6,
                                        "properties": {
                                            "title": "Web ECS - CPU and Memory",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ECS",
                                                    "CPUUtilization",
                                                    "ClusterName",
                                                    o["web_cluster"],
                                                    "ServiceName",
                                                    o["web_svc_name"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/ECS",
                                                    "MemoryUtilization",
                                                    "ClusterName",
                                                    o["web_cluster"],
                                                    "ServiceName",
                                                    o["web_svc_name"],
                                                    {"stat": "Average"},
                                                ],
                                            ],
                                        },
                                    }
                                ]
                                if "web_cluster" in o and "web_svc_name" in o
                                else []
                            ),
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 8,
                                        "y": 6,
                                        "width": 8,
                                        "height": 6,
                                        "properties": {
                                            "title": "Worker ECS - CPU and Memory",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ECS",
                                                    "CPUUtilization",
                                                    "ClusterName",
                                                    o["worker_cluster"],
                                                    "ServiceName",
                                                    o["worker_svc_name"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/ECS",
                                                    "MemoryUtilization",
                                                    "ClusterName",
                                                    o["worker_cluster"],
                                                    "ServiceName",
                                                    o["worker_svc_name"],
                                                    {"stat": "Average"},
                                                ],
                                            ],
                                        },
                                    }
                                ]
                                if "worker_cluster" in o and "worker_svc_name" in o
                                else []
                            ),
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 16,
                                        "y": 6,
                                        "width": 8,
                                        "height": 6,
                                        "properties": {
                                            "title": "Versioncheck ECS - CPU and Memory",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ECS",
                                                    "CPUUtilization",
                                                    "ClusterName",
                                                    o["versioncheck_cluster"],
                                                    "ServiceName",
                                                    o["versioncheck_svc_name"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/ECS",
                                                    "MemoryUtilization",
                                                    "ClusterName",
                                                    o["versioncheck_cluster"],
                                                    "ServiceName",
                                                    o["versioncheck_svc_name"],
                                                    {"stat": "Average"},
                                                ],
                                            ],
                                        },
                                    }
                                ]
                                if "versioncheck_cluster" in o
                                and "versioncheck_svc_name" in o
                                else []
                            ),
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 0,
                                        "y": 12,
                                        "width": 12,
                                        "height": 6,
                                        "properties": {
                                            "title": "Amazon MQ - Queue Health",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/AmazonMQ",
                                                    "MessageReadyCount",
                                                    "Broker",
                                                    o["mq_broker_id"],
                                                    "VirtualHost",
                                                    mq_vhost_dash,
                                                    "Queue",
                                                    mq_queue,
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/AmazonMQ",
                                                    "MessageUnacknowledgedCount",
                                                    "Broker",
                                                    o["mq_broker_id"],
                                                    "VirtualHost",
                                                    mq_vhost_dash,
                                                    "Queue",
                                                    mq_queue,
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/AmazonMQ",
                                                    "ConsumerCount",
                                                    "Broker",
                                                    o["mq_broker_id"],
                                                    "VirtualHost",
                                                    mq_vhost_dash,
                                                    "Queue",
                                                    mq_queue,
                                                    {
                                                        "stat": "Minimum",
                                                        "yAxis": "right",
                                                    },
                                                ],
                                            ],
                                            "yAxis": {
                                                "right": {
                                                    "label": "Consumers",
                                                    "showUnits": False,
                                                }
                                            },
                                        },
                                    },
                                    {
                                        "type": "metric",
                                        "x": 12,
                                        "y": 12,
                                        "width": 12,
                                        "height": 6,
                                        "properties": {
                                            "title": "Amazon MQ - Broker Resources",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/AmazonMQ",
                                                    "SystemCpuUtilization",
                                                    "Broker",
                                                    o["mq_broker_id"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/AmazonMQ",
                                                    "RabbitMQMemUsed",
                                                    "Broker",
                                                    o["mq_broker_id"],
                                                    {
                                                        "stat": "Average",
                                                        "yAxis": "right",
                                                    },
                                                ],
                                            ],
                                            "yAxis": {
                                                "right": {
                                                    "label": "Bytes",
                                                    "showUnits": False,
                                                }
                                            },
                                        },
                                    },
                                ]
                                if "mq_broker_id" in o
                                else []
                            ),
                            *(
                                [
                                    {
                                        "type": "metric",
                                        "x": 0,
                                        "y": 18,
                                        "width": 12,
                                        "height": 6,
                                        "properties": {
                                            "title": "Redis - Memory and Evictions",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ElastiCache",
                                                    "DatabaseMemoryUsagePercentage",
                                                    "CacheClusterId",
                                                    o["redis_cluster_id"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/ElastiCache",
                                                    "Evictions",
                                                    "CacheClusterId",
                                                    o["redis_cluster_id"],
                                                    {"stat": "Sum", "yAxis": "right"},
                                                ],
                                            ],
                                            "yAxis": {
                                                "right": {
                                                    "label": "Count",
                                                    "showUnits": False,
                                                }
                                            },
                                        },
                                    },
                                    {
                                        "type": "metric",
                                        "x": 12,
                                        "y": 18,
                                        "width": 12,
                                        "height": 6,
                                        "properties": {
                                            "title": "Redis - CPU and Connections",
                                            "region": region,
                                            "period": dash_period,
                                            "metrics": [
                                                [
                                                    "AWS/ElastiCache",
                                                    "EngineCPUUtilization",
                                                    "CacheClusterId",
                                                    o["redis_cluster_id"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/ElastiCache",
                                                    "CPUUtilization",
                                                    "CacheClusterId",
                                                    o["redis_cluster_id"],
                                                    {"stat": "Average"},
                                                ],
                                                [
                                                    "AWS/ElastiCache",
                                                    "CurrConnections",
                                                    "CacheClusterId",
                                                    o["redis_cluster_id"],
                                                    {
                                                        "stat": "Average",
                                                        "yAxis": "right",
                                                    },
                                                ],
                                            ],
                                            "yAxis": {
                                                "right": {
                                                    "label": "Connections",
                                                    "showUnits": False,
                                                }
                                            },
                                        },
                                    },
                                ]
                                if "redis_cluster_id" in o
                                else []
                            ),
                        ],
                    }
                )
            )

            aws.cloudwatch.Dashboard(
                f"{project.name_prefix}-dashboard",
                dashboard_name=f"{project.name_prefix}-health",
                dashboard_body=dashboard_body,
                opts=pulumi.ResourceOptions(depends_on=[alarm_topic]),
            )

        # -----------------------------------------------------------------
        # Monitoring exports
        # -----------------------------------------------------------------
        pulumi.export("monitoring_sns_topic_arn", alarm_topic.arn)
        pulumi.export(
            "monitoring_dashboard_name",
            f"{project.name_prefix}-health",
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
