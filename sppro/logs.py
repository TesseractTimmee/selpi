"""
Data-log download by date range, decoded into rows matching SP LINK's CSV export.

Mechanism (docs/protocol.md, "Data logs"): set download mode (0xa251 = 1), per log read its descriptor and
segment list, write the range, poll the result until the search is done, read the records from first to last
following the segments, then clear download mode. The range registers and 0xa251 are the only writes.
"""
import json
import logging
import os
import struct
import time
from datetime import datetime, timedelta

from .reader import Reader

EPOCH = datetime(2001, 1, 1)          # timestamps are seconds since this, in the inverter's local time
DOWNLOAD_MODE = 0xa251
MAGIC = 32768.0


def to_datetime(seconds: int) -> datetime:
    return EPOCH + timedelta(seconds=seconds)


def from_datetime(when: datetime) -> int:
    return int((when - EPOCH).total_seconds())


class Log:
    def __init__(self, key, title, descriptor, range_reg, result_reg, record_words):
        self.key, self.title = key, title
        self.descriptor, self.range_reg, self.result_reg = descriptor, range_reg, result_reg
        self.record_words = record_words


LOGS = {
    'events':   Log('events', 'Operational events', 0xa2a7, 0xa2d4, 0xa2d8, 36),
    'alerts':   Log('alerts', 'Alerts', 0xa266, 0xa293, 0xa297, 36),
    'detailed': Log('detailed', 'Detailed data', 0xa335, 0xa374, 0xa378, 44),
    'daily':    Log('daily', 'Daily summary', 0xa2e8, 0xa321, 0xa325, 36),
}

STATUS_SEARCHING, STATUS_FOUND, STATUS_NONE = 1, 3, 4


# ---------------------------------------------------------------------------------------------- download

def find(reader: Reader, log: Log, since: datetime, until: datetime, timeout: float = 30.0):
    """Write the range and wait for the SP PRO's search; returns (first, last, count) or None.

    The range register is followed directly by the result block, so the 5th word written is the result's
    status word: writing STATUS_SEARCHING (1) starts the search."""
    data = struct.pack('<IIH', from_datetime(since), from_datetime(until), STATUS_SEARCHING)
    reader.write(log.range_reg, data)
    deadline = time.monotonic() + timeout
    while True:
        status, first, last, count = struct.unpack('<HIIH', reader.read(log.result_reg, 6))
        if status == STATUS_FOUND:
            return first, last, count
        if status == STATUS_NONE:
            return None
        if time.monotonic() > deadline:
            raise TimeoutError('%s: search still running after %ds (status %d)' % (log.title, timeout, status))
        time.sleep(0.5)


def segments(reader: Reader, log: Log):
    count, size, newest, capacity = struct.unpack('<HHIH', reader.read(log.descriptor, 5))
    if size != log.record_words:
        raise ValueError('%s: record size %d, expected %d' % (log.title, size, log.record_words))
    raw = reader.read(log.descriptor + 5, count * 4)
    return [struct.unpack('<II', raw[i * 8:i * 8 + 8]) for i in range(count)]


def record_addresses(segs, size, first, last, count):
    """Addresses from first to last, stepping to the next segment when a record would not fit."""
    def seg_index(a):
        for i, (s, e) in enumerate(segs):
            if s <= a <= e:
                return i
        raise ValueError('address 0x%x is in no segment' % a)
    i = seg_index(first)
    a = first
    for _ in range(count):
        yield a
        if a == last:
            return
        a += size
        if a + size - 1 > segs[i][1]:
            i = (i + 1) % len(segs)
            a = segs[i][0]


