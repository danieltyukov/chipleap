"""A project file from someone else's repository: confinement and injection.

Every path stays inside the project unless the user allows a directory; every value that
reaches a tool script is refused at load time if a script could read it as syntax, and
quoted when the script is written."""

import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from chipleap.rtl import commercial, confine, quote
from chipleap.rtl import synth as synth_mod
from chipleap.rtl.lint import lint
from chipleap.rtl.pnr import sdc
from chipleap.rtl.project import (
    ALLOW_ENV,
    ProjectError,
    active_low_by_name,
    load_project,
    parse_filelist,
)
from chipleap.verify.tools import verilator, yosys

needs_landlock = pytest.mark.skipif(not confine.enabled(), reason="Landlock is not available")

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


def test_an_include_after_a_comment_in_a_string_is_still_found(counter_project, outside):
    # The tools read "/*" in a string as text, so the `include after it is real.
    src = counter_project / "rtl/counter.sv"
    body = src.read_text()
    for trick in (
        f'initial $display("/*");\n`include "{outside}/id_rsa.sv"\n// */\n',
        f'wire \\a" ;\n`include "{outside}/id_rsa.sv" // "\n',
        f'string s = """\n/*\n""";\n`include "{outside}/id_rsa.sv" // */\n',
        f'initial $display("`include \\"{outside}/id_rsa.sv\\"");\n',
    ):
        src.write_text(trick + body)
        msg = refused(counter_project, BASE, "rtl/counter.sv")
        assert "BEGIN KEY" not in msg


def test_every_place_an_include_could_come_from_is_followed(counter_project, outside):
    # Beside the source "defs.svh" is harmless; the one in the include directory, which
    # some tools find first, includes a file outside.
    rtl = counter_project / "rtl"
    (rtl / "defs.svh").write_text("`define ONE 1\n")
    (rtl / "inc").mkdir()
    (rtl / "inc/defs.svh").write_text(f'`include "{outside}/id_rsa.sv"\n')
    src = rtl / "counter.sv"
    src.write_text('`include "defs.svh"\n' + src.read_text())
    refused(counter_project, f"{BASE}include_dirs: [rtl/inc]\n", "rtl/inc/defs.svh:1")


def test_waiver_files_are_checked_too(counter_project, outside):
    # Verilator preprocesses its configuration files, so a waiver can `include.
    (counter_project / "rtl/waive.vlt").write_text(
        f'`verilator_config\n`include "{outside}/id_rsa.sv"\n'
    )
    refused(counter_project, f"{BASE}lint: {{waivers: [rtl/waive.vlt]}}\n", "rtl/waive.vlt:2")


@pytest.mark.parametrize("name", ["\\x2e\\x2e/secrets/id_rsa.sv", "inc\\056.svh", "a#b.svh"])
def test_include_names_with_escapes_are_refused(counter_project, name):
    src = counter_project / "rtl/counter.sv"
    src.write_text(f'`include "{name}"\n' + src.read_text())
    refused(counter_project, BASE, "is not a plain file name")


def test_memory_files_named_in_the_rtl_stay_inside(counter_project, outside):
    src = counter_project / "rtl/counter.sv"
    body = src.read_text()
    (counter_project / "rtl/rom.hex").write_text("00\n")
    mem = "logic [7:0] m [0:1];\n"
    for call in (
        f'initial $readmemh("{outside}/id_rsa.sv", m);',
        'initial $readmemb ( /* x */ "../secrets/id_rsa.sv", m);',
        'initial $readmemh("\\056\\056/secrets/id_rsa.sv", m);',
    ):
        src.write_text(body.replace("endmodule", mem + call + "\nendmodule"))
        refused(counter_project, BASE, "$readmem file")
    for call in ('initial $readmemh("rtl/rom.hex", m);', "initial $readmemh(FILE, m);"):
        src.write_text(body.replace("endmodule", mem + call + "\nendmodule"))
        assert load_project(project(counter_project, BASE)).top == "counter"


