import threading
import time

from django.conf import settings as django_settings
from django.core import management
from django.db import connection
from django.test.testcases import TransactionTestCase

import mock
import six

from olympia.addons import indexers as addons_indexers
from olympia.amo.tests import (
    ESTestCase, addon_factory, create_switch, owns_es_index,
    setup_es_test_data)
from olympia.amo.urlresolvers import reverse
from olympia.amo.utils import urlparams
from olympia.lib.es.utils import is_reindexing_amo, unflag_reindexing_amo


class TestIndexCommand(ESTestCase):
    def setUp(self):
        super(TestIndexCommand, self).setUp()
        if is_reindexing_amo():
            unflag_reindexing_amo()

        self.url = reverse('search.search')

        # Start every test from fresh indices with the aliases pointing at
        # them. The class sets them up only once, and each test's reindex
        # moves the alias to a new index that tearDown below then deletes,
        # so a second test in this class on the same worker would otherwise
        # start with an alias pointing at nothing. The one-addon test gets
        # away with that because indexing its add-on makes Elasticsearch
        # auto-create an index under the alias name; the zero-addon test
        # fails with a 404 as soon as it refreshes the alias while the
        # reindex is held. See thunderbird/addons-server#457.
        setup_es_test_data(self.es)

        # We store previously existing indices in order to delete the ones
        # created during this test run.
        self.indices = self.es.indices.stats()['indices'].keys()

        self.addons = []
        self.expected = self.addons[:]

    # Since this test plays with transactions, but we don't have (and don't
    # really want to have) a ESTransactionTestCase class, use the fixture setup
    # and teardown methods from TransactionTestCase.
    def _fixture_setup(self):
        return TransactionTestCase._fixture_setup(self)

    def _fixture_teardown(self):
        return TransactionTestCase._fixture_teardown(self)

    # TransactionTestCase's teardown flushes the database, and MySQL commits
    # implicitly on TRUNCATE. TestCase also opens an atomic block around the
    # whole class and rolls it back in tearDownClass. Together those two
    # destroy django_content_type for everything that runs afterwards in this
    # process: the TRUNCATE commits and cannot be undone, while the rows
    # post_migrate recreates right after it are inside the class atomic and
    # disappear when it rolls back. The table is then empty, the process
    # keeps cached content type ids pointing at rows that no longer exist,
    # and the next test that writes an admin log entry fails with
    #   IntegrityError (1452) ... django_admin_log.content_type_id
    # Opt out of the class-level atomic, which is what TransactionTestCase
    # does and what the fixture methods above already assume.
    # See thunderbird/addons-server#397.
    @classmethod
    def _enter_atomics(cls):
        return {}

    @classmethod
    def _rollback_atomics(cls, atomics):
        pass

    def tearDown(self):
        # Delete only indices we created. Another xdist worker can create one
        # of its own while these tests run, and anything missing from the
        # setUp snapshot is not automatically ours.
        current_indices = self.es.indices.stats()['indices'].keys()
        for index in current_indices:
            if index not in self.indices and owns_es_index(index):
                self.es.indices.delete(index, ignore=404)
        super(TestIndexCommand, self).tearDown()

    def check_settings(self, new_indices):
        """Make sure the indices settings are properly set."""

        for index, alias in new_indices:
            settings = self.es.indices.get_settings(alias)[index]['settings']

            # These should be set in settings_test.
            assert int(settings['index']['number_of_replicas']) == 0
            assert int(settings['index']['number_of_shards']) == 1

    def check_results(self, expected):
        """Make sure the expected addons are listed in a standard search."""
        response = self.client.get(urlparams(self.url, sort='downloads'))
        assert response.status_code == 200
        got = self.get_results(response)

        for addon in expected:
            assert addon.pk in got, '%s is not in %s' % (addon.pk, got)
        return response

    def get_results(self, response):
        """Return pks of add-ons shown on search results page."""
        pager = response.context['pager']
        results = []
        for page_num in range(pager.paginator.num_pages):
            results.extend([item.pk for item
                            in pager.paginator.page(page_num + 1)])
        return results

    @classmethod
    def get_indices_aliases(cls):
        """Return the test indices with an alias."""
        indices = cls.es.indices.get_alias()
        # Under xdist the other workers own indices in the same cluster that
        # also start with `test_`, so match our own prefix only.
        items = [(index, list(aliases['aliases'].keys())[0])
                 for index, aliases in indices.items()
                 if len(aliases['aliases']) > 0 and owns_es_index(index)]
        items.sort()
        return items

    def _test_reindexation(self):
        # Current indices with aliases.
        old_indices = self.get_indices_aliases()

        # This is to start a reindexation in the background.
        class ReindexThread(threading.Thread):
            def __init__(self):
                self.stdout = six.StringIO()
                self.exception = None
                super(ReindexThread, self).__init__()

            def run(self):
                # We need to wait at least a second, to make sure the alias
                # name is going to be different, since we already create an
                # alias in setUpClass.
                time.sleep(1)
                try:
                    management.call_command('reindex', stdout=self.stdout)
                except Exception as exc:
                    # Keep it for the main thread: without this the command
                    # dying is only visible as 'Reindexation done' missing
                    # from stdout further down, which says nothing about why.
                    self.exception = exc

        # Hold the reindex at a known point until the foreground work below
        # is done. Tests run Celery eagerly, so the whole task chain runs
        # inside the thread and the database is only flagged from
        # flag_database to unflag_database: with zero or one add-on that is
        # an index creation and an alias update, a few tens of milliseconds.
        # Polling for the flag could miss that window and the rest of the
        # thread's life, leaving nothing indexed in the foreground, and how
        # often it did depended on the poll interval and runner load.
        # Blocking right after the new index is created, while the database
        # is flagged and the alias still points at the old index, makes the
        # overlap this test is about always happen.
        # See thunderbird/addons-server#457.
        release_reindex = threading.Event()
        real_create_new_index = addons_indexers.create_new_index

        def create_new_index_then_wait(index_name=None):
            real_create_new_index(index_name)
            # Bounded, so a failure in the main thread cannot hang the run.
            release_reindex.wait(60)

        patcher = mock.patch.object(
            addons_indexers, 'create_new_index',
            side_effect=create_new_index_then_wait)
        patcher.start()
        t = ReindexThread()
        try:
            t.start()
            self._index_in_foreground(t)
        finally:
            release_reindex.set()
            t.join()  # Wait for the thread to finish.
            patcher.stop()

        if t.exception is not None:
            raise AssertionError(
                'The reindex command raised in its thread: %r' % t.exception)
        t.stdout.seek(0)
        stdout = t.stdout.read()
        assert 'Reindexation done' in stdout, stdout

        # The reindexation is done, let's double check we have all our docs.
        connection._commit()
        connection.clean_savepoints()
        self.refresh()
        self.check_results(self.expected)

        # New indices have been created, and aliases now point to them.
        new_indices = self.get_indices_aliases()
        assert len(new_indices)
        assert old_indices != new_indices, (stdout, old_indices, new_indices)

        self.check_settings(new_indices)

    def _index_in_foreground(self, t):
        # Wait for the reindex in the thread to flag the database.
        # The database transaction isn't shared with the thread, so force the
        # commit.
        # The reindex is held after creating its new index (see
        # _test_reindexation), so the flag stays set until this method
        # returns and the loop cannot miss it.
        while t.is_alive() and not is_reindexing_amo():
            connection._commit()
            connection.clean_savepoints()

        # We should still be able to search in the foreground while the reindex
        # is being done in the background. We should also be able to index new
        # documents, and they should not be lost.
        old_addons_count = len(self.expected)
        while t.is_alive() and len(self.expected) < old_addons_count + 3:
            self.expected.append(addon_factory())
            connection._commit()
            connection.clean_savepoints()
            self.refresh()
            self.check_results(self.expected)

        if len(self.expected) == old_addons_count:
            # The thread has exited. If the command raised before reaching
            # the hold, that is the real cause, so report it first.
            if t.exception is not None:
                raise AssertionError(
                    'The reindex command raised in its thread: %r'
                    % t.exception)
            raise AssertionError('Could not index objects in foreground while '
                                 'reindexing in the background.')

    def test_reindexation_starting_from_zero_addons(self):
        self._test_reindexation()

    def test_reindexation_starting_from_one_addon(self):
        self.addons.append(addon_factory())
        self.expected = self.addons[:]
        self.refresh()
        self.check_results(self.expected)
        self._test_reindexation()


class TestIndexCommandClassicAlgorithm(TestIndexCommand):
    """Tests that we correctly set the 'classic' similarity algorithm.

    Refs https://github.com/mozilla/addons-server/issues/8867
    """
    def setUp(self):
        super(TestIndexCommandClassicAlgorithm, self).setUp()
        create_switch('es-use-classic-similarity')

    def check_settings(self, new_indices):
        super(TestIndexCommandClassicAlgorithm, self).check_settings(
            new_indices)

        # We don't want to guess the index name. We are putting this here
        # explicitly to ensure that we actually run the test for the index
        # setting instead of using an `if` and failing silently. Read the
        # alias from the settings rather than hard-coding it: under xdist it
        # carries a per-worker prefix, and a hard-coded `test_amo_addons`
        # 404s on every worker.
        amo_addons_settings = self.es.indices.get_settings(
            django_settings.ES_INDEXES['default'])
        settings = amo_addons_settings[list(amo_addons_settings.keys())[0]]

        assert settings['settings']['index']['similarity']['default'] == {
            'type': 'classic'
        }
