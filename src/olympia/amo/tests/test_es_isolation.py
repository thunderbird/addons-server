"""Tests for the Elasticsearch index isolation helpers in olympia.amo.tests.

These guard one rule: a pytest process must never delete an index belonging
to another xdist worker. Every worker's indices start with `test_`, so the
prefix has to include the worker id for ownership to mean anything.

See thunderbird/addons-server#397.
"""


from olympia.amo.tests import es_index_prefix_for_config, owns_es_index


class FakeConfig(object):
    """Stand-in for the pytest config object, which xdist decorates."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_prefix_without_xdist():
    assert es_index_prefix_for_config(FakeConfig()) == 'test'


def test_prefix_from_workerinput():
    config = FakeConfig(workerinput={'workerid': 'gw3', 'slaveid': 'gw3'})
    assert es_index_prefix_for_config(config) == 'test_gw3'


def test_prefix_from_legacy_slaveinput():
    # pytest-xdist before the slave to worker rename only set this one.
    config = FakeConfig(slaveinput={'slaveid': 'gw1'})
    assert es_index_prefix_for_config(config) == 'test_gw1'


def test_worker_owns_only_its_own_indices():
    assert owns_es_index('test_gw0_amo_addons', prefix='test_gw0')
    assert owns_es_index('test_gw0_amo_stats', prefix='test_gw0')

    # The whole point: another worker's indices start with `test_` too, and
    # deleting one destroys live data in a run that is still going.
    assert not owns_es_index('test_gw1_amo_addons', prefix='test_gw0')
    assert not owns_es_index('test_amo_addons', prefix='test_gw0')


def test_worker_prefixes_do_not_match_on_a_shared_digit():
    # 'test_gw1' must not swallow gw10 and friends once there are ten or
    # more workers, which is why ownership compares against `prefix + _`.
    assert not owns_es_index('test_gw10_amo_addons', prefix='test_gw1')


def test_serial_run_owns_leftover_worker_indices():
    # A serial run has no peers to damage, so it owns anything prefixed
    # `test_`, including indices abandoned by an earlier parallel run. That
    # is what stops them accumulating on a long-lived local cluster.
    assert owns_es_index('test_amo_addons', prefix='test')
    assert owns_es_index('test_gw3_amo_addons', prefix='test')
