"""Live adapters; constructing these objects never sends input."""
from __future__ import annotations
from contextlib import contextmanager
import math
import time

from EDAP_data import (FlagsInMainShip, FlagsDocked, FlagsLanded, FlagsSupercruise,
    FlagsFlightAssistOff, FlagsLandingGearDown, FlagsCargoScoopDeployed,
    FlagsFsdCharging, FlagsFsdJump, FlagsBeingInterdicted, FlagsIsInDanger,
    FlagsOverHeating, FlagsScoopingFuel, FlagsHasLatLong, FlagsInFighter,
    Flags2OnFoot, Flags2FsdScoActive, Flags2SupercruiseAssistActive)
from ShipCalibration import CalibrationError, Conditions, ShipIdentity
from ShipCalibrationSession import CalibrationSession, Observation

BINDINGS = {'roll': ('RollLeftButton', 'RollRightButton'),
            'pitch': ('PitchDownButton', 'PitchUpButton'),
            'yaw': ('YawLeftButton', 'YawRightButton')}
ASSISTS = ('fsd_assist_enabled', 'sc_assist_enabled', 'waypoint_assist_enabled',
           'robigo_assist_enabled', 'afk_combat_assist_enabled', 'dss_assist_enabled',
           'single_waypoint_enabled')


def operating_conditions(ap, require_experiment=False):
    state = ap.jn.ship_state()
    identity = ShipIdentity.from_journal(state)
    status = ap.status.get_cleaned_data()
    flags, flags2 = status['Flags'], status.get('Flags2') or 0
    if not identity.hull or not flags & FlagsInMainShip or flags & FlagsInFighter or flags2 & Flags2OnFoot:
        raise CalibrationError('Ship response requires the main ship cockpit')
    if flags & FlagsFlightAssistOff:
        raise CalibrationError('These profiles require Flight Assist on')
    if flags2 & (Flags2FsdScoActive | Flags2SupercruiseAssistActive):
        raise CalibrationError('Disengage SCO and built-in Supercruise Assist before using these profiles')
    if status.get('GuiFocus') != 0:
        raise CalibrationError('Return to cockpit view')
    throttle = ap.speed_demand
    if throttle is None or throttle.startswith('SC') != bool(flags & FlagsSupercruise):
        raise CalibrationError('Set a known throttle in EDAP for the current flight mode')
    pips = status.get('pips')
    if not pips or pips.get('engine') is None:
        raise CalibrationError('Journal status does not currently report engine pips')
    mass = state.get('unladen_mass')
    if mass is not None:
        fuel, cargo = status.get('FuelMain'), status.get('Cargo')
        mass = None if fuel is None or cargo is None else mass + fuel + cargo + (status.get('FuelReservoir') or 0)
    conditions = Conditions(throttle, pips['engine'], mass)
    if require_experiment:
        blocked = (FlagsDocked | FlagsLanded | FlagsLandingGearDown | FlagsCargoScoopDeployed |
                   FlagsFsdCharging | FlagsFsdJump | FlagsBeingInterdicted | FlagsIsInDanger |
                   FlagsOverHeating | FlagsScoopingFuel | FlagsHasLatLong)
        if flags & blocked:
            raise CalibrationError('Calibration requires clear space: undocked, away from surfaces, danger and scooping')
        if not state.get('target'):
            raise CalibrationError('Select a distant system as a fixed compass reference')
        destination = status.get('Destination_Name')
        if destination and destination.casefold() != state['target'].casefold():
            # FSDTarget can remain in the journal after the pilot selects a
            # local body. That retained route target is not the compass target.
            raise CalibrationError('The current destination is not the selected distant system')
        if any(getattr(ap, name, False) for name in ASSISTS):
            raise CalibrationError('Stop all flight assists before calibrating')
    reference = (state.get('target'), status.get('Destination_System'),
                 status.get('Destination_Body'), status.get('Destination_Name'))
    return identity, conditions, reference


class LiveCalibration:
    def __init__(self, ap):
        self.ap = ap
        self._fallback_frame = 0

    def observe(self):
        self.ap.raise_if_stop_requested()
        off = self.ap.get_nav_offset(self.ap.scrReg)
        if not off:
            raise CalibrationError('Compass reference lost; no more input will be sent')
        radius2 = off['x']**2 + off['y']**2
        if radius2 >= .96 or off['z'] <= 0:
            raise CalibrationError('Keep the system reference away from the compass edge and on its front')
        frame = off.get('frame_sequence')
        if frame is None:
            self._fallback_frame += 1
            frame = self._fallback_frame
        return Observation(off.get('captured_at', time.monotonic()), frame, off['x'], off['y'],
                           math.sqrt(1-radius2), off.get('confidence', 0))

    @contextmanager
    def reserve(self):
        with self.ap._resource_lock:
            if self.ap.calibration_busy.is_set():
                raise CalibrationError('Another ship calibration is already running')
            if any(getattr(self.ap, name, False) for name in ASSISTS):
                raise CalibrationError('Stop all flight assists before calibrating')
            self.ap.calibration_busy.set()
        try:
            with self.ap.keys.exclusive_input():
                try:
                    # A previously dispatched manual command may still be unwinding.
                    self.ap.raise_if_stop_requested()
                    yield
                finally:
                    # Release while ownership is still exclusive so a new
                    # command cannot be mistaken for calibration input.
                    self.ap.keys.release_all_keys()
        finally:
            with self.ap._resource_lock:
                self.ap.calibration_busy.clear()

    def pulse(self, axis, direction, duration):
        return self.ap.keys.pulse(BINDINGS[axis][direction > 0], duration)

    def run(self, pid, axis):
        with self.reserve():
            session = CalibrationSession(self.ap.calibration_store, pid, self.observe, self.pulse,
                lambda: operating_conditions(self.ap, require_experiment=True), self.ap.stop_event,
                progress=lambda message, trial: self.ap.ap_ckb('ship_calibration_progress', message))
            session.run(axis)
