"""settime: set the SP PRO clock to this machine's local time (or only when it is out by more than --max-drift)."""
from sppro import clock
from sppro.reader import Reader


def add_parser(subparsers):
    parser = subparsers.add_parser('settime', help='set time on SP PRO')
    parser.add_argument('--max-drift', type=int, default=0, help='only set it if more than this many seconds out')
    parser.set_defaults(func=run)


def run(args, reader=None):
    before, after = clock.sync(reader or Reader(), max_drift=args.max_drift, lead=2.0)
    print('drift %+d s' % before + ('' if after is None else ', set: now %+d s' % after))