def read_records(reader: Reader, log: Log, since: datetime, until: datetime, progress=None):
    """Yield (address, raw bytes) for every record of `log` in [since, until].
    progress(phase, done, total), if given: ('search', 0, 0) while the SP PRO searches, then ('read', n, total)."""
    progress = progress or (lambda *a: None)
    progress('search', 0, 0)
    found = find(reader, log, since, until)
    if not found:
        progress('read', 0, 0)
        return
    first, last, count = found
    addresses = list(record_addresses(segments(reader, log), log.record_words, first, last, count))
    progress('read', 0, len(addresses))
    per_read = 256 // log.record_words
    i = 0
    while i < len(addresses):
        # batch consecutive records (same segment) into one read
        n = 1
        while n < per_read and i + n < len(addresses) and addresses[i + n] == addresses[i] + n * log.record_words:
            n += 1
        raw = reader.read(addresses[i], n * log.record_words)
        for k in range(n):
            yield addresses[i + k], raw[k * log.record_words * 2:(k + 1) * log.record_words * 2]
        i += n
        progress('read', i, len(addresses))


def download(reader: Reader, keys, since: datetime, until: datetime, progress=None) -> dict:
    """{key: [(address, raw)]} for the requested logs; download mode is always cleared afterwards.
    progress(key, index, phase, done, total), if given, reports each log's search and records read."""
    out = {}
    # Download mode (0xa251) is device state on the SP PRO, not Select.Live session state: a dropped-and-
    # reconnected tunnel is a fresh path to the same serial port and the register persists, so it is set once
    # here and cleared once at the end. (Re-asserting it on every reconnect only piles failed writes onto a
    # stalled link and makes a dead download far slower to give up.)
    reader.write(DOWNLOAD_MODE, struct.pack('<H', 1))
    try:
        for index, key in enumerate(keys):
            log = LOGS[key]
            report = (lambda ph, d, t, k=key, ix=index: progress(k, ix, ph, d, t)) if progress else None
            out[key] = list(read_records(reader, log, since, until, report))
            logging.info('%s: %d records', log.title, len(out[key]))
    finally:
        reader.write(DOWNLOAD_MODE, struct.pack('<H', 0))
    return out


# ---------------------------------------------------------------------------------------------- decoding

def _w(raw, off, fmt='H'):
    n = struct.calcsize(fmt)
    return struct.unpack('<' + fmt, raw[off * 2:off * 2 + n])[0]


class Scales:
    def __init__(self, ac_v, ac_i, dc_v, dc_i, temp, internal=180):
        self.ac_v, self.ac_i, self.dc_v, self.dc_i, self.temp, self.internal = ac_v, ac_i, dc_v, dc_i, temp, internal

    def dc_volts(self, r):  return r * self.dc_v / (MAGIC * 10)
    def dc_amps(self, r):   return r * self.dc_i / (MAGIC * 10)
    def ac_volts(self, r):  return r * self.ac_v / (MAGIC * 10)
    def ac_kw(self, r):     return r * self.ac_v * self.ac_i / (MAGIC * 800) / 1000
    def ac_kw_s(self, r):   return r * self.ac_v * self.ac_i / (MAGIC * 100) / 1000
    def dc_w(self, r):      return r * self.dc_v * self.dc_i / (MAGIC * 100)
    def dc_kwh(self, r):    return r * 24 * self.dc_v * self.dc_i / (MAGIC * 100) / 1000
    def ac_kwh(self, r):    return r * 24 * self.ac_v * self.ac_i / (MAGIC * 100) / 1000
    def degc(self, r):      return r * self.temp / MAGIC
    def int_v(self, r):     return r * self.internal / (MAGIC * 10)


def _tables():
    """Lookup tables extracted from SP LINK 15.40's own conversion functions (splink_tables.json)."""
    path = os.path.join(os.path.dirname(__file__), 'splink_tables.json')
    raw = json.load(open(path))
    return {k: {int(c): text for c, text in v.items()} for k, v in raw.items() if isinstance(v, dict)}


