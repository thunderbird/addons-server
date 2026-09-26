# -*- coding: utf-8 -*-

import logging
import os
import datetime
import json

import boto3
from botocore.exceptions import ClientError

from olympia.lib.settings_base import * # noqa


# -----------------------------------------------------------------------------
# Environment parameters
# -----------------------------------------------------------------------------
# The defaults reproduce the Fargate stage exactly; settings_local_k8s.py sets
# different defaults for the EKS stage before importing this module.
#
# ATN_SECRETS_ENV     -- secrets are read from atn/<ATN_SECRETS_ENV>/*
# ATN_SECRETS_REGION  -- Secrets Manager region
# ATN_SECRETS_ACCOUNT -- when set, secrets are addressed by (partial) ARN in
#                        this account instead of by name in the caller's own
# ATN_DOMAIN          -- public hostname; every site URL below derives from it
ATN_SECRETS_ENV = os.environ.get('ATN_SECRETS_ENV', 'stage')
ATN_SECRETS_REGION = os.environ.get('ATN_SECRETS_REGION', 'us-west-2')
ATN_SECRETS_ACCOUNT = os.environ.get('ATN_SECRETS_ACCOUNT', '')
ATN_DOMAIN = os.environ.get('ATN_DOMAIN', 'addons-stage.thunderbird.net')


# AWS Secrets Manager helper
_secrets_cache = {}


def secret_id(name):
    """Return the SecretId for atn/<ATN_SECRETS_ENV>/<name>."""
    secret_name = 'atn/%s/%s' % (ATN_SECRETS_ENV, name)
    if ATN_SECRETS_ACCOUNT:
        return 'arn:aws:secretsmanager:%s:%s:secret:%s' % (
            ATN_SECRETS_REGION, ATN_SECRETS_ACCOUNT, secret_name)
    return secret_name


def get_secret(secret_name, region_name=ATN_SECRETS_REGION):
    """Retrieve a secret from AWS Secrets Manager with caching."""
    if secret_name in _secrets_cache:
        return _secrets_cache[secret_name]

    client = boto3.client(service_name='secretsmanager', region_name=region_name)
    try:
        response = client.get_secret_value(SecretId=secret_name)
        secret = response['SecretString']
        # Try to parse as JSON, otherwise return raw string
        try:
            secret = json.loads(secret)
        except json.JSONDecodeError:
            pass
        _secrets_cache[secret_name] = secret
        return secret
    except ClientError as e:
        raise Exception(f"Failed to retrieve secret {secret_name}: {e}")


# -----------------------------------------------------------------------------
# Bootstrap safety toggle
# -----------------------------------------------------------------------------
# When BOOTSTRAP_SAFE is true we deliberately use RO database credentials
# (if present) so that even if something accidentally starts, MySQL itself
# enforces read-only access
BOOTSTRAP_SAFE = env.bool("BOOTSTRAP_SAFE", default=False)
MYSQL_SECRET_NAME = secret_id("mysql_ro" if BOOTSTRAP_SAFE else "mysql")


# Retrieve secrets from AWS Secrets Manager
_email_url_secret = get_secret(secret_id('email_url'))
_mysql_secret = get_secret(MYSQL_SECRET_NAME)
_inbound_email_secret = get_secret(secret_id('inbound_email'))
_django_secret = get_secret(secret_id('django_secret_key'))
_celery_broker_secret = get_secret(secret_id('celery_broker'))
_recaptcha_secret = get_secret(secret_id('recaptcha'))
_fxa_secret = get_secret(secret_id('fxa'))
_cache_host_secret = get_secret(secret_id('cache_host'))
_celery_result_backend_secret = get_secret(secret_id('celery_result_backend'))
_es_host_secret = get_secret(secret_id('elasticsearch_host'))


EMAIL_URL = env.email_url('EMAIL_URL', default=_email_url_secret)
EMAIL_HOST = EMAIL_URL['EMAIL_HOST']
EMAIL_PORT = EMAIL_URL['EMAIL_PORT']
EMAIL_BACKEND = EMAIL_URL['EMAIL_BACKEND']
EMAIL_HOST_USER = EMAIL_URL['EMAIL_HOST_USER']
EMAIL_HOST_PASSWORD = EMAIL_URL['EMAIL_HOST_PASSWORD']
EMAIL_USE_TLS = True
EMAIL_QA_ALLOW_LIST = ''
EMAIL_DENY_LIST = ''

SEND_REAL_EMAIL = False
ENV = 'tbstage'
DEBUG = True
DEBUG_PROPAGATE_EXCEPTIONS = True
SESSION_COOKIE_SECURE = True
ENABLE_ADDON_SIGNING = False

