"""The web Configuration page's server side (web/settings_ui.py) against the simulator."""
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import sim_sppro  # noqa: E402
from sppro import live, settings  # noqa: E402
from sppro.reader import Reader  # noqa: E402
from web import settings_ui  # noqa: E402

GRS = settings.address('GeneratorRemoteStart')
EQV = settings.address('EqualiseV')


def sim():
    mem = {}
    for k, i in live.VERSIONS.items():
        mem[live.VERSION_BASE + i] = struct.pack('<H', live.VERIFIED.get(k, 0))
    mem[GRS] = struct.pack('<H', 2)                  # Digital Control Input 2
    mem[EQV] = struct.pack('<H', 2346)               # 56.3 V on 24 cells
    mem[0xa07d] = struct.pack('<H', 3)               # output mode Sync
    s = sim_sppro.SimProtocol(mem)
    return s, Reader(lambda: s)


def word(s, a):
    return struct.unpack('<H', s.mem.get(a, b'\0\0'))[0]


class TestConfigurationPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['SELPI_ALLOW_CONFIG_WRITES'] = 'true'
        settings_ui.READS.clear()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('SELPI_ALLOW_CONFIG_WRITES', None)

    def test_layout(self):
        lay = settings_ui.layout()
        titles = [t['title'] for t in lay['tabs']]
        self.assertIn('Charger', titles)
        self.assertEqual(lay['settings']['GeneratorRemoteStart']['kind'], 'enum')
        self.assertTrue(lay['writes_allowed'])

    def test_read_shows_splink_values(self):
        s, r = sim()
        d = settings_ui.read(r)
        self.assertEqual((d['model'], d['cells']), ('SPMC482', 24))
        self.assertEqual(d['values']['GeneratorRemoteStart']['text'], 'Digital Control Input 2')
        self.assertEqual(d['values']['EqualiseV']['text'], '56.3 V')

    def test_check_then_write_two_settings(self):
        s, r = sim()
        rid = settings_ui.read(r)['read_id']
        c = settings_ui.check(r, rid, {'GeneratorRemoteStart': 'None', 'EqualiseV': '56.4'})
        self.assertEqual(c['changes']['GeneratorRemoteStart']['raw'], 0)
        self.assertEqual(c['changes']['EqualiseV']['raw'], 2350)
        self.assertTrue(c['writable'])
        out = settings_ui.write(r, rid, {'GeneratorRemoteStart': 'None', 'EqualiseV': '56.4'}, '1234',
                                out_dir=self.tmp.name, ignore_rules=[p['id'] for p in c['problems']])
        self.assertTrue(out['written'])
        self.assertEqual((word(s, GRS), word(s, EQV)), (0, 2350))
        self.assertTrue(all(ch['confirmed'] for ch in out['changes']))
        self.assertEqual((word(s, 0xa002), word(s, 0xca14), word(s, 0xa251)), (0, 0xffff, 0))   # relocked
        with self.assertRaises(ValueError):      # the read is used up: the page must read again
            settings_ui.check(r, rid, {})

    def test_refuses_without_the_option(self):
        s, r = sim()
        rid = settings_ui.read(r)['read_id']
        os.environ['SELPI_ALLOW_CONFIG_WRITES'] = 'false'
        with self.assertRaises(PermissionError):
            settings_ui.write(r, rid, {'GeneratorRemoteStart': 'None'}, '1234', out_dir=self.tmp.name)
        self.assertEqual(word(s, GRS), 2)

    def test_refuses_if_the_unit_changed_since_the_read(self):
        from sppro import config_write
        s, r = sim()
        rid = settings_ui.read(r)['read_id']
        s.mem[EQV] = struct.pack('<H', 2340)       # someone changed it (SP LINK, the front panel)
        with self.assertRaises(config_write.WriteError) as e:
            settings_ui.write(r, rid, {'GeneratorRemoteStart': 'None'}, '1234', out_dir=self.tmp.name,
                              ignore_rules=['soc_normal_stop_above_start'])
        self.assertIn('changed on the unit', str(e.exception))
        self.assertEqual(word(s, GRS), 2)

    def test_wrong_passcode_and_out_of_range(self):
        from sppro import config_write
        s, r = sim()
        rid = settings_ui.read(r)['read_id']
        with self.assertRaises(ValueError):
            settings_ui.write(r, rid, {'EqualiseTime': '0.5'}, '1234', out_dir=self.tmp.name)   # 1 - 10 h
        with self.assertRaises(config_write.WriteError):
            settings_ui.write(r, rid, {'GeneratorRemoteStart': 'None'}, '1111', out_dir=self.tmp.name)
        self.assertEqual(word(s, GRS), 2)


if __name__ == '__main__':
    unittest.main()
