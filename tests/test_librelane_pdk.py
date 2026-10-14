"""The PDK LibreLane runs on (chipleap.verify.librelane_pdk): the checkout's own commit,
installed with ciel on first use under a lock, checked, never another commit."""

from __future__ import annotations

import fcntl
import json
import threading
import time
from pathlib import Path

import pytest

from chip_fakes import fake_pdk
from chipleap.verify import librelane_pdk as lp
from chipleap.verify import tools

COMMIT = "22f2a25" + "0" * 33
OTHER = "c4b8b4e" + "0" * 33


def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit: str = COMMIT) -> Path:
    """A git checkout of the PDK at ``commit``, with the files an install is checked by."""
    pdk = fake_pdk(tmp_path / "src")
    git = pdk.parent / ".git"
    git.mkdir()
    (git / "HEAD").write_text(commit + "\n")
    for rel in lp.CHECKED:
        if not (pdk / rel).is_file():
            (pdk / rel).parent.mkdir(parents=True, exist_ok=True)
            (pdk / rel).write_text(f"{rel} at {commit}\n")
    monkeypatch.setenv("CHIPLEAP_IHP_PDK", str(pdk))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(lp.ENV, raising=False)
    monkeypatch.setenv(lp.CIEL_ENV, "/usr/bin/ciel")  # never run: run_ciel is replaced
    return pdk


