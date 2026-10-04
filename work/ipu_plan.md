# UFE3 (SLPS-25441) IPU needs & PCSX2 IPU port plan

Task: measure what the game needs from the PS2 IPU, then plan a port of PCSX2's IPU core
into PS2Recomp's runtime (tools/PS2Recomp/ps2xRuntime). Implementation is a separate task.

Protocol: findings appended incrementally as "## Finding N" below.

## Finding 1 — current IPU surface in the runtime

- `ps2_memory.cpp` write path (line ~1182): writes to 0x10002000..0x10002030 are stored in
  `m_ioRegisters`; a write to CTRL (0x10002010) keeps bit31 (BUSY) clear and a write with bit30
  (RST) set zeroes CMD/BP/TOP. Read path (line ~2322): returns the stored value, masks bit31 on
  CTRL. So CMD/CTRL/BP/TOP are plain storage — no decoding, no busy timing, no FIFO coupling.
- `Kernel/Stubs/IPU.cpp`: `sceIpuInit` performs a canned HLE init — writes CTRL=0x40000000
  (RST), pushes 8×128-bit IQ tables into IN_FIFO (0x10007010) and issues CMD 0x50000000
  (SETIQ) and 0x58000000 (SETTH), then 2×128-bit VQ table + CMD 0x60000000 (SETVQ) and
  0x90000000 (BCLR), then CTRL RST + CMD 0. These CMD values confirm the game (via Sony
  libipu) uses SETIQ/SETTH/SETVQ/BCLR at init. `sceIpuSync/StopDMA/RestartDMA` are no-ops
  returning 0.
- This means: whatever the game later writes to CMD will be ignored except for storage, and
  DMA ch3/ch4 to/from the FIFOs is the only place where real data can flow.

## Finding 2 — PCSX2 IPU command semantics (needed to decode the trace)

From `tools/pcsx2-ref/pcsx2/IPU/IPU.h` (`tIPU_CMD_*` unions, `SCE_IPU` enum) and
`IPU.cpp`/`IPU_MultiISA.cpp` dispatch (`IPUCMD_WRITE`, `IPUWorker`):

- CMD opcode = bits 31-28: 0=BCLR, 1=IDEC, 2=BDEC, 3=VDEC, 4=FDEC, 5=SETIQ, 6=SETVQ,
  7=CSC, 8=PACK, 9=SETTH.
- BCLR: clears in-FIFO + bit pointer, BP = val[6:0]. SETTH: th0=val[8:0], th1=val[24:16]
  (transparency thresholds used by CSC/IDEC RGB output). Both execute inline (no BUSY).
- IDEC: fb=val[5:0], qsc=val[20:16], DTD=bit24, SGN=bit25, DTE=bit26, OFM=bit27
  (0=RGB32, 1=RGB16). Decodes a whole intra macroblock (VLC+IDCT+yuv2rgb) to the out FIFO.
- BDEC: fb, qsc, DT=bit25, DCR=bit26, MBI=bit27 (macroblock coeff decode only).
- VDEC: table=val[27:26] (0=MBAI,1=MBT,2=MC,3=DMV), result in CMD.DATA + TOP.
- FDEC: skip fb bits, read 32 bits big-endian into TOP.
- SETIQ: bit27 selects non-intra matrix; reads 64 bytes (4 qwords) from in-FIFO.
  SETVQ: reads 32 bytes (2 qwords) VQ CLUT. CSC: mbc=val[10:0] macroblock count,
  DTE=bit26, OFM=bit27 (YCbCr->RGB). PACK: same fields (RGB32->RGB16/VQ4).

## Finding 3 — PS2X_TRACE_IPU instrumentation (trace-only)

- `ps2_memory.cpp`: logs every write to 0x10002000 (decoded CMD), 0x10002010 (CTRL),
  0x10002020 (BP), 0x10002030 (TOP); rate-limited reads of CTRL; writes to ch3/ch4
  madr/qwc/tadr/chcr; DMA starts on ch3 (0x1000B000) / ch4 (0x1000B400).
