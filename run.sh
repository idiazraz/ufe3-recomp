#!/usr/bin/env bash
# Run the recompiled game from the extracted disc folder. Usage: run.sh [seconds] (0 = no timeout)
root=$(cd "$(dirname "$0")" && pwd)
cd "$root/disc"
t=${1:-0}
if [ "$t" = 0 ]; then exec "$root/tools/PS2Recomp/build/ps2xRuntime/ps2EntryRunner" ./SLPS_254.41; fi
timeout "$t" "$root/tools/PS2Recomp/build/ps2xRuntime/ps2EntryRunner" ./SLPS_254.41
