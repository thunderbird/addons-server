import uuid

from django.conf import settings
from django.db.models import Q
from django.forms import ValidationError
from django.utils.translation import ugettext

import waffle
from celery import chain
from six import text_type

import olympia.core.logger

from olympia import amo
from olympia.addons.models import Addon
from olympia.amo.urlresolvers import linkify_escape
from olympia.files.models import File, FileUpload
from olympia.files.utils import parse_addon
from olympia.lib.akismet.models import AkismetReport
from olympia.tags.models import Tag
from olympia.translations.models import Translation
from olympia.versions.compare import version_int

from . import tasks


log = olympia.core.logger.getLogger('z.devhub')


def process_validation(validation, is_compatibility=False, file_hash=None):
    """Process validation results into the format expected by the web
    frontend, including transforming certain fields into HTML,  mangling
    compatibility messages, and limiting the number of messages displayed."""
    validation = fix_addons_linter_output(validation)

    # Secondly remove any privileged errors
    validation = remove_privileged_errors(validation)

    if is_compatibility:
        mangle_compatibility_messages(validation)

    # Set an ending tier if we don't have one (which probably means
    # we're dealing with mock validation results or the addons-linter).
    validation.setdefault('ending_tier', 0)

    if not validation['ending_tier'] and validation['messages']:
        validation['ending_tier'] = max(msg.get('tier', -1)
                                        for msg in validation['messages'])

    limit_validation_results(validation)

    htmlify_validation(validation)

    return validation


def remove_privileged_errors(validation):
    """
    New version of addons-linter includes some additional errors that we can't easily get around without pulling the code down,
    which will happen eventually. But for now just pop them off the errors
    """
    # No errors to pop!
    if validation['errors'] == 0:
        return validation

    to_remove = []

    for index, error in enumerate(validation['messages']):
        if error['id'][0] in ['PRIVILEGED_FEATURES_REQUIRED', 'MOZILLA_ADDONS_PERMISSION_REQUIRED']:
            # Prepend to the list, this will ensure our positional pops work correctly
            to_remove.insert(0, index)

    # If there's nothing to remove, then don't even bother
    if len(to_remove) == 0:
        return validation

    # Iterate and remove the specific items, we're iterating in reverse order here.
    for index in to_remove:
        validation['messages'].pop(index)

    validation['errors'] -= len(to_remove)

    return validation

def mangle_compatibility_messages(validation):
    """Mangle compatibility messages so that the message type matches the
    compatibility type, and alter totals as appropriate."""

    compat = validation['compatibility_summary']
    for k in ('errors', 'warnings', 'notices'):
        validation[k] = compat[k]

    for msg in validation['messages']:
        if msg['compatibility_type']:
            msg['type'] = msg['compatibility_type']


def limit_validation_results(validation):
    """Limit the number of messages displayed in a set of validation results,
    and if truncation has occurred, add a new message explaining so."""

    messages = validation['messages']
    lim = settings.VALIDATOR_MESSAGE_LIMIT
    if lim and len(messages) > lim:
        # Sort messages by severity first so that the most important messages
        # are the one we keep.
        TYPES = {'error': 0, 'warning': 2, 'notice': 3}

        def message_key(message):
            return TYPES.get(message.get('type'))
        messages.sort(key=message_key)

        leftover_count = len(messages) - lim
        del messages[lim:]

        # The type of the truncation message should be the type of the most
        # severe message in the results.
        if validation['errors']:
            msg_type = 'error'
        elif validation['warnings']:
            msg_type = 'warning'
        else:
            msg_type = 'notice'

        compat_type = (msg_type if any(msg.get('compatibility_type')
                                       for msg in messages)
                       else None)

        message = ugettext(
            'Validation generated too many errors/warnings so %s '
            'messages were truncated. After addressing the visible '
            'messages, you\'ll be able to see the others.') % leftover_count

        messages.insert(0, {
            'tier': 1,
            'type': msg_type,
            # To respect the message structure, see bug 1139674.
            'id': ['validation', 'messages', 'truncated'],
            'message': message,
            'description': [],
            'compatibility_type': compat_type})


