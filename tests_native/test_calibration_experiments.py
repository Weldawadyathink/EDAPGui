"""Offline experiments: simulated rays and input only, never an Elite adapter."""
import math
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from ShipCalibration import CalibrationError, Conditions, ProfileStore, ShipIdentity
from ShipCalibrationSession import CalibrationSession, Observation
import EDKeys as keys_module
from EDKeys import EDKeys
from EDAP_data import FlagsInMainShip
from ShipCalibrationRuntime import operating_conditions


class Plant:
    def __init__(self, axis):
        self.axis = axis
        self.time = 0.0
        self.frame = 0
        self.angle = 20.0
        self.stopped = False
        self.inputs = []
        self.identity = ShipIdentity('python', 1, 'test', 'Simulated', 'test-loadout')
        self.context = Conditions('Speed50', 2, 450)
        self.lost = False
        self.reverse = False

    def clock(self):
        return self.time

    def is_set(self):
        return self.stopped

    def wait(self, duration):
        self.time += duration
        return self.stopped

    def conditions(self):
        return self.identity, self.context, ('fixed-reference',)

    def observe(self):
        if self.lost:
            raise CalibrationError('Simulated reference loss')
        self.frame += 1
        a = math.radians(self.angle)
        if self.axis == 'roll':
            x, y, z = .5*math.sin(a), .5*math.cos(a), math.sqrt(.75)
        elif self.axis == 'pitch':
            x, y, z = 0, math.sin(a), math.cos(a)
        else:
            x, y, z = math.sin(a), 0, math.cos(a)
        return Observation(self.time, self.frame, x, y, z, .99)

    def pulse(self, axis, direction, duration):
        self.inputs.append((axis, direction, duration))
        self.time += duration
        # Deliberately nonlinear and asymmetric: neither a constant rate nor
        # equal/opposite responses can explain this simulated ship.
        movement = (18 if direction == 1 else 22)*duration + 30*duration**2
        self.angle -= direction*movement * (-1 if self.reverse else 1)
        return {'duration': duration, 'uncertainty': .001}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProfileStore(Path(self.temp.name)/'profiles.json')
        self.pid = self.store.create('Simulation', 'python')

    def session(self, plant):
        return CalibrationSession(self.store, self.pid, plant.observe, plant.pulse,
                                  plant.conditions, plant, clock=plant.clock)

    def test_complete_nonlinear_asymmetric_experiments_on_all_axes(self):
        for axis in ('roll', 'pitch', 'yaw'):
            with self.subTest(axis=axis):
                plant = Plant(axis)
                self.session(plant).run(axis)
                self.assertEqual(len(plant.inputs), 30)
                self.assertLessEqual(max(p[2] for p in plant.inputs), .4)
                positive = self.store.curve(self.pid, plant.context.key, axis, 1)
                negative = self.store.curve(self.pid, plant.context.key, axis, -1)
                self.assertTrue(positive.ready, positive.issues)
                self.assertTrue(negative.ready, negative.issues)
                self.assertLess(positive.knots[-1][1], negative.knots[-1][1])
                self.assertEqual(positive.validation_count, 3)
                self.assertEqual(positive.fit_count, 12)

    def test_stop_before_experiment_sends_no_input(self):
        plant = Plant('pitch')
        plant.stopped = True
        with self.assertRaises(InterruptedError):
            self.session(plant).run('pitch')
        self.assertEqual(plant.inputs, [])

    def test_lost_reference_after_pulse_stops_and_preserves_failed_trace(self):
        plant = Plant('pitch')
        original = plant.pulse
        def lose_reference(*args):
            timing = original(*args)
            plant.lost = True
            return timing
        plant.pulse = lose_reference
        with self.assertRaisesRegex(CalibrationError, 'reference loss'):
            self.session(plant).run('pitch')
        self.assertEqual(len(plant.inputs), 1)
        trial = self.store.profile(self.pid)['trials'][0]
        self.assertFalse(trial['accepted'])
        self.assertIn('reference loss', trial['reason'])
        self.assertGreater(len(trial['trace']), 0)

    def test_opposite_response_aborts_and_records_rejection(self):
        plant = Plant('yaw')
        plant.reverse = True
        with self.assertRaisesRegex(CalibrationError, 'opposite'):
            self.session(plant).run('yaw')
        self.assertEqual(len(plant.inputs), 1)
        self.assertFalse(self.store.profile(self.pid)['trials'][0]['accepted'])

    def test_stale_vision_never_sends_input(self):
        plant = Plant('roll')
        observation = plant.observe()
        plant.observe = lambda: observation
        with self.assertRaisesRegex(CalibrationError, 'delayed'):
            self.session(plant).run('roll')
        self.assertEqual(plant.inputs, [])

    def test_conditions_change_after_pulse_aborts(self):
        plant = Plant('pitch')
        original = plant.pulse
        def change_pips(*args):
            result = original(*args)
            plant.context = Conditions('Speed50', 4, 450)
            return result
        plant.pulse = change_pips
        with self.assertRaisesRegex(CalibrationError, 'changed'):
            self.session(plant).run('pitch')
        self.assertEqual(len(plant.inputs), 1)

    def test_old_route_target_cannot_disguise_current_local_destination(self):
        ap = SimpleNamespace(
            jn=SimpleNamespace(ship_state=lambda: {'type': 'python', 'target': 'Sol'}),
            status=SimpleNamespace(get_cleaned_data=lambda: {
                'Flags': FlagsInMainShip, 'GuiFocus': 0, 'pips': {'engine': 2},
                'Destination_Name': 'Nearby Station'}), speed_demand='Speed50')
        with self.assertRaisesRegex(CalibrationError, 'current destination'):
            operating_conditions(ap, require_experiment=True)

    def test_failed_atomic_write_preserves_memory_and_disk(self):
        before = self.store.path.read_bytes()
        with mock.patch('ShipCalibration.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.rename(self.pid, 'Lost rename')
        self.assertEqual(self.store.profile(self.pid)['name'], 'Simulation')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.store.path.parent.glob('.ship-calibration-*')), [])

    def test_malformed_profile_files_are_preserved_and_reported(self):
        for payload in (
                {'schema': 1, 'profiles': [], 'hulls': {}},
                {'schema': 1, 'profiles': {'p': None}, 'hulls': {}},
                {'schema': 2, 'profiles': {'p': []}, 'hulls': {}},
                {'schema': 2, 'profiles': {'p': {'id': 'p', 'name': 'p',
                    'hull': 'python', 'trials': []}}, 'hulls': {}}):
            with self.subTest(payload=payload):
                raw = json.dumps(payload)
                self.store.path.write_text(raw)
                damaged = ProfileStore(self.store.path)
                self.assertTrue(damaged.error)
                with self.assertRaises(CalibrationError):
                    damaged.create('New', 'python')
                self.assertEqual(self.store.path.read_text(), raw)


