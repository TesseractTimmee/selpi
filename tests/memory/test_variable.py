from unittest import TestCase, skip
from memory import Range
from memory import Variable, variable
from unittest_data_provider import data_provider

scales = {
    'CommonScaleForDcVolts': 1050,
    'CommonScaleForDcCurrent': 12000,
    'CommonScaleForTemperature': 530,
    'CommonScaleForAcVolts': 5300,
    'CommonScaleForAcCurrent': 2200,
}

class VariableTest(TestCase):
    @data_provider(lambda: (
        # (arg, name, address)
        ('CommonScaleForDcVolts', 'CommonScaleForDcVolts', 0xa02a),
        ('Shunt1Power', 'Shunt1Power', 0xa088),
        ('BatteryVolts', 'BatteryVolts', 0xa05c),
        ('BattSocPercent', 'BattSocPercent', 0xa081),

        (0xa02a, 'CommonScaleForDcVolts', 0xa02a),
        (0xa088, 'Shunt1Power', 0xa088),
        (0xa05c, 'BatteryVolts', 0xa05c),
        (0xa081, 'BattSocPercent', 0xa081),

        (0xcafe, 'Unknown', 0xcafe),
        (0xfeed, 'Unknown', 0xfeed),
    ))
    def test_create(self, arg, name, address):
        var = variable.create(arg)
        self.assertIsInstance(var, Variable)
        self.assertEqual(address, var.range.address)
        self.assertEqual(name, var.get_name())

    @data_provider(lambda: (
        ('CommonScaleForDcVolts', Range(41002, 1)),
        ('BattOutkWhPreviousAcc', Range(41356, 2)),
        ('DCBatteryPower', Range(41007, 2)),
        (0xcafe, Range(0xcafe, 1)),
        (0xfeed, Range(0xfeed, 1)),
    ))
    def test_range(self, arg, range):
        self.assertEqual(range, variable.create(arg).range)

    @data_provider(lambda: (
        # (arg, type)
        ('CommonScaleForDcVolts', 'ushort'),
        ('BattOutkWhPreviousAcc', 'uint'),
        ('DCBatteryPower', 'int'),
        (0xcafe, 'ushort'),
        (0xfeed, 'ushort'),
    ))
    def test_get_type(self, arg, type):
        self.assertEqual(type, variable.create(arg).get_type())

    @data_provider(lambda: (
        ('CommonScaleForDcVolts', b'\x1a\x04', 1050),
        ('BatteryVolts', b'\x41\x44', 55.989532470703125),
    ))
    def test_bytes(self, arg, bytes, value):
        var = variable.create(arg)
        var.bytes = bytes
        self.assertEqual(value, var.get_value(scales))
        self.assertEqual(bytes, var.bytes)

    @data_provider(lambda: (
        ('CommonScaleForDcVolts', b'\x1a\x04', 1050),
        ('CommonScaleForTemperature', b'\x12\x02', 530),
        ('Shunt1Power', b'\x46\xfe', -1699.5849609375),
        ('Shunt1Power', b'\x3f\xfe', -1726.50146484375),
        ('Shunt1Power', b'\x7a\xfe', -1499.6337890625),
        ('Shunt1Power', b'\xfd\xff', -11.53564453125),
        ('BatteryVolts', b'\x41\x44', 55.989532470703125),
        ('BatteryVolts', b'\x3a\x44', 55.96710205078125),
        ('BatteryTemperature', b'\x44\x05', 21.802978515625),
        ('LoadAcPower', b'\xff\x02\x00\x00', 341.1567687988281),
        ('LoadAcPower', b'\x1d\x03\x00\x00', 354.5005798339844),
        ('DCBatteryPower', b'\xd4\xff\xff\xff', -169.189453125),
        ('DCBatteryPower', b'\xf4\xff\xff\xff', -46.142578125),
        ('ACLoadkWhTotalAcc', b'\x19\xeb\x00\x00', 5139822.509765625),
        ('BattOutkWhPreviousAcc', b'\x29\x00\x00\x00', 3783.69140625),
        ('BattSocPercent', b'\xdb\x63', 99.85546875),
        ('Shunt1Name', b'\x01\x00', 'Solar'),
        ('Shunt2Name', b'\x00\x00', 'None'),
        ('Shunt1Name', b'\x3a\x00', 'Error'),
    ))
    def test_get_value(self, name, bytes, value):
        address = 0xcafe # address doesn't matter for this test
        var = Variable(name, address, bytes)
        self.assertEqual(value, var.get_value(scales))

    def test_get_value_errors(self):
        var = variable.create(0xcafe)
        with self.assertRaises(Exception) as context:
            var.get_value([])
        self.assertEqual(
            'Can not convert value for unknown variable type',
            context.exception.args[0]
        )


