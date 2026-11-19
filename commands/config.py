"""
config: the SP PRO configuration.

  selpi config backup [--out DIR] [--json]   write <time>_Config.SPLC + <time>_ServiceSettings.CSV (or JSON)
  selpi config show NAME [NAME ...]          print named settings with address and raw value
  selpi config set NAME VALUE [--yes]        change ONE setting (raw value): dry run without --yes; with --yes it
                                             asks for the SP LINK passcode (never stored), backs up the whole
                                             configuration first, relocks always, and reports every changed word
"""
import getpass
import json
import os
import sys
from datetime import datetime

from sppro import config, config_write, live
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('config', help='SP PRO configuration: backup, show, set one setting')
    sub = parser.add_subparsers(metavar='{backup,show,set}')
    backup = sub.add_parser('backup', help='save the configuration in SP LINK format (or JSON)')
    backup.add_argument('--out', default='.', help='directory for the files (default: current)')
    backup.add_argument('--json', action='store_true', help='write one JSON file with names, addresses and values')
    backup.set_defaults(func=run_backup)
    show = sub.add_parser('show', help='print settings by name (case-insensitive substring)')
    show.add_argument('names', nargs='+')
    show.set_defaults(func=run_show)
    st = sub.add_parser('set', help='change one configuration setting (dry run without --yes)')
    st.add_argument('name', help='exact setting name, e.g. GeneratorRemoteStart (see `config show`)')
    st.add_argument('value', type=int, help='raw 16-bit value')
    st.add_argument('--yes', action='store_true', help='really write it (asks for the SP LINK passcode)')
    st.add_argument('--passcode-stdin', action='store_true', help='read the passcode from stdin instead of asking')
    st.add_argument('--out', default='config-writes', help='directory for the before/after backups')
    st.add_argument('--force', action='store_true', help="write even if the unit's versions differ from the verified map")
    st.set_defaults(func=run_set)
    parser.set_defaults(func=lambda args: parser.print_help())


def run_backup(args, reader=None):
    reader = reader or Reader()
    live.check_versions(reader)
    memory = config.read_memory(reader)
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
    live.check_versions(reader)
    for s in matches:
        value = reader.words(int(s['address'], 16), 1)[0]
        print('%-50s %-8s %6d   (%s)' % (s['name'], s['address'], value, s['section']))


def run_set(args, reader=None, passcode=None, fresh_reader=None):
    try:
        setting = config_write.find_setting(args.name)
    except config_write.WriteError as e:
        raise SystemExit(str(e))
    reader = reader or Reader()
    current = reader.words(setting['address'], 1)[0]
    print('%s (0x%x, %s): now %d -> %d' % (setting['name'], setting['address'], setting['section'], current, args.value))
    if not args.yes:
        for what, addr, val in config_write.plan(setting, args.value):
            print('  W 0x%04x = %s   %s' % (addr, '<passcode>' if val is None else val, what))
        print('dry run: nothing written (add --yes). Undo afterwards: selpi config set %s %d --yes'
              % (setting['name'], current))
        return
    if passcode is None:
        text = sys.stdin.readline().strip() if args.passcode_stdin else getpass.getpass('SP LINK passcode: ')
        try:
            passcode = int(text)
        except ValueError:
            raise SystemExit('the passcode is a number')
    try:
        r = config_write.write_setting(reader, setting['name'], args.value, passcode, out_dir=args.out,
                                       force=args.force, fresh_reader=fresh_reader or (lambda: Reader(retries=3)))
    except config_write.WriteError as e:
        raise SystemExit(str(e))
    if not r['written']:
        print(r['note'])
        return
    print('%s: %d -> %d (%s)' % (r['setting'], r['old'], r['new'], 'confirmed' if r['confirmed'] else 'NOT confirmed'))
    print('other words changed: %s' % (r['other_changes'] or 'none'))
    print('backups: %s / %s (.SPLC, .json)' % (r['backup'], r['after']))
    print('undo: %s' % r['undo'])
