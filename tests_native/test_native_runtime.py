import json
import os
from pathlib import Path
import queue
import re
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import Overlay as overlay_module
import EDKeys as edkeys_module
from MacOSBridge import MacOSBridge
from EDAPGui import APGui
from EDKeys import EDKeys
from Screen import BRIDGE_HEADER, Screen
from Screen_Regions import Point, Quad
from StatusParser import StatusParser
from Voice import Voice


class NativeCaptureTests(unittest.TestCase):
    def test_bridge_dimensions_regions_and_stale_frame_guard(self):
        width, height = 8, 4
        pixels = np.arange(width * height * 4, dtype=np.uint8).tobytes()
        with tempfile.TemporaryDirectory() as directory:
            frame_path = Path(directory) / "frame.raw"
            frame_path.write_bytes(BRIDGE_HEADER.pack(2, width, height) + pixels)
            with mock.patch.dict(os.environ, {
                "EDAP_CAPTURE_FILE": str(frame_path),
                "EDAP_CAPTURE_STALE_SECONDS": "0.1",
            }, clear=False):
                screen = Screen(lambda *_: None)
                try:
                    self.assertEqual(screen.get_screen_size(), (width, height))
                    self.assertEqual(
                        screen._get_bridge_region(1, 1, 5, 3).shape, (2, 4, 4))
                    time.sleep(0.12)
                    self.assertIsNone(screen._get_bridge_region(0, 0, width, height))
                finally:
                    screen.close()


class NativeOverlayTests(unittest.TestCase):
    def tearDown(self):
        with overlay_module.overlay_state_lock:
            overlay_module.lines.clear()
            overlay_module.quadrilaterals.clear()
            overlay_module.text.clear()
            overlay_module.floating_text.clear()

    def test_capture_pixels_scale_to_retina_window_points(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "overlay.json"
            env = {
                "EDAP_NATIVE_OVERLAY_FILE": str(state_path),
                "EDAP_CAPTURE_WIDTH": "2560",
                "EDAP_CAPTURE_HEIGHT": "1440",
            }
            window = {"pid": 1, "x": 10, "y": 20, "width": 1280, "height": 720}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                    overlay_module.macos_bridge, "window_info", return_value=window):
                overlay = overlay_module.Overlay("", elite=1)
                overlay.overlay_rect("full", (0, 0), (2560, 1440), (0, 255, 0), 2)
                pixel_quad = Quad(
                    Point.from_xy((0, 0)), Point.from_xy((2560, 0)),
                    Point.from_xy((2560, 1440)), Point.from_xy((0, 1440)))
                overlay.overlay_quad_pix("pixels", pixel_quad, (0, 255, 0), 2)
                percent_quad = Quad(
                    Point.from_xy((0.1, 0.1)), Point.from_xy((0.2, 0.1)),
                    Point.from_xy((0.2, 0.2)), Point.from_xy((0.1, 0.2)))
                overlay.overlay_quad_pct("percent", percent_quad, (0, 255, 0), 2)
                overlay.overlay_floating_text("edge", "edge", 2560, 1440, (255, 255, 255))
                overlay.overlay_paint()
                state = json.loads(state_path.read_text())

                self.assertEqual(state["rectangles"][0][1], [1280.0, 720.0])
                self.assertEqual(state["quadrilaterals"][0][0][2], [1280.0, 720.0])
                self.assertEqual(state["quadrilaterals"][1][0][0], [128.0, 72.0])
                self.assertEqual(state["floating_text"][0][1:3], [1280.0, 720.0])
                overlay.overlay_quit()


