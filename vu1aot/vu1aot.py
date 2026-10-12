#!/usr/bin/env python3
"""Ahead-of-time compile VU1 micro code images into C++ (see ps2xRuntime/include/runtime/ps2_vu1_aot.h).

Usage: vu1aot.py <out.cpp> <image.bin>...   (images are dumped by PS2X_VU1_DUMP_DIR)

Semantics follow VU1Interpreter in PS2X_VU1_FAST mode: VF/VI/ACC results are visible to the next pair
(the VU interlocks on them), stores are immediate, MAC/status flags appear 4 pairs after the FMAC,
Q 7 (DIV/SQRT) or 13 (RSQRT) pairs after issue or at WAITQ, XGKICK copies its packet at once.
Within a pair, the lower instruction reads the registers as they were before the upper one wrote.
"""
import os
import struct
import sys

LANE = {0: 8, 1: 4, 2: 2, 3: 1}  # component -> dest bit (x y z w)
BC = 'xyzw'


def lanes(dest):
    return [c for c in range(4) if dest & LANE[c]]


def fs_(u): return (u >> 11) & 31
def ft_(u): return (u >> 16) & 31
def fd_(u): return (u >> 6) & 31
def dest_(u): return (u >> 21) & 15
def s11(i):
    v = i & 0x7FF
    return v - 0x800 if v & 0x400 else v


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- upper
def upper_code(u):
    """Returns (code computing into U[] / ACC, target) where target is 'vf<n>' or 'acc' or None.
    The code reads registers only; the caller commits U to the target after the lower op."""
    op = u & 0x3F
    d = dest_(u)
    fs, ft, fd = fs_(u), ft_(u), fd_(u)
    L = lanes(d)
    if op >= 0x3C:
        sp = ((u >> 6) & 0x1F) << 2 | (u & 3)
        if sp == 0x2F or sp == 0x30:
            return '', None  # NOP
        # ACC forms and conversions
        def acc_fmac(expr_of):
            lines = []
            for c in L:
                lines.append(f'U[{c}] = {expr_of(c)};')
            return fmac_finish(L, lines, True, d), 'acc'
        if sp <= 0x0F or 0x18 <= sp <= 0x1B:
            kind = {0: '+', 1: '-', 2: 'madd', 3: 'msub', 6: '*'}[sp >> 2 if sp < 0x10 else 6]
            b = sp & 3
            def e(c, kind=kind, b=b):
                a, t = f'N(s.vf[{fs}][{c}])', f'N(s.vf[{ft}][{b}])'
                return {'+': f'{a} + {t}', '-': f'{a} - {t}', '*': f'{a} * {t}',
                        'madd': f'N(s.acc[{c}]) + {a} * {t}', 'msub': f'N(s.acc[{c}]) - {a} * {t}'}[kind]
            return acc_fmac(e)
        simple_acc = {0x1C: ('*', 'q'), 0x1E: ('*', 'i'), 0x20: ('+', 'q'), 0x21: ('madd', 'q'), 0x22: ('+', 'i'),
                      0x23: ('madd', 'i'), 0x24: ('-', 'q'), 0x25: ('msub', 'q'), 0x26: ('-', 'i'), 0x27: ('msub', 'i')}
        if sp in simple_acc:
            kind, reg = simple_acc[sp]
            def e(c, kind=kind, reg=reg):
                a, t = f'N(s.vf[{fs}][{c}])', f'N(s.{reg})'
                return {'+': f'{a} + {t}', '-': f'{a} - {t}', '*': f'{a} * {t}',
                        'madd': f'N(s.acc[{c}]) + {a} * {t}', 'msub': f'N(s.acc[{c}]) - {a} * {t}'}[kind]
            return acc_fmac(e)
        if sp in (0x28, 0x29, 0x2A, 0x2C, 0x2D):
            def e(c, sp=sp):
                a, t = f'N(s.vf[{fs}][{c}])', f'N(s.vf[{ft}][{c}])'
                return {0x28: f'{a} + {t}', 0x29: f'N(s.acc[{c}]) + {a} * {t}', 0x2A: f'{a} * {t}',
                        0x2C: f'{a} - {t}', 0x2D: f'N(s.acc[{c}]) - {a} * {t}'}[sp]
            return acc_fmac(e)
        if sp == 0x2E:  # OPMULA
            lf, rt = [1, 2, 0, 3], [2, 0, 1, 3]
            return acc_fmac(lambda c: '0.0f' if c == 3 else f'N(s.vf[{fs}][{lf[c]}]) * N(s.vf[{ft}][{rt[c]}])')
        if 0x10 <= sp <= 0x13:  # ITOF0/4/12/15 -> vf[ft]
            scale = {0x10: '', 0x11: ' / 16.0f', 0x12: ' / 4096.0f', 0x13: ' / 32768.0f'}[sp]
            lines = [f'U[{c}] = static_cast<float>(I(s.vf[{fs}][{c}])){scale};' for c in L]
            return '\n'.join(lines), f'vf{ft}'
        if 0x14 <= sp <= 0x17:  # FTOI0/4/12/15 -> vf[ft]
            scale = {0x14: '1.0f', 0x15: '16.0f', 0x16: '4096.0f', 0x17: '32768.0f'}[sp]
            lines = [f'U[{c}] = F(ftoi(N(s.vf[{fs}][{c}]), {scale}));' for c in L]
            return '\n'.join(lines), f'vf{ft}'
        if sp == 0x1D:  # ABS -> vf[ft]
            lines = [f'U[{c}] = std::fabs(N(s.vf[{fs}][{c}]));' for c in L]
            return '\n'.join(lines), f'vf{ft}'
        raise Unsupported(f'upper special 0x{sp:02x}')
    # FD-destination ops
    def fmac(expr_of):
        lines = [f'U[{c}] = {expr_of(c)};' for c in L]
        return fmac_finish(L, lines, True, d), f'vf{fd}'

    def plain(expr_of):
        return '\n'.join(f'U[{c}] = {expr_of(c)};' for c in L), f'vf{fd}'
    if op < 0x1C:
        grp, b = op >> 2, op & 3
        a = lambda c: f'N(s.vf[{fs}][{c}])'
        t = f'N(s.vf[{ft}][{b}])'
        if grp == 0: return fmac(lambda c: f'{a(c)} + {t}')
        if grp == 1: return fmac(lambda c: f'{a(c)} - {t}')
        if grp == 2: return fmac(lambda c: f'N(s.acc[{c}]) + {a(c)} * {t}')
        if grp == 3: return fmac(lambda c: f'N(s.acc[{c}]) - {a(c)} * {t}')
        if grp == 4: return plain(lambda c: f'vmax({a(c)}, {t})')
        if grp == 5: return plain(lambda c: f'vmin({a(c)}, {t})')
        if grp == 6: return fmac(lambda c: f'{a(c)} * {t}')
    a = lambda c: f'N(s.vf[{fs}][{c}])'
    b = lambda c: f'N(s.vf[{ft}][{c}])'
    if op == 0x1C: return fmac(lambda c: f'{a(c)} * N(s.q)')
    if op == 0x1D: return plain(lambda c: f'vmax({a(c)}, N(s.i))')
    if op == 0x1E: return fmac(lambda c: f'{a(c)} * N(s.i)')
    if op == 0x1F: return plain(lambda c: f'vmin({a(c)}, N(s.i))')
    if op == 0x20: return fmac(lambda c: f'{a(c)} + N(s.q)')
    if op == 0x21: return fmac(lambda c: f'N(s.acc[{c}]) + {a(c)} * N(s.q)')
    if op == 0x22: return fmac(lambda c: f'{a(c)} + N(s.i)')
    if op == 0x23: return fmac(lambda c: f'N(s.acc[{c}]) + {a(c)} * N(s.i)')
    if op == 0x24: return fmac(lambda c: f'{a(c)} - N(s.q)')
    if op == 0x25: return fmac(lambda c: f'N(s.acc[{c}]) - {a(c)} * N(s.q)')
    if op == 0x26: return fmac(lambda c: f'{a(c)} - N(s.i)')
    if op == 0x27: return fmac(lambda c: f'N(s.acc[{c}]) - {a(c)} * N(s.i)')
    if op == 0x28: return fmac(lambda c: f'{a(c)} + {b(c)}')
    if op == 0x29: return fmac(lambda c: f'N(s.acc[{c}]) + {a(c)} * {b(c)}')
    if op == 0x2A: return fmac(lambda c: f'{a(c)} * {b(c)}')
    if op == 0x2B: return plain(lambda c: f'vmax({a(c)}, {b(c)})')
    if op == 0x2C: return fmac(lambda c: f'{a(c)} - {b(c)}')
    if op == 0x2D: return fmac(lambda c: f'N(s.acc[{c}]) - {a(c)} * {b(c)}')
    if op == 0x2E:  # OPMSUB
        lf, rt = [1, 2, 0, 3], [2, 0, 1, 3]
        return fmac(lambda c: '0.0f' if c == 3 else f'N(s.acc[{c}]) - N(s.vf[{fs}][{lf[c]}]) * N(s.vf[{ft}][{rt[c]}])')
    if op == 0x2F: return plain(lambda c: f'vmin({a(c)}, {b(c)})')
    raise Unsupported(f'upper op 0x{op:02x}')


