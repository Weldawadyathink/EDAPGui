"""Bounded pulse experiments against injected observation/input adapters."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import math
import statistics
import time
import uuid

from ShipCalibration import CalibrationError, Trial


def angle_delta(before, after):
    return (before-after+180) % 360 - 180


@dataclass(frozen=True)
class Observation:
    time: float
    frame: int
    x: float
    y: float
    z: float
    confidence: float

    def angle(self, axis):
        if axis == 'roll':
            return math.degrees(math.atan2(self.x, self.y))
        if axis == 'pitch':
            return math.degrees(math.atan2(self.y, self.z))
        return math.degrees(math.atan2(self.x, self.z))

    def invariant(self, axis):
        return {'roll': self.z, 'pitch': self.x, 'yaw': self.y}[axis]

    def check(self, axis):
        if not all(math.isfinite(v) for v in (self.time, self.x, self.y, self.z, self.confidence)):
            raise CalibrationError('Nonfinite visual observation')
        if self.confidence < .75 or self.z < .25:
            raise CalibrationError('Reference must be confidently visible on the front of the compass')
        if abs(self.x*self.x+self.y*self.y+self.z*self.z-1) > .05:
            raise CalibrationError('Invalid compass geometry')
        radius = math.sqrt(max(0, 1-self.invariant(axis)**2))
        if radius < (.25 if axis == 'roll' else .5):
            raise CalibrationError('Reference is too close to the rotation axis; move it off-center for roll')


@dataclass(frozen=True)
class ExperimentPolicy:
    first_pulse: float = .04
    maximum_pulse: float = .4
    levels: int = 4
    repeats: int = 3
    settle_timeout: float = 4
    stable_span: float = .35
    stable_noise: float = .4
    maximum_response: float = 25
    maximum_session: float = 180


class CalibrationSession:
    """One axis, both directions; training followed by independent validation.

    No angle/rate prior is consulted. Pulse growth stops as response approaches
    the observation limit. Opposite pulses alternate but are never assumed equal.
    """
    def __init__(self, store, profile_id, observe, pulse, conditions, stop_event,
                 progress=lambda *_: None, clock=time.monotonic, policy=None):
        self.store, self.pid = store, profile_id
        self.observe, self.pulse, self.conditions = observe, pulse, conditions
        self.stop = stop_event
        self.progress, self.clock = progress, clock
        self.policy = policy or ExperimentPolicy()
        self.session = uuid.uuid4().hex
        self.trace = []
        self.last_frame = None

    def check(self):
        if self.stop.is_set():
            raise InterruptedError('Ship calibration stopped')
        if self.clock() - self.started > self.policy.maximum_session:
            raise CalibrationError('Calibration session time limit reached')
        identity, conditions, reference = self.conditions()
        if identity != self.identity or conditions.key != self.context.key or reference != self.reference:
            raise CalibrationError('Ship, loadout, throttle, pips or reference changed during calibration')
        if self.context.mass and (conditions.mass is None or abs(conditions.mass/self.context.mass-1) > .02):
            raise CalibrationError('Mass changed during calibration')

    def stable(self, axis):
        deadline = self.clock()+self.policy.settle_timeout
        readings = []
        while self.clock() < deadline:
            self.check()
            observation = self.observe()
            observation.check(axis)
            if self.clock()-observation.time > .75:
                raise CalibrationError('Vision is too delayed for calibration')
            if observation.frame == self.last_frame:
                if self.stop.wait(.04):
                    raise InterruptedError('Ship calibration stopped')
                continue
            self.last_frame = observation.frame
            self.trace.append(asdict(observation))
            readings.append(observation)
            readings = [r for r in readings if observation.time-r.time <= self.policy.stable_span+.3]
            if len(readings) >= 4 and readings[-1].time-readings[0].time >= self.policy.stable_span:
                center = readings[-1].angle(axis)
                deviations = [angle_delta(r.angle(axis), center) for r in readings]
                noise = max(deviations)-min(deviations)
                if noise <= self.policy.stable_noise:
                    # Pick the actual frame nearest the circular median, keeping
                    # its ray and timestamp together rather than inventing a ray.
                    median = statistics.median(deviations)
                    settled = min(readings, key=lambda r: abs(angle_delta(r.angle(axis), center)-median))
                    return settled, max(noise/2, .05)
            if self.stop.wait(.06):
                raise InterruptedError('Ship calibration stopped')
        raise CalibrationError('Reference did not settle; stop manual input and keep Flight Assist on')

    def trial(self, axis, direction, duration, purpose='fit', revision=''):
        self.trace = []
        self.progress(f'{purpose.title()}: {axis} {direction:+}, {duration:.3f}s', None)
        before, noise1 = self.stable(axis)
        self.check()
        started = self.clock()
        timing = None
        released = started
        response, noise, reason = 0.0, noise1, ''
        failure = None
        try:
            timing = self.pulse(axis, direction, duration)
            released = self.clock()
            self.check()
            after, noise2 = self.stable(axis)
            response = angle_delta(before.angle(axis), after.angle(axis))*direction
            noise += noise2
            if response < -max(.5, 3*noise):
                raise CalibrationError('Response is opposite the expected direction; inspect binding/vision before continuing')
            if abs(response) > self.policy.maximum_response:
                raise CalibrationError('Pulse exceeded the angular response limit')
            if abs(before.invariant(axis)-after.invariant(axis)) > .035:
                raise CalibrationError('Cross-axis motion or reference mismatch detected')
            if response <= max(.3, 3*noise):
                reason = 'Movement is below the measured noise floor'
            elif timing['uncertainty'] > max(.008, .2*duration):
                reason = 'Input timing uncertainty is too large'
        except (CalibrationError, InterruptedError, RuntimeError, OSError) as exc:
            failure = exc
            reason = str(exc) or type(exc).__name__
            if timing is None:
                # No reliable hold interval was returned. Keep a rejected
                # diagnostic record; these bounds never become training data.
                timing = {'duration': max(.001, min(3, self.clock()-started)),
                          'uncertainty': 3}
                released = self.clock()
                reason += ' (input duration unavailable; recorded elapsed attempt only)'
        trial = Trial(axis, direction, self.context.key, duration, timing['duration'], response, noise,
                      min(30, max(0, self.clock()-released)), purpose=purpose, accepted=not reason, reason=reason,
                      timing_uncertainty=timing['uncertainty'], mass=self.context.mass,
                      fit_revision=revision, trace=self.trace, session=self.session)
        try:
            trial = self.store.add(self.pid, trial, self.identity)
        except (CalibrationError, OSError) as persist_error:
            if failure is not None:
                raise failure from persist_error
            raise
        self.progress(reason or f'Measured {response:.2f}°', trial)
        if failure is not None:
            raise failure
        return trial

    def run(self, axis):
        if axis not in ('roll', 'pitch', 'yaw'):
            raise CalibrationError('Choose roll, pitch or yaw')
        self.started = self.clock()
        self.identity, self.context, self.reference = self.conditions()
        profile = self.store.profile(self.pid)
        if profile['hull'] != self.identity.hull or not self.identity.loadout:
            raise CalibrationError('Select a profile for the current loaded ship')
        duration = self.policy.first_pulse
        for level in range(self.policy.levels):
            results = []
            for _ in range(self.policy.repeats):
                for direction in (1, -1):
                    results.append(self.trial(axis, direction, duration))
            maximum = max((t.degrees for t in results if t.accepted), default=0)
            if maximum > 12 or duration >= self.policy.maximum_pulse:
                break
            duration = round(min(duration*1.7, self.policy.maximum_pulse), 4)
        # Validation uses new pulses at inter-knot durations. Never train on it.
        for direction in (1, -1):
            curve = self.store.curve(
                self.pid, self.context.key, axis, direction, self.identity.loadout)
            if len(curve.knots) < 3:
                self.progress('Insufficient measurable coverage; retained trials for inspection', None)
                continue
            mids = [(a[0]+b[0])/2 for a, b in zip(curve.knots, curve.knots[1:])]
            low = curve.knots[0][0] / 2
            for duration in (low, mids[-1], low):
                self.trial(axis, direction, duration, 'validation', curve.revision)
        self.store.complete_session(self.pid, self.session, self.identity)
        self.progress('Experiment complete. This hull profile passed independent validation.', None)
