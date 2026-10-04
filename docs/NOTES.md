# Technical notes

## Game layout

- Main ELF `SLPS_254.41`: MIPS R5900, symbols stripped, but ~924 `.gnu.linkonce` section names keep C++ class names
  (`COpening`, `CWarning`, `CMtitle`, `ComboData`, …). Sony SDK: libgraph, libdma, libvu0, libpkt, libpad, libmc,
  libmc2, libcdvd, libipu, liberx.
- 62 ERX relocatable modules (`ERX/PLAYER00`, `ERX/PLAYER20`, `ERX/MISN`), loaded at runtime by liberx. PS2Recomp has
  no ERX support yet; the plan is to pin load addresses and recompile each module ahead of time.
- Two VU1 microcode sets (~28 KB, `.DVP.overlay` sections).
- IOP: Sony modules plus the game's own `SOUNDMAN.IRX`, which also streams data from `FILE1-4.BIN` to EE buffers.

## Boot progress

1. IOP modules load and run in PS2Recomp's IOP emulator.
2. Memory-card check screen ("チェック中です").
3. Health-warning screen renders correctly.
4. Opening movie: the game drives the IPU (MPEG decoder) directly. PS2Recomp has no IPU emulation, so movies are
   skipped (`work/ufe3.toml`: `ret1@0x0032CAD8` movie-ended query, `ret0@0x00356B88` movie-audio-busy query).
5. **Current blocker:** black screen after the movie. A load/stream slot in the game's stream manager reaches its
   "waiting" phase without the request being sent to SOUNDMAN. Under investigation (`work/mimo_post_movie.md`).

## PS2Recomp fixes (in `patches/ps2recomp.patch`)

| Area | Problem | Fix |
| --- | --- | --- |
| IOP cdvdman | Read-completion callback fired 128 cycles after `sceCdRead`; SOUNDMAN publishes its wake-up thread id only after issuing the read, so the wake-up was lost | Completion latency ~1 ms (36,864 IOP cycles) |
| HLE heap | newlib `_memalign_r`/`_realloc_r` ran as guest code on top of the HLE `_malloc_r`/`_free_r`; memalign's "free the leading slack" freed the whole block | Bind `memalign`/`_realloc_r` to HLE (config), `_memalign_r` via game override |
| DMA | `sceDmaSend` stub forced CHCR.TIE=1, so a tag with IRQ=1 ended every chain after the first tag; real libdma keeps the channel's TIE/TTE | Preserve TIE/TTE like libdma |
| DMA | Chain walker capped at 4,096 tags; ordering-table engines chain thousands of empty tags | Cap raised to 2^18 (loop guard only) |
| GS | `sceGsExecLoadImage`/`StoreImage` multiplied the block pointer by 8 | Use libgraph's block pointer unchanged |
| Memory card | HLE commands completed instantly; the game polls `sceMcSync` right after issuing and expects "executing" | Report "executing" once before completion |
| Recompiler | Jump-table detection missed tables whose index is sign-extended (`dsll32`/`dsra32`) between the bounds check and the scaling | Follow copies/sign-extensions back to the `sltiu` |

Game-specific: `sceCdLayerSearchFile@0x35E9A8` is bound to the runtime's `sceCdSearchFile`.

## Known PS2Recomp gaps hit by this game

- No IPU (MPEG) emulation.
- No ERX module support.
- SPU2 is register storage plus faked DMA completion.
- The analyzer mislabels small newlib wrappers (`memalign`, `realloc`, …) as `setlocale`.
