# Scene 0x13 "MTTITLE D" — slot 6/7 completion (title screen advance)

Goal: find what completes flow slots 6/7 at scene 0x13, satisfy the condition the way real
hardware would, verify scene 0x13 is left, report the next scene.

## Finding 1 — Slot helper semantics (exact disassembly, main.s)

- `0x2715B8(slot)`: `return *(s8*)(0x4BDC30 + 0x78 + slot)` — read status byte
  (0x4BDCA8 + slot). 0xFF (-1) = pending, 0 = done.
- `0x2715D0(slot)`: `*(u8*)(0x4BDC30 + 0x78 + slot) = 0` — **complete** the slot.
- `0x2714F8(flag)`: `*(u8*)(flowobj + 0x70) = flag` — set the flow flag byte only
  (no slot touched).
- `0x2719F0(idx)`: getter, `return *(s8*)(0x4BDC30 + idx + 0x98)`.
- `sub_00271A08(idx, value)`: s1 = min(value, 99); writes max(progress) byte at
  0x4BDC30+idx+0x98; **if value != 0**: poll slot 6 → if -1 complete it (0x271A78) and call
  0x272600(6); then same for **slot 11** (0x271A98); then 0x271F30 etc.

## Finding 2 — Who completes slot 6 and slot 7 (generated C++)

- **Slot 6** completed only at `0x271A78` inside `sub_00271A08(idx, value)`, condition:
  `value != 0` (a progress/percent report, clamped 0..99, index != 0x17).
  Slot 11 is completed by the same call.
- **Slot 7** completed only at `0x28E68C` inside `sub_0028E628(obj, arg)`, condition:
  `(*(obj+0x18) - 1) < 2u` i.e. handler state at obj+0x18 is 1 or 2 (else it jumps to
  0x28E8EC and never completes). Gated additionally on slot 7 still == -1.
- The prompt's "completers" 0x29CFBC / 0x29D038 / 0x29D0A0 do **not** complete any slot:
  they call `0x2714F8(0)` (set flag byte) — they are scene-transition requesters, gated on
  `( *(*(obj+0x24)) & 0x20 )` (a pad/flag word bit 5) and flow-table entry+8 == 7.
- Callers of `sub_0028E628` (JAL/j): the 0x28E9xx handler family only —
  0x28E9A8 (j 0x28E628), 0x28EA68, 0x28EC50, 0x28EE10 (j). No direct caller in .text;
  the family is reached via function pointers. `sub_00271A08` callers: 0x271600,
  0x271750, 0x2727A0, 0x286210 (via 0x271F30), and itself.

## Finding 3 — Live run t1: MC silent at title, slots 6, 7 **and 11** pending

`work/run_t1.log` (35 s, PS2X_TRACE_MC=1 PS2X_TRACE_PAD=1 + peeks 0x4bdca0..0x4bdcb0,
0x42ab5c):
- MC trace: only 4 commands at boot (`cmd=1 result=0` (GetInfo), `cmd=13 result=-4`
  x3 (GetDir, "no save data")). **Zero MC commands after tick ~10.**
