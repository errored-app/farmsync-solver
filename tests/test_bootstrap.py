"""First-run behaviour: bringing an existing install forward, and the wizard.

`migrate` is a pure function taking three directories so it can be tested
against real files under tmp_path rather than against a mocked filesystem —
the same reason test_state.py builds a real database.
"""

from src import bootstrap


def test_migration_copies_a_repo_shaped_install(tmp_path):
    """The documented upgrade path: drop the .exe into the project folder."""
    source = tmp_path / "beside-the-exe"
    (source / "input").mkdir(parents=True)
    (source / "data").mkdir(parents=True)
    (source / "input" / "config.json").write_text('{"api_key": "live"}', encoding="utf-8")
    (source / "data" / "state.db").write_bytes(b"SQLite format 3\x00")

    config = tmp_path / "userdir" / "config.json"
    db = tmp_path / "userdir" / "state.db"

    done = bootstrap.migrate(source, config, db)

    assert config.read_text(encoding="utf-8") == '{"api_key": "live"}'
    assert db.read_bytes() == b"SQLite format 3\x00"
    assert len(done) == 2


def test_migration_also_accepts_files_sitting_flat_beside_the_exe(tmp_path):
    source = tmp_path / "beside-the-exe"
    source.mkdir()
    (source / "config.json").write_text("{}", encoding="utf-8")
    (source / "state.db").write_bytes(b"db")

    config = tmp_path / "userdir" / "config.json"
    db = tmp_path / "userdir" / "state.db"

    assert len(bootstrap.migrate(source, config, db)) == 2
    assert config.exists() and db.exists()


def test_the_two_files_migrate_independently(tmp_path):
    """An operator who kept their config but deleted state.db still gets the
    config. Requiring both would strand them on the wizard for no reason."""
    source = tmp_path / "beside-the-exe"
    (source / "input").mkdir(parents=True)
    (source / "input" / "config.json").write_text("{}", encoding="utf-8")

    config = tmp_path / "userdir" / "config.json"
    db = tmp_path / "userdir" / "state.db"

    done = bootstrap.migrate(source, config, db)

    assert config.exists()
    assert not db.exists()
    assert len(done) == 1


def test_migration_never_removes_the_original(tmp_path):
    """Copies, not moves: a migration that half-finished must be recoverable
    by running the tool again."""
    source = tmp_path / "beside-the-exe"
    (source / "input").mkdir(parents=True)
    original = source / "input" / "config.json"
    original.write_text("{}", encoding="utf-8")

    bootstrap.migrate(source, tmp_path / "userdir" / "config.json",
                      tmp_path / "userdir" / "state.db")

    assert original.exists()


def test_an_existing_destination_is_never_overwritten(tmp_path):
    """The operator's live config wins over anything found next to the .exe.
    Otherwise every launch from the project folder would clobber their edits."""
    source = tmp_path / "beside-the-exe"
    (source / "input").mkdir(parents=True)
    (source / "input" / "config.json").write_text('{"api_key": "stale"}', encoding="utf-8")

    config = tmp_path / "userdir" / "config.json"
    config.parent.mkdir()
    config.write_text('{"api_key": "current"}', encoding="utf-8")

    done = bootstrap.migrate(source, config, tmp_path / "userdir" / "state.db")

    assert config.read_text(encoding="utf-8") == '{"api_key": "current"}'
    assert done == []


def test_migration_from_an_empty_folder_reports_nothing(tmp_path):
    source = tmp_path / "beside-the-exe"
    source.mkdir()
    assert bootstrap.migrate(source, tmp_path / "c.json", tmp_path / "s.db") == []


def test_migration_from_a_missing_folder_does_not_raise(tmp_path):
    """exe_dir() can be None from source; the caller substitutes cwd, which may
    not hold anything at all."""
    assert bootstrap.migrate(tmp_path / "nope", tmp_path / "c.json",
                             tmp_path / "s.db") == []


import json

from src import paths


