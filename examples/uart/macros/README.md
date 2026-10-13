# The UART chip's macros

`strongarm.macro` is a StrongARM latched comparator as a `macro/v1` package ([docs/macro-package.md](../../../docs/macro-package.md)), laid out, extracted, simulated and checked by aispice 0.2.0, built from its `master` branch at commit `43e25f7` (after pull request 43, which added macro export), on IHP SG13G2 at PDK commit `22f2a25`. The chip section in `../project.yaml` puts its inputs on two analog pads and its output on the UART's receive line ([docs/chips.md](../../../docs/chips.md)).

It was made from this netlist with `aispice layout generate comparator.cir --template comparator`, then `aispice macro export comparator.cir --layout comparator.gds --name strongarm --pins '{"clk": {"kind": "clock"}, "outp": {"kind": "signal"}, "outn": {"kind": "signal"}}'`:

```spice
* StrongARM comparator
VDD vdd 0 1.2
VCLK clk 0 PULSE(0 1.2 1n 50p 50p 2n 4n)
VP inp 0 0.61
VM inm 0 0.59
XM0 s clk 0 0 sg13_lv_nmos w=4u l=0.13u
XM1 xp inp s 0 sg13_lv_nmos w=4u l=0.13u
XM2 xn inm s 0 sg13_lv_nmos w=4u l=0.13u
XM3 outn outp xp 0 sg13_lv_nmos w=2u l=0.13u
XM4 outp outn xn 0 sg13_lv_nmos w=2u l=0.13u
XM5 outn outp vdd vdd sg13_lv_pmos w=2u l=0.13u
XM6 outp outn vdd vdd sg13_lv_pmos w=2u l=0.13u
XM7 outn clk vdd vdd sg13_lv_pmos w=1u l=0.13u
XM8 outp clk vdd vdd sg13_lv_pmos w=1u l=0.13u
XM9 xp clk vdd vdd sg13_lv_pmos w=1u l=0.13u
XM10 xn clk vdd vdd sg13_lv_pmos w=1u l=0.13u
CP outp 0 5f
CN outn 0 5f
.tran 10p 8n
.end
```

with the specs `decision = value_at(V(outp,outn), 2.9n) >= 0.8` and `outn_low = value_at(V(outn), 2.9n) <= 0.3`. To make it again, run the same two commands in an aispice project whose PDK is a git checkout of IHP-Open-PDK (aispice records its commit in the package) and replace the folder; `chipleap macro check strongarm.macro --pdk ihp-sg13g2` must pass. Exports of the same netlist differ only in the GDS's date records, the order of the extracted capacitors and the last digits of the simulated values.
