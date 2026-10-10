/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * The i386 instructions neither the DBT nor its host fallback
 * (src/pw_x86_hostexec.c) runs, done here on the guest context when the
 * Unix side stops with PW_WOW_UNSUPPORTED:
 *
 * - segment registers: push/pop of FS and GS, mov to and from any of them
 *   (register and memory forms), LES/LDS/LSS/LFS/LGS. Watcom-built games
 *   (Red Alert) save FS and GS in their prologues. Under WoW64 the
 *   selectors are all a segment is: every base is flat but FS's, the TEB's.
 * - instructions x86-64 dropped: DAA DAS AAA AAS AAM AAD SALC BOUND INTO,
 *   and ENTER, far CALL/JMP/RET and IRET in a flat address space;
 * - instructions copy protections and anti-debugging probe, given what
 *   Windows does with them: INT3, INT n and ICEBP raise their exceptions,
 *   IN/OUT/CLI/STI/HLT and the control-register moves raise a privileged
 *   instruction, SGDT/SIDT/SLDT/STR/SMSW answer as 32-bit Windows does;
 * - PREFETCH/PREFETCHW (0F 0D), a no-op.
 *
 * legacy_op returns 1 when it did the instruction, with the context moved
 * past it, or with *raise set to an exception to raise at ctx->Eip (and
 * *address for an access violation); 0 when it isn't one of these.
 */
#ifndef PW_LEGACY_OPS_H
#define PW_LEGACY_OPS_H

#define PWL_CF 0x001
#define PWL_PF 0x004
#define PWL_AF 0x010
#define PWL_ZF 0x040
#define PWL_SF 0x080
#define PWL_OF 0x800

#ifndef PWL_TEB32_BASE
/* A WoW64 thread's 32-bit TEB sits two pages after its 64-bit one. */
#define PWL_TEB32_BASE() ((DWORD)((ULONG_PTR)NtCurrentTeb() + 0x2000))
#endif

typedef struct PwlInsn {
    const BYTE *code;
    unsigned at;            /* bytes taken so far */
    unsigned size;          /* operand size, 2 or 4 */
    int seg;                /* override: -1 none, 4 FS, 5 GS */
} PwlInsn;

static DWORD *pwl_sreg( I386_CONTEXT *ctx, unsigned reg )
{
    switch (reg)
    {
    case 0: return &ctx->SegEs;
    case 1: return &ctx->SegCs;
    case 2: return &ctx->SegSs;
    case 3: return &ctx->SegDs;
    case 4: return &ctx->SegFs;
    case 5: return &ctx->SegGs;
    }
    return NULL;
}

static DWORD *pwl_gpr( I386_CONTEXT *ctx, unsigned reg )
{
    switch (reg & 7)
    {
    case 0: return &ctx->Eax;
    case 1: return &ctx->Ecx;
    case 2: return &ctx->Edx;
    case 3: return &ctx->Ebx;
    case 4: return &ctx->Esp;
    case 5: return &ctx->Ebp;
    case 6: return &ctx->Esi;
    default: return &ctx->Edi;
    }
}

static void pwl_set_gpr( I386_CONTEXT *ctx, unsigned reg, DWORD value, unsigned size )
{
    DWORD *r = pwl_gpr( ctx, reg );
    *r = size == 2 ? (*r & 0xffff0000u) | (value & 0xffff) : value;
}

/* The effective address of the ModRM at i->code[i->at] (mod != 3), with
 * i->at moved past it and its displacement. 0 when it can't be decoded. */
static int pwl_address( I386_CONTEXT *ctx, PwlInsn *i, DWORD *address )
{
    const BYTE *p = i->code + i->at;
    unsigned mod = p[0] >> 6, rm = p[0] & 7, n = 1;
    DWORD ea = 0;

    if (mod == 3) return 0;
    if (rm == 4)
    {
        BYTE sib = p[1];
        unsigned base = sib & 7, index = (sib >> 3) & 7, scale = sib >> 6;
        n = 2;
        if (index != 4) ea += *pwl_gpr( ctx, index ) << scale;
        if (base == 5 && mod == 0) { ea += *(const DWORD *)(p + n); n += 4; }
        else ea += *pwl_gpr( ctx, base );
    }
    else if (rm == 5 && mod == 0) { ea = *(const DWORD *)(p + 1); n = 5; }
    else ea = *pwl_gpr( ctx, rm );
    if (mod == 1) { ea += (DWORD)(signed char)p[n]; n += 1; }
    else if (mod == 2) { ea += *(const DWORD *)(p + n); n += 4; }
    if (i->seg == 4) ea += PWL_TEB32_BASE();
    i->at += n;
    *address = ea;
    return 1;
}

