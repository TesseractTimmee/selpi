"""
logs: download data logs for a date range into CSV files in SP LINK's export format.

  selpi logs [--since YYYY-MM-DD[THH:MM]] [--until ...] [--logs events,alerts,detailed,daily] [--out DIR]

Defaults: since today 00:00, until now, all logs. Times are the inverter's local time.
Writes to the SP PRO: download mode (0xa251) and each log's range register, nothing else.
"""
import csv
import os
from datetime import datetime

from sppro import logs
from sppro.reader import Reader

FILES = {'events': 'EventData', 'detailed': 'DetailedData', 'daily': 'DailySummaryData'}


def parse_time(text: str, end: bool = False) -> datetime:
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            t = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end and fmt == '%Y-%m-%d':
            t = t.replace(hour=23, minute=59, second=59)
        return t
    raise ValueError('unrecognised time %r (use YYYY-MM-DD or YYYY-MM-DDTHH:MM)' % text)


def add_parser(subparsers):
    parser = subparsers.add_parser('logs', help='download data logs (CSV, SP LINK format)')
    parser.add_argument('--since', help='start (YYYY-MM-DD or YYYY-MM-DDTHH:MM); default today 00:00')
    parser.add_argument('--until', help='end (inclusive); default now')
    parser.add_argument('--logs', default='events,alerts,detailed,daily',
                        help='comma-separated: events, alerts, detailed, daily')
    parser.add_argument('--out', default='.', help='directory for the CSV files (default: current)')
    parser.set_defaults(func=run)


def run(args, reader=None):
    now = datetime.now().replace(microsecond=0)
    since = parse_time(args.since) if args.since else now.replace(hour=0, minute=0, second=0)
    until = parse_time(args.until, end=True) if args.until else now
    keys = [k.strip() for k in args.logs.split(',') if k.strip()]
    unknown = [k for k in keys if k not in logs.LOGS]
    if unknown:
        raise SystemExit('unknown log(s): %s' % ', '.join(unknown))

    reader = reader or Reader()
    live = logs.live_scales(reader)
    records = logs.download(reader, keys, since, until)

    # Events and alerts go into one EventData file, like SP LINK's export.
    merged = {}
    for key, recs in records.items():
        header, rows = logs.rows(key, recs, live)
        target = 'events' if key == 'alerts' else key
        merged.setdefault(target, (header, []))[1].extend(rows)

    stamp = now.strftime('%Y-%m-%d_%H-%M-%S')
    os.makedirs(args.out, exist_ok=True)
    for key, (header, rows) in merged.items():
        rows.sort(key=lambda r: float(r[0]))
        path = os.path.join(args.out, '%s_%s.CSV' % (FILES[key], stamp))
        with open(path, 'w', newline='') as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
            w.writerow(header)
            w.writerows(rows)
        print('%s (%d rows)' % (path, len(rows)))
