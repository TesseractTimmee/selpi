"""One-setting configuration writes (sppro/config_write.py, `selpi config set`) against the simulator's lock model."""
import argparse
import io
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import sim_sppro  # noqa: E402
os.environ.setdefault('SELPI_ALLOW_UNVALIDATED_WRITES', 'true')  # these exercise the write engine
from commands import config as config_cmd  # noqa: E402
from sppro import config, config_write, live  # noqa: E402
from sppro.reader import Reader  # noqa: E402

SETTING = 'GeneratorRemoteStart'
ADDRESS = config_write.find_setting(SETTING)['address']


def sim(versions=None):
    mem = {}
    v = dict(live.VERIFIED, **(versions or {}))
    for k, i in live.VERSIONS.items():
        mem[live.VERSION_BASE + i] = struct.pack('<H', v.get(k, 0))
    mem[ADDRESS] = struct.pack('<H', 2)
    s = sim_sppro.SimProtocol(mem)
    return s, Reader(lambda: s)


def word(s, address):
    return struct.unpack('<H', s.mem.get(address, b'\0\0'))[0]


class TestConfigWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, s, r, value, passcode=1234, **kw):
        return config_write.write_setting(r, SETTING, value, passcode, out_dir=self.out, log=lambda *_: None, **kw)

    def test_writes_one_setting_and_relocks(self):
        s, r = sim()
        out = self.write(s, r, 0)
        self.assertEqual((out['old'], out['new'], out['written'], out['confirmed']), (2, 0, True, True))
        self.assertEqual(out['other_changes'], {})
        self.assertEqual(out['undo'], 'selpi config set GeneratorRemoteStart 2 --yes')
        self.assertEqual(word(s, ADDRESS), 0)
        # relocked and out of download mode
        self.assertEqual((word(s, 0xa002), word(s, 0xca14), word(s, 0xa251)), (0, 0xffff, 0))
        files = sorted(os.listdir(self.out))
        self.assertEqual(len(files), 4)          # before/after x SPLC/json
        self.assertTrue(any('before_' in f and f.endswith('.SPLC') for f in files))

    def test_wrong_passcode_writes_nothing_and_relocks(self):
        s, r = sim()
        with self.assertRaises(config_write.WriteError) as e:
            self.write(s, r, 0, passcode=1111)
        self.assertIn('not unlocked', str(e.exception))
        self.assertEqual(word(s, ADDRESS), 2)
        self.assertEqual((word(s, 0xa002), word(s, 0xca14), word(s, 0xa251)), (0, 0xffff, 0))
        self.assertNotIn(ADDRESS, [a for a, _ in s.writes])   # the setting word was never sent

    def test_refuses_when_busy_or_unverified(self):
        s, r = sim()
        s.mem[0xa251] = struct.pack('<H', 1)                   # a log download in progress
        with self.assertRaises(config_write.WriteError):
            self.write(s, r, 0)
        s, r = sim(versions={'config_settings': 49})
        with self.assertRaises(config_write.WriteError) as e:
            self.write(s, r, 0)
        self.assertIn('verified', str(e.exception))
        self.assertEqual(s.writes, [])

    def test_names_and_values_are_checked(self):
        s, r = sim()
        with self.assertRaises(config_write.WriteError):
            config_write.find_setting('GeneratorRemote')          # partial names are not enough
        with self.assertRaises(config_write.WriteError):
            self.write(s, r, 70000)
        self.assertEqual(s.writes, [])

    def test_same_value_writes_nothing(self):
        s, r = sim()
        out = self.write(s, r, 2)
        self.assertFalse(out['written'])
        self.assertEqual(s.writes, [])

    def test_locked_unit_ignores_setting_writes(self):
        s, r = sim()
        r.write(ADDRESS, struct.pack('<H', 0))
        self.assertEqual(word(s, ADDRESS), 2)
        self.assertEqual(len(s.blocked_config_writes), 1)

    def test_command_dry_run_and_write(self):
        s, r = sim()
        args = argparse.Namespace(name='generatorremotestart', value='None', yes=False, passcode_stdin=False,
                                  out=self.out, force=False, raw=False, allow_out_of_range=False, ignore_rule=[])
        buf = io.StringIO()
        with redirect_stdout(buf):
            config_cmd.run_set(args, reader=r)
        self.assertIn('dry run', buf.getvalue())
        self.assertIn('<passcode>', buf.getvalue())
        self.assertIn('Digital Control Input 2 -> None   (raw 2 -> 0)', buf.getvalue())
        self.assertEqual(s.writes, [])
        args.yes = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            config_cmd.run_set(args, reader=r, passcode=1234, fresh_reader=lambda: r)
        self.assertIn('Digital Control Input 2 -> None (confirmed)', buf.getvalue())
        self.assertIn('undo: selpi config set GeneratorRemoteStart "Digital Control Input 2" --yes', buf.getvalue())
        self.assertNotIn('1234', buf.getvalue())


class TestInstallerOnlyBlocked(unittest.TestCase):
    """Every installer-only setting is refused at the write engine itself, not just in the UI/CLI."""

    def test_all_installer_only_refused(self):
        from sppro import settings
        io = [n for n, e in settings.META.items() if e.get('installer_only')]
        self.assertGreaterEqual(len(io), 19)
        for n in io:
            s, r = sim()
            with self.assertRaises(config_write.WriteError) as e:
                config_write.write_setting(r, n, 1, 1234, out_dir=tempfile.mkdtemp(), log=lambda *a: None)
            self.assertIn('installer-only', str(e.exception))
            self.assertEqual(s.writes, [])


class TestFactoryReadOnly(unittest.TestCase):
    """Factory-mode settings are read-only at the write engine (SP LINK password-gates them)."""

    def test_factory_setting_refused(self):
        from sppro import settings
        fac = next(n for n in settings.META if settings.factory_only(n))
        s, r = sim()
        with self.assertRaises(config_write.WriteError) as e:
            config_write.write_setting(r, fac, 1, 1234, out_dir=tempfile.mkdtemp(), log=lambda *a: None)
        self.assertIn('factory-mode', str(e.exception))
        self.assertEqual(s.writes, [])


class TestSetChecks(unittest.TestCase):
    """config set refuses out-of-range values and failing rules unless overridden."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_set(self, name, value, **kw):
        s, r = sim()
        args = argparse.Namespace(name=name, value=value, yes=True, passcode_stdin=False, out=self.tmp.name,
                                  force=False, raw=False, allow_out_of_range=False, ignore_rule=[])
        for k, v in kw.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                config_cmd.run_set(args, reader=r, passcode=1234, fresh_reader=lambda: r)
                err = None
            except SystemExit as e:
                err = str(e)
        return s, buf.getvalue(), err

    def test_out_of_range_needs_override(self):
        s, out, err = self.run_set('EqualiseTime', '0.5')         # SP LINK: 1.0 - 10.0 hours
        self.assertIn('outside SP LINK', err)
        self.assertNotIn(config_write.find_setting('EqualiseTime')['address'], [a for a, _ in s.writes])
        s, out, err = self.run_set('EqualiseTime', '0.5', allow_out_of_range=True)
        self.assertIsNone(err)

    def test_unknown_label_refused(self):
        s, out, err = self.run_set('GeneratorRemoteStart', 'Digital Control Input 9')
        self.assertIn('is not one of', err)
        self.assertEqual(s.writes, [])


if __name__ == '__main__':
    unittest.main()