def fake_ciel(source: Path, calls: list[list[str]], commit: str | None = None, delay: float = 0):
    """ciel as it installs: the version under ciel/<family>/versions/<commit>, linked from
    the root, its files copied from ``source``."""

    def run(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        time.sleep(delay)
        root = Path(cmd[cmd.index("--pdk-root") + 1])
        version = commit or cmd[-1]
        tree = root / "ciel" / "ihp-sg13g2" / "versions" / version / "ihp-sg13g2"
        for rel in lp.CHECKED:
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes((source / rel).read_bytes())
        (root / "ihp-sg13g2").symlink_to(tree.relative_to(root))
        return 0, "Version 22f2a25 enabled for the ihp-sg13g2 PDK.\n"

    return run


def test_first_use_installs_the_checkouts_commit_and_checks_it(tmp_path, monkeypatch):
    pdk = checkout(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(lp, "run_ciel", fake_ciel(pdk, calls))
    target = tmp_path / "data" / "chipleap" / "librelane-pdk" / COMMIT
    assert lp.root() == target and not lp.installed(target)
    assert lp.ensure() == target
    assert calls[0][:2] == ["/usr/bin/ciel", "enable"] and calls[0][-1] == COMMIT
    assert lp.installed(target) and lp.commit_of(target / "ihp-sg13g2") == COMMIT
    assert json.loads((target / lp.MARKER).read_text())["commit"] == COMMIT
    # Installed into a temporary directory and renamed into place: nothing else is left.
    assert sorted(p.name for p in target.parent.iterdir()) == [".lock", COMMIT]
    # The next run finds it; the wrapper mounts the directory and trusts what is in it.
    assert lp.ensure() == target and len(calls) == 1
    assert lp.args_for(target) == ["--pdk", "ihp-sg13g2", "--manual-pdk", "--pdk-root", str(target)]
    assert tools.librelane_env()["LIBRELANE_PDK_ROOT"] == str(target.parent)
    assert target.parent.resolve() in tools.pdk_roots()


def test_an_offline_install_is_an_error_with_the_command_to_run(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch)
    target = lp.managed_root(COMMIT)
    for failure in (
        lambda cmd: (1, "[ERROR] Failed to download ihp-sg13g2 22f2a25: network unreachable\n"),
        lambda cmd: (_ for _ in ()).throw(OSError("docker: not found")),
    ):
        monkeypatch.setattr(lp, "run_ciel", failure)
        with pytest.raises(lp.LibrelanePdkError) as err:
            lp.ensure()
        assert f"ciel enable --pdk-root {target} --pdk-family ihp-sg13g2 {COMMIT}" in str(err.value)
        assert "about 800 MB" in str(err.value)
        # Nothing half-installed stays, and nothing falls back to another commit.
        assert sorted(p.name for p in target.parent.iterdir()) == [".lock"]


def test_two_runs_install_once(tmp_path, monkeypatch):
    pdk = checkout(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(lp, "run_ciel", fake_ciel(pdk, calls, delay=0.5))
    roots: list[Path] = []
    runs = [threading.Thread(target=lambda: roots.append(lp.ensure())) for _ in range(2)]
    for t in runs:
        t.start()
    for t in runs:
        t.join()
    assert len(calls) == 1 and roots == [lp.managed_root(COMMIT)] * 2


def test_a_run_waits_for_the_lock_no_longer_than_its_limit(tmp_path):
    lock = tmp_path / ".lock"
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(lp.LibrelanePdkError, match="another ChipLeap"), lp._locked(lock, 0.2):
            pass
        fcntl.flock(held, fcntl.LOCK_UN)
    with lp._locked(lock, timeout=0.2):
        pass


def test_an_install_of_another_commit_or_other_files_is_refused(tmp_path, monkeypatch):
    pdk = checkout(tmp_path, monkeypatch)
    target = lp.managed_root(COMMIT)
    monkeypatch.setattr(lp, "run_ciel", fake_ciel(pdk, [], commit=OTHER))
    with pytest.raises(lp.LibrelanePdkError, match="is IHP-Open-PDK c4b8b4e, not 22f2a25"):
        lp.ensure()
    assert not target.exists()
    # The right commit, but a file that is not the checkout's.
    altered = tmp_path / "altered"
    for rel in lp.CHECKED:
        (altered / rel).parent.mkdir(parents=True, exist_ok=True)
        (altered / rel).write_bytes((pdk / rel).read_bytes())
    (altered / "libs.tech/librelane/config.tcl").write_text("set ::env(OTHER) 1\n")
    monkeypatch.setattr(lp, "run_ciel", fake_ciel(altered, []))
    with pytest.raises(lp.LibrelanePdkError, match=r"config\.tcl of the PDK installed .* differs"):
        lp.ensure()
    assert not target.exists()


def test_without_a_checkout_the_pinned_commit_is_checked_against_chipleaps_record(
    tmp_path, monkeypatch
):
    pdk = checkout(tmp_path, monkeypatch, lp.PINNED_COMMIT)
    monkeypatch.setattr(lp, "checkout", lambda: (None, ""))
    assert lp.wanted_commit() == lp.PINNED_COMMIT
    monkeypatch.setattr(lp, "run_ciel", fake_ciel(pdk, []))
    # The fake files are not IHP's, so ChipLeap's record of them refuses the install.
    with pytest.raises(lp.LibrelanePdkError, match="differs from ChipLeap's record of 22f2a25"):
        lp.ensure()
    record = {rel: lp._sha256(pdk / rel) for rel in lp.CHECKED}
    monkeypatch.setattr(lp, "CHECKED", record)
    assert lp.installed(lp.ensure())


def test_a_checkout_of_no_known_commit_names_the_setting(tmp_path, monkeypatch):
    pdk = checkout(tmp_path, monkeypatch)
    (pdk.parent / ".git" / "HEAD").unlink()
    with pytest.raises(lp.LibrelanePdkError, match=lp.ENV):
        lp.ensure()


def test_a_pdk_root_of_your_own_is_used_as_it_is(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch)
    own = tmp_path / "own"
    version = own / "ciel/ihp-sg13g2/versions" / OTHER / "ihp-sg13g2"
    version.mkdir(parents=True)
    (own / "ihp-sg13g2").symlink_to(version.relative_to(own))
    monkeypatch.setenv(lp.ENV, str(own))
    monkeypatch.setattr(lp, "run_ciel", lambda cmd: pytest.fail("no install for a given root"))
    assert lp.ensure() == own and lp.mount_root() == own
    assert tools.librelane_env()["LIBRELANE_PDK_ROOT"] == str(own)
    assert own.resolve() in tools.pdk_roots()
    # A setting that names no PDK fails instead of falling back to another.
    (tmp_path / "empty").mkdir()
    for bad, reason in (("relative", "relative path"), (str(tmp_path / "empty"), "holds no")):
        monkeypatch.setenv(lp.ENV, bad)
        with pytest.raises(ValueError, match=reason):
            lp.ensure()


def test_a_run_on_another_pdk_commit_fails(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "librelane", lambda: tools.Tool("librelane", "/x", "3.0.14", ""))
    roots = {}
    for commit in (COMMIT, OTHER):
        root = tmp_path / commit[:7]
        version = root / "ciel/ihp-sg13g2/versions" / commit / "ihp-sg13g2"
        version.mkdir(parents=True)
        (root / "ihp-sg13g2").symlink_to(version.relative_to(root))
        roots[commit] = root
    run = tmp_path / "run"
    run.mkdir()
    resolved = {"meta": {"librelane_version": "3.0.14"}, "PDK_ROOT": str(roots[OTHER])}
    (run / "resolved.json").write_text(json.dumps(resolved))
    with pytest.raises(ValueError, match=r"ran on IHP-Open-PDK c4b8b4e .*, not 22f2a25"):
        tools.librelane_ran(run, roots[COMMIT])
    resolved["PDK_ROOT"] = str(roots[COMMIT])
    (run / "resolved.json").write_text(json.dumps(resolved))
    assert tools.librelane_ran(run, roots[COMMIT]) == "3.0.14"
