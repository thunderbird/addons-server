# -*- coding: utf-8 -*-
"""Settings for the EKS stage.

Builds on settings_local_stage.py, which is parameterised by environment
variables whose defaults reproduce the Fargate stage. This module only sets
different defaults for those variables and overrides the few values that must
differ on EKS, so the two stages cannot drift apart silently.

The atn/stage-eks/* secrets do not exist yet; the EKS deployment work creates
them. Until then this module fails at import (ResourceNotFoundException) rather
than falling back to the Fargate stage's atn/stage/* secrets, which would share
its Celery broker and memcached. ATN_SECRETS_ENV=stage is refused outright.

Environment variables (all optional; empty counts as unset):
    ATN_DOMAIN          -- public hostname
                           (default addons-stage-eks.thunderbird.net).
                           DOMAIN, CDN_HOST, SITE_URL, SERVICES_URL,
                           SESSION_COOKIE_DOMAIN and INBOUND_EMAIL_DOMAIN
                           derive from it, as does the http-to-https redirect
                           in docker/docker-entrypoint.sh
    ATN_SECRETS_ENV     -- secrets are read from atn/<ATN_SECRETS_ENV>/*
                           (default stage-eks; stage is refused)
    ATN_SECRETS_REGION  -- Secrets Manager region (default us-west-2)
    ATN_SECRETS_ACCOUNT -- account that owns the secrets; when set they are
                           addressed by ARN (default: the caller's account)
    ATN_ENV             -- ENV name, also the Elasticsearch index suffix;
                           must start with tbstageeks (default tbstageeks)
"""

import os
import re

from django.core.exceptions import ImproperlyConfigured

# Empty counts as unset, as in docker/docker-entrypoint.sh. These must be set
# before settings_local_stage is imported: it reads Secrets Manager on import.
if not os.environ.get('ATN_DOMAIN'):
    os.environ['ATN_DOMAIN'] = 'addons-stage-eks.thunderbird.net'
if not os.environ.get('ATN_SECRETS_ENV'):
    os.environ['ATN_SECRETS_ENV'] = 'stage-eks'
if os.environ['ATN_SECRETS_ENV'].strip() == 'stage':
    # atn/stage/* holds the Fargate stage's broker and cache endpoints; an EKS
    # worker reading them would consume Fargate's Celery tasks.
    raise ImproperlyConfigured(
        'ATN_SECRETS_ENV=stage would share the Fargate stage secrets')

import settings_local_stage as stage  # noqa: E402
from olympia.lib import settings_base  # noqa: E402

# Django only reads UPPERCASE module attributes as settings, so inheriting
# those is the whole of what a star import would do here.
globals().update(
    (name, value) for name, value in vars(stage).items() if name.isupper())


ENV = os.environ.get('ATN_ENV') or 'tbstageeks'
DEBUG = False
DEBUG_PROPAGATE_EXCEPTIONS = False
SEND_REAL_EMAIL = False

# Elasticsearch is shared between environments, so every index this stage
# writes carries its own suffix. Recompute from the unsuffixed base names;
# settings_local_stage has already applied the Fargate suffix. A required
# prefix rather than a list of other environments' names: names we do not
# know about can never match.
if not re.match(r'^tbstageeks[a-z0-9_]*$', ENV):
    raise ImproperlyConfigured(
        'ATN_ENV=%r must start with tbstageeks and contain only lowercase '
        'letters, digits and underscores, so EKS stage indexes cannot '
        'collide with another environment\'s' % ENV)
ES_INDEXES = {
    k: '%s_%s' % (v, ENV) for k, v in settings_base.ES_INDEXES.items()}

# settings_base derives SERVICES_DOMAIN from the container hostname; the
# services robots.txt policy (amo.views.robots) matches requests against it.
SERVICES_DOMAIN = 'services.' + stage.DOMAIN

# The inherited list only covers the stock suffixes; accept whatever
# ATN_DOMAIN names (and its services. and versioncheck. subdomains).
ALLOWED_HOSTS = stage.ALLOWED_HOSTS + ['.' + stage.DOMAIN]
