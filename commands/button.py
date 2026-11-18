"""button: press an SP PRO front panel button (generator, on, silence) the way SP LINK's Quick View does.

This acts on the inverter: `generator` starts or stops the auto start generator, `on --long` switches the
SP PRO between ON and IDLE (no AC output). It refuses to run without --yes; `on` also needs --i-accept-an-outage.
`--show` only reads the port and the button registers.
"""
from sppro import buttons
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('button', help='press a front panel button (generator, on, silence)')
    parser.add_argument('button', choices=sorted(buttons.BUTTONS), nargs='?')
    parser.add_argument('--long', action='store_true', help='long press (only for "on")')
    parser.add_argument('--show', action='store_true', help='read the port and button registers, press nothing')
    parser.add_argument('--yes', action='store_true', help='really press it')
    parser.add_argument('--i-accept-an-outage', action='store_true', help='needed for "on" (IDLE = no AC output)')
    parser.set_defaults(func=run)


def run(args, reader=None):
    reader = reader or Reader()
    if args.show or not args.button:
        p = buttons.port(reader)
        print('SP PRO comms port: %d' % p)
        if p in buttons.BUTTON_BASE:
            base = buttons.BUTTON_BASE[p]
            for name, off in sorted(buttons.BUTTONS.items(), key=lambda kv: kv[1]):
                print('  %-9s 0x%x = %d' % (name, base + off, reader.words(base + off, 1)[0]))
        return
    if not args.yes:
        raise SystemExit('refusing to press %s without --yes' % args.button)
    if args.button == 'on' and not args.i_accept_an_outage:
        raise SystemExit('"on" switches the SP PRO between ON and IDLE (no AC output): add --i-accept-an-outage')
    r = buttons.press(reader, args.button, long=args.long)
    print('%s %s press: wrote %d to 0x%x (was %d), read back %s' % (
        r['press'], r['button'], r['value'], r['register'], r['before'], r['read_back']))
