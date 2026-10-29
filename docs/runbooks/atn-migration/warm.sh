#!/bin/sh
# Full read of every affected table that exists (CHECKSUM TABLE reads whole rows incl. off-page columns).
M="mysql --batch --skip-column-names -h$DB_HOST -u$DB_USER $DB_NAME"
while read t; do
  e=$($M -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='$t'")
  [ "$e" = 1 ] || continue
  s=$(date +%s); $M -e "CHECKSUM TABLE \`$t\`" >/dev/null; echo "$t $(( $(date +%s) - s ))s"
done < /kit/tables.txt
