"""The implementation steps against a hostile project: one that ships its own tools and
Python packages hoping a step runs them, that points ``build/`` or a step's directory
elsewhere, or that plants results for a step to reuse."""

import json
import os
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from chipleap.rtl import dft as D
from chipleap.rtl import eco as E
from chipleap.rtl import irdrop as IR
from chipleap.rtl import openroad as orr
from chipleap.rtl import power as P
from chipleap.rtl import upf as U
from chipleap.rtl.netlist import Chain
from chipleap.rtl.pnr import config as pnr_config
from chipleap.rtl.pnr import seal_routed
from chipleap.rtl.project import EcoChange, load_project
from chipleap.rtl.provenance import file_digest, record, seal
from chipleap.rtl.verify import run_step
from chipleap.verify import launch, tools
from chipleap.verify.pnr import PnrError

# What a project might ship, hoping a step finds it on a relative or empty PATH entry.
PLANTED = ("vvp", "iverilog", "fault", "nl2bench", "quaigh", "openroad", "yosys", "docker")
PLANTED_PY = ("python", "python3")
# What a trusted tool runs by name, as Fault runs vvp and a wrapper runs docker.
PROBE = """\
[ -n "$CHIPLEAP_PROBED" ] && exit 1
export CHIPLEAP_PROBED=1
for c in {names}; do "$c" --version >/dev/null 2>&1; done
python3 -c 'import chipleap_planted' >/dev/null 2>&1
python3 -c pass >/dev/null 2>&1
exit 1
"""
SCAN_NETLIST = """\
module counter(clk_i, rst_ni, en_i, q_o);
  input clk_i; input rst_ni; input en_i; output [1:0] q_o;
  sg13g2_dfrbpq_1 q0 (.CLK(clk_i), .RESET_B(rst_ni), .D(en_i), .Q(q_o[0]));
  sg13g2_dfrbpq_1 q1 (.CLK(clk_i), .RESET_B(rst_ni), .D(q_o[0]), .Q(q_o[1]));
endmodule
"""


def executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def hostile(counter_project, tmp_path, monkeypatch, ihp_pdk):
    """The counter project shipping a planted copy of every tool and a Python package
    (``sitecustomize`` runs in any Python that has it on its path), each writing to
    ``sentinel`` when run. PATH and PYTHONPATH hold empty, relative and project entries
    ahead of the trusted ones, and ChipLeap runs from inside the project. The trusted
    tools live outside it and run the planted names, as Fault runs vvp."""
    sentinel = tmp_path / "SENTINEL"
    root = counter_project
    for where in (root, root / "bin", root / "build"):
        for name in (*PLANTED, *PLANTED_PY):
            executable(where / name, f'echo "{where}/{name}" >> "{sentinel}"\n')
    note = f'open({str(sentinel)!r}, "a").write(__file__ + "\\n")\n'
    (root / "sitecustomize.py").write_text(note)
    (root / "chipleap_planted").mkdir()
    (root / "chipleap_planted/__init__.py").write_text(note)

    trusted = tmp_path / "trusted"
    probe = PROBE.format(names=" ".join(PLANTED))
    version13 = 'case "$1" in -V) echo "Icarus Verilog version 13.0 (stable)";; esac\n'
    for name in ("fault", "nl2bench", "quaigh", "openroad"):
        executable(trusted / name, probe)
    executable(trusted / "gl/iverilog", version13 + probe)
    executable(trusted / "gl/vvp", probe)
    for var, exe in {
        "CHIPLEAP_FAULT": trusted / "fault",
        "CHIPLEAP_QUAIGH": trusted / "quaigh",
        "CHIPLEAP_OPENROAD": trusted / "openroad",
        "CHIPLEAP_GL_IVERILOG": trusted / "gl/iverilog",
    }.items():
        monkeypatch.setenv(var, str(exe))
    relative = ["", ".", "bin", "build", "..", "../..", "../../..", "../../../.."]
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([*relative, str(root / "bin"), str(trusted), "/usr/bin", "/bin"]),
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([*relative, str(root)]))
    monkeypatch.setenv("LD_LIBRARY_PATH", os.pathsep.join(["", ".", str(root)]))
    monkeypatch.chdir(root)
    return SimpleNamespace(root=root, sentinel=sentinel, trusted=trusted)


