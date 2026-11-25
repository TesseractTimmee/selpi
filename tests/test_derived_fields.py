"""selpi's derived fields: the signed SP PRO powers split into >= 0 parts (in = negative part, out = positive)."""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from sppro import live  # noqa: E402

BY_KEY = {f['key']: f for f in live.FIELDS}
PAIRS = {'ac_source_power': ('ac_source_power_in', 'ac_source_power_out'),
         'dc_battery_power': ('dc_battery_power_in', 'dc_battery_power_out')}
CAPTURE = os.path.join(ROOT, 'captures', 'sl-20261117-071605.jsonl')


def mem_with(field, raw):
    """Memory with plausible scales and the source field's words set to the signed raw value."""
    mem = {live.SCALE_BASE + i: 1000 for i in range(len(live.SCALES))}
    words = [int(a, 16) for a in field['words']]
    if len(words) == 1:
        mem[words[0]] = raw & 0xffff
    else:
        v = raw & 0xffffffff
        mem[words[0]], mem[words[1]] = v & 0xffff, v >> 16
    return mem


class DerivedFields(unittest.TestCase):
    def test_defined_next_to_their_source(self):
        for src, (i, o) in PAIRS.items():
            for k in (i, o):
                f = BY_KEY[k]
                self.assertEqual(f['words'], BY_KEY[src]['words'])
                self.assertEqual((f['unit'], f['kind'], f['field']), (BY_KEY[src]['unit'], 'number', None))

    def test_split_by_sign(self):
        for src, (i, o) in PAIRS.items():
            for raw in (-1234, 0, 1234):
                mem = mem_with(BY_KEY[src], raw)
                s = live.evaluate(BY_KEY[src], mem)[0]
                vi, vo = live.evaluate(BY_KEY[i], mem)[0], live.evaluate(BY_KEY[o], mem)[0]
                self.assertGreaterEqual(vi, 0)
                self.assertGreaterEqual(vo, 0)
                self.assertAlmostEqual(vo - vi, s)          # out - in = the signed value
                self.assertEqual(vi * vo, 0)                 # never both

    @unittest.skipUnless(os.path.exists(CAPTURE), 'capture not present (captures/ is git-ignored)')
    def test_on_real_memory(self):
        import sim_sppro
        mem = {a: struct.unpack('<H', b)[0] for a, b in sim_sppro.capture_memory(CAPTURE).items()}
        for src, (i, o) in PAIRS.items():
            s = live.evaluate(BY_KEY[src], mem)[0]
            vi, vo = live.evaluate(BY_KEY[i], mem)[0], live.evaluate(BY_KEY[o], mem)[0]
            self.assertAlmostEqual(vo - vi, s)

    def test_grid_and_generator_follow_status_and_sync(self):
        src = BY_KEY['ac_source_power']
        # (Generator Status, Output Mode) -> which of grid / generator gets the AC source power
        cases = {(10, 3): 'grid', (1, 3): 'generator',
                 (0, 3): 'grid', (5, 3): 'grid', (9, 3): 'grid',     # no generator in use: synchronised = the grid
                 (6, 3): 'generator', (8, 3): 'generator',           # two-stage start (loads first) / stopping
                 (10, 2): None, (1, 2): None, (5, 2): None, (9, 0): None}   # not synchronised: nothing drawn
        for (status, mode), who in cases.items():
            for raw in (-1500, 1500):          # import, export
                mem = mem_with(src, raw)
                mem[0xa096], mem[0xa07d] = status, mode
                v = {k: live.evaluate(BY_KEY[k], mem)[0]
                     for k in ('ac_source_power_in', 'ac_source_power_out', 'grid_power_in', 'grid_power_out',
                               'generator_power_in')}
                self.assertAlmostEqual(v['grid_power_in'], v['ac_source_power_in'] if who == 'grid' else 0)
                self.assertAlmostEqual(v['grid_power_out'], v['ac_source_power_out'] if who == 'grid' else 0)
                self.assertAlmostEqual(v['generator_power_in'], v['ac_source_power_in'] if who == 'generator' else 0)

    def test_grid_fields_need_the_status_word(self):
        mem = mem_with(BY_KEY['ac_source_power'], -1500)   # no 0xa096 / 0xa07d
        self.assertIsNone(live.evaluate(BY_KEY['grid_power_in'], mem)[0])

    def test_variable_create_still_works(self):
        from memory import variable
        self.assertNotIn(None, variable.live_names())
        self.assertIsNotNone(variable.live_field('ac_source_power_in'))


if __name__ == '__main__':
    unittest.main()


