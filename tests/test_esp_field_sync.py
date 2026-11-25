"""app↔ESP field-table sync: the ESPHome component must carry byte-identical field tables to the app
(sync_fields.sh pulls them from the pinned app tag at release). Skips if the ESP component isn't a sibling
checkout (CI without the add-on repo)."""
import os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SPPRO = os.path.join(os.path.dirname(HERE), 'sppro')
# the add-on repo sits beside the app repo in the dev tree
ESP = os.path.normpath(os.path.join(os.path.dirname(HERE), '..', 'selpi-homeassistant', 'esphome', 'components', 'sppro'))
SYNCED = ['live_fields.json', 'default_fields.json', 'splink_tables.json', 'field_meta.py']


@unittest.skipUnless(os.path.isdir(ESP), 'ESP component not a sibling checkout')
class EspFieldSync(unittest.TestCase):
    def test_field_tables_identical(self):
        mismatched = []
        for f in SYNCED:
            a, b = os.path.join(APP_SPPRO, f), os.path.join(ESP, f)
            self.assertTrue(os.path.exists(b), '%s missing in ESP component' % f)
            if open(a, 'rb').read() != open(b, 'rb').read():
                mismatched.append(f)
        self.assertEqual(mismatched, [], 'ESP field tables out of sync with the app (run tools/sync_fields.sh): %s' % mismatched)