def test_the_fixture_is_hostile(hostile):
    """Run the way a step ran its tools before (the inherited environment), a trusted
    tool runs the project's copies: the tests below would see it."""
    subprocess.run([str(hostile.trusted / "fault")], cwd=hostile.root / "build", check=False)
    assert hostile.sentinel.exists()


def test_tools_are_found_outside_the_project(hostile, monkeypatch):
    with tools.untrusted(hostile.root):
        for name in (*PLANTED, *PLANTED_PY):
            found = tools.which(name)
            assert found is None or not Path(found).resolve().is_relative_to(hostile.root)
        assert tools.fault().path == str(hostile.trusted / "fault")
        assert tools.gl_vvp(tools.gl_iverilog()) == str(hostile.trusted / "gl/vvp")
        # A tool variable may not name a relative path or a file in the project.
        monkeypatch.setenv("CHIPLEAP_FAULT", "bin/fault")
        assert not tools.fault().found
        monkeypatch.setenv("CHIPLEAP_FAULT", str(hostile.root / "bin/fault"))
        assert not tools.fault().found
        monkeypatch.setenv("CHIPLEAP_FAULT", "fault")  # a bare name, on the trusted path
        assert tools.fault().path == str(hostile.trusted / "fault")
        tools.all_tools()
    assert not hostile.sentinel.exists()


def test_tool_environments_hold_only_trusted_entries(hostile):
    with tools.untrusted(hostile.root):
        envs = [
            tools.clean_env(),
            tools.fault_env(str(hostile.trusted / "fault")),
            tools.fault_env(executable(hostile.trusted / "pyfault", "")),
        ]
        work = hostile.root / "build/w"
        work.mkdir(parents=True)
        launch.run(["/usr/bin/env"], work, work / "env.log", 30)
        logged = dict(
            line.split("=", 1)
            for line in (work / "env.log").read_text().splitlines()
            if "=" in line
        )
        envs.append(logged)
    for env in envs:
        for var in ("PATH", *tools.LIBRARY_PATHS):
            for entry in env.get(var, "").split(os.pathsep) if var in env else []:
                assert os.path.isabs(entry), (var, entry)
                if not entry.endswith(".chipleap-bin"):  # the run's own docker shim
                    assert not Path(entry).resolve().is_relative_to(hostile.root), (var, entry)
    assert envs[1]["PATH"].split(os.pathsep)[:2] == [
        str(hostile.trusted / "gl"),
        str(hostile.trusted),
    ]
    assert not hostile.sentinel.exists()


def test_scan_insertion_runs_nothing_from_the_project(hostile, monkeypatch):
    netlist = hostile.root.parent / "synth.v"
    netlist.write_text(SCAN_NETLIST)
    monkeypatch.setattr(D, "synthesize", lambda project, work: SimpleNamespace(netlist=netlist))
    proj = hostile.root / "project.yaml"
    proj.write_text(proj.read_text() + "dft: {}\n")
    r = run_step(load_project(hostile.root), "dft")
    assert r.status == "fail" and "OpenROAD scan insertion failed" in r.summary, r.summary
    assert not hostile.sentinel.exists()


def test_atpg_and_chain_simulation_run_nothing_from_the_project(hostile):
    p = load_project(hostile.root)
    work = p.work_dir("dft")
    scan = recorded_scan(work)
    views = orr.pdk_views(p)
    with tools.untrusted(p.root):
        with pytest.raises(D.DftError, match="fault cut failed"):
            D.run_atpg(p, scan, views, work)
        with pytest.raises(D.DftError, match="Icarus could not compile"):
            D.simulate_patterns(p, scan, [], views, work)
    assert not hostile.sentinel.exists()


def recorded_scan(work, text=SCAN_NETLIST):
    """A scan insertion's result as insert_scan leaves it: both netlists written and
    recorded in the manifest beside them."""
    netlist = work / "counter.scan.v"
    netlist.write_text(text)
    sealed = record(work, D.SCAN_MANIFEST, "scan", {netlist.name: file_digest(netlist)})
    chain = Chain("chain_0", "en_i", "q_o[1]", ("q0", "q1"))
    return D.ScanResult(netlist, netlist, (chain,), {}, "", 2, (), {}, None, sealed)


