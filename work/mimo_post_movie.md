# Post-movie black screen investigation (UFE3 / SLPS-25441)

Status: **ROOT CAUSE FOUND** — it is neither (a) a wrong game input nor (b) an EE-recompiler
divergence. The EE load pipeline is correct and *did* issue the SOUNDMAN RPC; the hang is
inside the IOP-side SOUNDMAN.IRX job engine, which the runtime's IOP emulation deadlocks
(and, separately, silently fails to deliver data for the other RPC flavour). Workaround
implemented in `game/ufe3_overrides.cpp` (see §7).

## 0. Inherited findings (from previous investigation — one correction, see §3)

- Movie skip is implemented via stub bindings in `work/ufe3.toml`:
  `"ret1@0x0032CAD8"`, `"ret0@0x00356B88"`.
- After the skip, each frame draws only a clear + a full-screen black fade overlay;
  the scene underneath never fades in → permanent black screen.
- The scene waits on the game's load/stream manager (per-frame calls around
  `sub_0032EFB8` / `sub_0032F050` / `sub_0032F5C8` / `sub_0032F9C0`; SOUNDMAN RPC helpers
  `sub_00356948` / `sub_00356998` / `sub_00356A18` → `sub_00356E10` → SOUNDMAN.IRX).
- `sub_0032F968` is a pure table lookup: `v0 = (lsn < 0xDE0) ? table[lsn].w1 : 0`,
  table at **0x413F98**, 16 bytes/entry: `{w0 = byte offset in FILEn.BIN, w1 = packed size,
  w2 = real size, w3 = device}`. Confirmed against the ELF (see §2).
- One stream slot (slot 13) reaches phase 2 ("waiting") but no SOUNDMAN RPC was ever
  issued for it. — **Correction: the RPC *was* issued (§3).** Phase 2 is the legitimate
  "RPC in flight" state; the RPC's IOP-side job simply never completes.

## 1. The stream/load manager (EE side, all verified against `work/re/main.s`)

`gp = 0x4BAD70` (from `.sdata` at 0x4B2D80 + 0x7FF0), so the manager globals are absolute:

