"""Commercial tool backends, remote execution and technologies of your own."""

import math
import shutil
import subprocess

import pytest

from chipleap.cli import main
from chipleap.hw import Accel, liberty, techlib
from chipleap.hw.tech import load_user_techs, tech_from_dict
from chipleap.verify import backends, csynth, hdlsim, innovus, remote
from chipleap.verify.sim import Case, default_cases
from chipleap.verify.tools import commercial, ihp_liberty

ACCEL = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)

STD_LIB = """
library (testlib) {
  time_unit : "1ns";
  leakage_power_unit : "1nW";
  capacitive_load_unit (1,pf);
  nom_voltage : 1.1;
  cell (ND2D2) {
    area : 1.5;
    cell_leakage_power : 3.0;
    pin (A1) { direction : input; }
    pin (A2) { direction : input; }
    pin (ZN) { direction : output; function : "!(A1&A2)"; }
  }
  cell (ND2D1) {
    area : 1.0;
    cell_leakage_power : 2.0;
    pin (A1) { direction : input; }
    pin (A2) { direction : input; }
    pin (ZN) { direction : output; function : "!(A1 A2)"; }
  }
  cell (NR2D1) {
    area : 1.0;
    pin (A1) { direction : input; }
    pin (A2) { direction : input; }
    pin (ZN) { direction : output; function : "!(A1|A2)"; }
  }
  cell (DFCNQD1) {
    area : 5.5;
    ff (IQ, IQN) { clocked_on : "CP"; next_state : "D"; clear : "!CDN"; }
    pin (D) { direction : input; }
    pin (CP) { direction : input; }
    pin (CDN) { direction : input; }
    pin (Q) { direction : output; function : "IQ"; }
  }
}
"""


def sram_lib(words: int, bits: int, area: float, read_pj: float) -> str:
    aw = int(math.log2(words))
    return f"""
library (sram_{words}x{bits}) {{
  leakage_power_unit : "1nW";
  capacitive_load_unit (1,pf);
  cell (RAM{words}x{bits}) {{
    area : {area};
    memory () {{ address_width : {aw}; word_width : {bits}; }}
    pin (CLK) {{
      direction : input;
      internal_power () {{
        when : "CEN & !WEN";
        rise_power (scalar) {{ values ("{read_pj}"); }}
      }}
      internal_power () {{
        when : "CEN & WEN";
        rise_power (scalar) {{ values ("{read_pj * 2}"); }}
      }}
    }}
  }}
}}
"""


# --- remote execution -------------------------------------------------------


def _remote_bash(line: str) -> str:
    """The bash script inside a wrapped remote command line, unquoted."""
    import shlex

    inner = shlex.split(line)[-1]  # the outer shell's -c argument
    return shlex.split(inner.split("exec bash -c ", 1)[1])[0]


def test_setup_shell_follows_the_script():
    assert remote.is_csh("source /eda/scripts/flexlm.csh")
    assert remote.is_csh("setenv CDS_LIC_FILE 1@x")
    assert not remote.is_csh("source /eda/scripts/flexlm.sh; export X=1")
    steps = [remote.Step(("xrun", "-f", "run.f"))]
    csh = remote.wrap(steps, "/tmp/w", "source a.csh")
    assert csh.startswith("tcsh -f -c ")
    assert "source a.csh; cd /tmp/w && exec bash -c " in csh
    assert _remote_bash(csh) == "xrun -f run.f"
    sh = remote.wrap([remote.Step(("genus", "-batch"), "g.out")], "/tmp/w x", "source a.sh;")
    assert sh.startswith("bash -c ")
    assert _remote_bash(sh) == "genus -batch > g.out 2>&1"
    assert remote.wrap(steps, "/tmp/w").startswith("bash -c ")