API_THROTTLE = False

DOMAIN = ATN_DOMAIN
CDN_HOST = 'https://' + DOMAIN

SERVER_EMAIL = 'thunderbird-seamonkey-ops@mozilla.com'
SITE_URL = 'https://' + DOMAIN
SERVICES_URL = 'https://services.' + DOMAIN
STATIC_URL = '%s/static/' % CDN_HOST
MEDIA_URL = '%s/user-media/' % CDN_HOST

SESSION_COOKIE_DOMAIN = ".%s" % DOMAIN

# Filter IP addresses of allowed clients that can post email through the API.
# This is normally blank on production.
ALLOWED_CLIENTS_EMAIL_API = []
# Auth token required to authorize inbound email.
INBOUND_EMAIL_SECRET_KEY = _inbound_email_secret['secret_key']
# Validation key we need to send in POST response.
INBOUND_EMAIL_VALIDATION_KEY = _inbound_email_secret['validation_key']
# Domain emails should be sent to.
INBOUND_EMAIL_DOMAIN = DOMAIN

NETAPP_STORAGE_ROOT = env('NETAPP_STORAGE_ROOT')
NETAPP_STORAGE = NETAPP_STORAGE_ROOT + '/shared_storage'
GUARDED_ADDONS_PATH = NETAPP_STORAGE_ROOT + '/guarded-addons'
MEDIA_ROOT = NETAPP_STORAGE + '/uploads'

TMP_PATH = os.path.join(NETAPP_STORAGE, 'tmp')
PACKAGER_PATH = os.path.join(TMP_PATH, 'packager')

ADDONS_PATH = NETAPP_STORAGE_ROOT + '/files'

# Must be forced in settings because name => path can't be dyncamically
# computed: reviewer_attachmentS VS reviewer_attachment.
# TODO: rename folder on file system.
# (One can also just rename the setting, but this will not be consistent
# with the naming scheme.)
REVIEWER_ATTACHMENTS_PATH = MEDIA_ROOT + '/reviewer_attachment'

FILESYSTEM_CACHE_ROOT = NETAPP_STORAGE_ROOT + '/cache'

DATABASES = {}
DATABASES['default'] = {
        'NAME': 'addons_mozilla_org',
        'USER': _mysql_secret['username'],
        'PASSWORD': _mysql_secret['password'],
        'HOST': _mysql_secret['host'],
        'PORT': str(_mysql_secret['port']),
}
DATABASES['default']['ENGINE'] = 'django.db.backends.mysql'
# Run all views in a transaction (on master) unless they are decorated not to.
DATABASES['default']['ATOMIC_REQUESTS'] = True
# Pool our database connections up for 300 seconds
DATABASES['default']['CONN_MAX_AGE'] = 300
DATABASES['default']['OPTIONS'] = {'sql_mode': 'STRICT_ALL_TABLES'}
DATABASES['default']['TEST'] = {
    'CHARSET': 'utf8',
    'COLLATION': 'utf8_general_ci'
}

DATABASES['slave'] = {
        'NAME': 'addons_mozilla_org',
        'USER': _mysql_secret['username'],
        'PASSWORD': _mysql_secret['password'],
        # Use the same host as default; the old value 'database.services.atn-stage'
        # is a private DNS name in a PHZ not resolvable from the ECS VPC
        # For stage, reading from the primary is acceptable
        'HOST': _mysql_secret['host'],
        'PORT': str(_mysql_secret['port']),
}
# Do not open a transaction for every view on the slave DB.
DATABASES['slave']['ATOMIC_REQUESTS'] = False
DATABASES['slave']['ENGINE'] = 'django.db.backends.mysql'
# Pool our database connections up for 300 seconds
DATABASES['slave']['CONN_MAX_AGE'] = 300
DATABASES['slave']['OPTIONS'] = {'sql_mode': 'STRICT_ALL_TABLES'}

SERVICES_DATABASE = {
        'NAME': 'addons_mozilla_org',
        'USER': _mysql_secret['username'],
        'PASSWORD': _mysql_secret['password'],
        # Same fix as slave above
        'HOST': _mysql_secret['host'],
        'PORT': str(_mysql_secret['port']),
}

SLAVE_DATABASES = ['slave']

CACHE_MIDDLEWARE_KEY_PREFIX = CACHE_KEY_PREFIX

CACHES = {
    'filesystem': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': FILESYSTEM_CACHE_ROOT,
    }
}

CACHES['default'] = {
    'LOCATION': [
        _cache_host_secret,
    ]
}
CACHES['default']['TIMEOUT'] = 60
CACHES['default']['BACKEND'] = 'django.core.cache.backends.memcached.MemcachedCache'
CACHES['default']['KEY_PREFIX'] = CACHE_KEY_PREFIX

