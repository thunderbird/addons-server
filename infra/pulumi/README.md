# Thunderbird Add-ons Infra (Pulumi)

ECS Fargate infrastructure for addons-server (stage environment)

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

pulumi stack select thunderbird/thunderbird-addons/stage
```

## Preview and Deploy

```bash
# Preview (RO -- no AWS changes)
pulumi preview --diff

# Deploy (RW -- creates/updates AWS resources)
pulumi up
```

## Architecture

| Component | Implementation |
|-----------|---------------|
| Web | Fargate service, ALB (HTTPS) |
| Worker | Fargate service (internal, no ALB) |
| Versioncheck | Fargate service, ALB (HTTPS) |
| Cron | 16 EventBridge-scheduled ECS tasks |
| Cache | ElastiCache Redis (private subnets) |
| Networking | New VPC peered to existing default VPC |

## Safety Layers

Services deploy cold by default. Each layer is independently verifiable

| Layer | Config key | Default |
|-------|-----------|---------|
| Desired count | `desired_count` | `0` |
| Autoscaling | `suspend` | `true` |
| EventBridge schedules | `state` | `DISABLED` |
| DB credentials | `BOOTSTRAP_SAFE` env var | `true` (RO user) |

## CI/CD

### Build and Push (`build-and-push.yml`)

- **Pull requests**: Build validation only (no AWS auth)
- **Push to stage**: Build + push to ECR via OIDC
- **Manual trigger**: `workflow_dispatch` (for re-builds without a code push)

### Enabling ECR Publishing

1. AWS OIDC provider for `token.actions.githubusercontent.com` (already exists)
2. IAM role created by Pulumi with trust policy scoped to `refs/heads/stage`
3. Set repository variable: `AWS_ROLE_ARN` (from Pulumi output `gha_ecr_publish_role_arn`)

## Scheduled Tasks

16 cron jobs run via EventBridge Scheduler (all `DISABLED` by default):

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

- `stage-latest` -- current stage build
- `sha-{commit}` -- per-commit builds
- ECR lifecycle: keep 50 tagged images, expire untagged after 7 days

## Secrets

No secrets are stored in the repository.

Application expects Secrets Manager paths under `atn/stage/*`:
- Database credentials (RW and RO variants)
- Django secret key
- External service configuration

See `settings_local_stage.py` for full mapping.

## Post-Deployment Verification

All commands below are read-only

### ECS Services

```bash
for svc in web worker versioncheck; do
  echo "=== $svc ==="
  aws ecs describe-services \
    --cluster "thunderbird-addons-stage-${svc}" \
    --services "thunderbird-addons-stage-${svc}" \
    --region us-west-2 \
    --query 'services[0].[desiredCount,runningCount,status]' \
    --output text
done
```

### Scheduled Tasks

```bash
aws scheduler list-schedules \
  --group-name thunderbird-addons-stage-cron \
  --region us-west-2 \
  --query 'Schedules[*].[Name,State,ScheduleExpression]' \
  --output table
```

### RO Healthcheck

The `ro_healthcheck` management command validates connectivity to all backends
from within the ECS VPC. Run as a one-off Fargate task with `BOOTSTRAP_SAFE=true`.

```bash
aws ecs run-task \
  --cluster thunderbird-addons-stage-worker \
  --task-definition thunderbird-addons-stage-ro-healthcheck \
  --launch-type FARGATE \
  --network-configuration "..." \
  --region us-west-2
```

## Resources Created

- VPC with public/private subnets across 3 AZs peered to existing default VPC
- ECR repository with lifecycle policy
- 3 ECS Fargate services (web, worker, versioncheck) with ALBs where applicable
- ElastiCache Redis replication group
- 16 EventBridge scheduled tasks
- IAM roles (task execution, task, scheduler, OIDC for CI)
- CloudWatch log groups with KMS encryption
- VPC endpoints (ECR, SSM, Logs, Secrets Manager, S3)
- Application autoscaling targets (suspended by default)