| symbol | address | meaning |
|---|---|---|
| `gp+0x2AF8` | **0x4BD868** | shared tick "phase": 0 = init, 1 = issue, 2 = waiting |
| `gp+0x2AFC` | **0x4BD86C** | current queue index |
| `gp+0x2B00** | **0x4BD870** | end queue index (0x4BD86C == 0x4BD870 ⇒ queue drained) |
| manager object | 0x4BD000 | `+0` = "pump disabled" flag (`sub_0032F618` skips work when set) |
| queue entries | 0x4C0550 | 0x24-byte entries, 256 slots, indices wrap mod 256 |

Queue entry layout (0x24 bytes):

| off | meaning |
|---|---|
| +0x00 half | handler id → table at **0x413F30**: [1]=`sub_0032F050` SOUNDMAN stream tick, [2]=`sub_0032F330` no-op (mark done), [3]=`sub_0032F340` sceCdReadClock poll, [4]=`sub_0032F1B0` direct-CD tick (sceCdRead) |
| +0x02 half | done flag (1 = done) |
| +0x04 | lsn (index into the 0x413F98 table; -1 = "nothing to load") |
| +0x08 | dest buffer (EE address) |
| +0x0C | SOUNDMAN command id (= IOP job slot id; -1 = not issued) |
| +0x10 | RPC flavour: 0 = cmd 0x1A (`sub_00356948`), 1 = cmd 0x1B (`sub_00356998`) |
| +0x14 | byte offset added to `table[lsn].w0` |
| +0x18 | size override (-1 ⇒ use `table[lsn].w1`) |
| +0x1C byte | post-load flag: 1 ⇒ run LZSS decompress (`sub_0032F7B8`) temp→final, then free temp (`sub_003315E8`) |
| +0x20 | final dest for the decompress step |

Key functions (MIPS ↔ generated C++ compared instruction-by-instruction, **no divergence**):

- `sub_0032EFB8` — queue walker: calls `table[entry.h0](entry)`, then if `entry.done`
  resets phase (`sw 0, 0x2AF8(gp)`) and advances the index (mod 256). Stays on the entry
  while `done == 0`.
- `sub_0032F050` — **the SOUNDMAN stream tick** (handler 1). This is both
  **the code that moves a slot to phase 2** and **the code that should issue the RPC**:
  - phase 0/1 body (`0x32f0a8`):
    - `lsn == -1` ⇒ phase = 2 with **no RPC** (the legitimate skip path);
    - else size = (`entry+0x18 == -1`) ? `sub_0032F968(lsn)` : `entry+0x18`;
    - if `entry+0x10 == 0` ⇒ `sub_00356948(dest, (w0+off)>>11, size>>11, w3)`
      (SOUNDMAN cmd 0x1A), else `sub_00356998(...)` (cmd 0x1B);
    - `entry+0xC = <result>`; **phase = 2** (`sw 2, 0x2AF8(gp)` at `0x32f110`).
  - phase 2 body (`0x32f148`): poll `sub_00356A18(entry+0xC)` (cmd 0x1D "job done?") until
    nonzero; then post-process (decompress if `entry+0x1C != 0`), clear dest, `done = 1`.
- `sub_00356948`/`sub_00356998` — round byte offset/size to sectors, then
  `sub_00356E10(cmd 0x1A/0x1B, dest, posSectors, sizeSectors, w3)`: writes the 4 args to the
  RPC buffer at 0x4C2CC0 and `sceSifCallRpc`s SOUNDMAN.IRX; returns the word the IOP wrote
  back (= the IOP job slot id).
- `sub_00356A18(jobId)` — `sceSifCallRpc(cmd 0x1D, jobId)`, returns `(result != 0)`.
- `sub_0032F3D0` / `sub_0032F430` / `sub_0032F4E8` — job submits; write the entry fields
  and enqueue via `sub_0032F3A0`. Only difference: `entry+0x10` = 0 (cmds 0x1A) or
  1 (cmd 0x1B), and `sub_0032F430` adds the temp-buffer + LZSS post-processing for lsns
  flagged in the table at 0x426DD8.

## 2. lsn table sanity check (from the ELF)

`table[0xABE] = {w0=0x147C6800, w1=0x5000, w2=0x4860, w3=0}`. FILE1.BIN is 377 MB
(0x1796A800), and 0x147C6800 + 0x5000 = 0x147CB800 = `table[0xABF].w0` — so `w0` is a
**byte offset inside FILEn.BIN**, `w1` the packed size. SOUNDMAN adds the FILE's disc-LSN
base (0x543 for FILE1) before seeking; everything is consistent.

## 3. What slot 13 actually did (correcting the inherited finding)

`PS2X_IDLE_DUMP=2000 PS2X_EE_PEEK=0x4bd860,...` on the previous run (`work/run_diag2.log`)
shows, at the hang (identical across the last three dumps):

```
[idle-dump] ee 004bd860: 00000000 00000000 00000002 0000000d 00000010 ...
```

i.e. **phase = 2, cur = 13, end = 16** — the queue is stuck on entry 13. Entry 13 at
0x4C0724:

```
[idle-dump] ee 004c0724: 00000001 00000abe 00bfccc0 00000001 00000000 00000000 ffffffff 00000000 00000000
```

| field | value | meaning |
|---|---|---|
| h0/done | 1 / 0 | handler 1 (SOUNDMAN stream tick), not done |
| lsn | 0xABE | `table[0xABE]` = FILE1.BIN @ 0x147C6800, size 0x5000 |
| dest | 0xBFCCC0 | |
| **cmd id** | **1** | **the RPC *was* issued** — `sub_00356948` returned 1 = SOUNDMAN job slot 1 |
| +0x10 | 0 | cmd 0x1A (SOUNDMAN job type 5) |
| +0x18 | -1 | size from table |

So the pipeline up to and including the RPC is healthy; the tick is correctly parked in
phase 2 polling cmd 0x1D, and cmd 0x1D never reports done. (Entries 0..12 completed with
`+0x10 = 1` ⇒ cmd 0x1B / job type 6.)

## 4. The IOP side: SOUNDMAN.IRX's job engine (disc/SOUNDMAN.IRX, disasm in work/re/soundman.s)

RPC dispatcher at 0x164, jump table at 0x71E0 (fno < 0x29):

- fno 0x1A → `sub_5A48(type=5, prio=0x1e, args)`; fno 0x1B → `sub_5A48(type=6, ...)`;
  fno 0x1D → `sub_5E88(slotId)` ("done?"); fno 0x24 → `sub_3470` (movie audio stream).
- `sub_5A48` allocates a job slot (`sub_59DC`), fills the job (`sub_61E4`: type, prio,
  pos, size, dest, w3) and starts it (`sub_634C` → WakeupThread). Slot table at
  IOP 0x486D8 `{job*, active}` (what `PS2X_SOUNDMAN_PEEK` prints).
- Each slot runs **its own IOP thread** on `sub_6568(job)` — a state machine (states 0..8,
  jump table at 0x7330) using the module's imports: cdvdman (sceCdRead/Seek/Sync/GetError),
  sifman (sceSifSetDma/sceSifDmaStat), thbase (SleepThread/WakeupThread/...),
  thsemap (SignalSema idx 6 / WaitSema idx 8), sysclib memcpy, intrman.
- State flow: 0 (acquire shared sema 0x486C0, SleepThread, WaitSema) → 1 (`sceCdSeek`)
  → 2 (`sceCdSync`) → 3 (`sceCdRead` into the slot's IOP staging buffer) →
  4 (SleepThread until the cdvd callback wakes the tid published at 0x486D4;
  then **SignalSema**) → 5 (transfer chunk: **type&3 == 1 ⇒ sceSifSetDma IOP→EE**,
  **type&3 == 2 ⇒ sysclib memcpy**) → 6 (sceSifDmaStat / done) → 7 (**WaitSema**) → 1 …
  until progress ≥ size ⇒ state 8 (done: `sub_64F8` = `state == 8`).
- `sub_5E88` returns 1 when `job.state == 8` (then marks the slot free), else
  `DelayThread(1000)` and 0.

## 5. The stall: job slot 1 is stuck in state 7 with `job+0x3C == 0`

`PS2X_SOUNDMAN_PEEK` at the hang (last 12 dumps identical):

```
sm slot=1 job=00127e20 active=1 flags=00000005 state=7 w3c=00000000 | 0000001e 000000a0 000294d0 0000000a
```

- `flags=5` = job type 5 ⇒ this IS entry 13's job (cmd 0x1A).
- `job[3]=0x294d0` = pos 0x28F8D + FILE1 base LSN 0x543 ✓, `job[4]=0xa` = 0x5000>>11
  sectors ✓ — the request parameters are **correct** (not lsn == -1, not size 0).
- `state=7` and `w3c(job+0x3C)=0`: the state-7 handler's **first** action is
  `WaitSema(job->sema)` (thsemap idx 8 at 0x7198), and only after it returns does it set
  `job+0x3C = 1`. `job+0x3C == 0` ⇒ **the thread is blocked inside that WaitSema**.
  The semaphore is the module-global token at 0x486C0 shared by all job slots.

Token protocol: state 4 signals (+1) after each read; state 7 waits (-1) before the next
chunk; state 0/8 also signal/wait around job start/end. The threads get parked mid-protocol
on job free (state 8 parks in SleepThread with a pending WaitSema) and resume on slot reuse
(sometimes skipping state 0's acquire), and the two slots' threads share one semaphore —
so under the emulator's scheduling one extra WaitSema strands the token and the last
consumer blocks forever with nobody left to signal. The same class of bug was already hit
once before (see `docs/NOTES.md`: "SOUNDMAN publishes its wake-up thread id only after
issuing the read, so the wake-up was lost", worked around with a 1 ms cdvd latency).

## 6. Verdict: (a) no, (b) no — it is an IOP-emulation gap (plus a second one behind it)

- **(a) is disproven**: the input (lsn 0xABE, size, dest, file index) is valid and the game
  did not take the `lsn == -1` skip path; the RPC was issued (entry+0xC = 1, and the IOP
  job's parameters match `table[0xABE]` exactly).
- **(b) is disproven** for the load pipeline: `sub_0032EFB8`, `sub_0032F050`,
  `sub_0032F1B0`, `sub_0032F330/340`, `sub_0032F3D0/430/4E8`, `sub_0032F968`,
  `sub_00356948/356998/356A18/356E10` were all compared instruction-by-instruction with
  `work/re/main.s` (branch targets, delay slots incl. likely-branch annulment, the mod-256
  index wrap, the `slti/movn` round-up clamps) — the generated C++ matches the MIPS.
- **Root cause**: the runtime's IOP emulation deadlocks SOUNDMAN's job-engine semaphore
  handshake (§5). A second, independent IOP-emulation gap sits behind it: the type-6
  (cmd 0x1B) jobs complete via IOP-side `memcpy` to the EE dest, but IOP RAM is a separate
  2 MiB space (`iop_memory.cpp`: writes outside `RamSize` are dropped) — so those 13
  completed loads **silently delivered nothing** to EE RAM (dests like 0x1782C0). Only the
  type-5 path (`sceSifSetDma`, which writes guest memory) can deliver data in this runtime,
  and its job deadlocks. Neither issue is game-side logic, and neither can be fixed by
  editing `tools/PS2Recomp` (forbidden) — hence the game-side override below.

## 7. Workaround (game-side override)

`game/ufe3_overrides.cpp` now replaces the two SOUNDMAN RPC issuers and the "job done?"
query with a synchronous, EE-side implementation of the same request:

- `sub_00356948` / `sub_00356998` (`0x356948`/`0x356998`) → same handler: read
  `size` sectors from FILEn.BIN at `pos` sectors (args as computed by the untouched game
  code) straight into the EE dest buffer, return job id 1.
- `sub_00356A18` (`0x356A18`) → always "done" (1).

This preserves the game's own semantics (including the LZSS temp-buffer/decompress path —
the compressed stream is delivered to the temp buffer exactly as SOUNDMAN would have),
while bypassing only the broken IOP job engine. All queue/phasing code in
`sub_0032F050` runs unchanged.

## 8. Evidence / verification

→ See **Findings 15–16** below for the rebuild + 60 s headless verification run
(work/run_post6.log, work/shots_post5/): queue fully drained (cur == end == 0x14),
entry-13 data present at 0x00BFCCC0, EE alive in scene code.

---

## Finding 10 (run-4 merge note — provenance of the sections above)

An earlier instance of this same task rewrote this file wholesale at 17:29 (and
`game/ufe3_overrides.cpp` at 17:31), clobbering the `## Finding 0/1` sections a concurrent
instance had appended minutes before. The §0–§8 content above is kept: it is consistent with
the independent re-derivation below and adds EE-side detail (queue entry layout, lsn table,
override). The findings below are the independent SOUNDMAN.IRX re-derivation (Q1/Q2 answers),
re-added after the clobber. 0x7144 (called before every WaitSema) = thbase import #0x18 =
**SleepThread** — matches §4's "SleepThread until the cdvd callback wakes the tid published
at 0x486D4".