SECRET_KEY = _django_secret


# Celery
AWS_STATS_S3_BUCKET = 'versioncheck-athena-results-stage'
CELERY_RESULT_BACKEND = _celery_result_backend_secret
CELERY_BROKER_URL = _celery_broker_secret
CELERY_TASK_IGNORE_RESULT = True
CELERY_WORKER_DISABLE_RATE_LIMITS = True
CELERY_BROKER_CONNECTION_TIMEOUT = 0.5

# Always eager to function with no brokers, remove when adding brokers.
CELERY_TASK_ALWAYS_EAGER = False
CELERY_ALWAYS_EAGER = False

LOG_LEVEL = logging.DEBUG

LOGGING['loggers'].update({
    'adi.updatecountsfromfile': {'level': logging.INFO},
    'amqp': {'level': logging.WARNING},
    'raven': {'level': logging.WARNING},
    'requests': {'level': logging.WARNING},
    'z.addons': {'level': logging.DEBUG},
    'z.task': {'level': logging.DEBUG},
    'z.redis': {'level': logging.DEBUG},
    'z.pool': {'level': logging.ERROR},
})

# New Recaptcha V2
NOBOT_RECAPTCHA_PUBLIC_KEY = _recaptcha_secret['public']
NOBOT_RECAPTCHA_PRIVATE_KEY = _recaptcha_secret['private']

ES_TIMEOUT = 60
# Note: there is no separate stage ES domain; amo-tb is shared.
ES_HOSTS = [_es_host_secret]
ES_URLS = ['http://%s' % h for h in ES_HOSTS]
ES_INDEXES = dict((k, '%s_%s' % (v, ENV)) for k, v in ES_INDEXES.items())

# TODO: STATSD
# STATSD_HOST = env('STATSD_HOST')
# STATSD_PREFIX = env('STATSD_PREFIX')

# CEF_PRODUCT = STATSD_PREFIX

NEW_FEATURES = True

CLEANCSS_BIN = 'node_modules/.bin/cleancss'
UGLIFY_BIN = 'node_modules/.bin/uglifyjs'
ADDONS_LINTER_BIN = 'node_modules/.bin/addons-linter'

LESS_PREPROCESS = True

XSENDFILE_HEADER = 'X-Accel-Redirect'

GOOGLE_ANALYTICS_CREDENTIALS = {} # TODO GOOGLE ANALYTICS
GOOGLE_ANALYTICS_CREDENTIALS['user_agent'] = None
GOOGLE_ANALYTICS_CREDENTIALS['token_expiry'] = datetime.datetime(2013, 1, 3, 1, 20, 16, 45465)  # noqa

GEOIP_URL = 'https://geo.services.mozilla.com'

# 256-byte AES key file for encrypting developer api keys. Mind the dictionary format.
AES_KEYS = {'api_key:secret': '/data/aeskeys/api_key_secret.key'}

# Signing
SIGNING_SERVER = '' # TODO SIGNING SERVER?

SENTRY_DSN = '' # TODO SENTRY

GOOGLE_ANALYTICS_DOMAIN = DOMAIN

NEWRELIC_ENABLE = False

FXA_CONFIG = {
    'default': {
        'client_id': _fxa_secret['client_id'],
        'client_secret': _fxa_secret['client_secret'],
        'content_host': 'https://accounts.firefox.com',
        'oauth_host': 'https://oauth.accounts.firefox.com/v1',
        'profile_host': 'https://profile.accounts.firefox.com/v1',
        'redirect_url':
            'https://%s/api/v3/accounts/authenticate/' % DOMAIN,
        'scope': 'profile',
    },
    'internal': {
        'client_id': '',
        'client_secret': '',
        'content_host': 'https://accounts.firefox.com',
        'oauth_host': 'https://oauth.accounts.firefox.com/v1',
        'profile_host': 'https://profile.accounts.firefox.com/v1',
        'redirect_url':
            'https://addons-admin.stage.mozaws.net/fxa-authenticate',
        'scope': 'profile',
    },
    'amo': {
        'client_id': _fxa_secret['client_id'],
        'client_secret': _fxa_secret['client_secret'],
        'content_host': 'https://accounts.firefox.com',
        'oauth_host': 'https://oauth.accounts.firefox.com/v1',
        'profile_host': 'https://profile.accounts.firefox.com/v1',
        'redirect_url':
            'https://%s/api/v3/accounts/authenticate/' % DOMAIN,
        'scope': 'profile',
        'skip_register_redirect': True,
    },
}
DEFAULT_FXA_CONFIG_NAME = 'default'
INTERNAL_FXA_CONFIG_NAME = 'internal'
ALLOWED_FXA_CONFIGS = ['default', 'amo']