class PulseFailureTests(unittest.TestCase):
    def setUp(self):
        self.keys = EDKeys.__new__(EDKeys)
        self.keys.stop_event = threading.Event()
        self.keys.activate_window = False
        self.keys.key_mod_delay = 0
        self.keys.keys = {'PitchUpButton': {'key': 17, 'mods': [42, 29]}}

    def test_failed_modifier_never_presses_rotation_key(self):
        with mock.patch.object(keys_module, 'PressKey', return_value=0) as press, \
                mock.patch.object(keys_module, 'ReleaseKey', return_value=1) as release:
            with self.assertRaisesRegex(RuntimeError, 'Modifier'):
                self.keys.pulse('PitchUpButton', .1)
        press.assert_called_once_with(42)
        self.assertEqual(release.call_args_list, [mock.call(17), mock.call(29), mock.call(42)])

    def test_stop_during_hold_releases_entire_chord(self):
        def sleep(duration):
            if duration:
                self.keys.stop_event.set()
                raise InterruptedError('Stopped')
        with mock.patch.object(keys_module, 'PressKey', return_value=1), \
                mock.patch.object(keys_module, 'ReleaseKey', return_value=1) as release, \
                mock.patch.object(self.keys, '_interruptible_sleep', side_effect=sleep):
            with self.assertRaises(InterruptedError):
                self.keys.pulse('PitchUpButton', .1)
        self.assertEqual(release.call_args_list, [mock.call(17), mock.call(29), mock.call(42)])

    def test_one_failed_release_does_not_skip_remaining_modifiers(self):
        with mock.patch.object(keys_module, 'PressKey', return_value=1), \
                mock.patch.object(keys_module, 'ReleaseKey', side_effect=[1, OSError('lost helper'), 1]) as release, \
                mock.patch.object(self.keys, '_interruptible_sleep'):
            with self.assertRaisesRegex(RuntimeError, 'released'):
                self.keys.pulse('PitchUpButton', .1)
        self.assertEqual(release.call_args_list, [mock.call(17), mock.call(29), mock.call(42)])

    def test_experiment_ownership_rejects_input_from_another_thread(self):
        errors = []
        def other_command():
            try:
                self.keys.pulse('PitchUpButton', .1)
            except RuntimeError as exc:
                errors.append(str(exc))
        with mock.patch.object(keys_module, 'PressKey') as press:
            with self.keys.exclusive_input():
                other = threading.Thread(target=other_command)
                other.start()
                other.join(1)
                self.assertFalse(other.is_alive())
            press.assert_not_called()
        self.assertEqual(errors, ['Ship calibration currently owns input'])


if __name__ == '__main__':
    unittest.main()
