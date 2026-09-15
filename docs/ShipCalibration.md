# Ship response calibration

Ship response calibration measures how far the current hull rotates after a
short keyboard pulse. It replaces the old guessed rate curve and continuous
auto-tune behavior. An empty profile is genuinely unknown: EDAP does not add
made-up points or learn from ordinary autopilot corrections.

## Profile rules

- A profile is permanently associated with one hull type.
- You can keep multiple named profiles for a hull and choose which one is
  active with **Use for this hull**.
- `ShipID` and ship name are stored with trials for diagnosis, but they never
  select or lock a profile.
- EDAP fingerprints the journal `Loadout` event. A changed loadout displays a
  warning, but does not switch, disable, or delete the active hull profile.
- Trials from different loadout fingerprints are retained but never mixed into
  one response curve. A newly validated experiment promotes that loadout as the
  profile's latest calibration.

The journal identifies the hull, mass, modules, and engineering state. It does
not report ship attitude or angular velocity, so journal data cannot substitute
for a measured rotation experiment.

## Before starting

Remain at the controls. In Elite:

1. Be in the main ship, undocked, in clear space and away from a planetary
   surface.
2. Turn Flight Assist on. Disengage SCO, built-in Supercruise Assist, fuel
   scooping, and every EDAP assist.
3. Select a distant star system. This is a fixed direction reference, not a
   distance input to the response model. A system target avoids parallax that a
   nearby body or station could introduce.
4. Set the desired throttle and ENG pips. Calibration is separate for normal
   space and supercruise, throttle setting, axis, ENG pips, and direction.
5. For roll, place the compass dot visibly away from its center. A point on the
   roll axis cannot reveal roll angle.

Use **Check reference (no input)** first. It checks the live journal/status
conditions and the compass geometry without moving the ship.

## Running an experiment

Create or select an empty profile, select the throttle/mode, ENG pips, and
axis, then choose **Start axis experiment**. The selected axis is tested in both
directions.

The worker alternates directions and sends repeated pulses at gradually longer
durations. Each trial waits for stable observations before and after input. It
rejects stale or low-confidence vision, excessive noise, cross-axis movement,
unexpected direction, poor input timing, unsafe state changes, and responses
over the angular safety limit. **Stop / End** cooperatively cancels the worker
and releases held keys.

Three repeatable pulse lengths are fit with a monotone, non-overshooting curve.
Zero duration is constrained to zero rotation, allowing small final corrections
without inventing an initial rate. Separate low- and mid-range validation pulses
must agree with the fit before that axis/direction becomes usable. Validation
pulses are never training data.

Progress appears both on this tab and in the main UI log. The chart and trial
table remain available after a stopped or failed run, so useful evidence is not
lost. A suspect trial can be excluded; changing training data invalidates old
validation and requires a new experiment.

## What “ready” means

A green/validated curve is usable only for its displayed flight mode, throttle,
ENG pips, axis, and direction. EDAP interpolates within measured coverage and
splits larger turns into bounded pulses. It does not extrapolate beyond the
largest validated response. Missing calibration stops the requesting assist
with a clear UI-log message rather than silently falling back to a guessed
curve.

Loadout and mass warnings are advisory. They tell you when a fresh calibration
would be prudent while allowing the last validated hull profile to remain in
service.

## Data and recovery

Profiles and raw observation traces are stored in
`configs/ship_calibration.json`. This user-owned file is ignored by Git and is
replaced atomically on save. A corrupt file is left untouched and reported in
the UI. **Export trials CSV** exports summary measurements; the JSON retains the
full visual traces.

The implementation is informed by Frontier's journal `Loadout` semantics and
standard system-identification practice: collect controlled input/output data,
repeat measurements, constrain only known physical behavior, and validate on
independent observations.

- [Frontier Journal Manual](https://hosting.zaonce.net/community/journal/v37/Journal_Manual_v37.pdf)
- [MathWorks System Identification overview](https://www.mathworks.com/help/releases/r2025a/pdf_doc/ident/ident_gs.pdf)
- [SciPy monotone interpolation notes](https://docs.scipy.org/doc/scipy/tutorial/interpolate.html)

## Design choices and limits to test with a pilot

The measured quantity is **settled displacement after a pulse**, including
motion while Flight Assist brakes the ship after release. A single degrees per
second value would hide that behavior. Alternating short pulses, one axis at a
time, makes direction errors and unexpected motion diagnosable. Random inputs
across multiple axes would make both measurement and recovery harder. Repeated
levels expose noise; separate validation observations test whether the fitted
relationship predicts new measurements. This follows the input/output and
validation separation in the [MathWorks identification workflow](https://www.mathworks.com/help/ident/gs/system-identification-workflow.html).

The current experiment starts at 40 ms and grows by 1.7 per level, with four
levels, three repeats per direction, a 400 ms ceiling and a three-minute
session deadline. It stops growing above a 12-degree response and aborts above
25 degrees. These are provisional experimental bounds, not measured Elite
limits. Opposite pulses may have different effects; alternating them does not
guarantee a return to the starting orientation.

Important measurement limits:

- The compass-to-angle calculation assumes the existing compass image maps to
  a projected unit sphere. Detector geometry, polarity and accuracy still need
  to be checked against actual Elite movement. Passing synthetic tests cannot
  establish those facts.
- Throttle is the last command sent by EDAP, not measured speed. Wait for speed
  to settle and leave manual throttle controls alone. Supercruise conditions
  may require additional distinctions after flight testing.
- Zero input implies zero rotation only from rest. The line from zero to the
  shortest measured pulse is a modeling assumption checked with shorter
  validation pulses; sub-frame input response may still limit tiny corrections.
- The 0.8-degree / 15% validation tolerance, vision confidence and settling
  thresholds need adjustment using recorded real trials. Repeat spread is a
  diagnostic bound, not a statistical confidence interval.
- Recalibrating a channel changes its training revision. Old holdout results
  cannot validate the changed fit. Use a new named profile to preserve an
  existing calibration while experimenting with the same loadout.

### Supervised acceptance sequence

1. Start with normal space, known throttle, fixed ENG pips, and a distant system
   reference. Inspect the no-input reference check, then record one axis.
2. Verify the expected direction for each binding and compare observed motion
   with the stored before/after trace. Test roll with an off-center reference.
3. During a subsequent experiment, press End. Confirm immediate key release,
   responsive UI, no subsequent pulse, and retained failed-trial diagnostics.
4. Repeat an axis. Inspect repeat spread and holdout errors before trying small
   alignment corrections. Then test larger corrections that require multiple
   settled pulses; check accumulated error and timeouts.
5. Change throttle or ENG pips: unmeasured channels must report missing
   calibration. Switch hulls and verify automatic profile selection. Change
   loadout and verify the advisory warning and retained measurements.
6. Repeat separately in supercruise with the pilot present. Revisit the model
   and context keys if speed or environment produces inconsistent response.

Offline coverage includes nonlinear asymmetric simulated plants for all axes,
stale/lost vision, reversed motion, context changes, cancellation, input
ownership, partial delivery/release failure, and atomic persistence failure.
No Elite launch or live input is part of these tests.