def test_gate_level_tests_run_nothing_from_the_project(hostile):
    """The power step runs the project's tests (its own code, as the sim step does) but no
    tool, and no Python package, from elsewhere in the project."""
    pytest.importorskip("cocotb_tools")
    p = load_project(hostile.root)
    netlist = hostile.root.parent / "counter.nl.v"
    netlist.write_text(SCAN_NETLIST)
    with tools.untrusted(p.root):
        run = P.gate_level_run(p, netlist, orr.pdk_views(p), p.work_dir("power"), False)
    assert run.vcd is None
    assert not hostile.sentinel.exists()


# Symlinked build directories: every implementation step refuses, and nothing outside the
# project is deleted or written.


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    for d in ("", "pnr", "dft", "power", "ir_drop", "upf", "paths"):
        (h / d).mkdir(parents=True, exist_ok=True)
        (h / d / "keep.txt").write_text("precious\n")
    return h


def tree(d):
    return {
        p.relative_to(d).as_posix(): p.read_text() if p.is_file() else "/"
        for p in sorted(d.rglob("*"))
    }


SECTIONS = """\
dft: {}
upf:
  domains:
    - {name: PD_TOP}
"""
# Each step, how it is run, and the directory in build/ it writes.
STEPS = {
    "dft": (lambda p: D.dft(p), "dft"),
    "power": (lambda p: P.power(p), "power"),
    "ir_drop": (lambda p: IR.ir_drop(p), "ir_drop"),
    "upf": (lambda p: U.upf(p), "upf"),
    "eco": (lambda p: E.eco(p, [EcoChange("resize", "_1_", "sg13g2_inv_2")]), "pnr"),
}


def project_with_sections(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + SECTIONS)
    return counter_project


@pytest.mark.parametrize("name", STEPS)
@pytest.mark.parametrize("whole", [True, False], ids=["build", "build-step"])
def test_every_step_refuses_a_symlinked_build_directory(
    counter_project, home, ihp_pdk, name, whole
):
    run, step_dir = STEPS[name]
    root = project_with_sections(counter_project)
    at = "build" if whole else f"build/{step_dir}"
    (root / at).parent.mkdir(exist_ok=True)
    (root / at).symlink_to(home / ("" if whole else step_dir))
    before = tree(home)
    r = run(load_project(root))
    assert r.status == "fail" and f"{at} is a symlink" in r.summary, r.summary
    assert tree(home) == before


@pytest.mark.parametrize("whole", [True, False], ids=["build", "build-paths"])
def test_timing_paths_refuse_a_symlinked_build_directory(counter_project, home, whole):
    at = "build" if whole else "build/paths"
    (counter_project / at).parent.mkdir(exist_ok=True)
    (counter_project / at).symlink_to(home / ("" if whole else "paths"))
    before = tree(home)
    with pytest.raises(ValueError, match=f"{at} is a symlink"):
        E.timing_paths(load_project(counter_project))
    assert tree(home) == before


@pytest.mark.parametrize("name", ["dft", "power", "ir_drop", "upf"])
def test_a_symlink_inside_a_step_directory_is_removed_not_followed(
    counter_project, home, ihp_pdk, name
):
    run, step_dir = STEPS[name]
    root = project_with_sections(counter_project)
    (root / "build" / step_dir).mkdir(parents=True)
    planted = root / "build" / step_dir / "gl"
    planted.symlink_to(home / step_dir)
    before = tree(home)
    run(load_project(root))
    assert tree(home) == before and not planted.is_symlink()