## Finding 11 (Q1 — who signals job+0x14)

Complete call-site census of the thsemap imports in SOUNDMAN (work/re/soundman.s):
- **SignalSema (0x7188)**: RPC/worker region 0x362C, 0x3D4C, 0x3E9C, 0x3F78, 0x3FB0, 0x3FE8,
  0x4018, 0x4050, 0x409C, 0x40E8, 0x414C, 0x41C0, 0x420C, 0x42C4 — **all of these signal the
  module-global worker doorbell** (word at lui-0x5/-0x7960 = IOP 0x486A0 after reloc; §5 calls
  the job token 0x486C0 = lui-0x5/-0x7940), NOT job+0x14; plus job state machine sites
  **0x6604 (state 0, only if job->0x3C==1)**, **0x6738 (state 4, after preparing a chunk)**,
  **0x687C (state 8, job done)**.
- **iSignalSema (0x7190)**: 0x55B8 = SIF-DMA-done callback 0x5594(chan, arg) →
  iSignalSema(*(u32*)arg); registered at 0x4B40 as (chan=1, func=0x5594, arg=&worker_sema_var)
  via wrapper 0x6F6C → it signals the **worker doorbell**. 0x5624 = EE-command/IRQ handler
  0x55E4 (registered at 0x4B60 via 0x6F74): if (flags&2) iSignalSema(worker doorbell), then
  obj->0x38++ bookkeeping. Neither touches job+0x14.