def test_step_arguments_are_quoted():
    s = remote.Step(("vsim", "-do", "run -all; quit -f", "$(rm -rf ~)"), "out log")
    assert s.shell() == "vsim -do 'run -all; quit -f' '$(rm -rf ~)' > 'out log' 2>&1"
    with pytest.raises(ValueError):
        remote.Step(("x",), "../escape.log")
    with pytest.raises(ValueError):
        remote.Step(())


@pytest.mark.parametrize(
    "host",
    ["-oProxyCommand=x", "a;b", "a b", "", "a\nb", "@host", "user@-x", "a$(id)", "x/y"],
)
def test_bad_hosts_are_refused(host, monkeypatch):
    with pytest.raises(remote.RemoteError, match="ssh alias or user@host"):
        remote.Remote(host)
    monkeypatch.setenv("CHIPLEAP_REMOTE", host or "-")
    with pytest.raises(remote.RemoteError):
        remote.Remote.from_env()
    with pytest.raises(techlib.TechError):
        techlib.read_text("/pdk/std.lib", host or "-x")


@pytest.mark.parametrize("host", ["eda-server", "user@eda.example.com", "my-host_1", "a.b"])
def test_good_hosts(host):
    assert remote.Remote(host).host == host


def test_which_reads_bash_and_csh_output():
    out = "@xrun\n/eda/x/bin/xrun\n@vcs\nvcs: Command not found.\n@genus\n/eda/g/bin/genus\n"
    assert remote.parse_which(out, ["xrun", "vcs", "genus"]) == {
        "xrun": "/eda/x/bin/xrun",
        "genus": "/eda/g/bin/genus",
    }
    with pytest.raises(remote.RemoteError, match="program name"):
        remote.Remote("server").which(["xrun; rm -rf ~"])


def _fake_ssh(calls, scratch="/tmp/chipleap-abcd1234"):
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        out = scratch + "\n" if "mktemp" in cmd[-1] else "ok\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    return fake_run