- `Kernel/Stubs/IPU.cpp`: logs sceIpuInit/StopDMA/RestartDMA and rate-limited sceIpuSync.
- No behaviour change; all logging behind `getenv("PS2X_TRACE_IPU")`.
- Runs: `work/ipu_trace.txt` (40 s) and `work/ipu_trace2.txt` (20 s), headless, no input.

## Finding 4 — commands the game actually issues (40 s run)

Counts (`work/ipu_trace.txt`, 21542 trace lines):

| command | count | parameters (all runs) |
|---|---|---|
| BCLR   | 3572 | always `bp=0` |
| SETIQ  | 2382 | alternating `intra` / `non-intra` (bit27), fb=0 (64 B each) |
| SETTH  | 2381 | always `th0=0 th1=0` |
| FDEC   | 2380 | alternating `fb=0` / `fb=8` |
| SETVQ  | 1191 | no params (32 B CLUT) |
| IDEC   | 1190 | always `0x10010000`: fb=0 qsc=1 dtd=0 sgn=0 dte=0 **ofm=RGB32** |

Not observed: BDEC, VDEC, CSC, PACK. The stream is decoded with IDEC (full intra-MB
decode incl. YUV->RGB32), so CSC/PACK are unused at least for the first macroblock of
every frame; VDEC may appear for later macroblocks (see Finding 5 caveat).

## Finding 5 — order per decode cycle (the movie decoder loop)

Every cycle (≈30 Hz in run 1; the loop is stuck retrying the first macroblock because
the runtime returns no decoded data, so the game re-inits the ipum decoder each frame —
1191 sceIpuInit in 40 s = 1 per cycle). Cycle order, identical every time:

1. `WR DMA ch3 chcr=0`, `ch4 chcr=0` (teardown of previous cycle)
2. sceIpuInit: CTRL=0x40000000 (RST), CMD BCLR bp=0, SETIQ intra, SETIQ non-intra,
   SETVQ, SETTH th0=0 th1=0, CTRL RST, BCLR  (tables pushed to in-FIFO first)
3. BCLR bp=0, sceIpuSync, BCLR bp=0, sceIpuSync, SETTH th0=0 th1=0
4. ch4 toIPU: tadr=0x8E25C0, qwc=0, chcr=0x00000105 (STR|chain) -> feeds bitstream
5. FDEC fb=0 (peek picture header word), sceIpuSync x2, FDEC fb=8 (peek next word),
   sceIpuSync
6. CTRL=0x00400000 write, then IDEC 0x10010000 (1 macroblock, RGB32 out)
7. ch3 fromIPU: madr=0x684540, qwc=0, chcr=0x00000100 (STR|normal|burst)
8. sceIpuSync, then teardown (see 1.)

## Finding 6 — DMA sizes / addressing

- ch3 fromIPU (PCX2 "IPU0"): normal mode, **qwc=0 as programmed by the guest**
  (measured: `WR DMA ch3 qwc=0x00000000` before every CHCR kick). PCSX2's `dmaIPU0()`
  comment (IPUdma.cpp): QWC 0 is changed to 0x10000 and the transfer is
  "transfer first, ask questions later" — it moves whatever the IPU out FIFO holds and
  ends (clears STR, fires DMAC_FROM_IPU IRQ) when the FIFO runs dry. So the effective
  size is IPU-driven: one IDEC of RGB32 = 1 macroblock = 64 qwords = 1024 bytes to
  madr=0x684540 (fixed buffer address, identical every cycle).
- ch4 toIPU (PCSX2 "IPU1"): chain mode, TADR=0x8E25C0 (also identical every cycle),
  qwc=0 at kick; per-tag qwc drives `ipu_fifo.in.write()` (IPU1chain).
- toIPU demand-driven: PCSX2 stalls the channel when the in FIFO is full (8 qwords)
  and the IPU requests data (`IPUCoreStatus.DataRequested`).

## Finding 7 — synchronization / IRQ expectations of the game

- The game never reads IPU CTRL: **0 CTRL reads** in 40 s (read32/64 route through
  `readIORegister`, so this is a real zero). No busy-polling of CMD.BUSY/CTRL.BUSY from
  game code.
