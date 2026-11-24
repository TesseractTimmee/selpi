"""sppro.multiphase: unit discovery, addressing and the 0xc0ee phase/role decode, against the offline simulator."""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from sppro import multiphase as mp          # noqa: E402
from sppro.reader import Reader             # noqa: E402
import sim_sppro                            # noqa: E402

CAP = os.path.join(ROOT, 'captures', 'sl-20261117-071605.jsonl')


def reader_for(sim):
    return Reader(lambda: sim, retries=1)


class DecodePhaseRole(unittest.TestCase):
    def test_managers_workers_unassigned(self):
        self.assertEqual(mp.decode_phaserole(1), (1, 'manager', 'L1 Manager'))
        self.assertEqual(mp.decode_phaserole(3), (3, 'manager', 'L3 Manager'))
        self.assertEqual(mp.decode_phaserole(4), (4, 'manager', 'Split Manager'))
        self.assertEqual(mp.decode_phaserole(5), (1, 'worker', 'L1 Worker'))
        self.assertEqual(mp.decode_phaserole(7), (3, 'worker', 'L3 Worker'))
        self.assertEqual(mp.decode_phaserole(0), (None, None, 'Unassigned'))
        self.assertEqual(mp.decode_phaserole(99), (None, None, 'Unassigned'))

    def test_disambiguate_duplicate_labels(self):
        # two workers on the same phase share a label; detect() must make them distinct (append serial)
        units = [{'index': 0, 'serial': 10000001, 'label': 'L1 Manager'},
                 {'index': 1, 'serial': 10000005, 'label': 'L1 Worker'},
                 {'index': 2, 'serial': 10000006, 'label': 'L1 Worker'}]
        labels = [u['label'] for u in mp.disambiguate_labels(units)]
        self.assertEqual(labels[0], 'L1 Manager')         # unique label untouched
        self.assertEqual(len(set(labels)), 3)             # all distinct now
        self.assertTrue(labels[1].endswith('(10000005)') and labels[2].endswith('(10000006)'))


@unittest.skipUnless(os.path.exists(CAP), 'capture not present (captures/ is git-ignored)')
class AgainstSim(unittest.TestCase):
    def mem(self):
        return sim_sppro.capture_memory(CAP)

    def test_serial_list_three_units(self):
        sim = sim_sppro.SimProtocol(self.mem(), multiphase=3)
        self.assertEqual(mp.serial_list(reader_for(sim)), {0: 10000001, 1: 10000002, 2: 10000003})

    def test_change_destination_routes_and_verifies(self):
        sim = sim_sppro.SimProtocol(self.mem(), multiphase=3)
        r = reader_for(sim)
        self.assertEqual(mp.change_destination(r, 2, 10000003), 10000003)
        with self.assertRaises(RuntimeError):                      # wrong expected serial -> refuse
            mp.change_destination(r, 1, 999999)

    def test_detect_lists_all_units(self):
        sim = sim_sppro.SimProtocol(self.mem(), multiphase=3)
        units = mp.detect(reader_for(sim))
        self.assertEqual([u['index'] for u in units], [0, 1, 2])
        self.assertEqual([u['serial'] for u in units], [10000001, 10000002, 10000003])

    def test_read_unit_decodes_phase_role(self):
        sim = sim_sppro.SimProtocol(self.mem(), multiphase=3)
        # stage a known powerchain: unit 1 = L2 manager (code 2), unit 2 = L2 worker (code 6)
        sim.units[1][mp.REG_PHASEROLE] = struct.pack('<H', 2)
        sim.units[2][mp.REG_PHASEROLE] = struct.pack('<H', 6)
        r = reader_for(sim)
        self.assertEqual(mp.read_unit(r, 1, 10000002)['label'], 'L2 Manager')
        self.assertEqual(mp.read_unit(r, 2, 10000003)['label'], 'L2 Worker')

    def test_single_inverter_is_one_unit(self):
        sim = sim_sppro.SimProtocol(self.mem())            # no multiphase: empty serial list
        units = mp.detect(reader_for(sim))
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]['index'], 0)
        self.assertFalse(mp.is_multi(reader_for(sim)))

    def test_for_unit_single_never_writes_gateway(self):
        sim = sim_sppro.SimProtocol(self.mem())            # single: multi=False
        r = reader_for(sim)
        fn = mp.for_unit({'index': 0, 'serial': 1}, multi=False, fn=lambda rd: rd.u32(mp.REG_SERIAL))
        fn(r)
        self.assertEqual([a for a, _ in sim.writes if a == mp.REG_GATEWAY], [])   # no re-address written

    def test_for_unit_multi_readdresses(self):
        sim = sim_sppro.SimProtocol(self.mem(), multiphase=3)
        r = reader_for(sim)
        fn = mp.for_unit({'index': 2, 'serial': 10000003}, multi=True, fn=lambda rd: rd.u32(mp.REG_SERIAL))
        self.assertEqual(fn(r), 10000003)                  # fn saw unit 2's memory

    def test_verified_write_targets_the_right_unit(self):
        sim = sim_sppro.SimProtocol(self.mem(), multiphase=3)
        r = reader_for(sim)
        mp.verified_write(r, {'index': 1, 'serial': 10000002}, True, 0xa081, struct.pack('<H', 0x1234))
        self.assertEqual(struct.unpack('<H', sim.units[1][0xa081])[0], 0x1234)   # landed in unit 1
        self.assertNotEqual(struct.unpack('<H', sim.units[2][0xa081])[0], 0x1234)
        with self.assertRaises(RuntimeError):              # serial mismatch -> no write
            mp.verified_write(r, {'index': 2, 'serial': 999999}, True, 0xa081, struct.pack('<H', 0x9999))


if __name__ == '__main__':
    unittest.main()
