"""The generated configurations against X-HEEP's own generator (needs a checkout)."""

import pytest

from chipleap.gen.xheep import xheep_config
from chipleap.hw import Accel, Design
from chipleap.verify import xheep

pytestmark = [
    pytest.mark.xheep,
    pytest.mark.skipif(xheep.find_checkout() is None, reason="no X-HEEP checkout"),
]


@pytest.mark.parametrize(
    "design",
    [
        Design(bus="onetoM", accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)),
        Design(bus="NtoM", banks=2, il_banks=2, accel=Accel("ws", 4, 4, 8, ports=2)),
        Design(bus="onetoM", accel=None),
    ],
    ids=["onetoM", "NtoM-interleaved", "no-accel"],
)
def test_xheep_accepts_the_configuration(tmp_path, design):
    cfg = tmp_path / "cfg.py"
    cfg.write_text(xheep_config(design))
    checkout = xheep.find_checkout()
    assert checkout is not None
    r = xheep.validate(cfg, checkout)
    assert r.ok, r.log