- **WaitSema (0x7198)**: 0x3F0C (worker loop 0x3ED4 on the worker doorbell),
  0x6630 (state 0), 0x682C (state 7 — the stuck one), 0x689C (state 8).

**Answer: no RPC command and no callback signals job+0x14.** The only signalers are the job's
own state machine (states 0/4/8). job init 0x61E4 (called from cmd 0x1A handler @ 0x5BA8 and
cmd 0x1B handler @ 0x5D88) receives the sema id as arg 4 and both callers pass the same
module-global word (0x486C0) — the semaphore created ONCE at module init (sub_5700 →
CreateSema @ 0x5730) with iop_sema_t {attr=1, init_count=1 (fp+0x20), max_count=1 (fp+0x24)}.
So **job+0x14 is one shared mutex-style token (init=1, max=1) for all job slots** — exactly
what §5 describes ("the module-global token at 0x486C0 shared by all job slots"), here
confirmed from the sema creation arguments and the job-init callers.

## Finding 12 (Q2 — the EE side never has to signal anything; the token is IOP-internal)

Since job+0x14 is never signalled from any RPC handler, there is **no "chunk consumed"
RPC/callback from the EE** for a type-5 job: the EE's only interactions with the job are
cmd 0x1A/0x1B (submit) and cmd 0x1D (sub_5E88 "done?": `type&8==0 && state==8`, else
DelayThread(1000) and 0 — polled by `sub_00356A18` from `sub_0032F050` phase 2 at 0x32f148).
The semaphore protocol is purely IOP-side pacing: state 4 **releases** the token after
preparing each chunk (before the SIF DMA of states 5/6), state 7 **re-acquires** it after the
DMA completed (sub_6DA8 polls sceSifDmaStat; negative ⇒ done ⇒ job->0x38 += job->0x4C,
state 7), and states 0/8 release/re-acquire around job start/end. job->0x3C = "token
currently acquired". The type-5 SIF DMA itself (sub_6BA8) is a single SifDmaTransfer_t
{src = job->0x28 + job->0x50*0x800, dst = job->0x18 + job->0x38*0x800, size = job->0x4C*0x800,
attr=0}, SifSetDma(desc,1) → job+0x24 = dma id; no second descriptor, no status word written
to EE memory, no EE callback — so the EE has no condition to observe and nothing to send.
This is why "presumably an EE request signals it" (Finding 0) is wrong: with max_count=1 and
one shared token, one unmatched WaitSema (thread parked mid-protocol on job free per §5, or a
lost wakeup in the IOP emulator's SignalSema) strands the token and the last consumer blocks
forever — the observed state-7 hang.

## Finding 13 (Q3, part 1 — existing evidence; fresh run planned)

Old runs with `PS2X_TRACE_SIFDMA=1` (work/run13.log, run14.log — early-boot era, before the
movie skip) show the runtime's IOP→EE SIF DMA path works in general and logs every transfer,
e.g. `[trace:sifdma] iop_ra=00053bb4 src=00127e80 dst=00633bc0 size=20480 attr=00000000`
(these runs contain no transfer to 0x00BFCCC0 because they never reached the post-movie
scene). run_diag2.log (the hang run) has EE_PEEK/SOUNDMAN_PEEK but no TRACE_SIFDMA. Fresh
run with all traces on the *unmodified* path follows below to answer Q3 directly.

## Finding 14 (Q3, part 2 — answer, from evidence + the state machine)

- The type-5 chunk's SIF DMA **was issued and reported complete on the unmodified path**:
  the stuck job is observed in state 7, and state 7 is only reachable via state 5
  (sub_6BA8 → SifSetDma(desc,1) with dst = job->0x18 + job->0x38*0x800 = 0x00BFCCC0) and
  state 6 (sub_6DA8 → sceSifDmaStat, which only returns "done" on negative status). Old
  TRACE_SIFDMA logs (run13/14) show the runtime logs and completes IOP→EE SIF DMAs in
  general; no TRACE_SIFDMA run of the post-movie scene exists (run_diag2 predates the
  switch), so the literal bytes-at-0x00BFCCC0 readout for the pre-fix path was not captured.
- The EE-side condition from Q2 **does not exist and never becomes true**: there is no
  status word, no sceSifDmaStat on the EE side and no callback — the EE only polls
  SOUNDMAN cmd 0x1D (`sub_00356A18` at `0x32f148` in `sub_0032F050`), which stays 0 because
  the IOP job engine is deadlocked before ever reaching state 8. That is the whole hang.

## Finding 15 (Q4 — root cause and fix)

**Root cause: the runtime's IOP emulation (thsemap/thread handling around SOUNDMAN's job
engine), not the game and not the SIF DMA emulation.** SOUNDMAN's job state machine uses one
shared mutex-style semaphore (init=1, max=1 — confirmed by the live `[idle-dump] sema=N
count=? max=1` lines) as a strict token: state 4 releases it before each chunk's SIF DMA,
state 7 re-acquires after sceSifDmaStat says done. Under the emulator's IOP scheduling the
token accounting breaks (threads parked mid-protocol on job free per §5 / lost SignalSema
wakeup), one unmatched WaitSema strands the token, and the last consumer (entry 13's type-5
job) blocks forever in state 7 → cmd 0x1D never reports done → the scene's black fade never
lifts. A second, independent gap (§6): type-6 jobs' memcpy target is IOP RAM, so those
completed loads silently delivered nothing to EE RAM. Neither is fixable in the game's own
code and `tools/PS2Recomp` is off-limits — the exact runtime fixes to describe upstream:

