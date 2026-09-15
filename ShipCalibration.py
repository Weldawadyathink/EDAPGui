"""Measured ship response profiles. No game, GUI, or numerical package dependencies.

Curves map host-measured key-down duration to settled angular displacement.
Unknown channels have no synthetic knots; validation trials never train a curve.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import threading
import uuid

AXES = ('roll', 'pitch', 'yaw')
THROTTLES = ('Speed0', 'Speed50', 'Speed100', 'SCSpeed0', 'SCSpeed50', 'SCSpeed100')
SCHEMA_VERSION = 2


class CalibrationError(RuntimeError):
    pass


class CalibrationRequired(CalibrationError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def finite(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise CalibrationError(f'Invalid {name}: {value!r}')
    return float(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def loadout_fingerprint(event):
    """Exclude transient health, ammo and price; include modules/engineering/mass."""
    modules = [{k: module[k] for k in ('Slot', 'Item', 'Engineering') if k in module}
               for module in event.get('Modules', [])]
    return digest({'ship': event.get('Ship', '').lower(), 'mass': event.get('UnladenMass'),
                   'modules': sorted(modules, key=lambda item: item.get('Slot', ''))})


@dataclass(frozen=True)
class ShipIdentity:
    hull: str
    ship_id: int | None = None
    commander: str = ''
    name: str = ''
    loadout: str = ''

    @classmethod
    def from_journal(cls, state):
        return cls((state.get('type') or '').lower(), state.get('ship_id'),
                   state.get('commander_id') or '', state.get('ship_name') or '',
                   state.get('loadout_fingerprint') or '')

@dataclass(frozen=True)
class Conditions:
    throttle: str
    engine_pips: float
    mass: float | None = None

    def __post_init__(self):
        if self.throttle not in THROTTLES:
            raise CalibrationError('Set a known throttle before using ship calibration')
        finite(self.engine_pips, 0, 4, 'engine pips')
        if self.mass is not None:
            finite(self.mass, 0.01, 100000, 'ship mass')

    @property
    def key(self):
        return f'{self.throttle}/ENG{self.engine_pips:g}'


@dataclass
class Trial:
    axis: str
    direction: int
    context: str
    requested: float
    duration: float
    degrees: float
    noise: float
    settle_seconds: float
    purpose: str = 'fit'
    accepted: bool = True
    reason: str = ''
    enabled: bool = True
    timing_uncertainty: float = 0
    mass: float | None = None
    fit_revision: str = ''
    trace: list = field(default_factory=list)
    loadout: str = ''
    ship_id: int | None = None
    ship_name: str = ''
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session: str = ''
    timestamp: str = field(default_factory=now_iso)

    def validate(self):
        if self.axis not in AXES or self.direction not in (-1, 1) or self.purpose not in ('fit', 'validation'):
            raise CalibrationError('Invalid trial axis, direction or purpose')
        if not isinstance(self.context, str) or not self.context:
            raise CalibrationError('Trial is missing its operating conditions')
        finite(self.requested, .001, 2, 'requested pulse')
        finite(self.duration, .001, 3, 'measured pulse')
        finite(self.degrees, -180, 180, 'displacement')
        finite(self.noise, 0, 180, 'measurement noise')
        finite(self.settle_seconds, 0, 30, 'settle time')
        finite(self.timing_uncertainty, 0, 3, 'timing uncertainty')
        if not all(isinstance(v, bool) for v in (self.accepted, self.enabled)):
            raise CalibrationError('Invalid trial inclusion flag')
        if len(self.trace) > 500:
            raise CalibrationError('Trial trace is too large')
        if self.mass is not None:
            finite(self.mass, .01, 100000, 'trial mass')
        if not isinstance(self.loadout, str) or not isinstance(self.ship_name, str):
            raise CalibrationError('Invalid trial provenance')
        if self.ship_id is not None and (isinstance(self.ship_id, bool) or not isinstance(self.ship_id, int)):
            raise CalibrationError('Invalid trial ship id')


@dataclass(frozen=True)
class ResponseCurve:
    knots: tuple[tuple[float, float], ...]  # seconds, degrees
    revision: str
    repeat_spread: float
    settle_seconds: float
    fit_count: int
    validation_count: int
    validation_error: float | None
    issues: tuple[str, ...]
    mass: float | None

    @property
    def ready(self):
        return not self.issues

    def predict(self, seconds):
        if len(self.knots) < 2 or not 0 <= seconds <= self.knots[-1][0]:
            raise CalibrationRequired('Pulse is outside measured duration coverage')
        if seconds <= self.knots[0][0]:
            return self.knots[0][1] * seconds / self.knots[0][0]
        for (t0, a0), (t1, a1) in zip(self.knots, self.knots[1:]):
            if seconds <= t1:
                return a0 + (a1 - a0) * (seconds - t0) / (t1 - t0)
        return self.knots[-1][1]

    def duration_for(self, angle):
        if not self.ready:
            raise CalibrationRequired('; '.join(self.issues))
        if not math.isfinite(angle) or not 0 < angle <= self.knots[-1][1]:
            raise CalibrationRequired('Requested angle is outside measured coverage')
        if angle <= self.knots[0][1]:
            return self.knots[0][0] * angle / self.knots[0][1]
        for (t0, a0), (t1, a1) in zip(self.knots, self.knots[1:]):
            if angle <= a1:
                return t0 if a1 == a0 else t0 + (t1-t0) * (angle-a0) / (a1-a0)
        return self.knots[-1][0]


def fit_response(trials):
    """Median repeated pulse levels + isotonic regression, independently validated.

    Repeated levels expose outliers and noise. Pool-adjacent-violators enforces
    nondecreasing rotation without inventing a polynomial between measurements.
    This is a repeatability bound, not a statistical confidence interval.
    """
    fit = [t for t in trials if t.enabled and t.accepted and t.purpose == 'fit' and t.degrees > 0]
    revision = digest([asdict(t) for t in sorted(fit, key=lambda t: t.id)])
    groups = {}
    for trial in fit:
        groups.setdefault(round(trial.requested, 4), []).append(trial)
    issues, points, spreads = [], [], []
    for group in groups.values():
        median = statistics.median(t.degrees for t in group)
        mad = statistics.median(abs(t.degrees-median) for t in group)
        limit = max(.5, .15*median, 3*1.4826*mad)
        retained = [t for t in group if abs(t.degrees-median) <= limit]
        if len(retained) < 3:
            continue
        angle = statistics.median(t.degrees for t in retained)
        spread = max(max(t.degrees for t in retained)-min(t.degrees for t in retained),
                     max(t.noise for t in retained)*2)
        spreads.append(spread)
        if spread > max(.8, .2*angle):
            issues.append('Repeated pulses are too variable')
        points.append((statistics.median(t.duration for t in retained), angle, len(retained)))
    points.sort()
    if len(points) < 3:
        issues.append('Need three pulse lengths with three reliable repeats each')
    # PAVA with count weighting; every output knot stays at a measured duration.
    blocks = []
    for index, (_, angle, weight) in enumerate(points):
        blocks.append(([index], angle*weight, weight))
        while len(blocks) > 1 and blocks[-2][1]/blocks[-2][2] > blocks[-1][1]/blocks[-1][2]:
            right, left = blocks.pop(), blocks.pop()
            blocks.append((left[0]+right[0], left[1]+right[1], left[2]+right[2]))
    adjusted = {}
    for indices, total, weight in blocks:
        for index in indices:
            adjusted[index] = total/weight
    knots = tuple((t, adjusted[i]) for i, (t, _, _) in enumerate(points))
    if len(knots) > 1:
        if any(b[0] <= a[0] for a, b in zip(knots, knots[1:])):
            issues.append('Pulse timing does not separate the measured levels')
        if knots[-1][1] - knots[0][1] < max(1, max(spreads, default=0)*2):
            issues.append('Measured range is too small relative to noise')
        if any(abs(adjusted[i]-point[1]) > max(.8, .2*point[1]) for i, point in enumerate(points)):
            issues.append('Response is not consistently increasing; repeat this channel')
    settle = max((t.settle_seconds for t in fit), default=.5)
    masses = [t.mass for t in fit if t.mass is not None]
    curve = ResponseCurve(knots, revision, max(spreads, default=0), settle, len(fit), 0, None,
                          tuple(issues), statistics.median(masses) if masses else None)
    checks = [t for t in trials if t.enabled and t.accepted and t.purpose == 'validation'
              and t.fit_revision == revision and len(knots) >= 2
              and 0 < t.duration <= knots[-1][0]]
    errors = [abs(curve.predict(t.duration)-t.degrees) for t in checks]
    if len(checks) < 3 or len({round(t.requested, 4) for t in checks}) < 2:
        issues.append('Need three separate validation pulses at two lengths')
    elif any(error > max(.8, .15*t.degrees, 2*t.noise) for t, error in zip(checks, errors)):
        issues.append('Validation error exceeds 0.8° / 15% tolerance')
    return ResponseCurve(knots, revision, curve.repeat_spread, settle, len(fit), len(checks),
                         max(errors) if errors else None, tuple(dict.fromkeys(issues)), curve.mass)


class ProfileStore:
    """Thread-safe, atomic profile persistence; corrupt files remain untouched."""
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('EDAP_SHIP_CALIBRATION_FILE', 'configs/ship_calibration.json'))
        self._lock = threading.RLock()
        self.error = ''
        self.data = {'schema': SCHEMA_VERSION, 'profiles': {}, 'hulls': {}}
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())
                data = self._migrate(data)
                self._validate(data)
                self.data = data
        except (OSError, ValueError, TypeError, KeyError, CalibrationError) as exc:
            self.error = f'Cannot load calibration profiles; original file preserved: {exc}'

    @staticmethod
    def _migrate(data):
        """Drop prototype per-ship locks while retaining its measured trials."""
        if isinstance(data, dict) and data.get('schema') == 1:
            data = deepcopy(data)
            data['schema'] = SCHEMA_VERSION
            data.pop('ships', None)
            for profile in data.get('profiles', {}).values():
                profile.setdefault('updated', profile.get('created', now_iso()))
                profile.setdefault('last_ship_id', None)
                profile.setdefault('last_ship_name', '')
                for trial in profile.get('trials', []):
                    trial.setdefault('loadout', profile.get('loadout', ''))
                    trial.setdefault('ship_id', profile.get('last_ship_id'))
                    trial.setdefault('ship_name', profile.get('last_ship_name', ''))
        return data

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or data.get('schema') != SCHEMA_VERSION:
            raise CalibrationError('Unsupported calibration file version')
        for key in ('profiles', 'hulls'):
            if not isinstance(data.get(key), dict):
                raise CalibrationError(f'Invalid {key} collection')
        for pid, profile in data['profiles'].items():
            if profile['id'] != pid or not isinstance(profile['name'], str) or not profile['hull']:
                raise CalibrationError('Invalid calibration profile')
            for trial in profile['trials']:
                Trial(**trial).validate()
        if any(pid not in data['profiles'] for pid in data['hulls'].values()):
            raise CalibrationError('Profile assignment refers to a missing profile')
        for hull, pid in data['hulls'].items():
            if data['profiles'][pid]['hull'] != hull:
                raise CalibrationError('Profile assignment does not match its hull')

    def _commit(self, candidate):
        if self.error:
            raise CalibrationError(self.error)
        self._validate(candidate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, prefix='.ship-calibration-',
                                             delete=False, encoding='utf-8') as stream:
                name = stream.name
                json.dump(candidate, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self.data = candidate
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def profiles(self, hull=None):
        with self._lock:
            return deepcopy([p for p in self.data['profiles'].values() if hull is None or p['hull'] == hull])

    def profile(self, pid):
        with self._lock:
            if pid not in self.data['profiles']:
                raise CalibrationError('Select a calibration profile')
            return deepcopy(self.data['profiles'][pid])

    def create(self, name, hull):
        name, hull = name.strip(), hull.strip().lower()
        if not name or not hull:
            raise CalibrationError('A profile needs a name and a hull type')
        with self._lock:
            pid = uuid.uuid4().hex
            candidate = deepcopy(self.data)
            timestamp = now_iso()
            candidate['profiles'][pid] = {
                'id': pid, 'name': name, 'hull': hull, 'loadout': '',
                'last_ship_id': None, 'last_ship_name': '', 'trials': [],
                'created': timestamp, 'updated': timestamp}
            self._commit(candidate)
            return pid

    def rename(self, pid, name):
        if not name.strip():
            raise CalibrationError('Enter a profile name')
        with self._lock:
            candidate = deepcopy(self.data)
            candidate['profiles'][pid]['name'] = name.strip()
            self._commit(candidate)

    def assign(self, pid, identity):
        """Select a profile for its hull; ShipID remains provenance only."""
        with self._lock:
            profile = self.profile(pid)
            if profile['hull'] != identity.hull:
                raise CalibrationError('Profile hull does not match the current ship')
            candidate = deepcopy(self.data)
            candidate['hulls'][identity.hull] = pid
            self._commit(candidate)

    def selected(self, identity):
        with self._lock:
            pid = self.data['hulls'].get(identity.hull)
            return self.profile(pid) if pid else None

    def add(self, pid, trial, identity):
        trial = replace(trial, loadout=identity.loadout, ship_id=identity.ship_id,
                        ship_name=identity.name)
        trial.validate()
        with self._lock:
            candidate = deepcopy(self.data)
            profile = candidate['profiles'][pid]
            if profile['hull'] != identity.hull or not identity.loadout:
                raise CalibrationError('A current journal loadout for this hull is required')
            profile['trials'].append(asdict(trial))
            profile['updated'] = now_iso()
            self._commit(candidate)
        return trial

    def toggle_trial(self, pid, trial_id):
        with self._lock:
            candidate = deepcopy(self.data)
            trial = next(t for t in candidate['profiles'][pid]['trials'] if t['id'] == trial_id)
            trial['enabled'] = not trial['enabled']
            self._commit(candidate)

    def curve(self, pid, context, axis, direction, loadout=None):
        profile = self.profile(pid)
        loadout = profile['loadout'] if loadout is None else loadout
        return fit_response([Trial(**t) for t in profile['trials'] if
                             (t['context'], t['axis'], t['direction']) == (context, axis, direction)
                             and t.get('loadout', '') == loadout])

    def complete_session(self, pid, session, identity):
        """Promote a loadout only after both directions independently validate."""
        with self._lock:
            profile = self.profile(pid)
            session_trials = [Trial(**t) for t in profile['trials'] if t.get('session') == session]
            groups = {(t.context, t.axis, t.direction) for t in session_trials if t.purpose == 'fit'}
            if len(groups) != 2 or {direction for _, _, direction in groups} != {-1, 1}:
                raise CalibrationError('Calibration session did not measure both directions')
            for context, axis, direction in groups:
                curve = self.curve(pid, context, axis, direction, identity.loadout)
                if not curve.ready:
                    raise CalibrationError('; '.join(curve.issues))
            candidate = deepcopy(self.data)
            profile = candidate['profiles'][pid]
            profile['loadout'] = identity.loadout
            profile['last_ship_id'] = identity.ship_id
            profile['last_ship_name'] = identity.name
            profile['updated'] = now_iso()
            self._commit(candidate)

    def warnings(self, identity, conditions=None):
        """Report drift without changing selection or blocking a hull profile."""
        profile = self.selected(identity)
        if not profile:
            return []
        warnings = []
        if not identity.loadout:
            warnings.append('Current loadout has not arrived from the journal yet')
        elif profile['loadout'] and profile['loadout'] != identity.loadout:
            warnings.append('Loadout changed since this hull profile was calibrated')
        if conditions is not None and profile['loadout']:
            for axis in AXES:
                for direction in (-1, 1):
                    curve = self.curve(profile['id'], conditions.key, axis, direction)
                    if curve.mass is not None and conditions.mass is not None \
                            and abs(conditions.mass/curve.mass-1) > .05:
                        warnings.append('Current mass differs by more than 5% from calibration')
                        return warnings
        return warnings

    def require_curve(self, identity, conditions, axis, direction):
        profile = self.selected(identity)
        if not profile:
            raise CalibrationRequired(f'No calibration profile assigned to {identity.hull or "this ship"}')
        curve = self.curve(profile['id'], conditions.key, axis, direction)
        if not curve.ready:
            raise CalibrationRequired(f'{profile["name"]} · {conditions.key} · {axis} {direction:+}: ' + '; '.join(curve.issues))
        return curve
