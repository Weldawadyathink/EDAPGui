import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import Overlay as overlay_module
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
                "EDAP_CAPTURE_STALE_SECONDS": "0.01",
            }, clear=False):
                screen = Screen(lambda *_: None)
                self.assertEqual(screen.get_screen_size(), (width, height))
                self.assertEqual(screen._get_bridge_region(1, 1, 5, 3).shape, (2, 4, 4))
                time.sleep(0.02)
                self.assertIsNone(screen._get_bridge_region(0, 0, width, height))
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
    def test_status_wait_stops_immediately(self):
        parser = StatusParser.__new__(StatusParser)
        parser.stop_event = threading.Event()
        parser.stop_event.set()
        with self.assertRaises(InterruptedError):
            parser._poll_wait(30)

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
