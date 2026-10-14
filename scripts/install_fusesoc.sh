#!/usr/bin/env bash
# Install the FuseSoC that X-HEEP's cores need, for place and route of the whole SoC
# (`chipleap verify DIR --steps soc`). X-HEEP pins its own forks of FuseSoC and Edalize:
# the release FuseSoC refuses several of the OpenTitan cores X-HEEP vendors. They go into
# a virtual environment of their own, with the packages X-HEEP's generators import, at the
# commits X-HEEP's python-requirements.txt names (X-HEEP bf0748a). Nothing goes on the
# path:
#
#   scripts/install_fusesoc.sh [PREFIX]          # default ~/.local/opt/xheep-fusesoc
#   export CHIPLEAP_FUSESOC=PREFIX/bin/fusesoc
#
# X-HEEP's boot ROM is compiled during FuseSoC's setup, so the step also needs a RISC-V
# GCC (riscv32-unknown-elf-gcc or riscv64-unknown-elf-gcc on the path, or
# CHIPLEAP_RISCV_GCC).
set -euo pipefail

PREFIX="${1:-$HOME/.local/opt/xheep-fusesoc}"
FUSESOC=c36dffc854241740cf2f96c14522d05830677549
EDALIZE=abf56019cd74ac0e8762849323a91bef4635542a

python3 -m venv "$PREFIX"
"$PREFIX/bin/pip" install -q \
  "fusesoc @ git+https://github.com/x-heep/fusesoc.git@$FUSESOC" \
  "edalize @ git+https://github.com/x-heep/edalize.git@$EDALIZE" \
  hjson==3.0.2 mako==1.4.1 pyyaml==6.0.3 jsonref==1.1.0 tabulate==0.9.0 \
  deepdiff==9.1.0 gitpython==3.1.25 "setuptools<81"
"$PREFIX/bin/fusesoc" --version
