"""
config: the SP PRO configuration.

  selpi config backup [--out DIR] [--json]   write <time>_Config.SPLC + <time>_ServiceSettings.CSV (or JSON)
  selpi config show NAME [NAME ...]          print named settings with address and raw value
  selpi config set NAME VALUE [--yes]        change ONE setting, VALUE as SP LINK shows it ("56.4", "Digital Control
                                             Input 2", "06:30", "1 Jan", "Disabled"; --raw for the stored number).
                                             Checks SP LINK's range and rules on the resulting configuration; dry run
                                             without --yes; with --yes it asks for the SP LINK passcode (never
                                             stored), backs up the whole configuration, relocks always, and reports
                                             every changed word
"""
import getpass
import json
import os
import sys
from datetime import datetime

from sppro import config, config_write, live, settings
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('config', help='SP PRO configuration: backup, show, set one setting')
    sub = parser.add_subparsers(metavar='{backup,show,set}')
    backup = sub.add_parser('backup', help='save the configuration in SP LINK format (or JSON)')
    backup.add_argument('--out', default='.', help='directory for the files (default: current)')
    backup.add_argument('--json', action='store_true', help='write one JSON file with names, addresses and values')
    backup.set_defaults(func=run_backup)
    show = sub.add_parser('show', help='print settings by name (case-insensitive substring) with their SP LINK values')
    show.add_argument('names', nargs='+')
    show.add_argument('--check', action='store_true', help="also run SP LINK's rules over the whole configuration")
    show.set_defaults(func=run_show)
    st = sub.add_parser('set', help='change one configuration setting (dry run without --yes)')
    st.add_argument('name', help='exact setting name, e.g. GeneratorRemoteStart (see `config show`)')
    st.add_argument('value', help='the value as SP LINK shows it (or the stored number with --raw)')
    st.add_argument('--raw', action='store_true', help='VALUE is the stored 16-bit number')
    st.add_argument('--allow-out-of-range', action='store_true', help="write a value outside SP LINK's range")
    st.add_argument('--ignore-rule', action='append', default=[], metavar='ID',
                    help="write although SP LINK's rule ID fails (repeatable; see the dry run)")
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


def _unit(reader):
    """(model, cells) from the unit's model word."""
    m = settings.model(reader)
    return m, settings.cells_for(m)


def _row(name, raw, cells):
    e = settings.entry(name)
    _, text = settings.to_display(name, raw, cells)
    rng = e.get('range_text') or ''
    return '%-44s %-24s raw %-6d %s' % (name, text, raw, rng)


def run_show(args, reader=None):
    wanted = [n.lower() for n in args.names]
    names = sorted(n for n in settings.META if any(w in n.lower() for w in wanted))
    if not names:
        raise SystemExit('no setting matches %s' % ', '.join(args.names))
    reader = reader or Reader()
    live.check_versions(reader)
    model, cells = _unit(reader)
    print('unit model %s (%s cells)' % (model or '?', cells or '?'))
    for n in names:
        a = settings.address(n)
        if a is None:
            continue
        print(_row(n, reader.words(a, 1)[0], cells))
        w = settings.where(n)
        if w:
            print('%44s   SP LINK: %s' % ('', w))
    if getattr(args, 'check', False):
        raw = settings.raw_config(config.read_memory(reader))
        _report(settings.check(raw, cells, model))


def _report(problems):
    real = [p for p in problems if p['severity'] != 'skipped']
    if not real:
        print("SP LINK's rules: no problems")
    for p in real:
        print("  %-7s %s  [%s]" % (p['severity'].upper(), p['message'], p['id']))
    skipped = [p['id'] for p in problems if p['severity'] == 'skipped']
    if skipped:
        print('  (not checked, missing information: %s)' % ', '.join(sorted(set(skipped))))
    return real


def _unit_state(reader, raw_now, cells):
    from sppro import buttons, controls
    v = live.versions(reader)
    region = settings.to_display('Region', raw_now['Region'], cells)[0] if 'Region' in raw_now else 'None'
    app = settings.to_display('ApplicationType', raw_now['ApplicationType'], cells)[0] if 'ApplicationType' in raw_now else None
    unit = {'output_mode': controls.output_state(reader)['mode'], 'hardware_revision': None,
            'comms_card_revision': v.get('comms_card_revision'), 'comms_card_attached': bool(v.get('comms_card_attached')),
            'region': region if app == 'Solar Hybrid (AS4777.2:2020)' else 'None', 'factory_export_limit': None}
    return unit, {'port': buttons.port(reader)}


