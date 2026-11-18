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

    def use(self, what, fn, wait=False):
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
