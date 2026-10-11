"""A project file from someone else's repository: confinement and injection.

Every path stays inside the project unless the user allows a directory; every value that
reaches a tool script is refused at load time if a script could read it as syntax, and
quoted when the script is written."""

import dataclasses
import os
from pathlib import Path

import pytest
import yaml

from chipleap.rtl import commercial, quote
from chipleap.rtl import synth as synth_mod
from chipleap.rtl.pnr import sdc
from chipleap.rtl.project import (
    ALLOW_ENV,
    ProjectError,
    active_low_by_name,
    load_project,
    parse_filelist,
)

BASE = "top: counter\nsources: [rtl/counter.sv]\n"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory next to the project, with a file a project must not read."""
    d = tmp_path / "secrets"
    d.mkdir()
    (d / "id_rsa.sv").write_text("-----BEGIN KEY-----\n")
    (d / "inc").mkdir()
    (d / "more.f").write_text("id_rsa.sv\n")
    return d


def project(root: Path, text: str) -> Path:
    (root / "project.yaml").write_text(text)
    return root


def refused(root: Path, text: str, *messages: str) -> str:
    project(root, text)
    with pytest.raises(ProjectError) as exc:
        load_project(root)
    for m in messages:
        assert m in str(exc.value)
    return str(exc.value)


@pytest.mark.parametrize(
    "source",
    ["{out}/id_rsa.sv", "../secrets/id_rsa.sv", "rtl/../../secrets/id_rsa.sv"],
)
def test_paths_outside_the_project_are_refused(counter_project, outside, source):
    msg = refused(
        counter_project,
        f"top: counter\nsources: [rtl/counter.sv, {source.format(out=outside)}]\n",
        "outside the project directory",
        "--allow-path",
    )
    assert "BEGIN KEY" not in msg


def test_refusal_does_not_say_whether_the_file_exists(counter_project, outside):
    a = refused(counter_project, f"{BASE}golden: {outside}/id_rsa.sv\n")
    b = refused(counter_project, f"{BASE}golden: {outside}/nothing.py\n")
    assert "outside the project directory" in a and "outside the project directory" in b
    assert "not a file" not in a + b


def test_symlinks_out_of_the_project_are_refused(counter_project, outside):
    (counter_project / "rtl/key.sv").symlink_to(outside / "id_rsa.sv")
    refused(counter_project, "top: counter\nsources: [rtl/counter.sv, rtl/key.sv]\n", "outside")
    (counter_project / "inc").symlink_to(outside / "inc", target_is_directory=True)
    refused(counter_project, f"{BASE}include_dirs: [inc]\n", "include_dirs", "outside")


def test_globs_cannot_leave_the_project(counter_project, outside):
    for pattern in ("../secrets/*.sv", f"{outside}/*.sv", "rtl/../../secrets/*.sv"):
        refused(
            counter_project,
            f"top: counter\nsources: ['{pattern}']\n",
            "a pattern stays inside the project",
        )
    # A symlinked file or directory that leads out is never followed, so a glob lists
    # only the project's own files, whatever is outside.
    (counter_project / "rtl/key.sv").symlink_to(outside / "id_rsa.sv")
    (counter_project / "rtl/ext").symlink_to(outside, target_is_directory=True)
    p = load_project(project(counter_project, "top: counter\nsources: ['rtl/**/*.sv']\n"))
    assert [p.rel(x) for x in p.sources] == ["rtl/counter.sv"]
    text = "top: counter\nsources: ['rtl/ext/*.sv']\n"
    with_file = refused(counter_project, text, "outside the project directory")
    (outside / "id_rsa.sv").unlink()
    assert refused(counter_project, text) == with_file


def test_paths_compare_by_component(counter_project, tmp_path):
    evil = tmp_path / "counter-evil"
    evil.mkdir()
    (evil / "x.sv").write_text("module x; endmodule\n")
    refused(counter_project, f"{BASE}golden: ../counter-evil/x.sv\n", "outside")
    refused(counter_project, f"top: counter\nsources: [{evil}/x.sv]\n", "outside")


def test_paths_are_resolved_once_and_kept(counter_project):
    (counter_project / "rtl/alias.sv").symlink_to(counter_project / "rtl/counter.sv")
    p = load_project(project(counter_project, "top: counter\nsources: [rtl/alias.sv]\n"))
    assert p.sources == ((counter_project / "rtl/counter.sv").resolve(),)


def test_includes_are_followed_and_confined(counter_project, outside):
    rtl = counter_project / "rtl"
    (rtl / "inc").mkdir()
    (rtl / "inc/defs.svh").write_text('`include "more.svh"\n')
    (rtl / "inc/more.svh").write_text("`define ONE 1\n")
    src = rtl / "counter.sv"
    body = src.read_text()
    with_inc = f"{BASE}include_dirs: [rtl/inc]\n"
    src.write_text('`include "defs.svh"\n' + body)
    assert load_project(project(counter_project, with_inc)).top == "counter"
    for line, message in (
        (f'`include "{outside}/id_rsa.sv"', "absolute paths and .. are not allowed"),
        ('`include "../../secrets/id_rsa.sv"', "absolute paths and .. are not allowed"),
        ("`include `SECRET_FILE", "cannot be checked"),
        ('`include "$HOME/x.vh"', "is not a plain file name"),
    ):
        src.write_text(line + "\n" + body)
        refused(counter_project, with_inc, message)
    # Through a symlink in an include directory, and from a file that is itself included.
    (rtl / "inc/leak.svh").symlink_to(outside / "id_rsa.sv")
    src.write_text('`include "leak.svh"\n' + body)
    refused(counter_project, with_inc, '`include "leak.svh" leads outside')
    src.write_text('`include "defs.svh"\n' + body)
    (rtl / "inc/more.svh").write_text('`include "leak.svh"\n')
    refused(counter_project, with_inc, "rtl/inc/more.svh", "leads outside")
    # Commented out, it is not read.
    (rtl / "inc/more.svh").write_text('// `include "leak.svh"\n/* `include "leak.svh" */\n')
    assert load_project(project(counter_project, with_inc)).top == "counter"


def test_only_the_user_can_allow_directories(counter_project, outside, monkeypatch):
    import inspect

    from chipleap import mcp_server
    from chipleap.rtl.project import allowed_roots

    for tool in (
        "describe_project",
        "lint_rtl",
        "simulate_rtl",
        "coverage_rtl",
        "synthesize_rtl",
        "verify_rtl",
    ):
        params = inspect.signature(getattr(mcp_server, tool)).parameters
        assert not {"allow", "allow_path", "allowed"} & set(params), tool
    monkeypatch.chdir(outside.parent)
    monkeypatch.setenv(ALLOW_ENV, "secrets")
    assert allowed_roots() == (outside.resolve(),)  # relative entries resolve once, here


def test_filelists_cannot_leave_the_project(counter_project, outside):
    rtl = counter_project / "rtl"
    for body, where in (
        (f"-f {outside}/more.f\n", "outside"),
        ("-F ../../secrets/more.f\n", "outside"),
        (f"+incdir+{outside}/inc\n", "outside"),
        (f"-I{outside}/inc\n", "outside"),
        (f"counter.sv\n{outside}/id_rsa.sv\n", "outside"),
    ):
        (rtl / "files.f").write_text(body)
        refused(counter_project, "top: counter\nfilelist: rtl/files.f\n", where)
    refused(counter_project, f"top: counter\nfilelist: {outside}/more.f\n", "outside")
    with pytest.raises(ProjectError, match="outside"):
        (rtl / "files.f").write_text("../../secrets/id_rsa.sv\n")
        parse_filelist(rtl / "files.f")


def test_no_environment_or_home_expansion(counter_project, monkeypatch):
    monkeypatch.setenv("SECRET_DIR", "/etc")
    for text in ("$SECRET_DIR/passwd.sv", "${SECRET_DIR}/x.sv", "$HOME/x.sv", "~/x.sv"):
        msg = refused(counter_project, f"top: counter\nsources: ['{text}']\n", "are not expanded")
        assert "/etc" not in msg and str(Path.home()) not in msg


def test_a_user_can_allow_a_directory(counter_project, outside, monkeypatch):
    text = f"top: counter\nsources: [rtl/counter.sv, {outside}/id_rsa.sv]\n"
    project(counter_project, text)
    with pytest.raises(ProjectError):
        load_project(counter_project)
    p = load_project(counter_project, allow=[outside])
    assert p.sources[-1] == (outside / "id_rsa.sv").resolve()
    assert p.allowed == (outside.resolve(),)
    monkeypatch.setenv(ALLOW_ENV, f"/nonexistent{os.pathsep}{outside}")
    assert load_project(counter_project).sources[-1].name == "id_rsa.sv"
    # The project file has no key that widens it.
    refused(counter_project, f"{text}allow: [{outside}]\n", "unknown top-level keys: allow")


def test_staging_and_hashing_touch_only_the_projects_files(counter_project, outside, tmp_path):
    inc = counter_project / "rtl/inc"
    inc.mkdir()
    (inc / "defs.svh").write_text("`define ONE 1\n")
    (inc / "leak.svh").symlink_to(outside / "id_rsa.sv")
    (counter_project / "test/leak.py").symlink_to(outside / "id_rsa.sv")
    project(counter_project, f"{BASE}include_dirs: [rtl/inc]\ntests: {{dir: test}}\n")
    p = load_project(counter_project)
    names = {p.rel(f) for f in p.input_files()}
    assert "rtl/inc/defs.svh" in names and not any("leak" in n for n in names)
    dest = tmp_path / "staged"
    p.stage(inc, dest / "inc")
    p.stage(p.tests.dir, dest / "test")
    staged = {f.relative_to(dest).as_posix() for f in dest.rglob("*") if f.is_file()}
    assert staged == {"inc/defs.svh", "test/test_counter.py"}
    with pytest.raises(ProjectError):
        p.stage(outside / "id_rsa.sv", dest / "x")


def test_build_outputs_in_an_include_dir_are_not_inputs(counter_project):
    project(counter_project, f"{BASE}include_dirs: [.]\n")
    p = load_project(counter_project)
    before = p.inputs_digest()
    (counter_project / "build/synth").mkdir(parents=True)
    (counter_project / "build/synth/counter.synth.v").write_text("module counter; endmodule\n")
    assert p.inputs_digest() == before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"parameters": {"W": "1\n!touch /tmp/chipleap-pwned"}}, "must be an integer"),
        ({"defines": {"X": "1; !touch /tmp/chipleap-pwned"}}, "is not a plain value"),
        ({"defines": {"X": "-y/etc"}}, "is not a plain value"),
        ({"defines": {"-y": None}}, "must be a Verilog identifier"),
        ({"defines": {"X": "[exec id]"}}, "is not a plain value"),
        ({"top": "x; shell"}, "top must be a Verilog identifier"),
        ({"top": "x\n!touch /tmp/chipleap-pwned"}, "top must be a Verilog identifier"),
        ({"clocks": [{"port": "[exec id]", "period_ns": 10}]}, "must be a Verilog identifier"),
        ({"clocks": [{"port": "clk$x", "period_ns": 10}]}, "must be a Verilog identifier"),
        ({"resets": [{"port": "rst;id"}]}, "must be a Verilog identifier"),
        ({"pnr": {"librelane": {"FP_IO_MODE": "[exec id]"}}}, "pnr.librelane.FP_IO_MODE"),
        ({"pnr": {"librelane": {"X": ["ok", "$env(HOME)"]}}}, "pnr.librelane.X"),
        ({"tests": {"dir": "test", "toplevel": "a b"}}, "tests.toplevel must be"),
    ],
)
def test_values_that_scripts_would_run_are_refused(counter_project, change, message):
    data = {"top": "counter", "sources": ["rtl/counter.sv"], **change}
    refused(counter_project, yaml.safe_dump(data), message)
    assert not Path("/tmp/chipleap-pwned").exists()


@pytest.mark.parametrize(
    "name", ["bad\nname.sv", "a;b.sv", "a[1].sv", "a$b.sv", "a{b}.sv", 'a"b.sv', "-y.sv"]
)
def test_file_names_that_scripts_would_read_are_refused(counter_project, name):
    (counter_project / "rtl" / name).write_text("module x; endmodule\n")
    refused(counter_project, yaml.safe_dump({"top": "counter", "sources": [f"rtl/{name}"]}))


def test_scripts_quote_and_refuse_as_a_second_layer(counter_project, tmp_path):
    p = load_project(counter_project)
    for bad in ("x; shell", "x\n!touch /tmp/chipleap-pwned", "[exec id]", "a$b", 'a"b'):
        with pytest.raises(quote.QuoteError):
            quote.tcl(bad)
        with pytest.raises(quote.QuoteError):
            quote.yosys(bad)
    assert quote.tcl("rtl/a b.sv") == "{rtl/a b.sv}" and quote.yosys("a b") == '"a b"'
    evil = dataclasses.replace(p, top="x; exec id")
    with pytest.raises(quote.QuoteError):
        commercial.genus_script(evil, ["src/rtl/counter.sv"], [], "lib.lib", "syn")
    with pytest.raises(quote.QuoteError):
        synth_mod._script(evil, list(p.sources), True, Path("lib.lib"), tmp_path, 5.0)
    clock = dataclasses.replace(p.clocks[0], port="[exec id]")
    with pytest.raises(quote.QuoteError):
        sdc(dataclasses.replace(p, clocks=(clock,)))
    with pytest.raises(quote.QuoteError):
        commercial.dc_script(dataclasses.replace(p, clocks=(clock,)), [], [], "l.db", "syn")


@pytest.mark.parametrize(
    ("port", "low"),
    [
        ("rst_n", True),
        ("RST_N", True),
        ("rst_ni", True),
        ("resetn", True),
        ("ARESETN", True),
        ("rst_b", True),
        ("rst_in", False),
        ("rst", False),
        ("reset_i", False),
    ],
)
def test_reset_polarity_from_the_name(port, low):
    assert active_low_by_name(port) is low


def test_librelane_configuration_is_checked_again(counter_project, tmp_path):
    from chipleap.rtl.pnr import config
    from chipleap.verify.pnr import PnrError

    p = load_project(counter_project)
    evil = dataclasses.replace(
        p, pnr=dataclasses.replace(p.pnr, librelane={"FP_IO_MODE": "[exec id]"})
    )
    with pytest.raises(PnrError, match="LibreLane configuration"):
        config(evil, tmp_path)