def htmlify_validation(validation):
    """Process the `message` and `description` fields into
    safe HTML, with URLs turned into links."""

    for msg in validation['messages']:
        msg['message'] = linkify_escape(msg['message'])

        if 'description' in msg:
            # Description may be returned as a single string, or list of
            # strings. Turn it into lists for simplicity on the client side.
            if not isinstance(msg['description'], (list, tuple)):
                msg['description'] = [msg['description']]

            msg['description'] = [
                linkify_escape(text) for text in msg['description']]


def fix_addons_linter_output(validation, listed=True):
    """Make sure the output from the addons-linter is the same as amo-validator
    for backwards compatibility reasons."""
    if 'messages' in validation:
        # addons-linter doesn't contain this, return the original validation
        # untouched
        return validation

    def _merged_messages():
        for type_ in ('errors', 'notices', 'warnings'):
            for msg in validation[type_]:
                # FIXME: Remove `uid` once addons-linter generates it
                msg['uid'] = uuid.uuid4().hex
                msg['type'] = msg.pop('_type')
                msg['id'] = [msg.pop('code')]
                # We don't have the concept of tiers for the addons-linter
                # currently
                msg['tier'] = 1
                yield msg

    identified_files = {
        name: {'path': path}
        for name, path in validation['metadata'].get('jsLibs', {}).items()
    }

    # Essential metadata.
    metadata = {
        'listed': listed,
        'identified_files': identified_files,
        'processed_by_addons_linter': True,
        'is_webextension': True
    }
    # Add metadata already set by the linter.
    metadata.update(validation.get('metadata', {}))

    return {
        'success': not validation['errors'],
        'compatibility_summary': {
            'warnings': 0,
            'errors': 0,
            'notices': 0,
        },
        'notices': validation['summary']['notices'],
        'warnings': validation['summary']['warnings'],
        'errors': validation['summary']['errors'],
        'messages': list(_merged_messages()),
        'metadata': metadata,
        # The addons-linter only deals with WebExtensions and no longer
        # outputs this itself, so we hardcode it.
        'detected_type': 'extension',
        'ending_tier': 5,
    }


def find_previous_version(addon, file, version_string, channel):
    """
    Find the most recent previous version of this add-on, prior to
    `version`, that can be used to issue upgrade warnings.
    """
    if not addon or not version_string:
        return

    statuses = [amo.STATUS_PUBLIC]
    # Find all previous files of this add-on with the correct status and in
    # the right channel.
    qs = File.objects.filter(
        version__addon=addon, version__channel=channel, status__in=statuses)

    if file:
        # Add some extra filters if we're validating a File instance,
        # to try to get the closest possible match.
        qs = (qs.exclude(pk=file.pk)
              # Files which are not for the same platform, but have
              # other files in the same version which are.
                .exclude(~Q(platform=file.platform) &
                         Q(version__files__platform=file.platform))
              # Files which are not for either the same platform or for
              # all platforms, but have other versions in the same
              # version which are.
                .exclude(~Q(platform__in=(file.platform,
                                          amo.PLATFORM_ALL.id)) &
                         Q(version__files__platform=amo.PLATFORM_ALL.id)))

    vint = version_int(version_string)
    for file_ in qs.order_by('-id'):
        # Only accept versions which come before the one we're validating.
        if file_.version.version_int < vint:
            return file_


