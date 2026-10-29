#!/bin/sh
# Apply one migration file exactly as schematic does (BEGIN; sql; COMMIT;),
# without bumping schema_version. usage: migrate_one.sh <file name in /mig>
f="/mig/$1"
s=$(date +%s.%N)
printf 'BEGIN;\n%s\nCOMMIT;' "$(cat "$f")" | mysql --silent -h"$DB_HOST" -u"$DB_USER" "$DB_NAME"
rc=$?
echo "$1 rc=$rc $(echo "$(date +%s.%N) - $s" | bc)s"
exit $rc
