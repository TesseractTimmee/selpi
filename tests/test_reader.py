import struct
import unittest
from datetime import datetime

from sppro.reader import Reader
from sppro import logs


def zeros(rng):
    return b'\x00' * (rng.words * 2)


class TestReaderStallHandling(unittest.TestCase):
    def test_transient_stall_retries_the_same_session(self):
        # A stalled read (timeout) must be retried on the SAME connection (reset first), not by reconnecting —
        # reconnecting spawns a second Select.Live session and makes stalls worse.
        created = []

        class P:
            def __init__(self):
                self.calls = 0
                self.resets = 0
                created.append(self)

            def query(self, rng):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError('read timed out')
                return zeros(rng)

            def reset(self):
                self.resets += 1

            def close(self):
                pass

        r = Reader(protocol_factory=P, retries=3)
        r.read(0x2000, 1)
        self.assertEqual(len(created), 1)        # one session only — no reconnect
        self.assertEqual(created[0].resets, 1)   # buffer reset before the retry
        self.assertEqual(created[0].calls, 2)    # timed out once, then succeeded on the same session

    def test_hard_error_reconnects(self):
        created = []

        class P:
            def __init__(self):
                self.calls = 0
                created.append(self)

            def query(self, rng):
                self.calls += 1
                if len(created) == 1:
                    raise ValueError('CRC mismatch')   # not transient -> drop + reconnect
                return zeros(rng)

            def reset(self):
                pass

            def close(self):
                pass

        r = Reader(protocol_factory=P, retries=3)
        r.read(0x2000, 1)
        self.assertEqual(len(created), 2)        # hard error dropped the link, so it reconnected

    def test_persistent_stall_gives_up_without_reconnect_churn(self):
        created = []

        class P:
            def __init__(self):
                created.append(self)

            def query(self, rng):
                raise TimeoutError('still stalled')

            def reset(self):
                pass

            def close(self):
                pass

        r = Reader(protocol_factory=P, retries=2)
        with self.assertRaises(TimeoutError):
            r.read(0x2000, 1)
        self.assertEqual(len(created), 1)        # retried on one session, never spun up new ones mid-op

    def test_max_words_chunks_large_reads(self):
        # Over Select.Live a big block is split into max_words-sized reads (each a quick cloud round-trip);
        # a local link keeps one read of the whole block.
        ranges = []

        class P:
            def query(self, rng):
                ranges.append(rng.words)
                return zeros(rng)

            def reset(self):
                pass

            def close(self):
                pass

        out = Reader(protocol_factory=P, max_words=4).read(0x2000, 10)
        self.assertEqual(len(out), 20)           # 10 words x 2 bytes, fully reassembled
        self.assertEqual(ranges, [4, 4, 2])      # chunked at max_words
        ranges.clear()
        Reader(protocol_factory=P).read(0x2000, 10)   # default (local): one big read
        self.assertEqual(ranges, [10])


class TestLogsDownloadMode(unittest.TestCase):
    def test_download_sets_then_clears_mode(self):
        class FakeReader:
            def __init__(self):
                self.writes = []

            def write(self, address, data):
                self.writes.append((address, bytes(data)))

            def read(self, address, words):
                return struct.pack('<HIIH', logs.STATUS_NONE, 0, 0, 0)   # "no records"

        fr = FakeReader()
        out = logs.download(fr, ['events'], datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual(out, {'events': []})
        modes = [d for a, d in fr.writes if a == logs.DOWNLOAD_MODE]
        self.assertEqual(modes, [b'\x01\x00', b'\x00\x00'])   # enter download mode, then leave — once each


if __name__ == '__main__':
    unittest.main()


class TestIdleReconnect(unittest.TestCase):
    """Select.Live idle-closes at ~60 s; with idle_reconnect set, the reader pre-empts that by closing a session
    that has been idle too long so the next call reconnects fresh (instead of wasting the first read on a dead
    socket). A fast poll (idle < the threshold) must NOT reconnect."""

    def _reader(self, idle_reconnect):
        created, closed = [], []

        class P:
            def __init__(self):
                created.append(self)

            def query(self, rng):
                return zeros(rng)

            def reset(self):
                pass

            def close(self):
                closed.append(self)

        return Reader(protocol_factory=P, idle_reconnect=idle_reconnect), created, closed

    def _patch_clock(self):
        import sppro.reader as R
        self._R = R
        self._orig = R.time.monotonic
        self._now = [1000.0]
        R.time.monotonic = lambda: self._now[0]

    def tearDown(self):
        if getattr(self, "_R", None) is not None:
            self._R.time.monotonic = self._orig

    def test_long_idle_reconnects(self):
        self._patch_clock()
        r, created, closed = self._reader(idle_reconnect=50)
        r.read(0x2000, 1)                      # session 1
        self.assertEqual(len(created), 1)
        self._now[0] += 40                     # 40 s idle (< 50): keep the session
        r.read(0x2000, 1)
        self.assertEqual((len(created), len(closed)), (1, 0))
        self._now[0] += 60                     # 60 s idle (> 50): stale -> close + reconnect
        r.read(0x2000, 1)
        self.assertEqual((len(created), len(closed)), (2, 1))

    def test_disabled_never_reconnects_on_idle(self):
        self._patch_clock()
        r, created, closed = self._reader(idle_reconnect=None)   # local link: no idle-close
        r.read(0x2000, 1)
        self._now[0] += 10000                  # arbitrarily long idle
        r.read(0x2000, 1)
        self.assertEqual((len(created), len(closed)), (1, 0))
