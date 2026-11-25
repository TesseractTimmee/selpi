"""The kept-open Select.Live session is held across polls (fewer logins), but TCP/ESP is released sooner."""
import os
import unittest
from datetime import datetime, timedelta

from web import server


class _FakeReader:
    def __init__(self, closed):
        self._closed = closed

    def close(self):
        self._closed.append(1)


class TestIdleClose(unittest.TestCase):
    def _inv(self, seconds_idle):
        inv = server.Inverter()
        closed = []
        inv.reader = _FakeReader(closed)
        inv.last_ok = datetime.now() - timedelta(seconds=seconds_idle)
        return inv, closed

    def tearDown(self):
        os.environ.pop('SELPI_CONNECTION_TYPE', None)

    def test_selectlive_keeps_session_within_idle_window(self):
        os.environ['SELPI_CONNECTION_TYPE'] = 'SelectLive'
        inv, closed = self._inv(server.SELECTLIVE_IDLE - 30)   # steady 60 s polling never reaches the window
        inv.close_if_idle()
        self.assertIsNotNone(inv.reader)                       # session kept -> no reconnect/login churn
        self.assertEqual(closed, [])

    def test_selectlive_drops_once_truly_idle(self):
        os.environ['SELPI_CONNECTION_TYPE'] = 'SelectLive'
        inv, closed = self._inv(server.SELECTLIVE_IDLE + 10)   # polling stopped: let it go
        inv.close_if_idle()
        self.assertIsNone(inv.reader)
        self.assertEqual(closed, [1])

    def test_tcp_released_sooner(self):
        os.environ['SELPI_CONNECTION_TYPE'] = 'TCP'
        inv, closed = self._inv(server.TCP_IDLE + 10)          # past the short TCP window -> free the ESP slot
        inv.close_if_idle()
        self.assertIsNone(inv.reader)
        self.assertEqual(closed, [1])


if __name__ == '__main__':
    unittest.main()
