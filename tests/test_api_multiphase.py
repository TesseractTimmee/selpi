"""web.api: the integration API exposes the Powerchain units and reads per-unit values (re-addressing the link)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from sppro import multiphase          # noqa: E402
from sppro.reader import Reader       # noqa: E402
from web import api as api_mod        # noqa: E402
import sim_sppro                      # noqa: E402

CAP = os.path.join(ROOT, 'captures', 'sl-20261117-071605.jsonl')


class Busy(Exception):
    pass


class FakeSppro:
    """Just enough of web.server.Inverter for the Api: runs fn against a Reader over the sim, caches units."""
    def __init__(self, sim):
        self.reader = Reader(lambda: sim, retries=1)
        self.versions = {'memory_map': 31}
        self.version_problems = []
        self.last_ok = self.last_error = self.busy = None
        self.units = None
        self.multi = False

    def use(self, what, fn, **kw):
        return fn(self.reader)

    def unit_list(self, **kw):
        if self.units is None:
            self.units = multiphase.detect(self.reader)
            self.multi = len(self.units) > 1
        return self.units


def make_api(sim):
    return api_mod.Api(FakeSppro(sim), Busy, backup_fn=lambda *_: None,
                       list_backups=lambda: [], clock={}, install_id='test')


@unittest.skipUnless(os.path.exists(CAP), 'capture not present (captures/ is git-ignored)')
class ApiMultiphase(unittest.TestCase):
    def mem(self):
        return sim_sppro.capture_memory(CAP)

    def test_info_lists_units(self):
        api = make_api(sim_sppro.SimProtocol(self.mem(), multiphase=3))
        info = api.info()
        self.assertTrue(info['multi'])
        real = [u for u in info['units'] if u['index'] != 'system']   # the physical units (system is synthetic)
        self.assertEqual([u['index'] for u in real], [0, 1, 2])
        self.assertEqual([u['serial'] for u in real], [10000001, 10000002, 10000003])

    def test_per_unit_values_differ(self):
        # the sim seeds a different Battery SoC per unit (0xa081 = (50 + idx*10) %) -> per-unit reads must differ
        api = make_api(sim_sppro.SimProtocol(self.mem(), multiphase=3))
        api.info()                                        # triggers unit discovery
        soc1 = api.values(['batt_soc_percent'], unit=1)['values']['batt_soc_percent']
        soc2 = api.values(['batt_soc_percent'], unit=2)['values']['batt_soc_percent']
        self.assertAlmostEqual(soc1, 60, delta=1)
        self.assertAlmostEqual(soc2, 70, delta=1)

    def test_system_unit_listed_when_multi(self):
        api = make_api(sim_sppro.SimProtocol(self.mem(), multiphase=3))
        info = api.info()
        sysu = [u for u in info['units'] if u['index'] == 'system']
        self.assertEqual(len(sysu), 1)
        self.assertEqual(sysu[0]['label'], 'System')

    def test_system_totals_sum_and_avg(self):
        api = make_api(sim_sppro.SimProtocol(self.mem(), multiphase=3))
        api.info()
        # avg: SoC is seeded 50/60/70 per unit -> system average 60
        socs = [api.values(['batt_soc_percent'], unit=i)['values']['batt_soc_percent'] for i in (0, 1, 2)]
        sys_soc = api.values(['batt_soc_percent'], unit='system')['values']['batt_soc_percent']
        self.assertAlmostEqual(sys_soc, sum(socs) / 3, delta=0.5)
        # sum: a power field (kW) adds across the units
        per = [api.values(['load_ac_power'], unit=i)['values'].get('load_ac_power') for i in (0, 1, 2)]
        sys_p = api.values(['load_ac_power'], unit='system')['values'].get('load_ac_power')
        self.assertAlmostEqual(sys_p, sum(x for x in per if x is not None), delta=0.01)

    def test_system_values_without_prior_units_call(self):
        # regression: after an add-on restart the integration keeps polling /values (no fresh /units call), so the
        # Api must discover units lazily. Otherwise the 'system' read aggregates over zero units -> all-None ->
        # stale/empty, and the System device goes unavailable until a reload.
        api = make_api(sim_sppro.SimProtocol(self.mem(), multiphase=3))
        got = api.values(['load_ac_power', 'batt_soc_percent'], unit='system')   # no api.info()/units() first
        self.assertFalse(got['stale'])
        self.assertIsNotNone(got['values'].get('load_ac_power'))
        self.assertIsNotNone(got['values'].get('batt_soc_percent'))

    def test_single_inverter_no_units_no_readdress(self):
        sim = sim_sppro.SimProtocol(self.mem())           # single
        api = make_api(sim)
        info = api.info()
        self.assertFalse(info['multi'])
        self.assertEqual(info['units'], [{'index': 0, 'serial': info['units'][0]['serial'],
                                          'phase': info['units'][0]['phase'], 'role': info['units'][0]['role'],
                                          'label': info['units'][0]['label'], 'model': info['units'][0]['model']}])
        api.values(['batt_soc_percent'])                  # unit=None on a single inverter
        self.assertEqual([a for a, _ in sim.writes if a == multiphase.REG_GATEWAY], [])   # no re-address


if __name__ == '__main__':
    unittest.main()
