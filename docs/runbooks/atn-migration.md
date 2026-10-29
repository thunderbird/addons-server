# ATN schema migration runbook (1049-1079)

Rehearsed twice on a full copy of production for
[thunderbird/addons-server#399](https://github.com/thunderbird/addons-server/issues/399)
on 2026-10-29. This document is the procedure, the evidence, and the teardown.

The 31 migration files `src/olympia/migrations/1049-*.sql` .. `1079-*.sql` are the
one-way door of the Python 3 / EKS move: 1049, 1053, 1060, 1061, 1062, 1066, 1078 and
1079 drop columns or tables. **Rollback means restoring the pre-migration snapshot.**

**No command in this runbook connects to or writes to the production instance.** The only
production-account actions are copying an existing automated snapshot, sharing that copy
with the rehearsal account and deleting it again (steps 2-5).

## TL;DR

| | Run 1 | Run 2 |
|---|---|---|
| Source snapshot | `rds:<PROD_DB_INSTANCE>-2026-10-29-03-04` | same (tb-dev copy `atn-399-prod-2026-10-29`) |
| Restore to `available` | 10 min 32 s | 10 min 10 s |
| Warm-up (affected tables) | 17 min | 14 min 55 s |
| 1049-1067 (file by file) | 4 min 52 s | 4 min 41 s |
| 1072-1079 (`./schematic`) | 3.3 s | 2.7 s |
| Errors | none | none |
| `schema_version` | 1071 -> 1079 | 1071 -> 1079 |

## The trap this runbook exists for

`./schematic` stores one integer in `schema_version` and applies only files numbered
**above** it. Production runs `master`, which already carries ATN's own 1068-1071 but none
of 1049-1067. Production is therefore at **1071**, and a plain
`./schematic src/olympia/migrations` would apply 1072-1079 and **silently skip
1049-1067**. Confirmed on both rehearsal copies (`SELECT version FROM schema_version`
returned `1071`, and every column and table that 1049-1067 remove was still present).

The procedure is therefore:

1. apply 1049-1067 **file by file, in order**, exactly as schematic does
   (`BEGIN; <file>; COMMIT;` piped to `mysql`), without touching `schema_version`;
2. then run the repo `./schematic`, which applies 1072-1079 and bumps the version to 1079.

1068-1071 are byte-identical on `master` and `stage-eks`; they are already applied.

## Accounts, names and limits

| Thing | Value |
|---|---|
| Production DB (never touched) | `<PROD_DB_INSTANCE>`, mzla-tb-legacy `<LEGACY_ACCOUNT_ID>`, us-west-2 |
| Rehearsal account | mzla-tb-dev `<TB_DEV_ACCOUNT_ID>`, eu-central-1, cluster `mzla-eks-tb-dev01` |
| Snapshot names | legacy manual copy and tb-dev copy are both `atn-399-prod-<DATE>` |
| Rehearsal instance | `atn-399-rehearsal-<DATE>` (run 2: `atn-399-rehearsal2-<DATE>`) |
| Rehearsal network | subnet group `atn-399-rehearsal` (private 1a/1b), SG `atn-399-rehearsal-mysql` (3306 from the cluster SG `<CLUSTER_SG_ID>` only) |
| Parameter group | `atn-399-rehearsal-gtid`, mysql8.0, the 7 user overrides of the prod parameter group `<PROD_PARAMETER_GROUP>` |
| Tag on everything | `atn-issue=399` |
| Runner | namespace `atn-399-rehearsal` on mzla-eks-tb-dev01, pod spec `docs/runbooks/atn-migration/toolbox.yaml` |
| Out of bounds | `amo-py3upgrade-test` (the existing Fargate stage database): do not touch, read or restore over it |

Every AWS-bound string in this procedure is plain ASCII (no em dashes): RDS and EC2
descriptions reject anything else.

## Procedure

Each step lists the command, the expected output and the maximum duration (about 3x the
slower of the two rehearsal runs, rounded up). If a step exceeds its maximum, stop and
investigate before continuing.

Set once per shell (zsh does not word-split variables; write commands literally):

```sh
DATE=2026-10-29                    # date of the automated snapshot you will use
SNAP_HHMM=03-04                    # time part of that snapshot's name (<HH>-<MM>)
PROD_DB_INSTANCE=<PROD_DB_INSTANCE>
LEGACY_ACCOUNT_ID=<LEGACY_ACCOUNT_ID>
TB_DEV_ACCOUNT_ID=<TB_DEV_ACCOUNT_ID>
VPC_ID=<VPC_ID>                    # mzla-eks-tb-dev01 VPC
SUBNET_ID_A=<SUBNET_ID_A>          # tb-dev private subnet, eu-central-1a
SUBNET_ID_B=<SUBNET_ID_B>          # tb-dev private subnet, eu-central-1b
CLUSTER_SG_ID=<CLUSTER_SG_ID>      # describe-cluster .resourcesVpcConfig.clusterSecurityGroupId
MASTER_USER=<MASTER_USER>          # master user carried over in the snapshot
```

The real values are in the issue and in the platform-infrastructure repository; they are
kept out of this public file.

### 1. Identify the newest automated prod snapshot (legacy, read-only)

```sh
AWS_PROFILE=mzla-tb-legacy aws rds describe-db-snapshots --region us-west-2 \
  --db-instance-identifier $PROD_DB_INSTANCE --snapshot-type automated \
  --query 'reverse(sort_by(DBSnapshots,&SnapshotCreateTime))[0].[DBSnapshotIdentifier,Status,Encrypted,AllocatedStorage,EngineVersion]' \
  --output text
```

Expected: `rds:<PROD_DB_INSTANCE>-<DATE>-<HH>-<MM>  available  False  1000  8.0.45`
(automated snapshots of this instance were taken at 03:04 UTC, so `<HH>-<MM>` was `03-04`).
Max: 1 min. The snapshots are **unencrypted**, which is what makes the cross-account,
cross-region copy below possible without sharing a KMS key.

### 2. Copy it to a manual snapshot (legacy)

An automated snapshot cannot be shared; a manual copy can. This reads the snapshot only,
never the instance. Do not use `--copy-tags` (prod's `Env=prod` tags must not follow).

```sh
AWS_PROFILE=mzla-tb-legacy aws rds copy-db-snapshot --region us-west-2 \
  --source-db-snapshot-identifier rds:$PROD_DB_INSTANCE-$DATE-$SNAP_HHMM \
  --target-db-snapshot-identifier atn-399-prod-$DATE \
  --tags Key=project,Value=atn Key=atn-issue,Value=399
AWS_PROFILE=mzla-tb-legacy aws rds wait db-snapshot-available --region us-west-2 \
  --db-snapshot-identifier atn-399-prod-$DATE
```

Expected: `creating`, then the wait returns. Observed: about 90 s. Max: 15 min.

### 3. Share it with tb-dev only (legacy)

```sh
AWS_PROFILE=mzla-tb-legacy aws rds modify-db-snapshot-attribute --region us-west-2 \
  --db-snapshot-identifier atn-399-prod-$DATE --attribute-name restore \
  --values-to-add $TB_DEV_ACCOUNT_ID
```

Expected: `AttributeValues` is exactly `["<TB_DEV_ACCOUNT_ID>"]`. **Never `all`.** Max: 1 min.

### 4. Cross-region copy into tb-dev, encrypted (tb-dev)

```sh
AWS_PROFILE=mzla-tb-dev aws rds copy-db-snapshot --region eu-central-1 --source-region us-west-2 \
  --source-db-snapshot-identifier arn:aws:rds:us-west-2:$LEGACY_ACCOUNT_ID:snapshot:atn-399-prod-$DATE \
  --target-db-snapshot-identifier atn-399-prod-$DATE \
  --kms-key-id alias/aws/rds \
  --tags Key=atn-issue,Value=399 Key=project,Value=atn
```

Expected: `creating  True  arn:aws:kms:<REGION>:<TB_DEV_ACCOUNT_ID>:key/<KEY_ID>`.
Poll `describe-db-snapshots ... --query 'DBSnapshots[0].[Status,PercentProgress]'` until
`available 100`. Observed: **32.5 min** (17:48 to 18:20 UTC; first cross-region copy is
always full). Max: 3 h (AWS may queue cross-region copies with no progress shown).

Encrypting while copying a *shared, unencrypted* snapshot works in one step; the
documented fallback (copy unencrypted, then re-copy in-region with `--kms-key-id`) was not
needed.

### 5. Unshare and delete the legacy manual copy (legacy)

Only once step 4 shows `available`.

```sh
AWS_PROFILE=mzla-tb-legacy aws rds modify-db-snapshot-attribute --region us-west-2 \
  --db-snapshot-identifier atn-399-prod-$DATE --attribute-name restore \
  --values-to-remove $TB_DEV_ACCOUNT_ID
AWS_PROFILE=mzla-tb-legacy aws rds delete-db-snapshot --region us-west-2 \
  --db-snapshot-identifier atn-399-prod-$DATE
```

Expected: `deleted manual`. The automated source snapshot is not touched. Max: 1 min.
After this, nothing of the rehearsal exists in the legacy account.

### 6. Rehearsal network and parameter group (tb-dev, free, once)

```sh
export AWS_PROFILE=mzla-tb-dev AWS_REGION=eu-central-1
aws rds create-db-subnet-group --db-subnet-group-name atn-399-rehearsal \
  --db-subnet-group-description "ATN 399 rehearsal - tb-dev private subnets" \
  --subnet-ids $SUBNET_ID_A $SUBNET_ID_B \
  --tags Key=atn-issue,Value=399 Key=project,Value=atn
aws rds create-db-parameter-group --db-parameter-group-name atn-399-rehearsal-gtid \
  --db-parameter-group-family mysql8.0 \
  --description "ATN 399 rehearsal - mirrors the prod parameter group overrides" \
  --tags Key=atn-issue,Value=399 Key=project,Value=atn
aws rds modify-db-parameter-group --db-parameter-group-name atn-399-rehearsal-gtid --parameters \
  ParameterName=enforce_gtid_consistency,ParameterValue=ON,ApplyMethod=pending-reboot \
  ParameterName=gtid-mode,ParameterValue=ON,ApplyMethod=pending-reboot \
  ParameterName=log_output,ParameterValue=FILE,ApplyMethod=immediate \
  ParameterName=log_slow_extra,ParameterValue=ON,ApplyMethod=immediate \
  ParameterName=log_slow_slave_statements,ParameterValue=1,ApplyMethod=immediate \
  ParameterName=slave_parallel_workers,ParameterValue=4,ApplyMethod=immediate \
  ParameterName=slow_query_log,ParameterValue=1,ApplyMethod=immediate
aws ec2 create-security-group --group-name atn-399-rehearsal-mysql \
  --description "ATN 399 rehearsal MySQL - 3306 from tb-dev01 nodes only" \
  --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=security-group,Tags=[{Key=atn-issue,Value=399},{Key=project,Value=atn},{Key=Name,Value=atn-399-rehearsal-mysql}]'
aws ec2 authorize-security-group-ingress --group-id <SG_ID> \
  --ip-permissions "IpProtocol=tcp,FromPort=3306,ToPort=3306,UserIdGroupPairs=[{GroupId=$CLUSTER_SG_ID,Description=MySQL from tb-dev01 nodes and non-SGP pods}]"
```

Expected: subnet group `Complete`, the SG id, `True`. Max: 5 min.

`<CLUSTER_SG_ID>` is the EKS-created cluster SG that every tb-dev01 node carries
(`describe-cluster` reports it as `clusterSecurityGroupId`). The rule is on the rehearsal SG
only; no shared SG is modified.

### 7. Restore the rehearsal instance (tb-dev)

```sh
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier atn-399-rehearsal-$DATE \
  --db-snapshot-identifier atn-399-prod-$DATE \
  --db-instance-class db.m7g.large \
  --storage-type gp3 --allocated-storage 1000 --iops 12000 --storage-throughput 500 \
  --db-subnet-group-name atn-399-rehearsal --vpc-security-group-ids <SG_ID> \
  --db-parameter-group-name atn-399-rehearsal-gtid \
  --no-publicly-accessible --no-multi-az --no-auto-minor-version-upgrade --no-deletion-protection \
  --backup-retention-period 1 --engine-lifecycle-support open-source-rds-extended-support \
  --copy-tags-to-snapshot \
  --tags Key=atn-issue,Value=399 Key=project,Value=atn Key=environment,Value=rehearsal
aws rds wait db-instance-available --db-instance-identifier atn-399-rehearsal-$DATE
```

Expected: `available`, parameter group `atn-399-rehearsal-gtid` `in-sync`. The create
response shows `default.mysql8.0`; RDS swaps in the custom group (with one reboot) before
the instance becomes available. Observed: 10 min 32 s and 10 min 10 s. Max: 45 min (the
`wait` times out after 30 min; run it twice).

- `--copy-tags-to-snapshot` is what actually ran (the Phase A2 plan said
  `--no-copy-tags-to-snapshot`). It copies the rehearsal instance's own tags
  (`atn-issue=399`) to its automated snapshots, which is what let step 17's tag query
  find the one RDS took during run 2. Production tags never reach the copy, because step 2
  copies the snapshot without `--copy-tags`.
- `--backup-retention-period 1` keeps the binlog on, as on prod (which feeds a replica);
  retention 0 would make DDL faster than prod and the timings would lie.
- 1000 GiB is the minimum: a restore cannot be smaller than the snapshot's allocation.
- MySQL 8.0 left RDS standard support on 2026-07-31; Extended Support applies (accepted).

### 8. Replace the production master password, enable Performance Insights

Do this before anything else touches the copy. Generate the password into a 0600 file, never
on the command line, and keep it only in the runner namespace's Secret:

```sh
umask 077
python3 -c 'import secrets,string,json; a=string.ascii_letters+string.digits; p="".join(secrets.choice(a) for _ in range(40)); open("pw","w").write(p); json.dump({"DBInstanceIdentifier":"atn-399-rehearsal-'$DATE'","MasterUserPassword":p,"ApplyImmediately":True},open("modify.json","w"))'
aws rds modify-db-instance --cli-input-json file://modify.json
aws rds modify-db-instance --db-instance-identifier atn-399-rehearsal-$DATE \
  --enable-performance-insights --performance-insights-retention-period 7 --apply-immediately
```

Expected: RDS event `Reset master credentials` (about 1 min) and `Performance Insights has
been enabled`. Max: 10 min.

### 9. Runner namespace, kit and toolbox (tb-dev01)

```sh
alias k="kubectl --context arn:aws:eks:eu-central-1:$TB_DEV_ACCOUNT_ID:cluster/mzla-eks-tb-dev01 -n atn-399-rehearsal"
kubectl --context arn:aws:eks:eu-central-1:$TB_DEV_ACCOUNT_ID:cluster/mzla-eks-tb-dev01 create ns atn-399-rehearsal
k create secret generic atn-399-db --from-file=MYSQL_PWD=pw
mkdir mig && cp src/olympia/migrations/10[4-7]?-*.sql mig/ && rm mig/104[0-8]-*.sql && cp schematic mig/
cp docs/runbooks/atn-migration/schematic_settings.py mig/
k create configmap atn-399-mig --from-file=mig/
k create configmap atn-399-kit --from-file=docs/runbooks/atn-migration/inventory.sh \
  --from-file=docs/runbooks/atn-migration/warm.sh --from-file=docs/runbooks/atn-migration/migrate_one.sh \
  --from-file=docs/runbooks/atn-migration/tables.txt
rm -f pw modify.json
sed -e 's#REPLACE_ENDPOINT#<endpoint from step 7>#' -e "s#REPLACE_MASTER_USER#$MASTER_USER#" \
  docs/runbooks/atn-migration/toolbox.yaml | k apply -f -
k wait --for=condition=Ready pod/atn-399-toolbox --timeout=180s
until k exec atn-399-toolbox -- test -f /tmp/ready; do
  k exec atn-399-toolbox -- test -f /tmp/install-failed && { echo 'tool install failed, see /tmp/install.log'; break; }
  sleep 5
done
k cp docs/runbooks/atn-migration/drive.sh atn-399-toolbox:/work/drive.sh
```

Expected: `mig/` holds 31 `.sql` files (1049-1079), `schematic` and `schematic_settings.py`;
the pod is `Running` and `/tmp/ready` exists after about 30 s. If `/tmp/install-failed`
appears instead, the package install failed (no egress or repo outage): read
`/tmp/install.log`, delete the pod and retry. Max: 5 min.

The runner is the public `mysql:8.0` image (client 8.0.46) plus `python3` and
`python3-six`, running the **repo's own `schematic`** with a Django-free settings stub
(`schematic_settings.py` here: same `db` / `table` contract as
`src/olympia/migrations/schematic_settings.py`, password from `MYSQL_PWD`). 1049-1079 are
all plain SQL, so nothing else from the application is needed. A branch-built image was
not used because the only stage image lives in the legacy account's ECR with no
cross-account pull policy.

### 10. Lock the production MySQL accounts on the copy

The copy carries every production MySQL account with its production password. Lock all
of them except the master user and the RDS-managed accounts, without naming them here:

```sh
k exec atn-399-toolbox -- sh -c 'mysql -h"$DB_HOST" -u"$DB_USER" --batch --skip-column-names -e "SELECT CONCAT(\"ALTER USER \", QUOTE(user), \"@\", QUOTE(host), \" ACCOUNT LOCK;\") FROM mysql.user WHERE account_locked = \"N\" AND user NOT IN (\"$DB_USER\", \"rdsadmin\", \"rdsrepladmin\")" | mysql -h"$DB_HOST" -u"$DB_USER"'
k exec atn-399-toolbox -- sh -c 'mysql -h"$DB_HOST" -u"$DB_USER" -e "SELECT COUNT(*) AS unlocked FROM mysql.user WHERE account_locked = \"N\" AND user NOT IN (\"$DB_USER\", \"rdsadmin\", \"rdsrepladmin\")"'
```

**Untested:** both rehearsals locked the three non-RDS accounts by name. This query form
was written afterwards so the public runbook names no production account; run the second
command and confirm `0` before continuing, and fall back to locking by name if it is not.

Expected: `unlocked` is `0`. The schema has no routines, views or triggers, so locking breaks
no DEFINER. `rdsadmin` / `rdsrepladmin` are RDS-managed: leave them. Max: 1 min.

### 11. Before-inventory

```sh
k exec atn-399-toolbox -- sh -c '/kit/inventory.sh before; cat /work/before/counts.tsv'
```

Expected: `schema_version` **1071**; row counts as in the table below;
`webext_permission_descriptions_translation_id` absent. Max: 15 min (the cold
`COUNT(*)` on `translations` alone took 154-180 s).

**If `schema_version` is not 1071, stop.** The file-by-file list in step 13 assumes it.

### 12. Warm the affected tables

A volume restored from a snapshot loads blocks from S3 on first read. Without warming, the
rebuild timings in step 13 are inflated.

```sh
k exec atn-399-toolbox -- sh -c '/kit/warm.sh'
```

Expected: one line per table; `translations` about 10 min, `users` about 3 min,
`compatibility_reports` about 2.5 min, `collections` under 1 min. Observed: 17 min (run 1), 14 min 55 s (run 2).
Max: 60 min.

### 13 and 14. Migrations

```sh
k exec atn-399-toolbox -- sh -c 'nohup /work/drive.sh >/dev/null 2>&1 &'
k exec atn-399-toolbox -- cat /work/migrate.log   # poll until DRIVE-DONE
```

`drive.sh` applies 1049-1067 one file at a time through `migrate_one.sh` (stops at the
first non-zero exit), then runs `python3 /mig/schematic /mig` for 1072-1079.

Expected: every `1049..1067 ... rc=0`, then `Running SQL migration 1072` .. `1079`,
`END ... schematic_rc=0 ... schema_version=1079`, `DRIVE-DONE rc=0`.
Max: 20 min for 1049-1067, 2 min for 1072-1079.

Per-migration wall time (warm volume, db.m7g.large, 1000 GiB gp3):

| Migration | Run 1 | Run 2 | Max | What it does |
|---|---|---|---|---|
| 1049 | 36.68 s | 32.84 s | 3 min | drop `addons.locale_disambiguation` (rebuild) |
| 1050 | 0.04 s | 0.04 s | 1 min | waffle insert |
| 1051 | 0.04 s | 0.04 s | 1 min | 3 waffle inserts |
| 1052 | 27.94 s | 26.42 s | 2 min | `UPDATE addons` + `contributions` NOT NULL (rebuild) |
| 1053 | 0.17 s | 0.16 s | 1 min | drop `compatibility_reports` (4.8M rows), `compat_totals` |
| 1054 | 0.04 s | 0.04 s | 1 min | waffle delete |
| 1055 | 0.03 s | 0.03 s | 1 min | waffle delete |
| 1056 | 0.04 s | 0.04 s | 1 min | 15 Android theme categories, ids 400-428 |
| 1057 | 52.39 s | 51.06 s | 3 min | `collections.icontype` nullable (rebuild, 2.7M rows) |
| 1058 | 0.04 s | 0.04 s | 1 min | waffle insert |
| 1059 | 21.74 s | 21.63 s | 2 min | `addons` column defaults (rebuild) |
| 1060 | 47.15 s | 44.97 s | 3 min | drop 5 collection tables, 8 `collections` columns, `users.display_collections_fav` |
| 1061 | 0.05 s | 0.05 s | 1 min | drop `addons_dependencies` |
| 1062 | 0.35 s | 0.34 s | 1 min | drop 2 `addons` columns (INSTANT) |
| 1063 | 4.36 s | 3.79 s | 1 min | add `versions.source_git_hash` |
| 1064 | 97.95 s | 96.61 s | 5 min | `collections.uuid`: strip dashes, char(32) |
| 1065 | 2.65 s | 2.68 s | 1 min | `hubrsskeys.rsskey` char(32) |
| 1066 | 0.05 s | 0.05 s | 1 min | drop `addons_addonfeaturecompatibility` |
| 1067 | 0.07 s | 0.06 s | 1 min | add `version_previews.colors` |
| 1068-1071 | applied | applied | - | already on production |
| 1072-1079 | 3.3 s total | 2.7 s total | 2 min | via `./schematic`; 1078 drops `categories.name` (0.21 s), 1079 no-op |

### 15. After-inventory and verification

```sh
k exec atn-399-toolbox -- sh -c '/kit/inventory.sh after; cat /work/after/counts.tsv /work/after/columns.tsv /work/after/waffles.tsv'
```

Expected (both runs identical):

| Table | Before | After |
|---|---|---|
| addons | 390,512 | 390,512 |
| collections | 2,716,132 | 2,716,132 |
| users | 6,552,150 | 6,552,150 |
| translations | 13,366,353 | 13,366,353 |
| versions | 914,191 | 914,191 |
| hubrsskeys | 201,877 | 201,877 |
| abuse_reports | 75,448 | 75,448 |
| editors_autoapprovalsummary | 76,110 | 76,110 |
| addons_addonreviewerflags | 11,984 | 11,984 |
| version_previews | 3,958 | 3,958 |
| discovery_discoveryitem | 17 | 17 |
| waffle_switch | 18 | 21 |
| waffle_sample | 4 | 5 |
| categories | 125 | 140 |
| compatibility_reports | 4,839,404 | dropped |
| addons_addonfeaturecompatibility | 197,128 | dropped |
| addons_dependencies | 326 | dropped |
| compat_totals | 1 | dropped |
| collection_subscriptions, collections_votes, stats_addons_collections_counts, stats_collections_counts, stats_collections | 0 | dropped |
| webext_permission_descriptions_translation_id | absent | absent |

`waffle_switch` 18 -> 21 = +4 (1050, 1051) -2 (1054, 1055) +1 (1058) +1 (1072) -1 (1076).
Columns: `collections.uuid` and `hubrsskeys.rsskey` are `char(32)` NULL,
`abuse_reports.report_entry_point` exists, `addons.locale_disambiguation`,
`addons.externalsoftware`, `addons.auto_repackage`, `categories.name` and
`users.display_collections_fav` are gone. Max: 15 min.

### 16. Disk and IOPS profile

```sh
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name WriteIOPS \
  --dimensions Name=DBInstanceIdentifier,Value=atn-399-rehearsal-$DATE \
  --start-time <migration start> --end-time <migration end> --period 60 --statistics Maximum Average
```

Repeat for ReadIOPS, WriteThroughput, DiskQueueDepth, CPUUtilization, FreeStorageSpace.
Observed during the migration window:

| Metric | Run 1 max / avg | Run 2 max / avg |
|---|---|---|
| WriteIOPS | 5,878 / 2,242 | 6,126 / 2,610 |
| ReadIOPS | 895 / 332 | 989 / 438 |
| Write throughput | 143 MiB/s / 55 MiB/s | 145 MiB/s / 64 MiB/s |
| DiskQueueDepth | 27.3 / 8.6 | 23.3 / 9.8 |
| CPU | 77% / 44% | 63% / 39% |
| FreeStorageSpace (min) | 639.1 GiB | 638.9 GiB |

The gp3 baseline (12,000 IOPS, 500 MiB/s) was never approached; db.m7g.large is enough.

### 17. Teardown

```sh
aws rds delete-db-instance --db-instance-identifier atn-399-rehearsal-$DATE \
  --skip-final-snapshot --delete-automated-backups
aws rds wait db-instance-deleted --db-instance-identifier atn-399-rehearsal-$DATE
aws rds delete-db-snapshot --db-snapshot-identifier atn-399-prod-$DATE
aws rds delete-db-parameter-group --db-parameter-group-name atn-399-rehearsal-gtid
aws rds delete-db-subnet-group --db-subnet-group-name atn-399-rehearsal
aws ec2 delete-security-group --group-id <SG_ID>
kubectl --context arn:aws:eks:eu-central-1:$TB_DEV_ACCOUNT_ID:cluster/mzla-eks-tb-dev01 delete ns atn-399-rehearsal
aws resourcegroupstaggingapi get-resources --tag-filters Key=atn-issue,Values=399 \
  --query 'ResourceTagMappingList[].ResourceARN'
```

Expected: the last command returns `[]` (the tagging API can lag a few minutes behind
deletes; re-run). The instance is deleted **without a final snapshot** (`--skip-final-snapshot`):
it is a disposable copy. Deleting the tb-dev snapshot copy is the **default**; keep it only if
another run is planned within days.
Max: 20 min (instance deletion is about 5-8 min).

## Gotchas

1. **schematic skips 1049-1067** on a 1071 database. See "The trap" above.
2. **`schematic` needs `six`.** The first phase-2 attempt of run 1 failed with
   `ModuleNotFoundError: No module named 'six'` before touching the database
   (`schema_version` stayed 1071); fixed by installing `python3-six` in the toolbox.
3. **DDL is not transactional.** `schematic` wraps each file in `BEGIN; ... COMMIT;`, but
   MySQL auto-commits DDL. A failure in the middle of a multi-statement file (1053, 1060)
   leaves a partial state. Recovery is the snapshot, not a re-run.
4. **1078's commented-out FK drop is not needed.** Production has no foreign key on
   `categories` at all. Leave the comment as is.
5. **1079 is a no-op on production:** `webext_permission_descriptions_translation_id` is
   already absent.
6. **1056 adds Firefox-for-Android static-theme categories** (ids 400-428, application 61)
   to the Thunderbird database. Applies cleanly; whether ATN wants those rows is a
   product decision outside this runbook.
7. **1049 rebuilds `addons`** even though MySQL 8.0.45 can drop columns INSTANTly;
   after that rebuild, 1062's drops are INSTANT (0.35 s).
8. **Restore parameter group:** the create response says `default.mysql8.0`; the custom
   group is applied (with a reboot) before `available`. Check `in-sync` before step 8.
9. **Cold reads are slow:** cold `COUNT(*)` on `translations` takes 154-180 s; warm the
   tables (step 12) before timing anything.
10. **The copy contains production credentials and PII.** Step 8 replaces the `<MASTER_USER>` password,
    step 10 locks the application accounts. The SG admits only tb-dev01 nodes.

## Cost of the rehearsal (2026-10-29)

| Item | Basis | Cost |
|---|---|---|
| Cross-region transfer us-west-2 to eu-central-1 | about 388 GB at USD 0.02/GB (one copy, reused by both runs) | about USD 7.75 |
| Run 1 instance (db.m7g.large + 8.0 Extended Support + 1000 GiB gp3) | 18:21 to about 19:12, USD 0.632/h | about USD 0.54 |
| Run 2 instance | 19:06 to about 19:52, USD 0.632/h | about USD 0.48 |
| tb-dev snapshot copy (about 361 GiB, about 2 h) | inside the free backup allocation while an instance existed | about USD 0 |
| Legacy manual snapshot (35 min) | USD 0.095/GB-month pro-rated | under USD 0.05 |
| Performance Insights (7-day) | free tier | USD 0 |
| **Total** | | **about USD 9** |

The approved ceiling was USD 461/month while an instance exists plus the one-time copies;
everything was deleted the same day. Figures are from list prices and timestamps; the
invoice will show the data-transfer line within a day.

## Production cutover notes

**Not part of the rehearsal.** The cutover changes the real target database and needs its own
plan and a separate go on its issue before anything here is run.

The cutover runs the same steps 11-15 against the target database. Budget, from the
rehearsal: about 5 minutes of DDL on warm data, 20 minutes as the stop-and-investigate
limit, plus whatever the snapshot and restore of the target take. Writes must be stopped for
the duration (1052, 1057, 1059, 1060 and 1064 rebuild or rewrite `addons` and `collections`).
Take the pre-migration snapshot of the target first and record its identifier; restoring it
is the only rollback.
