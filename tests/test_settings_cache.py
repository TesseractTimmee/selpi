import time
import unittest

from web import settings_ui as su


class TestConfigCache(unittest.TestCase):
    """The Configuration page is served from the last read when one is recent, so reopening it doesn't re-read
    the whole config over a slow Select.Live link. A fresh read (?refresh=1) still goes to the unit."""

    def setUp(self):
        self._render = su._render
        su._render = lambda rid, snap, from_cache: {'rid': rid, 'cached': from_cache, 'at': snap['at']}
        su.READS.clear()
        su._LATEST = None

    def tearDown(self):
        su._render = self._render
        su.READS.clear()
        su._LATEST = None

    def test_empty_is_none(self):
        self.assertIsNone(su.cached())

    def test_serves_latest_marked_cached(self):
        su.READS['abc'] = {'at': time.time()}
        su._LATEST = 'abc'
        hit = su.cached()
        self.assertEqual(hit['rid'], 'abc')
        self.assertTrue(hit['cached'])

    def test_snapshot_past_ttl_not_served(self):
        su.READS['abc'] = {'at': time.time() - su.READ_MAX_AGE - 5}
        su._LATEST = 'abc'
        self.assertIsNone(su.cached())        # too old for a write, so not served
        self.assertNotIn('abc', su.READS)     # and forgotten

    def test_write_invalidation_clears_cache(self):
        su.READS['xyz'] = {'at': time.time()}
        su._LATEST = 'xyz'
        self.assertIsNotNone(su.cached())
        su._LATEST = None                     # what write() does after a successful write
        self.assertIsNone(su.cached())

    def test_max_age_cache_first_ttl(self):
        # the cache-first TTL (settings.cache_ttl()) is passed as max_age: a read older than it isn't served
        # without reading, even though it's still recent enough to write against (READ_MAX_AGE).
        su.READS['abc'] = {'at': time.time() - 120}
        su._LATEST = 'abc'
        self.assertIsNone(su.cached(60))          # 2 min old, TTL 60 s -> re-read
        self.assertIsNotNone(su.cached(300))      # TTL 5 min -> serve
        self.assertIsNotNone(su.cached())         # no TTL (stall fallback) -> serve whatever its age


class TestCachePolicy(unittest.TestCase):
    """settings.cache_ttl()/stale_grace() defaults follow the connection type but are overridable."""

    def setUp(self):
        import os
        self._saved = {k: os.environ.get(k) for k in
                       ('SELPI_CONNECTION_TYPE', 'SELPI_CACHE_TTL', 'SELPI_STALE_GRACE')}

    def tearDown(self):
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set(self, **kw):
        import os
        for k in ('SELPI_CONNECTION_TYPE', 'SELPI_CACHE_TTL', 'SELPI_STALE_GRACE'):
            os.environ.pop(k, None)
        for k, v in kw.items():
            os.environ[k] = v

    def test_select_live_defaults_cache(self):
        import settings
        self._set(SELPI_CONNECTION_TYPE='SelectLive')
        self.assertEqual(settings.cache_ttl(), 1800)
        self.assertEqual(settings.stale_grace(), 600)

    def test_local_defaults_fresh_and_fast_unknown(self):
        import settings
        self._set(SELPI_CONNECTION_TYPE='Serial')
        self.assertEqual(settings.cache_ttl(), 0)
        self.assertEqual(settings.stale_grace(), 0)

    def test_tcp_over_internet_can_opt_in(self):
        import settings
        self._set(SELPI_CONNECTION_TYPE='TCP', SELPI_CACHE_TTL='900', SELPI_STALE_GRACE='120')
        self.assertEqual(settings.cache_ttl(), 900)
        self.assertEqual(settings.stale_grace(), 120)

    def test_blank_and_bad_fall_back_to_default(self):
        import settings
        self._set(SELPI_CONNECTION_TYPE='SelectLive', SELPI_CACHE_TTL='', SELPI_STALE_GRACE='oops')
        self.assertEqual(settings.cache_ttl(), 1800)
        self.assertEqual(settings.stale_grace(), 600)


if __name__ == '__main__':
    unittest.main()
