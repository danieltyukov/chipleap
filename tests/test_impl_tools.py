"""Finding the tools the implementation steps use: Icarus 13, Fault, Quaigh."""

import sys
import sysconfig

from chipleap.verify import tools


def fake(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


def test_gate_level_icarus_needs_13(tmp_path, monkeypatch):
    (tmp_path / "old").mkdir()
    old = fake(tmp_path / "old" / "iverilog", "#!/bin/sh\necho 'Icarus Verilog version 12.0'\n")
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    new = fake(new_dir / "iverilog", "#!/bin/sh\necho 'Icarus Verilog version 13.0 (stable)'\n")
    fake(new_dir / "vvp", "#!/bin/sh\n")
    monkeypatch.delenv("CHIPLEAP_GL_IVERILOG", raising=False)
    monkeypatch.setenv("PATH", str(old.parent))
    t = tools.gl_iverilog()
    assert not t.found and t.version == "12.0"
    monkeypatch.setenv("CHIPLEAP_GL_IVERILOG", str(new))
    t = tools.gl_iverilog()
    assert t.found and t.version == "13.0" and tools.gl_vvp(t) == str(new_dir / "vvp")


def test_fault_sees_its_own_packages(tmp_path, monkeypatch):
    script = fake(tmp_path / "fault", f"#!{sys.executable}\nprint('0.9.4')\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    env = tools.fault_env(str(script), {"PATH": "/usr/bin"})
    assert env["PYTHONPATH"].split(":")[0] == sysconfig.get_paths()["purelib"]
    assert env["PYTHONHOME"] == sys.base_prefix
    assert env["PATH"].endswith("/usr/bin")


def test_fault_uses_the_matching_vvp(tmp_path, monkeypatch):
    gl = tmp_path / "icarus"
    gl.mkdir()
    fake(gl / "iverilog", "#!/bin/sh\necho 'Icarus Verilog version 13.0'\n")
    fake(gl / "vvp", "#!/bin/sh\n")
    monkeypatch.setenv("CHIPLEAP_GL_IVERILOG", str(gl / "iverilog"))
    q = tmp_path / "q"
    q.mkdir()
    fake(q / "quaigh", "#!/bin/sh\necho 'quaigh 0.0.6'\n")
    monkeypatch.setenv("CHIPLEAP_QUAIGH", str(q / "quaigh"))
    env = tools.fault_env("/nonexistent/fault", {"PATH": "/usr/bin"})
    assert env["FAULT_IVERILOG"] == str(gl / "iverilog") and env["FAULT_VVP"] == str(gl / "vvp")
    assert env["PATH"].split(":")[:2] == [str(gl), str(q)]
    assert "PYTHONPATH" not in env  # not a Python script


def test_doctor_lists_them(monkeypatch):
    names = [t.name for t in tools.all_tools()]
    assert {"iverilog-gl", "fault", "quaigh", "openroad"} <= set(names)