1. `tools/PS2Recomp/.../iop_kernel/thsemap` (SignalSema/WaitSema): make SignalSema always
   hand the count to a queued waiter (currently a signal issued while a waiter is between
   "ready" and "blocking" is dropped, stranding max=1 tokens); and ensure DeleteSema/job-free
   paths can't leave a thread parked with a pending WaitSema that steals the next token.
2. `iop_memory.cpp` (IOP RAM writes outside `RamSize` are dropped): SOUNDMAN's type-6 path
   memcpy's to an *EE* destination — the runtime must translate/mirror those writes into
   guest EE RAM (or reject them loudly instead of silently dropping data).

**Game-side fix (implemented in `game/ufe3_overrides.cpp`)**: replace exactly the three
SOUNDMAN RPC entry points — `sub_00356948`/`sub_00356998` (0x356948/0x356998) → synchronous
`loadFileRangeToEe` (reads FILE1-4.BIN at the guest-computed sector range straight into the
EE dest, returns job id 1 / -1) and `sub_00356A18` (0x356A18) → always "done" — leaving all
queue walking, phasing and the LZSS decompress step untouched.

**Verification (60 s headless run, `work/run_post6.log`, shots in `work/shots_post5/`, run
with PS2X_SOUNDMAN_PEEK + PS2X_EE_PEEK=0x4bd868,0x4bd86c,0x4bd870,0xbfccc0):**
- Queue manager at the end of the run: `ee 004bd868: 00000000 00000014 00000014` ⇒
  phase=0, cur=0x14, end=0x14 — **the queue drained completely** (previously frozen at
  phase=2/cur=13/end=16).
