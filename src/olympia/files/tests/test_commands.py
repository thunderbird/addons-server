# -*- coding: utf-8 -*-
from django.conf import settings
from django.core.management import call_command

import mock

from olympia import amo
from olympia.addons.models import Addon
from olympia.applications.models import AppVersion
from olympia.files.models import File, WebextPermission
from olympia.files.tests.test_models import UploadTest
from olympia.files.utils import parse_addon
from olympia.versions.models import Version
from olympia.users.models import UserProfile


class TestWebextExtractPermissions(UploadTest):
    def setUp(self):
        super(TestWebextExtractPermissions, self).setUp()
        for version in ('3.0', '3.6', '3.6.*', '4.0b6'):
            AppVersion(application=amo.FIREFOX.id, version=version).save()
        for version in ('60.0', '60.*', '*'):
            AppVersion(application=amo.THUNDERBIRD.id, version=version).save()
        self.platform = amo.PLATFORM_ALL.id
        self.addon = Addon.objects.create(guid='guid@jetpack',
                                          type=amo.ADDON_EXTENSION,
                                          name='xxx')
        self.version = Version.objects.create(addon=self.addon)
        UserProfile.objects.create(pk=settings.TASK_USER_ID)

    def test_extract(self):
        upload = self.get_upload('webextension_with_id.xpi')
        parsed_data = parse_addon(upload, user=mock.Mock())
        # Remove the permissions from the parsed data so they aren't added.
        pdata_permissions = parsed_data.pop('permissions')
        pdata_cscript = parsed_data.pop('content_scripts')
        file_ = File.from_upload(upload, self.version, self.platform,
                                 parsed_data=parsed_data)
        assert WebextPermission.objects.count() == 0
        assert file_.webext_permissions_list == []

        call_command('extract_permissions')

        file_ = File.objects.get(id=file_.id)
        assert WebextPermission.objects.get(file=file_)
        permissions_list = file_.webext_permissions_list
        assert len(permissions_list) == 8
        assert permissions_list == [
            # first 5 are 'permissions'
            u'http://*/*', u'https://*/*', 'bookmarks', 'made up permission',
            'https://google.com/',
            # last 3 are 'content_scripts' matches we treat the same
            '*://*.mozilla.org/*', '*://*.mozilla.com/*',
            'https://*.mozillians.org/*']
        assert permissions_list[0:5] == pdata_permissions
        assert permissions_list[5:8] == [x for y in [
            cs['matches'] for cs in pdata_cscript] for x in y]

    def test_force_extract(self):
        upload = self.get_upload('webextension_with_id.xpi')
        parsed_data = parse_addon(upload, user=mock.Mock())
        # change the permissions so we can tell they've been re-parsed.
        parsed_data['permissions'].pop()
        file_ = File.from_upload(upload, self.version, self.platform,
                                 parsed_data=parsed_data)
        assert WebextPermission.objects.count() == 1
        assert len(file_.webext_permissions_list) == 7

        call_command('extract_permissions', force=True)

        file_ = File.objects.get(id=file_.id)
        assert WebextPermission.objects.get(file=file_)
        assert len(file_.webext_permissions_list) == 8
