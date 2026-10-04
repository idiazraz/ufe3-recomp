// Ultraman Fighting Evolution 3 (SLPS-25441) runtime overrides.
#include "game_overrides.h"
#include "ps2_runtime.h"
#include "runtime/ps2_memory.h"

#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace
{
    void applyUfe3(PS2Runtime &runtime);

    // ---------------------------------------------------------------------------
    // SOUNDMAN load bypass.
    //
    // The game's stream/load queue (sub_0032F050, entries at 0x4C0550) sends bulk
    // FILE1-4.BIN reads to the game's SOUNDMAN.IRX over SIF RPC:
    //   sub_00356948 = cmd 0x1A, sub_00356998 = cmd 0x1B ("load file range"),
    //   sub_00356A18 = cmd 0x1D ("is job done?").
    // The EE-side pipeline is correct, but the runtime's IOP emulation cannot run
    // SOUNDMAN's job engine to completion (the per-slot worker threads deadlock on
    // the module's shared semaphore, job state 7's WaitSema).  That is a runtime gap,
    // so we replace just these three RPC entry points with a synchronous
    // implementation of the same request.  All queue walking, phasing, and the LZSS
    // post-load step (sub_0032F7B8 via the temp buffer) are untouched and behave as
    // before.
    //
    // Incoming arguments (same as the guest functions receive):
    //   a0 = EE destination, a1 = byte offset inside FILEn.BIN,
    //   a2 = byte size, a3 = file index (0..3 -> FILE1..FILE4).
    // The guest helpers sectorize a1/a2 exactly like this before the RPC:
    //   sectors = ((int32_t)x < 0 ? x + 0x7FF : x) >> 11
    // ---------------------------------------------------------------------------
    constexpr uint32_t kCdSectorSize = 2048u;
    constexpr uint32_t kSoundmanFileBaseTable = 0x000486C4u; // SOUNDMAN.IRX: FILEn.BIN base LSN, indexed by w3

    uint32_t toSectors(uint32_t value)
    {
        const int32_t signedValue = static_cast<int32_t>(value);
        return static_cast<uint32_t>(signedValue < 0 ? signedValue + 0x7FF : signedValue) >> 11;
    }

    bool loadFileRangeToEe(PS2Runtime &runtime,
                           uint8_t *rdram,
                           uint32_t dest,
                           uint32_t posSectors,
                           uint32_t sizeSectors,
                           uint32_t fileIndex)
    {
        if (sizeSectors == 0u)
        {
            return true;
        }
        const uint64_t byteCount64 = static_cast<uint64_t>(sizeSectors) * kCdSectorSize;
        const uint64_t fileOffset = static_cast<uint64_t>(posSectors) * kCdSectorSize;

        if (fileIndex > 3u)
        {
            std::cerr << "[ufe3] stream load: bad file index " << fileIndex << std::endl;
            return false;
        }

        const std::string fileName = "FILE" + std::to_string(fileIndex + 1u) + ".BIN";
        const std::filesystem::path filePath = PS2Runtime::getIoPaths().cdRoot / fileName;

        std::ifstream file(filePath, std::ios::binary);
        if (!file.is_open())
        {
            std::cerr << "[ufe3] stream load: cannot open " << filePath.string() << std::endl;
            return false;
        }
        file.seekg(static_cast<std::streamoff>(fileOffset), std::ios::beg);
        if (!file.good())
        {
            std::cerr << "[ufe3] stream load: seek failed in " << filePath.string()
                      << " at 0x" << std::hex << fileOffset << std::dec << std::endl;
            return false;
        }

        std::vector<uint8_t> data(static_cast<size_t>(byteCount64));
        file.read(reinterpret_cast<char *>(data.data()), static_cast<std::streamsize>(data.size()));
        if (!file.good())
        {
            std::cerr << "[ufe3] stream load: short read from " << filePath.string()
                      << " at 0x" << std::hex << fileOffset << " len=0x" << data.size() << std::dec << std::endl;
            return false;
        }

        // cmd 0x1B jobs (SOUNDMAN's own "copy into the buffer allocated by cmd 1")
        // usually target IOP RAM (e.g. 0x1782C0), cmd 0x1A jobs target EE RAM
        // (e.g. 0xBFCCC0); a few cmd 0x1B jobs carry EE destinations as well.
        // Deliver each range to the address space the address belongs to.
        const uint32_t physical = dest & 0x1FFFFFFFu;
        if (physical < 0x00200000u)
        {
            if (!runtime.writeIopMemory(physical, data.data(), data.size()))
            {
                std::cerr << "[ufe3] stream load: IOP write failed at 0x" << std::hex << dest << std::dec << std::endl;
                return false;
            }
            return true;
        }

        const uint32_t destOffset = dest & PS2_RAM_MASK;
        const uint64_t maxBytes = static_cast<uint64_t>(PS2_RAM_SIZE - destOffset);
        if (data.size() > maxBytes)
        {
            std::cerr << "[ufe3] stream load clipped: dest=0x" << std::hex << dest
                      << " size=0x" << data.size() << std::dec << std::endl;
            data.resize(static_cast<size_t>(maxBytes));
        }
        std::memcpy(rdram + destOffset, data.data(), data.size());
        return true;
    }

    void ufe3SoundmanLoad(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        const uint32_t dest = getRegU32(ctx, 4);  // a0
        const uint32_t pos = getRegU32(ctx, 5);   // a1: byte offset inside FILEn.BIN
        const uint32_t size = getRegU32(ctx, 6);  // a2: byte size
        const uint32_t fileIndex = getRegU32(ctx, 7); // a3: w3 (0..3)

        const uint32_t posSectors = toSectors(pos);
        const uint32_t sizeSectors = toSectors(size);

        const bool ok = loadFileRangeToEe(*runtime, rdram, dest, posSectors, sizeSectors, fileIndex);
        // The real SOUNDMAN returns its job slot id (1) on success, -1 on failure;
        // sub_0032F050 stores it into the queue entry and polls sub_00356A18 with it.
        setReturnS32(ctx, ok ? 1 : -1);
    }

    void ufe3SoundmanLoadDone(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        // cmd 0x1D ("is job done?"): the load above completed synchronously.
        // The real query returns nonzero when done; sub_00356A18 then reports 1.
        (void)rdram;
        (void)runtime;
        setReturnS32(ctx, 1);
    }

    void applyUfe3(PS2Runtime &runtime)
    {
        // newlib _memalign_r carves the aligned block out of a _malloc_r chunk and _free_r's the
        // leading slack; with the HLE heap that frees the whole block. memalign@0x3A0198 is bound in
        // ufe3.toml, but other wrappers tail-jump straight to _memalign_r, which has no function start.
        if (!ps2_game_overrides::bindAddressHandler(runtime, 0x003A01C0u, "memalign_r"))
        {
            std::cerr << "[ufe3] failed to bind _memalign_r@0x3A01C0" << std::endl;
        }

        // See the SOUNDMAN load bypass above.
        if (!runtime.replaceFunction(0x00356948u, &ufe3SoundmanLoad) ||
            !runtime.replaceFunction(0x00356998u, &ufe3SoundmanLoad) ||
            !runtime.replaceFunction(0x00356A18u, &ufe3SoundmanLoadDone))
        {
            std::cerr << "[ufe3] failed to bind SOUNDMAN load overrides" << std::endl;
        }
    }
}

PS2_REGISTER_GAME_OVERRIDE("Ultraman Fighting Evolution 3", "SLPS_254.41", 0x0020001Cu, 0x80E63037u, applyUfe3);
