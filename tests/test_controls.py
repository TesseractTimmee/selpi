"""State-aware controls (sppro/controls.py) against the simulator's generator / output model."""
import os
import struct
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import sim_sppro  # noqa: E402
from sppro import controls  # noqa: E402
from sppro.reader import Reader  # noqa: E402

FAST = dict(settle=1.5, poll=0.1)


def sim(gen=0, out=2, port=1):
    s = sim_sppro.SimProtocol({0xa000: struct.pack('<H', 0x0100 | port),
                               controls.GENERATOR_STATUS: struct.pack('<H', gen),
                               controls.OUTPUT_MODE: struct.pack('<H', out),
                               controls.DAYS_TO_EQUALISE: struct.pack('<H', 12)})
    s.TRANSITION = 0.5
    return s, Reader(lambda: s)


def wait(fn, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


class TestGenerator(unittest.TestCase):
    def test_start_then_tracks_running(self):
        s, r = sim(gen=0)
        out = controls.set_generator(r, True, **FAST)
        self.assertTrue(out['pressed'])
        self.assertEqual(s.presses, [('generator', 1)])
        self.assertTrue(controls.generator_state(r)['on'])            # Starting counts as on
        self.assertTrue(wait(lambda: controls.generator_state(r)['status'] == 'Running'))

    def test_no_press_when_already_in_that_state(self):
        for gen, on in ((1, True), (6, True), (0, False), (8, False)):
            s, r = sim(gen=gen)
            out = controls.set_generator(r, on, **FAST)
            self.assertFalse(out['pressed'], (gen, on))
            self.assertEqual(s.presses, [])

    def test_stop_while_running_or_starting(self):
        for gen in (1, 6):
            s, r = sim(gen=gen)
            self.assertTrue(controls.set_generator(r, False, **FAST)['pressed'])
            self.assertTrue(wait(lambda: controls.generator_state(r)['status'] == 'Not Running'))

    def test_refused_when_the_unit_cant(self):
        for gen in (2, 3, 4, 5, 7, 9, 10):
            s, r = sim(gen=gen)
            for on in (True, False):
                with self.assertRaises(controls.ControlError):
                    controls.set_generator(r, on, **FAST)
            self.assertEqual(s.presses, [], gen)

    def test_start_while_stopping_is_refused(self):
        s, r = sim(gen=8)
        with self.assertRaises(controls.ControlError):
            controls.set_generator(r, True, **FAST)
        self.assertEqual(s.presses, [])

    def test_second_request_doesnt_toggle_back(self):
        s, r = sim(gen=0)
        controls.set_generator(r, True, **FAST)
        time.sleep(1.2)                                            # button released, still Starting
        self.assertFalse(controls.set_generator(r, True, **FAST)['pressed'])
        self.assertEqual(len(s.presses), 1)


class TestOutput(unittest.TestCase):
    def test_idle_and_back(self):
        s, r = sim(out=2)
        self.assertTrue(controls.set_output(r, False, **FAST)['pressed'])
        self.assertEqual(controls.output_state(r)['mode'], 'Idle')
        time.sleep(1.2)
        self.assertTrue(controls.set_output(r, True, **FAST)['pressed'])
        self.assertEqual(controls.output_state(r)['mode'], 'On')
        self.assertEqual(s.presses, [('on', 2), ('on', 2)])

    def test_no_press_when_already_there(self):
        for out, on in ((1, True), (2, True), (3, True), (0, False)):
            s, r = sim(out=out)
            self.assertFalse(controls.set_output(r, on, **FAST)['pressed'])
            self.assertEqual(s.presses, [])

    def test_unknown_mode_refused(self):
        s, r = sim(out=7)
        with self.assertRaises(controls.ControlError):
            controls.set_output(r, False, **FAST)


class TestEqualise(unittest.TestCase):
    def test_two_long_generator_presses(self):
        s, r = sim(gen=0)
        out = controls.equalise(r, gap=0.2, timeout=3, poll=0.1)
        self.assertEqual(s.presses, [('generator', 2), ('generator', 2)])
        self.assertTrue(s.equalise_requested)
        self.assertEqual(controls.generator_state(r)['status'], 'Not Running')   # long presses don't start it
        self.assertEqual(out['days_to_equalise_before'], 12)


if __name__ == '__main__':
    unittest.main()