class CooperativeRuntimeTests(unittest.TestCase):
    def test_coalesced_background_task_runs_latest_request(self):
        gui = APGui.__new__(APGui)
        gui._background_tasks = {}
        gui._pending_background_tasks = {}
        gui._ui_queue = queue.Queue()
        gui._closing = False
        gui.log_msg = lambda *_: None

        release_first = threading.Event()
        superseded_ran = threading.Event()
        latest_ran = threading.Event()
        self.assertTrue(gui._start_background_task(
            "set throttle", lambda: release_first.wait(1), announce=False, coalesce=True))
        self.assertTrue(gui._start_background_task(
            "set throttle", superseded_ran.set, announce=False, coalesce=True))
        self.assertTrue(gui._start_background_task(
            "set throttle", latest_ran.set, announce=False, coalesce=True))

        release_first.set()
        message, body = gui._ui_queue.get(timeout=1)
        gui._dispatch_callback(message, body)
        self.assertTrue(latest_ran.wait(1))
        self.assertFalse(superseded_ran.is_set())
        message, body = gui._ui_queue.get(timeout=1)
        gui._dispatch_callback(message, body)
        self.assertNotIn("set throttle", gui._background_tasks)

    def test_stop_cancels_queued_command_and_new_command_clears_stop(self):
        gui = APGui.__new__(APGui)
        gui._background_tasks = {}
        gui._pending_background_tasks = {"set throttle": (lambda: None, False, True, True)}
        gui._ui_queue = queue.Queue()
        gui._closing = False
        gui.log_msg = lambda *_: None
        gui.callback = lambda *_: None
        gui.ed_ap = mock.Mock()
        gui.ed_ap.stop_event = threading.Event()

        gui.stop_all_assists()
        self.assertEqual(gui._pending_background_tasks, {})
        gui.ed_ap.request_stop_all.assert_called_once_with()

        gui.ed_ap.stop_event.set()
        ran = threading.Event()
        gui._start_background_task(
            "set throttle", ran.set, announce=False, clear_stop=True)
        self.assertTrue(ran.wait(1))
        self.assertFalse(gui.ed_ap.stop_event.is_set())

    def test_new_command_supersedes_queue_behind_finished_worker(self):
        gui = APGui.__new__(APGui)
        gui._ui_queue = queue.Queue()
        gui._closing = False
        gui.log_msg = lambda *_: None
        gui._pending_background_tasks = {}

        old_worker = threading.Thread(target=lambda: None)
        old_worker.start()
        old_worker.join()
        stale_ran = threading.Event()
        latest_ran = threading.Event()
        gui._background_tasks = {"set throttle": old_worker}
        gui._pending_background_tasks = {
            "set throttle": (stale_ran.set, False, True, False)}

        gui._start_background_task(
            "set throttle", latest_ran.set, announce=False, coalesce=True)
        self.assertTrue(latest_ran.wait(1))
        self.assertNotIn("set throttle", gui._pending_background_tasks)

        message, body = gui._ui_queue.get(timeout=1)
        gui._dispatch_callback(message, body)
        self.assertFalse(stale_ran.is_set())

    def test_key_failure_attempts_to_release_entire_chord(self):
        keys = EDKeys.__new__(EDKeys)
        keys.ap_ckb = lambda *_: None
        keys.stop_event = threading.Event()
        keys.key_mod_delay = 0
        keys.key_def_hold_time = 0
        keys.key_repeat_delay = 0
        keys.activate_window = False
        keys.keys = {"SetSpeed50": {"key": 23, "mods": [29]}}
        keys.reversed_dict = {23: "Key_I"}
        released = []

        def press(scan_code):
            if scan_code == 23:
                raise RuntimeError("simulated window replacement")

        with mock.patch.object(edkeys_module, "PressKey", side_effect=press), \
                mock.patch.object(edkeys_module, "ReleaseKey", side_effect=released.append):
            with self.assertRaises(RuntimeError):
                keys.send("SetSpeed50")

        self.assertEqual(released, [23, 29])

    def test_native_release_all_does_not_start_an_idle_helper(self):
        bridge = MacOSBridge()
        with mock.patch.object(bridge, "_start") as start:
            self.assertIsNone(bridge.release_all())
        start.assert_not_called()

    def test_native_release_all_avoids_per_key_cleanup(self):
        keys = EDKeys.__new__(EDKeys)
        with mock.patch.object(edkeys_module, "ReleaseAllKeys", return_value=True), \
                mock.patch.object(edkeys_module, "ReleaseKey") as release:
            keys.release_all_keys()
        release.assert_not_called()

    def test_status_wait_stops_immediately(self):
        parser = StatusParser.__new__(StatusParser)
        parser.stop_event = threading.Event()
        parser.stop_event.set()
        with self.assertRaises(InterruptedError):
            parser._poll_wait(30)

    def test_status_read_retry_honors_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "Status.json"
            status_path.write_text("{")
            stop_event = threading.Event()
            stop_event.set()
            with self.assertRaises(InterruptedError):
                StatusParser(status_path, stop_event=stop_event)

    def test_voice_queue_never_blocks_producer(self):
        voice = Voice()
        voice.v_enabled = True
        start = time.monotonic()
        for index in range(20):
            voice.say(f"message {index}")
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(voice.q.qsize(), 5)

    def test_active_elite_bindings_are_supported_by_native_input(self):
        messages = []
        keys = EDKeys(lambda *args: messages.append(args))
        if not keys.keys:
            self.skipTest("No local Elite bindings file is available")

        swift = Path("platform/macos/macos_input_bridge.swift").read_text()
        mapping = re.search(
            r"private let macKeyCode: \[Int: CGKeyCode\] = \[(.*?)\n\]",
            swift, re.DOTALL)
        self.assertIsNotNone(mapping)
        supported = {int(value) for value in re.findall(r"(\d+)\s*:", mapping.group(1))}
        used = {binding["key"] for binding in keys.keys.values()}
        used.update(
            modifier for binding in keys.keys.values() for modifier in binding["mods"])
        self.assertEqual(used - supported, set())
        self.assertEqual(len(keys.keys), len(keys.keys_to_obtain))


if __name__ == "__main__":
    unittest.main()
