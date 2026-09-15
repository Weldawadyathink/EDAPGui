import json
from pathlib import Path
import tempfile
import unittest

from ShipCalibration import (
    CalibrationRequired, Conditions, ProfileStore, ShipIdentity, Trial,
    fit_response, loadout_fingerprint,
)


def measured_trials(context='Speed50/ENG2', axis='pitch', direction=1,
                    session='session', loadout='loadout-a'):
    trials = []
    for duration, degrees in ((.05, 1.0), (.10, 3.0), (.20, 8.0)):
        for adjustment in (-.05, 0, .05):
            trials.append(Trial(
                axis, direction, context, duration, duration,
                degrees + adjustment, .05, .4, session=session,
                loadout=loadout))
    revision = fit_response(trials).revision
    for duration, degrees in ((.075, 2.0), (.15, 5.5), (.075, 2.0)):
        trials.append(Trial(
            axis, direction, context, duration, duration, degrees, .05, .4,
            purpose='validation', fit_revision=revision, session=session,
            loadout=loadout))
    return trials


class ResponseCurveTests(unittest.TestCase):
    def test_unknown_profile_has_no_invented_curve(self):
        curve = fit_response([])
        self.assertEqual(curve.knots, ())
        self.assertFalse(curve.ready)
        with self.assertRaises(CalibrationRequired):
            curve.duration_for(1)

    def test_repeated_trials_fit_monotone_curve_and_validate(self):
        curve = fit_response(measured_trials())
        self.assertTrue(curve.ready, curve.issues)
        self.assertEqual(len(curve.knots), 3)
        self.assertGreater(curve.duration_for(5), .10)
        self.assertLess(curve.duration_for(5), .20)

    def test_loadout_fingerprint_ignores_transient_module_fields(self):
        event = {'Ship': 'Python', 'UnladenMass': 400, 'Modules': [
            {'Slot': 'MainEngines', 'Item': 'engine', 'Health': .5,
             'Engineering': {'BlueprintName': 'DirtyDrive', 'Level': 5}},
        ]}
        changed_health = json.loads(json.dumps(event))
        changed_health['Modules'][0]['Health'] = .9
        changed_engineering = json.loads(json.dumps(event))
        changed_engineering['Modules'][0]['Engineering']['Level'] = 4
        self.assertEqual(loadout_fingerprint(event), loadout_fingerprint(changed_health))
        self.assertNotEqual(loadout_fingerprint(event), loadout_fingerprint(changed_engineering))


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'profiles.json'
        self.store = ProfileStore(self.path)
        self.original = ShipIdentity('python', 11, 'cmdr', 'First', 'loadout-a')
        self.conditions = Conditions('Speed50', 2, 450)

    def tearDown(self):
        self.directory.cleanup()

    def _complete_axis(self, pid, axis='pitch'):
        session = f'{axis}-session'
        for direction in (-1, 1):
            trials = measured_trials(axis=axis, direction=direction, session=session)
            for trial in trials[:9]:
                self.store.add(pid, trial, self.original)
            revision = self.store.curve(
                pid, self.conditions.key, axis, direction,
                self.original.loadout).revision
            for trial in trials[9:]:
                trial.fit_revision = revision
                self.store.add(pid, trial, self.original)
        self.store.complete_session(pid, session, self.original)

    def test_profile_selection_is_hull_scoped_and_loadout_change_warns(self):
        first = self.store.create('General Python', 'python')
        second = self.store.create('Alternate Python', 'python')
        self.store.assign(first, self.original)
        self._complete_axis(first)

        changed = ShipIdentity('python', 99, 'cmdr', 'Second', 'loadout-b')
        self.assertEqual(self.store.selected(changed)['id'], first)
        self.assertIn('Loadout changed', ' '.join(self.store.warnings(changed)))
        self.assertTrue(
            self.store.require_curve(changed, self.conditions, 'pitch', 1).ready)

        self.store.assign(second, changed)
        self.assertEqual(self.store.selected(self.original)['id'], second)

    def test_new_loadout_trials_do_not_mix_with_last_completed_curve(self):
        pid = self.store.create('Python', 'python')
        self.store.assign(pid, self.original)
        self._complete_axis(pid)
        changed = ShipIdentity('python', 11, 'cmdr', 'First', 'loadout-b')
        self.store.add(pid, Trial(
            'pitch', 1, self.conditions.key, .05, .05, 20, .05, .4), changed)
        curve = self.store.require_curve(
            changed, self.conditions, 'pitch', 1)
        self.assertLess(curve.knots[0][1], 2)

    def test_prototype_schema_migrates_without_individual_ship_locks(self):
        pid = 'profile'
        self.path.write_text(json.dumps({
            'schema': 1,
            'profiles': {pid: {
                'id': pid, 'name': 'Python', 'hull': 'python',
                'loadout': '', 'trials': [], 'created': 'then'}},
            'hulls': {'python': pid},
            'ships': {'cmdr:11': pid},
        }))
        migrated = ProfileStore(self.path)
        self.assertFalse(migrated.error)
        self.assertNotIn('ships', migrated.data)
        self.assertEqual(migrated.selected(self.original)['id'], pid)


if __name__ == '__main__':
    unittest.main()
