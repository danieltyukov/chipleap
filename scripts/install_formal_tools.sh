#!/usr/bin/env bash
# Install SymbiYosys (formal proofs) and EQY (equivalence checking) into one prefix, built
# from the YosysHQ tags that match the Yosys on the path: both drive that Yosys, and EQY's
# plugins are loaded into it, so their versions must agree. A z3 goes beside them, where
# ChipLeap looks first: z3 4.8.12, the one Ubuntu 22.04 and 24.04 ship, ran out of memory
# on the UART example's proof, which z3 4.15 finishes in a second. Nothing goes on the
# path; each finds its Python modules and plugins beside itself:
#
#   scripts/install_formal_tools.sh [PREFIX]   # default ~/.local/opt/yosyshq-<yosys version>
#   export CHIPLEAP_SBY=PREFIX/bin/sby CHIPLEAP_EQY=PREFIX/bin/eqy
#
# Needs yosys-config and Yosys's headers for EQY's plugins (Debian and Ubuntu: apt install
# yosys-dev), and Python's venv module for z3 (apt install python3-venv). Both tools are
# Python scripts that import click (apt install python3-click, or pip install click into
# the Python that runs them).
set -euo pipefail

# PyPI's z3-solver wheel carries a z3 binary that needs only the C and C++ libraries.
Z3_VERSION=4.15.8.0

command -v yosys-config >/dev/null || {
    echo "install_formal_tools.sh: needs yosys-config (Debian and Ubuntu: apt install yosys-dev)" >&2
    exit 2
}
version=$(yosys -V | sed -n 's/^Yosys \([0-9][0-9.]*\).*/\1/p')
PREFIX="${1:-$HOME/.local/opt/yosyshq-$version}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for tool in sby eqy; do
    git clone -q --depth 1 --branch "yosys-$version" "https://github.com/YosysHQ/$tool.git" "$tmp/$tool"
    make -s -C "$tmp/$tool" install PREFIX="$PREFIX" >/dev/null
done
python3 -m venv "$tmp/z3"
"$tmp/z3/bin/pip" install -q "z3-solver==$Z3_VERSION"
install -m 755 "$tmp/z3/bin/z3" "$PREFIX/bin/z3"
echo "SymbiYosys and EQY for Yosys $version, and $("$PREFIX/bin/z3" --version), in $PREFIX/bin"
