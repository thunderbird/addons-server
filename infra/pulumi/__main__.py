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
import tb_pulumi.elasticache
import tb_pulumi.fargate
import tb_pulumi.network


def main():
    # Create a ThunderbirdPulumiProject to aggregate resources
    # This loads config.{stack}.yaml automatically
    project = tb_pulumi.ThunderbirdPulumiProject()

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
        ecr_repo = aws.ecr.Repository(
            f"{project.name_prefix}-{repo_name}",
            name=repo_config.get("name", f"{project.name_prefix}-{repo_name}"),
            image_tag_mutability=repo_config.get("image_tag_mutability", "MUTABLE"),
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
        workflow_file = gha_oidc_config.get("workflow_file", ".github/workflows/build-and-push.yml")

        # Build the subject conditions for allowed branches
        sub_conditions = [
            f"repo:{github_org}/{github_repo}:ref:refs/heads/{branch}"
            for branch in allowed_branches
        ]

        # Build workflow ref conditions (job_workflow_ref hardening)
        workflow_ref_conditions = [
            f"{github_org}/{github_repo}/{workflow_file}@refs/heads/{branch}"
            for branch in allowed_branches
        ]

        gha_trust_policy = json.dumps({
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
                            "token.actions.githubusercontent.com:iss": "https://token.actions.githubusercontent.com"
                        },
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": sub_conditions if len(sub_conditions) > 1 else sub_conditions[0],
                            "token.actions.githubusercontent.com:job_workflow_ref": workflow_ref_conditions if len(workflow_ref_conditions) > 1 else workflow_ref_conditions[0]
                        }
                    }
                }
            ]
        })

        gha_ecr_publish_role = aws.iam.Role(
            f"{project.name_prefix}-gha-ecr-publish",
            name=f"{project.name_prefix}-gha-ecr-publish",
            description=f"GitHub Actions OIDC role for ECR publishing ({github_org}/{github_repo})",
            assume_role_policy=gha_trust_policy,
            tags=project.common_tags,
        )

        # ECR push permissions derive ARN from actual repo to avoid drifts
        gha_ecr_policy_doc = addons_repo.arn.apply(lambda arn: json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ECRAuth",
                    "Effect": "Allow",
                    "Action": "ecr:GetAuthorizationToken",
                    "Resource": "*"
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
                        "ecr:UploadLayerPart"
                    ],
                    "Resource": arn
                }
            ]
        }))

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
    # Security Groups
    # =========================================================================
    sg_configs = resources.get("tb:network:SecurityGroupWithRules", {})
    security_groups = {}

    for sg_name, sg_config in sg_configs.items():
        if vpc_resource:
            sg_config["vpc_id"] = vpc_resource.id

        security_groups[sg_name] = tb_pulumi.network.SecurityGroupWithRules(
            name=f"{project.name_prefix}-{sg_name}",
            project=project,
            **sg_config,
        )

    # =========================================================================
    # Fargate Services
    # =========================================================================
    fargate_configs = resources.get("tb:fargate:FargateClusterWithLogging", {})
    fargate_services = {}

    for service_name, service_config in fargate_configs.items():
        # Inject subnet IDs based on whether service is internal or external
        is_internal = service_config.get("internal", True)
        subnets = private_subnets if is_internal else public_subnets

        if subnets:
            # Determine which security groups to apply
            if service_name == "web":
                container_sgs = [security_groups.get("web-sg")]
            elif service_name == "worker":
                container_sgs = [security_groups.get("worker-sg")]
            else:
                container_sgs = [security_groups.get("web-sg")]  # Default

            # Filter out None values
            container_sg_ids = [sg.resources["sg"].id for sg in container_sgs if sg is not None]

            fargate_services[service_name] = tb_pulumi.fargate.FargateClusterWithLogging(
                name=f"{project.name_prefix}-{service_name}",
                project=project,
                subnets=[s.id for s in subnets] if subnets else [],
                container_security_groups=container_sg_ids,
                load_balancer_security_groups=container_sg_ids if not is_internal else [],
                **service_config,
            )

    # =========================================================================
    # ElastiCache - Redis
    # =========================================================================
    elasticache_configs = resources.get("tb:elasticache:ElastiCacheReplicationGroup", {})
    elasticache_clusters = {}

    for cluster_name, cluster_config in elasticache_configs.items():
        if private_subnets:
            # Add source access from private subnets
            if "source_cidrs" not in cluster_config:
                cluster_config["source_cidrs"] = ["10.100.0.0/16"]  # VPC CIDR

            elasticache_clusters[cluster_name] = tb_pulumi.elasticache.ElastiCacheReplicationGroup(
                name=f"{project.name_prefix}-{cluster_name}",
                project=project,
                subnets=private_subnets,
                **cluster_config,
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
        task_execution_assume_role = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        })

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
        cron_secrets_policy_doc = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [
                    f"arn:aws:secretsmanager:{project.aws_region}:{project.aws_account_id}:secret:atn/stage/*"
                ]
            }]
        })

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

        # ---------------------------------------------------------------------
        # CloudWatch Log Group for cron tasks
        # ---------------------------------------------------------------------
        cron_log_group = aws.cloudwatch.LogGroup(
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
            lambda url: json.dumps([{
                "name": "cron",
                "image": f"{url}:stage-latest",
                "essential": True,
                "command": ["manage", "help"],  # Default; again overridden per schedule
                "environment": [
                    {"name": "DJANGO_SETTINGS_MODULE", "value": "settings"}
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/ecs/{project.name_prefix}-cron",
                        "awslogs-region": project.aws_region,
                        "awslogs-stream-prefix": "cron"
                    }
                }
            }])
        )

        cron_task_definition = aws.ecs.TaskDefinition(
            f"{project.name_prefix}-cron",
            family=f"{project.name_prefix}-cron",
            cpu="512",       # 0.5 vCPU - probably sufficient for management commands
            memory="1024",   # 1 GB
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            execution_role_arn=cron_execution_role.arn,
            task_role_arn=cron_task_role.arn,
            container_definitions=cron_container_def,
            tags=project.common_tags,
        )

        # ---------------------------------------------------------------------
        # EventBridge Scheduler IAM Role
        # ---------------------------------------------------------------------
        scheduler_assume_role_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        })

        scheduler_role = aws.iam.Role(
            f"{project.name_prefix}-scheduler-role",
            name=f"{project.name_prefix}-scheduler-role",
            assume_role_policy=scheduler_assume_role_policy,
            tags=project.common_tags,
        )

        # Policy for Scheduler to run ECS tasks and pass roles
        # TODO Evalulate PB policy on PassRole action
        scheduler_policy_doc = cron_task_definition.arn.apply(lambda task_arn: json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RunTask",
                    "Effect": "Allow",
                    "Action": ["ecs:RunTask"],
                    "Resource": [task_arn],
                    "Condition": {
                        "ArnLike": {
                            "ecs:cluster": f"arn:aws:ecs:{project.aws_region}:{project.aws_account_id}:cluster/{project.name_prefix}-worker-cluster"
                        }
                    }
                },
                {
                    "Sid": "PassRole",
                    "Effect": "Allow",
                    "Action": ["iam:PassRole"],
                    "Resource": ["*"],
                    "Condition": {
                        "StringLike": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                    }
                }
            ]
        }))

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
        worker_sg = security_groups.get("worker-sg")
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
                    input=json.dumps({
                        "containerOverrides": [{
                            "name": "cron",
                            "command": command
                        }]
                    }),
                ),
                state="ENABLED",
                opts=pulumi.ResourceOptions(
                    parent=schedule_group,
                    depends_on=[cron_task_definition, scheduler_role]
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
