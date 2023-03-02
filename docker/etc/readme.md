# Documented Changes

## MariaDB
 - Enabling listening on port 3306
 - Binding to all addresses

## Nginx
 - Add atn.conf for `olympia.test:8000`

## RabbitMQ
 - Set default user/pass/vhost for docker, as env variables weren't working

## Redis
 - Remove security protection, and bind to all addresses for docker