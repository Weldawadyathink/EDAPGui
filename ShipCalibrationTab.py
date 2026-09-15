"""Tk presentation for measured ship response. All widget work stays on Tk."""
from __future__ import annotations

import csv
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from ShipCalibration import AXES, THROTTLES, CalibrationError, Conditions, ShipIdentity
from ShipCalibrationRuntime import LiveCalibration, operating_conditions


class ShipCalibrationTab:
    def __init__(self, parent, gui):
        self.gui, self.ap = gui, gui.ed_ap
        self.store = self.ap.calibration_store
        self.frame = ttk.Frame(parent, padding=12)
        self.frame.pack(fill='both', expand=True)
        self.frame.columnconfigure(0, weight=1)
        self._identity = None
        self._profile_ids = []
        self.hull = tk.StringVar()
        self.profile_var = tk.StringVar()
        self.name_var = tk.StringVar(value='My ship')
        self.throttle = tk.StringVar(value='Speed0')
        self.pips = tk.StringVar(value='2')
        self.axis = tk.StringVar(value='roll')
        self.direction = tk.StringVar(value='+')
        self.current = tk.StringVar()
        self.message = tk.StringVar(value='Create a profile, set the flight conditions, then check the reference view.')
        self.summary = tk.StringVar()
        ttk.Label(self.frame, text='Ship response calibration', font=('', 15, 'bold')).grid(row=0, sticky='w')
        ttk.Label(self.frame, textvariable=self.current, wraplength=760).grid(row=1, sticky='w', pady=(3, 8))
        profiles = ttk.LabelFrame(self.frame, text='Profiles', padding=8)
        profiles.grid(row=2, sticky='ew')
        profiles.columnconfigure(1, weight=1)
        ttk.Label(profiles, text='Hull').grid(row=0, column=0, sticky='w')
        self.hull_combo = ttk.Combobox(profiles, textvariable=self.hull, state='readonly', width=25)
        self.hull_combo.grid(row=0, column=1, sticky='ew', padx=6)
        self.hull_combo.bind('<<ComboboxSelected>>', lambda _: self.refresh())
        ttk.Button(profiles, text='Follow current ship', command=self.follow_ship).grid(row=0, column=2)
        ttk.Label(profiles, text='Profile').grid(row=1, column=0, sticky='w', pady=5)
        self.profile_combo = ttk.Combobox(profiles, textvariable=self.profile_var, state='readonly')
        self.profile_combo.grid(row=1, column=1, sticky='ew', padx=6)
        self.profile_combo.bind('<<ComboboxSelected>>', lambda _: self.render())
        profile_actions = ttk.Frame(profiles)
        profile_actions.grid(row=1, column=2)
        ttk.Button(profile_actions, text='Rename',
                   command=lambda: self.action(self.rename)).pack(side='left')
        ttk.Button(profile_actions, text='Delete',
                   command=lambda: self.action(self.delete)).pack(side='left', padx=(4, 0))
        ttk.Entry(profiles, textvariable=self.name_var).grid(row=2, column=1, sticky='ew', padx=6)
        ttk.Button(profiles, text='New empty profile', command=lambda: self.action(self.create)).grid(row=2, column=2)
        buttons = ttk.Frame(profiles)
        buttons.grid(row=3, column=0, columnspan=3, sticky='w', pady=(5, 0))
        ttk.Button(buttons, text='Use for this hull',
                   command=lambda: self.action(self.assign)).pack(side='left', padx=2)
        conditions = ttk.Frame(self.frame)
        conditions.grid(row=3, sticky='ew', pady=8)
        for column, (label, variable, values) in enumerate([
            ('Throttle / mode', self.throttle, THROTTLES), ('ENG pips', self.pips, ('0', '.5', '1', '1.5', '2', '2.5', '3', '3.5', '4')),
            ('Axis', self.axis, AXES), ('Direction', self.direction, ('+', '-'))]):
            box = ttk.Frame(conditions)
            box.grid(row=0, column=column, padx=(0, 10), sticky='w')
            ttk.Label(box, text=label).pack(anchor='w')
            combo = ttk.Combobox(box, textvariable=variable, values=values, state='readonly', width=14 if column == 0 else 8)
            combo.pack()
            combo.bind('<<ComboboxSelected>>', lambda _: self.render())
        ttk.Button(conditions, text='Set selected throttle', command=self.set_throttle).grid(row=0, column=4)
        ttk.Label(self.frame, textvariable=self.summary, wraplength=780).grid(row=4, sticky='w')
        self.canvas = tk.Canvas(self.frame, width=750, height=185, bg='#19212b', highlightthickness=0)
        self.canvas.grid(row=5, sticky='ew', pady=7)
        self.canvas.bind('<Configure>', lambda _: self.draw())
        commands = ttk.Frame(self.frame)
        commands.grid(row=6, sticky='ew')
        ttk.Button(commands, text='Check reference (no input)', command=self.preview).pack(side='left', padx=2)
        ttk.Button(commands, text='Start axis experiment…', command=self.run).pack(side='left', padx=2)
        ttk.Button(commands, text='Stop / End', command=self.gui.stop_all_assists).pack(side='left', padx=2)
        ttk.Button(commands, text='Export trials CSV', command=lambda: self.action(self.export)).pack(side='right', padx=2)
        ttk.Label(self.frame, textvariable=self.message, wraplength=780).grid(row=7, sticky='w', pady=6)
        self.samples = ttk.Treeview(self.frame, columns=('purpose', 'pulse', 'angle', 'state'), show='headings', height=5)
        for key, label, width in [('purpose', 'Trial', 80), ('pulse', 'Measured pulse', 105),
                                  ('angle', 'Rotation', 85), ('state', 'Quality / reason', 460)]:
            self.samples.heading(key, text=label)
            self.samples.column(key, width=width, stretch=key == 'state')
        self.samples.grid(row=8, sticky='nsew')
        self.frame.rowconfigure(8, weight=1)
        ttk.Button(self.frame, text='Include / exclude selected trial', command=lambda: self.action(self.toggle)).grid(row=9, sticky='w', pady=4)
        self.refresh()

    @property
    def pid(self):
        index = self.profile_combo.current()
        return self._profile_ids[index] if 0 <= index < len(self._profile_ids) else None

    def identity(self):
        # The journal engine owns updates. A shallow snapshot is enough for UI
        # identity; live experiments independently read the journal each time.
        return ShipIdentity.from_journal(dict(self.ap.jn.ship))

    def action(self, callback):
        if self.ap.calibration_busy.is_set():
            self.message.set('Finish or stop the active experiment before editing profiles.')
            return
        try:
            callback()
        except (CalibrationError, OSError, ValueError) as exc:
            self.message.set(str(exc))
        self.refresh()

    def follow_ship(self):
        self.hull.set(self.identity().hull)
        self._identity = None
        self.refresh()

    def refresh(self):
        identity = self.identity()
        changed = identity != self._identity
        old_pid = self.pid
        self._identity = identity
        if changed or not self.hull.get():
            self.hull.set(identity.hull)
        hulls = {p['hull'] for p in self.store.profiles()}
        if identity.hull:
            hulls.add(identity.hull)
        self.hull_combo['values'] = sorted(hulls)
        active = self.store.selected(identity)
        active_label = active['name'] if active else 'unassigned — response unknown'
        self.current.set(f'{identity.name or identity.hull or "No ship identified"} · ShipID {identity.ship_id if identity.ship_id is not None else "unknown"} · Active: {active_label}')
        profiles = self.store.profiles(self.hull.get())
        self._profile_ids = [p['id'] for p in profiles]
        self.profile_combo['values'] = [f'{p["name"]}  · {p["id"][:6]}' for p in profiles]
        selected = active['id'] if changed and active else old_pid
        if profiles:
            self.profile_combo.current(self._profile_ids.index(selected) if selected in self._profile_ids else 0)
        else:
            self.profile_var.set('')
        if self.store.error:
            self.message.set(self.store.error)
        self.render()

    def selected_trials(self):
        if self.pid is None:
            return []
        key = Conditions(self.throttle.get(), float(self.pips.get())).key
        loadout = self.view_loadout()
        return [t for t in self.store.profile(self.pid)['trials'] if
                (t['context'], t['axis'], t['direction']) ==
                (key, self.axis.get(), 1 if self.direction.get() == '+' else -1)
                and t.get('loadout', '') == loadout]

    def view_loadout(self):
        if self.pid is None:
            return ''
        profile = self.store.profile(self.pid)
        identity = self.identity()
        if profile['hull'] == identity.hull and identity.loadout:
            return identity.loadout
        return profile['loadout']

    def curve(self):
        if self.pid is None:
            return None
        context = Conditions(self.throttle.get(), float(self.pips.get())).key
        return self.store.curve(
            self.pid, context, self.axis.get(),
            1 if self.direction.get() == '+' else -1, self.view_loadout())

    def render(self):
        self.samples.delete(*self.samples.get_children())
        for trial in self.selected_trials():
            state = 'Excluded' if not trial['enabled'] else trial['reason'] or 'Accepted measurement'
            self.samples.insert('', 'end', iid=trial['id'], values=(trial['purpose'], f'{trial["duration"]:.4f}s', f'{trial["degrees"]:.2f}°', state))
        curve = self.curve()
        if curve is None or not curve.knots:
            self.summary.set('Unknown response. No default curve is assumed. Each direction and operating condition is measured separately.')
        else:
            status = 'Validated' if curve.ready else 'Not ready: ' + '; '.join(curve.issues)
            self.summary.set(f'{status}\n{curve.fit_count} training / {curve.validation_count} current validation trials · repeat spread {curve.repeat_spread:.2f}°'
                             + (f' · max validation error {curve.validation_error:.2f}°' if curve.validation_error is not None else ''))
        if self.pid:
            profile = self.store.profile(self.pid)
            identity = self.identity()
            warnings = []
            if profile['hull'] == identity.hull:
                if not identity.loadout:
                    warnings.append('Current loadout has not arrived from the journal yet')
                elif profile['loadout'] and profile['loadout'] != identity.loadout:
                    warnings.append('Loadout changed since this hull profile was calibrated')
            if warnings:
                self.summary.set('Warning: ' + ' · '.join(warnings)
                                 + '. The hull profile remains selected and usable.\n'
                                 + self.summary.get())
        self.draw()

    def draw(self):
        self.canvas.delete('all')
        w, h = max(self.canvas.winfo_width(), 650), 185
        trials, curve = self.selected_trials(), self.curve()
        if not trials:
            self.canvas.create_text(w/2, h/2, text='Unknown — awaiting measured pulses', fill='#ced8e5', font=('', 12))
            return
        max_t = max([t['duration'] for t in trials]+[.1])*1.12
        max_a = max([abs(t['degrees']) for t in trials]+[1])*1.15
        def point(t, a):
            return 48+t/max_t*(w-70), h-30-max(0, a)/max_a*(h-55)
        self.canvas.create_line(48, 15, 48, h-30, w-20, h-30, fill='#718198')
        self.canvas.create_text(w/2, h-9, text='Measured key hold (seconds)', fill='#aab9cc')
        self.canvas.create_text(55, 9, text=f'{max_a:.1f}° settled rotation', fill='#aab9cc', anchor='nw')
        self.canvas.create_text(w-25, h-20, text=f'{max_t:.3f}s', fill='#aab9cc', anchor='e')
        if curve and len(curve.knots) > 1:
            coords = [coord for t, a in curve.knots for coord in point(t, a)]
            self.canvas.create_line(*coords, fill='#56d6c2' if curve.ready else '#d7b16c', width=2)
        for trial in trials:
            x, y = point(trial['duration'], trial['degrees'])
            color = '#738197' if not trial['enabled'] or not trial['accepted'] else '#cc94ff' if trial['purpose'] == 'validation' else '#56d6c2'
            self.canvas.create_oval(x-3, y-3, x+3, y+3, fill=color, outline='')
        self.canvas.create_text(w-24, 10, text='Teal: training   Purple: validation   Gray: excluded/rejected', anchor='ne', fill='#aab9cc')

    def create(self):
        pid = self.store.create(self.name_var.get(), self.hull.get())
        self.refresh()
        self.profile_combo.current(self._profile_ids.index(pid))
        self.message.set('Created an empty profile. Existing measurements and legacy settings were preserved.')

    def rename(self):
        profile = self.store.profile(self.pid)
        name = simpledialog.askstring('Rename calibration profile', 'Profile name:', initialvalue=profile['name'], parent=self.frame)
        if name:
            self.store.rename(self.pid, name)

    def delete(self):
        profile = self.store.profile(self.pid)
        if messagebox.askyesno(
                'Delete calibration profile',
                f'Delete “{profile["name"]}” and all of its measured trials?\n\n'
                'This cannot be undone.', parent=self.frame):
            self.store.delete(self.pid)
            self.message.set('Calibration profile deleted.')

    def assign(self):
        self.store.assign(self.pid, self.identity())
        self.message.set('Profile assigned. Only validated channels matching current conditions can send rotations.')

    def toggle(self):
        selected = self.samples.selection()
        if selected:
            self.store.toggle_trial(self.pid, selected[0])
            self.message.set('Trial inclusion changed. Training changes require new independent validation.')

    def export(self):
        profile = self.store.profile(self.pid)
        path = filedialog.asksaveasfilename(parent=self.frame, defaultextension='.csv', initialfile='ship-calibration.csv')
        if path:
            fields = ('id', 'timestamp', 'session', 'ship_id', 'ship_name', 'loadout',
                      'axis', 'direction', 'context', 'purpose', 'requested', 'duration',
                      'degrees', 'noise', 'settle_seconds', 'timing_uncertainty', 'mass', 'accepted', 'enabled', 'reason', 'fit_revision')
            with open(path, 'w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(profile['trials'])
            self.message.set('Exported trials. Full visual traces remain in the profile JSON.')

    def set_throttle(self):
        requested = self.throttle.get()
        def set_speed():
            flags = self.ap.status.get_cleaned_data()['Flags']
            if bool(flags & 16) != requested.startswith('SC'):
                raise CalibrationError('Selected throttle mode does not match the current flight mode')
            {'0': self.ap.set_throttle_0, '50': self.ap.set_throttle_50, '100': self.ap.set_throttle_100}[requested.split('Speed')[1]]()
            self.ap.ap_ckb('ship_calibration_progress', 'Throttle command sent. Let speed settle; set ENG pips in Elite to match this page.')
        self.gui._start_background_task('set throttle', set_speed, clear_stop=True, coalesce=True)

    def preview(self):
        axis = self.axis.get()
        def inspect():
            identity, conditions, _ = operating_conditions(self.ap, require_experiment=True)
            observation = LiveCalibration(self.ap).observe()
            observation.check(axis)
            self.ap.ap_ckb('ship_calibration_progress', f'Reference visible · {conditions.key} · {identity.hull}. Keep controls still before starting.')
        self.gui._start_background_task('calibration reference check', inspect, clear_stop=True)

    def run(self):
        if not self.pid:
            self.message.set('Create or select a profile first.')
            return
        if any(task.is_alive() for name, task in self.gui._background_tasks.items() if name != 'update check'):
            self.message.set('Wait for other commands to finish before starting an experiment.')
            return
        pid, axis = self.pid, self.axis.get()
        context = Conditions(self.throttle.get(), float(self.pips.get())).key
        profile = self.store.profile(pid)
        if not messagebox.askokcancel('Supervised ship calibration',
            f'{profile["name"]} · {axis} in both directions · {context}\n\n'
            'This sends repeated short rotation inputs (starting at 0.04s, capped at 0.4s). '
            'It then sends separate validation pulses. It will not change throttle or ENG pips.\n\n'
            'Be in clear space, Flight Assist on, with a distant system selected. '
            'For roll, move the compass dot visibly off-center. Keep controls still and remain present. '
            'End stops the experiment. Start now?', parent=self.frame):
            return
        def experiment():
            _, actual, _ = operating_conditions(self.ap, require_experiment=True)
            if actual.key != context:
                raise CalibrationError(f'Current conditions are {actual.key}; selected view is {context}')
            LiveCalibration(self.ap).run(pid, axis)
        self.gui._start_background_task('ship calibration', experiment, clear_stop=True)