- Pad: read continuously (read #3901 at tick 919), buttons=ffff when unpressed.
- Peek decode (bytes): slots 0-5 = 0, **6 = 0xFF, 7 = 0xFF, 8-10 = 0, 11 = 0xFF**
  (stable). So the flow waits on entries 7 (slot 6), 8 (slot 7), 3 (slot 11) — the
  prompt's "slots 6-7" omitted slot 11. 0x4bdca0 flag word = 1 (set), value = 0.
- Scene word 0x42ab5c = 2 early (WARNING); the 3 s peeks are too coarse to see 0x13.

## Finding 4 — Flow table 0x410290: entry i -> slot = entry+0x18; next scene = entry+4

`sub_003262A0` (per-event refresh): `entry+3 = 2 if slot(entry+0x18) != -1 else 0`.
Entry bytes b0,b1 = paired item ids; b2 = class (0, entries 10/11 have 1); +4 = next
scene id; +8/+0xC/+0x10/+0x14 = params. Mapping (entry -> slot, next scene):

| entry | slot | +4 scene | params (+8,+0xC,+0x10,+0x14) |
|---|---|---|---|
| 0,1,4,5,6 | 0,1,3,4,5 | 7 | 1,1,0..8,0 |
| 2 | 2 | -1 (no transition) | 0,0,-1,-1 |
| **3** | **11** | 7 (special sub-scene) | 0x01010001,0x100,3,1 |
| **7** | **6** | **0x16 "NEW VIEW"** | 0,0,-1,-1 |
| **8** | **7** | 7 (special sub-scene) | 1,1,5,0 |
| 9 | 8 | 0xD "OPTION" | 1,1,-1,-1 |
| 10,11 | 9,10 | 7 | 0x01000101/1,1,2,0 |

`sub_00326458(idx, flag)`: if entry+4 >= 0 → set flag byte (0x2714F8), if entry+4==7
run `0x282020(*(0x4B4690), entry+0x10, &entry+8, entry+0x14)` + 0x271578, then
`sub_0032FCB8(taskmgr, entry+4)` = request the next scene. **No slot check in
0x326458** — input-driven advance does not need the pending slots.

## Finding 5 — Flow object template and who arms slots; title input decider

- `0x2713D8` (flow init): `memcpy(0x4BDC30, 0x3D1878, 0x1778)` — the slot status bytes
  come from a **template**; slots 6, 7, 11 are born -1 (armed at boot), the others
  born 0. Only 3 `jal 0x2715D0` exist in .text (0x271A78 slot 6, 0x271A98 slot 11,
  0x28E68C slot 7), so pending slots can ONLY clear through those sites.
- Slot 6's completing call originates in `sub_00286210` (item/loader loop):
  `0x28643C: v0=progress(idx); 0x271A08(idx, v0+1)` — first real progress (>= 1)
  completes slots 6 + 11. So slots 6/11 complete when sub_00286210's per-item
  processing reports progress (its condition: `*(s1+0xDB0)==1`, item idx != 0x17).
- `sub_003268D0(obj)` = the title's input decider (scene 0x13 method family):
  reads pad words at 0x42E580+0x2CC (port0 buttons) / +0x37C (port0 "new") and
  +0x2D8 / +0x388 (port1); tests bits 0x1000 (Triangle), 0x4000 (Cross), 0x20, 0x40;
  on a press it sets obj+0x35=1, obj+0x36=flag, obj+0x38=picked entry idx, and
  `sub_00327100` later tail-calls `sub_00326458(idx, flag)` → next scene requested.
  So **input-driven scene changes never touch slots 6/7/11**; the pending slots only
  gate the *event-driven* entries 3/7/8.

## Finding 6 — Button sweep at the title does nothing (run_t2)

`work/run_t2.log` (45 s): PS2X_PAD_SCRIPT pressed **all 16 buttons** sequentially
(triangle/cross/circle/square/up/down/left/right/l1/r1/select/start, 20-tick holds
from tick 620, scene 0x13 reached ~tick 575). Result: scene word 0x42ab5c = 0x13 for
every dump after boot; slots 6/7/11 stayed 0xFF. **No button leaves scene 0x13.**
(One unimplemented syscall 0x7e at boot, unrelated.)

## Finding 7 — Game pad buffer layout + long holds (run_t3)

`work/run_t3.log` (45 s): long holds triangle@620-720, cross@740-840, right@860-960,
down@980-1080, 1 s peeks of 0x42E84C (= 0x42E580+0x2CC), 0x42E8FC (+0x37C), slots,
scene.
- The game's own buffer **does** receive the presses: 32-bit words at 0x42E84C show
  0x10, 0x40, 0x2000, 0x4000 during the four holds. These are the raw masks with the
  two bytes swapped (triangle 0x1000→0x0010, cross 0x4000→0x0040, right 0x0020→0x2000,
  down 0x0040→0x4000) — the buffer holds u16 values and the runtime's DMA fill or the
  game's own copy lands them byte-swapped relative to the raw pad mask. The +0x37C
  "new" word stays 0 in every dump.
- Scene timeline (whole run): 0, 2 x12 (WARNING), 3 x5 (LOGO), **0x14 (HIDDEN ELEM,
  normal boot step — OPENING's exit requests 0x14 per Finding 5 of
  mimo_post_movie_b.md)**, then 0x13 x26 — never moves despite the holds.

## Finding 8 — Why the sweeps can't work: the title's decider never acts

Even granting the byte-swap (so the decider's tested bits correspond to Circle/Up/
Down/Cross in raw terms), run_t2 pressed all of those and nothing happened. Combined
with the previous session's per-frame hist at scene 0x13 (only `sub_0021AC30` x2 +
`sub_0032CB48` x1 per frame — the movie-player getters; the scene-0x13 method family
with the decider `sub_003268D0` never appears), the conclusion is: **the title's
input decider is not running at all**, because scene 0x13 is parked inside the
movie/attract wrapper (its per-frame code is only movie state polling). The wrapper
waits for the attract movie's load/play pipeline — which the movie skip (ret1@0x32CAD8
"already ended") and the absent IPU/stream completion events leave unfinished. That
same unfinished pipeline is exactly what would report loader progress (`sub_00286210`
→ `0x271A08(idx, >=1)`) and complete slots 6/11, and queue the port/MC request whose
processing (0x28E628, state 1/2) completes slot 7.