TABLES = _tables()
OPERATIONAL_EVENTS = TABLES['operational']
ALERT_EVENTS = TABLES['alert']
OPERATIONAL_MODE = TABLES['inverter_mode']
CHARGE_MODE = TABLES['charger_status']
CONTACTOR = TABLES['contactor_state']
GENERATOR_STATUS = TABLES['generator_status']
GENERATOR_REASON = TABLES['generator_reason']


def _enum(table, value):
    return table.get(value, 'Unknown (%d)' % value)


def f(value, decimals):
    text = '%.*f' % (decimals, value)
    return text[1:] if text.startswith('-') and float(text) == 0 else text   # no "-0.00"



DETAILED_HEADER = [
    'Date/Time Stamp [Seconds From The Year 2001]', 'Date/Time Stamp [dd/MM/yyyy - HH:mm:ss]',
    'Inverter AC Power (Average) [kW]', 'DC Input Accumulated (Sample) [kWh]', 'DC Output Accumulated (Sample) [kWh]',
    'Battery In Accumulated (Sample) [kWh]', 'Battery Out Accumulated (Sample) [kWh]', 'DC Voltage (Average) [V DC]',
    'DC Voltage (Min) [V DC]', 'DC Voltage (Max) [V DC]', 'DC Mid Voltage (Average) [V DC]',
    'DC Mid Voltage (Sync Sample at Min DC V) [V DC]', 'DC Mid Voltage (Sync Sample at Max DC V) [V DC]',
    'Inverter DC Current (Average) [A]', 'Shunt1 Current (Average) [A]', 'Shunt 2 Current (Average) [A]',
    'Load AC Power (Average) [kW]', 'Load AC Power (Max) [kW]', 'AC Input Power (Average) [kW]',
    'AC Load Voltage (Average) [V AC]', 'AC Load Frequency (Average) [Hz]', 'Transformer Temperature (Max) [Degrees C]',
    'Heatsink Temperature (Max) [Degrees C]', 'Battery Temperature (Max) [Degrees C]',
    'Internal Temperature (Max) [Degrees C]', 'Power Module Temperature (Max) [Degrees C]',
    'State of Charge (Sample) [%]', 'AC Input kWh Accumulated (Sample) [kWh]', 'AC Load kWh Accumulated (Sample) [kWh]',
    'Shunt 1 kWh Accumulated (Sample) [kWh]', 'Shunt 2 kWh Accumulated (Sample) [kWh]',
    'Analogue In 1 DC Voltage (Average) [V DC]', 'Analogue In 2 DC Voltage (Average) [V DC]',
    'AC Export kWh Accumulated (Sample) [kWh]', 'Total AC Coupled Power (Average) [kW]',
    'Total AC Coupled Energy (Sample) [kWh]']


def decode_detailed(address, raw):
    s = Scales(*(_w(raw, o) for o in range(39, 44)))
    ts = _w(raw, 0, 'I')
    return [
        '%d' % ts, to_datetime(ts).strftime('%d/%m/%Y - %H:%M:%S'),
        f(s.ac_kw(_w(raw, 2, 'i')), 2),
        f(s.dc_kwh(_w(raw, 4, 'I')), 2), f(s.dc_kwh(_w(raw, 6, 'I')), 2),
        f(s.dc_kwh(_w(raw, 8, 'I')), 2), f(s.dc_kwh(_w(raw, 10, 'I')), 2),
        *(f(s.dc_volts(_w(raw, o)), 2) for o in range(12, 18)),
        *(f(s.dc_amps(_w(raw, o, 'h')), 2) for o in (18, 19, 20)),
        *(f(s.ac_kw_s(_w(raw, o, 'h')), 2) for o in (21, 22, 23)),
        f(s.ac_volts(_w(raw, 24)), 1), f(_w(raw, 25) / 100, 2),
        *(f(s.degc(_w(raw, o, 'h')), 0) for o in (26, 27, 28)),
        '', '',
        f(_w(raw, 31) / 256, 1),
        f(s.ac_kwh(_w(raw, 32)), 2), f(s.ac_kwh(_w(raw, 33)), 2),
        f(s.dc_kwh(_w(raw, 34, 'h')), 2), f(s.dc_kwh(_w(raw, 35, 'h')), 2),
        f(s.int_v(_w(raw, 36)), 1), f(s.int_v(_w(raw, 37)), 1),
        f(s.ac_kwh(_w(raw, 38)), 2),
        f(s.ac_kw_s(_w(raw, 29, 'h')), 2), f(s.ac_kwh(_w(raw, 30)), 2),
    ]


