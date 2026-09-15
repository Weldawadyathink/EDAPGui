"""Run manually from the repo: .venv-macos/bin/python tests_native/smoke_gui.py.

Uses local Elite data read-only, copies EDAP configuration into a temporary
working directory, blocks native input, and constructs a withdrawn Tk window.
No game launch, assist engine, speech, network update, or hotkey registration.
"""

import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import mock

repo = Path.cwd()
sys.path.insert(0, str(repo))
import EDAPGui as gui_module
import ED_AP as ap_module
from Screen import BRIDGE_HEADER
import MacOSBridge
import EDKeys
from ShipCalibration import Trial

with tempfile.TemporaryDirectory(prefix='edap gui smoke ') as directory:
    work = Path(directory)
    shutil.copytree(repo / 'configs', work / 'configs')
    shutil.copytree(repo / 'waypoints', work / 'waypoints')
    for name in ['locales', 'templates', 'screen']:
        (work / name).symlink_to(repo / name, target_is_directory=True)
    frame = work / 'frame.raw'
    frame.write_bytes(BRIDGE_HEADER.pack(2, 2560, 1440) + bytes(2560*1440*4))
    os.chdir(work)
    real_load = ap_module.EDAutopilot.load_config
    def load_config(ap):
        real_load(ap)
        ap.config.update(VoiceEnable=False, HotkeysEnable=False, EnableEDMesg=False)
    def no_input(*a, **kw):
        raise AssertionError('GUI smoke attempted native input')
    with mock.patch.dict(os.environ, {'EDAP_CAPTURE_FILE':str(frame), 'EDAP_DISABLE_OVERLAY':'1'}), \
         mock.patch.object(ap_module.EDAutopilot, 'load_config', load_config), \
         mock.patch.object(ap_module, 'delete_old_log_files', lambda: None), \
         mock.patch.object(gui_module, 'EDAutopilot', lambda cb: ap_module.EDAutopilot(cb, do_thread=False)), \
         mock.patch.object(gui_module.keyboard, 'remove_all_hotkeys'), \
         mock.patch.object(MacOSBridge.bridge, 'request', no_input), \
         mock.patch.object(EDKeys, 'PressKey', no_input), \
         mock.patch.object(EDKeys, 'ReleaseAllKeys', return_value=True), \
         mock.patch.object(gui_module.APGui, 'check_updates', lambda self: None):
        root = gui_module.tk.Tk()
        root.withdraw()
        app = gui_module.APGui(root)
        root.update()
        # Exercise profile editing/automatic selection against copied data.
        # Synthetic journal identity changes only this no-engine process.
        app.ed_ap.jn.ship.update(type='python', ship_id=101,
            ship_name='Smoke Python', loadout_fingerprint='smoke-loadout')
        tab = app.ship_calibration_tab
        tab.refresh()
        tab.name_var.set('Smoke profile')
        tab.create()
        python_pid = tab.pid
        tab.assign()
        trial = tab.store.add(python_pid, Trial('roll', 1, 'Speed0/ENG2',
            .04, .04, 1, .1, .5), tab.identity())
        tab.render()
        assert tab.samples.exists(trial.id)
        tab.samples.selection_set(trial.id)
        tab.toggle()
        assert not tab.store.profile(python_pid)['trials'][-1]['enabled']
        app.ed_ap.jn.ship.update(type='anaconda', ship_id=102)
        tab.refresh()
        tab.name_var.set('Smoke Anaconda')
        tab.create()
        tab.assign()
        anaconda_pid = tab.pid
        assert anaconda_pid != python_pid
        app.ed_ap.jn.ship.update(type='python', ship_id=103)
        tab.refresh()
        assert tab.pid == python_pid
        assert tab.store.selected(tab.identity())['id'] == python_pid
        assert not tab.curve().ready
        root.update()
        app.stop_all_assists()
        root.update()
        app.close_window()
        root.update()
        print('PASS: full Tk GUI, profile editing, hull switching, callbacks, stop and shutdown; input blocked')