@pytest.mark.parametrize("name", ["power", "ir_drop", "eco"])
def test_a_routed_run_reached_through_a_symlink_is_refused(
    counter_project, ihp_pdk, tmp_path, name
):
    """build/pnr pointing at another project's routed run, recorded by ChipLeap on this
    machine, is still not this project's."""
    other = tmp_path / "other/build/pnr"
    final = other / "runs/chipleap/final"
    final.mkdir(parents=True)
    (final / "metrics.json").write_text("{}")
    seal_routed(other)
    root = project_with_sections(counter_project)
    (root / "build").mkdir()
    (root / "build/pnr").symlink_to(other)
    r = STEPS[name][0](load_project(root))
    assert r.status == "fail" and "build/pnr is a symlink" in r.summary, r.summary


# Results a step reuses must be ChipLeap's.


def planted_run(root):
    """A routed run shipped with the repository: the files LibreLane would leave, and no
    manifest from this machine."""
    final = root / "build/pnr/runs/chipleap/final"
    for rel in ("nl/counter.nl.v", "def/counter.def", "sdc/counter.sdc"):
        (final / rel).parent.mkdir(parents=True, exist_ok=True)
        (final / rel).write_text("exec touch pwned\n")
    (final / "metrics.json").write_text("{}")


@pytest.mark.parametrize("name", ["power", "ir_drop"])
def test_a_planted_routed_run_is_not_reused(counter_project, ihp_pdk, name):
    planted_run(counter_project)
    r = STEPS[name][0](load_project(counter_project))
    assert r.status == "fail" and "ChipLeap did not make build/pnr" in r.summary, r.summary


def test_dft_does_not_simulate_a_planted_routed_netlist(counter_project, ihp_pdk):
    planted_run(counter_project)
    p = load_project(counter_project)
    scan = D.ScanResult(Path("x"), Path("x"), (), {}, "", 0, (), {})
    with pytest.raises(orr.OpenroadError, match="ChipLeap did not make build/pnr"):
        D.simulate_routed(p, scan, [], p.work_dir("dft"))


def test_a_planted_activity_file_is_not_reused(counter_project, ihp_pdk, monkeypatch):
    """IR drop reuses the power step's VCD only when ChipLeap recorded it for this netlist
    and these tests; otherwise it runs the tests again (here it cannot, so it has none)."""
    p = load_project(counter_project)
    netlist = counter_project / "net.v"
    netlist.write_text(SCAN_NETLIST)
    gl = counter_project / "build/power/gl"
    gl.mkdir(parents=True)
    (gl / P.VCD_NAME).write_text("$enddefinitions $end\n")
    (gl / P.ACTIVITY_MARKER).write_text(P._activity_key(p, "0" * 64))
    (gl / P.GL_MANIFEST).write_text(json.dumps({"format": "chipleap.manifest/v1", "mac": "0"}))
    ran = []

    def no_tests(*a, **k):
        ran.append(1)
        raise orr.OpenroadUnavailable("no Icarus 13 here")

    monkeypatch.setattr(P, "gate_level_run", no_tests)
    assert P.activity(p, netlist, orr.pdk_views(p), False) is None and ran
    # Recorded by ChipLeap, but for another netlist: run again.
    seal(gl, P.GL_MANIFEST, "gl", [P.VCD_NAME, P.ACTIVITY_MARKER])
    ran.clear()
    assert P.activity(p, netlist, orr.pdk_views(p), False) is None and ran
    # Recorded for this netlist: reused.
    (gl / P.ACTIVITY_MARKER).write_text(P._activity_key(p, file_digest(netlist)))
    seal(gl, P.GL_MANIFEST, "gl", [P.VCD_NAME, P.ACTIVITY_MARKER])
    ran.clear()
    found = P.activity(p, netlist, orr.pdk_views(p), False)
    assert found is not None and found[0] == gl / P.VCD_NAME and not ran


def test_sections_are_quoted_into_upf(tmp_path):
    """Domain names and elements are identifiers by the time they reach a UPF file; the
    writer quotes them again and refuses what it cannot."""
    text = textwrap.dedent(
        """\
        schema: project/v1
        name: x
        top: x
        sources: [x.v]
        upf:
          domains:
            - {name: PD_TOP}
            - {name: "PD_B]; exec touch pwned; #", elements: [u_b]}
        """
    )
    (tmp_path / "x.v").write_text("module x; endmodule\n")
    (tmp_path / "project.yaml").write_text(text)
    with pytest.raises(ValueError, match="upf"):
        load_project(tmp_path)


