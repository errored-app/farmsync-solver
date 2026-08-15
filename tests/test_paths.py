"""Where every file lives, in all three modes.

The first test here is the important one. If source mode ever stops returning
today's paths, a live operator's state.db silently relocates and the tool
forgets every ban and every grace stamp it has learned — with nothing in the
output saying so.
"""

import sys

from src import paths


def test_source_mode_returns_todays_exact_paths(monkeypatch):
    """The regression that would silently relocate a live operator's database."""
    # paths.py honours FARMSYNC_DATA_DIR from any environment, so anyone who
    # has set it would otherwise fail this test for no reason of their making.
    monkeypatch.delenv(paths.OVERRIDE_ENV, raising=False)
    assert paths.portable() is False
    assert paths.config_file() == paths.REPO_ROOT / "input" / "config.json"
    assert paths.state_db() == paths.REPO_ROOT / "data" / "state.db"


def test_source_mode_has_no_exe_directory():
    assert paths.exe_dir() is None


def test_the_override_flattens_both_files_into_one_directory(tmp_path, monkeypatch):
    """FARMSYNC_DATA_DIR means 'act frozen', which is how a frozen layout is
    testable without freezing anything."""
    monkeypatch.setenv(paths.OVERRIDE_ENV, str(tmp_path))
    assert paths.portable() is True
    assert paths.config_file() == tmp_path / "config.json"
    assert paths.state_db() == tmp_path / "state.db"
    assert paths.updates_dir() == tmp_path / "updates"


def test_a_frozen_build_lives_under_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.OVERRIDE_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.user_dir() == tmp_path / "FarmsyncSolver"
    assert paths.config_file() == tmp_path / "FarmsyncSolver" / "config.json"
    assert paths.state_db() == tmp_path / "FarmsyncSolver" / "state.db"


def test_the_override_wins_over_a_frozen_build(tmp_path, monkeypatch):
    """Otherwise a frozen build could not be pointed at a scratch directory."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv(paths.OVERRIDE_ENV, str(tmp_path / "scratch"))
    assert paths.user_dir() == tmp_path / "scratch"


def test_a_frozen_build_with_no_localappdata_falls_back_to_the_profile(tmp_path, monkeypatch):
    """LOCALAPPDATA is always set on a real Windows session, but a service
    account or a stripped environment is not worth crashing over."""
    monkeypatch.delenv(paths.OVERRIDE_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
    assert paths.user_dir() == tmp_path / "AppData" / "Local" / "FarmsyncSolver"


def test_exe_dir_is_the_folder_holding_the_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "FarmsyncSolver.exe"))
    assert paths.exe_dir() == tmp_path


def test_state_and_util_agree_with_paths_rather_than_computing_their_own():
    """Both modules used to resolve from their own __file__. If either drifts
    back to that, a frozen build writes its database into a temp directory
    that is deleted on exit."""
    from src import state, util
    assert state.DB_PATH == paths.state_db()
    assert util.CONFIG_FILE == paths.config_file()
