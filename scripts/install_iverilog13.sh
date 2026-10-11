#!/usr/bin/env bash
# Install Icarus Verilog 13 beside the system one, for gate-level simulation of IHP SG13G2
# cells: Icarus 12 elaborates the cell models but every flip-flop stays X, so every test
# fails the same way. This is Tiny Tapeout's build of the v13_0 release for Ubuntu 24.04,
# unpacked (not installed as a package) so the system Icarus is untouched, with two small
# wrappers that point it at its own library directory. Nothing goes on the path:
#
#   scripts/install_iverilog13.sh [PREFIX]          # default ~/.local/opt/iverilog-13
#   export CHIPLEAP_IVERILOG=PREFIX/bin/iverilog    # chipleap uses PREFIX/bin/vvp with it
set -euo pipefail

PREFIX="${1:-$HOME/.local/opt/iverilog-13}"
URL=https://github.com/TinyTapeout/iverilog/releases/download/v13.0/iverilog_13.0-1_amd64.deb
SHA256=888bca6b06289ea8b55368d43db38dc88932938319ab40d45a763ce5114e8505

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -sSfL -o "$tmp/iverilog.deb" "$URL"
echo "$SHA256  $tmp/iverilog.deb" | sha256sum -c --quiet -
mkdir -p "$PREFIX/bin"
dpkg-deb -x "$tmp/iverilog.deb" "$PREFIX/pkg"

# iverilog finds ivlpp, ivl and the targets under -B; vvp finds system.vpi under -M and
# takes more -M options after it (cocotb adds its own).
cat > "$PREFIX/bin/iverilog" <<EOF
#!/bin/sh
exec "$PREFIX/pkg/usr/bin/iverilog" -B"$PREFIX/pkg/usr/lib/ivl" "\$@"
EOF
cat > "$PREFIX/bin/vvp" <<EOF
#!/bin/sh
exec "$PREFIX/pkg/usr/bin/vvp" -M"$PREFIX/pkg/usr/lib/ivl" "\$@"
EOF
chmod +x "$PREFIX/bin/iverilog" "$PREFIX/bin/vvp"
"$PREFIX/bin/iverilog" -V 2>&1 | sed -n 1p
