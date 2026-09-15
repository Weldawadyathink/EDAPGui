import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import EDKeys as edkeys_module
import EDShipControl as ship_control_module
from EDKeys import EDKeys
from EDShipControl import EDShipControl
from EDAP_data import FlagsInMainShip
from ED_AP import EDAutopilot
from ShipCalibration import (
    CalibrationError, CalibrationRequired, Conditions, ProfileStore, ShipIdentity, Trial,
    fit_response, loadout_fingerprint,
)
from ShipCalibrationRuntime import operating_conditions
from ShipCalibrationSession import Observation, angle_delta


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
    def test_angle_measurement_wraps_and_is_independent_of_target_range(self):
        self.assertEqual(angle_delta(179, -179), -2)
        # Observations are unit direction vectors: physical target range never
        # enters the angular measurement, while geometry/confidence still do.
        observation = Observation(1, 1, 0, .5, 3 ** .5 / 2, .9)
        self.assertAlmostEqual(observation.angle('pitch'), 30, places=5)
        observation.check('pitch')

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
        self.assertAlmostEqual(curve.predict(.025), .5, places=2)
        self.assertAlmostEqual(curve.duration_for(.5), .025, places=3)
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

    def test_deleting_active_profile_clears_hull_assignment(self):
        pid = self.store.create('Python', 'python')
        self.store.assign(pid, self.original)
        self.store.delete(pid)
        self.assertIsNone(self.store.selected(self.original))


class CalibrationRuntimeTests(unittest.TestCase):
    def test_assist_cannot_start_while_calibration_owns_resources(self):
        ap = EDAutopilot.__new__(EDAutopilot)
        ap._resource_lock = threading.Lock()
        ap.calibration_busy = threading.Event()
        ap.calibration_busy.set()
        ap.stop_event = threading.Event()
        ap.fsd_assist_enabled = False
        with self.assertRaises(CalibrationError):
            ap.set_fsd_assist(True)
        self.assertFalse(ap.fsd_assist_enabled)

    def test_rotation_can_use_validated_origin_for_small_correction(self):
        curve = mock.Mock()
        curve.knots = ((.05, 1.0), (.20, 8.0))
        curve.settle_seconds = 0
        curve.duration_for.return_value = .025
        store = mock.Mock()
        store.require_curve.return_value = curve
        store.warnings.return_value = []
        ap = mock.Mock()
        ap.calibration_busy = threading.Event()
        ap.calibration_store = store
        ap.raise_if_stop_requested = mock.Mock()
        ap._interruptible_sleep = mock.Mock()
        ap.get_compass_target_offset.return_value = {'pit': 0}
        keys = mock.Mock()
        control = EDShipControl.__new__(EDShipControl)
        control.ap, control.keys, control.ap_ckb = ap, keys, mock.Mock()
        control._last_profile_warning = None
        identity = ShipIdentity('python', loadout='loadout-a')
        conditions = Conditions('Speed50', 2, 450)
        with mock.patch.object(
                ship_control_module, 'operating_conditions',
                return_value=(identity, conditions, None)):
            control._rotate('pitch', .5)
        keys.pulse.assert_called_once_with('PitchUpButton', .025)

    def test_measured_pulse_releases_key_and_reports_timing(self):
        keys = EDKeys.__new__(EDKeys)
        keys.ap_ckb = lambda *_: None
        keys.stop_event = threading.Event()
        keys.key_mod_delay = 0
        keys.activate_window = False
        keys.keys = {'PitchUpButton': {'key': 17, 'mods': [42]}}
        events = []
        with mock.patch.object(
                edkeys_module, 'PressKey', side_effect=lambda key: events.append(('down', key)) or 1), \
                mock.patch.object(
                    edkeys_module, 'ReleaseKey', side_effect=lambda key: events.append(('up', key)) or 1):
            timing = keys.pulse('PitchUpButton', .001)
        self.assertEqual(events, [('down', 42), ('down', 17), ('up', 17), ('up', 42)])
        self.assertGreater(timing['duration'], 0)
        self.assertGreaterEqual(timing['uncertainty'], 0)

    def test_runtime_conditions_require_a_distant_system_reference_only_for_experiment(self):
        status = {
            'Flags': FlagsInMainShip, 'Flags2': 0, 'GuiFocus': 0,
            'pips': {'engine': 2}, 'FuelMain': 1, 'FuelReservoir': 0,
            'Cargo': 0, 'Destination_System': '', 'Destination_Body': -1,
            'Destination_Name': '',
        }
        ap = mock.Mock()
        ap.speed_demand = 'Speed50'
        for name in ('fsd_assist_enabled', 'sc_assist_enabled',
                     'waypoint_assist_enabled', 'robigo_assist_enabled',
                     'afk_combat_assist_enabled', 'dss_assist_enabled',
                     'single_waypoint_enabled'):
            setattr(ap, name, False)
        ap.jn.ship_state.return_value = {
            'type': 'python', 'ship_id': 1, 'loadout_fingerprint': 'loadout',
            'unladen_mass': 400, 'target': None,
        }
        ap.status.get_cleaned_data.return_value = status
        identity, conditions, _ = operating_conditions(ap)
        self.assertEqual(identity.hull, 'python')
        self.assertEqual(conditions.mass, 401)
        with self.assertRaisesRegex(Exception, 'distant system'):
            operating_conditions(ap, require_experiment=True)


if __name__ == '__main__':
    unittest.main()
