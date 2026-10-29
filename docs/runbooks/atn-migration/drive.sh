#!/bin/sh
# Rehearsal driver for thunderbird/addons-server#399.
# Phase 1: 1049-1067 applied file by file (schema_version stays 1071; schematic would skip them).
# Phase 2: repo schematic for 1072-1079 (applies only files > schema_version).
LOG=/work/migrate.log
echo "START $(date -u +%FT%TZ) schema_version=$(mysql --silent -h$DB_HOST -u$DB_USER $DB_NAME -e 'SELECT version FROM schema_version')" >> $LOG
for f in $(ls /mig | awk -F- '$1>=1049 && $1<=1067' | sort -n); do
  echo "BEGIN $f $(date -u +%FT%TZ)" >> $LOG
  /kit/migrate_one.sh $f >> $LOG 2>&1 || { echo "FAILED $f $(date -u +%FT%TZ)" >> $LOG; echo "DRIVE-DONE rc=1" >> $LOG; exit 1; }
done
echo "PHASE2 schematic $(date -u +%FT%TZ)" >> $LOG
cd /mig && python3 /mig/schematic /mig >> $LOG 2>&1; rc=$?
echo "END $(date -u +%FT%TZ) schematic_rc=$rc schema_version=$(mysql --silent -h$DB_HOST -u$DB_USER $DB_NAME -e 'SELECT version FROM schema_version')" >> $LOG
echo "DRIVE-DONE rc=$rc" >> $LOG