# DFT reuses its own netlists only as it recorded them.


@pytest.fixture
def scan_tools(tmp_path, monkeypatch, ihp_pdk):
    """Fault, Quaigh and Icarus stand-ins that note each run in ``ran`` and fail."""
    ran = tmp_path / "ran"
    bin_dir = tmp_path / "tools"
    for name in ("fault", "nl2bench", "quaigh", "iverilog", "vvp"):
        # A version probe is not a run.
        body = 'case "$1" in -V) echo "Icarus Verilog version 13.0"; exit 0;; --version | --help) exit 0;; esac\n'
        executable(bin_dir / name, body + f'echo {name} >> "{ran}"\nexit 1\n')
    for var, name in (
        ("CHIPLEAP_FAULT", "fault"),
        ("CHIPLEAP_QUAIGH", "quaigh"),
        ("CHIPLEAP_GL_IVERILOG", "iverilog"),
    ):
        monkeypatch.setenv(var, str(bin_dir / name))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    return ran


@pytest.mark.parametrize(
    "tamper, why",
    [
        # The scan netlist swapped after it was recorded.
        (
            lambda scan: scan.netlist.write_text(SCAN_NETLIST.replace("q_o[0]", "planted")),
            "changed after ChipLeap recorded it",
        ),
        # The manifest deleted, as by a repository that ships its own build/dft.
        (lambda scan: (scan.netlist.parent / D.SCAN_MANIFEST).unlink(), "is missing"),
        # The manifest of another insertion (here, of the swapped netlist) put in its place.
        (
            lambda scan: recorded_scan(scan.netlist.parent, SCAN_NETLIST + "// other\n"),
            "made again",
        ),
        # The manifest edited to vouch for the swapped netlist.
        (
            lambda scan: (scan.netlist.parent / D.SCAN_MANIFEST).write_text(
                (scan.netlist.parent / D.SCAN_MANIFEST)
                .read_text()
                .replace(scan.sealed.files["counter.scan.v"], "0" * 64)
            ),
            "does not verify",
        ),
    ],
    ids=["swapped-netlist", "deleted-manifest", "stale-manifest", "edited-manifest"],
)
def test_dft_uses_its_netlists_only_as_recorded(
    counter_project, scan_tools, monkeypatch, tamper, why
):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "dft: {}\n")
    p = load_project(counter_project)
    work = p.work_dir("dft")
    scan = recorded_scan(work)
    tamper(scan)
    views = orr.pdk_views(p)
    monkeypatch.setattr(D, "insert_scan", lambda project, work: scan)
    (counter_project / "build/pnr").mkdir()
    for use in (
        lambda: D.run_atpg(p, scan, views, work),
        lambda: D.simulate_patterns(p, scan, [], views, work),
        # Place and route copies the scan netlist for LibreLane through the same manifest.
        lambda: pnr_config(p, counter_project / "build/pnr"),
    ):
        with pytest.raises((D.DftError, PnrError), match=why):
            use()
    assert not scan_tools.exists(), "a tool ran on a netlist that was not checked"
    assert not (counter_project / "build/pnr/src/counter.scan.v").exists()


def test_scan_insertion_records_the_bytes_it_used(counter_project, ihp_pdk, monkeypatch):
    """The synthesized netlist is read for its ports from the copy OpenROAD gets, checked
    against the hash taken while copying; a copy changed in between is refused."""
    netlist = counter_project / "synth.v"
    netlist.write_text(SCAN_NETLIST)
    monkeypatch.setattr(D, "synthesize", lambda project, work: SimpleNamespace(netlist=netlist))
    real_stage = orr.Job.stage

    def stage_then_swap(self, src, name=None, *, sealed=None):
        rel = real_stage(self, src, name, sealed=sealed)
        if src == netlist:
            (self.dir / rel).write_text(SCAN_NETLIST + "// swapped\n")
        return rel

    monkeypatch.setattr(orr.Job, "stage", stage_then_swap)
    with pytest.raises(D.DftError, match="changed after ChipLeap copied it"):
        D.insert_scan(load_project(counter_project))
