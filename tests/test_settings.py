"""Settings meanings and SP LINK's rules (sppro/settings.py, settings_meta.json, settings_rules.json)."""
import math
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

    def test_baud_needs_hw20(self):
        b115 = int(next(k for k, v in S.META['Port1Baudrate']['values'].items() if v == '115200'))
        b576 = int(next(k for k, v in S.META['Port1Baudrate']['values'].items() if v == '57600'))
        new = {'Port1Baudrate': b115, 'Port2Baudrate': b576}
        unit = dict(output_mode='Idle', hardware_revision=15, comms_card_revision=1, comms_card_attached=True,
                    region='None', factory_export_limit=None)
        err = lambda u: {p['id'] for p in S.send_check(new, {'Port1Baudrate': b576}, u, {'port': 1}, CELLS, 'SPMC482')
                         if p['severity'] == 'error'}
        self.assertIn('baud_needs_hw20', err(unit))               # old hardware: >57600 refused
        self.assertNotIn('baud_needs_hw20', err(dict(unit, hardware_revision=24)))  # rev >= 20: fine

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
        self.assertEqual(S.to_display('Region', 0, CELLS)[1], 'None')          # not set
        self.assertEqual(S.to_display('Region', 1, CELLS)[1], 'Australia A')   # real region word, not the percent box
        self.assertIn('Region', S.where('Region'))
        self.assertIn('as4777_needs_region', ids({'ApplicationType': app, 'Region': 0}))
        self.assertNotIn('as4777_needs_region', ids({'ApplicationType': app, 'Region': 1}))

    def test_labels_are_clean(self):
        import json, os
        lay = json.load(open(os.path.join(os.path.dirname(S.__file__), 'settings_layout.json')))
        texts = [x for t in lay['tabs'] for st in t['subtabs'] for g in st['groups']
                 for x in [g.get('title')] + [r.get('label') for r in g['rows']] if x]
        self.assertFalse([x for x in texts if '"' in x or '  ' in x or x != x.strip()])

    def test_rollover_date(self):
        self.assertIn(('YearToDateRollOverDay', 'YearToDateRollOverMonth'), S.date_pairs())
        self.assertEqual(S.from_display('YearToDateRollOverMonth', 'jul', CELLS)[0], 7)
        ids = lambda d, m: {p['id'] for p in S.check({'YearToDateRollOverDay': d, 'YearToDateRollOverMonth': m})}
        self.assertIn('date_valid', ids(31, 4))
        self.assertIn('date_leap_day', ids(29, 2))
        self.assertFalse({'date_valid', 'date_leap_day'} & ids(1, 7))

    def test_dc_limits_follow_battery_type(self):
        self.assertEqual(S.limits('EqualiseV', CELLS, 'BMS - Discover'), (48.0, 64.8))
        self.assertEqual(S.limits('FloatV', CELLS, 'Lithium LiFePO4'), (48.0, 62.4))
        self.assertEqual(S.limits('FloatV', CELLS, 'Custom'), (48.0, 68.4))
        self.assertEqual(S.limits('LowDcShutDownRecovery', CELLS, 'Lead Acid Sealed'), (45.6, 52.8))
        # Initial Return: SP LINK's maximum is min(Float, Float Return) x 0.995
        self.assertEqual(S.limits('InitialReturnVoltage', CELLS, 'BMS - Discover', {'FloatV': 55.8, 'FloatReturn': 54.0}),
                         (45.6, 53.7))
        warn = S.from_display('FloatV', '61', CELLS, 'Lead Acid Sealed')[1]
        self.assertTrue(warn)
        self.assertFalse(S.from_display('FloatV', '61', CELLS, 'Custom')[1])

    def test_battery_type_change_flags_voltages(self):
        cfg = {'BatteryType': 17, 'LowDcShutDownRecovery': 2208}          # 53.0 V: fine for BMS - Discover
        self.assertNotIn('dc_voltage_range', {p['id'] for p in S.check(cfg, CELLS, 'SPMC482')})
        cfg['BatteryType'] = 0                                              # Lead Acid Sealed: max 52.8 V
        self.assertIn('dc_voltage_range', {p['id'] for p in S.check(cfg, CELLS, 'SPMC482')})

    def test_inverters_per_phase_and_mppt_model(self):
        self.assertEqual(S.to_display('AdvancedMultiphaseNumberOfWorkers', 0, CELLS)[1], '1')    # stored = shown - 1
        self.assertEqual(S.from_display('AdvancedMultiphaseNumberOfWorkers', '3', CELLS), (2, []))
        self.assertTrue(S.from_display('AdvancedMultiphaseNumberOfWorkers', '5', CELLS)[1])      # SP LINK: 1 - 4
        self.assertEqual(S.to_display('MpptModel', 1, CELLS)[1], 'Morningstar')
        self.assertEqual(S.from_display('MpptModel', 'AERL', CELLS)[0], 2)

    def test_power_factor(self):
        r = S.from_display('SupportPowerFactor', '0.95 Leading', CELLS)[0]
        self.assertEqual(r, (-round(math.tan(math.acos(0.95)) * 4096)) & 0xffff)
        self.assertEqual(S.to_display('SupportPowerFactor', r, CELLS)[1], '0.950 Leading')
        self.assertEqual(S.to_display('SupportPowerFactor', 0, CELLS)[1], '1.000 Lagging')
        self.assertEqual(S.from_display('CosPowerFactorEnd', '0.9', CELLS)[0], 410)          # (1 - 0.9) x 4096
        self.assertEqual(S.to_display('ConstantVarPercent', S.from_display('ConstantVarPercent', '12.5 leading')[0])[1],
                         '12.5 % Leading')
        self.assertTrue(S.from_display('SupportPowerFactor', '0.4', CELLS)[1])               # SP LINK: 0.500 - 1.000

    def test_installer_only_flagged(self):
        self.assertTrue(S.installer_only('ACVMin'))
        self.assertTrue(S.installer_only('VoltVarLowVoltageStart'))
        self.assertFalse(S.installer_only('EqualiseV'))

    def test_firmware_min_version(self):
        self.assertEqual(S.min_version('AdvancedMultiPhaseStructure'), 38)   # Powerchain needs config v38
        self.assertIsNone(S.min_version('EqualiseV'))                        # always present
        self.assertTrue(S.available('AdvancedMultiPhaseStructure', 48))
        self.assertFalse(S.available('AdvancedMultiPhaseStructure', 30))
        self.assertTrue(S.available('AdvancedMultiPhaseStructure', None))    # unknown version: assume present

    def test_powerchain_powador_rule(self):
        kaco = int(next(k for k, v in S.META['KacoModeCommPort']['values'].items() if v == 'RS485 Port 1 Powador'))
        ids = lambda cfg: {p['id'] for p in S.check(cfg, CELLS, 'SPMC482')}
        self.assertIn('powador_single_inverter_only', ids({'KacoModeCommPort': kaco, 'AdvancedMultiphaseNumberOfWorkers': 1}))  # 2/phase
        self.assertNotIn('powador_single_inverter_only', ids({'KacoModeCommPort': kaco, 'AdvancedMultiphaseNumberOfWorkers': 0}))  # 1/phase
        # Powerchain workers shows stored+1
        self.assertEqual(S.to_display('AdvancedMultiphaseNumberOfWorkers', 1, CELLS)[1], '2')

    def test_models(self):
        self.assertEqual(S.cells_for('SPMC482'), 24)
        self.assertEqual(S.cells_for('SPMC241'), 12)
        self.assertEqual(S.cells_for('SPLC1202'), 60)


if __name__ == '__main__':
    unittest.main()