def fmac_finish(L, lines, _flags, d):
    if not L:
        return ''
    out = list(lines)
    out.append('{ uint32_t mac = 0, st = 0, lf;')
    for c in L:
        lane = LANE[c]
        out.append(f'U[{c}] = R(U[{c}], lf); mac |= macBits(lf, {lane}); st |= lf;')
    out.append('flagFmac(t, mac, st); }')
    return '\n'.join(out)


# ---------------------------------------------------------------- lower
IALU_DELAY = set()


def lower_code(lo, pc):
    """Returns (code, info) where info has keys: branch ('static', target) / ('dyn',) / None,
    cond expression for conditional branches, delaysBranch reg for IALU writes."""
    info = {'branch': None, 'cond': None, 'bk': None, 'kick': False}
    if lo in (0, 0x8000033C):
        return '', info
    op = (lo >> 25) & 0x7F
    it, is_, id_ = (lo >> 16) & 31, (lo >> 11) & 31, (lo >> 6) & 31
    d = (lo >> 21) & 15
    L = lanes(d)
    def vi_w(reg, expr):
        return '' if reg == 0 else f'VIW({reg}, {expr});'
    if op == 0x40:
        lo6 = lo & 0x3F
        if lo6 < 0x3C:
            if lo6 == 0x30: info['bk'] = id_; return (f'{{ int32_t bkold = s.vi[{id_}]; ' + vi_w(id_, f'(int16_t)(s.vi[{is_}] + s.vi[{it}])') + f' BK({id_}, bkold); }}'), info
            if lo6 == 0x31: info['bk'] = id_; return (f'{{ int32_t bkold = s.vi[{id_}]; ' + vi_w(id_, f'(int16_t)(s.vi[{is_}] - s.vi[{it}])') + f' BK({id_}, bkold); }}'), info
            if lo6 == 0x32:
                imm = id_ - 32 if id_ & 16 else id_
                info['bk'] = it
                return (f'{{ int32_t bkold = s.vi[{it}]; ' + vi_w(it, f'(int16_t)(s.vi[{is_}] + ({imm}))') + f' BK({it}, bkold); }}'), info
            if lo6 == 0x34: info['bk'] = id_; return (f'{{ int32_t bkold = s.vi[{id_}]; ' + vi_w(id_, f'(int16_t)(s.vi[{is_}] & s.vi[{it}])') + f' BK({id_}, bkold); }}'), info
            if lo6 == 0x35: info['bk'] = id_; return (f'{{ int32_t bkold = s.vi[{id_}]; ' + vi_w(id_, f'(int16_t)(s.vi[{is_}] | s.vi[{it}])') + f' BK({id_}, bkold); }}'), info
            raise Unsupported(f'lower 0x40/{lo6:02x}')
        f2 = (lo & 3) | ((lo >> 4) & 0x7C)
        fsf, ftf = (lo >> 21) & 3, (lo >> 23) & 3
        if f2 == 0x30:  # MOVE vf[it] <- vf[is]
            return ' '.join(f'LW({it}, {c}, s.vf[{is_}][{c}]);' for c in L), info
        if f2 == 0x31:  # MR32
            return ' '.join(f'LW({it}, {c}, s.vf[{is_}][{(c + 1) & 3}]);' for c in L), info
        if f2 == 0x34:  # LQI vf[it] <- mem[vi[is]++]
            code = f'{{ const float *m = QW(s.vi[{is_}]); ' + ' '.join(f'LW({it}, {c}, m[{c}]);' for c in L)
            code += (f' if ({is_}) s.vi[{is_}] = (int16_t)(s.vi[{is_}] + 1);' if is_ else '') + ' }'
            return code, info
        if f2 == 0x35:  # SQI vf[is] -> mem[vi[it]++]
            code = f'{{ float *m = QWW(s.vi[{it}]); ' + ' '.join(f'm[{c}] = s.vf[{is_}][{c}];' for c in L)
            code += (f' s.vi[{it}] = (int16_t)(s.vi[{it}] + 1);' if it else '') + ' }'
            return code, info
        if f2 == 0x36:
            code = (f'{{ if ({is_}) s.vi[{is_}] = (int16_t)(s.vi[{is_}] - 1); const float *m = QW(s.vi[{is_}]); '
                    + ' '.join(f'LW({it}, {c}, m[{c}]);' for c in L) + ' }')
            return code, info
        if f2 == 0x37:
            code = (f'{{ if ({it}) s.vi[{it}] = (int16_t)(s.vi[{it}] - 1); float *m = QWW(s.vi[{it}]); '
                    + ' '.join(f'm[{c}] = s.vf[{is_}][{c}];' for c in L) + ' }')
            return code, info
        if f2 == 0x38:  # DIV
            return f'opDiv(t, N(s.vf[{is_}][{fsf}]), N(s.vf[{it}][{ftf}]));', info
        if f2 == 0x39:  # SQRT
            return f'opSqrt(t, N(s.vf[{it}][{ftf}]));', info
        if f2 == 0x3A:  # RSQRT
            return f'opRsqrt(t, N(s.vf[{is_}][{fsf}]), N(s.vf[{it}][{ftf}]));', info
        if f2 == 0x3B:  # WAITQ
            return 'waitQ(t);', info
        if f2 == 0x3C:  # MTIR vi[it] <- vf[is].fsf
            return vi_w(it, f'(int32_t)(int16_t)(I(s.vf[{is_}][{fsf}]) & 0xFFFF)'), info
        if f2 == 0x3D:  # MFIR vf[it] <- vi[is]
            return f'{{ float v = F((int32_t)(int16_t)(s.vi[{is_}] & 0xFFFF)); ' + ' '.join(f'LW({it}, {c}, v);' for c in L) + ' }', info
        if f2 == 0x3E:  # ILWR
            comp = L[0] if L else 3
            return vi_w(it, f'(int32_t)(int16_t)(I(QW(s.vi[{is_}])[{comp}]) & 0xFFFF)'), info
        if f2 == 0x3F:  # ISWR
            return f'{{ float *m = QWW(s.vi[{is_}]); float v = F((int32_t)(uint16_t)(s.vi[{it}] & 0xFFFF)); ' + ' '.join(f'm[{c}] = v;' for c in L) + ' }', info
        if f2 == 0x64:  # MFP
            return ' '.join(f'LW({it}, {c}, s.p);' for c in L), info
        if f2 == 0x68:  # XTOP
            return vi_w(it, '(int32_t)(s.top & 0x3FF)'), info
        if f2 == 0x69:  # XITOP
            return vi_w(it, '(int32_t)(s.itop & 0x3FF)'), info
        if f2 == 0x6C:  # XGKICK
            info['kick'] = True
            return f'env.kick(env.user, (uint32_t)(uint16_t)s.vi[{is_}]);', info
        raise Unsupported(f'lower special 0x{f2:02x}')
    imm = s11(lo)
    if op == 0x00:  # LQ vf[it] <- mem[vi[is]+imm]
        return f'{{ const float *m = QW(s.vi[{is_}] + ({imm})); ' + ' '.join(f'LW({it}, {c}, m[{c}]);' for c in L) + ' }', info
    if op == 0x01:  # SQ vf[is] -> mem[vi[it]+imm]
        return f'{{ float *m = QWW(s.vi[{it}] + ({imm})); ' + ' '.join(f'm[{c}] = s.vf[{is_}][{c}];' for c in L) + ' }', info
    if op == 0x04:  # ILW
        comp = L[0] if L else 3
        return vi_w(it, f'(int32_t)(int16_t)(I(QW(s.vi[{is_}] + ({imm}))[{comp}]) & 0xFFFF)'), info
    if op == 0x05:  # ISW
        return f'{{ float *m = QWW(s.vi[{is_}] + ({imm})); float v = F((int32_t)(uint16_t)(s.vi[{it}] & 0xFFFF)); ' + ' '.join(f'm[{c}] = v;' for c in L) + ' }', info
    if op in (0x08, 0x09):  # IADDIU / ISUBIU
        u15 = (lo & 0x7FF) | ((lo >> 10) & 0x7800)
        sign = '+' if op == 0x08 else '-'
        info['bk'] = it
        return f'{{ int32_t bkold = s.vi[{it}]; ' + vi_w(it, f'(int16_t)(s.vi[{is_}] {sign} {u15})') + f' BK({it}, bkold); }}', info
    if op == 0x15:  # FSSET
        imm12 = (((lo >> 21) & 1) << 11) | (lo & 0x7FF)
        return f'flagFsset(t, 0x{imm12:x});', info
    if op == 0x16:  # FSAND
        imm12 = (((lo >> 21) & 1) << 11) | (lo & 0x7FF)
        return vi_w(it, f'(int32_t)((s.status & 0xFFF) & 0x{imm12:x})'), info
    if op == 0x14:  # FSEQ
        imm12 = (((lo >> 21) & 1) << 11) | (lo & 0x7FF)
        return vi_w(it, f'((s.status & 0xFFF) == 0x{imm12:x}) ? 1 : 0'), info
    if op == 0x17:  # FSOR
        imm12 = (((lo >> 21) & 1) << 11) | (lo & 0x7FF)
        return vi_w(it, f'(int32_t)((s.status & 0xFFF) | 0x{imm12:x})'), info
    if op == 0x1A:  # FMAND
        return vi_w(it, f'(int32_t)(s.mac & (uint32_t)(uint16_t)s.vi[{is_}])'), info
    if op == 0x1B:  # FMOR
        return vi_w(it, f'(int32_t)(s.mac | (uint32_t)(uint16_t)s.vi[{is_}])'), info
    if op == 0x18:  # FMEQ
        return vi_w(it, f'((s.mac & 0xFFFF) == (uint32_t)(uint16_t)s.vi[{is_}]) ? 1 : 0'), info
    if op == 0x20:  # B
        info['branch'] = ('static', (pc + 8 + imm * 8) & 0x3FFF)
        return '', info
    if op == 0x21:  # BAL
        info['branch'] = ('static', (pc + 8 + imm * 8) & 0x3FFF)
        return vi_w(it, f'{(pc + 16) // 8}'), info
    if op in (0x24, 0x25):  # JR / JALR
        info['branch'] = ('dyn',)
        info['cond'] = f'((uint32_t)(uint16_t)BV({is_}) * 8u) & 0x3FF8u'
        return (vi_w(it, f'{(pc + 16) // 8}') if op == 0x25 else ''), info
    conds = {0x28: f'(int16_t)BV({is_}) == (int16_t)BV({it})', 0x29: f'(int16_t)BV({is_}) != (int16_t)BV({it})',
             0x2C: f'(int16_t)BV({is_}) < 0', 0x2D: f'(int16_t)BV({is_}) > 0',
             0x2E: f'(int16_t)BV({is_}) <= 0', 0x2F: f'(int16_t)BV({is_}) >= 0'}
    if op in conds:
        info['branch'] = ('static', (pc + 8 + imm * 8) & 0x3FFF)
        info['cond'] = conds[op]
        return '', info
    raise Unsupported(f'lower op 0x{op:02x}')