def run_set(args, reader=None, passcode=None, fresh_reader=None):
    try:
        setting = config_write.find_setting(args.name)
        e = settings.entry(setting['name'])
    except (config_write.WriteError, settings.SettingError) as ex:
        raise SystemExit(str(ex))
    reader = reader or Reader()
    model, cells = _unit(reader)
    memory = config.read_memory(reader)
    raw_now = settings.raw_config(memory)
    current = memory.get(setting['address'], reader.words(setting['address'], 1)[0])
    try:
        if args.raw:
            new, warnings = int(args.value, 0), []
        else:
            new, warnings = settings.from_display(setting['name'], args.value, cells)
    except (ValueError, settings.SettingError) as ex:
        raise SystemExit(str(ex))
    if e.get('words') == 2:
        raise SystemExit('%s spans two words; not supported by config set' % setting['name'])
    old_text, new_text = settings.to_display(setting['name'], current, cells)[1], settings.to_display(setting['name'], new, cells)[1]
    print('%s (0x%x)%s: %s -> %s   (raw %d -> %d)' % (setting['name'], setting['address'],
          '  [%s]' % settings.where(setting['name']) if settings.where(setting['name']) else '', old_text, new_text, current, new))
    for w in warnings:
        print('  RANGE   %s' % w)
    raw_new = dict(raw_now, **{setting['name']: new})
    key = lambda p: (p['id'], p['message'])
    before = {key(p) for p in settings.check(raw_now, cells, model) if p['severity'] != 'skipped'}
    after = settings.check(raw_new, cells, model)
    new_problems = [p for p in after if p['severity'] != 'skipped' and key(p) not in before]
    if before:
        print("already in the unit's configuration (not caused by this change, not blocking):")
        for i, m in sorted(before):
            print('  %-7s %s  [%s]' % ('', m, i))
    print("SP LINK's checks on this change:")
    problems = _report(new_problems + [p for p in after if p['severity'] == 'skipped'])
    unit, link = _unit_state(reader, raw_now, cells)
    problems += _report(settings.send_check(raw_new, raw_now, unit, link, cells, model))
    blocking = [p for p in problems if p['severity'] == 'error' and p['id'] not in args.ignore_rule]
    stop = []
    if warnings and not args.allow_out_of_range:
        stop.append('outside SP LINK\'s range (--allow-out-of-range to write anyway)')
    if blocking:
        stop.append('SP LINK would refuse this: %s (--ignore-rule ID to write anyway)' % ', '.join(p['id'] for p in blocking))
    undo = 'selpi config set %s %s --yes' % (setting['name'], json.dumps(old_text) if ' ' in old_text else old_text)
    if not args.yes:
        for what, addr, val in config_write.plan(setting, new):
            print('  W 0x%04x = %s   %s' % (addr, '<passcode>' if val is None else val, what))
        print('dry run: nothing written%s. Undo afterwards: %s'
              % ('; with --yes this would stop: ' + '; '.join(stop) if stop else ' (add --yes)', undo))
        return
    if stop:
        raise SystemExit('not written: ' + '; '.join(stop))
    if passcode is None:
        text = sys.stdin.readline().strip() if args.passcode_stdin else getpass.getpass('SP LINK passcode: ')
        try:
            passcode = int(text)
        except ValueError:
            raise SystemExit('the passcode is a number')
    try:
        r = config_write.write_setting(reader, setting['name'], new, passcode, out_dir=args.out,
                                       force=args.force, fresh_reader=fresh_reader or (lambda: Reader(retries=3)))
    except config_write.WriteError as ex:
        raise SystemExit(str(ex))
    if not r['written']:
        print(r['note'])
        return
    print('%s: %s -> %s (%s)' % (r['setting'], old_text, settings.to_display(setting['name'], r['new'], cells)[1],
                                 'confirmed' if r['confirmed'] else 'NOT confirmed'))
    print('other words changed: %s' % (r['other_changes'] or 'none'))
    print('backups: %s / %s (.SPLC, .json)' % (r['backup'], r['after']))
    print('undo: %s' % undo)
