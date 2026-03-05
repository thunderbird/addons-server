# ECS Fargate Stage Deployment Validation

> **Date**: 2026-03-07
> **Stack**: `thunderbird-addons/stage`
> **Region**: `us-west-2`

---

## Infrastructure deployment

```
pulumi up --stack thunderbird/thunderbird-addons/stage

Resources:
    + 129 created
    +-  2 replaced
    131 changes. 26 unchanged

Duration: 5m 54s
Exit code: 0
```

### Resource breakdown

| Category | Resources | Notes |
|----------|-----------|-------|
| VPC + networking | VPC, 3 public subnets, 3 private subnets, NAT gateway, IGW, route tables, VPC peering | New VPC peered to existing default VPC |
| VPC endpoints | ECR (api + dkr), SSM, CloudWatch Logs, Secrets Manager, S3 gateway | Private connectivity for Fargate tasks |
| ECS clusters + services | web, worker, versioncheck | All at `desired_count: 0` |
| ALBs + target groups | web, versioncheck | Listeners on 80/443 |
| Security groups | ALB SGs, container SGs, VPC endpoint SG, Redis SG | SG-to-SG ingress (ALB -> container) |
| IAM | Execution roles, task roles, OIDC role, scoped policies | Least-privilege, secrets access scoped |
| ECR | Repository (imported from existing) | Tag mutability updated |
| ElastiCache | Redis replication group | Private subnets only |
| EventBridge | 16 scheduled tasks | All `DISABLED` by default |
| Autoscaling | 3 target-tracking policies | All `suspended`, `min_capacity: 0` |
| CloudWatch | Log groups, KMS keys | Per-cluster logging |

### Post-deploy state verification

```
ECS Services:
  web:            desired=0  running=0  pending=0  status=ACTIVE
  worker:         desired=0  running=0  pending=0  status=ACTIVE
  versioncheck:   desired=0  running=0  pending=0  status=ACTIVE

Autoscaling:
  All 3 services: min=0, DynamicScalingIn=suspended,
                  DynamicScalingOut=suspended, ScheduledScaling=suspended

EventBridge Schedules:
  All 16: DISABLED
```

---

## Read-only MySQL user

A dedicated read-only MySQL user was created for safe bootstrap validation:

- **Scope**: `SELECT` on the application database only
- **Host restriction**: Connections accepted only from the ECS VPC CIDR
- **Secrets Manager**: Credentials stored as a separate secret (`_ro` suffix)
- **App integration**: `BOOTSTRAP_SAFE=true` environment variable selects the RO
  credentials at startup; no code changes required

---

## RO healthcheck (one-off ECS task)

A one-off Fargate task was launched in the private subnets to validate end-to-end
connectivity from the new VPC to all shared backend services.

**Task configuration**:
- Image: current `stage-latest` from ECR
- Settings: `BOOTSTRAP_SAFE=true` (RO database credentials)
- Network: private subnets, worker security group, no public IP

**Results**:

```
======================================================================
ATN Read-Only Health Check (ECS Deployment Validation)
======================================================================
  [OK] Django settings import
       DJANGO_SETTINGS_MODULE=settings_local_stage
  [OK] MySQL database (read-only ORM query)
       Connected, 241480 addons (56ms)
  [OK] Cache backend
       Backend: django.core.cache.backends.memcached.MemcachedCache (0ms)
  [OK] Celery broker (RabbitMQ)
       Connected (19ms)
  [OK] Elasticsearch / OpenSearch
       Reachable, version: 5.6.17 (46ms)
----------------------------------------------------------------------
  Results: 5 passed, 0 failed
======================================================================
```

All five checks passed, confirming:

1. Django settings load correctly in the ECS environment
2. The RO MySQL user can query the application database from the new VPC
3. Memcached is reachable across the VPC peering connection
4. RabbitMQ (Celery broker) is reachable across the VPC peering connection
5. Elasticsearch 5.6 is reachable across the VPC peering connection

---

## Safety layers active

| Layer | Purpose | State |
|-------|---------|-------|
| `desired_count: 0` | No tasks run unless explicitly scaled | Active |
| Autoscaling suspended | Prevents automatic scale-out | Active |
| EventBridge `DISABLED` | No cron jobs fire | Active |
| `BOOTSTRAP_SAFE=true` | App uses RO database credentials | Active |

---

## Next steps

1. Scale `versioncheck` to 1 (read-heavy, safest service)
2. Scale `web` to 1
3. Scale `worker` (coordinate with legacy EC2 worker shutdown)
4. Enable EventBridge schedules incrementally
5. Flip `BOOTSTRAP_SAFE` to `false` for RW operations (separate deliberate step)
