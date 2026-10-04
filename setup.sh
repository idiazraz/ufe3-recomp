#!/usr/bin/env bash
# Fetch PS2Recomp at the pinned upstream commit, apply this project's patches, and build the tools.
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
upstream=https://github.com/ran-j/PS2Recomp
commit=c5a9d02573410a2085a4b4b831b0b68ba3515440
tools=$root/tools/PS2Recomp
if [ ! -d "$tools/.git" ]; then
    git clone "$upstream" "$tools"
fi
git -C "$tools" checkout --quiet "$commit"
if git -C "$tools" apply --check "$root/patches/ps2recomp.patch" 2>/dev/null; then
    git -C "$tools" apply "$root/patches/ps2recomp.patch"
else
    echo "patches/ps2recomp.patch already applied or does not apply cleanly; leaving tools/ as is" >&2
fi
cmake -S "$tools" -B "$tools/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_CXX_FLAGS=-march=x86-64-v3 -DPS2X_ENABLE_AGRESSIVE_LOGS=OFF
cmake --build "$tools/build" --target ps2_recomp ps2_analyzer -j"$(nproc)"
echo "Tools built. Put the extracted disc in disc/ (see README), then run ./rebuild.sh"
