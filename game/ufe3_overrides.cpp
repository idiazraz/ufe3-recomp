// Ultraman Fighting Evolution 3 (SLPS-25441) runtime overrides.
#include "game_overrides.h"
#include "ps2_runtime.h"

#include <iostream>

namespace
{
    void applyUfe3(PS2Runtime &runtime)
    {
        // newlib _memalign_r carves the aligned block out of a _malloc_r chunk and _free_r's the
        // leading slack; with the HLE heap that frees the whole block. memalign@0x3A0198 is bound in
        // ufe3.toml, but other wrappers tail-jump straight to _memalign_r, which has no function start.
        if (!ps2_game_overrides::bindAddressHandler(runtime, 0x003A01C0u, "memalign_r"))
        {
            std::cerr << "[ufe3] failed to bind _memalign_r@0x3A01C0" << std::endl;
        }
    }
}

PS2_REGISTER_GAME_OVERRIDE("Ultraman Fighting Evolution 3", "SLPS_254.41", 0x0020001Cu, 0x80E63037u, applyUfe3);
