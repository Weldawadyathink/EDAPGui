from __future__ import annotations

import json
import threading
from functools import wraps
from contextlib import contextmanager
import time
from os import listdir
import os
from os.path import getmtime, isfile, join
from time import sleep
from typing import Any, final
from xml.etree.ElementTree import ParseError, parse

import xmltodict

from PlatformPaths import elite_options_dir
from Screen import set_focus_elite_window
from directinput import *
from EDlogger import logger

"""
Description:  Pulls the keybindings for specific controls from the ED Key Bindings file, this class also
  has method for sending a key to the display that has focus (so must have ED with focus)

Constraints:  This file will use the latest modified *.binds file
"""


# Multiple assists and GUI workers share one keyboard destination. Serialize
# whole chords/text so another command cannot inherit a half-pressed modifier.
# Emergency release deliberately does not acquire this lock.
_input_lock = threading.RLock()
_exclusive_owner = None


def serialized_input(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        if _exclusive_owner is not None and _exclusive_owner != threading.get_ident():
            raise RuntimeError("Ship calibration currently owns input")
        with _input_lock:
            if _exclusive_owner is not None and _exclusive_owner != threading.get_ident():
                raise RuntimeError("Ship calibration currently owns input")
            return method(self, *args, **kwargs)
    return run


@final
class EDKeys:

    def __init__(self, cb, stop_event=None):
        self.ap_ckb = cb
        self.stop_event = stop_event
        self.key_mod_delay = 0.01  # Delay for key modifiers to ensure modifier is detected before/after the key
        self.key_def_hold_time = 0.2  # Default hold time for a key press
        self.key_repeat_delay = 0.1  # Delay between key press repeats
        self.activate_window = False

        self.keys_to_obtain = [
            'YawLeftButton',
            'YawRightButton',
            'RollLeftButton',
            'RollRightButton',
            'PitchUpButton',
            'PitchDownButton',
            'SetSpeedZero',
            'SetSpeed50',
            'SetSpeed100',
            'HyperSuperCombination',
            'SelectTarget',
            'DeployHeatSink',
            'UIFocus',
            'UI_Up',
            'UI_Down',
            'UI_Left',
            'UI_Right',
            'UI_Select',
            'UI_Back',
            'CycleNextPanel',
            'HeadLookReset',
            'PrimaryFire',
            'SecondaryFire',
            'ExplorationFSSEnter',
            'ExplorationFSSQuit',
            'MouseReset',
            'DeployHardpointToggle',
            'IncreaseEnginesPower',
            'IncreaseWeaponsPower',
            'IncreaseSystemsPower',
            'ResetPowerDistribution',
            'GalaxyMapOpen',
            'CamZoomIn',  # Gal map zoom in
            'SystemMapOpen',
            'UseBoostJuice',
            'Supercruise',
            'UpThrustButton',
            'LandingGearToggle',
            'TargetNextRouteSystem',  # Target next system in route
            'CamTranslateForward',
            'CamTranslateRight',
            'OrderAggressiveBehaviour',
        ]
        self.keys = self.get_bindings()
        self.bindings = self.get_bindings_dict()

        self.missing_keys = []
        # We want to log the keyboard name instead of just the key number so we build a reverse dictionary
        # so we can look up the name also
        self.reversed_dict = {value: key for key, value in SCANCODE.items()}

        # dump config to log
        for key in self.keys_to_obtain:
            try:
                # lookup the keyname in the SCANCODE reverse dictionary and output that key name
                keyname = self.reversed_dict.get(self.keys[key]['key'], "Key not found")
                keymod = " "
                # if key modifier, then look up that modifier name also
                if len(self.keys[key]['mods']) != 0:
                    keymod = self.reversed_dict.get(self.keys[key]['mods'][0], " ")

                logger.info('\tget_bindings_<{}>={} Key: <{}> Mod: <{}>'.format(key, self.keys[key], keyname, keymod))
                if key not in self.keys:
                    self.ap_ckb('log',
                                f"WARNING: \tget_bindings_<{key}>= does not have a valid keyboard keybind {keyname}.")
                    logger.warning(
                        "\tget_bindings_<{}>= does not have a valid keyboard keybind {}".format(key, keyname).upper())
                    self.missing_keys.append(key)
            except Exception as e:
                self.ap_ckb('log', f"WARNING: \tget_bindings_<{key}>= does not have a valid keyboard keybind.")
                logger.warning("\tget_bindings_<{}>= does not have a valid keyboard keybind.".format(key).upper())
                self.missing_keys.append(key)

        # Check if the hotkeys are used in ED.
        for key_name in ('Key_End', 'Key_Insert', 'Key_PageUp', 'Key_Home'):
            binding_name = self.check_hotkey_in_bindings(key_name)
            if binding_name:
                warn_text = (
                    f"Hotkey '{key_name}' is used in the ED keybindings for "
                    f"'{binding_name}'. Recommend changing it in ED to avoid "
                    "EDAP accidentally being triggered.")
                self.ap_ckb('log', f"WARNING: {warn_text}")
                logger.warning(warn_text)

    def _raise_if_stop_requested(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("EDAP assist stop requested")

    def _interruptible_sleep(self, delay):
        if not delay or delay <= 0.0:
            return
        if self.stop_event is not None:
            if self.stop_event.wait(delay):
                raise InterruptedError("EDAP assist stop requested")
        else:
            sleep(delay)

    def get_bindings(self) -> dict[str, Any]:
        """Returns a dict struct with the direct input equivalent of the necessary elite keybindings"""
        direct_input_keys = {}
        latest_bindings = self.get_latest_keybinds()
        if not latest_bindings:
            return {}
        bindings_tree = parse(latest_bindings)
        bindings_root = bindings_tree.getroot()

        for item in bindings_root:
            if item.tag in self.keys_to_obtain:
                key = None
                mods = []
                hold = None
                # Check primary
                if item[0].attrib['Device'].strip() == "Keyboard":
                    key = item[0].attrib['Key']
                    for modifier in item[0]:
                        if modifier.tag == "Modifier":
                            mods.append(modifier.attrib['Key'])
                        elif modifier.tag == "Hold":
                            hold = True
                # Check secondary (and prefer secondary)
                if item[1].attrib['Device'].strip() == "Keyboard":
                    key = item[1].attrib['Key']
                    mods = []
                    hold = None
                    for modifier in item[1]:
                        if modifier.tag == "Modifier":
                            mods.append(modifier.attrib['Key'])
                        elif modifier.tag == "Hold":
                            hold = True
                # Prepare final binding
                binding: None | dict[str, Any] = None
                try:
                    if key is not None:
                        binding = {}
                        binding['key'] = SCANCODE[key]
                        binding['mods'] = []
                        for mod in mods:
                            binding['mods'].append(SCANCODE[mod])
                        if hold is not None:
                            binding['hold'] = True
                except KeyError:
                    print("Unrecognised key '" + (
                        json.dumps(binding) if binding else '?') + "' for bind '" + item.tag + "'")
                if binding is not None:
                    direct_input_keys[item.tag] = binding

        if len(list(direct_input_keys.keys())) < 1:
            return {}
        else:
            return direct_input_keys

    def get_bindings_dict(self) -> dict[str, Any]:
        """Returns a dict of all the elite keybindings.
        @return: A dictionary of the keybinds file.
        Example:
        {
        'Root': {
            'YawLeftButton': {
                'Primary': {
                    '@Device': 'Keyboard',
                    '@Key': 'Key_A'
                },
                'Secondary': {
                    '@Device': '{NoDevice}',
                    '@Key': ''
                }
            }
        }
        }
        """
        latest_bindings = self.get_latest_keybinds()
        if not latest_bindings:
            return {}

        try:
            with open(latest_bindings, 'r') as file:
                my_xml = file.read()
                my_dict = xmltodict.parse(my_xml)
                return my_dict

        except OSError as e:
            logger.error(f"OS Error reading Elite Dangerous bindings file: {latest_bindings}.")
            raise Exception(f"OS Error reading Elite Dangerous bindings file: {latest_bindings}.")

    def check_hotkey_in_bindings(self, key_name: str) -> str:
        """ Check for the action keys. """
        ret = []
        for key, value in self.bindings['Root'].items():
            if type(value) is dict:
                primary = value.get('Primary', None)
                if primary is not None:
                    if primary['@Key'] == key_name:
                        ret.append(f"{key} (Primary)")
                secondary = value.get('Secondary', None)
                if secondary is not None:
                    if secondary['@Key'] == key_name:
                        ret.append(f"{key} (Secondary)")
        return " and ".join(ret)

    def get_latest_keybinds(self):
        path_bindings = join(elite_options_dir(), "Bindings")
        try:
            list_of_bindings = [join(path_bindings, f) for f in listdir(path_bindings) if
                                isfile(join(path_bindings, f)) and f.endswith('.binds')]
        except FileNotFoundError as e:
            return None

        if not list_of_bindings:
            return None

        # StartPreset names the active preset but not its schema version.
        # Prefer the highest-version file for that preset; Elite can touch an
        # older 4.2 file after the active 4.4 file, making mtime unreliable.
        active_preset = None
        start_preset = join(path_bindings, 'StartPreset.4.start')
        try:
            with open(start_preset, 'r', encoding='utf-8-sig') as preset_file:
                active_preset = next(
                    (line.strip() for line in preset_file if line.strip()), None)
        except (OSError, UnicodeError):
            logger.warning(f'Unable to read active keybindings preset:{start_preset}')

        candidates = []
        if active_preset:
            for bindings_file in list_of_bindings:
                try:
                    root = parse(bindings_file).getroot()
                    if root.attrib.get('PresetName') != active_preset:
                        continue
                    version = (
                        int(root.attrib.get('MajorVersion', 0)),
                        int(root.attrib.get('MinorVersion', 0)),
                        getmtime(bindings_file),
                        bindings_file,
                    )
                    candidates.append(version)
                except (OSError, ParseError, ValueError):
                    logger.warning(f'Unable to inspect keybindings file:{bindings_file}')

        if candidates:
            latest_bindings = max(candidates)[3]
            logger.info(f'Active keybindings file:{latest_bindings}')
            return latest_bindings

        latest_bindings = max(list_of_bindings, key=getmtime)
        logger.info(f'Latest keybindings file (active preset unavailable):{latest_bindings}')
        return latest_bindings

    @serialized_input
    def send_key(self, type, key):
        self._raise_if_stop_requested()
        # Focus Elite window if configured
        if self.activate_window:
            set_focus_elite_window()
            self._interruptible_sleep(0.05)

        if type == 'Up':
            ReleaseKey(key)
        else:
            PressKey(key)

    @serialized_input
    def type_text(self, value, interval=0.05):
        """Type printable text through the same hardware-key path as controls."""
        char_keys = {
            **{chr(ord('a') + i): f"Key_{chr(ord('A') + i)}" for i in range(26)},
            **{str(i): f"Key_{i}" for i in range(10)},
            ' ': 'Key_Space', '-': 'Key_Minus', '=': 'Key_Equals',
            '.': 'Key_Period', ',': 'Key_Comma', '/': 'Key_Slash',
            "'": 'Key_Apostrophe', '[': 'Key_LeftBracket', ']': 'Key_RightBracket',
            ';': 'Key_SemiColon', '\\': 'Key_BackSlash', '`': 'Key_Grave',
        }
        shifted = {
            '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
            '*': '8', '(': '9', ')': '0', '_': '-', '+': '=', ':': ';', '?': '/',
            '"': "'", '<': ',', '>': '.', '{': '[', '}': ']', '|': '\\', '~': '`',
        }
        for char in str(value):
            self._raise_if_stop_requested()
            base = shifted.get(char, char.lower())
            key_name = char_keys.get(base)
            if key_name is None:
                logger.warning(f"Cannot type unsupported character {char!r}")
                continue
            needs_shift = char.isupper() or char in shifted
            key_released = False
            try:
                if needs_shift:
                    PressKey(SCANCODE['Key_LeftShift'])
                    self._interruptible_sleep(self.key_mod_delay)
                PressKey(SCANCODE[key_name])
                self._interruptible_sleep(0.02)
                ReleaseKey(SCANCODE[key_name])
                key_released = True
                if needs_shift:
                    self._interruptible_sleep(self.key_mod_delay)
            finally:
                # Preserve normal modifier timing, but release immediately on
                # interruption or failure anywhere in the text chord.
                if not key_released:
                    ReleaseKey(SCANCODE[key_name])
                if needs_shift:
                    ReleaseKey(SCANCODE['Key_LeftShift'])
            self._interruptible_sleep(interval)

    @serialized_input
    def send(self, key_binding, hold=None, repeat=1, repeat_delay=None, state=None):
        """ Send a key based on the defined keybind
        @param key_binding: The key bind name (i.e. UseBoostJuice).
        @param hold: The time to hold the key down in seconds.
        @param repeat: Number of times to repeat the key.
        @param repeat_delay: Time delay in seconds between repeats. If None, uses the default repeat delay.
        @param state: Key state:
            None - press and release (default).
            1 - press (but don't release).
            0 - release (a previous press state).
        """
        key = self.keys.get(key_binding)
        if key is None:
            logger.warning('SEND=NONE !!!!!!!!')
            self.ap_ckb('log', f"WARNING: Unable to retrieve keybinding for {key_binding}.")
            raise Exception(
                f"Unable to retrieve keybinding for {key_binding}. Advise user to check game settings for keyboard bindings.")

        key_name = self.reversed_dict.get(key['key'], "Key not found")
        logger.debug('\tsend=' + key_binding + ',key:' + str(key) + ',key_name:' + key_name + ',hold:' + str(
            hold) + ',repeat:' + str(
            repeat) + ',repeat_delay:' + str(repeat_delay) + ',state:' + str(state))

        for i in range(repeat):
            self._raise_if_stop_requested()
            # Focus Elite window if configured.
            if self.activate_window:
                if not set_focus_elite_window():
                    raise RuntimeError("Elite window is not currently available for input")
                self._interruptible_sleep(0.05)

            try:
                if state is None or state == 1:
                    for mod in key['mods']:
                        PressKey(mod)
                        self._interruptible_sleep(self.key_mod_delay)

                    PressKey(key['key'])

                if state is None:
                    if hold:
                        self._interruptible_sleep(hold)
                    else:
                        self._interruptible_sleep(self.key_def_hold_time)

                if 'hold' in key:
                    self._interruptible_sleep(0.1)

                if state is None or state == 0:
                    ReleaseKey(key['key'])

                    for mod in key['mods']:
                        self._interruptible_sleep(self.key_mod_delay)
                        ReleaseKey(mod)
            except InterruptedError:
                # A stop can arrive in the middle of a held chord.
                ReleaseKey(key['key'])
                for mod in key['mods']:
                    ReleaseKey(mod)
                raise
            except Exception:
                # A window handoff or helper failure can also happen midway
                # through a chord. Always attempt key-up before propagating it.
                ReleaseKey(key['key'])
                for mod in key['mods']:
                    ReleaseKey(mod)
                raise

            if repeat_delay:
                self._interruptible_sleep(repeat_delay)
            else:
                self._interruptible_sleep(self.key_repeat_delay)

    @contextmanager
    def exclusive_input(self):
        """Reserve flight input for a supervised experiment; End can still release."""
        global _exclusive_owner
        with _input_lock:
            previous = _exclusive_owner
            _exclusive_owner = threading.get_ident()
            try:
                yield
            finally:
                _exclusive_owner = previous

    @serialized_input
    def pulse(self, binding, seconds):
        """A measured key pulse, excluding modifier setup and repeat delays.

        Timestamps bracket the OS calls. Their midpoint estimates hold duration;
        half the combined call latency is retained as timing uncertainty.
        """
        if not 0 < seconds <= 2:
            raise ValueError('Pulse duration must be between zero and two seconds')
        self._raise_if_stop_requested()
        key = self.keys.get(binding)
        if key is None:
            raise RuntimeError(f'Missing Elite binding: {binding}')
        if self.activate_window and not set_focus_elite_window():
            raise RuntimeError('Elite window is unavailable')
        up_done = False
        try:
            for modifier in key['mods']:
                PressKey(modifier)
                self._interruptible_sleep(self.key_mod_delay)
            self._raise_if_stop_requested()
            down_start = time.monotonic()
            if PressKey(key['key']) == 0:
                raise RuntimeError('Key-down was not delivered')
            down_end = time.monotonic()
            self._interruptible_sleep(seconds)
            up_start = time.monotonic()
            if ReleaseKey(key['key']) == 0:
                raise RuntimeError('Key-up was not delivered')
            up_end = time.monotonic()
            up_done = True
            return {'duration': (up_start+up_end-down_start-down_end)/2,
                    'uncertainty': (down_end-down_start+up_end-up_start)/2}
        finally:
            if not up_done:
                ReleaseKey(key['key'])
            for modifier in reversed(key['mods']):
                ReleaseKey(modifier)

    def release_all_keys(self):
        """Release all modifier keys and any currently tracked key presses to prevent stuck keys.
        Called on stop/interrupt to ensure no keys remain held down."""
        # The native helper knows exactly which keys it posted. Releasing them
        # in one request avoids repeated window searches during stop/quit, and
        # deliberately does not start a new helper merely to shut it down.
        if ReleaseAllKeys():
            return

        modifier_scancodes = [
            SCANCODE.get("Key_LeftShift"),
            SCANCODE.get("Key_RightShift"),
            SCANCODE.get("Key_LeftControl"),
            SCANCODE.get("Key_RightControl"),
            SCANCODE.get("Key_LeftAlt"),
            SCANCODE.get("Key_RightAlt"),
        ]
        for sc in modifier_scancodes:
            if sc is not None:
                ReleaseKey(sc)

        for binding in self.keys.values():
            ReleaseKey(binding['key'])
            for mod in binding['mods']:
                ReleaseKey(mod)

    def get_collisions(self, key_name: str) -> list[str]:
        """ Get key name collisions (keys used for more than one binding).
        @param key_name: The key name (i.e. UI_Up, UI_Down).
        """
        key = self.keys.get(key_name)
        collisions = []
        for k, v in self.keys.items():
            if key == v:
                collisions.append(k)
        return collisions