def test_the_wizard_writes_every_key_the_example_config_documents():
    """The drift guard. A key added to config.json and not to
    config.example.json is silently missed by a fresh clone. The wizard is a
    third copy of that list, so it is pinned to the second one rather than
    trusted."""
    example = json.loads(
        (paths.REPO_ROOT / "input" / "config.example.json").read_text(encoding="utf-8"))
    assert set(bootstrap.build_config({"api_key": "k", "farm_token": "t"})) == set(example)


def test_the_wizards_defaults_match_the_example_config():
    """The check above compares key names, so a `threads` reading 45 in one
    file and 15 in the other passes it. A fresh clone and a fresh wizard would
    then build two differently-sized pools with nothing saying so.

    Credential placeholders are skipped: the example is required to hold
    REPLACE_WITH_*, and the wizard is required not to."""
    example = json.loads(
        (paths.REPO_ROOT / "input" / "config.example.json").read_text(encoding="utf-8"))
    for key, value in bootstrap.DEFAULTS.items():
        if "REPLACE" in str(example[key]):
            continue
        assert example[key] == value, key


def test_the_wizard_writes_a_config_the_startup_guard_accepts():
    """main.py rejects any value containing REPLACE. A wizard that wrote a
    placeholder would produce a config that fails on the very next line."""
    built = bootstrap.build_config({"api_key": "real-key", "farm_token": "real-token"})
    assert built["api_key"] == "real-key"
    assert built["farm_token"] == "real-token"
    assert "REPLACE" not in json.dumps(built)


def test_the_wizard_writes_valid_json_to_a_directory_that_does_not_exist_yet(tmp_path):
    dest = tmp_path / "userdir" / "config.json"
    answers = iter(["my-api-key", "my-farm-token", "", ""])

    assert bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=lambda _: None)

    written = json.loads(dest.read_text(encoding="utf-8"))
    assert written["api_key"] == "my-api-key"
    assert written["farm_token"] == "my-farm-token"
    assert written["grace_minutes"] == 0


def test_pressing_enter_takes_the_default_for_the_optional_two(tmp_path):
    """Threads and the webhook are the two the operator may not have an answer
    for on a first run. Requiring them would be a wall in front of the tool."""
    dest = tmp_path / "config.json"
    answers = iter(["my-api-key", "my-farm-token", "", ""])

    bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=lambda _: None)

    written = json.loads(dest.read_text(encoding="utf-8"))
    assert written["threads"] == bootstrap.DEFAULTS["threads"]
    assert written["discord_webhook_url"] == ""


def test_the_wizard_stores_threads_as_a_number(tmp_path):
    """`min(config["threads"], max_concurrent)` compares it against an int. A
    string there sizes the pool by a comparison that cannot fail."""
    dest = tmp_path / "config.json"
    answers = iter(["k", "t", "30", ""])

    bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=lambda _: None)

    assert json.loads(dest.read_text(encoding="utf-8"))["threads"] == 30


def test_the_wizard_re_asks_a_thread_count_that_is_not_a_number(tmp_path):
    dest = tmp_path / "config.json"
    answers = iter(["k", "t", "lots", "30", ""])
    said = []

    bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=said.append)

    assert json.loads(dest.read_text(encoding="utf-8"))["threads"] == 30
    assert any("number" in line.lower() for line in said)


def test_the_wizard_re_asks_a_webhook_that_is_not_a_url(tmp_path):
    dest = tmp_path / "config.json"
    good = "https://discord.com/api/webhooks/1/xyz"
    answers = iter(["k", "t", "", "discord.com/api/webhooks/1/xyz", good])
    said = []

    bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=said.append)

    assert json.loads(dest.read_text(encoding="utf-8"))["discord_webhook_url"] == good
    assert any("https" in line for line in said)


def test_the_wizard_re_asks_on_an_empty_answer(tmp_path):
    dest = tmp_path / "config.json"
    answers = iter(["", "  ", "good-key", "good-token", "", ""])
    said = []

    assert bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=said.append)

    assert json.loads(dest.read_text(encoding="utf-8"))["api_key"] == "good-key"
    assert any("empty" in line for line in said)