def test_the_project_file_stays_in_its_directory(counter_project, outside):
    secret = outside / "settings.yaml"
    secret.write_text("token: [not closed\nkey: SECRET-VALUE\n")
    (counter_project / "project.yaml").unlink()
    (counter_project / "project.yaml").symlink_to(secret)
    with pytest.raises(ProjectError, match="leads outside its directory") as exc:
        load_project(counter_project)
    assert "SECRET" not in str(exc.value)
    # A YAML error names the place, not the text there.
    with pytest.raises(ProjectError, match="not valid YAML at line") as exc:
        load_project(secret)
    assert "SECRET" not in str(exc.value) and "not closed" not in str(exc.value)
    (outside / "credentials").write_text("[default]\nkey = SECRET-VALUE\n")
    with pytest.raises(ProjectError, match="a project is a directory") as exc:
        load_project(outside / "credentials")
    assert "SECRET" not in str(exc.value)


def test_a_symlink_swapped_in_after_the_check_is_not_read(counter_project, outside, monkeypatch):
    p = load_project(counter_project)
    digest = p.inputs_digest()
    (outside / "counter.sv").write_text("-----BEGIN KEY-----\n")
    rtl = counter_project / "rtl"

    def swap() -> None:
        # The checked path stays the same, but a directory on it now leads outside.
        rtl.rename(counter_project / "rtl.orig")
        rtl.symlink_to(outside, target_is_directory=True)

    def unswap() -> None:
        rtl.unlink()
        (counter_project / "rtl.orig").rename(rtl)

    swap()  # before the read: resolving the path finds it
    with pytest.raises(ProjectError, match="outside the project directory"):
        p.read(p.sources[0])
    with pytest.raises(ProjectError, match="outside the project directory"):
        p.stage(p.sources[0], counter_project / "staged.sv")
    assert not (counter_project / "staged.sv").exists()
    changed = p.inputs_digest()
    (outside / "counter.sv").write_text("other\n")
    assert p.inputs_digest() == changed != digest  # counted missing, never read
    unswap()
    if not Path("/proc/self/fd").is_dir():
        return  # only Linux says where an open file is
    # Between the check and the open: the open file itself is checked.
    real_open = os.open

    def open_after_swap(path, *args, **kwargs):
        if Path(path) == p.sources[0]:
            swap()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_after_swap)
    with pytest.raises(ProjectError, match="outside the project directory"):
        p.read(p.sources[0])


def test_glob_brackets_are_characters(counter_project):
    (counter_project / "rtl/a(b.sv").write_text("module ab; endmodule\n")
    p = load_project(project(counter_project, "top: counter\nsources: ['rtl/[(c]*.sv']\n"))
    assert [x.name for x in p.sources] == ["counter.sv"]
    refused(counter_project, "top: counter\nsources: ['rtl/[\\\\]*.sv']\n", "matches no files")


def test_init_does_not_look_into_a_directory_outside(counter_project, outside):
    from chipleap.rtl.project import init_project

    (outside / "test_leak.py").write_text("")
    (counter_project / "project.yaml").unlink()
    (counter_project / "test").rename(counter_project / "spec")
    (counter_project / "tests").symlink_to(outside, target_is_directory=True)
    _, notes = init_project(counter_project, top="counter")
    assert not any("tests" in n for n in notes)


@needs_landlock
def test_a_confined_tool_reads_only_what_it_is_allowed(counter_project, outside):
    cat = shutil.which("cat")
    assert cat
    for path, readable in (
        (counter_project / "rtl/counter.sv", True),
        (outside / "id_rsa.sv", False),
    ):
        with confine.confined([cat, str(path)], [counter_project]) as (argv, env):
            out = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
        assert (out.returncode == 0) is readable, out.stderr
        assert "BEGIN KEY" not in out.stdout
    # It has a private temporary directory, removed afterwards.
    script = 'echo x > "$TMPDIR/f" && cat "$TMPDIR/f"'
    with confine.confined(["sh", "-c", script], []) as (argv, env):
        out = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
        tmp = Path(env["TMPDIR"])
    assert out.stdout == "x\n" and not tmp.exists()


