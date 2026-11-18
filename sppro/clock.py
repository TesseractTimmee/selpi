"""
The SP PRO's real-time clock (local time, BCD).

Read  0x1d0000 x8: [?, sec, min, hour, weekday (Sun=1), day, month, year-2000]
Set   0x1d0011 x7: [sec, min, hour, weekday, day, month, year-2000]   (login only, no installer code)
Same registers SP LINK uses (btnSetInverterTime / mServiceTab.CheckTime).
"""
import logging
import struct
import time
from datetime import datetime, timedelta

CLOCK_READ, CLOCK_SET = 0x1d0000, 0x1d0011


def _bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def _unbcd(b: int) -> int:
    return ((b >> 4) & 0xf) * 10 + (b & 0xf)


def decode(words) -> datetime:
    sec, mins, hour, _wday, day, month, year = (_unbcd(w & 0xff) for w in words[1:8])
    return datetime(2000 + year, month, day, hour, mins, sec)


def encode(when: datetime) -> bytes:
    vals = [when.second, when.minute, when.hour, when.isoweekday() % 7 + 1, when.day, when.month, when.year % 100]
    return struct.pack('<7H', *(_bcd(v) for v in vals))


def read(reader):
    """(SP PRO time, local time at the middle of the read)."""
    t0 = time.time()
    words = reader.words(CLOCK_READ, 8)
    mid = datetime.fromtimestamp((t0 + time.time()) / 2).replace(microsecond=0)
    return decode(words), mid


def write(reader, when: datetime):
    reader.write(CLOCK_SET, encode(when))


def sync(reader, max_drift: int = 10, lead: float = 1.0):
    """Read the clock; set it to local time if it is more than max_drift seconds out.
    lead: seconds the write takes to land (more over Select.Live). Returns (drift before, drift after or None)."""
    unit, now = read(reader)
    drift = int((unit - now).total_seconds())
    if abs(drift) <= max_drift:
        return drift, None
    target = datetime.now() + timedelta(seconds=lead)
    logging.info('SP PRO clock %+d s out (%s vs %s): setting it', drift, unit, now)
    write(reader, target.replace(microsecond=0))
    unit, now = read(reader)
    after = int((unit - now).total_seconds())
    logging.info('SP PRO clock now %+d s', after)
    return drift, after
