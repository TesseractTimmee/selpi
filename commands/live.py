"""
live: everything SP LINK's data screens show, read from the SP PRO (read-only).

  selpi live                          Now + Today screens
  selpi live --screen now,tech,dc     chosen screens (now today tech dc ac ac2 service settings unit), or 'all'
  selpi live --json                   machine-readable (key, label, value, text, unit, words)
  selpi live --list                   list the fields without reading the SP PRO
"""
import json

from sppro import live
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('live', help='read live values as SP LINK shows them (read-only)')
    parser.add_argument('--screen', default='now,today',
                        help='comma list: %s, or all' % ' '.join(live.SCREENS))
    parser.add_argument('--json', action='store_true', help='print JSON')
    parser.add_argument('--list', action='store_true', help='list fields and addresses, no reading')
    parser.set_defaults(func=run)


def run(args, reader=None):
    screens = None if args.screen == 'all' else args.screen.split(',')
    if args.list:
        for f in live.fields_for(screens):
            print('%-24s %-34s %-28s %-5s %s' % (f['screen'][:24], f['key'][:34], f['name'][:28], f['unit'],
                                                ','.join(f['words'])))
        return
    reader = reader or Reader()
    problems = live.version_problems(live.versions(reader))
    if problems:
        print('WARNING: this unit differs from the verified map; values may be wrong: ' + ', '.join(problems))
    rows = live.snapshot(reader, screens)
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return
    screen = None
    for r in rows:
        if r['screen'] != screen:
            screen = r['screen']
            print('\n[%s]' % screen)
        print('  %-36s %14s %-4s %s%s' % (r['label'][:36], r['text'], r['unit'], ','.join(r['words']),
                                         '   (%s)' % r['note'] if r['note'] else ''))