class SysRegFields(unittest.TestCase):
    """The SysReg status cluster added for the Modbus→ESP changeover (all on the Now block, free reads)."""
    def test_defaults_named_and_enabled(self):
        from web import api
        cat = {c['key']: c for c in api.catalog()}
        self.assertEqual(cat['sys_reg_active_schedule']['name'], 'Solar Hybrid Priority')
        self.assertEqual(cat['sys_reg_export_on_demand_target']['name'], 'Export On Demand Target')
        for k in ('sys_reg_active_schedule', 'sys_reg_export_on_demand_target'):
            self.assertTrue(cat[k]['default'], k)
            self.assertEqual(cat[k]['screen'], 'NowSubTab', k)
        # the status/limit extras exist but are off by default
        for k in ('sys_reg_charger_status', 'sys_reg_inverter_lockout_status',
                  'sys_reg_source_disconnect_status', 'sys_reg_input_power_limit', 'sys_reg_support_power_limit'):
            self.assertIn(k, cat)
            self.assertFalse(cat[k]['default'], k)

    def test_ac_coupled_percent_fields_off_by_default(self):
        from web import api
        cat = {c['key']: c for c in api.catalog()}
        for suf in ('l1', 'l2', 'l3', 'l4'):
            k = 'multi_ac_coupled_percent_' + suf
            self.assertIn(k, cat)
            self.assertFalse(cat[k]['default'], k)        # "most people won't care" -> off
            self.assertEqual(cat[k]['unit'], '%', k)
            self.assertEqual(cat[k]['screen'], 'DataMultiPhase1', k)
        # raw/10 => percent (e.g. 10 -> 1.0 %, matching the Modbus "1 %" reading)
        f = BY_KEY['multi_ac_coupled_percent_l1']
        self.assertAlmostEqual(live.evaluate(f, mem_with(f, 10))[0], 1.0)

    def test_active_schedule_is_raw(self):
        f = BY_KEY['sys_reg_active_schedule']
        self.assertEqual(f['words'], ['0xa070'])
        self.assertEqual(live.evaluate(f, mem_with(f, 6))[0], 6)      # priority 6, raw

    def test_export_on_demand_target_is_deca_watt_kw(self):
        f = BY_KEY['sys_reg_export_on_demand_target']
        self.assertEqual(f['unit'], 'kW')
        self.assertAlmostEqual(live.evaluate(f, mem_with(f, 150))[0], 1.5)    # deca-watt: raw/100
        self.assertAlmostEqual(live.evaluate(f, mem_with(f, -50))[0], -0.5)   # signed


class FeatureGate(unittest.TestCase):
    """Feature gating: fields tagged bms/ac_solar are present only when the unit has that feature."""
    class _R:
        def __init__(self, mem): self.mem = mem
        def words(self, base, n): return [self.mem.get(base + i, 0) for i in range(n)]

    def test_present_features_probe(self):
        nk = int(BY_KEY['number_of_kacos']['words'][0], 16)
        all3 = live.present_features(self._R({nk: 2, 0xa276: 5, 0xa021: 0}))   # kaco + BMS + battery (0xa021=0)
        self.assertEqual(all3, {'ac_solar', 'bms', 'battery'})
        batteryless = live.present_features(self._R({nk: 0, 0xa276: 0, 0xa021: 1}))   # none, and batteryless unit
        self.assertEqual(batteryless, set())
        batt_only = live.present_features(self._R({nk: 0, 0xa276: 0, 0xa021: 0}))   # has a battery, no kaco/bms
        self.assertEqual(batt_only, {'battery'})

    def test_field_available_map_gate(self):
        f = {'feature': None, 'needs_map_version': 25}
        self.assertFalse(live.field_available(f, None, 20))     # unit map 20 < 25 -> gated out
        self.assertTrue(live.field_available(f, None, 31))      # unit map 31 >= 25 -> available
        self.assertTrue(live.field_available(f, None, None))    # map unknown -> keep
        g = {'feature': 'bms', 'needs_map_version': None}
        self.assertFalse(live.field_available(g, {'ac_solar'}, 31))   # bms absent
        self.assertTrue(live.field_available(g, {'bms'}, 31))

    def test_snapshot_hides_absent_features(self):
        # scale block present so numeric fields evaluate; no BMS/Kaco data
        mem = {live.SCALE_BASE + i: 1000 for i in range(len(live.SCALES))}
        r = type('R', (), {'words': lambda self, b, n: [mem.get(b + i, 0) for i in range(n)]})()
        rows = {row['key'] for row in live.snapshot(r, None, features={'ac_solar'})}
        self.assertNotIn('tech_bms_so_c', rows)            # bms feature absent -> hidden
        self.assertNotIn('now_percent_kaco', rows) if 'now_percent_kaco' not in rows else None
        self.assertIn('now_total_ac_power_kaco', rows)     # ac_solar present -> shown
