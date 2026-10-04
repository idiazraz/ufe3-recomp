#!/usr/bin/env bash
# Recompile SLPS_254.41 from work/ufe3.toml and rebuild the runner.
# Also (re)generates the ERX module sources (erx/step2) and metadata table
# (erx/step3) and wires them into the runner -- see erx/step3/README.md.
# Order from a clean checkout: ./setup.sh (builds tools/PS2Recomp) -> ./rebuild.sh.
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
tools=$root/tools/PS2Recomp
erx=$root/erx
cd "$root/work"
rm -rf output
"$tools/build/ps2xRecomp/ps2_recomp" ufe3.toml > recomp.log 2>&1
# Only generated files are replaced; game-specific sources come from game/.
find "$tools/ps2xRuntime/src/runner" -not -path '*/erx/*' \( -name 'sub_*.cpp' -o -name 'entry_*.cpp' -o -name 'register_functions.cpp' -o -name 'ufe3_*.cpp' \) -delete
cp output/*.cpp "$tools/ps2xRuntime/src/runner/"
cp "$root"/game/*.cpp "$tools/ps2xRuntime/src/runner/"
cp output/*.h "$tools/ps2xRuntime/include/"

# --- ERX module support (steps 1-3) ------------------------------------------
# Step 1 products (erx/out/<MOD>.bin + manifest.json) come from the repo; if they
# are absent (fresh checkout without them) rebuild them from disc/ERX.
if [ ! -f "$erx/out/manifest.json" ]; then
    python3 "$erx/erx_prelink.py" --root "$root"
fi
# Step 2 module sources: regenerate erx/step2/out when missing or stale relative to
# the generator scripts, the step-1 manifest, or ps2_recomp itself; otherwise reuse.
step2_stamp=$erx/step2/out/erx_register.cpp
need_erx=0
[ -f "$step2_stamp" ] || need_erx=1
for f in "$erx"/step2/*.py "$erx/out/manifest.json" "$tools/build/ps2xRecomp/ps2_recomp"; do
    if [ "$f" -nt "$step2_stamp" ]; then need_erx=1; fi
done
if [ "$need_erx" = 1 ]; then
    # Drop step-2's synthetic-ELF test scratch (erx/step2/README.md section 2) so it
    # is not rebuilt into the runner; it is regenerable from the documented recipe.
    rm -f "$erx/step2/cfg/TEST.toml" "$erx/step2/wrap/TEST.elf"
    rm -rf "$erx/step2/gen"
    python3 "$erx/step2/erx_wrap.py" --root "$root"
    for m in "$erx"/step2/cfg/*.toml; do
        "$tools/build/ps2xRecomp/ps2_recomp" "$m" >> "$erx/step2/recomp_erx.log" 2>&1
    done
    python3 "$erx/step2/erx_post.py" --root "$root"
fi
# Step 3 metadata table for the game override (cheap; always regenerate).
python3 "$erx/step3/erx_tables.py" --root "$root"
# Wire everything into the runner (PS2X_ERX_SOURCE_DIR, see tools/PS2Recomp
# ps2xRuntime/CMakeLists.txt): module sources in src/runner/erx/, the metadata
# table as a regular runner source + header.
rm -rf "$tools/ps2xRuntime/src/runner/erx"
mkdir -p "$tools/ps2xRuntime/src/runner/erx"
cp "$erx"/step2/out/* "$tools/ps2xRuntime/src/runner/erx/"
cp "$erx/step3/out/erx_tables.h" "$tools/ps2xRuntime/include/"
cp "$erx/step3/out/erx_tables.cpp" "$tools/ps2xRuntime/src/runner/"
# Re-run configure so the ERX source block (if(EXISTS erx_register.cpp)) activates
# when the module sources appeared after the last configure; cache flags are kept.
cmake -S "$tools" -B "$tools/build" > /dev/null
# ------------------------------------------------------------------------------
cmake --build "$tools/build" --target ps2EntryRunner -j"$(nproc)" 2>&1 | grep -E ' error|FAILED' || true
