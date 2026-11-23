import os
import unittest
from unittest import mock

from web import server


def _handler(peer, command, path):
    """A Handler instance wired up enough to exercise _allowed() without a socket."""
    h = server.Handler.__new__(server.Handler)
    h.client_address = (peer, 12345)
    h.command = command
    h.path = path
    h.sent = []
    h._send = lambda code, body=None, *a, **k: h.sent.append((code, body))
    return h


class TestAccessGate(unittest.TestCase):
    def test_ingress_peer_has_full_access(self):
        for method, path in [('GET', '/api/live'), ('POST', '/api/config/write'),
                             ('DELETE', '/api/backups/123')]:
            h = _handler('172.30.32.2', method, path)
            self.assertTrue(h._allowed())
            self.assertEqual(h.sent, [])

    def test_non_ingress_refused_without_allow_any(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SELPI_WEB_ALLOW_ANY', None)
            h = _handler('192.168.1.50', 'GET', '/api/live')
            self.assertFalse(h._allowed())
            self.assertEqual(h.sent[0][0], 403)

    def test_allow_any_permits_read_only_endpoints(self):
        with mock.patch.dict(os.environ, {'SELPI_WEB_ALLOW_ANY': '1'}):
            for path in ('/api/live', '/api/status', '/api/live/'):
                h = _handler('192.168.1.50', 'GET', path)
                self.assertTrue(h._allowed(), path)
                self.assertEqual(h.sent, [])

    def test_allow_any_refuses_writes_and_other_reads(self):
        with mock.patch.dict(os.environ, {'SELPI_WEB_ALLOW_ANY': '1'}):
            for method, path in [('POST', '/api/config/write'), ('POST', '/api/backups'),
                                 ('POST', '/api/logs'), ('DELETE', '/api/backups/123'),
                                 ('GET', '/api/config/read'), ('GET', '/api/backups'),
                                 ('GET', '/api/backups/123/Config.SPLC'), ('GET', '/')]:
                h = _handler('192.168.1.50', method, path)
                self.assertFalse(h._allowed(), '%s %s' % (method, path))
                self.assertEqual(h.sent[0][0], 403, '%s %s' % (method, path))


if __name__ == '__main__':
    unittest.main()
