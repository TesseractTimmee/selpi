"""SP LINK proxy against the simulator (tools/sim_sppro.py) over the app's shared TCP link."""
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAPTURE = os.path.join(ROOT, 'captures', 'sl-20261117-071605.jsonl')
PASSWORD = 'testpass'

from memory import CRC  # noqa: E402
from web import proxy  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def q(addr, words):
    m = b'Q' + bytes([words - 1]) + struct.pack('<I', addr)
    return m + CRC(m).as_bytes()


def w(addr, data):
    m = b'W' + bytes([len(data) // 2 - 1]) + struct.pack('<I', addr)
    m += CRC(m).as_bytes() + data
    return m + CRC(m).as_bytes()


def recv(s, n, wait=3.0):
    s.settimeout(wait)
    buf = b''
    try:
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                break
            buf += c
    except (socket.timeout, ConnectionResetError):
        pass
    return buf


class TestRules(unittest.TestCase):
    def test_log_download_writes(self):
        self.assertTrue(proxy.is_log_download_write(w(0xa251, struct.pack('<H', 1))))
        self.assertFalse(proxy.is_log_download_write(w(0xa251, struct.pack('<H', 2))))
        self.assertTrue(proxy.is_log_download_write(w(0xa2d4, bytes(10))))
        self.assertFalse(proxy.is_log_download_write(w(0xa2d4, bytes(2))))
        self.assertFalse(proxy.is_log_download_write(w(0xc185, struct.pack('<H', 2183))))

    def test_nets(self):
        n = proxy.nets(['192.168.192.105', '10.0.0.0/8', ''])
        self.assertTrue(proxy.in_nets('192.168.192.105', n))
        self.assertTrue(proxy.in_nets('::ffff:10.1.2.3', n))
        self.assertFalse(proxy.in_nets('192.168.192.106', n))
        self.assertFalse(proxy.in_nets('nonsense', n))

    def test_lengths(self):
        self.assertEqual(proxy.reply_len(q(0xa007, 25)), 8 + 50 + 2)
        f = w(0xa2d4, bytes(10))
        self.assertEqual(proxy.request_len(f), len(f))
        self.assertEqual(proxy.reply_len(f), len(f))


@unittest.skipUnless(os.path.exists(CAPTURE), 'simulator capture not present (captures/ is git-ignored)')
class TestProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import sim_sppro
        cls.sim = sim_sppro.SimProtocol(sim_sppro.capture_memory(CAPTURE))
        cls.sim_port = free_port()
        threading.Thread(target=sim_sppro.serve, args=(cls.sim, cls.sim_port, PASSWORD), daemon=True).start()
        time.sleep(0.3)
        os.environ.update(SELPI_CONNECTION_TYPE='TCP', SELPI_CONNECTION_TCP_HOSTNAME='127.0.0.1',
                          SELPI_CONNECTION_TCP_PORT=str(cls.sim_port), SELPI_SPPRO_PASSWORD=PASSWORD)
        from web import server
        cls.server = server

    def start(self, **kw):
        kw.setdefault('allowed', ['127.0.0.0/8', '::1/128'])
        p = proxy.Proxy(self.server.SPPRO, self.server.Busy, 0, host='127.0.0.1', frame_wait=5, **kw).start()
        return p

    def connect(self, p):
        s = socket.create_connection(('127.0.0.1', p.port))
        self.addCleanup(s.close)
        time.sleep(0.2)
        return s

    def test_read_only_client(self):
        p = self.start()
        s = self.connect(p)
        s.sendall(q(0xa007, 25))
        r = recv(s, 8 + 2 * 25 + 2)
        self.assertEqual(len(r), 60)
        self.assertEqual(CRC(r).as_int(), 0)
        s.sendall(w(0xc185, struct.pack('<H', 2183)))                 # a setting: refused, no reply
        self.assertEqual(recv(s, 12, wait=1.5), b'')
        self.assertEqual(p.status['refused_writes'], 1)
        f = w(0xa251, struct.pack('<H', 0))                             # log download mode: allowed
        s.sendall(f)
        self.assertEqual(recv(s, len(f)), f)
        self.assertEqual(p.status['client'], '127.0.0.1')
        self.assertFalse(p.status['may_write'])
        # a second client is turned away while the first is connected
        s2 = socket.create_connection(('127.0.0.1', p.port))
        self.assertEqual(recv(s2, 1, wait=1.5), b'')
        s2.close()
        # garbage before a frame is skipped
        s.sendall(b'\x00\x01junk' + q(0xa081, 1))
        r = recv(s, 12)
        self.assertEqual(len(r), 12)

    def test_writer_client(self):
        p = self.start(write=['127.0.0.1'])
        s = self.connect(p)
        f = w(0xc185, struct.pack('<H', 2183))
        s.sendall(f)
        self.assertEqual(recv(s, len(f)), f)
        self.assertTrue(p.status['may_write'])
        self.assertIn((0xc185, struct.pack('<H', 2183)), [(a, bytes(d)) for a, d in self.sim.writes])

    def test_not_allowed(self):
        p = self.start(allowed=['10.0.0.0/8'])
        s = socket.create_connection(('127.0.0.1', p.port))
        self.addCleanup(s.close)
        s.sendall(q(0xa081, 1))
        self.assertEqual(recv(s, 12, wait=1.5), b'')
        self.assertIsNone(p.status['client'])


if __name__ == '__main__':
    unittest.main()
