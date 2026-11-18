"""
config: read the SP PRO configuration (read-only).

  selpi config backup [--out DIR] [--json]   write <time>_Config.SPLC + <time>_ServiceSettings.CSV (or JSON)
  selpi config show NAME [NAME ...]          print named settings with address and raw value
"""
import json
import os
from datetime import datetime

from sppro import config
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('config', help='read the SP PRO configuration (read-only)')
    sub = parser.add_subparsers(metavar='{backup,show}')
    backup = sub.add_parser('backup', help='save the configuration in SP LINK format (or JSON)')
    backup.add_argument('--out', default='.', help='directory for the files (default: current)')
    backup.add_argument('--json', action='store_true', help='write one JSON file with names, addresses and values')
    backup.set_defaults(func=run_backup)
    show = sub.add_parser('show', help='print settings by name (case-insensitive substring)')
    show.add_argument('names', nargs='+')
    show.set_defaults(func=run_show)
    parser.set_defaults(func=lambda args: parser.print_help())


def run_backup(args, reader=None):
    memory = config.read_memory(reader or Reader())
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    os.makedirs(args.out, exist_ok=True)
    if args.json:
        path = os.path.join(args.out, 'Config_%s.json' % stamp)
        with open(path, 'w') as fh:
            json.dump({'created': stamp, 'settings': config.settings(memory)}, fh, indent=1)
        print(path)
        return
    for name, text in (('Config_%s.SPLC' % stamp, config.to_splc(memory)),
                       ('ServiceSettings_%s.CSV' % stamp, config.to_service_csv(memory))):
        path = os.path.join(args.out, name)
        with open(path, 'w', newline='') as fh:
            fh.write(text)
        print(path)


def run_show(args, reader=None):
    wanted = [n.lower() for n in args.names]
    matches = [s for s in config.settings({}) if any(w in s['name'].lower() for w in wanted)]
    if not matches:
        raise SystemExit('no setting matches %s' % ', '.join(args.names))
    reader = reader or Reader()
    for s in matches:
        value = reader.words(int(s['address'], 16), 1)[0]
        print('%-50s %-8s %6d   (%s)' % (s['name'], s['address'], value, s['section']))