# cors_endpoint_overrides was removed in the Thunderbird fork;
# CORS is handled via django-cors-headers middleware configuration
# CORS_ENDPOINT_OVERRIDES = cors_endpoint_overrides([...])

VALIDATOR_TIMEOUT = 360

ES_DEFAULT_NUM_SHARDS = 10

READ_ONLY = env.bool("READ_ONLY", default=BOOTSTRAP_SAFE)

# TODO: Github user ?
GITHUB_API_USER = ''
GITHUB_API_TOKEN = ''

RECOMMENDATION_ENGINE_URL = env(
    'RECOMMENDATION_ENGINE_URL',
default='https://taar.stage.mozaws.net/api/recommendations/')

# OVERRIDE
# new base settings below

ALLOWED_HOSTS = [
    '.thunderbird.net',
    '.allizom.org',
    '.mozilla.org',
    '.mozilla.com',
    '.mozilla.net',
    '.mozaws.net',
]

MIDDLEWARE = (
    'olympia.amo.middleware_healthcheck.ALBHealthCheckMiddleware',
) + MIDDLEWARE

FLIGTAR = 'addons+fligtar-rip@thunderbird.net'
THEMES_EMAIL = 'addons+theme-reviews@thunderbird.net'
ABUSE_EMAIL = 'addons+abuse@thunderbird.net'
NOBODY_EMAIL = 'nobody@thunderbird.net'

DEFAULT_APP = 'thunderbird'

# URL paths
# paths for images, e.g. mozcdn.com/amo or '/static'
VAMO_URL = 'https://versioncheck.' + DOMAIN
NEW_PERSONAS_UPDATE_URL = VAMO_URL + '/%(locale)s/themes/update-check/%(id)d'

# TODO Outgoing URL bouncer
REDIRECT_URL = ''
REDIRECT_SECRET_KEY = ''

# Allow URLs from these servers. Use full domain names.
REDIRECT_URL_ALLOW_LIST = [DOMAIN]

# Email settings
ADDONS_EMAIL = "Thunderbird Add-ons <nobody@thunderbird.net>"
DEFAULT_FROM_EMAIL = ADDONS_EMAIL

# Please use all lowercase for the deny_list.
EMAIL_DENY_LIST = (
    'nobody@thunderbird.net',
)

# URL for Add-on Validation FAQ.
VALIDATION_FAQ_URL = ('https://wiki.mozilla.org/Add-ons/Reviewers/Guide/'
                      'AddonReviews#Step_2:_Automatic_validation')

# CSP Settings
PROD_CDN_HOST = CDN_HOST + '/'
ANALYTICS_HOST = 'https://ssl.google-analytics.com'

CSP_BASE_URI = (
    "'self'",
    # Required for the legacy discovery pane.
    CDN_HOST,
)
CSP_CONNECT_SRC = (
    "'self'",
    'https://sentry.prod.mozaws.net',
)
CSP_FORM_ACTION = (
    "'self'",
    'https://developer.mozilla.org',
)
CSP_FONT_SRC = (
    "'self'",
    PROD_CDN_HOST,
)
CSP_CHILD_SRC = (
    "'self'",
    'https://www.google.com/recaptcha/',
)
CSP_FRAME_SRC = CSP_CHILD_SRC
CSP_IMG_SRC = (
    "'self'",
    'data:',  # Used in inlined mobile css.
    'blob:',  # Needed for image uploads.
    ANALYTICS_HOST,
    PROD_CDN_HOST,
    'https://static.addons.mozilla.net',  # CDN origin server.
    'https://sentry.prod.mozaws.net',
)
CSP_MEDIA_SRC = (
    'https://videos.cdn.mozilla.net',
)
CSP_OBJECT_SRC = ("'none'",)

CSP_SCRIPT_SRC = (
    'https://ssl.google-analytics.com/ga.js',
    'https://www.google.com/recaptcha/',
    'https://www.gstatic.com/recaptcha/',
    PROD_CDN_HOST,
)
CSP_STYLE_SRC = (
    "'self'",
    "'unsafe-inline'",
    PROD_CDN_HOST,
)

# An approved list of domains that the authentication script will redirect to
# upon successfully logging in or out.
VALID_LOGIN_REDIRECTS = {
    'builder': 'https://builder.addons.mozilla.org',
    'builderstage': 'https://builder-addons.allizom.org',
    'buildertrunk': 'https://builder-addons-dev.allizom.org',
}

# Blog URL
DEVELOPER_BLOG_URL = 'http://blog.mozilla.com/addons/feed/'