static void pwl_push( I386_CONTEXT *ctx, DWORD value, unsigned size )
{
    ctx->Esp -= size;
    if (size == 2) *(WORD *)ULongToPtr( ctx->Esp ) = (WORD)value;
    else *(DWORD *)ULongToPtr( ctx->Esp ) = value;
}

static DWORD pwl_pop( I386_CONTEXT *ctx, unsigned size )
{
    DWORD value = size == 2 ? *(const WORD *)ULongToPtr( ctx->Esp ) : *(const DWORD *)ULongToPtr( ctx->Esp );
    ctx->Esp += size;
    return value;
}

static void pwl_szp( I386_CONTEXT *ctx, BYTE value )
{
    unsigned bits = value;
    bits ^= bits >> 4; bits ^= bits >> 2; bits ^= bits >> 1;
    ctx->EFlags &= ~(PWL_SF | PWL_ZF | PWL_PF);
    if (value & 0x80) ctx->EFlags |= PWL_SF;
    if (!value) ctx->EFlags |= PWL_ZF;
    if (!(bits & 1)) ctx->EFlags |= PWL_PF;
}

static void pwl_sreg_load( I386_CONTEXT *ctx, unsigned reg, DWORD value )
{
    DWORD *slot = pwl_sreg( ctx, reg );
    if (slot && reg != 1) *slot = value & 0xffff;     /* never CS */
}