def test_the_wizard_rejects_the_placeholder_text(tmp_path):
    """Pasting REPLACE_WITH_DIBYCAP_API_KEY out of the example is the obvious
    mistake, and the startup guard would reject it one screen later."""
    dest = tmp_path / "config.json"
    answers = iter(["REPLACE_WITH_DIBYCAP_API_KEY", "real-key", "real-token", "", ""])
    said = []

    assert bootstrap.run_wizard(dest, ask=lambda _: next(answers), echo=said.append)

    assert json.loads(dest.read_text(encoding="utf-8"))["api_key"] == "real-key"
    assert any("placeholder" in line for line in said)


def test_the_wizard_gives_up_rather_than_looping_forever(tmp_path):
    """A closed stdin raises EOFError on every read. Without a bound this is an
    infinite loop in a process the operator cannot see."""
    dest = tmp_path / "config.json"

    def closed(_):
        raise EOFError

    assert bootstrap.run_wizard(dest, ask=closed, echo=lambda _: None) is False
    assert not dest.exists()


def test_the_wizard_gives_up_after_three_empty_answers(tmp_path):
    dest = tmp_path / "config.json"
    answers = iter(["", "", "", "", "", ""])

    assert bootstrap.run_wizard(dest, ask=lambda _: next(answers),
                                echo=lambda _: None) is False
    assert not dest.exists()


def test_prepare_is_a_no_op_when_a_config_already_exists(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.OVERRIDE_ENV, str(tmp_path))
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    def must_not_be_called(_):
        raise AssertionError("the wizard ran with a config already present")

    assert bootstrap.prepare([], ask=must_not_be_called) is True


from src import update


class Recorder:
    """Stands in for src.update, recording what the offer decided to do."""

    def __init__(self, found=None):
        self.found = found
        self.applied = False
        self.relaunched = False
        self.relaunch_args = None


def install_update_double(monkeypatch, recorder, *, sha_ok=True, writable=True):
    monkeypatch.setattr(bootstrap.update, "check", lambda *_a, **_k: recorder.found)
    monkeypatch.setattr(bootstrap.update, "writable", lambda _f: writable)
    monkeypatch.setattr(bootstrap.update, "download",
                        lambda _s, _u, dest, **_k: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                    dest.write_bytes(b"new"), dest)[-1])
    monkeypatch.setattr(bootstrap.update, "fetch_text", lambda *_a, **_k: "sums")
    monkeypatch.setattr(bootstrap.update, "expected_sha", lambda *_a, **_k: "aaa")
    monkeypatch.setattr(bootstrap.update, "sha256_of",
                        lambda *_a, **_k: "aaa" if sha_ok else "bbb")

    def fake_apply(_new, _current):
        recorder.applied = True
        return _current

    def fake_relaunch(_exe, args):
        recorder.relaunched = True
        recorder.relaunch_args = list(args)

    monkeypatch.setattr(bootstrap.update, "apply", fake_apply)
    monkeypatch.setattr(bootstrap.update, "relaunch", fake_relaunch)


FOUND = {"version": "1.1.0", "tag": "v1.1.0",
         "exe_url": "https://x/e", "sums_url": "https://x/s"}


