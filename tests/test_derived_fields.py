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

    def test_variable_create_still_works(self):
        from memory import variable
        self.assertNotIn(None, variable.live_names())
        self.assertIsNotNone(variable.live_field('ac_source_power_in'))


if __name__ == '__main__':
    unittest.main()