def test_confinement_can_be_turned_off_and_leaves_containers_alone(tmp_path, monkeypatch):
    wrapper = tmp_path / "yosys-ll"
    wrapper.write_text('#!/bin/sh\nexec docker run --rm yosys "$@"\n')
    with confine.confined([str(wrapper), "-V"], []) as (argv, _):
        assert argv == [str(wrapper), "-V"]
    monkeypatch.setenv(confine.ENV, "0")
    with confine.confined(["cat", "x"], []) as (argv, _):
        assert argv == ["cat", "x"]


def test_a_tool_reads_its_installation_but_not_a_shared_prefix(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    tools = {
        ".local/bin/sv2v": [
            ".local/bin",
            *(f".local/{d}/sv2v" for d in ("share", "lib", "libexec")),
        ],
        ".local/opt/slang-9/bin/slang": [".local/opt/slang-9/bin", ".local/opt/slang-9"],
        "yosys": ["yosys"],
    }
    for rel, allowed in tools.items():
        exe = home / rel
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("")
        assert confine.installation(exe) == [(home / a).resolve() for a in allowed], rel


@needs_landlock
@pytest.mark.skipif(not verilator().found, reason="Verilator not installed")
def test_lint_cannot_show_a_file_that_a_macro_includes(counter_project, outside):
    # No reading of the text finds this `include: Verilator builds it from the macro.
    # Read, the file's text would come back in a message about an unknown macro.
    (outside / "id_rsa.sv").write_text("`AKIA_SECRET_TOKEN\n")
    src = counter_project / "rtl/counter.sv"
    src.write_text(f'`define INC(x) `x\n`INC(include) "{outside}/id_rsa.sv"\n' + src.read_text())
    r = lint(load_project(counter_project))
    assert r.status == "fail"
    assert "AKIA" not in json.dumps(r.data) + r.summary


@needs_landlock
@pytest.mark.synth
@pytest.mark.skipif(
    not yosys().found or synth_mod.liberty_for("ihp-sg13g2") is None,
    reason="needs Yosys and the IHP SG13G2 liberty",
)
def test_synthesis_cannot_read_a_memory_file_a_parameter_names(counter_project, outside):
    (outside / "rom.hex").write_text("a5\n")
    src = counter_project / "rtl/counter.sv"
    mem = f'  localparam F = "{outside}/rom.hex";\n  logic [7:0] m [0:0];\n'
    src.write_text(
        src.read_text().replace("endmodule", mem + "  initial $readmemh(F, m);\nendmodule")
    )
    r = synth_mod.synth(load_project(counter_project))
    assert r.status == "fail" and "Can not open file" in r.summary, r.summary


def test_a_script_tool_can_read_its_interpreter(tmp_path, monkeypatch):
    # sby and eqy are Python scripts: confined, they still need the Python that runs them.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    py = home / "tools/py312/bin/python3"
    py.parent.mkdir(parents=True)
    py.write_text("")
    py.chmod(0o755)
    monkeypatch.setenv("PATH", f"{py.parent}{os.pathsep}{os.environ['PATH']}")
    sby = home / ".local/opt/yosyshq/bin/sby"
    sby.parent.mkdir(parents=True)
    sby.write_text("#!/usr/bin/env python3\nprint('sby')\n")
    allowed = confine.installation(sby)
    assert (home / ".local/opt/yosyshq").resolve() in allowed
    assert (home / "tools/py312").resolve() in allowed
    direct = home / ".local/opt/eqy/bin/eqy"
    direct.parent.mkdir(parents=True)
    direct.write_text(f"#!{py}\n")
    assert (home / "tools/py312").resolve() in confine.installation(direct)
