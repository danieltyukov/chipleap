from pathlib import Path

import pytest

from chipleap.explore import explore, pareto_front
from chipleap.spec import SpecError, load_spec, parse_spec

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def spec(**over):
    base = {
        "name": "t",
        "workload": {"llm": {"model": "smollm2-135m", "context": 256, "weights": "int4"}},
        "targets": {"throughput": 2},
        "constraints": {"tech": ["ihp-sg13g2"]},
    }
    base.update(over)
    return parse_spec(base)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"workload": {"llm": {"model": "smollm2-135m"}}, "colour": 1}, "unknown top-level"),
        ({"workload": {}}, "exactly one"),
        ({"workload": {"llm": {"model": "nope"}}}, "unknown model"),
        ({"workload": {"llm": {"model": "smollm2-135m", "speed": 1}}}, "unknown keys"),
        (
            {"workload": {"llm": {"model": "smollm2-135m"}}, "targets": {"throughput": -1}},
            "positive",
        ),
        (
            {"workload": {"llm": {"model": "smollm2-135m"}}, "constraints": {"tech": ["7nm"]}},
            "not one of",
        ),
        ({"workload": {"llm": {"model": "smollm2-135m"}}, "objective": "cost"}, "objective"),
        ({"workload": {"llm": {"model": "smollm2-135m"}}, "start": {"banks": 99}}, "start"),
    ],
)
def test_spec_errors(data, match):
    with pytest.raises(SpecError, match=match):
        parse_spec(data)


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.yaml")))
def test_examples_load(path):
    load_spec(path).workload()


def test_explore_is_deterministic():
    s = spec()
    a = explore(s, samples=200, seed=4)
    b = explore(s, samples=200, seed=4)
    assert a.best.design == b.best.design
    assert [st.reason for st in a.steps] == [st.reason for st in b.steps]


def test_agent_steps_improve():
    r = explore(spec(), samples=0)
    keys = [s.before.key for s in r.steps] + [r.steps[-1].before.key]
    assert keys == sorted(keys) or keys[0] > keys[-1]
    assert r.steps[-1].chosen is None  # the loop stops by itself


def test_open_pdk_design_for_small_model():
    r = explore(spec(), samples=300)
    best = r.best
    assert best.assessment.meets
    assert best.design.tech == "ihp-sg13g2"
    assert best.design.ext_mem in ("hyperram", "octal-psram")
    assert best.design.accel is not None


def test_1b_model_needs_lpddr():
    r = explore(load_spec(EXAMPLES / "llama-3.2-1b-decode.yaml"), samples=300)
    assert r.best.assessment.meets
    assert r.best.design.ext_mem.startswith("lpddr")


def test_pareto_front_is_non_dominated():
    r = explore(spec(targets={}), samples=400)
    front = pareto_front(r.cloud)
    for a in front:
        for b in front:
            ea, eb = a.evaluation, b.evaluation
            assert not (
                eb.throughput >= ea.throughput
                and eb.energy_per_unit_mj <= ea.energy_per_unit_mj
                and eb.total_area <= ea.total_area
                and (eb.throughput, eb.energy_per_unit_mj, eb.total_area)
                != (ea.throughput, ea.energy_per_unit_mj, ea.total_area)
            )


def test_precision_is_a_decision():
    s = spec(
        allow={"weights": ["int8", "int4"]},
        targets={"throughput": 3},
        constraints={"tech": ["ihp-sg13g2"]},
    )
    r = explore(s, samples=200)
    assert r.best.point.weights == "int4"


def test_infeasible_spec_says_why():
    s = spec(targets={"throughput": 500})
    r = explore(s, samples=100)
    assert not r.best.assessment.meets
    assert r.best.assessment.misses


def test_spec_names_are_safe(tmp_path):
    with pytest.raises(SpecError, match="name"):
        parse_spec({"name": "../x", "workload": {"llm": {"model": "smollm2-135m"}}})
    path = tmp_path / "my spec (v2).yaml"
    path.write_text("workload:\n  llm:\n    model: smollm2-135m\n")
    assert load_spec(path).name == "my-spec-v2"
