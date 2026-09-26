# -*- coding: utf-8 -*-
"""Settings for the EKS stage.

Builds on settings_local_stage.py, which is parameterised by environment
variables whose defaults reproduce the Fargate stage. This module only sets
different defaults for those variables and overrides the few values that must
differ on EKS, so the two stages cannot drift apart silently.

Environment variables (all optional):
    ATN_DOMAIN          -- public hostname
                           (default addons-stage-eks.thunderbird.net).
                           DOMAIN, CDN_HOST, SITE_URL, SERVICES_URL,
                           SESSION_COOKIE_DOMAIN and INBOUND_EMAIL_DOMAIN
                           derive from it, as does the http-to-https redirect
                           in docker/docker-entrypoint.sh
    ATN_SECRETS_ENV     -- secrets are read from atn/<ATN_SECRETS_ENV>/*
                           (default stage)
    ATN_SECRETS_REGION  -- Secrets Manager region (default us-west-2)
    ATN_SECRETS_ACCOUNT -- account that owns the secrets; when set they are
                           addressed by ARN (default: the caller's account)
    ATN_ENV             -- ENV name, also the Elasticsearch index suffix
                           (default tbstageeks)
"""

import os
import re

from django.core.exceptions import ImproperlyConfigured

os.environ.setdefault('ATN_DOMAIN', 'addons-stage-eks.thunderbird.net')

import settings_local_stage as stage  # noqa: E402
from olympia.lib import settings_base  # noqa: E402

# Django only reads UPPERCASE module attributes as settings, so inheriting
# those is the whole of what a star import would do here.
globals().update(
    (name, value) for name, value in vars(stage).items() if name.isupper())


ENV = os.environ.get('ATN_ENV', 'tbstageeks')
DEBUG = False
DEBUG_PROPAGATE_EXCEPTIONS = False
SEND_REAL_EMAIL = False

# Elasticsearch is shared between environments, so every index this stage
# writes carries its own suffix. Recompute from the unsuffixed base names;
# settings_local_stage has already applied the Fargate suffix.
_RESERVED_ES_SUFFIXES = {'tbstage', 'stage', 'prod', 'tbprod', 'dev'}
if not re.match(r'^[a-z0-9_]+$', ENV):
    raise ImproperlyConfigured(
        'ATN_ENV=%r is not a valid Elasticsearch index suffix '
        '(lowercase letters, digits and underscores only)' % ENV)
if ENV in _RESERVED_ES_SUFFIXES:
    raise ImproperlyConfigured(
        'ATN_ENV=%r would share Elasticsearch indexes with another '
        'environment' % ENV)
ES_INDEXES = {
    k: '%s_%s' % (v, ENV) for k, v in settings_base.ES_INDEXES.items()}

# The inherited list only covers the stock suffixes; accept whatever
# ATN_DOMAIN names (and its services. and versioncheck. subdomains).
ALLOWED_HOSTS = stage.ALLOWED_HOSTS + ['.' + stage.DOMAIN]
