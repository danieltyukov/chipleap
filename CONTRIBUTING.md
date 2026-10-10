# Contributing

Thanks for helping with ChipLeap. This page covers the setup, how the code is organised, the checks to run and the ground rules.

## Setup

You need Python 3.10 or newer and git. Verilator 5, Yosys, sv2v and the IHP SG13G2 PDK are needed only for the RTL tests and `chipleap verify`; tests that need them skip themselves.

```sh
git clone https://github.com/danieltyukov/chipleap.git
cd chipleap
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`chipleap doctor` shows which tools were found.

## Layout

[docs/design.md](docs/design.md) describes the packages and the reasons behind the main decisions; [docs/model.md](docs/model.md) every number the model uses. In short: `workload` builds operators, `hw` describes technologies, memories, cores and design points, `model` evaluates them, `explore` searches, `gen` writes the bundle, `verify` checks it, `report` renders results, `mcp_server` and `cli` are the two front ends.

## Checks

Before opening a pull request:

```sh
ruff check src tests scripts
ruff format --check src tests scripts
mypy
pytest
```

- New behaviour has a test. Model changes need a test that pins the number they change.
- RTL changes keep `src/chipleap/model/accel.py` in step: `pytest tests/test_rtl.py` must pass with zero cycle error without memory stalls.
- Technology or memory numbers come with a source, in the code and in `docs/model.md`. Measured and scaled numbers are labelled as such.
- LLM presets come from the model's `config.json`, with the URL, and a published parameter count in `tests/test_workload.py`.
- Run `python scripts/build_site_data.py` if the change affects what the website shows.
- `CHANGELOG.md` has an entry under Unreleased for user-visible changes.

## Commits and pull requests

Commits follow the conventional commit format (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`, `ci:`), with a subject under 72 characters that describes the change. No emojis in code, comments, docs or commit messages.

Questions and ideas are welcome in GitHub issues.
