#!/usr/bin/env bash
# Recompile SLPS_254.41 from work/ufe3.toml and rebuild the runner.
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
tools=$root/tools/PS2Recomp
cd "$root/work"
rm -rf output
"$tools/build/ps2xRecomp/ps2_recomp" ufe3.toml > recomp.log 2>&1
# Only generated files are replaced; game-specific sources come from game/.
find "$tools/ps2xRuntime/src/runner" \( -name 'sub_*.cpp' -o -name 'entry_*.cpp' -o -name 'register_functions.cpp' -o -name 'ufe3_*.cpp' \) -delete
cp output/*.cpp "$tools/ps2xRuntime/src/runner/"
cp "$root"/game/*.cpp "$tools/ps2xRuntime/src/runner/"
cp output/*.h "$tools/ps2xRuntime/include/"
cmake --build "$tools/build" --target ps2EntryRunner -j"$(nproc)" 2>&1 | grep -E ' error|FAILED' || true
