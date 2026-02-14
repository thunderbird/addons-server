# Thunderbird Add-ons Infra (Pulumi)

ECS Fargate infrastructure for addons-server

## Prerequisites

- Python 3.13+
- Pulumi CLI
- AWS credentials (local credentials only; CI uses OIDC)

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pulumi login  # browser-based authn flow Pulumi Cloud

# Select the staging stack (name may vary depending on org setup)
pulumi stack select thunderbird/thunderbird-addons/stage
```

## Preview Changes

```bash
pulumi preview
```

## CI/CD

GitHub Actions workflow (`.github/workflows/build-and-push.yml`) handles image builds.

- **Pull requests**: Build validation only (no AWS auth)
- **Push to stage**: Build + push to ECR via OIDC

### Enabling ECR Publishing

1. Ensure AWS OIDC provider exists for `token.actions.githubusercontent.com`
2. IAM role is created by Pulumi with trust policy scoped to `refs/heads/stage`
3. Set repository variable: `AWS_ROLE_ARN` (from Pulumi output `gha_ecr_publish_role_arn`)

## Scheduled Tasks

Scheduled tasks mirror the existing cron workload from the legacy environment and are executed as ECS tasks via EventBridge Scheduler.

16 cron jobs run via EventBridge Scheduler:

| Task | Schedule | Command |
|------|----------|---------|
| auto-approve | Every 5 min | `manage auto_approve` |
| addon-last-updated | Hourly (:20) | `manage addon_last_updated` |
| info-request-warning | Hourly (:15) | `manage send_info_request_warning` |
| update-addon-appsupport | Hourly (:45) | `manage update_addon_appsupport` |
| cleanup-extracted-file | Hourly (:50) | `manage cleanup_extracted_file` |
| unhide-disabled-files | Hourly (:55) | `manage unhide_disabled_files` |
| hide-disabled-files | 05:25, 17:25 UTC | `manage hide_disabled_files` |
| cleanup-image-files | 06:25, 18:25 UTC | `manage cleanup_image_files` |
| update-user-ratings | 01:00 UTC | `manage update_user_ratings` |
| gc | 22:00 UTC | `manage gc` |
| dump-apps | 01:30 UTC | `manage dump_apps` |
| update-product-details | 01:45 UTC | `manage update_product_details` |
| add-latest-appversion | 02:00 UTC | `manage add_latest_appversion` |
| category-totals | 14:30 UTC | `manage category_totals` |
| update-global-totals | 00:40 UTC | `manage update_addon_hotness` |
| update-addon-daily-users | 00:20 UTC | `manage update_addon_daily_users` |

## Image Tagging

- `atn-stage-addons-server:stage-latest` - current stage build
- ECR lifecycle: keep 50 tagged images, expire untagged after 7 days

## Secrets

No secrets are stored in the repository.

Application expects Secrets Manager paths under `atn/stage/*`:
- Database credentials
- Django secret key
- External service API keys

See `settings_local_stage.py` for full mapping.

## Post-Deployment Verification

All commands are read-only

### ECR Repository

```bash
aws ecr describe-images \
  --repository-name atn-stage-addons-server \
  --region us-west-2 \
  --query 'imageDetails[*].[imageTags,imagePushedAt]' \
  --output table
```

### ECS Services

```bash
# List services
aws ecs list-services --cluster atn-stage-web-cluster --region us-west-2
aws ecs list-services --cluster atn-stage-worker-cluster --region us-west-2

# Check service status
aws ecs describe-services \
  --cluster atn-stage-web-cluster \
  --services atn-stage-web \
  --region us-west-2 \
  --query 'services[*].[serviceName,runningCount,desiredCount,status]'
```

### Scheduled Tasks

```bash
aws scheduler list-schedules \
  --group-name thunderbird-addons-stage-cron \
  --region us-west-2 \
  --query 'Schedules[*].[Name,State,ScheduleExpression]' \
  --output table
```

### CloudWatch Logs

```bash
# Recent web logs
aws logs tail /ecs/thunderbird-addons-stage-web --since 5m --region us-west-2

# Recent cron logs
aws logs tail /ecs/thunderbird-addons-stage-cron --since 5m --region us-west-2
```

### ALB Health Check

```bash
# Get ALB DNS (after deployment)
pulumi stack output --json | jq -r '.web_alb_dns'

# Test health endpoint
curl -I https://{alb-dns}/services/monitor
```

## Resources Created

- New VPC with public/private subnets across 3 AZs (connectivity to existing RDS may require VPC peering - confirm with Andrei)
- ECR repository with lifecycle policy
- ECS clusters (web, worker)
- Fargate services (web, worker, versioncheck)
- ElastiCache Redis cluster
- 16 EventBridge scheduled tasks
- ALB with HTTPS listener
- IAM roles (task execution, task, scheduler, OIDC)
- CloudWatch log groups

## Workflow

All infrastructure changes are proposed via pull requests and reviewed before deployment. Direct `pulumi up` execution is restricted to approved paths.