def test_a_source_run_never_offers_an_update(monkeypatch):
    """There is no .exe to replace, and a developer's git checkout is not
    something an updater should touch."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: False)

    def must_not_be_called(*_a, **_k):
        raise AssertionError("checked for updates from a source run")

    monkeypatch.setattr(bootstrap.update, "check", must_not_be_called)
    assert bootstrap.maybe_update([]) is True


def test_no_update_flag_skips_the_check(monkeypatch):
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)

    def must_not_be_called(*_a, **_k):
        raise AssertionError("checked despite --no-update")

    monkeypatch.setattr(bootstrap.update, "check", must_not_be_called)
    assert bootstrap.maybe_update(["app.exe", "--no-update"]) is True


def test_nothing_newer_means_a_silent_normal_start(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    recorder = Recorder(found=None)
    install_update_double(monkeypatch, recorder)

    assert bootstrap.maybe_update(["app.exe"]) is True
    assert "version" not in capsys.readouterr().out.lower()


def test_the_offer_shows_both_versions(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder)

    bootstrap.maybe_update(["app.exe"], ask=lambda _: "n", current="1.0.0")

    printed = capsys.readouterr().out
    assert "1.0.0" in printed
    assert "1.1.0" in printed


def test_declining_starts_the_current_version(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder)

    assert bootstrap.maybe_update(["app.exe"], ask=lambda _: "n", current="1.0.0") is True
    assert recorder.applied is False


def test_an_empty_answer_means_yes(monkeypatch, tmp_path):
    """The prompt reads [Y/n]; pressing enter must do what the capital says."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    monkeypatch.setattr(bootstrap.sys, "executable", str(tmp_path / "app.exe"))
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder)

    assert bootstrap.maybe_update(["app.exe"], ask=lambda _: "", current="1.0.0") is False
    assert recorder.applied is True
    assert recorder.relaunched is True


def test_the_relaunch_skips_its_own_update_check(monkeypatch, tmp_path):
    """`--no-update` ahead of the forwarded arguments, or a release whose tag
    disagrees with the __version__ in its asset updates, relaunches, finds
    itself out of date again, and never reaches any work. CI's tag check
    guards the producer; this is the consumer's guard."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    monkeypatch.setattr(bootstrap.sys, "executable", str(tmp_path / "app.exe"))
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder)

    assert bootstrap.maybe_update(["app.exe", "--grace-report"],
                                  ask=lambda _: "y", current="1.0.0") is False
    assert recorder.relaunch_args == ["--no-update", "--grace-report"]


def test_a_failed_relaunch_never_claims_it_is_restarting(monkeypatch, tmp_path, capsys):
    """apply() succeeded, so the .exe on disk is already the new one. Saying
    "restarting" and then falling through to "update check skipped" is two
    contradictory lines followed by the old in-memory code running against a
    new binary."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    monkeypatch.setattr(bootstrap.sys, "executable", str(tmp_path / "app.exe"))
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder)

    def no_console(*_a, **_k):
        raise OSError("cannot spawn a console")

    monkeypatch.setattr(bootstrap.update, "relaunch", no_console)

    assert bootstrap.maybe_update(["app.exe"], ask=lambda _: "y",
                                  current="1.0.0") is False
    assert recorder.applied is True

    printed = capsys.readouterr().out.lower()
    assert "restart to use 1.1.0" in printed
    assert "restarting" not in printed
    assert "update check skipped" not in printed


def test_the_update_session_ignores_the_ambient_environment(monkeypatch):
    """HTTPS_PROXY and REQUESTS_CA_BUNDLE reroute the one path that downloads
    a binary and then executes it — and the asset and its SHA256SUMS.txt ride
    the same session, so whoever owns the channel owns both and the checksum
    anchors nothing."""
    session = bootstrap._new_session()
    try:
        assert session.trust_env is False
    finally:
        session.close()


class SpySession:
    def __init__(self):
        self.closed = False
        self.trust_env = False

    def close(self):
        self.closed = True


def test_a_caller_supplied_session_is_never_closed(monkeypatch, tmp_path):
    """The injected-session seam: a session the callee did not build is one it
    does not own."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    recorder = Recorder(found=None)
    install_update_double(monkeypatch, recorder)

    session = SpySession()
    assert bootstrap.maybe_update(["app.exe"], session=session, current="1.0.0") is True
    assert session.closed is False


def test_the_session_it_builds_itself_is_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    recorder = Recorder(found=None)
    install_update_double(monkeypatch, recorder)

    session = SpySession()
    monkeypatch.setattr(bootstrap, "_new_session", lambda: session)

    assert bootstrap.maybe_update(["app.exe"], current="1.0.0") is True
    assert session.closed is True


def test_the_session_is_closed_even_when_the_check_explodes(monkeypatch, tmp_path):
    """The failure that leaks a socket every launch is the one that happens on
    a bad day, not the one that happens on a good one."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(bootstrap.update, "check", boom)
    session = SpySession()
    monkeypatch.setattr(bootstrap, "_new_session", lambda: session)

    assert bootstrap.maybe_update(["app.exe"], current="1.0.0") is True
    assert session.closed is True


