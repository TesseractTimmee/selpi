import json
import os
import threading
import unittest
import urllib.request

from web import api


class FakeInverter:
    versions = {'memory_map': 31}
    version_problems = []
    last_ok = None
    last_error = None
    busy = None

    def __init__(self):
        self.lock = threading.Lock()
        self.reads = []

    def use(self, what, fn, wait=False, check_versions=True):
        if not self.lock.acquire(blocking=False):
            raise Busy('busy: backup')
        try:
            return fn(self)
        finally:
            self.lock.release()

    def words(self, base, n):
        self.reads.append((base, n))
        return [0] * n


class Busy(Exception):
    pass


class TestCatalog(unittest.TestCase):
    def test_defaults_first_and_unique(self):
        cat = api.catalog()
        defaults = json.load(open(os.path.join(os.path.dirname(api.__file__), '..', 'sppro', 'default_fields.json')))
        self.assertEqual([f['key'] for f in cat[:len(defaults)]], list(defaults))
        self.assertEqual(cat[0]['name'], 'Load power')
        names = [f['name'] for f in cat]
        self.assertEqual(len(names), len(set(names)))
        by = {f['key']: f for f in cat}
        self.assertEqual(by['batt_soc_percent']['device_class'], 'battery')
        self.assertEqual(by['ac_loadk_wh_acc']['state_class'], 'total_increasing')
        self.assertEqual(by['ac_out_status']['options'], ['Econo', 'Idle', 'On', 'Sync'])
        self.assertEqual(by['inverter_run_hrs_total_acc']['state_class'], 'total_increasing')


class TestValues(unittest.TestCase):
    def setUp(self):
        self.inv = FakeInverter()
        self.api = api.Api(self.inv, Busy, lambda reason: None, lambda: [], {}, 'id1')

    def test_reads_only_needed_screens(self):
        out = self.api.values(['batt_soc_percent', 'nonsense'])
        self.assertEqual(set(out['values']), {'batt_soc_percent'})
        self.assertFalse(out['stale'])
        bases = {b for b, n in self.inv.reads}
        self.assertIn(0xa058, bases)               # Now screen
        self.assertEqual(len(bases), 2)            # + the scale block only

    def test_busy_serves_cache_then_fails_without(self):
        self.api.values(['batt_soc_percent'])
        self.inv.lock.acquire()
        try:
            out = self.api.values(['batt_soc_percent'])
            self.assertTrue(out['stale'])
            self.assertIn('batt_soc_percent', out['values'])
            with self.assertRaises(Busy):
                self.api.values(['load_ac_power'])
        finally:
            self.inv.lock.release()

    def test_enum_as_label(self):
        out = self.api.values(['digital_in_status1'])
        self.assertEqual(out['values']['digital_in_status1'], 'Inactive')


class TestHttp(unittest.TestCase):
    def test_token_required(self):
        a = api.Api(FakeInverter(), Busy, lambda r: None, lambda: [], {}, 'id1')
        srv = api.serve(a, 0, 'secret', host='127.0.0.1')
        port = srv.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen('http://127.0.0.1:%d/api/v1/info' % port)
            self.assertEqual(cm.exception.code, 401)
            req = urllib.request.Request('http://127.0.0.1:%d/api/v1/info' % port,
                                         headers={'Authorization': 'Bearer secret'})
            self.assertEqual(json.load(urllib.request.urlopen(req))['id'], 'id1')
        finally:
            srv.shutdown()


class SimInverter:
    """Inverter.use() over the simulator (for button presses through the API)."""
    versions = {'memory_map': 31}
    version_problems = []
    last_ok = last_error = busy = None

    def __init__(self):
        import struct
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(api.__file__), '..', 'tools'))
        import sim_sppro
        from sppro.reader import Reader
        self.sim = sim_sppro.SimProtocol({0xa000: struct.pack('<H', 0x0101)})
        self.reader = Reader(lambda: self.sim)

    def use(self, what, fn, wait=False, check_versions=True):
        return fn(self.reader)


class TestButtons(unittest.TestCase):
    def post(self, a, name):
        srv = api.serve(a, 0, 'secret', host='127.0.0.1')
        try:
            req = urllib.request.Request('http://127.0.0.1:%d/api/v1/button?name=%s' % (srv.server_address[1], name),
                                         data=b'', headers={'Authorization': 'Bearer secret'}, method='POST')
            try:
                return 200, json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
        finally:
            srv.shutdown()

    def setUp(self):
        self.inv = SimInverter()
        self.api = api.Api(self.inv, Busy, lambda r: None, lambda: [], {}, 'id1')

    def test_press(self):
        self.assertEqual(self.api.info()['buttons'], ['generator', 'silence'])
        code, body = self.post(self.api, 'generator')
        self.assertEqual(code, 200)
        self.assertEqual(body['register'], '0xa232')
        self.assertEqual(self.inv.sim.presses, [('generator', 1)])

    def test_on_and_unknown_refused(self):
        for name in ('on', 'nope', ''):
            code, body = self.post(self.api, name)
            self.assertEqual(code, 400, name)
        self.assertEqual(self.inv.sim.writes, [])
