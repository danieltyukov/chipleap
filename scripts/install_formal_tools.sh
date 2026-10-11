#!/usr/bin/env bash
# Install SymbiYosys (formal proofs) and EQY (equivalence checking) into one prefix, built
# from the YosysHQ tags that match the Yosys on the path: both drive that Yosys, and EQY's
# plugins are loaded into it, so their versions must agree. Nothing goes on the path; each
# finds its Python modules and plugins beside itself:
#
#   scripts/install_formal_tools.sh [PREFIX]   # default ~/.local/opt/yosyshq-<yosys version>
#   export CHIPLEAP_SBY=PREFIX/bin/sby CHIPLEAP_EQY=PREFIX/bin/eqy
#
# Needs yosys-config and Yosys's headers for EQY's plugins (Debian and Ubuntu: apt install
# yosys-dev), and a solver for the proofs (apt install z3). Both tools are Python scripts
# that import click (apt install python3-click, or pip install click into the Python that
# runs them).
set -euo pipefail

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
echo "SymbiYosys and EQY for Yosys $version in $PREFIX/bin"