- All synchronization goes through `sceIpuSync` (>= 7001 calls in 40 s, ~3-6 per cycle)
  — currently stubbed to return 0 immediately. `sceIpuStopDMA`/`sceIpuRestartDMA` are
  never called (0 in 40 s) but must stay bound (the stubs exist).
- Consequence: the port can execute IPU commands and DMA **synchronously** (like the
  runtime already does for VIF/GIF transfers) and keep sceIpuSync = "done", provided the
  observed order (bitstream feed -> FDEC -> IDEC -> fromIPU DMA -> sync) is honoured.
- Interrupts the core normally raises (INTC_IPU after each command, DMAC_FROM_IPU /
  DMAC_TO_IPU at DMA end) must still be modelled for the runtime's `completeDmacChannel`
  / D_STAT + INTC plumbing, even if this game tolerates synchronous completion.

## Finding 8 — what the movie container looks like (for validation)

`work/ipu_ref/movie0.ipu` (first bytes): `'ipum'`, u32 payload size 0x13EA26, u32 packed
0x00F00140 (w=320, h=240), u32 0x83 (131 frames), then 0, 0x83, 0x0B, 1, then pairs of
u32s (0xB90,3), (0xBB0,5), (0xBD0,7) ... — a seek/offset table.
- ffmpeg has both an `ipu` demuxer and an `ipu` decoder (MPEG-2 variant for PS2 IPU).
- Quick probe (temp files): feeding the file from offset 0x20/0x44/0x200 gives
  "Picture size 0x0 is invalid"; from offset **0xB90** (the first table offset) ffmpeg's
  ipu demuxer demuxes 123 packets, but the decoder rejects them
  ("Invalid data found when processing input"). So the payload is close but per-frame
  chunk framing (or a sequence/picture header the game injects via FDEC/BCLR) still has
  to be straightened out. The game's FDEC fb=0 / fb=8 peeks match picture-header words.

## Finding 9 — PCSX2 pcsx2/IPU inventory (Q2)

| file | lines | implements | external dependencies |
|---|---|---|---|
| IPU.h | 299 | register file (`ipuRegs` aliased at `eeHw[0x2000]`), tIPU_CTRL/BP/CMD bitfields, BP ring buffer | `Common.h`, `IPU_Fifo.h`, `IPUdma.h`, `cpuRegs`/`CPU_INT` macros |
| IPU.cpp | 540 | register read/write (ipuWrite32/64), command dispatch (`IPUCMD_WRITE`), BCLR/SETTH/IDEC/BDEC setup, `IPUWorker` pump, savestate, reset | `Config.h`, `Console`, `hwIntcIrq`, IPU_MultiISA, CPU_INT scheduling |
| IPU_MultiISA.h | 165 | macroblock structs (mb8/mb16/rgb32/rgb16), decoder_t state, VLC helpers | `GS/MultiISA.h` (MULTI_ISA_* dispatch), endian helpers |
| IPU_MultiISA.cpp | 2008 | the core: bitstream reader (getBits32/64, FillBuffer), VLC decode (mpeg2 tables), `mpeg2sliceIDEC`, `mpeg2_slice` (BDEC), `ipuVDEC`, `ipuFDEC`, `ipuSETIQ`, `ipuSETVQ`, `ipuCSC`, `ipuPACK`, `ipu_csc`, `ipu_vq`, `IPUWorker` | cpuRegs (cycle), hwIntcIrq, FMV flags |
| mpeg2_vlc.h | 467 | MPEG-1/2 VLC tables (DCT, motion, MB type...) | none |
| yuv2rgb.cpp/.h | 276 | YCbCr->RGB32 conversion (scalar + SSE + NEON variants) | GS/MultiISA.h |
| IPUdither.cpp | 124 | RGB32->RGB16 dithering (PACK/CSC OFM=1 paths) | none |
| IPU_Fifo.cpp/.h | 225 | in FIFO (8 qwords, `g_BP.IFC`) / out FIFO (`ctrl.OFC`) ring buffers + demand flags | IPU.h, IPUdma.h |
| IPUdma.cpp/.h | 334 | ch4 toIPU chain feeder (IPU1chain/IPU1dma), ch3 fromIPU mover (IPU0dma incl. the qwc=0->0x10000 idiom), DMAC IRQ/STALL signalling, `IPUCoreStatus` | DMA channel regs `ipu0ch/ipu1ch`, `dmacRegs` (STS/STADR), `dmaGetAddr`, `hwDmacIrq/hwDmacSrcChain/hwDmacSrcTadrInc`, `CPU_SET_DMASTALL`, `CPU_INT` |