EVENT_HEADER = [
    'Date/Time Stamp [Seconds From The Year 2001]', 'Date/Time Stamp [dd/MM/yyyy - HH:mm:ss]', 'Event Type',
    'Event Description', 'DC Voltage (Instantaneous) [V DC]', 'DC Mid Voltage (Instantaneous) [V DC]',
    'Inverter DC Current (Instantaneous) [A]', 'Shunt1 Current (Instantaneous) [A]',
    'Shunt 2 Current (Instantaneous) [A]', 'Load AC Power (Instantaneous) [kW]',
    'Inverter AC Power (Instantaneous) [kW]', 'AC Input Power (Instantaneous) [kW]',
    'AC Load Voltage (Instantaneous) [V AC]', 'State of Charge (Instantaneous) [%]',
    'AC Load Frequency (Instantaneous) [Hz]', 'Transformer Temperature (Instantaneous) [Degrees C]',
    'Heatsink 1 Temperature (Instantaneous) [Degrees C]', 'Heatsink 2 Temperature (Instantaneous) [Degrees C]',
    'Battery Temperature (Instantaneous) [Degrees C]', 'Internal Temperature (Instantaneous) [Degrees C]',
    'Power Module Temperature (Instantaneous) [Degrees C]', 'Operational Mode (Instantaneous) [State]',
    'Battery Charge Mode (Instantaneous) [Enum]', 'Contactor State (Instantaneous) [Enum]',
    'Generator Status (Instantaneous) [Enum]', 'Generator Start Reason (Instantaneous) [Enum]',
    'Generator Running Reason (Instantaneous) [Enum]', 'DC Battery Cable Loss (Instantaneous) [W]',
    'Fan Speed as Percent of Full Speed (Instantaneous) [%]', 'Battery Charge Efficiency Index (Sample) [Units]',
    'BMS State of Health (Instantaneous) [%]', 'Discharge Current Target (Instantaneous) [A]',
    'Charge Current Target (Instantaneous) [A]']


def decode_event(address, raw, default_type, live):
    # Event records carry only some scale factors; AC V and DC V come from the live registers (0xa028).
    s = Scales(live[0], _w(raw, 32), live[2], _w(raw, 34), _w(raw, 35))
    ts = _w(raw, 0, 'I')
    code = _w(raw, 2)
    kind = default_type
    table = ALERT_EVENTS if kind == 'Alert' else OPERATIONAL_EVENTS
    text = table.get(code, 'Unknown %s event %d' % (kind.lower(), code))
    return [
        '%d.%06d' % (ts, address - 0x80000), to_datetime(ts).strftime('%d/%m/%Y - %H:%M:%S'), kind, text,
        f(s.dc_volts(_w(raw, 3)), 1), f(s.dc_volts(_w(raw, 4)), 1),
        *(f(s.dc_amps(_w(raw, o, 'h')), 1) for o in (5, 6, 7)),
        f(s.ac_kw(_w(raw, 8, 'i')), 2), f(s.ac_kw(_w(raw, 10, 'i')), 2), f(s.ac_kw_s(_w(raw, 12, 'h')), 2),
        f(s.ac_volts(_w(raw, 13)), 1), f(_w(raw, 14) / 256, 1), f(_w(raw, 15) / 100, 2),
        *(f(s.degc(_w(raw, o, 'h')), 0) for o in range(16, 22)),
        _enum(OPERATIONAL_MODE, _w(raw, 22)), _enum(CHARGE_MODE, _w(raw, 23)), _enum(CONTACTOR, _w(raw, 24)),
        _enum(GENERATOR_STATUS, _w(raw, 25)), _enum(GENERATOR_REASON, _w(raw, 26)),
        _enum(GENERATOR_REASON, _w(raw, 27)),
        f(s.dc_w(_w(raw, 28)), 0), f(_w(raw, 29) / 256, 0), '', f(_w(raw, 30) / 256, 1),
        f(_w(raw, 31, 'h') * 100 / 256, 1), f(_w(raw, 33, 'h') * 100 / 256, 1),
    ]


