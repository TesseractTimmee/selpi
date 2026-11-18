import struct
import unittest
from datetime import datetime

from sppro import config, logs


class FakeReader:
    """Word memory plus a scripted result block for log searches."""
    def __init__(self, mem=None, results=None):
        self.mem = dict(mem or {})
        self.results = list(results or [])
        self.writes = []

    def read(self, address, words):
        return b''.join(struct.pack('<H', self.mem.get(address + i, 0)) for i in range(words))

    def words(self, address, words, fmt='H'):
        return [self.mem.get(address + i, 0) for i in range(words)]

    def write(self, address, data):
        self.writes.append((address, bytes(data)))
        for i in range(len(data) // 2):
            self.mem[address + i] = struct.unpack('<H', data[2 * i:2 * i + 2])[0]
        if self.results and address in [log.range_reg for log in logs.LOGS.values()]:
            status, first, last, count = self.results.pop(0)
            block = struct.pack('<HIIH', status, first, last, count)
            for i in range(6):
                self.mem[address + 4 + i] = struct.unpack('<H', block[2 * i:2 * i + 2])[0]


def put_u32(mem, address, value):
    mem[address] = value & 0xffff
    mem[address + 1] = value >> 16


class TestTimestamps(unittest.TestCase):
    def test_epoch_is_2001_local(self):
        self.assertEqual(datetime(2026, 11, 17, 0, 0), logs.to_datetime(0x30abd080))
        self.assertEqual(0x30abd080, logs.from_datetime(datetime(2026, 11, 17)))


class TestFormatting(unittest.TestCase):
    def test_no_negative_zero(self):
        self.assertEqual('0.00', logs.f(-0.004, 2))
        self.assertEqual('-0.01', logs.f(-0.006, 2))
        self.assertEqual('27', logs.f(26.6, 0))


class TestRecordAddresses(unittest.TestCase):
    def test_wraps_to_next_segment(self):
        segs = [(0x1000, 0x1047), (0x2000, 0x2047)]   # 72 words each: 2 records of 36
        got = list(logs.record_addresses(segs, 36, 0x1024, 0x2024, 3))
        self.assertEqual([0x1024, 0x2000, 0x2024], got)

    def test_wraps_from_last_segment_to_first(self):
        segs = [(0x1000, 0x1047), (0x2000, 0x2047)]
        self.assertEqual([0x2024, 0x1000], list(logs.record_addresses(segs, 36, 0x2024, 0x1000, 2)))


class TestDownload(unittest.TestCase):
    def setUp(self):
        mem = {}
        log = logs.LOGS['daily']
        mem[log.descriptor] = 1           # one segment
        mem[log.descriptor + 1] = 36      # record size
        put_u32(mem, log.descriptor + 5, 0x3000)
        put_u32(mem, log.descriptor + 7, 0x30ff)
        for n in range(3):
            put_u32(mem, 0x3000 + 36 * n, 1000 + n)
        self.mem = mem

    def test_range_write_starts_search_and_mode_is_cleared(self):
        r = FakeReader(self.mem, results=[(3, 0x3000, 0x3048, 3)])
        out = logs.download(r, ['daily'], datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual([0x3000, 0x3024, 0x3048], [a for a, _ in out['daily']])
        self.assertEqual((logs.DOWNLOAD_MODE, b'\x01\x00'), r.writes[0])
        self.assertEqual((logs.DOWNLOAD_MODE, b'\x00\x00'), r.writes[-1])
        address, data = r.writes[1]
        self.assertEqual(logs.LOGS['daily'].range_reg, address)
        self.assertEqual(1, struct.unpack('<IIH', data)[2])   # status word = start search

    def test_no_records(self):
        r = FakeReader(self.mem, results=[(4, 0, 0, 0)])
        self.assertEqual({'daily': []}, logs.download(r, ['daily'], datetime(2026, 1, 1), datetime(2026, 1, 2)))


class TestDecode(unittest.TestCase):
    SCALES = [5300, 2934, 1050, 16000, 530]

    def test_detailed(self):
        w = [0] * 44
        w[0], w[1] = 0x3d7c, 0x30ac          # 2026-11-17 07:45:00
        w[2] = 488                          # 0.29 kW
        w[12] = 16356                       # 52.41 V
        w[25] = 5001                        # 50.01 Hz
        w[31] = 17 * 256                    # 17.0 %
        w[39:44] = self.SCALES
        row = logs.decode_detailed(0x8f404, struct.pack('<44H', *w))
        self.assertEqual('816594300', row[0])
        self.assertEqual('17/11/2026 - 07:45:00', row[1])
        self.assertEqual('0.29', row[2])
        self.assertEqual('52.41', row[7])
        self.assertEqual('50.01', row[20])
        self.assertEqual('17.0', row[26])

    def test_event_uses_address_as_fraction(self):
        w = [0] * 36
        w[0], w[1] = 0x3c33, 0x30ac
        w[2] = 0xffff                       # unknown code
        w[32], w[34], w[35] = 2934, 16000, 530
        row = logs.decode_event(0xdaac0, struct.pack('<36H', *w), 'Operational', [5300, 2934, 1050, 16000, 530, 180])
        self.assertEqual('816593971.371392', row[0])
        self.assertEqual('Unknown operational event 65535', row[3])

    def test_daily_solar_high_word_first(self):
        w = [0] * 36
        w[27], w[28] = 2, 61223
        w[30:35] = self.SCALES
        row = logs.decode_daily(0xcf7ac, struct.pack('<36H', *w))
        self.assertEqual('21901.1', row[18])


class TestTables(unittest.TestCase):
    def test_codes_from_splink(self):
        self.assertEqual('Solar Hybrid SoC Recovery Begin', logs.OPERATIONAL_EVENTS[332])
        self.assertEqual('Unit - Real Time Clock - Reading newer time', logs.ALERT_EVENTS[110])
        self.assertEqual('AC Source Present', logs.GENERATOR_STATUS[10])


class TestConfig(unittest.TestCase):
    def test_slot_addresses(self):
        addr = {name: a for _, _, name, a in config.slots() if name}
        self.assertEqual(0xc000, addr['ApplicationType'])
        self.assertEqual(0xc185, addr['InitialReturnVoltage'])
        self.assertEqual(0xc0e5, addr['PreSyncGeneratorWarmUp'])   # common slots >= 197 move to 0xc0e5

    def test_backup_is_read_only_and_splc_shaped(self):
        r = FakeReader({0xc185: 2183})
        mem = config.read_memory(r)
        self.assertEqual([], r.writes)
        text = config.to_splc(mem)
        self.assertIn('"InitialReturnVoltage"," 2183"," 0"," 0"," 0"', text)
        self.assertEqual(3 + 4 + sum(len(s['slots']) for s in config.LAYOUT['config']['sections']),
                         len(text.split('\r\n')))


if __name__ == '__main__':
    unittest.main()


class TestLive(unittest.TestCase):
    from sppro import live as _live
    SC = [5300, 2934, 1050, 16000, 530, 180]

    def mem(self, **words):
        m = {0xa028 + i: v for i, v in enumerate(self.SC)}
        m.update({int(k[1:], 16): v for k, v in words.items()})
        return m

    def field(self, key):
        return next(f for f in self._live.FIELDS if f['key'] == key)

    def test_load_power_32bit_signed_kw(self):
        raw = 26214400 * 5 // (5300 * 2934) * 1000     # about 8.4 kW
        v, _ = self._live.evaluate(self.field('load_ac_power'), self.mem(xa07a=raw & 0xffff, xa07b=raw >> 16))
        self.assertAlmostEqual(raw * 5300 * 2934 / 26214400 / 1000, v, 6)

    def test_battery_energy_today_uses_32bit_word_pair(self):
        f = self.field('batt_ink_wh_acc')
        self.assertEqual(['0xa0b5', '0xa0b6'], f['words'])
        v, _ = self._live.evaluate(f, self.mem(xa0b5=0x1234, xa0b6=0x0001))
        self.assertAlmostEqual(0x11234 * 24 * 1050 * 16000 / 3276800 / 1000, v, 6)

    def test_enum_and_missing(self):
        f = self.field('generator_status_from_led')
        self.assertEqual((10, 'AC Source Present'), self._live.evaluate(f, self.mem(xa096=10)))
        self.assertEqual((None, ''), self._live.evaluate(f, self.mem()))

    def test_filter_capacitor_life_uses_splink_scale(self):
        f = self.field('capacitor_life_consumed_filter')
        self.assertAlmostEqual(50.0, self._live.evaluate(f, self.mem(xa0ad=8192))[0])

    def test_version_gate(self):
        class R:
            def __init__(self, mm): self.mm = mm
            def words(self, a, n): return [48, 14, self.mm, 3, 3, 5] + [0] * (n - 6)
        self.assertEqual([], self._live.version_problems(self._live.versions(R(31))))
        self.assertEqual(['memory_map 30 (verified 31)'], self._live.version_problems(self._live.versions(R(30))))

    def test_every_formula_compiles_and_blocks_cover_words(self):
        m = {a: 1 for a in range(0xa000, 0xe000)}
        for f in self._live.FIELDS:
            self._live.evaluate(f, m)
            blocks = self._live.BLOCKS[f['screen']]
            for a in f['words']:
                self.assertTrue(any(b <= int(a, 16) < b + n for b, n in blocks), (f['key'], a))


class TestClock(unittest.TestCase):
    def test_round_trip_and_weekday(self):
        import struct
        from datetime import datetime
        from sppro import clock
        t = datetime(2026, 11, 17, 15, 43, 33)          # a Tuesday: SP PRO weekday 3 (Sun=1)
        words = list(struct.unpack('<7H', clock.encode(t)))
        self.assertEqual([0x33, 0x43, 0x15, 3, 0x17, 0x11, 0x26], words)
        self.assertEqual(t, clock.decode([0x44] + words))
        self.assertEqual(1, struct.unpack('<7H', clock.encode(datetime(2026, 11, 15, 6, 0, 0)))[3])  # Sunday
