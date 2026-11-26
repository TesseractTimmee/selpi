import unittest

from web import server


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.inv = server.Inverter()
        self.inv.versions = {'memory_map': 31}   # pre-set so _run skips the version read

    def test_trips_after_repeated_failures_then_fails_fast(self):
        calls = []

        def fn(r):
            calls.append(1)
            raise RuntimeError('boom')

        for _ in range(server.BREAKER_FAILS):
            with self.assertRaises(RuntimeError):
                self.inv.use('poll', fn, wait=True, check_versions=False)
        self.assertTrue(self.inv.link_down)
        ran = len(calls)
        # tripped: the next call returns Busy immediately without touching the link (fn not invoked)
        with self.assertRaises(server.Busy):
            self.inv.use('poll', fn, wait=True, check_versions=False)
        self.assertEqual(len(calls), ran)

    def test_a_successful_probe_clears_the_breaker(self):
        self.inv._fails = server.BREAKER_FAILS
        self.inv._backoff_until = 0.0            # cooldown elapsed: a probe is allowed through
        out = self.inv.use('poll', lambda r: 'ok', wait=True, check_versions=False)
        self.assertEqual(out, 'ok')
        self.assertFalse(self.inv.link_down)

    def test_within_cooldown_a_second_caller_fails_fast(self):
        import time
        self.inv._fails = server.BREAKER_FAILS
        self.inv._backoff_until = time.monotonic() + server.BREAKER_COOLDOWN
        with self.assertRaises(server.Busy):
            self.inv.use('poll', lambda r: 'ok', wait=True, check_versions=False)

    def test_backoff_escalates_per_failed_probe_and_caps(self):
        # A sustained wedge must escalate the probe gap past Select.Live's ~60 s session-linger, else every
        # short-gap probe just opens a new session while the old one still holds the device (the overnight hang).
        self.inv._fails = server.BREAKER_FAILS                    # just tripped
        self.assertEqual(self.inv._backoff_secs(), server.BREAKER_COOLDOWN)
        self.inv._fails = server.BREAKER_FAILS + 1               # one failed probe -> doubled
        self.assertEqual(self.inv._backoff_secs(), server.BREAKER_COOLDOWN * 2)
        self.inv._fails = server.BREAKER_FAILS + 2
        self.assertEqual(self.inv._backoff_secs(), server.BREAKER_COOLDOWN * 4)
        self.inv._fails = server.BREAKER_FAILS + 50              # escalates far -> capped
        self.assertEqual(self.inv._backoff_secs(), server.BREAKER_COOLDOWN_MAX)
        self.assertGreater(server.BREAKER_COOLDOWN_MAX, 60)      # long enough for the dead session to clear


if __name__ == '__main__':
    unittest.main()