# ---------------------------------------------------------------- program
PRELUDE = r'''// Generated by vu1aot/vu1aot.py - do not edit.
#include "runtime/ps2_vu1.h"
#include "runtime/ps2_vu1_aot.h"

#include <cfenv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>

// PS2 FMAC multiplies and adds with separate rounding: no FMA contraction.
#pragma clang fp contract(off)

namespace
{
    inline uint32_t I(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
    inline int32_t Is(float f) { int32_t u; std::memcpy(&u, &f, 4); return u; }
    inline float F(uint32_t u) { float f; std::memcpy(&f, &u, 4); return f; }
    inline float F(int32_t u) { float f; std::memcpy(&f, &u, 4); return f; }
    // VU1Interpreter::normalizeOperand
    inline float N(float v)
    {
        uint32_t b = I(v);
        const uint32_t e = (b >> 23) & 0xFFu;
        if (e == 0u) b &= 0x80000000u;
        else if (e == 0xFFu) b = (b & 0x80000000u) | 0x7F7FFFFFu;
        return F(b);
    }
    // FMAC result: Z/S/U/O lane flags (bits 0..3), denormal -> 0, inf/NaN -> +/-MAX.
    inline float R(float v, uint32_t &lf)
    {
        uint32_t b = I(v);
        const uint32_t sign = b & 0x80000000u, mag = b & 0x7FFFFFFFu, e = (b >> 23) & 0xFFu;
        lf = sign ? 0x2u : 0u;
        if (mag == 0u) lf |= 0x1u;
        else if (e == 0u) { lf |= 0x5u; b = sign; }
        else if (e == 0xFFu) { lf |= 0x8u; b = sign | 0x7F7FFFFFu; }
        return F(b);
    }
    inline uint32_t macBits(uint32_t lf, uint32_t lane)
    {
        return ((lf & 1u) ? lane : 0u) | ((lf & 2u) ? lane << 4 : 0u) | ((lf & 4u) ? lane << 8 : 0u) | ((lf & 8u) ? lane << 12 : 0u);
    }
    inline float vmax(float a, float b) { return a > b ? a : b; }
    inline float vmin(float a, float b) { return a < b ? a : b; }
    inline int32_t ftoi(float v, float scale)
    {
        const double x = static_cast<double>(v) * static_cast<double>(scale);
        if (x >= static_cast<double>(std::numeric_limits<int32_t>::max())) return std::numeric_limits<int32_t>::max();
        if (x <= static_cast<double>(std::numeric_limits<int32_t>::min())) return std::numeric_limits<int32_t>::min();
        return static_cast<int32_t>(x);
    }

    struct FlagEntry
    {
        uint64_t ready;
        bool fmac;
        uint32_t mac, st, fssetImm;
    };
    struct T
    {
        VU1State &s;
        FlagEntry fl[8];
        int nfl;
        uint64_t n;   // executed pairs
        uint64_t cyc; // cycle of the current pair (pairs plus WAITQ stalls)
        float qPend;
        uint32_t qDi;
        bool qValid;
        uint64_t qReady;
        int bkReg; // VI written by the previous pair's integer ALU op (branches read the old value)
        int32_t bkVal;
        int bkNext;
        int32_t bkNextVal;
    };
    inline void commitQ(T &t)
    {
        t.s.q = t.qPend;
        t.s.status = (t.s.status & 0xFCFu) | t.qDi | (t.qDi << 6);
        t.qValid = false;
    }
    inline void commitFlags(T &t, uint64_t upTo)
    {
        int keep = 0;
        for (int k = 0; k < t.nfl; ++k)
        {
            FlagEntry &f = t.fl[k];
            if (f.ready <= upTo)
            {
                if (f.fmac)
                {
                    t.s.mac = f.mac;
                    const uint32_t cur = f.st & 0xFu;
                    t.s.status = (t.s.status & 0xFF0u) | cur | (cur << 6);
                }
                else
                    t.s.status = (t.s.status & 0x03Fu) | (f.fssetImm & 0xFC0u);
            }
            else
                t.fl[keep++] = f;
        }
        t.nfl = keep;
    }
    inline void pairBegin(T &t)
    {
        if (t.nfl) commitFlags(t, t.cyc);
        if (t.qValid && t.qReady <= t.cyc) commitQ(t);
        t.bkReg = t.bkNext; t.bkVal = t.bkNextVal; t.bkNext = -1;
    }
    inline void flagFmac(T &t, uint32_t mac, uint32_t st) { if (t.nfl < 8) t.fl[t.nfl++] = {t.cyc + 4, true, mac, st, 0}; }
    inline void flagFsset(T &t, uint32_t imm) { if (t.nfl < 8) t.fl[t.nfl++] = {t.cyc + 4, false, 0, 0, imm}; }
    inline void queueQ(T &t, float v, int latency, uint32_t di) { t.qPend = v; t.qDi = di; t.qValid = true; t.qReady = t.cyc + latency; }
    inline void waitQ(T &t)
    {
        if (!t.qValid) return;
        if (t.qReady > t.cyc) t.cyc = t.qReady;
        if (t.nfl) commitFlags(t, t.cyc);
        commitQ(t);
    }
    inline void opDiv(T &t, float num, float den)
    {
        uint32_t di = 0; float r;
        if (den == 0.0f) { di = num == 0.0f ? 0x10u : 0x20u; r = (std::signbit(num) != std::signbit(den)) ? -std::numeric_limits<float>::max() : std::numeric_limits<float>::max(); }
        else r = num / den;
        uint32_t lf; r = R(r, lf);
        queueQ(t, r, 7, di);
    }
    inline void opSqrt(T &t, float v) { queueQ(t, std::sqrt(std::fabs(v)), 7, v < 0.0f ? 0x10u : 0u); }
    inline void opRsqrt(T &t, float num, float rad)
    {
        const float den = std::sqrt(std::fabs(rad));
        uint32_t di = rad < 0.0f ? 0x10u : 0u; float r;
        if (den != 0.0f) r = num / den;
        else { di = num == 0.0f ? 0x10u : 0x20u; r = std::signbit(num) ? -std::numeric_limits<float>::max() : std::numeric_limits<float>::max(); }
        uint32_t lf; r = R(r, lf);
        queueQ(t, r, 13, di);
    }
    inline void finish(T &t)
    {
        // Like VU1Interpreter::flushPipelines: commit everything in ready order.
        while (t.nfl || t.qValid)
        {
            uint64_t next = t.qValid ? t.qReady : ~0ull;
            for (int k = 0; k < t.nfl; ++k) next = t.fl[k].ready < next ? t.fl[k].ready : next;
            if (t.nfl) commitFlags(t, next);
            if (t.qValid && t.qReady <= next) commitQ(t);
        }
    }
}

#define QW(a) (reinterpret_cast<const float *>(mem + ((static_cast<uint32_t>(a) & 0x3FFu) * 16u)))
#define QWW(a) (reinterpret_cast<float *>(mem + ((static_cast<uint32_t>(a) & 0x3FFu) * 16u)))
#define VIW(r, e) (s.vi[r] = (e))
#define BK(r, old) (t.bkNext = (r), t.bkNextVal = (old))
#define BV(r) ((r) == 0 ? 0 : ((r) == t.bkReg ? t.bkVal : s.vi[r]))
#define LW(r, c, v) do { if ((r) != 0) s.vf[r][c] = (v); } while (0)
'''


