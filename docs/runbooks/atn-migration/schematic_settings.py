# Rehearsal stub for thunderbird/addons-server#399: same db/table contract as
# src/olympia/migrations/schematic_settings.py, without importing Django.
# MYSQL_PWD comes from the pod environment (k8s Secret), as in the original.
import os
db = 'mysql --silent {0} -h{1} -u{2} -P3306'.format(
    os.environ['DB_NAME'], os.environ['DB_HOST'], os.environ['DB_USER'])
table = 'schema_version'
handlers = {}
