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
