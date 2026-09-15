"""Ship rotation using independently validated, measured response profiles."""
from __future__ import annotations

import math
from typing import TypedDict

from EDAP_data import GuiFocusNoFocus
from ShipCalibration import CalibrationRequired
from ShipCalibrationRuntime import BINDINGS, operating_conditions


def scale(inp, in_min, in_max, out_min, out_max, clamp):
    value = (inp-in_min)/(in_max-in_min)*(out_max-out_min)+out_min
    return max(min(value, max(out_min, out_max)), min(out_min, out_max)) if clamp else value


class CompassTargetOffset(TypedDict):
    roll: float
    pit: float
    yaw: float
    tar_occ: bool
    tar_behind: bool
    used_nav: bool
    used_tar: bool


class EDShipControl:
    def __init__(self, ed_ap, screen, keys, cb):
        self.ap, self.screen, self.keys, self.ap_ckb = ed_ap, screen, keys, cb
        self.status_parser = ed_ap.status
        self._last_profile_warning = None

    def goto_cockpit_view(self):
        for _ in range(12):
            self.ap.raise_if_stop_requested()
            if self.status_parser.get_gui_focus() == GuiFocusNoFocus:
                return True
            self.keys.send('UI_Back')
        return False

    def _rotate(self, axis, degrees):
        if not math.isfinite(degrees) or abs(degrees) > 360:
            raise ValueError('Rotation must be finite and within one revolution')
        self.ap.raise_if_stop_requested()
        if degrees == 0:
            return self.ap.get_compass_target_offset()
        if self.ap.calibration_busy.is_set():
            raise CalibrationRequired('Finish ship calibration before using alignment')
        identity, conditions, _ = operating_conditions(self.ap)
        warnings = self.ap.calibration_store.warnings(identity, conditions)
        warning_key = tuple(warnings)
        if warning_key and warning_key != self._last_profile_warning:
            self.ap_ckb('log', 'Ship calibration warning: ' + '; '.join(warnings))
        self._last_profile_warning = warning_key
        direction = 1 if degrees > 0 else -1
        curve = self.ap.calibration_store.require_curve(identity, conditions, axis, direction)
        # Large requests are split into measured, settled pulses. Neither end of
        # the curve is extrapolated, and a pulse never lasts more than 0.4 s.
        total = abs(degrees)
        maximum = curve.knots[-1][1]
        count = math.ceil(total/maximum)
        if count > 40:
            raise CalibrationRequired(f'{axis.title()} request exceeds bounded calibrated coverage')
        pulse_angle = total/count
        duration = curve.duration_for(pulse_angle)
        if duration > .4:
            raise CalibrationRequired('Measured profile exceeds the allowed single-pulse duration')
        for _ in range(count):
            self.ap.raise_if_stop_requested()
            current_identity, current_conditions, _ = operating_conditions(self.ap)
            if current_identity != identity or current_conditions.key != conditions.key:
                raise CalibrationRequired('Ship or operating conditions changed during rotation')
            self.ap.calibration_store.require_curve(current_identity, current_conditions, axis, direction)
            self.keys.pulse(BINDINGS[axis][direction > 0], duration)
            self.ap._interruptible_sleep(max(.5, curve.settle_seconds))
        # Alignment callers reobserve after each requested correction. Ordinary
        # flight never trains a profile; only the calibration tab records trials.
        return self.ap.get_compass_target_offset()

    def roll_clockwise_anticlockwise(self, deg, auto_tune=False, cur_deg=0.0):
        return self._rotate('roll', deg)

    def pitch_up_down(self, deg, auto_tune=False, cur_deg=0.0):
        return self._rotate('pitch', deg)

    def yaw_right_left(self, deg, auto_tune=False, cur_deg=0.0):
        return self._rotate('yaw', deg)

    def ship_tst_roll(self, angle):
        return self._rotate('roll', angle)

    def ship_tst_pitch(self, angle):
        return self._rotate('pitch', angle)

    def ship_tst_yaw(self, angle):
        return self._rotate('yaw', angle)
