import json
import os
import tempfile
import unittest

from web import server


class TestBackupDeletion(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._orig = server.BACKUPS
        server.BACKUPS = self._dir.name

    def tearDown(self):
        server.BACKUPS = self._orig
        self._dir.cleanup()

    def _make(self, name, reason):
        folder = os.path.join(server.BACKUPS, name)
        os.makedirs(folder)
        with open(os.path.join(folder, 'settings.json'), 'w') as fh:
            json.dump({'reason': reason, 'settings': []}, fh)
        with open(os.path.join(folder, 'config_backup.SPLC'), 'w') as fh:
            fh.write('x')

    def test_delete_removes_the_whole_folder(self):
        self._make('20261123_0330', 'manual')
        server.delete_backup('20261123_0330')
        self.assertFalse(os.path.exists(os.path.join(server.BACKUPS, '20261123_0330')))
        self.assertEqual(server.list_backups(), [])

    def test_delete_rejects_a_bad_name(self):
        with self.assertRaises(ValueError):
            server.delete_backup('../etc')
        with self.assertRaises(ValueError):
            server.delete_backup('')

    def test_delete_missing_backup_raises(self):
        with self.assertRaises(ValueError):
            server.delete_backup('20261123_9999')

    def test_prune_keeps_manual_backups_and_newest_scheduled(self):
        for d in ('20261101', '20261102', '20261103'):
            self._make(d + '_0330', 'scheduled')
        self._make('20261104_1200', 'manual')
        server.prune(1)                       # keep only the newest scheduled
        names = {b['name'] for b in server.list_backups()}
        self.assertEqual(names, {'20261103_0330', '20261104_1200'})


if __name__ == '__main__':
    unittest.main()
