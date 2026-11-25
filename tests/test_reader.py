import unittest

from sppro.reader import Reader
from sppro import logs


class FakeProto:
    """Minimal SP PRO protocol: records writes, answers queries with zeros, and can be told to drop the next
    operation once (simulating Select.Live closing the link mid-transfer)."""
    def __init__(self, registry):
        self.writes = []
        self.queries = 0
        self.boom = False
        registry.append(self)

    def _maybe(self):
        if self.boom:
            self.boom = False
            raise IOError('link dropped')

    def query(self, rng):
        self._maybe()
        self.queries += 1
        return b'\x00' * (rng.words * 2)

    def write(self, address, data):
        self._maybe()
        self.writes.append((address, bytes(data)))

    def close(self):
        pass


class TestReaderReconnectHook(unittest.TestCase):
    def test_on_connect_fires_on_each_connect(self):
        created = []
        r = Reader(protocol_factory=lambda: FakeProto(created))
        fired = []
        r.on_connect = lambda rr: fired.append(len(created))
        r.read(0x2000, 1)                     # first connect
        self.assertEqual(len(created), 1)
        self.assertEqual(fired, [1])          # hook fired once, for the first session

    def test_reconnect_reprimes_download_mode(self):
        created = []
        r = Reader(protocol_factory=lambda: FakeProto(created), retries=3)
        # as logs.download does: re-assert download mode on every (re)connect
        r.on_connect = lambda rr: rr.write(logs.DOWNLOAD_MODE, b'\x01\x00')
        r.read(0x2000, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].writes, [(logs.DOWNLOAD_MODE, b'\x01\x00')])   # primed on first session
        created[0].boom = True                # next op drops the link
        r.read(0x2000, 1)                     # fails once -> reconnect -> reprime -> retry succeeds
        self.assertEqual(len(created), 2)                                          # one reconnect happened
        self.assertEqual(created[1].writes, [(logs.DOWNLOAD_MODE, b'\x01\x00')])   # re-primed on the new session

    def test_hook_is_reentrancy_guarded(self):
        # The hook itself writes; that write must not recursively re-fire the hook (which would recurse / double up).
        created = []
        r = Reader(protocol_factory=lambda: FakeProto(created))
        depth = {'max': 0, 'cur': 0}

        def hook(rr):
            depth['cur'] += 1
            depth['max'] = max(depth['max'], depth['cur'])
            rr.write(logs.DOWNLOAD_MODE, b'\x01\x00')   # goes through the reader; must not re-enter the hook
            depth['cur'] -= 1

        r.on_connect = hook
        r.read(0x2000, 1)
        self.assertEqual(depth['max'], 1)               # never nested


class TestLogsDownloadHook(unittest.TestCase):
    def test_download_sets_reprime_hook_in_mode_and_restores_after(self):
        import struct
        from datetime import datetime

        class FakeReader:
            def __init__(self):
                self.on_connect = 'ORIGINAL'          # download() must restore whatever was here
                self.writes = []
                self.hook_while_downloading = 'unset'

            def write(self, address, data):
                self.writes.append((address, bytes(data)))
                if address == logs.DOWNLOAD_MODE and bytes(data) == b'\x01\x00':
                    self.hook_while_downloading = self.on_connect   # capture the hook during the download

            def read(self, address, words):
                # find(): report "no records" (STATUS_NONE) so the search ends at once
                return struct.pack('<HIIH', logs.STATUS_NONE, 0, 0, 0)

        fr = FakeReader()
        out = logs.download(fr, ['events'], datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual(out, {'events': []})
        # mode written 1 (enter) then 0 (exit)
        modes = [d for a, d in fr.writes if a == logs.DOWNLOAD_MODE]
        self.assertEqual(modes, [b'\x01\x00', b'\x00\x00'])
        # while in download mode a reprime hook was installed (not the original)
        self.assertTrue(callable(fr.hook_while_downloading))
        # and it was restored afterwards, so later polls don't keep re-writing download mode
        self.assertEqual(fr.on_connect, 'ORIGINAL')


if __name__ == '__main__':
    unittest.main()