class Validator(object):
    """Class which handles creating or fetching validation results for File
    and FileUpload instances."""

    def __init__(self, file_, addon=None, listed=None):
        self.addon = addon
        self.file = None
        self.prev_file = None

        if isinstance(file_, FileUpload):
            assert listed is not None
            channel = (amo.RELEASE_CHANNEL_LISTED if listed else
                       amo.RELEASE_CHANNEL_UNLISTED)
            save = tasks.handle_upload_validation_result
            is_webextension = False
            is_mozilla_signed = False
            is_experiment = False

            # We're dealing with a bare file upload. Try to extract the
            # metadata that we need to match it against a previous upload
            # from the file itself.
            try:
                addon_data = parse_addon(file_, minimal=True)
                is_webextension = addon_data['is_webextension']
                is_experiment = addon_data['is_experiment']
                is_mozilla_signed = addon_data.get(
                    'is_mozilla_signed_extension', False)
            except ValidationError as form_error:
                log.info('could not parse addon for upload {}: {}'
                         .format(file_.pk, form_error))
                addon_data = None
            else:
                file_.update(version=addon_data.get('version'))
            validate = self.validate_upload(file_, channel, is_webextension, is_experiment)
        elif isinstance(file_, File):
            # The listed flag for a File object should always come from
            # the status of its owner Addon. If the caller tries to override
            # this, something is wrong.
            assert listed is None

            channel = file_.version.channel
            is_mozilla_signed = file_.is_mozilla_signed_extension
            save = tasks.handle_file_validation_result
            validate = self.validate_file(file_)

            self.file = file_
            self.addon = self.file.version.addon
            addon_data = {'guid': self.addon.guid,
                          'version': self.file.version.version}
        else:
            raise ValueError

        # Fallback error handler to save a set of exception results, in case
        # anything unexpected happens during processing.
        on_error = save.subtask(
            [amo.VALIDATOR_SKELETON_EXCEPTION, file_.pk, channel,
             is_mozilla_signed],
            immutable=True)

        # When the validation jobs complete, pass the results to the
        # appropriate save task for the object type.
        self.task = chain(validate, save.subtask(
            [file_.pk, channel, is_mozilla_signed],
            link_error=on_error))

        # Create a cache key for the task, so multiple requests to
        # validate the same object do not result in duplicate tasks.
        opts = file_._meta
        self.cache_key = 'validation-task:{0}.{1}:{2}:{3}'.format(
            opts.app_label, opts.object_name, file_.pk, listed)

    @staticmethod
    def validate_file(file):
        """Return a subtask to validate a File instance."""
        kwargs = {
            'hash_': file.original_hash,
            'is_webextension': file.is_webextension,
            'is_experiment': file.is_experiment
        }
        return tasks.validate_file.subtask([file.pk], kwargs)

    @staticmethod
    def validate_upload(upload, channel, is_webextension, is_experiment=False):
        """Return a subtask to validate a FileUpload instance."""
        assert not upload.validation

        kwargs = {
            'hash_': upload.hash,
            'listed': (channel == amo.RELEASE_CHANNEL_LISTED),
            'is_webextension': is_webextension,
            'is_experiment': is_experiment
        }
        return tasks.validate_file_path.subtask([upload.path], kwargs)


def add_dynamic_theme_tag(version):
    if version.channel != amo.RELEASE_CHANNEL_LISTED:
        return
    files = version.all_files
    if any('theme' in file_.webext_permissions_list for file_ in files):
        Tag(tag_text='dynamic theme').save_tag(version.addon)


def fetch_existing_translations_from_addon(addon, properties):
    translation_ids_gen = (
        getattr(addon, prop + '_id', None) for prop in properties)
    translation_ids = [id_ for id_ in translation_ids_gen if id_]
    # Just get all the values together to make it simplier
    return {
        text_type(value)
        for value in Translation.objects.filter(id__in=translation_ids)}


def get_addon_akismet_reports(user, user_agent, referrer, upload=None,
                              addon=None, data=None, existing_data=()):
    if not waffle.switch_is_active('akismet-spam-check'):
        return []
    assert addon or upload
    properties = ('name', 'summary', 'description')

    if upload:
        addon = addon or upload.addon
        data = data or Addon.resolve_webext_translations(
            parse_addon(upload, addon, user, minimal=True), upload)

    reports = []
    for prop in properties:
        locales = data.get(prop)
        if not locales:
            continue
        if isinstance(locales, dict):
            # Avoid spam checking the same value more than once by using a set.
            locale_values = set(locales.values())
        else:
            # It's not a localized dict, it's a flat string; wrap it anyway.
            locale_values = {locales}
        for comment in locale_values:
            if not comment or comment in existing_data:
                # We don't want to submit empty or unchanged content
                continue
            report = AkismetReport.create_for_addon(
                upload=upload, addon=addon, user=user, property_name=prop,
                property_value=comment, user_agent=user_agent,
                referrer=referrer)
            reports.append((prop, report))
    return reports
