"""Failure injection tests. All child executables are temporary fakes."""

import io
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from CooperativeStop import StopEvent
from EDAPGui import APGui
from ED_AP import EDAutopilot
from EDKeys import EDKeys
import EDKeys as key_module
import GlobalHotkeys as hotkeys
from MacOSBridge import MacOSBridge, MacOSBridgeError
import Overlay as overlay_module
from Screen import BRIDGE_HEADER, Screen
from StatusParser import StatusParser


def helper(path, code):
    """Build a fake executable without relying on space-free shebang paths."""
    script = path.with_suffix('.py')
    script.write_text(code)
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -u ' +
                    shlex.quote(str(script)) + ' "$@"\n')
    path.chmod(0o755)
    return path


class BridgeFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bridge = MacOSBridge()
        self.bridge.timeout = 0.15
        self.addCleanup(self.bridge.close)

    def use_helper(self, code):
        self.bridge.helper = helper(Path(self.directory.name) / 'input', code)

    def test_partial_response_times_out_and_reaps_unresponsive_helper(self):
        self.use_helper('import sys, time, signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                        'sys.stdin.readline()\nsys.stdout.write("{")\nsys.stdout.flush()\ntime.sleep(30)\n')
        start = time.monotonic()
        with self.assertRaises(MacOSBridgeError):
            self.bridge.request('window')
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNone(self.bridge._process)

    def test_blocked_write_has_deadline(self):
        self.use_helper('import time\ntime.sleep(30)\n')
        start = time.monotonic()
        with self.assertRaises(MacOSBridgeError):
            self.bridge.request('window', data='x' * 1_000_000)
        self.assertLess(time.monotonic() - start, 3)

    def test_malformed_response_shapes_fail_cleanly(self):
        for value in ['[]', 'null', '{"ok":"yes"}', '{', 'x' * 65537]:
            with self.subTest(value=value[:30]):
                self.use_helper('import sys\nsys.stdin.readline()\nprint(' + repr(value) + ')\n')
                with self.assertRaises(MacOSBridgeError):
                    self.bridge.request('window')
                self.assertIsNone(self.bridge._process)

    def test_multiple_requests_and_eof_cleanup(self):
        self.use_helper('import sys, json\nfor line in sys.stdin:\n'
                        ' print(json.dumps({"ok":True,"op":json.loads(line)["op"]}))\n')
        self.assertEqual(self.bridge.request('one')['op'], 'one')
        self.assertEqual(self.bridge.request('two')['op'], 'two')
        process = self.bridge._process
        self.bridge.close()
        self.assertEqual(process.returncode, 0)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_key_up_does_not_spawn_helper(self):
        with mock.patch.object(self.bridge, '_start') as start:
            self.assertIsNone(self.bridge.key(42, False))
        start.assert_not_called()


class CancellationTests(unittest.TestCase):
    def test_restart_cannot_revive_stopped_worker_or_nested_command(self):
        stop = StopEvent()
        resume = threading.Event()
        observed = []
        def old_command():
            resume.wait(1)
            with stop.command():
                observed.append(stop.is_set())
        worker = threading.Thread(target=stop.wrap(old_command))
        worker.start()
        stop.set()
        stop.clear()
        self.assertFalse(stop.is_set())
        resume.set()
        worker.join(1)
        self.assertEqual(observed, [True])

    def test_wrapped_work_captures_stop_before_thread_starts(self):
        stop = StopEvent()
        observations = []
        run = stop.wrap(lambda: observations.append(stop.wait(0)))
        stop.set()
        stop.clear()
        run()
        self.assertEqual(observations, [True])
        self.assertFalse(stop.is_set())

    def test_text_cleans_up_after_stop_during_shift_delay(self):
        keys = EDKeys.__new__(EDKeys)
        keys.stop_event = threading.Event()
        keys.key_mod_delay = 0.01
        def press(_):
            keys.stop_event.set()
        with mock.patch.object(key_module, 'PressKey', side_effect=press), \
                mock.patch.object(key_module, 'ReleaseKey') as release:
            with self.assertRaises(InterruptedError):
                keys.type_text('A')
        self.assertEqual(release.call_args_list, [mock.call(30), mock.call(42)])

    def test_text_cleans_up_after_key_failure(self):
        keys = EDKeys.__new__(EDKeys)
        keys.stop_event = threading.Event()
        keys.key_mod_delay = 0
        with mock.patch.object(key_module, 'PressKey', side_effect=[1, RuntimeError('failed')]), \
                mock.patch.object(key_module, 'ReleaseKey') as release:
            with self.assertRaises(RuntimeError):
                keys.type_text('A')
        self.assertEqual(release.call_args_list, [mock.call(30), mock.call(42)])

    def test_simultaneous_chords_do_not_inherit_each_others_modifiers(self):
        keys = EDKeys.__new__(EDKeys)
        keys.stop_event = threading.Event()
        keys.key_mod_delay = keys.key_def_hold_time = keys.key_repeat_delay = 0
        keys.activate_window = False
        keys.keys = {'first': {'key': 23, 'mods': [29]}, 'second': {'key': 24, 'mods': []}}
        keys.reversed_dict = {}
        modifier_down = threading.Event()
        allow_first = threading.Event()
        second_started = threading.Event()
        second_posted = threading.Event()
        posted = []
        def press(key):
            posted.append(('down', key))
            if key == 24:
                second_posted.set()
            if key == 29:
                modifier_down.set()
                allow_first.wait(1)
        def second():
            second_started.set()
            keys.send('second')
        with mock.patch.object(key_module, 'PressKey', side_effect=press), \
                mock.patch.object(key_module, 'ReleaseKey', side_effect=lambda key: posted.append(('up', key))):
            first = threading.Thread(target=lambda: keys.send('first'))
            other = threading.Thread(target=second)
            first.start()
            self.assertTrue(modifier_down.wait(1))
            other.start()
            self.assertTrue(second_started.wait(1))
            self.assertFalse(second_posted.wait(0.05))
            allow_first.set()
            first.join(1)
            other.join(1)
        self.assertEqual(posted, [('down', 29), ('down', 23), ('up', 23), ('up', 29),
                                  ('down', 24), ('up', 24)])

    def test_stop_discards_already_queued_start_callback(self):
        gui = APGui.__new__(APGui)
        gui._closing = False
        gui.gui_loaded = False
        gui._ui_queue = queue.Queue()
        gui._command_epoch = 0
        gui._pending_background_tasks = {}
        gui.ed_ap = mock.Mock()
        gui.log_msg = mock.Mock()
        gui.callback('fsd_start')
        gui.stop_all_assists()
        # No Tk attributes exist: dispatching this stale start would fail.
        gui._dispatch_callback(*gui._ui_queue.get_nowait())

    def test_engine_error_releases_held_keys_and_keeps_stop_latched(self):
        ap = EDAutopilot.__new__(EDAutopilot)
        ap.terminate = False
        ap.debug_show_compass_overlay = ap.debug_show_target_overlay = False
        ap.ship_tst_roll_enabled = ap.ship_tst_pitch_enabled = ap.ship_tst_yaw_enabled = False
        ap.fsd_assist_enabled = True
        ap.gui_loaded = ap.cv_view = False
        ap.scrReg = mock.Mock()
        ap.stop_event = StopEvent()
        ap.keys = mock.Mock()
        ap.ap_ckb = mock.Mock()
        ap.update_overlay = lambda: setattr(ap, 'terminate', True)
        def fail(*_):
            ap.stop_event.set()
            raise RuntimeError('vision failed after key-down')
        ap.fsd_assist = fail
        with mock.patch('ED_AP.set_focus_elite_window'), mock.patch('ED_AP.sleep'), \
                mock.patch('ED_AP.traceback.print_exc'), mock.patch('builtins.print'):
            ap.engine_loop()
        ap.keys.release_all_keys.assert_called_once_with()
        self.assertTrue(ap.stop_event.is_set())

    def test_throttle_repeat_is_not_interpreted_as_hold_seconds(self):
        ap = EDAutopilot.__new__(EDAutopilot)
        ap.status = mock.Mock()
        ap.keys = mock.Mock()
        ap.ap_ckb = mock.Mock()
        for method, binding in [(ap.set_throttle_0, 'SetSpeedZero'),
                                (ap.set_throttle_50, 'SetSpeed50'),
                                (ap.set_throttle_100, 'SetSpeed100')]:
            method(repeat=3)
            ap.keys.send.assert_called_with(binding, repeat=3)


class FileFailureTests(unittest.TestCase):
    def test_status_read_is_bounded_and_caches_pre_read_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Status.json'
            path.write_text('{"timestamp":"2026-09-15T00:00:00Z","Flags":0}')
            parser = StatusParser(path)
            with mock.patch.object(parser, 'get_file_modified_time', side_effect=[11, 12]):
                parser.get_cleaned_data()
                self.assertEqual(parser.last_mod_time, 11)
                parser.get_cleaned_data()
                self.assertEqual(parser.last_mod_time, 12)
            path.write_text('{')
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                parser.get_cleaned_data(timeout=0.05)
            self.assertLess(time.monotonic() - start, 0.3)
            path.unlink()
            with self.assertRaises(TimeoutError):
                parser.get_cleaned_data(timeout=0.01)

    def test_flags2_wait_supports_status_without_flags2(self):
        parser = StatusParser.__new__(StatusParser)
        parser.current_data = {'Flags2': None}
        parser.get_cleaned_data = lambda **_: parser.current_data
        self.assertTrue(parser.wait_for_flag2_off(1, timeout=0.01))

    def test_status_wait_deadline_includes_corrupt_file_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Status.json'
            path.write_text('{"timestamp":"2026-09-15T00:00:00Z","Flags":0}')
            parser = StatusParser(path)
            path.write_text('{')
            start = time.monotonic()
            self.assertFalse(parser.wait_for_flag_on(1, timeout=0.05))
            self.assertLess(time.monotonic() - start, 0.3)

    def test_capture_rejects_incomplete_file_and_does_not_reopen_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frame'
            path.write_bytes(BRIDGE_HEADER.pack(2, 8, 4))
            with mock.patch.dict(os.environ, {'EDAP_CAPTURE_FILE': str(path)}):
                with self.assertRaises(RuntimeError):
                    Screen(lambda *_: None)
                path.write_bytes(BRIDGE_HEADER.pack(2, 8, 4) + bytes(8 * 4 * 4))
                screen = Screen(lambda *_: None)
                self.assertIsNotNone(screen._get_bridge_region(0, 0, 8, 4))
                screen.close()
                self.assertIsNone(screen._get_bridge_region(0, 0, 8, 4))
                self.assertIsNone(screen._bridge_map)

    def test_overlay_quit_cannot_be_overwritten_by_late_paint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'overlay.json'
            with mock.patch.dict(os.environ, {'EDAP_NATIVE_OVERLAY_FILE': str(path)}), \
                    mock.patch.object(overlay_module.macos_bridge, 'window_info',
                                      return_value={'pid':1,'x':0,'y':0,'width':8,'height':4}):
                overlay = overlay_module.Overlay('', elite=1)
                overlay.overlay_quit()
                overlay._write_native_state()
                self.assertTrue(json.loads(path.read_text())['quit'])


@unittest.skipUnless(sys.platform == 'darwin', 'native hotkeys')
class HotkeyTests(unittest.TestCase):
    def test_old_reader_cannot_dispatch_new_callbacks(self):
        callback = mock.Mock()
        process = mock.Mock(stdout=io.StringIO('[]\n{"id":"0"}\n'))
        with mock.patch.object(hotkeys, '_process', object()):
            hotkeys._reader(process, threading.Event(), {}, {'0': callback})
        callback.assert_not_called()

    def test_removed_legacy_bindings_do_not_return(self):
        with mock.patch.object(hotkeys, '_process', None):
            hotkeys._legacy_bindings['home'] = (lambda: None, ())
            hotkeys.remove_all_hotkeys()
            self.assertEqual(hotkeys._legacy_bindings, {})


@unittest.skipUnless(shutil.which('zsh'), 'requires zsh')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='edap launcher test ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        platform = self.root / 'platform/macos'
        platform.mkdir(parents=True)
        self.launcher = platform / 'launch_native.command'
        shutil.copyfile('platform/macos/launch_native.command', self.launcher)
        self.launcher.chmod(0o755)
        self.platform = platform
        self.venv = self.root / 'venv/bin'
        self.venv.mkdir(parents=True)
        self.env = {**os.environ, 'EDAP_SUPPRESS_LAUNCH_ALERT':'1',
                    'EDAP_NATIVE_VENV':str(self.venv.parent)}
        for name in ['macos_input_bridge', 'macos_hotkey_bridge', 'macos_overlay_bridge']:
            helper(platform / name, 'import time\ntime.sleep(30)\n')
        helper(self.venv / 'python', 'import time\ntime.sleep(30)\n')

    def test_capture_exit_before_frame_is_failure_even_with_zero_status(self):
        helper(self.platform / 'macos_capture_bridge', 'pass\n')
        result = subprocess.run([str(self.launcher)], env=self.env, capture_output=True, timeout=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.native-runtime/launcher.lock').exists())

    def test_capture_exit_reaps_python_that_ignores_term(self):
        helper(self.venv / 'python', 'import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(30)\n')
        helper(self.platform / 'macos_capture_bridge',
               'import sys,struct,time\nfrom pathlib import Path\n'
               'p=Path(sys.argv[sys.argv.index("--output")+1])\n'
               'p.write_bytes(struct.pack("<QII",2,1,1)+bytes(4))\ntime.sleep(0.6)\nsys.exit(78)\n')
        result = subprocess.run([str(self.launcher)], env=self.env, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse((self.root / '.native-runtime/launcher.lock').exists())

    def test_concurrent_launcher_cannot_steal_unpublished_lock(self):
        # The directory intentionally has no PID yet, reproducing the old race.
        runtime = self.root / '.native-runtime'
        (runtime / 'launcher.lock').mkdir(parents=True)
        lockfile = runtime / 'launcher.flock'
        lockfile.touch()
        holder = subprocess.Popen(['zsh', '-c',
            'zmodload zsh/system; zsystem flock -t 0 -f fd "$1"; print ready; read line',
            'holder', str(lockfile)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'ready')
            result = subprocess.run([str(self.launcher)], env=self.env, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0)
            self.assertTrue((runtime / 'launcher.lock').is_dir())
        finally:
            holder.communicate('\n', timeout=3)


class InferenceTests(unittest.TestCase):
    def test_stop_after_coreml_does_not_fall_back_to_cpu(self):
        import numpy as np
        from MachineLearning import MachLearn, ModelType
        ml = MachLearn.__new__(MachLearn)
        ml.ap = mock.Mock()
        ml.ap.raise_if_stop_requested.side_effect = [None, InterruptedError('stopped')]
        ml.ap_ckb = mock.Mock()
        ml.coreml_models = {ModelType.Compass: object()}
        ml._coreml_predict = mock.Mock(return_value=[])
        ml.compass_ml_model = mock.Mock()
        with self.assertRaises(InterruptedError):
            ml._model_predict(ModelType.Compass, np.zeros((4, 4, 3)), '')
        ml.compass_ml_model.predict.assert_not_called()
        self.assertIn(ModelType.Compass, ml.coreml_models)

    def test_missing_capture_does_not_invoke_yolo_default_source(self):
        from MachineLearning import MachLearn, ModelType
        ml = MachLearn.__new__(MachLearn)
        ml.compass_ml_model = mock.Mock()
        self.assertIsNone(ml._model_predict(ModelType.Compass, None, ''))
        ml.compass_ml_model.predict.assert_not_called()


class SnapshotParserTests(unittest.TestCase):
    def test_all_snapshot_readers_bound_corrupt_file_retries_and_stop(self):
        from NavRouteParser import NavRouteParser
        from CargoParser import CargoParser
        from MarketParser import MarketParser
        from FleetCarrierMonitorDataParser import FleetCarrierMonitorDataParser
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            path.write_text('{')
            for cls, method in [(NavRouteParser, 'get_nav_route_data'),
                                (CargoParser, 'get_cargo_data'),
                                (MarketParser, 'get_market_data'),
                                (FleetCarrierMonitorDataParser, 'get_fleetcarrier_data')]:
                with self.subTest(parser=cls.__name__):
                    parser = cls.__new__(cls)
                    parser.file_path = path
                    parser.last_mod_time = None
                    parser.stop_event = threading.Event()
                    start = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        getattr(parser, method)(timeout=0.02)
                    self.assertLess(time.monotonic() - start, 0.3)
                    parser.stop_event.set()
                    with self.assertRaises(InterruptedError):
                        getattr(parser, method)(timeout=0.02)

    def test_empty_route_has_no_last_system(self):
        from NavRouteParser import NavRouteParser
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'route.json'
            path.write_text('{"event":"NavRoute","Route":[]}')
            self.assertEqual(NavRouteParser(path).get_last_system(), '')
