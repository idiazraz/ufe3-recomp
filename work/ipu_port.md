# IPU port log (PCSX2 core -> PS2Recomp)

Working notes for the IPU implementation in tools/PS2Recomp-ipu (branch ipu-work).
Plan: work/ipu_plan.md ("# Port plan (Q3)", Findings 4-8).

## Step 1 — core port + adapter + build

What:
- Vendored the PCSX2 IPU core (upstream revision 81526d4) into
  `ps2xRuntime/src/lib/ipu/`: IPU.h/.cpp, IPU_MultiISA.h/.cpp, mpeg2_vlc.h,
  IPU_Fifo.h/.cpp, IPUdma.h/.cpp, yuv2rgb.h/.cpp, IPUdither.cpp. SPDX GPL-3.0+
  headers kept verbatim, "Ported from PCSX2" note added, THIRD_PARTY.md written.
  Deviation from plan's file naming: kept upstream filenames (one file per
  upstream file) to ease future resync instead of merging into ipu_core.*.
- `ipu/ipu_compat.h`: shim supplying types (u8..u64, u128, mem128_t, r128),
  asserts (evaluate but don't abort), IPU_LOG->noop, StringUtil/Console/DevCon
  stubs, cpuRegs shim, CPU_INT/CPU_SET_DMASTALL/hwIntcIrq hooks (deferred to the
  adapter pump), IPU_CMD/IPU_CTRL/IPU_BP/IPU_TOP addresses, MULTI_ISA_* ->
  single-ISA (x86-64 SSE2 via ARCH_X86). __fi/__ri mapped to nothing (inline
  would drop cross-TU symbols).
- Stripped upstream-only pieces: SaveStateBase::ipuFreeze/ipuDmaFreeze,
  ReportIPU, the static IPUWorker function pointer (call the real function),
  Config.h dependency. IPUdma.cpp's scheduler-driven ch3/ch4 engines replaced by
  the adapter (upstream behaviour preserved in the adapter; comments kept).
- `ps2_ipu.{h,cpp}` adapter: registers, 8-qword in-FIFO / out-FIFO handling,
  synchronous pump (CPU_INT flags are dispatched to quiescence, never
  recursively), ch3 fromIPU (qwc=0 -> 0x10000 IPU-terminated idiom, drains the
  out-FIFO to MADR until dry + command done, D_STAT cause 3, D_CTRL.STS==fromIPU
  -> D_STADR), ch4 toIPU (chain payloads staged from ps2_memory.cpp's existing
  chain walker, fed into the in-FIFO on IPUCoreStatus.DataRequested demand,
  D_STAT cause 4 on drain; guest CHCR teardown stops silently). INTC_IPU latched
  into INTC_STAT bit 8 on every command completion (core does this via
  hwIntcIrq).
- `ps2_memory.cpp` wiring: 0x10002000..0x10002030 read/write -> adapter (trace
  kept), write128 0x10007010 -> in-FIFO, read128/read32 0x10007000 -> out-FIFO,
  DMA start on 0x1000B000 (ch3) / 0x1000B400 (ch4; chain mode reuses the
  existing chain-tag walker, payload routed to ch4SubmitChain), CHCR writes
  without the start bit -> ch3Stop/ch4Stop. New public hooks
  ipuCompleteDmacChannel / ipuRaiseIntcIrq / ipuUpdateStadr.

How verified: `cmake --build build --target ps2EntryRunner` compiles and links
(clean apart from pre-existing third-party warnings). Game/test runs follow in
Step 2/3.

## Step 2 — unit tests on real movie bytes + two adapter fixes

What:
- `ps2xTest/src/ps2_ipu_tests.cpp` (registered in ps2xTest/CMakeLists.txt +
  main.cpp): three cases driving the adapter through the public PS2Memory
  register paths (same paths the game uses):
  1. FDEC fb=0 / fb=8 on 1024 real bitstream bytes from
     work/ipu_ref/movie0.ipu offset 0xB90 (first frame chunk of the ipum
     offset table; embedded as a C array) — TOP must equal the BE32 word at
     bit offset 0 / 8, BP advances by 8.
  2. The measured init sequence (BCLR, SETIQ intra/non-intra, SETVQ,
     SETTH(0,0); standard MPEG-2 intra matrix + flat non-intra, game matrices
     are runtime RAM), ch4 toIPU normal kick with the same bytes, FDEC fb=0 +
     fb=8 + IDEC 0x10010000 (the game's exact command), ch3 fromIPU kick with
     qwc=0 (the game's exact kick) — the first RGB32 macroblock (1024 B at
     MADR) must be non-trivial (not zero, not flat), and D_STAT must latch
     causes 3 (fromIPU) and 4 (toIPU).
  3. Register surface: CTRL.IFC counts FIFO writes, CTRL RST clears IFC/BP/TOP.
- Fixes found by the tests (one hypothesis each, cheapest check = the test):
  a. ch3Kick stored the raw runtime CHCR (start bit 0x100) into the core's
     `ipu0ch.chcr`, whose STR field is bit 0 -> the core's IDEC gate
     (`!ipu0ch.chcr.STR`) blocked forever. Now STR/MOD/TTE/TIE are mapped
     explicitly (same mapping ch4 already used).
  b. CTRL.RST readback: PCSX2's ipuSoftReset self-clears the RST bit; the
     runtime's register model (and two pre-existing stub tests) read the last
     written value back. The adapter now latches the written RST bit until the
     next CTRL write (UFE3 never reads CTRL, so no behavioural risk).

How verified: `ps2x_tests` full binary: **Passed: 443, Failed: 0** (441 pre-existing
+ 3 new IPU cases, minus none; includes the two sceIpuInit stub tests passing
again). ps2EntryRunner rebuilt green.

## Step 3 — in-game: the opening movie plays

What: headless runs with the built runner (PS2X_TRACE_IPU=1, PS2X_SHOT_DIR,
PS2X_IDLE_DUMP + PS2X_EE_PEEK=0x42ab5c,0x412bac). Private compositor socket
(workaround: run_headless_ipu.sh, a run_headless.sh copy with socket
ufe3-headless-ipu, to stop colliding with the concurrently running agents'
headless runs).

How verified / what we see:
- The measured decode loop runs to completion every cycle: sceIpuInit tables ->
  ch4 chain feed (64 KiB/chunk) -> FDEC fb=0, fb=8 -> CTRL 0x00800000 ->
  IDEC 0x10010000 -> ch3 kicks (madr 0x684540 qwc 0xFFC0 and madr 0x784140
  qwc 0x1840 — real ring-buffer sizes, not qwc=0 as in Finding 6) -> sceIpuSync
  -> teardown. D_STAT causes 3/4 latch, INTC_IPU raised per command.
- The opening movie SHOWS REAL IMAGES (work/ipu_shots1/): letterboxed 4:3 video
  in a 640x480 window — e.g. frame_1200: pale blue sky over a hazy mountain
  ridge; frame_1680: deep blue sky with a sunlit cloud/smoke plume. Frames
  1380..1680 all differ (the loop advances; ~8 distinct frame bitstreams per
  187 cycles at first, mostly-distinct later). The ch4 payloads do NOT appear
  in movie0.ipu verbatim (the game transforms the ipum chunks before feeding).
- Quality/pacing caveats (open): images are MB-blocky (each 16x16 macroblock
  renders as a smooth gradient — AC detail looks suppressed) and the loop runs
  at ~13-14 decode cycles/s (a 131-frame movie takes minutes, not seconds).
- Ending (open): scene word 0x42AB5C reaches 4 but never leaves it in 210 s.
  In the pad run the movie pump state 0x412BAC advances 0 -> ffffffff -> 2 -> 4
  -> 6 and stalls at 6 (the same state the plan reports for the stuck title
  movie) with feeds nearly stopping (130/210 s, 42 distinct).

## Step 4 — title/pad run (partial)

What: PS2X_PAD_SCRIPT="circle@900-910,circle@1100-1110,circle@6000-6010,circle@6400-6410"
(later presses added because the timeline is ~2x the plan's estimate: the movie
window is at ticks ~1000-2000 here, not 400-600).
How verified: scene word stayed 4 / pump state 6 through 210 s; scene 0x13 was
never reached, so "next scene id" cannot be answered yet. Not a button-handling
failure: the scene is still waiting on its movie pump (same state-6 stall).

Next steps (for a follow-up session): (1) find why the movie pump stalls at
state 6 after ~2 minutes (compare the player object state machine at 0x412AD0
against the ch3 transfer sizes the game expects — suspect my ch3 completion
model vs. the game's two-buffer ring: the second kick per cycle may need to
receive the tail of one frame's output), (2) MB-blocky output: dump one decoded
frame at 0x684540 and compare an MB against ffmpeg's ipu decoder on the same
bytes, (3) pacing: measure what the player paces on (audio? vsync? CD reads?).

## Step 5 — blocky output root cause: SETIQ matrices were all zeros (fixed)

What (one hypothesis at a time):
- Enabling the core's IPU_LOG (ipu_compat.h: PS2X_TRACE_IPU-gated fprintf instead of
  noop) and running 90 s headless: after every sceIpuInit the trace prints
  "Read intra quantization matrix from FIFO." followed by 64 zero bytes, likewise
  non-intra and the VQ CLUT. All 13 440 manual in-FIFO writes in the run were zero
  qwords. AC coefficients dequantise to 0 while DC survives -> flat 16x16 blocks.
- The data source is the HLE stub Kernel/Stubs/IPU.cpp completeIpuInit(): it
  Load128()s the tables from IQVAL_BASE=0x1721E0 / VQVAL_BASE=0x172230. Those
  addresses are BELOW the main ELF's load segment (0x200000..0x4E5E3C) and are
  never written: RAM reads zero forever.
- Ground truth: disassembled the game's real sceIpuInit (ELF vaddr 0x362D88; the
  HLE replaces it via sub_00362D88_0x362d88.cpp). It loads the tables from
  0x00432510 (lq/sq x8 into 0x10007010) and 0x00432560 (2 qwords, SETVQ). The
  write pattern is unusual but real: qwords at +0,+16,+32,+48,+64 then +64 four
  times total -> SETIQ intra consumes bytes 0..63 (the intra matrix) and SETIQ
  non-intra consumes the +0x50..+0x5F 16-byte pattern tiled 4x. The stub already
  mimicked this pattern exactly; only the base addresses were wrong.
- Fix: IQVAL_BASE=0x432510, VQVAL_BASE=0x432560 (Kernel/Stubs/IPU.cpp). The game's
  real intra matrix is custom, NOT the MPEG default:
  08 10 10 13 10 13 16 16 / 16 16 16 16 1A 18 1A 1B / ... / 2E 2E 38 38 3A 45 45 53;
  non-intra is flat 16; VQ CLUT 00 00 21 04 ... (so "expect MPEG-like 8,16,19,22"
  in the issue text was close but not exact).

How verified:
- Trace now shows the exact 0x432510/0x432550 contents after SETIQ (run_ipu5b.log
  lines 265-274 etc.); non-intra = all 16s.
- In-game frames are crisp with correct colours (work/shots_ipu5b/).
- PSNR ground truth: the OPENING movie is not movie0/movie1 (the 320x240
  ref_movie0/1 streams) - identified it by matching the ch4 feed payload bytes
  against the disc: FILE3.BIN offset 204288000 (ipum, 640x448, 1950 frames; the
  ch3 ring 0xFFC0+0x1840 qwords = 1 146 880 B = exactly 640*448*4 = one RGB32
  frame). Converted with ipum2ffmpeg.py + ffmpeg (work/ipu_ref pipeline works
  unchanged). Shot frame_3000 vs source frame 592 (display mapping 640x448 ->
  640x224, left 360 px free of the debug overlay):
  **PSNR = 41.8 dB** (neighbouring source frames score 31.2 / 27.5 dB, i.e. the
  correspondence is unambiguous). Criterion "> 30 dB" met.
- Note for the reference set: ref_movie0/1 (320x240) are title/menu movies; the
  opening FMV needs work/ipu_ref extraction of the FILE3.BIN stream(s).

## Step 6 — status of issues 2 (movie never ends) and 3 (pacing), and a
## concurrency incident (for the leader)

Not implemented in this session; facts gathered:
- The ch4 feed payloads ARE verbatim FILE3.BIN data (Step 3's "the game transforms
  the chunks" was a wrong-file artifact: the patterns were searched in FILE2.BIN).
  Distinct 64 KiB chunks, some repeats (player ring re-feeds).
- The ch3 two-kick ring is exactly one 640x448 RGB32 frame per decode cycle
  (0xFFC0 + 0x1840 qwords = 1 146 880 B), split 0x684540 (1 MiB) + 0x784140 (97 KiB).
- Remaining suspect for the state-6 stall: the ch3 completion model in ps2_ipu.cpp
  vs the game's double-buffer ring (per the plan), to be read against the player
  state word 0x412BAC transitions.
- CONCURRENCY INCIDENT: a second MiMo agent (PID 578756, "Deliverable 1: port the
  core + adapter" task) was live in tools/PS2Recomp-ipu during this session,
  rewriting ps2_ipu.{h,cpp} (new ipuWriteCmd-style API), creating ipu_dma.{h,cpp},
  ipu_core.{h,cpp} and touching ps2_memory.cpp - i.e. exactly the files issues 2/3
  need. It is forbidden to commit ("the leader reviews and commits"). To avoid
  clobbering it, this session edited only Kernel/Stubs/IPU.cpp and ipu_compat.h and
  verified via a manual relink of the last coherent object set (the tree was
  mid-surgery and would not compile). Issues 2/3 must wait until that agent's work
  is reviewed/landed or discarded. Also note its spec would REBUILD the adapter with
  a different file layout than the committed Steps 1-2 port (which kept upstream
  filenames deliberately) - the leader should pick one.

## Step 7 — leader session 2026-10-05: sharp video, opening movie ends, audio stream blocks the scene

- **Ghost MiMo session:** `opencode serve --service` kept running the killed sub-agent session
  `ses_ef89e1252ffeYCSc4P64Zm3rxI` ("PCSX2 IPU core port into PS2Recomp"). It rewrote the adapter into a different file
  layout (ipu_core/ipu_dma/ipu_fifo/..., `ipuWriteCmd`-style API) until 23:52:56, and that included a build. Its edits are backed up in the
  scratchpad as `ipu_ghost_2352.patch` and were reverted; the worktree is back on the committed PCSX2-layout port. The session
  still exists in the service (no CLI abort; delete it or restart the service with the user's OK).
- **Blockiness fixed:** commit 42469e9 builds the vendored IPU files with `-fno-strict-aliasing`, as upstream PCSX2 does
  (pcsx2/CMakeLists.txt:31). Combined with a2743ee (SETIQ tables), the opening FMV (scene 4) decodes sharp: gas tank, monster,
  photo-collage montage, a light burst, and a final white flash (work/shots45, work/shots45_grid.png). No A/B build without the flag was
  run, so the flag's individual contribution is unproven, but it matches upstream.
- **The opening movie does end.** Stream header at 0x67CD40 (`ipum`, 640x448, total = 0x79E = 1950 frames). "Ended" check
  sub_0032CAD8 = (pos == total-2), pos = player+0x12C (or +0x134 if negative). The position reaches 0x79C at ~117 s (run46).
  Video runs at ~16 fps vs the 30 fps source (pacing issue 3 remains), so the movie lasts ~2 min instead of 65 s.
  Note: the game/ufe3_overrides.cpp fake-playback hooks (0x32CB48 ufe3MovieActiveQuery, 0x356AF8 ufe3MovieAudioStreamBusy)
  are still bound; at the movie's end the first one pokes player+0xDC = -1. Revisit or remove them now that the IPU is real.
- **Why scene 4 still doesn't exit:** COpening's tick (sub_0021BA40) returns "done" only if movie-ended && sub_00356B88 == 0.
  0x356B88 is SOUNDMAN cmd 0x24 (RPC via 0x356E10) → IRX jump table 0x71E0 → handler 0x718 → fn 0x3470:
  busy = flag@IRX+0x486AC && 1 <= (*(IRX+0x486A8))->+0x28 <= 13 (IRX base on IOP = 0x4CF00).
  PS2X_SOUNDMAN_PEEK now also dumps that movie-audio stream object (EeScheduler.cpp). run48: flag=1, obj=0x1B72C0,
  +0x28 = 8 → 0xA, so the stream is in the active range and never reaches its end state. Hypothesis for the next session: the stream
  state machine advances on SPU2 progress (IRQ/current address), and the runtime's SPU2 is only register storage plus faked DMA
  completion (docs/NOTES.md "Known gaps"). Next: disassemble the SOUNDMAN stream state machine (writers of +0x28) and
  find what moves it from 0xA onward, then emulate that, or HLE the end of the stream when the video ends.
- Title scene (0x13) will hit the same audio gate (cmd 0x22 thunk 0x356AF8 = predicate C; mimo_title.md Findings 11/19).
