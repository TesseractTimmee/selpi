"""Front panel buttons (sppro/buttons.py) against the simulator's button model."""
import os
import struct
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import sim_sppro  # noqa: E402
from sppro import buttons  # noqa: E402
from sppro.reader import Reader  # noqa: E402


def sim(port=1):
    s = sim_sppro.SimProtocol({0xa000: struct.pack('<H', 0x0100 | port)})
    return s, Reader(lambda: s)


class TestButtons(unittest.TestCase):
    def test_port_picks_the_register_set(self):
        s, r = sim(1)
        self.assertEqual(buttons.port(r), 1)
        self.assertEqual(buttons.register(r, 'generator'), 0xa232)
        self.assertEqual(buttons.register(r, 'silence'), 0xa234)
        s, r = sim(2)
        self.assertEqual(buttons.register(r, 'generator'), 0xa235)
        self.assertEqual(buttons.register(r, 'on'), 0xa236)

    def test_unknown_port(self):
        s, r = sim(3)
        with self.assertRaises(buttons.ButtonError):
            buttons.register(r, 'generator')

    def test_short_generator_press(self):
        s, r = sim(1)
        out = buttons.press(r, 'generator', settle=2.0, poll=0.2)
        self.assertEqual(s.presses, [('generator', 1)])
        self.assertEqual(out['register'], 0xa232)
        self.assertEqual(out['read_back'][0], 1)       # held for a moment ...
        self.assertEqual(out['read_back'][-1], 0)      # ... then released

    def test_long_on_press_on_port_2(self):
        s, r = sim(2)
        buttons.press(r, 'on', long=True, settle=1.5, poll=0.2)
        self.assertEqual(s.presses, [('on', 2)])
        self.assertIn((0xa236, struct.pack('<H', 2)), [(a, bytes(d)) for a, d in s.writes])

    def test_unsupported_presses_are_refused_before_writing(self):
        s, r = sim(1)
        for b, long in (('generator', True), ('silence', True), ('on', False), ('nope', False)):
            with self.assertRaises(buttons.ButtonError):
                buttons.press(r, b, long=long, settle=0)
        self.assertEqual(s.writes, [])

    def test_already_pressed(self):
        s, r = sim(1)
        buttons.press(r, 'silence', settle=0)            # still held when the next press arrives
        with self.assertRaises(buttons.ButtonError):
            buttons.press(r, 'silence', settle=0.5, poll=0.1)
        self.assertEqual(s.presses, [('silence', 1)])
        time.sleep(1.2)
        buttons.press(r, 'silence', settle=0)            # released: accepted again
        self.assertEqual(len(s.presses), 2)


if __name__ == '__main__':
    unittest.main()
