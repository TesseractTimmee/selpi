"""Settings meanings and SP LINK's rules (sppro/settings.py, settings_meta.json, settings_rules.json)."""
import unittest

from sppro import settings as S

CELLS = 24   # SPMC482


class TestConversions(unittest.TestCase):
    def roundtrip(self, name, raw, text):
        self.assertEqual(S.to_display(name, raw, CELLS)[1], text)
        back, warnings = S.from_display(name, text, CELLS)
        self.assertEqual(back, raw)
        return warnings

    def test_kinds(self):
        self.roundtrip('GeneratorRemoteStart', 2, 'Digital Control Input 2')      # pick-list
        self.roundtrip('EqualiseV', 2346, '56.3 V')                               # DC volts per cell x 24
        self.roundtrip('BeeperLockoutStartTime', 1320, '22:00')                    # minutes since midnight
        self.roundtrip('SystemSchedule1StopDate', 3103, '31 Dec')                 # packed day / month
        self.roundtrip('Port1Baudrate', 6, '57600')                               # baud code
        self.roundtrip('SystemSchedule1AcInputCapacity', 1500, '15.0 kW')          # input power, 0.01 kW
        self.roundtrip('BatteryType', 17, 'BMS - Discover')

    def test_range_warning_and_errors(self):
        self.assertTrue(self.roundtrip('EqualiseTime', 6, '0.1 hours'))           # SP LINK: 1 - 10 h
        with self.assertRaises(S.SettingError):
            S.from_display('GeneratorRemoteStart', 'Digital Control Input 9', CELLS)
        with self.assertRaises(S.SettingError):
            S.from_display('EqualiseV', '56.3', None)                              # DC volts need the cell count
        with self.assertRaises(S.SettingError):
            S.from_display('BeeperLockoutStartTime', '25:00', CELLS)

    def test_addresses_and_screen(self):
        self.assertEqual(S.address('GeneratorRemoteStart'), 0xc0a8)
        self.assertEqual(S.address('Port1AccInput'), 0xc087)                     # unnamed in SP LINK's table
        self.assertIn('Generator Inputs', S.where('GeneratorRemoteStart'))


class TestRules(unittest.TestCase):
    def test_cross_setting_rules(self):
        ok = {'LowDcShutDown0PercentLoad': 2083, 'LowDcShutDown100PercentLoad': 2000,
              'AbsoluteVoltageLimit': 2358, 'HighDcAlertClear': 2367, 'HighDcAlert': 2400}
        ids = {p['id'] for p in S.check(ok, CELLS, 'SPMC482') if p['severity'] == 'error'}
        self.assertNotIn('dc_shutdown_0_ge_100', ids)
        self.assertNotIn('dc_high_alert_clear_vs_absolute', ids)
        bad = dict(ok, LowDcShutDown0PercentLoad=1990, HighDcAlertClear=2400)
        ids = {p['id'] for p in S.check(bad, CELLS, 'SPMC482') if p['severity'] == 'error'}
        self.assertIn('dc_shutdown_0_ge_100', ids)
        self.assertIn('dc_high_alert_clear_vs_absolute', ids)

    def test_one_function_per_input(self):
        cfg = {'GeneratorRemoteStart': 2, 'GridAvailableInput': 2}
        probs = [p for p in S.check(cfg, CELLS, 'SPMC482') if p['id'] == 'io_unique_digital_inputs']
        self.assertEqual(len(probs), 1)
        self.assertIn('Digital Control Input 2', probs[0]['message'])
        cfg = {'AcInputPowerLevelInput': 5, 'ExportModeControlInput': 5}       # Follow Backup Select: shared is fine
        self.assertFalse([p for p in S.check(cfg, CELLS, 'SPMC482') if p['id'] == 'io_unique_digital_inputs'])

    def test_send_checks(self):
        old = {'NominalFrequency': 0}
        unit = dict(output_mode='Sync', hardware_revision=20, comms_card_revision=1, comms_card_attached=True,
                    region='None', factory_export_limit=None)
        ids = {p['id'] for p in S.send_check({'NominalFrequency': 1}, old, unit, {'port': 1}, CELLS, 'SPMC482')}
        self.assertIn('frequency_needs_idle', ids)
        unit['output_mode'] = 'Idle'
        ids = {p['id'] for p in S.send_check({'NominalFrequency': 1}, old, unit, {'port': 1}, CELLS, 'SPMC482')
               if p['severity'] == 'error'}
        self.assertNotIn('frequency_needs_idle', ids)

    def test_as4777_needs_a_region(self):
        app = int(next(k for k, v in S.META['ApplicationType']['values'].items() if 'AS4777' in v))
        ids = lambda cfg: {p['id'] for p in S.check(cfg, CELLS, 'SPMC482')}
        self.assertEqual(S.to_display('Region', 0, CELLS)[1], 'None')          # SP LINK's blank entry
        self.assertIn('as4777_needs_region', ids({'ApplicationType': app, 'Region': 0}))
        self.assertNotIn('as4777_needs_region', ids({'ApplicationType': app, 'Region': 1}))

    def test_labels_are_clean(self):
        import json, os
        lay = json.load(open(os.path.join(os.path.dirname(S.__file__), 'settings_layout.json')))
        texts = [x for t in lay['tabs'] for st in t['subtabs'] for g in st['groups']
                 for x in [g.get('title')] + [r.get('label') for r in g['rows']] if x]
        self.assertFalse([x for x in texts if '"' in x or '  ' in x or x != x.strip()])

    def test_models(self):
        self.assertEqual(S.cells_for('SPMC482'), 24)
        self.assertEqual(S.cells_for('SPMC241'), 12)
        self.assertEqual(S.cells_for('SPLC1202'), 60)


if __name__ == '__main__':
    unittest.main()
