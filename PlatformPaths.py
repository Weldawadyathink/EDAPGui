"""Cross-platform locations for Elite Dangerous data files."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _moltenvr_prefix() -> Path:
    configured = os.environ.get("MOLTENVR_PREFIX")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library/Application Support/MoltenVR/Bottles/MoltenVR"


def _wine_user_dir() -> Path:
    configured = os.environ.get("EDAP_WINE_USER_DIR")
    if configured:
        return Path(configured).expanduser()

    users_dir = _moltenvr_prefix() / "drive_c/users"
    preferred = users_dir / os.environ.get("EDAP_WINE_USER", os.environ.get("USER", ""))
    if preferred.is_dir():
        return preferred

    if users_dir.is_dir():
        for candidate in users_dir.iterdir():
            if (candidate / "Saved Games/Frontier Developments/Elite Dangerous").is_dir():
                return candidate
    return preferred


def elite_saved_games_dir() -> str:
    configured = os.environ.get("EDAP_ELITE_SAVED_GAMES")
    if configured:
        return str(Path(configured).expanduser())
    if sys.platform == "darwin":
        return str(_wine_user_dir() / "Saved Games/Frontier Developments/Elite Dangerous")
    if sys.platform != "win32":
        return "./linux_ed"

    from WindowsKnownPaths import FOLDERID, UserHandle, get_path
    return str(Path(get_path(FOLDERID.SavedGames, UserHandle.current)) /
               "Frontier Developments/Elite Dangerous")


def elite_local_appdata_dir() -> str:
    configured = os.environ.get("EDAP_ELITE_LOCAL_APPDATA")
    if configured:
        return str(Path(configured).expanduser())
    if sys.platform == "darwin":
        return str(_wine_user_dir() / "AppData/Local")
    if sys.platform != "win32":
        return os.environ.get("LOCALAPPDATA", ".")
    return os.environ["LOCALAPPDATA"]


def elite_options_dir() -> str:
    return str(Path(elite_local_appdata_dir()) /
               "Frontier Developments/Elite Dangerous/Options")