Needed for the commands found in Q1 (BCLR/SETIQ/SETVQ/SETTH/FDEC/IDEC):
IPU.h, IPU.cpp (dispatch + regs), IPU_MultiISA.cpp (ipuSETIQ/SETVQ/FDEC/VDEC +
mpeg2sliceIDEC + bitstream reader), mpeg2_vlc.h, yuv2rgb (IDEC RGB32 output),
IPU_Fifo, IPUdma (ch3/ch4). Keep IPUdither + CSC/PACK anyway: they are small, and later
macroblocks of a frame may use VDEC/CSC (the stuck loop prevented observing steady
state); dropping them buys little.

---

# Port plan (Q3)

## Target layout in tools/PS2Recomp/ps2xRuntime

1. `src/lib/ipu/` (new, vendored from PCSX2, GPL-3.0+, SPDX headers preserved):
   - `ipu_core.{h,cpp}` = IPU.cpp + IPU.h + IPU_MultiISA.h/.cpp + mpeg2_vlc.h
   - `ipu_fifo.{h,cpp}` = IPU_Fifo.*
   - `ipu_yuv2rgb.cpp` = yuv2rgb.cpp (x86-64 SSE2 or scalar variant only)
   - `ipu_dither.cpp` = IPUdither.cpp
   - `ipu_dma.{h,cpp}` = IPUdma.* (rewritten against ps2_memory DMA hooks)
2. `src/lib/ps2_ipu.cpp` (new): the adapter. Holds the FIFOs/core state and exposes:
   `ipuWriteCmd/ctrl`, `ipuReadCmd/ctrl/bp/top`, `ipuFifoInWrite128`,
   `ipuToIpuDmaKick(channel regs)`, `ipuFromIpuDmaKick(channel regs)`, `ipuReset`.
3. `ps2_memory.cpp`: replace the 0x10002000..0x10002030 plain-storage blocks (write
   ~line 1182, read ~line 2322) with calls into the adapter; route `write128` of
   0x10007010 (in FIFO) and 128-bit/32-bit reads of 0x10007000 (out FIFO) to the FIFOs;
   in the DMA-start handler (`channelBase == 0x1000B000` / `0x1000B400`) implement the
   transfers described in Findings 5/6 and complete them via the existing
   `completeDmacChannel(channelBase, cause)` (cause 3 = fromIPU, 4 = toIPU).
4. `Kernel/Stubs/IPU.cpp`: keep the HLE init; make `sceIpuSync` report completion of the
   (synchronously executed) command/DMA; leave StopDMA/RestartDMA bound.

## Adapter layer specifics

- **Registers**: CMD write -> `IPUCMD_WRITE` equivalent; run the worker synchronously to
  completion (in-FIFO is always fed first by this game — ch4 DMA precedes the FDEC/IDEC).
  CTRL write semantics per `tIPU_CTRL::write` + RST -> reset; reads must return real
  IFC/OFC/BUSY bits. BP/TOP are read-only results (BDEC/VDEC/FDEC).
- **FIFOs**: in FIFO 8 qwords (writes from ch4 chain tags and CPU write128), out FIFO
  (IDEC RGB32 output, 64 qwords/MB max, drained by ch3). Model backpressure exactly
  (PCSX2 stalls DMA on full FIFO); with synchronous execution the stall is simply
  "process command after feed".
- **ch4 toIPU**: chain mode at TADR; reuse the existing chain-tag walker in
  `ps2_memory.cpp` (it already handles NEXT/REF/REFE/CALL/RET/END) but feed `chainBuf`
  into the in FIFO instead of GIF/VIF queues; respect per-tag qwc + CHCR.TTE.