static int legacy_op( I386_CONTEXT *ctx, DWORD *raise, UINT *address )
{
    PwlInsn in = { ULongToPtr( ctx->Eip ), 0, 4, -1 };
    const BYTE *c = in.code;
    DWORD ea, value, *slot;
    BYTE op;

    *raise = 0;
    *address = 0;
    for (;;)
    {
        BYTE p = c[in.at];
        if (p == 0x66) in.size = 2;
        else if (p == 0x64) in.seg = 4;
        else if (p == 0x65) in.seg = 5;
        else if (p == 0x67) return 0;                 /* 16-bit addressing */
        else if (p != 0x26 && p != 0x2e && p != 0x36 && p != 0x3e && p != 0xf0 && p != 0xf2 && p != 0xf3) break;
        if (++in.at > 4) return 0;
    }
    op = c[in.at++];
    switch (op)
    {
    /* push/pop ES CS SS DS (the host fallback normally has these) */
    case 0x06: case 0x0e: case 0x16: case 0x1e:
        pwl_push( ctx, *pwl_sreg( ctx, op >> 3 ) & 0xffff, in.size );
        break;
    case 0x07: case 0x17: case 0x1f:
        pwl_sreg_load( ctx, op >> 3, pwl_pop( ctx, in.size ) );
        break;

    case 0x8c:                                         /* mov r/m16, sreg */
    case 0x8e:                                         /* mov sreg, r/m16 */
    {
        BYTE modrm = c[in.at];
        unsigned reg = (modrm >> 3) & 7;
        if (!(slot = pwl_sreg( ctx, reg ))) return 0;
        if ((modrm >> 6) == 3)
        {
            in.at++;
            if (op == 0x8c) pwl_set_gpr( ctx, modrm, *slot & 0xffff, in.size == 2 ? 2 : 4 );
            else pwl_sreg_load( ctx, reg, *pwl_gpr( ctx, modrm ) );
        }
        else
        {
            if (!pwl_address( ctx, &in, &ea )) return 0;
            if (op == 0x8c) *(WORD *)ULongToPtr( ea ) = (WORD)*slot;
            else pwl_sreg_load( ctx, reg, *(const WORD *)ULongToPtr( ea ) );
        }
        break;
    }

    case 0xc4: case 0xc5:                              /* les, lds */
    {
        unsigned reg = (c[in.at] >> 3) & 7;
        if (!pwl_address( ctx, &in, &ea )) return 0;
        value = in.size == 2 ? *(const WORD *)ULongToPtr( ea ) : *(const DWORD *)ULongToPtr( ea );
        pwl_set_gpr( ctx, reg, value, in.size );
        pwl_sreg_load( ctx, op == 0xc4 ? 0 : 3, *(const WORD *)ULongToPtr( ea + in.size ) );
        break;
    }

    case 0x8f:                                         /* pop r32 in its ModRM form */
        if ((c[in.at] >> 6) != 3 || ((c[in.at] >> 3) & 7)) return 0;
        value = pwl_pop( ctx, in.size );
        pwl_set_gpr( ctx, c[in.at], value, in.size );
        in.at++;
        break;

    case 0xc8:                                         /* enter imm16, imm8 */
    {
        WORD frame = *(const WORD *)(c + in.at);
        BYTE level = c[in.at + 2] & 31;
        DWORD ebp = ctx->Ebp, temp;
        in.at += 3;
        pwl_push( ctx, ebp, in.size );
        temp = ctx->Esp;
        if (level)
        {
            unsigned l;
            for (l = 1; l < level; l++)
            {
                ebp -= in.size;
                pwl_push( ctx, in.size == 2 ? *(const WORD *)ULongToPtr( ebp ) : *(const DWORD *)ULongToPtr( ebp ), in.size );
            }
            pwl_push( ctx, temp, in.size );
        }
        pwl_set_gpr( ctx, 5, temp, in.size );
        ctx->Esp -= frame;
        break;
    }

    case 0x27: case 0x2f:                              /* daa, das */
    {
        BYTE al = (BYTE)ctx->Eax, old = al;
        DWORD cf = ctx->EFlags & PWL_CF;
        ctx->EFlags &= ~PWL_CF;
        if ((al & 0x0f) > 9 || (ctx->EFlags & PWL_AF))
        {
            al = op == 0x27 ? al + 6 : al - 6;
            ctx->EFlags |= PWL_AF;
            if (op == 0x2f && old < 6) ctx->EFlags |= PWL_CF;
            if (cf) ctx->EFlags |= PWL_CF;
        }
        else ctx->EFlags &= ~PWL_AF;
        if (old > 0x99 || cf)
        {
            al = op == 0x27 ? al + 0x60 : al - 0x60;
            ctx->EFlags |= PWL_CF;
        }
        ctx->Eax = (ctx->Eax & ~0xffu) | al;
        pwl_szp( ctx, al );
        break;
    }
    case 0x37: case 0x3f:                              /* aaa, aas */
        if ((ctx->Eax & 0x0f) > 9 || (ctx->EFlags & PWL_AF))
        {
            WORD ax = (WORD)ctx->Eax;
            if (op == 0x37) { ax += 0x106; }
            else { ax = (WORD)(((ax & 0xff00) - 0x100) | ((ax - 6) & 0xff)); }
            ctx->Eax = (ctx->Eax & 0xffff0000u) | ax;
            ctx->EFlags |= PWL_AF | PWL_CF;
        }
        else ctx->EFlags &= ~(PWL_AF | PWL_CF);
        ctx->Eax &= ~0x00f0u;
        break;
    case 0xd4:                                         /* aam imm8 */
    {
        BYTE base = c[in.at++], al = (BYTE)ctx->Eax;
        if (!base) { *raise = EXCEPTION_INT_DIVIDE_BY_ZERO; return 1; }
        ctx->Eax = (ctx->Eax & 0xffff0000u) | ((al / base) << 8) | (al % base);
        pwl_szp( ctx, (BYTE)ctx->Eax );
        break;
    }
    case 0xd5:                                         /* aad imm8 */
    {
        BYTE base = c[in.at++];
        BYTE al = (BYTE)((BYTE)ctx->Eax + (BYTE)(ctx->Eax >> 8) * base);
        ctx->Eax = (ctx->Eax & 0xffff0000u) | al;
        pwl_szp( ctx, al );
        break;
    }
    case 0xd6:                                         /* salc */
        ctx->Eax = (ctx->Eax & ~0xffu) | ((ctx->EFlags & PWL_CF) ? 0xff : 0);
        break;

    case 0x62:                                         /* bound r32, m32&32 */
    {
        unsigned reg = (c[in.at] >> 3) & 7;
        LONG index, low, high;
        if (!pwl_address( ctx, &in, &ea )) return 0;
        if (in.size == 2)
        {
            index = (SHORT)*pwl_gpr( ctx, reg );
            low = *(const SHORT *)ULongToPtr( ea );
            high = *(const SHORT *)ULongToPtr( ea + 2 );
        }
        else
        {
            index = (LONG)*pwl_gpr( ctx, reg );
            low = *(const LONG *)ULongToPtr( ea );
            high = *(const LONG *)ULongToPtr( ea + 4 );
        }
        if (index < low || index > high) { *raise = EXCEPTION_ARRAY_BOUNDS_EXCEEDED; return 1; }
        break;
    }
    case 0xce:                                         /* into */
        ctx->Eip += in.at;
        if (ctx->EFlags & PWL_OF) *raise = EXCEPTION_INT_OVERFLOW;
        return 1;

    /* far transfers in a flat address space: the selector is CS */
    case 0x9a:                                         /* call ptr16:32 */
    {
        DWORD target = in.size == 2 ? *(const WORD *)(c + in.at) : *(const DWORD *)(c + in.at);
        in.at += in.size + 2;
        pwl_push( ctx, ctx->SegCs, in.size );
        pwl_push( ctx, ctx->Eip + in.at, in.size );
        ctx->Eip = target;
        return 1;
    }
    case 0xea:                                         /* jmp ptr16:32 */
        ctx->Eip = in.size == 2 ? *(const WORD *)(c + in.at) : *(const DWORD *)(c + in.at);
        return 1;
    case 0xca: case 0xcb:                              /* retf [imm16] */
    {
        WORD pop = op == 0xca ? *(const WORD *)(c + in.at) : 0;
        ctx->Eip = pwl_pop( ctx, in.size );
        pwl_pop( ctx, in.size );
        ctx->Esp += pop;
        return 1;
    }
    case 0xcf:                                         /* iret */
    {
        DWORD flags;
        ctx->Eip = pwl_pop( ctx, in.size );
        pwl_pop( ctx, in.size );
        flags = pwl_pop( ctx, in.size );
        ctx->EFlags = (ctx->EFlags & ~0x00000fd5u) | (flags & 0x00000fd5u);
        return 1;
    }

    /* what Windows makes of them in user mode */
    case 0xcc:
        *raise = EXCEPTION_BREAKPOINT;
        return 1;
    case 0xcd:
        if (c[in.at] == 3 || c[in.at] == 0x2d) *raise = EXCEPTION_BREAKPOINT;
        else { *raise = EXCEPTION_ACCESS_VIOLATION; *address = 0xffffffff; }
        return 1;
    case 0xf1:                                         /* icebp */
        ctx->Eip += in.at;
        *raise = EXCEPTION_SINGLE_STEP;
        return 1;
    case 0x6c: case 0x6d: case 0x6e: case 0x6f:
    case 0xe4: case 0xe5: case 0xe6: case 0xe7:
    case 0xec: case 0xed: case 0xee: case 0xef:
    case 0xf4: case 0xfa: case 0xfb:
        *raise = EXCEPTION_PRIV_INSTRUCTION;
        return 1;

    case 0x0f:
        op = c[in.at++];
        switch (op)
        {
        case 0xa0: case 0xa8:                          /* push fs, push gs */
            pwl_push( ctx, *pwl_sreg( ctx, op == 0xa0 ? 4 : 5 ) & 0xffff, in.size );
            break;
        case 0xa1: case 0xa9:                          /* pop fs, pop gs */
            pwl_sreg_load( ctx, op == 0xa1 ? 4 : 5, pwl_pop( ctx, in.size ) );
            break;
        case 0xb2: case 0xb4: case 0xb5:               /* lss, lfs, lgs */
        {
            unsigned reg = (c[in.at] >> 3) & 7;
            if (!pwl_address( ctx, &in, &ea )) return 0;
            value = in.size == 2 ? *(const WORD *)ULongToPtr( ea ) : *(const DWORD *)ULongToPtr( ea );
            pwl_set_gpr( ctx, reg, value, in.size );
            pwl_sreg_load( ctx, op == 0xb2 ? 2 : op == 0xb4 ? 4 : 5, *(const WORD *)ULongToPtr( ea + in.size ) );
            break;
        }
        case 0x00:                                     /* sldt, str; the rest privileged */
        case 0x01:                                     /* sgdt, sidt, smsw; the rest privileged */
        {
            BYTE modrm = c[in.at];
            unsigned reg = (modrm >> 3) & 7;
            DWORD answer;
            if (op == 0x00 && reg <= 1) answer = reg ? 0x28 : 0;
            else if (op == 0x01 && reg == 4) answer = 0x8001003b;
            else if (op == 0x01 && reg <= 1 && (modrm >> 6) != 3)
            {
                if (!pwl_address( ctx, &in, &ea )) return 0;
                *(WORD *)ULongToPtr( ea ) = reg ? 0x7ff : 0x3ff;
                *(DWORD *)ULongToPtr( ea + 2 ) = reg ? 0x8003f400 : 0x8003f000;
                break;
            }
            else { *raise = EXCEPTION_PRIV_INSTRUCTION; return 1; }
            if ((modrm >> 6) == 3)
            {
                in.at++;
                pwl_set_gpr( ctx, modrm, answer, in.size );
            }
            else
            {
                if (!pwl_address( ctx, &in, &ea )) return 0;
                *(WORD *)ULongToPtr( ea ) = (WORD)answer;
            }
            break;
        }
        case 0x0d:                                     /* prefetch, prefetchw */
            if ((c[in.at] >> 6) == 3 || !pwl_address( ctx, &in, &ea )) return 0;
            break;
        case 0x06: case 0x08: case 0x09: case 0x20: case 0x21: case 0x22: case 0x23:
        case 0x30: case 0x32: case 0x33:
            *raise = EXCEPTION_PRIV_INSTRUCTION;
            return 1;
        default:
            return 0;
        }
        break;
    default:
        return 0;
    }
    ctx->Eip += in.at;
    return 1;
}

#endif