## Finding 9 — 0x32CAD8 caller inventory + hypothesis verdict: ret1@0x32CAD8 is NOT why the title is stuck

Callers of 0x32CAD8 (generated C++, `dispatchGuestBranch` JAL sites only): 0x200418
(sub_00200270), 0x21BA84 + 0x21BB24 (sub_0021BA40, COpening tick), 0x286E6C (sub_00286E18,
calls COpening tick), 0x289FE4 (sub_002895B0), 0x2B84B4 (sub_002B83B8). ELF32 data-pointer
scan (whole SLPS_254.41 for the word 0x0032CAD8): **zero hits** — no vtable/jump-table
indirect caller exists. Histograms at scene 0x13 (work/shots28/hist_1800..5100) show none
of the 5 caller functions running; the movie pump `sub_0032B980` calls only
0x32C9D8/0x32C9E8/0x32C9F8/0x32CA38/0x32CB90/0x32D098/0x32F3xx/0x32F430/0x32F5C8 —
never a 0x32CAD8 caller. **Hypothesis wrong**: at the title nothing queries 0x32CAD8, so
the global ret1 cannot be causing a per-frame movie restart. (It correctly skips only the
opening scene's movies; COpening's tick ran 11+7 times at frames 1200-1500 and the boot
chain completed.)

## Finding 10 — What the title actually waits for: the 8-state machine at obj+0x34 and its state-2 gate

Title object (0x54-byte, allocated in create 0x3261E0, pointer at gp+0x2ABC) has a state
byte at **+0x34** with an 8-way switch (`sub_00327038`, jump table 0x49F1BC:
0x32708c/327078/3270a0/3270b4/3270c8/3270dc/327100/327114 = out-of-line state bodies).
Sequence: state 0 init (requests 0x21a7e0(0x17), 0x21a850(0x6d), flow 0x326400) → **state 2
= wait** → 3 (teardown, 0x21ac50, sound 0x355fa0) → 4 (float timer obj+0x30) → **5 =
input** (decider sub_003268D0 via 0x3270DC; on press obj+0x35=1 → state 6 → sub_00327100 →
sub_00326458 → next scene) → 7.
State-2 body (0x326D10), runs exactly 1/frame (hist: sub_003270A0/326AF0/32F618/32B968
x268, 0x21AC30/0x357000 x536 = 2 frames^-1), advances to state 3 iff:
`sub_0032F618(0x4BD000) != 0` (gp+0x2AFC == gp+0x2B00, load-progress counters, polls
0x32EFB8) `&& sub_0032B968(0x412AD0, 0) == 0` (movie-player pump idle) `&&
sub_00357000(0x4BD228) == 0` (*(u8*)0x4BD228 != 0 && 0x356AF8(0x4BD228) == 0).

## Finding 11 — Live proof of the blocking predicate (run_t4/run_t5, 25-30 s)

