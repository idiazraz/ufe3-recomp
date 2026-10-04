# QoL fixes (PS2Recomp-qol, branch qol-work)

## Step 1 — configure
- Hardlink-copied `tools/PS2Recomp/build/_deps` (raylib/imgui/etc FetchContent sources, 1.4 GB hardlinks) into
  `tools/PS2Recomp-qol/build/_deps` so configure works offline (same trick as PS2Recomp-erx worktree).
- Ran the mandated configure command: exit 0 (log: `work/qol_configure.log`).
- Baseline build of `ps2EntryRunner` succeeded (log: `work/qol_build1.log`).

## Step 2 — FIX 1 gamepad detection (commit c5b346c)
- Read `ps2_pad.cpp` / `runtime/ps2_pad.h`: `readState()` runs on the guest **GameThread**
  (`Pad.cpp` stub -> called from recompiled code), while `PS2Runtime::run()` (main thread) owns the
  raylib render loop -> GLFW joystick calls must be sampled on the main thread.
- Change: added `PSPadBackend::pollMain()`, called once per render-loop iteration in `ps2_runtime.cpp`
  (right before `UploadFrame`). It scans jid 0..15 with `glfwJoystickPresent` /
  `glfwJoystickIsGamepad` / `glfwGetJoystickName`, logs every joystick once as
  `[pad] joystick N: <name> (<mapped gamepad|not a mapped gamepad>)`, picks the first mapped
  gamepad, and copies `glfwGetGamepadState()` buttons/axes into a mutex-protected snapshot.
  `readState()` reads that snapshot (keyboard path untouched).
- GLFW 3.3 (bundled in raylib `src/external/glfw`) has no digital trigger buttons, so L2/R2 come
  from `GLFW_GAMEPAD_AXIS_*_TRIGGER > 0.5`. CMake: added raylib's bundled glfw include dir to
  `ps2_runtime` (guarded for non-Vita/non-Android).
- Verified: build OK; headless boot works (keyboard path unchanged); `[pad]` log from a 25 s run:
  - joystick 0 "MOJI M3S PRO Consumer Control", 1 "Creative Stage Air V2", 2 "Audeze Maxwell Dongle",
    3 "libvirtualhid Mouse" - all *not* mapped gamepads (raylib's 4-slot scan would have picked one of these)
  - joystick 4 "Microsoft X-Box 360 pad" (mapped) -> **used**, 5 "X-Box 360 pad 0" (mapped),
    6 "BY Tech HERO 84 HE Consumer Control" (not mapped)
  - `[pad] keyboard input enabled`

## Step 3 — FIX 2 investigation: why frames latch black
- Baseline measurement (25 s, `PS2X_SHOT_EVERY=30`, scene 2 warning screen): **43 shots, 23 black, 20 with content**
  (before-fix dir `work/shots_qol_before`, counter `work/qol_count.py`).
- Temporary traces (added then removed before commit): `[trace:swapdbuffdc]` in `sceGsSwapDBuffDc`
  (which/dispfb.fbp/draw.fbp/tick) and `[trace:latch2]` in `UploadFrame` (tick, latched displayFbp,
  sampled non-black count).
- Findings from `work/qol_run_trace2.log` (240 latches):
  - The game calls `sceGsSwapDBuffDc` **twice per vsync tick**, alternating `which=0`
    (DISPFB -> fbp 140) and `which=1` (DISPFB -> fbp 0); fbp unit is 8 KiB pages
    (0 -> VRAM 0x0, 140 -> VRAM 0x118000 = exactly one 640x448 CT32 buffer).
  - **fbp 0 always contains the drawn image (non-black), fbp 140 is always black** whenever it is
    presented: the swap for `which=0` clears the *other* buffer and the game never draws into
    fbp 140 during that pass, so the flip to it presents a freshly cleared buffer.
  - The latch always happens after the last swap of the tick -> the presented buffer is exactly
    what DISPFB pointed at after that swap; the bug is that this buffer is the cleared one, not
    that the host reads a stale DISPFB.
- Existing mitigation found in `gs_cpu_backend.cpp PresentFromLocalMemory`: `copySource()` already
  falls back to a finished draw-context frame when the presented buffer is all black, but it was
  gated to `displayFrame.fbp == 0u` only - and the black buffer is the *other* one (fbp 140), so
  the fallback never fired.

## Step 4 — FIX 2 fix + verification (commit c58861e)
- Dropped the `fbp == 0` gate: whenever the buffer DISPFB points at comes back all-black,
  `PresentFromLocalMemory` now substitutes the first draw-context frame with real content
  (falls through if both are black, so genuinely empty screens stay empty).
- Removed all temporary traces (GS.cpp restored byte-identical; no `PS2X_TRACE_LATCH` left).
- Rebuilt and re-ran the exact verification command (25 s, `PS2X_SHOT_EVERY=30`,
  `PS2X_HIDE_DEBUG_UI=1`, dir `work/shots_qol_after`):
  - **before: 43 shots -> 20 with content / 23 black**
  - **after: 43 shots -> 40 with content / 3 black**
  - remaining black: `frame_60` (early boot, before any GS content exists in either buffer),
    `frame_360` and `frame_1110` (rare mid-swap race window: latch between the DISPFB write and
    the draw-env write inside `sceGsSwapDBuffDc`, when both display and context frames point at
    the still-empty buffer). Warning-screen frames otherwise show the text (nonblack ~23500 px).
- Both commits on `qol-work` (no push, no Co-Authored-By). The pre-existing modification of
  `ps2xRuntime/src/runner/register_functions.cpp` was left unstaged/untouched.
- Housekeeping: test harness `work/qol_headless.sh` (private kwin socket `ufe3-qolheadless` so it
  can run concurrently with other agents' headless sessions); no background processes left.
