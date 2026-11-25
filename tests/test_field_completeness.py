"""Completeness: every SP LINK live-display field (the manifest) is either in the catalogue or on the
exclude-list with a reason. Guards against silently missing fields (as happened before: 98 were missing)."""
import json, os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SP = os.path.join(os.path.dirname(HERE), 'sppro')


class LiveFieldCompleteness(unittest.TestCase):
    def setUp(self):
        self.manifest = {f['field'] for f in json.load(open(os.path.join(SP, 'live_fields_manifest.json')))['fields']}
        self.exclude = {k: v for k, v in json.load(open(os.path.join(SP, 'live_fields_exclude.json'))).items()
                        if not k.startswith('_')}   # _-prefixed keys are policy notes, not fields
        self.have = {f['field'] for f in json.load(open(os.path.join(SP, 'live_fields.json')))['fields'] if f.get('field')}

    def test_every_manifest_field_accounted(self):
        unaccounted = self.manifest - self.have - set(self.exclude)
        self.assertEqual(unaccounted, set(), "SP LINK live fields missing from catalogue and exclude-list: %s" % sorted(unaccounted))

    def test_excludes_have_reasons_and_are_real(self):
        for fn, reason in self.exclude.items():
            self.assertTrue(reason.strip(), "exclude %s has no reason" % fn)
            self.assertIn(fn, self.manifest, "excluded field %s is not a real SP LINK field" % fn)

    def test_excludes_not_also_in_catalogue(self):
        both = set(self.exclude) & self.have
        self.assertEqual(both, set(), "fields both excluded and in catalogue: %s" % sorted(both))


class SettingsCompleteness(unittest.TestCase):
    """The second completeness check: every SP LINK config/service settings slot (layout.json) is in settings_meta."""
    def setUp(self):
        self.layout = json.load(open(os.path.join(SP, 'layout.json')))
        self.meta = {s['name'] for s in json.load(open(os.path.join(SP, 'settings_meta.json')))['settings']}

    def test_every_layout_slot_in_settings_meta(self):
        universe = [s for sec in self.layout['config']['sections'] for s in sec['slots'] if s]
        universe += [s for s in self.layout['service']['slots'] if s]
        missing = [n for n in universe if n not in self.meta]
        self.assertEqual(missing, [], "settings in layout.json but missing from settings_meta: %s" % missing)