Peeks at scene 0x13: `*(u32*)0x412BAC` (= movie player 0x412AD0 + 0xDC, pump state) = **6**,
stable. Pump switch (table 0x49F804): state 6 → `sub_0032BCA8` = **`return 1;`
unconditionally** (single `b` to the return tail with v0=1). So `sub_0032B968(0x412AD0,0)`
returns 1 forever and the state-2 gate can never close. The player leaves state 6 only via
the movie update `sub_0032B5C0`, which the main loop runs only when
`*(u8*)(0x412AD0+0xE6) != 0`; live peek of 0x412BB4 word 0x00000100 → **+0xE6 = 0** (movie
"active" flag clear; +0xE5 = 1). With no IPU/stream completion events the movie sits in
"playing" state 6 while inactive: pos never reaches total-2, the pump never reports idle,
state 2 never reaches the input state 5. (0x4BD228 word0 = 1 → predicate C plausible;
0x4BD000 word0 = 0 → A polls its counters; B is the one proven stuck.)

**Refined conclusion**: the title waits for its background movie to finish
(pump state 6 → idle). The existing movie skip reaches only callers of 0x32CAD8; the
title's wait path does not use 0x32CAD8 — the pump keeps its own busy verdict in
sub_0032BCA8 and its end-of-movie transition lives in the skipped movie update
(0x32B5C0 / +0xE6 pipeline). Next step (not done here): extend the game-side movie skip to
this path — e.g. in game/ufe3_overrides.cpp make the pump report idle when the movie is
inactive (+0xE6 == 0) instead of state-6-forever, or drive +0xE6/end so 0x32B5C0 runs its
real end transition — then verify 0x13 → next scene with a pad script.

## Answers (superseding the first pass where noted)

**Q1 — Completers and conditions** (from exact code, not inference):
- **Slot 6** (and slot 11, always together): completed only at `0x271A78`/`0x271A98`
  in `sub_00271A08(idx, value)` when `value != 0`. The only producer of value >= 1 is
  `sub_00286210`'s item loop at `0x28644C` (`0x271A08(idx, progress+1)`), gated on
  `*(obj+0xDB0) == 1` and item idx != 0x17. Condition = **loader/item-processing
  progress event** (timer/progress, not a button, not a card result).
- **Slot 7**: completed only at `0x28E68C` in `sub_0028E628(obj, arg)`, gated on the
  handler state `*(obj+0x18) ∈ {1,2}` and slot 7 still -1. The 0x28E9xx family that
  calls 0x28E628 is reached via function-pointer tables (0x3D58xx region) and the
  handler calls sceCdReadClock + sceMc*. Condition = **card/port request processed**
  (card-result driven).
- **Not input**: 0x29CFBC/0x29D038/0x29D0A0 only set the flag byte (0x2714F8) and
  request scene transitions; the decider sub_003268D0 fires `sub_00326458` on button
  bits without touching any slot. Slots 6/7/11 are armed by the flow-object template
  (0x3D1878 → 0x4BDC30) at boot and can only clear through the three sites above.

**Q2 — Attempted and verified state**:
- Scripted input (both exhaustive 20-tick sweep and 100-tick holds of the four tested
  buttons) provably does **not** leave scene 0x13 in this runtime: run_t2 and run_t3
  both end at scene id 0x13 with slots 6/7/11 = 0xFF (peeks above). The reason is
  Finding 8: the title's decider never runs because the attract-movie pipeline the
  scene is parked in never completes (movie skip / missing IPU + stream completion
  events — an HLE gap, exactly the class of gap the protocol allows fixing).
- **Next scene** (statically determined from the flow table, `sub_00326458` semantics):
  the first event-driven entry to clear decides it:
  - entry 7 (slot 6, cleared first by loader progress) → **scene 0x16 "NEW VIEW"**;
  - entry 8 (slot 7) / entry 3 (slot 11) → scene 7 ("----") sub-scenes via
    `0x282020(*(0x4B4690), 5/&entry+8, 0)` and `(…, 3, …, 1)`.
  So the title's normal advance is **0x13 → 0x16 "NEW VIEW"** (the attract/movie
  view), with the scene-7 sub-scenes as the alternative entries.
- Not completed in this session: a verified run showing the scene id leave 0x13. The
  minimal faithful fix (next step) is to make the movie/stream completion event fire
  the way hardware would — e.g. give the movie player's end path the completion
  callback the skipped IPU never delivers, so `sub_00286210` reports progress (slots
  6/11 → entry 7 → scene 0x16) instead of the pipeline staying inert. That touches
  either the runtime's movie/IPU stubs (tools/, off-limits here) or a small
  game-side hook in `game/ufe3_overrides.cpp` on the movie player's completion query
  target; it needs a rebuild + headless verification run beyond this session's 30
  min budget.


