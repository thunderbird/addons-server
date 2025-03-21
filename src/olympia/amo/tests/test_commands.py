# -*- coding: utf-8 -*-
import os
from importlib import import_module

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test.utils import override_settings

import mock
import pytest
import six


def sample_cron_job(*args):
    pass


@override_settings(
    CRON_JOBS={'sample_cron_job': 'olympia.amo.tests.test_commands'})
@mock.patch('olympia.amo.tests.test_commands.sample_cron_job')
def test_cron_command(_mock):
    assert _mock.call_count == 0
    call_command('cron', 'sample_cron_job', 'arg1', 'arg2')
    assert _mock.call_count == 1
    _mock.assert_called_with('arg1', 'arg2')


@override_settings(
    CRON_JOBS={'sample_cron_job': 'olympia.amo.tests.test_commands'})
def test_cron_command_no_job():
    with pytest.raises(CommandError) as error_info:
        call_command('cron')
    assert 'These jobs are available:' in str(error_info.value)
    assert 'sample_cron_job' in str(error_info.value)


def test_cron_command_invalid_job():
    with pytest.raises(CommandError) as error_info:
        call_command('cron', 'made_up_job')
    assert 'Unrecognized job name: made_up_job' in str(error_info.value)


def test_cron_jobs_setting():
    for name, path in six.iteritems(settings.CRON_JOBS):
        module = import_module(path)
        getattr(module, name)


@pytest.mark.static_assets
def test_compress_assets_command_without_git():
    settings.MINIFY_BUNDLES = {
        'css': {'zamboni/css': ['css/legacy/main.css']}}

    # Capture output to avoid it being logged and allow us to validate it
    # later if needed
    out = six.StringIO()
    call_command('compress_assets', stdout=out)

    build_id_file = os.path.realpath(os.path.join(settings.ROOT, 'build.py'))
    assert os.path.exists(build_id_file)

    with open(build_id_file) as f:
        contents_before = f.read()

    # Call command a second time. We should get a different build id, since it
    # depends on a uuid.
    call_command('compress_assets', stdout=out)
    with open(build_id_file) as f:
        contents_after = f.read()

    assert contents_before != contents_after


@pytest.mark.static_assets
def test_compress_assets_correctly_fetches_static_images(settings, tmpdir):
    """
    Make sure that `compress_assets` correctly fetches static assets
    such as icons and writes them correctly into our compressed
    and concatted files.

    Refs https://github.com/mozilla/addons-server/issues/8760
    """
    settings.MINIFY_BUNDLES = {
        'css': {'zamboni/css': ['css/legacy/main.css']}}

    # Capture output to avoid it being logged and allow us to validate it
    # later if needed
    out = six.StringIO()

    # Now run compress and collectstatic
    call_command('compress_assets', force=True, stdout=out)
    call_command('collectstatic', interactive=False, stdout=out)

    css_all = os.path.join(
        settings.STATIC_ROOT, 'css', 'zamboni', 'css-all.css')

    css_min = os.path.join(
        settings.STATIC_ROOT, 'css', 'zamboni', 'css-min.css')

    with open(css_all, 'r') as fobj:
        expected = 'background-image: url(../../img/icons/stars.png'
        assert expected in fobj.read()

    # Compressed doesn't have any whitespace between `background-image:` and
    # the url and the path is slightly different
    with open(css_min, 'r') as fobj:
        data = fobj.read()
        assert 'background-image:url(' in data
        assert 'img/icons/stars.png' in data


@pytest.mark.needs_locales_compilation
def test_generate_jsi18n_files():
    dirname = os.path.join(settings.STATICFILES_DIRS[0], 'js', 'i18n')
    assert os.path.exists(dirname)
    filename = os.path.join(dirname, 'fr.js')
    call_command('generate_jsi18n_files')
    # Regardless of whether or not the file existed before, it needs to exist
    # now.
    assert os.path.exists(filename), filename

    # Spot-check: Look for a string we know should be in the french file
    # (Translation for "Error").
    filename = os.path.join(
        settings.STATICFILES_DIRS[0], 'js', 'i18n', 'fr.js')
    with open(filename) as f:
        content = f.read()
        assert u'Erreur' in content
