#!/bin/bash

echo "Starting services...."
service mariadb start
service memcached start
service redis-server start
service nginx start
service rabbitmq-server start

# This might error out if we're good, and that's OKAY.
if [[ -z $(mysqlshow | grep olympia) ]]; then
mysql -u root -P 3306 -e "CREATE DATABASE olympia";
# Allow docker access
mysql -u root -P 3306 -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'172.%' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}' WITH GRANT OPTION;"
# Allow olympia user to actually use root
mysql -u root -P 3306 -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}' WITH GRANT OPTION;"
echo "Ran first time mysql setup!"
fi

# Follow a log so we don't die
tail -f /var/log/nginx/error.log