def test_a_checksum_mismatch_refuses_to_install(monkeypatch, tmp_path, capsys):
    """The one thing verification exists for."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder, sha_ok=False)

    assert bootstrap.maybe_update(["app.exe"], ask=lambda _: "y", current="1.0.0") is True
    assert recorder.applied is False
    assert "checksum" in capsys.readouterr().out.lower()


def test_a_read_only_folder_is_reported_and_never_downloaded(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder, writable=False)

    def must_not_be_called(*_a, **_k):
        raise AssertionError("downloaded into a folder it cannot write")

    monkeypatch.setattr(bootstrap.update, "download", must_not_be_called)

    assert bootstrap.maybe_update(["app.exe"], ask=lambda _: "y", current="1.0.0") is True
    assert recorder.applied is False
    assert "program files" in capsys.readouterr().out.lower()


def test_any_exception_in_the_update_path_still_starts_the_tool(monkeypatch, tmp_path):
    """A daemon that survives a dibycap outage must survive a GitHub one."""
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(bootstrap.update, "check", boom)
    assert bootstrap.maybe_update(["app.exe"], current="1.0.0") is True


def test_a_rollback_failure_is_reported_with_its_own_message_not_a_generic_warning(
        monkeypatch, tmp_path, capsys):
    """RollbackFailed must be caught before the generic Exception clause, or
    its critical message — the backup path and the recovery instruction —
    degrades into a bare 'update check skipped' warning. The clause order is
    load-bearing, which is why it is pinned rather than left to review.

    Pinning "no 'update check skipped' in the output" is what makes this test
    fail if the two except clauses are swapped: the backup path alone would
    still appear either way, since the generic handler also stringifies the
    exception. It is the *absence* of the generic prefix that proves the
    RollbackFailed clause — not the fallback — is the one that ran.
    """
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: True)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.paths, "updates_dir", lambda: tmp_path / "updates")
    recorder = Recorder(found=FOUND)
    install_update_double(monkeypatch, recorder)

    backup = tmp_path / "app.exe.old"

    def fake_apply(_new, _current):
        raise update.RollbackFailed(
            f"Update failed and rollback failed. The good binary is at {backup}. "
            f"Rename it to app.exe.")

    monkeypatch.setattr(bootstrap.update, "apply", fake_apply)

    assert bootstrap.maybe_update(["app.exe"], ask=lambda _: "y", current="1.0.0") is True

    printed = capsys.readouterr().out
    assert str(backup) in printed
    assert "update check skipped" not in printed.lower()


def test_prepare_stops_without_reaching_the_config_when_maybe_update_relaunches(monkeypatch):
    """That bool is the entire reason maybe_update returns a value, and
    prepare is its only production caller."""
    monkeypatch.setattr(bootstrap, "maybe_update", lambda *_a, **_k: False)

    def must_not_be_called():
        raise AssertionError("prepare kept going after maybe_update said stop")

    monkeypatch.setattr(bootstrap.paths, "config_file", must_not_be_called)

    assert bootstrap.prepare(["app.exe"]) is False


def test_prepare_sweeps_stale_backups_before_anything_else(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.OVERRIDE_ENV, str(tmp_path))
    monkeypatch.setattr(bootstrap.paths, "frozen", lambda: False)
    monkeypatch.setattr(bootstrap.paths, "exe_dir", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    swept = []
    monkeypatch.setattr(bootstrap.update, "sweep",
                        lambda folder, _name: swept.append(folder))

    def must_not_be_called(_):
        raise AssertionError("the wizard ran with a config already present")

    assert bootstrap.prepare([], ask=must_not_be_called) is True
    assert swept == [tmp_path]