class LiveVariableTest(TestCase):
    """Names not in MAP come from SP LINK's field table (sppro/live_fields.json)."""

    def test_create_by_splink_name_or_key(self):
        for arg in ('BattOutKWHTotalAcc', 'battoutkwhtotalacc', 'batt_out_kwh_total_acc'):
            v = variable.create(arg)
            self.assertIsInstance(v, variable.LiveVariable)
            self.assertEqual('BattOutKWHTotalAcc', v.get_name())
            self.assertEqual(Range(0xa1a5, 2), v.range)
            self.assertEqual('kWh', v.units)

    def test_map_names_unchanged(self):
        v = variable.create('BattOutkWhTotalAcc')
        self.assertNotIsInstance(v, variable.LiveVariable)
        self.assertEqual('uint', v.get_type())
        self.assertEqual('BattOutkWhTotalAcc', variable.create(0xa1a5).get_name())

    def test_unknown(self):
        with self.assertRaises(KeyError):
            variable.create('NoSuchVariable')
        self.assertEqual('Unknown', variable.create(0xcafe).get_name())

    def test_value_and_enum(self):
        import struct
        soc = variable.create('BattSocPercent')               # MAP: percent / 256
        live = variable.LiveVariable(variable.live_field('batt_soc_percent'))
        soc.bytes = live.bytes = struct.pack('<H', 64 * 256)
        self.assertEqual(64.0, soc.get_value(scales))
        self.assertEqual(64.0, live.get_value(scales))
        mode = variable.create('ac_out_status')                # enum: SP LINK label
        mode.bytes = b'\x00' * 2 * mode.range.words
        self.assertIsInstance(mode.get_value(scales), str)

    def test_needs_scales(self):
        v = variable.create('BattOutKWHTotalAcc')
        v.bytes = b'\x01\x00\x00\x00'
        with self.assertRaises(ValueError):
            v.get_value({})

    def test_many_fields_available(self):
        self.assertGreater(len(variable.live_names()), 250)


class AgreementTest(TestCase):
    """Where MAP and SP LINK's table cover the same words, both conversions give the same value
    (MAP in W / Wh, SP LINK in kW / kWh), on real memory captured from an SP PRO."""

    def test_map_agrees_with_splink(self):
        import os, struct, sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        capture = os.path.join(root, 'captures', 'sl-20261117-071605.jsonl')
        if not os.path.exists(capture):
            self.skipTest('capture not present (captures/ is git-ignored)')
        sys.path.insert(0, os.path.join(root, 'tools'))
        import sim_sppro
        mem = {a: b for a, b in sim_sppro.capture_memory(capture).items()}
        caps = {n: struct.unpack('<H', mem[0xa028 + i])[0] for i, n in enumerate(
            ['CommonScaleForAcVolts', 'CommonScaleForAcCurrent', 'CommonScaleForDcVolts', 'CommonScaleForDcCurrent',
             'CommonScaleForTemperature', 'CommonScaleForInternalVoltages'])}
        checked = 0
        for name, m in variable.MAP.items():
            if variable.CONVERSION not in m or m[variable.CONVERSION] == 'shunt_name':
                continue
            field = variable._live_by_address().get(m[variable.ADDRESS])
            if field is None:
                continue
            legacy = variable.create(name)
            live = variable.LiveVariable(field)
            if legacy.range.words != live.range.words:
                continue
            try:
                legacy.bytes = b''.join(mem[legacy.range.address + i] for i in range(legacy.range.words))
                live.bytes = legacy.bytes
            except KeyError:
                continue
            a, b = legacy.get_value(caps), live.get_value(caps)
            factor = 1000.0 if field['unit'] in ('kW', 'kWh') else 1.0   # MAP is in W / Wh
            self.assertAlmostEqual(a, b * factor, delta=abs(a) * 1e-6 + 1e-6, msg=name)
            checked += 1
        self.assertGreater(checked, 15)
