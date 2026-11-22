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

    def use(self, what, fn, wait=False, check_versions=True, deadline=None, key=None):
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
        self.assertEqual(cat[0]['name'], 'Load Power')
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

    def test_renamed_keys_still_answered(self):
        out = self.api.values(['days_to_equalise', 'daysto_recharge'])
        self.assertEqual(set(out['values']), {'days_to_equalise', 'daysto_recharge'})
        self.assertNotIn('daysto_equalise', self.api.fields)

    def test_enum_as_label(self):
        out = self.api.values(['digital_in_status1'])
        self.assertEqual(out['values']['digital_in_status1'], 'Inactive')

    def test_partial_poll_keeps_last_value_and_marks_stale(self):
        # a flaky Select.Live link where one block (the Now screen, 0xa058) times out must not fail the whole
        # poll: the field on it keeps its last good value (served stale) instead of being wiped to None.
        self.api.values(['batt_soc_percent'])          # first poll populates the cache
        self.assertIn('batt_soc_percent', self.api.cache)
        good = self.inv.words
        def flaky(base, n):
            if base == 0xa058:
                raise TimeoutError('The read operation timed out')
            return good(base, n)
        self.inv.words = flaky
        out = self.api.values(['batt_soc_percent'])    # does NOT raise despite the failed block
        self.assertTrue(out['stale'])                  # partial poll is flagged stale
        self.assertIn('batt_soc_percent', out['values'])   # last good value still served, not overwritten


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

    def use(self, what, fn, wait=False, check_versions=True, deadline=None, key=None):
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
        self.assertEqual(self.api.info()['buttons'], ['generator', 'silence', 'on'])
        code, body = self.post(self.api, 'generator')
        self.assertEqual(code, 200)
        self.assertEqual(body['register'], '0xa232')
        self.assertEqual(self.inv.sim.presses, [('generator', 1)])

    def test_on_needs_confirm(self):
        code, body = self.post(self.api, 'on')
        self.assertEqual(code, 400)
        self.assertIn('confirm=outage', body['error'])
        self.assertEqual(self.inv.sim.writes, [])
        code, body = self.post(self.api, 'on&confirm=outage')
        self.assertEqual(code, 200)
        self.assertEqual(body['register'], '0xa233')
        self.assertEqual(self.inv.sim.presses, [('on', 2)])

    def test_unknown_refused(self):
        for name in ('nope', ''):
            code, body = self.post(self.api, name)
            self.assertEqual(code, 400, name)
        self.assertEqual(self.inv.sim.writes, [])


class TestControls(unittest.TestCase):
    """State-aware controls through the API (generator / output / equalise) over the simulator."""

    def post(self, query):
        srv = api.serve(self.api, 0, 'secret', host='127.0.0.1')
        try:
            req = urllib.request.Request('http://127.0.0.1:%d/api/v1/control?%s' % (srv.server_address[1], query),
                                         data=b'', headers={'Authorization': 'Bearer secret'}, method='POST')
            try:
                return 200, json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
        finally:
            srv.shutdown()

    def setUp(self):
        import struct
        self.inv = SimInverter()
        self.inv.version_problems = []
        self.inv.sim.mem[0xa096] = struct.pack('<H', 0)      # generator Not Running
        self.inv.sim.mem[0xa07d] = struct.pack('<H', 2)      # output On
        self.inv.sim.TRANSITION = 60                         # stays Starting for the test
        self.api = api.Api(self.inv, Busy, lambda r: None, lambda: [], {}, 'id1')

    def test_info_lists_controls(self):
        self.assertEqual(self.api.info()['controls'], ['equalise', 'generator', 'output'])

    def test_generator_on_once(self):
        code, body = self.post('name=generator&state=on')
        self.assertEqual((code, body['pressed'], body['status']), (200, True, 'Not Running'))
        code, body = self.post('name=generator&state=on')      # now Starting: already on, no press
        self.assertEqual((code, body['pressed']), (200, False))
        self.assertEqual(self.inv.sim.presses, [('generator', 1)])

    def test_press_not_yet_visible_blocks_a_second(self):
        import struct
        self.post('name=generator&state=on')
        self.inv.sim._gen_change = None
        self.inv.sim.mem[0xa096] = struct.pack('<H', 0)      # the unit hasn't shown the start yet
        code, body = self.post('name=generator&state=on')
        self.assertEqual(code, 409)
        self.assertIn('still being processed', body['error'])
        self.assertEqual(len(self.inv.sim.presses), 1)

    def test_generator_refused_on_grid(self):
        import struct
        self.inv.sim.mem[0xa096] = struct.pack('<H', 10)     # AC Source Present
        code, body = self.post('name=generator&state=on')
        self.assertEqual(code, 409)
        self.assertIn('AC Source Present', body['error'])
        self.assertEqual(self.inv.sim.writes, [])

    def test_output_idle_needs_confirm(self):
        code, body = self.post('name=output&state=idle')
        self.assertEqual(code, 400)
        self.assertEqual(self.inv.sim.writes, [])
        code, body = self.post('name=output&state=idle&confirm=outage')
        self.assertEqual((code, body['pressed'], body['mode']), (200, True, 'On'))
        self.assertEqual(self.inv.sim.presses, [('on', 2)])

    def test_equalise(self):
        code, body = self.post('name=equalise')
        self.assertEqual(code, 200)
        self.assertEqual(self.inv.sim.presses, [('generator', 2), ('generator', 2)])
        self.assertTrue(self.inv.sim.equalise_requested)

    def test_bad_requests(self):
        for q in ('name=nope', 'name=generator', 'name=generator&state=idle', 'name=output&state=off'):
            code, body = self.post(q)
            self.assertEqual(code, 400, q)
        self.assertEqual(self.inv.sim.writes, [])

    def test_unverified_versions_refused(self):
        self.inv.version_problems = ['memory map 32 (verified: 31)']
        code, body = self.post('name=generator&state=on')
        self.assertEqual(code, 409)
        self.assertEqual(self.inv.sim.writes, [])
