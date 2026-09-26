# -*- coding: utf-8 -*-
"""Tests for settings_local_stage.py (Fargate) and settings_local_k8s.py (EKS).

Both modules read Secrets Manager at import time, so boto3.client is replaced
by a fake and the modules are imported fresh for every test.
"""
import importlib
import json
import os
import sys

from django.core.exceptions import ImproperlyConfigured

import pytest

from mock import patch


FAKE_SECRETS = {
    'email_url': 'smtp://user:pass@mail.example:587',
    'mysql': {'username': 'rw', 'password': 'pw', 'host': 'db', 'port': 3306},
    'mysql_ro': {'username': 'ro', 'password': 'pw', 'host': 'db',
                 'port': 3306},
    'inbound_email': {'secret_key': 'k', 'validation_key': 'v'},
    'django_secret_key': 'not-a-secret',
    'celery_broker': 'amqp://broker',
    'recaptcha': {'public': 'pub', 'private': 'priv'},
    'fxa': {'client_id': 'id', 'client_secret': 'secret'},
    'cache_host': 'memcached:11211',
    'celery_result_backend': 'redis://redis',
    'elasticsearch_host': 'es:9200',
}

ATN_VARS = ('ATN_DOMAIN', 'ATN_ENV', 'ATN_SECRETS_ENV', 'ATN_SECRETS_REGION',
            'ATN_SECRETS_ACCOUNT', 'BOOTSTRAP_SAFE')


class FakeSecretsManager(object):
    def __init__(self, calls, region_name):
        self.calls = calls
        self.region_name = region_name

    def get_secret_value(self, SecretId):
        self.calls.append((self.region_name, SecretId))
        name = SecretId.rsplit('/', 1)[1]
        return {'SecretString': json.dumps(FAKE_SECRETS[name])}


def load(module_name, **environ):
    """Import module_name fresh with only the given ATN_* variables set.

    Returns (module, [(region, SecretId), ...]).
    """
    calls = []

    def fake_client(service_name, region_name):
        assert service_name == 'secretsmanager'
        return FakeSecretsManager(calls, region_name)

    def forget():
        for name in ('settings_local_stage', 'settings_local_k8s'):
            sys.modules.pop(name, None)

    forget()
    clean = {k: v for k, v in os.environ.items() if k not in ATN_VARS}
    clean['NETAPP_STORAGE_ROOT'] = '/tmp/storage'
    clean.update(environ)
    try:
        with patch.dict(os.environ, clean, clear=True), \
                patch('boto3.client', fake_client):
            module = importlib.import_module(module_name)
    finally:
        forget()
    return module, calls


def test_stage_defaults_unchanged():
    stage, calls = load('settings_local_stage')
    assert stage.DOMAIN == 'addons-stage.thunderbird.net'
    assert stage.CDN_HOST == 'https://addons-stage.thunderbird.net'
    assert stage.SITE_URL == 'https://addons-stage.thunderbird.net'
    assert stage.SERVICES_URL == (
        'https://services.addons-stage.thunderbird.net')
    assert stage.SESSION_COOKIE_DOMAIN == '.addons-stage.thunderbird.net'
    assert stage.INBOUND_EMAIL_DOMAIN == 'addons-stage.thunderbird.net'
    assert stage.VAMO_URL == (
        'https://versioncheck.addons-stage.thunderbird.net')
    assert stage.PROD_CDN_HOST == 'https://addons-stage.thunderbird.net/'
    assert stage.FXA_CONFIG['amo']['redirect_url'] == (
        'https://addons-stage.thunderbird.net/api/v3/accounts/authenticate/')
    assert stage.ENV == 'tbstage'
    assert stage.DEBUG is True
    assert stage.SEND_REAL_EMAIL is False
    assert stage.ES_INDEXES == {
        'default': 'addons_tbstage', 'stats': 'addons_stats_tbstage'}
    assert ('us-west-2', 'atn/stage/mysql') in calls
    assert all(region == 'us-west-2' and sid.startswith('atn/stage/')
               for region, sid in calls)


def test_stage_bootstrap_safe_reads_ro_secret():
    stage, calls = load('settings_local_stage', BOOTSTRAP_SAFE='true')
    assert ('us-west-2', 'atn/stage/mysql_ro') in calls
    assert ('us-west-2', 'atn/stage/mysql') not in calls
    assert stage.DATABASES['default']['USER'] == 'ro'


def test_k8s_defaults():
    k8s, calls = load('settings_local_k8s')
    assert k8s.DOMAIN == 'addons-stage-eks.thunderbird.net'
    assert k8s.CDN_HOST == 'https://addons-stage-eks.thunderbird.net'
    assert k8s.SITE_URL == 'https://addons-stage-eks.thunderbird.net'
    assert k8s.SERVICES_URL == (
        'https://services.addons-stage-eks.thunderbird.net')
    assert k8s.SESSION_COOKIE_DOMAIN == '.addons-stage-eks.thunderbird.net'
    assert k8s.INBOUND_EMAIL_DOMAIN == 'addons-stage-eks.thunderbird.net'
    assert k8s.STATIC_URL == (
        'https://addons-stage-eks.thunderbird.net/static/')
    assert k8s.SERVICES_DOMAIN == 'services.addons-stage-eks.thunderbird.net'
    assert k8s.DEBUG is False
    assert k8s.DEBUG_PROPAGATE_EXCEPTIONS is False
    assert k8s.SEND_REAL_EMAIL is False
    assert k8s.ENV == 'tbstageeks'
    assert k8s.ES_INDEXES == {
        'default': 'addons_tbstageeks', 'stats': 'addons_stats_tbstageeks'}
    assert all(sid.startswith('atn/stage/') for _, sid in calls)


def test_k8s_domain_and_secrets_from_env():
    k8s, calls = load(
        'settings_local_k8s', ATN_DOMAIN='example.test',
        ATN_SECRETS_ENV='stage-eks', ATN_SECRETS_REGION='eu-central-1',
        ATN_SECRETS_ACCOUNT='111122223333')
    assert k8s.SITE_URL == 'https://example.test'
    assert k8s.SERVICES_URL == 'https://services.example.test'
    assert k8s.SESSION_COOKIE_DOMAIN == '.example.test'
    assert k8s.INBOUND_EMAIL_DOMAIN == 'example.test'
    assert '.example.test' in k8s.ALLOWED_HOSTS
    assert (
        'eu-central-1',
        'arn:aws:secretsmanager:eu-central-1:111122223333:secret:'
        'atn/stage-eks/django_secret_key') in calls
    assert all(region == 'eu-central-1' for region, _ in calls)


def test_empty_domain_counts_as_unset():
    stage, _ = load('settings_local_stage', ATN_DOMAIN='')
    assert stage.SITE_URL == 'https://addons-stage.thunderbird.net'
    k8s, _ = load('settings_local_k8s', ATN_DOMAIN='')
    assert k8s.SITE_URL == 'https://addons-stage-eks.thunderbird.net'


@pytest.mark.parametrize(
    'env_name', ['tbstage', 'prod', 'TBSTAGE', 'TbStageEks', 'stage-eks', ''])
def test_k8s_refuses_shared_es_suffix(env_name):
    with pytest.raises(ImproperlyConfigured):
        load('settings_local_k8s', ATN_ENV=env_name)
