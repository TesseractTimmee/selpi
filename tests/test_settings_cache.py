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


if __name__ == '__main__':
    unittest.main()
