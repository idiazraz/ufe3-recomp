# ufe3-recomp
<img width="850" height="274" alt="6796f650a074ef60bd6d33a1b59054a0" src="https://github.com/user-attachments/assets/d5fd326c-700c-41d3-bcb2-88c7ee07a206" />

A static recompilation of **Ultraman Fighting Evolution 3** (PlayStation 2, Banpresto/Metro, Japan 2004, `SLPS-25441`)
to native Linux code, built on [PS2Recomp](https://github.com/ran-j/PS2Recomp).

> **Status: early, not playable.** The game boots through its IOP modules and sound driver, passes the memory-card
> check, renders the health-warning screen, and skips the opening movie. It currently stops on a black screen
> before the title. No 3D (VU1) rendering yet. See [docs/NOTES.md](docs/NOTES.md).

This repository contains **no game data**. You need your own copy of the game.

## What's here

| Path | Purpose |
| --- | --- |
| `work/ufe3.toml` | PS2Recomp config: function bindings for this game (HLE stubs, skips) |
| `game/ufe3_overrides.cpp` | Game-specific runtime override, keyed to the `SLPS_254.41` ELF |
| `patches/ps2recomp.patch` | Fixes and debugging aids for PS2Recomp, against upstream `c5a9d02` |
| `setup.sh` | Clones PS2Recomp at the pinned commit, applies the patch, builds the tools |
| `rebuild.sh` | Recompiles the ELF and builds the native runner |
| `run.sh` / `run_headless.sh` | Run the game (windowed, or inside a private virtual KWin session) |
| `docs/` | Technical notes and investigation reports |

## Requirements

- Linux x86-64 with a CPU supporting x86-64-v3 (AVX2), clang, CMake, Ninja
- Your own disc image of *Ultraman Fighting Evolution 3* (Japan, `SLPS-25441`)
- Optional: `kwin_wayland` for `run_headless.sh`; gdb for debugging

## Build

```bash
./setup.sh
```

Extract the disc image into `disc/` (keep the original file names, so you get `disc/SLPS_254.41`, `disc/SYSTEM.CNF`,
`disc/ERX/`, `disc/IRX/`, `disc/FILE1.BIN` … `disc/FILE4.BIN`), for example with `7z x -odisc your.iso`.

```bash
./rebuild.sh
```

```bash
./run.sh
```

## Debugging switches

The patched runtime understands these environment variables:

| Variable | Effect |
| --- | --- |
| `PS2X_SHOT_DIR=<dir>`, `PS2X_SHOT_EVERY=<n>` | Save the window's own frame every n frames, plus GS draw history and (with `-DPS2X_FUNC_HISTOGRAM=ON`) per-function call counts |
| `PS2X_IDLE_DUMP=<ms>` | Periodically print EE thread, semaphore and event-flag state |
| `PS2X_EE_PEEK=addr[*],…` | Dump EE RAM words (`*` dereferences first) with the idle dump |
| `PS2X_SOUNDMAN_PEEK=1` | Dump the game's sound-driver job slots from IOP RAM |
| `PS2X_HIDE_DEBUG_UI=1` | Start with the runtime debug panel hidden (F1 toggles) |
| `PS2X_TRACE_{DBUFF,SIFDMA,HEAP,GSXFER,LOADIMAGE,MC}=1` | Targeted traces |

## License

GPL-3.0, matching PS2Recomp. Ultraman and the game are trademarks and copyrights of their owners; this project is
not affiliated with them.