## Reviewer note (2026-10-04)

The fake-playback hook on 0x32CB48 (advance obj+0x134 per frame) was tested in a 100 s headless run with button presses:
the pump state at 0x412BAC stayed 6 and the scene stayed 0x13, so it was removed. The title waits for its background
movie to really play/end; next step is a real IPU decoder (port of PCSX2's IPU core into PS2Recomp).

## Finding 12 — Movie player 0x412AD0 field map (exact code + live peeks)

Getters are methods on the same object (sub_0032CAD8 passes one base to both):
- `sub_0032C9F8` "total": `ptr = *(obj+0); return *(u32*)(ptr+0xC)` — obj+0 is a **stream
  object pointer**, not a count.
- `sub_0032CAC0` "pos": `*(s32*)(obj+0x12C) >= 0 ? that : *(obj+0x134)` — 0x12C is the
  in-flight decode position, 0x134 the idle/fallback position.
- `sub_0032CAD8` "movie ended?" = `pos == total-2` (xor/sltiu equality, returns 0/1).
- `sub_0032CB48` "active" = `*(u8*)(obj+0xE6)`.
- `sub_0032B930(obj, a1, a2, a3)` = arm: `+0xD0 = a1` (frame count), `+0x12C = -1`,
  `+0xD8 = a2` (dbp), `+0x134 = a3` (start pos). `sub_0032B5B0` sets +0xE6 = 1.
- `sub_0032B5C0` = **stop/teardown**, not per-frame update: loops i<4 waiting on
  `func_32F5C8(0x4BD000, *(obj+0xFC+4i)) != 0` (busy-wait at 0x32b618..0x32b620 — the
  stall point without IPU DMA completion), frees 6 buffers (obj+0,+4..+0x30 via
  func_3315E8), then `+0xE6 = 0`, `+0x138 = -1`.
- Stream object (title): magic "ipum" (0x6D757069) at +0, `+0xC = 0x708 = 1800`
  (= total used by the ended query).

## Finding 13 — Title state: armed but never stepped (run_t5, 2 s peeks)

At scene 0x13 the player is **frozen across all dumps**: `+0x12C = -1`, `+0x134 = 0`
(so pos getter = 0), `+0xE6 = 0`, `+0xD0 = 0x79 (121)`, `+0xD8 = 0x1184 (dbp)`,
`+0x138 = 1`, `+0xD4 = 0`, `+0xDC = 6`. During OPENING (scene 4) the same object is
live: `+0x12C = 0x1d/0x1e` (advances even with no button), `+0xD0 = 0x84 (132)`,
`+0xD4 = 1`. So at the title the movie is armed (0x32B930 ran) but **no frame is ever
decoded/advanced**: the title is waiting on the movie player to finish.

## Finding 14 — Where it stalls: the movie loop sub_00231F48 has no exit without IPU

`sub_00231F48` = the movie scene's main loop ("CMovieScene::Play"): entry check
`func_2320C0() == 0 -> return -1`, then a per-iteration body (0x231fc8..0x232084:
pad read func_354A80(0x42E580), sceDmaGetChan, render double-buffer 0x34ED50/0x34EE18,
sceGsSyncPath 0x35BC98, movie step 0x34F430, `sub_0032CB48` active check and
`if (active) sub_0032B5C0` teardown, DMA-manager polls 0x32F9C0/0x32EFB8/0x32EE20,
0x355DC8) ending in `b 0x231fc8`. **The loop body contains no branch that exits** —
the only way out is a pending "movie action" slot `gp-0x7930`, consumed once per
iteration by `sub_00231F10` (tail-calls `func_279AB0(slot)`) and cleared by
`sub_00231F40`; the slot is set only by `sub_00231F08(value)` (registered by the
movie-scene setup). With no IPU the movie never reaches its end, the end/skip path
never arms gp-0x7930, and the scene parks in this loop forever — which is exactly why
the title's input decider (sub_003268D0, Finding 5/8) never runs: it lives outside
this loop. The same loop is used by the OPENING; there a button press exits through
the game's own skip path (known facts).

## Finding 15 — What "movie end" is hooked to

Callers of `sub_0032CAD8` (ended query) are the movie wrappers only (0x200270,
0x21BA40, 0x26DCF8, 0x286E18, 0x2895B0, 0x2B83B8) — none in the 0x326/0x327 title
family. The loop itself never queries "ended"; the end/skip notification reaches the
loop via the gp-0x7930 action slot (Finding 11). The end condition the wrappers use
is `pos == total-2` (title: total = 1800).

## Finding 16 — Fake playback v1 (0x32CB48 getter tick) — implemented, superseded

Implemented the smallest fake playback as a runtime hook on `sub_0032CB48` (0x0032CB48)
— called exactly once per movie-loop iteration via a real JAL from sub_00231F48, so
`runtime.replaceFunction` intercepts it (the SOUNDMAN hooks use the same mechanism):
each call advances `obj+0x134` by one frame (video rate) while `+0x12C < 0` (no real
decode in flight), capped at `*(stream+0xC) - 2` so `sub_0032CAD8` reports "ended" at
the last frame exactly as hardware does; the original `+0xE6` return is preserved.
Rebuilt (`nice -n 19 ./rebuild.sh`, clean).
Verification (run_t6, 50 s, opening skipped with circle@420-430,circle@480-490 and
button presses at the title): the fake playback is **live** — `+0x134` advances
steadily at ~56 frames/s (dumps: 0xc0, 0x131, 0x1a2, 0x214, 0x285, 0x2f7, 0x369,
0x3db, 0x44b, 0x4bd, 0x52e, 0x59e, 0x610, 0x681...) with `+0x12C` staying -1, and
the stream object resolves to the "ipum" header with total = 1800 (the cap target).
**Not yet verified**: that the title leaves 0x13 (or shows the menu) once the counter
reaches 1798 and a button is pressed — run_t7/t8 were cut short: PNG capture with a
relative PS2X_SHOT_DIR failed ("Failed to open file"), and two run_t8 attempts failed
on environmental collisions ("Permission denied" executing ps2EntryRunner while a
*concurrent, non-session* `cmake --build --target ps2EntryRunner` was relinking it at
21:48, then a stale /run/user/1000/ufe3-headless.lock from the killed attempt). The
pending check: rerun (absolute PS2X_SHOT_DIR, ~75 s, presses after the counter passes
1798) and confirm the scene word changes or the menu appears in the PNGs.
**Superseded** by Finding 17: the concurrent review session tested this getter-level
tick in a 100 s run — the pump state (+0xDC) stayed 6 and the scene stayed 0x13, and
removed the hook. Advancing the position alone cannot unstick the title because its
wait is gated on the *pump poll's return value* (sub_0032B968), not on the position.

## Finding 17 — Fake playback v2: hook the movie PUMP POLL (0x32B968), not the getter

Target chosen from the concurrent review's Finding 10/11 (the proven-blocking gate):
the title's state-2 body (sub_00326AF0 at 0x326D38) advances to the input state iff
`sub_0032F618(0x4BD000) != 0 && sub_0032B968(0x412AD0, 0) == 0 && sub_00357000(0x4BD228) == 0`,
and `sub_0032B968` (the movie player's pump poll, 12 real JAL call sites —
replaceFunction intercepts them; sub_0032B950 has no JAL callers and is left bound to
the real tail call) is stuck returning 1 because pump state +0xDC == 6 whose handler
sub_0032BCA8 returns 1 unconditionally.
Implementation (game/ufe3_overrides.cpp, `ufe3MoviePumpTick` bound at 0x0032B968):
- no stream object (obj+0 == 0) -> return 0 (idle, no movie);
- stream present but header magic "ipum" (0x6D757069) not yet parsed (movie file
  still loading, total undefined) -> return 1 (busy), as hardware would;
- otherwise fake frames: while the real decode path is idle (obj+0x12C < 0) advance
  obj+0x134 by 1 per poll (video rate; blank frames already shown on the movie plane),
  capped at total-2 with total = *(stream+0xC) = 1800 for the title movie;
- return 1 ("playing") until pos >= total-2, then 0 ("idle") = the game's own
  end-of-movie value (sub_0032CAD8 semantics), so the state-2 gate closes through the
  game's normal path and the title reaches its input state (5).
Verification pending the rebuild + headless run.

## Finding 18 — Pump-poll replacement breaks the movie pipeline (v2/v3 of the fix)

Replacing sub_0032B968 (the pump poll) itself is wrong: the pump is not a pure query —
the movie file load (stream object 0x67CE80, "ipum" header, total 1800) and the frame
pipeline only progress while the REAL pump code runs. With the poll replaced (v2/v3,
run_t9/t11/t13) the stream object stays empty/zeroed and nothing ever plays; with the
poll left real (v4) the load completes and the movie plays. (In pump state 6 the
handler sub_0032BCA8 does return 1 with no side effects, but the other states are the
pipeline driver.) Also verified: pump state out of range (>= 7 or negative, e.g. the
reset value -1 written by sub_0032B3D8) takes sub_0032B980's default path at 0x32b9e4
which **returns 0 ("idle")** — the value the title's state-2 gate needs.

## Finding 19 — v4/v5 fake playback: the movie plays AND ends (verified)

Final implementation in game/ufe3_overrides.cpp (built into the runner):
- `ufe3MovieActiveQuery` @ 0x0032CB48 (once per frame from the movie loop): advances
  the movie position by one frame per video tick (obj+0x134 while the real decode path
  is idle, obj+0x12C < 0) toward total-2 = 1798; at the end pokes the pump state
  obj+0xDC = -1 so the REAL sub_0032B968 poll reports idle (Finding 18).
- `ufe3MovieAudioStreamBusy` @ 0x00356AF8 (SOUNDMAN cmd 0x22 thunk, only caller is the
  gate's predicate-C helper sub_00357000): reports "stream busy" while the movie plays
  and "stopped" once it ends. This is what unsticks the REAL pipeline: with the audio
  reported active the game's own decoder loop starts delivering frames — run_t16/t17:
  obj+0x12C (real decode position) advances 542 -> 723 -> 923 -> ... -> 1798 with pump
  state 6, and at total-2 the position wraps to 0 (the attract movie restarts) —
  exactly hardware-like blank-frame playback at the runtime's frame rate.
- Predicate A (sub_0032F618(0x4BD000)): live counters gp+0x2AFC == gp+0x2B00 ==
  0x87 (gp = 0x4BAD70 from ELF .reginfo) => passes. B/C pass per the hooks above.

## Finding 20 — Title reaches its interactive state and accepts a button press (verified)

Title object = *(gp+0x2ABC) = 0x67CE00 (peeked via PS2X_EE_PEEK deref). Live state byte
(+0x34): 2 (wait) during the movie, then **5 (input)** for 8 consecutive dumps
(~2 min) while the attract movie keeps playing, then 7. A scripted button press during
state 5 is **accepted**: obj+0x35 = 1, obj+0x36 = 1 (flag), obj+0x38 = 0 (picked flow
entry) — the title's decider (sub_003268D0) ran and state 5 -> 6 -> 7 fired
sub_00327100 -> sub_00326458 -> sub_0032FCB8(taskmgr, entry+4).
**Next scene id = 7** (flow entry 0's +4 field, Finding 4's table: entries 0/1/4/5/6
-> scene 7 "----" special sub-scene, dispatched through 0x282020(*(0x4B4690), ...)).
Gotcha for future runs: PS2X_PAD_SCRIPT ticks advance only ~2.3/s of wall time in this
runtime (a 950 s run reaches tick ~2186), so scripts must target ticks < ~2200 for a
< 1000 s run (the opening-skip presses at 420/480 and input presses at 600-2200 are
the working range).
Remaining gap (next step): after the request for scene 7 the scene word 0x42AB5C stays
0x13 — the scene-7 sub-scene dispatch (0x282020 + its loader) is the next thing to
investigate; it may be gated on the flow slots 6/7/11 (Findings 1-4) or on its own
load pipeline. The title's movie-driven stall itself is solved: movie plays at video
rate with blank frames, ends through the game's own end value, the wait state closes,
and the title accepts input.