DAILY_HEADER = [
    'Date/Time Stamp [Seconds From The Year 2001]', 'Date/Time Stamp [dd/MM/yyyy]',
    'DC Input Total Accumulated (Sample) [kWh]', 'DC Output Total Accumulated (Sample) [kWh]',
    'Battery In Total Accumulated (Sample) [kWh]', 'Battery Out Total Accumulated (Sample) [kWh]',
    'AC Input kWh Total Accumulated (Sample) [kWh]', 'AC Load kWh Total Accumulated (Sample) [kWh]',
    'Shunt 1 kWh Total Accumulated (Sample) [kWh]', 'Shunt 2 kWh Total Accumulated (Sample) [kWh]',
    'AC In Hours Total Accumulated (Sample) [Hours]', 'AC Export kWh Total Accumulated (Sample) [kWh]',
    'Inverter DC KWh Total Accumulated (Sample) [kWh]', 'Modulation Hours (Sample) [Hours]',
    'Float Hours Today (Sample) [Hours]', 'Shunt 1 kW Peak Today (Peak) [kW]', 'Shunt 2 kW Peak Today (Peak) [kW]',
    'Battery Charge Efficiency Index (Sample) [Units]', 'AC Coupled Solar kWh Total Accumulated (Sample) [kWh]']


def decode_daily(address, raw):
    s = Scales(*(_w(raw, o) for o in range(30, 35)))
    ts = _w(raw, 0, 'I')
    solar = (_w(raw, 27) << 16) | _w(raw, 28)       # high word first in this field
    return [
        '%d' % ts, to_datetime(ts).strftime('%d/%m/%Y'),
        f(s.dc_kwh(_w(raw, 2, 'I')), 1), f(s.dc_kwh(_w(raw, 4, 'I')), 1),
        f(s.dc_kwh(_w(raw, 6, 'I')), 1), f(s.dc_kwh(_w(raw, 8, 'I')), 1),
        f(s.ac_kwh(_w(raw, 10, 'I')), 1), f(s.ac_kwh(_w(raw, 12, 'I')), 1),
        f(s.dc_kwh(_w(raw, 14, 'i')), 1), f(s.dc_kwh(_w(raw, 16, 'i')), 1),
        f(_w(raw, 18, 'I') / 60, 2), f(s.ac_kwh(_w(raw, 20, 'I')), 1), f(s.dc_kwh(_w(raw, 22, 'I')), 1),
        f(_w(raw, 24, 'I') / 60, 2), f(_w(raw, 26) / 60, 2), '', '',
        f(_w(raw, 29) / 2048, 3), f(s.ac_kwh(solar), 1),
    ]


LIVE_SCALES = 0xa028


def live_scales(reader: Reader) -> list:
    return reader.words(LIVE_SCALES, 6)


def rows(key, records, live):
    """(header, rows) for a log key; events and alerts share SP LINK's EventData format.
    `live` is the list of live scale factors (live_scales())."""
    if key == 'detailed':
        return DETAILED_HEADER, [decode_detailed(a, r) for a, r in records]
    if key == 'daily':
        return DAILY_HEADER, [decode_daily(a, r) for a, r in records]
    default = 'Alert' if key == 'alerts' else 'Operational'
    return EVENT_HEADER, [decode_event(a, r, default, live) for a, r in records]