- **ch3 fromIPU**: normal mode; implement the PCSX2 qwc=0 => 0x10000 "IPU-terminated"
  idiom: copy out-FIFO qwords to madr until the FIFO is dry, then clear CHCR.STR and
  raise the DMAC_FROM_IPU completion (D_STAT cause 3). Also update DMAC STADR if
  DCTRL.STS==fromIPU (check whether UFE3 uses it; cheap to implement).
- **IRQ/status bits**: this game polls none (Finding 7) but the core must still: INTC_IPU
  (IPU command done, `hwIntcIrq` analogue) and DMAC ch3/ch4 completion. Wire to the
  runtime's existing D_STAT/INTC mechanism (`completeDmacChannel`,
  `queueCompletedDmacCause`). Keep CTRL.BUSY accurate for other titles.

## What to strip / replace

- `SaveStateBase::ipuFreeze`, `ReportIPU`, `Console.*`, `StringUtil`, `Config.h`,
  `FMVstarted/EnableFMV` heuristics.
- MULTI_ISA machinery (`GS/MultiISA.h`): compile the core once, scalar+SSE2 only;
  drop NEON/AVX variants and `_byteswap` MSVC paths (use `__builtin_bswap`).
- PCSX2 scheduler macros (`CPU_INT`, `CPU_SET_DMASTALL`, `eCycle`, `cpuRegs.cycle`)
  and `eeHw` alias: replace with synchronous execution + runtime callbacks;
  `dmaGetAddr` -> `PS2Memory::translateAddress` + rdram/scratchpad bases.
- Keep the timing-delay comments in spirit: no fake delays needed (game syncs via
  sceIpuSync, measured synchronous order).

## License / attribution

- PCSX2 IPU code is `SPDX-License-Identifier: GPL-3.0+` with
  `SPDX-FileCopyrightText: 2002-2026 PCSX2 Dev Team`. Keep both header lines verbatim in
  every vendored/derived file and add a note "Ported from PCSX2 (https://github.com/PCSX2/pcsx2)".
  PS2Recomp is GPL-3; distributing the combined work under GPL-3.0+ satisfies both.
  Record the vendored files + upstream revision in a `THIRD_PARTY.md`-style note.

## Estimated size

- Vendored/adapted core: ~2600 lines after stripping (from ~3900: IPU.cpp 540,
  IPU_MultiISA 2173, mpeg2_vlc 467, IPUdma 334, IPU_Fifo 225, yuv2rgb 276, IPUdither 124).
- Adapter (`ps2_ipu.cpp` + edits in `ps2_memory.cpp`, `Stubs/IPU.cpp`): ~400-600 lines.
- Total ~3000-3200 lines, of which ~2600 is upstream code kept close to source
  (eases future resync).

## Validation strategy

1. **Reference frames**: finish the ipum->ffmpeg adapter hinted in Finding 8 — parse the
   0x20-byte header + offset table, emit each frame's payload as ffmpeg `ipu` demuxer
   packets (per-frame size framing as `ipu` demuxer expects). Decoder already demuxes
   from offset 0xB90; resolve the packet rejection (likely per-frame chunk headers, or
   the picture header words the game peeks with FDEC fb=0/fb=8 must be present in-band).
   Output: PPM/PNG per frame for pixel comparison.
2. **Command-level harness**: unit-test the adapter with the exact measured init
   sequence (Finding 5 steps 2-3: SETIQ intra/non-intra tables, SETVQ CLUT, SETTH) using
   the IQ/VQ tables at 0x1721E0/0x172230 from the ELF, then a hand-built FDEC/IDEC on a
   single known macroblock; compare output RGB32 with ffmpeg's decode of the same bits.
3. **In-game**: rerun `PS2X_TRACE_IPU=1 ./run_headless.sh 40` and confirm the trace shows
   the loop *advancing* (varying IDEC values, ch3 madr marching, per-frame FDEC header
   values changing, sceIpuInit count ~= frames instead of one per vsync); screenshot
   compare the FMV vs. reference frames (e.g. via existing PS2X_SHOT_DIR support).
4. **Regression**: run the non-movie scenes (they never touch the IPU) to confirm no
   behaviour change outside FMV; verify sceIpuSync call counts stay in the same range.
5. **Retained trace**: keep PS2X_TRACE_IPU in the tree (env-gated) for debugging the
   remaining stream-framing questions.

