"""control: state-aware SP PRO controls (generator on/off, inverter output on/idle, equalise request).

Unlike `selpi button`, these read the unit's state first and press a front panel button only when that
changes something (see sppro/controls.py). Without --yes they only show the current state.

  selpi control generator on|off --yes
  selpi control output on|idle --yes [--i-accept-an-outage for idle]
  selpi control equalise --yes
"""
from sppro import controls, live
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('control', help='generator on/off, inverter output on/idle, equalise request')
    parser.add_argument('what', choices=['generator', 'output', 'equalise', 'show'])
    parser.add_argument('state', nargs='?', choices=['on', 'off', 'idle'])
    parser.add_argument('--yes', action='store_true', help='really press the button if needed')
    parser.add_argument('--i-accept-an-outage', action='store_true', help='needed for "output idle" (no AC output)')
    parser.add_argument('--force', action='store_true', help='act even if the unit\'s versions differ from the verified map')
    parser.set_defaults(func=run)


def run(args, reader=None):
    reader = reader or Reader()
    g, o = controls.generator_state(reader), controls.output_state(reader)
    if args.what == 'show' or not args.yes:
        print('generator: %s (%s)' % ({True: 'on', False: 'off', None: 'n/a'}[g['on']], g['status']))
        print('output:    %s (%s)' % ({True: 'on', False: 'idle', None: 'n/a'}[o['on']], o['mode']))
        print('days to equalise: %d' % reader.words(controls.DAYS_TO_EQUALISE, 1)[0])
        if args.what != 'show':
            print('(nothing pressed: add --yes)')
        return
    if args.what == 'generator' and args.state not in ('on', 'off'):
        raise SystemExit('generator: on or off')
    if args.what == 'output' and args.state not in ('on', 'idle'):
        raise SystemExit('output: on or idle')
    if args.what == 'output' and args.state == 'idle' and not args.i_accept_an_outage:
        raise SystemExit('output idle cuts the AC output: add --i-accept-an-outage')
    try:
        live.check_versions(reader, writes=True, force=args.force)
    except live.VersionMismatch as e:
        raise SystemExit(str(e))
    try:
        if args.what == 'generator':
            r = controls.set_generator(reader, args.state == 'on')
        elif args.what == 'output':
            r = controls.set_output(reader, args.state == 'on')
        else:
            r = controls.equalise(reader)
    except controls.ControlError as e:
        raise SystemExit(str(e))
    if args.what == 'equalise':
        print('equalise requested (two long Generator presses); days to equalise %d -> %d'
              % (r['days_to_equalise_before'], r['days_to_equalise_after']))
    elif r['pressed']:
        print('%s: pressed (was %s)' % (args.what, r.get('status') or r.get('mode')))
    else:
        print('%s: %s, nothing pressed' % (args.what, r['note']))