def gen_program(code, name):
    pairs = [struct.unpack_from('<II', code, pc) for pc in range(0, len(code), 8)]
    n = len(pairs)
    out = [f'static bool {name}(Vu1AotEnv &env)', '{',
           '    VU1State &s = *env.state;', '    uint8_t *mem = env.data;',
           '    T t{s, {}, 0, 0, 0, 0.0f, 0, false, 0, -1, 0, -1, 0};',
           '    float U[4];', '    (void)U;',
           '    bool taken = false; uint32_t dynTarget = 0; bool ending = false; uint32_t pc = s.pc & 0x3FF8u;',
           '    const int prevRound = std::fegetround(); std::fesetround(FE_TOWARDZERO);',
           '    static int warned = 0;',
           '    (void)warned; (void)dynTarget;',
           'dispatch:',
           '    switch (pc)', '    {']
    for i in range(n):
        out.append(f'    case 0x{i * 8:x}: goto L_{i * 8:04x};')
    out += ['    default: goto L_0000;', '    }']
    infos = []
    unsupported_pairs = 0
    bodies = []
    for i, (lo, up) in enumerate(pairs):
        pc = i * 8
        ibit, ebit = (up >> 31) & 1, (up >> 30) & 1
        try:
            ucode, target = upper_code(up)
            if ibit:
                lcode, info = f's.i = N(F((uint32_t)0x{lo:08x}u));', {'branch': None, 'cond': None, 'bk': None, 'kick': False}
            else:
                lcode, info = lower_code(lo, pc)
            ok = True
        except Unsupported as e:
            ok = False
            unsupported_pairs += 1
            info = {'branch': None, 'cond': None, 'bk': None, 'kick': False}
            ucode = lcode = ''
            target = None
            reason = str(e)
        infos.append((ok, info, ebit))
        body = [f'L_{pc:04x}:', '    pairBegin(t);', f'    if (g_vu1AotTraceHook) {{ s.cycles = t.cyc; g_vu1AotTraceHook(1, 0x{pc:x}, s); }}']
        if lcode == 'waitQ(t);':
            # WAITQ stalls the whole pair: the upper instruction already sees the new Q.
            body.append('    waitQ(t);')
            lcode = ''
        if not ok:
            body.append(f'    if (warned++ < 4) std::fprintf(stderr, "[vu1aot] unsupported pair at 0x{pc:x} ({reason})\\n");')
        if ucode:
            body.append('    ' + ucode.replace('\n', '\n    '))
        if lcode:
            body.append('    ' + lcode)
        if target and ucode:
            if target == 'acc':
                L = lanes(dest_(up))
                body.append('    ' + ' '.join(f's.acc[{c}] = U[{c}];' for c in L))
            else:
                reg = int(target[2:])
                if reg != 0:
                    L = lanes(dest_(up))
                    body.append('    ' + ' '.join(f's.vf[{reg}][{c}] = U[{c}];' for c in L))
        body.append('    ++t.n; ++t.cyc;')
        # branch issued by this pair
        br = info['branch']
        if br:
            if br[0] == 'static':
                if info['cond']:
                    body.append(f'    taken = ({info["cond"]});')
                else:
                    body.append('    taken = true;')
            else:
                body.append(f'    dynTarget = {info["cond"]}; taken = true;')
        if ebit:
            body.append('    ending = true;')
        bodies.append(body)
    # delay-slot epilogues: pair i+1 after branch pair i
    for i in range(n):
        ok, info, ebit = infos[i]
        nxt = (i + 1) % n
        if info['branch']:
            br = info['branch']
            if br[0] == 'static':
                tgt = br[1]
                bodies[nxt].append(f'    if (taken) {{ taken = false; if (ending) {{ s.pc = 0x{tgt:x}; goto done; }} goto L_{tgt:04x}; }}')
            else:
                bodies[nxt].append('    if (taken) { taken = false; pc = dynTarget; if (ending) { s.pc = pc; goto done; } goto dispatch; }')
        if ebit:
            after = ((i + 2) % n) * 8
            bodies[nxt].append(f'    if (ending) {{ s.pc = 0x{after:x}; goto done; }}')
    for i, body in enumerate(bodies):
        out += body
        if i == n - 1:
            out.append('    goto L_0000;')
    out += ['done:',
            '    finish(t);',
            '    s.vi[0] = 0; s.vf[0][0] = 0.0f; s.vf[0][1] = 0.0f; s.vf[0][2] = 0.0f; s.vf[0][3] = 1.0f;',
            '    s.ebit = false; s.branchPending = false; s.haltAfterDelaySlot = false;',
            '    std::fesetround(prevRound);',
            '    env.pairs = t.n;',
            '    return true;',
            '}']
    return '\n'.join(out), unsupported_pairs


def fnv1a(data):
    h = 0xCBF29CE484222325
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def main():
    out_path, images = sys.argv[1], sys.argv[2:]
    parts = [PRELUDE]
    regs = []
    for path in images:
        code = open(path, 'rb').read()
        h = fnv1a(code)
        name = f'vu1aot_{h:016x}'
        src, bad = gen_program(code, name)
        print(f'{os.path.basename(path)}: hash {h:016x}, unsupported pairs {bad}', file=sys.stderr)
        parts.append(src)
        regs.append((h, name))
    parts.append('namespace\n{\n    struct Register\n    {\n        Register()\n        {')
    for h, name in regs:
        parts.append(f'            ps2RegisterVu1AotProgram(0x{h:016x}ull, &{name});')
    parts.append('        }\n    } g_register;\n}\n')
    open(out_path, 'w').write('\n'.join(parts))


if __name__ == '__main__':
    main()