def test_remote_run_copies_runs_and_cleans_up(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(remote.subprocess, "run", _fake_ssh(calls))
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/rsync")
    r = remote.Remote("server", "source /tools/setup.csh")
    out = r.run(tmp_path, [remote.Step(("xrun", "-q", "tb.sv"))])
    assert out.stdout == "ok\n"
    ssh = [c for c in calls if c[0] == "ssh"]
    sync = [c for c in calls if c[0] == "rsync"]
    for c in ssh:
        assert c[c.index("--") + 1] == "server"  # the host always follows "--"
    assert "BatchMode=yes" in ssh[0]
    assert sync[0][-3:] == ["--", f"{tmp_path}/", "server:/tmp/chipleap-abcd1234/"]
    assert "tcsh -f -c" in ssh[1][-1] and _remote_bash(ssh[1][-1]) == "xrun -q tb.sv"
    assert sync[1][-3:] == ["--", "server:/tmp/chipleap-abcd1234/", f"{tmp_path}/"]
    assert ssh[-1][-1] == "rm -rf -- /tmp/chipleap-abcd1234"


@pytest.mark.parametrize("scratch", ["/tmp/chipleap-x; rm -rf ~", "/", "/home/me", ""])
def test_unexpected_scratch_directories_are_refused(tmp_path, monkeypatch, scratch):
    calls: list[list[str]] = []
    monkeypatch.setattr(remote.subprocess, "run", _fake_ssh(calls, scratch))
    with pytest.raises(remote.RemoteError, match="scratch"):
        remote.Remote("server").run(tmp_path, [remote.Step(("true",))])
    assert not any("rm" in c[-1] for c in calls)


def test_local_steps_run_without_a_shell(tmp_path):
    steps = [
        remote.Step(("echo", "$(touch pwned)"), "a.log"),
        remote.Step(("echo", "visible")),
    ]
    out = remote.run_steps(steps, tmp_path, None)
    assert out.returncode == 0 and out.stdout == "visible\n"
    assert (tmp_path / "a.log").read_text() == "$(touch pwned)\n"
    assert not (tmp_path / "pwned").exists()
    bad = remote.run_steps([remote.Step(("false",)), remote.Step(("echo", "x"))], tmp_path, None)
    assert bad.returncode != 0 and bad.stdout == ""


def test_remote_refuses_password_logins(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 255, "", "Permission denied (publickey).")

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    with pytest.raises(remote.RemoteError, match="with a key"):
        remote.Remote("server").which(["xrun"])


def test_remote_from_env(monkeypatch):
    monkeypatch.delenv("CHIPLEAP_REMOTE", raising=False)
    assert remote.Remote.from_env() is None
    monkeypatch.setenv("CHIPLEAP_REMOTE", "icd")
    monkeypatch.setenv("CHIPLEAP_REMOTE_SETUP", "source x.sh")
    assert remote.Remote.from_env() == remote.Remote("icd", "source x.sh")
    assert remote.Remote.from_env("other", "") == remote.Remote("other", "")


# --- simulation -------------------------------------------------------------


def test_testbench_parameters_follow_the_design():
    cfg = hdlsim.tb_config(ACCEL)
    assert "localparam int unsigned ROWS = 1;" in cfg
    assert "localparam int unsigned ABUF_WORDS = 512;" in cfg
    sv = hdlsim.testbench_sv()
    assert '`include "tb_config.svh"' in sv and "module tb_chipleap" in sv
    for key in ("xcelium", "vcs", "questa"):
        sim = hdlsim.SIMULATORS[key]
        assert any("tb_chipleap" in s.argv for s in sim.steps) and sim.commercial
        assert sim.steps[0].argv[0] in ("xrun", "vcs", "vlib")


def test_mcp_cannot_choose_where_or_how_tools_run(tmp_path):
    import inspect

    from chipleap import mcp_server

    params = set(inspect.signature(mcp_server.TOOLS["verify_soc"]).parameters)
    assert params == {"out_dir", "steps", "fetch_xheep", "sim_tool", "synth_tool", "synth_tech"}
    for name, fn in mcp_server.TOOLS.items():
        for p in inspect.signature(fn).parameters:
            assert not any(w in p for w in ("remote", "setup", "host", "script")), (name, p)
    with pytest.raises(mcp_server.ToolError, match="sim_tool"):
        mcp_server.verify_soc(str(tmp_path), sim_tool="xcelium; rm -rf ~")
    with pytest.raises(mcp_server.ToolError, match="synth_tech"):
        mcp_server.verify_soc(str(tmp_path), synth_tech="../../etc")

    server = mcp_server.build_server()
    try:
        from mcp import Client
    except ImportError:  # mcp 1.x has no in-process client
        return

    async def schemas() -> dict[str, set[str]]:
        async with Client(server) as c:
            tools = (await c.list_tools()).tools
            out = {}
            for t in tools:
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", {})
                out[t.name] = set(schema.get("properties", {}))
            return out

    import asyncio

    for name, props in asyncio.run(schemas()).items():
        assert not any(w in p for p in props for w in ("remote", "setup", "host")), name
    assert asyncio.run(schemas())["verify_soc"] == params


def test_simulator_output_with_a_transcript_prefix_parses():
    cases = [Case(1, 64, 3, 8, seed=1)]
    line = (
        '# {"m":1,"k":64,"n":3,"fmt":8,"stall":0,"latency":1,"tm":1,"tn":1,"tk":64,'
        '"x_resident":1,"cycles":104,"wall":106,"mismatches":0,"ok":true}'
    )
    (r,) = hdlsim.parse(ACCEL, cases, "noise\n" + line + "\n# done\n")
    assert r.ok and r.cycles == 104 and r.predicted == pytest.approx(104)
    with pytest.raises(hdlsim.SimError, match="0 results"):
        hdlsim.parse(ACCEL, cases, "# nothing here\n")


def test_unknown_simulator_is_named():
    with pytest.raises(hdlsim.SimError, match="xcelium"):
        hdlsim.get_simulator("modelsim-pe")


@pytest.mark.skipif(shutil.which("c++") is None, reason="no C++ compiler")
def test_stimulus_holds_driver_writes_and_golden_results(tmp_path):
    case = Case(1, 64, 3, 8, seed=7)
    hdlsim.prepare(ACCEL, [case], tmp_path)
    nums = (tmp_path / "stim.txt").read_text().split()
    head = [int(x, 16) for x in nums[:15]]
    m, k, n, fmt = head[:4]
    nw, nm, ne = head[11:14]
    assert (m, k, n, fmt) == (1, 64, 3, 8)
    assert nw == 11  # X, W, Y, M, K, N, TM, TK, FMT, XRES, CTRL
    assert nm == (64 + 3 * 64) // 4 and ne == 3
    assert len(nums) == 15 + 2 * (nw + nm + ne)
    for name in ("chipleap_accel.sv", "tb_chipleap.sv", "tb_config.svh"):
        assert (tmp_path / name).is_file()


def _verilator_timing() -> bool:
    exe = shutil.which("verilator")
    if not exe:
        return False
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
    return out.stdout.split()[1:2] >= ["5"] if out.stdout else False


@pytest.mark.rtl
@pytest.mark.skipif(not _verilator_timing(), reason="needs Verilator 5 with --timing")
def test_systemverilog_testbench_on_verilator(tmp_path):
    cases = default_cases(ACCEL, fmts=(8, 4))[:6]
    res = hdlsim.run(ACCEL, cases, "verilator-sv", tmp_path)
    assert all(r.ok for r in res)
    assert all(r.cycles == round(r.predicted) for r in res if r.case.stall_pct == 0)


@pytest.mark.commercial
@pytest.mark.parametrize("key", ["xcelium", "vcs", "questa"])
def test_commercial_simulator(tmp_path, key):
    if shutil.which(hdlsim.SIMULATORS[key].binary) is None:
        pytest.skip(f"{key} not on the path")
    res = hdlsim.run(ACCEL, default_cases(ACCEL)[:4], key, tmp_path)
    assert all(r.ok for r in res)


# --- synthesis --------------------------------------------------------------


def test_parameters_are_written_into_the_rtl():
    from chipleap.gen.rtl import accel_sv

    sv = csynth.specialize(accel_sv(), csynth.run_params(ACCEL, 8))
    assert "parameter int unsigned ROWS = 1" in sv
    assert "parameter int unsigned ABUF_WORDS = 8" in sv
    with pytest.raises(csynth.SynthError, match="NOPE"):
        csynth.specialize(accel_sv(), {"NOPE": 1})


def test_synthesis_scripts():
    g = csynth.genus_script("a.sv", "lib.lib", 10.0, "w8")
    assert "read_libs {lib.lib}" in g and "create_clock -name clk -period 10" in g
    assert "report_area > area_w8.rpt" in g
    d = csynth.dc_script("a.sv", "lib.db", 5.0, "w16")
    assert "set_app_var target_library {lib.db}" in d and "-period 5" in d
    assert "write_lib" in csynth.lc_script("x.lib", "x.db")


GENUS_AREA = """
  Instance          Module  Cell Count  Cell Area  Net Area  Total Area  Wireload
---------------------------------------------------------------------------------
chipleap_accel                   1234   5678.901     0.000    5678.901  <none> (D)
"""
GENUS_TIMING = "Path 1: VIOLATED (-1500 ps) Setup Check with Pin acc_reg/CLK->D\n"
DC_AREA = "Number of cells:  2000\nTotal cell area:   9876.5\nTotal area: 9876.5\n"
DC_TIMING = "  data arrival time   -8.2\n  slack (MET)   1.80\n"


def test_report_parsers():
    assert csynth.parse_area("genus", GENUS_AREA) == (5678.901, 1234)
    assert csynth.parse_slack("genus", GENUS_TIMING) == pytest.approx(-1.5)
    assert csynth.parse_area("dc", DC_AREA) == (9876.5, 2000)
    assert csynth.parse_slack("dc", DC_TIMING) == pytest.approx(1.8)
    with pytest.raises(csynth.SynthError):
        csynth.parse_area("genus", "nothing")


def test_commercial_tools_are_listed_without_starting_them():
    names = {t.name for t in commercial()}
    assert {"xrun", "vcs", "vsim", "genus", "dc_shell", "innovus"} <= names
    assert set(backends.SIM_TOOLS) >= {"verilator", "xcelium", "vcs", "questa"}
    assert set(backends.SYNTH_TOOLS) == {"yosys", "genus", "dc"}


def test_missing_tool_says_how_to_run_remotely(tmp_path, monkeypatch):
    monkeypatch.setattr(csynth.shutil, "which", lambda name: None)
    with pytest.raises(csynth.SynthError, match="--remote"):
        csynth.synthesize(ACCEL, "genus", "lib.lib", tmp_path)


def test_paths_in_tool_scripts_are_plain(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    server = remote.Remote("server")
    assert csynth.stage("/pdk/std.lib", work, server) == "/pdk/std.lib"
    with pytest.raises(csynth.SynthError, match="a path on the server"):
        csynth.stage("/pdk/x}; exec rm -rf ~", work, server)
    local = tmp_path / "my lib (v2).lib"
    local.write_text("library (x) {}")
    name = csynth.stage(str(local), work, None)
    assert name == "my_lib__v2_.lib" and (work / name).is_file()


def test_innovus_script():
    s = innovus.script("net.v", ("tech.lef", "cells.lef"), utilisation=0.5)
    assert "set init_lef_file [list tech.lef cells.lef]" in s
    assert "floorPlan -r 1.0 0.5" in s and "routeDesign" in s
    assert "streamOut" not in s.split("# streamOut")[0]
    with pytest.raises(ValueError):
        innovus.script("net.v", ())


@pytest.mark.commercial
def test_genus_synthesis(tmp_path):
    lib = ihp_liberty()
    if shutil.which("genus") is None or lib is None:
        pytest.skip("Genus or the IHP liberty not available")
    r = csynth.synthesize(ACCEL, "genus", str(lib), tmp_path)
    assert r.logic_um2 > 0 and r.delay_ns > 0


# --- technologies of your own ----------------------------------------------


def test_liberty_measurements():
    lib = liberty.parse(STD_LIB)
    assert lib.name == "testlib" and lib.nom_voltage == 1.1
    n = liberty.nand2(lib)
    assert n.name == "ND2D1" and n.area == 1.0 and n.leakage_w == pytest.approx(2e-9)
    f = liberty.flop(lib)
    assert f.name == "DFCNQD1" and f.async_reset


def test_foundry_style_names_and_state_leakage():
    text = """
library (foundry) {
  leakage_power_unit : "1pW";
  cell (ND2D0BWP) {
    area : 0.8;
    pin (A1) { direction : input; }
    pin (A2) { direction : input; }
    pin (ZN) { direction : output; function : "!(A1 & A2)"; }
  }
  cell (ND2D1BWP) {
    area : 1.06;
    leakage_power () { when : "A1 & A2"; value : 10.0; }
    leakage_power () { when : "!A1 & A2"; value : 30.0; }
    pin (A1) { direction : input; }
    pin (A2) { direction : input; }
    pin (ZN) { direction : output; function : "!(A1 & A2)"; }
  }
}
"""
    n = liberty.nand2(liberty.parse(text))
    # The drive-1 cell defines the gate equivalent even when a smaller D0 exists.
    assert n.name == "ND2D1BWP"
    assert n.leakage_w == pytest.approx(20e-12)


def test_sram_fit():
    cells = []
    for words, area, pj in ((256, 20000.0, 40.0), (1024, 50000.0, 70.0)):
        cells += liberty.parse(sram_lib(words, 64, area, pj)).cells.values()
    fit = liberty.sram_fit(cells)
    assert fit.mm2_per_kib == pytest.approx(0.005)
    assert fit.macro_mm2 == pytest.approx(0.01)
    assert fit.max_kib == 8 and fit.macros == 2
    assert fit.pj_per_byte_16kib > 40 / 8  # grows with size


def test_ihp_liberty_measures_like_the_builtin():
    path = ihp_liberty()
    if path is None:
        pytest.skip("IHP SG13G2 PDK not installed")
    n = liberty.nand2(liberty.parse(path.read_text()))
    assert n.name == "sg13g2_nand2_1" and n.area == pytest.approx(7.2576)


def test_tech_add_round_trip(tmp_path):
    std = tmp_path / "std.lib"
    std.write_text(STD_LIB)
    sram_dir = tmp_path / "sram"
    sram_dir.mkdir()
    (sram_dir / "a.lib").write_text(sram_lib(256, 64, 20000.0, 40.0))
    (sram_dir / "b.lib").write_text(sram_lib(1024, 64, 50000.0, 70.0))
    tech, basis = techlib.measure("mylib40", str(std), 40, sram=[str(sram_dir)])
    assert tech.ge_um2 == 1.0 and tech.vdd == 1.1 and tech.user
    assert tech.sram_mm2_per_kib == pytest.approx(0.005)
    assert 300 < tech.max_freq_mhz < 800 and "sram" in basis
    out = tmp_path / "techs"
    techlib.save(tech, out)
    with pytest.raises(techlib.TechError, match="exists"):
        techlib.save(tech, out)
    loaded = load_user_techs(out)
    assert loaded["mylib40"] == tech
    with pytest.raises(techlib.TechError, match="built-in"):
        techlib.measure("ihp-sg13g2", str(std), 130)
    with pytest.raises(ValueError, match="unknown technology fields"):
        tech_from_dict({"key": "x", "colour": "red"})


def test_tech_cli(tmp_path, monkeypatch, capsys):
    std = tmp_path / "std.lib"
    std.write_text(STD_LIB)
    monkeypatch.setenv("CHIPLEAP_TECH_DIR", str(tmp_path / "techs"))
    assert main(["tech", "add", "mylib", "--liberty", str(std), "--node", "28"]) == 0
    assert (tmp_path / "techs" / "mylib.yaml").is_file()
    assert "mylib" in capsys.readouterr().out
    assert main(["tech", "list"]) == 0
    assert main(["tech", "remove", "mylib"]) == 0
    assert not (tmp_path / "techs" / "mylib.yaml").exists()


def test_synthesis_needs_the_server_that_holds_the_library(tmp_path, monkeypatch):
    from chipleap.hw.design import Design
    from chipleap.hw.tech import TECHNOLOGIES, Technology

    std = tmp_path / "std.lib"
    std.write_text(STD_LIB)
    tech, _ = techlib.measure("srvlib", str(std), 40)
    remote_tech = Technology(**{**tech.__dict__, "host": "server", "liberty": "/pdk/std.lib"})
    monkeypatch.setitem(TECHNOLOGIES, "srvlib", remote_tech)
    with pytest.raises(csynth.SynthError, match="--remote server"):
        backends.synthesize_with(Design(accel=ACCEL), "genus", tmp_path, None, "srvlib")


def test_verify_cli_offers_the_commercial_tools():
    from chipleap.cli import build_parser

    args = build_parser().parse_args(
        ["verify", "out", "--sim-tool", "xcelium", "--synth-tool", "genus", "--remote", "srv"]
    )
    assert (args.sim_tool, args.synth_tool, args.remote) == ("xcelium", "genus", "srv")