- `ee 00bfccc0: 004d4f50 00000040 00000100 ...` ⇒ the entry-13 destination contains real
  data ("POM" magic + directory fields) delivered by the override.
- No `[ufe3]` error lines (all stream loads succeeded silently), SOUNDMAN job slots idle for
  the load path; a *different* SOUNDMAN job (flags=0x12, a sound-stream job) runs healthily
  through states of the job engine (slot 0 state=3, w3c=1), and the EE has 2 threads alive
  with PC=0x00323008 (scene/stream code) at 6.7 G cycles — the game progressed well past
  the previous hang point.
- **PNGs**: `work/shots_post5/frame_120..3360.png` were written every 120 frames, but the
  runner's "Runtime Debugger" overlay is composited over the whole 640x448 capture (visible
  in frame_960 and frame_3360), so the fade itself cannot be judged visually from them; the
  register snapshots differ between frames (r6=0x005EDF10 etc., PC/RA steady at 0x00323008),
  and the draw history per frame (`[shot:gs] draw ... prim=6` quad) is unchanged. The hard
  state evidence above (queue drained + data at 0x00BFCCC0 + EE alive in scene code) is the
  verification; a follow-up run with the debugger overlay hidden would give visual
  confirmation of the fade-in.

## Finding 16 (housekeeping)

- The verification run initially failed to write PNGs because `PS2X_SHOT_DIR` must be an
  absolute path (relative paths resolve against the runner's cwd and fail with
  "Failed to open file").
- The 80 s run left the private headless kwin/Xwayland session running (run_headless.sh's
  process-group kill missed it: PIDs of `dbus-run-session -- kwin_wayland --socket
  ufe3-headless`); it was terminated and the stale `/run/user/1000/ufe3-headless*`
  lock/socket removed before the final run. No background processes remain.
