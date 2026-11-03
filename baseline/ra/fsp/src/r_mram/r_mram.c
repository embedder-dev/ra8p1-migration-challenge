/*
 * Copyright (c) 2026 Renesas Electronics Corporation and/or its affiliates
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "r_mram.h"

/* R7KA8P1AD SVD; RA8P1 Hardware Manual R01UH1064EJ0130, sections 60.4, 60.5 and 60.14.
 * Code MRAM has a bus-fed 32-byte buffer, not the extra-MRAM MACI command interface. */
#define MRAM_OPEN                    (0x4D52414DUL)
#define MRAM_PRV_ERROR               (0x03U)
#define MRAM_PRV_EMPTY               (0x20U)
#define MRAM_PRV_FULL                (0x40U)
#define MRAM_PRV_BUSY                (0x80U)
#define MRAM_PRV_KEY_NS              (0x8600U)
#define MRAM_PRV_KEY_S               (0x6800U)
#define MRAM_PRV_FLUSH               (0xC301U)
#define MRAM_PRV_CACHE_MASK          ((1UL << 16) | (1UL << 17))

#if BSP_TZ_NONSECURE_BUILD
 #define MRAM_PRV_REGS    R_MRAM_NS
#else
 #define MRAM_PRV_REGS    R_MRAM
#endif

/* FSP_ERROR_RETURN can call fsp_error_log and read __FILE__ from MRAM. RAM-resident error paths must not do that. */
#define MRAM_PRV_ERROR_RETURN(condition, error)    do { if (!(condition)) { return (error); } } while (0)

/* Access boundaries also allow a host register model to observe bus ordering and access widths. */
#ifndef MRAM_PRV_READ8
 #define MRAM_PRV_READ8(p)            (*(volatile uint8_t const *) (p))
 #define MRAM_PRV_READ16(p)           (*(volatile uint16_t const *) (p))
 #define MRAM_PRV_WRITE8(p, value)    (*(volatile uint8_t *) (p) = (uint8_t) (value))
 #define MRAM_PRV_WRITE16(p, value)   (*(volatile uint16_t *) (p) = (uint16_t) (value))
#endif

static mram_instance_ctrl_t * gp_mram_owner;

static MRAM_CODE_IN_RAM bool r_mram_accessible(uintptr_t address, uint32_t length);
static MRAM_CODE_IN_RAM bool r_mram_range(mram_instance_ctrl_t const * p_ctrl, uint32_t address, uint32_t length);
static MRAM_CODE_IN_RAM bool r_mram_idle(void);
static MRAM_CODE_IN_RAM fsp_err_t r_mram_wait(mram_instance_ctrl_t * p_ctrl, uint8_t mask, uint8_t value);
static MRAM_CODE_IN_RAM fsp_err_t r_mram_clear_errors(mram_instance_ctrl_t const * p_ctrl);
static MRAM_CODE_IN_RAM fsp_err_t r_mram_acquire(mram_instance_ctrl_t * p_ctrl, uint32_t * p_primask);
static MRAM_CODE_IN_RAM void r_mram_release(mram_instance_ctrl_t * p_ctrl, uint32_t primask);

flash_api_t const g_flash_on_mram =
{
    .open = R_MRAM_Open,
    .write = R_MRAM_Write,
    .erase = R_MRAM_Erase,
    .blankCheck = R_MRAM_BlankCheck,
    .infoGet = R_MRAM_InfoGet,
    .close = R_MRAM_Close,
    .statusGet = R_MRAM_StatusGet,
    .accessWindowSet = R_MRAM_AccessWindowSet,
    .accessWindowClear = R_MRAM_AccessWindowClear,
    .idCodeSet = R_MRAM_IdCodeSet,
    .reset = R_MRAM_Reset,
    .updateFlashClockFreq = R_MRAM_UpdateFlashClockFreq,
    .startupAreaSelect = R_MRAM_StartUpAreaSelect,
    .bankSwap = R_MRAM_BankSwap,
    .callbackSet = R_MRAM_CallbackSet,
    .antiRollbackCounterIncrement = R_MRAM_AntiRollbackCounterIncrement,
    .antiRollbackCounterRefresh = R_MRAM_AntiRollbackCounterRefresh,
    .antiRollbackCounterRead = R_MRAM_AntiRollbackCounterRead,
    .userLockableAreaWrite = R_MRAM_UserLockableAreaWrite,
};

/*******************************************************************************************************************//**
 * Open without changing protection, clock, cache or option registers. Data-flash BGO is unsupported.
 * timeout_us must exceed the normal-mode tPMC maximum plus 25% margin and two microseconds (Table 70.124):
 * 1879 / MRICLK[MHz] + 78.75 us, plus one MRPCLK cycle below 125 MHz. Normal mode bounds high-speed mode too.
 **********************************************************************************************************************/
fsp_err_t R_MRAM_Open (flash_ctrl_t * const p_api_ctrl, flash_cfg_t const * const p_cfg)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl && p_cfg && p_cfg->p_extend, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN((BSP_FEATURE_MRAM_CODE_SIZE > 0U) && (BSP_FEATURE_MRAM_CODE_SIZE <= 0x100000U),
                     FSP_ERR_UNSUPPORTED);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened != MRAM_OPEN, FSP_ERR_ALREADY_OPEN);
    MRAM_PRV_ERROR_RETURN(!p_cfg->data_flash_bgo, FSP_ERR_UNSUPPORTED);
    mram_extended_cfg_t const * p_ext = (mram_extended_cfg_t const *) p_cfg->p_extend;
    MRAM_PRV_ERROR_RETURN((p_ext->code_start == MRAM_CODE_SECURE_START) ||
                     (p_ext->code_start == MRAM_CODE_NONSECURE_START), FSP_ERR_INVALID_ADDRESS);
#if BSP_TZ_NONSECURE_BUILD
    MRAM_PRV_ERROR_RETURN(p_ext->code_start == MRAM_CODE_NONSECURE_START, FSP_ERR_INVALID_ADDRESS);
#endif
    MRAM_PRV_ERROR_RETURN((p_ext->mriclk_hz > 0U) && (p_ext->mriclk_hz <= 250000000U) &&
                     (p_ext->mrpclk_hz > 0U) && (p_ext->mrpclk_hz <= 125000000U), FSP_ERR_INVALID_ARGUMENT);
    uint64_t time_ns = (1879000000000ULL + p_ext->mriclk_hz - 1U) / p_ext->mriclk_hz + 78750U;
    if (p_ext->mrpclk_hz < 125000000U)
    {
        time_ns += (1000000000ULL + p_ext->mrpclk_hz - 1U) / p_ext->mrpclk_hz;
    }
    uint64_t minimum_us = (time_ns * 5U + 3999U) / 4000U + 2U;
    MRAM_PRV_ERROR_RETURN((p_ext->timeout_us >= minimum_us) && (p_ext->timeout_us <= 1000000U), FSP_ERR_INVALID_ARGUMENT);
    MRAM_PRV_ERROR_RETURN((SystemCoreClock > 0U) && (SystemCoreClock <= 1000000000U), FSP_ERR_INVALID_ARGUMENT);
    MRAM_PRV_ERROR_RETURN(r_mram_accessible((uintptr_t) p_ctrl, sizeof(*p_ctrl)), FSP_ERR_INVALID_ADDRESS);
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    if (gp_mram_owner)
    {
        __set_PRIMASK(primask);
        return FSP_ERR_IN_USE;
    }
    gp_mram_owner = p_ctrl;
    p_ctrl->extend = *p_ext;
    /* A second, finite poll bound protects against a stopped application time source. Each iteration costs >= 1 cycle. */
    p_ctrl->poll_limit = (uint32_t) (((uint64_t) SystemCoreClock * p_ext->timeout_us + 999999U) / 1000000U);
    p_ctrl->busy = false;
    p_ctrl->faulted = false;
    p_ctrl->interrupts_held = false;
    p_ctrl->saved_primask = 0U;
    p_ctrl->region.block_section_st_addr = p_ext->code_start;
    p_ctrl->region.block_section_end_addr = p_ext->code_start + BSP_FEATURE_MRAM_CODE_SIZE - 1U;
    p_ctrl->region.block_size = 0U; /* No erase blocks. */
    p_ctrl->region.block_size_write = 1U;
    p_ctrl->p_callback = p_cfg->p_callback;
    p_ctrl->p_context = p_cfg->p_context;
    p_ctrl->p_callback_memory = NULL;
    p_ctrl->opened = MRAM_OPEN;
    __set_PRIMASK(primask);
    return FSP_SUCCESS;
}

/*******************************************************************************************************************//**
 * Program byte-granular data, completing and verifying each 32-byte page before issuing another page.
 * Safety checks are unconditional, including when MRAM_CFG_PARAM_CHECKING_ENABLE is zero.
 * A timeout can leave an in-flight write; it is NOT cancellation. Check StatusGet, then Reset only after idle.
 **********************************************************************************************************************/
MRAM_CODE_IN_RAM fsp_err_t R_MRAM_Write (flash_ctrl_t * const p_api_ctrl, uint32_t const src_address,
                                        uint32_t const flash_address, uint32_t const num_bytes)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
    MRAM_PRV_ERROR_RETURN(r_mram_range(p_ctrl, flash_address, num_bytes), FSP_ERR_INVALID_ADDRESS);
    MRAM_PRV_ERROR_RETURN(src_address && (num_bytes - 1U <= UINT32_MAX - src_address) &&
                     r_mram_accessible(src_address, num_bytes), FSP_ERR_INVALID_ADDRESS);
#if defined(__GNUC__)
    MRAM_PRV_ERROR_RETURN(r_mram_accessible((uintptr_t) __builtin_return_address(0), 1U), FSP_ERR_INVALID_MODE);
#endif
    MRAM_PRV_ERROR_RETURN(!p_ctrl->faulted, FSP_ERR_INVALID_MODE);
    uint32_t primask;
    fsp_err_t err = r_mram_acquire(p_ctrl, &primask);
    MRAM_PRV_ERROR_RETURN(FSP_SUCCESS == err, err);
    volatile uint16_t * p_protect = &MRAM_PRV_REGS->MRCPC0;
    uint16_t key = MRAM_PRV_KEY_NS;
#if !BSP_TZ_NONSECURE_BUILD
    if (p_ctrl->extend.code_start == MRAM_CODE_SECURE_START)
    {
        p_protect = &MRAM_PRV_REGS->MRCPC1;
        key = MRAM_PRV_KEY_S;
    }
#endif
    uint16_t saved = MRAM_PRV_READ16(p_protect) & 1U;
    err = r_mram_wait(p_ctrl, MRAM_PRV_BUSY | MRAM_PRV_FULL | MRAM_PRV_EMPTY, MRAM_PRV_EMPTY);
    if (FSP_SUCCESS == err)
    {
        err = r_mram_clear_errors(p_ctrl);
    }
    if (FSP_SUCCESS == err)
    {
        MRAM_PRV_WRITE16(p_protect, key | 1U);
        __DMB();
        if ((MRAM_PRV_READ16(p_protect) & 1U) == 0U)
        {
            err = FSP_ERR_WRITE_FAILED;
        }
    }
    uint32_t offset = 0U;
    while ((FSP_SUCCESS == err) && (offset < num_bytes))
    {
        uint32_t address = flash_address + offset;
        uint32_t count = MRAM_PROGRAM_BUFFER_SIZE - (address & (MRAM_PROGRAM_BUFFER_SIZE - 1U));
        if (count > num_bytes - offset)
        {
            count = num_bytes - offset;
        }
        for (uint32_t i = 0; i < count; i++)
        {
            MRAM_PRV_WRITE8((uintptr_t) address + i, MRAM_PRV_READ8((uintptr_t) src_address + offset + i));
        }
        /* DMB orders posted data stores before the flush-register write (60.14(6)). DSB here could stall the CPU. */
        __DMB();
        if ((count != MRAM_PROGRAM_BUFFER_SIZE) && !(MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCPS) & MRAM_PRV_EMPTY))
        {
            MRAM_PRV_WRITE16(&MRAM_PRV_REGS->MRCFLR, MRAM_PRV_FLUSH);
        }
        err = r_mram_wait(p_ctrl, MRAM_PRV_EMPTY | MRAM_PRV_BUSY | MRAM_PRV_FULL, MRAM_PRV_EMPTY);
        if (FSP_SUCCESS == err)
        {
            if (MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCPS) & MRAM_PRV_ERROR)
            {
                err = FSP_ERR_WRITE_FAILED;
            }
            else
            {
                __DSB();
                for (uint32_t i = 0; i < count; i++)
                {
                    if (MRAM_PRV_READ8((uintptr_t) address + i) !=
                        MRAM_PRV_READ8((uintptr_t) src_address + offset + i))
                    {
                        err = FSP_ERR_WRITE_FAILED;
                        break;
                    }
                }
                if (MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCRAES) & MRAM_PRV_ERROR)
                {
                    err = FSP_ERR_WRITE_FAILED;
                }
            }
        }
        offset += count;
    }
    /* Restore the original protection value even after timeout; never cancel block protection or touch extra MRAM. */
    MRAM_PRV_WRITE16(p_protect, key | saved);
    __DMB();
    if ((MRAM_PRV_READ16(p_protect) & 1U) != saved)
    {
        err = FSP_ERR_WRITE_FAILED;
        p_ctrl->faulted = true;
    }
    if (FSP_ERR_TIMEOUT == err)
    {
        p_ctrl->faulted = true;
    }
    r_mram_release(p_ctrl, primask);
    return err;
}

MRAM_CODE_IN_RAM fsp_err_t R_MRAM_BlankCheck (flash_ctrl_t * const p_api_ctrl, uint32_t const address,
                                             uint32_t const num_bytes, flash_result_t * const p_result)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl && p_result, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
    MRAM_PRV_ERROR_RETURN(r_mram_range(p_ctrl, address, num_bytes), FSP_ERR_INVALID_ADDRESS);
    MRAM_PRV_ERROR_RETURN(r_mram_accessible((uintptr_t) p_result, sizeof(*p_result)), FSP_ERR_INVALID_ADDRESS);
    uint32_t primask;
    fsp_err_t err = r_mram_acquire(p_ctrl, &primask);
    MRAM_PRV_ERROR_RETURN(FSP_SUCCESS == err, err);
    if (!r_mram_idle())
    {
        err = FSP_ERR_IN_USE;
    }
    else
    {
        flash_result_t result = FLASH_RESULT_BLANK;
        for (uint32_t i = 0; i < num_bytes; i++)
        {
            if (MRAM_PRV_READ8((uintptr_t) address + i) != UINT8_MAX)
            {
                result = FLASH_RESULT_NOT_BLANK;
                break;
            }
        }
        if (MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCRAES) & MRAM_PRV_ERROR)
        {
            err = FSP_ERR_WRITE_FAILED;
        }
        else
        {
            *p_result = result;
        }
    }
    r_mram_release(p_ctrl, primask);
    return err;
}

MRAM_CODE_IN_RAM fsp_err_t R_MRAM_Reset (flash_ctrl_t * const p_api_ctrl)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
#if defined(__GNUC__)
    MRAM_PRV_ERROR_RETURN(r_mram_accessible((uintptr_t) __builtin_return_address(0), 1U), FSP_ERR_INVALID_MODE);
#endif
    uint32_t primask;
    fsp_err_t err = r_mram_acquire(p_ctrl, &primask);
    MRAM_PRV_ERROR_RETURN(FSP_SUCCESS == err, err);
    if (!r_mram_idle())
    {
        err = FSP_ERR_IN_USE; /* No code-MRAM abort command exists. Do not flush someone else's data. */
    }
    else
    {
        err = r_mram_clear_errors(p_ctrl);
        if (FSP_SUCCESS == err)
        {
            p_ctrl->faulted = false;
        }
    }
    r_mram_release(p_ctrl, primask);
    return err;
}

MRAM_CODE_IN_RAM fsp_err_t R_MRAM_Close (flash_ctrl_t * const p_api_ctrl)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    if (p_ctrl->busy || p_ctrl->faulted || !r_mram_idle())
    {
        __set_PRIMASK(primask);
        return FSP_ERR_IN_USE;
    }
    gp_mram_owner = NULL;
    p_ctrl->opened = 0U;
    __set_PRIMASK(primask);
    return FSP_SUCCESS;
}

fsp_err_t R_MRAM_InfoGet (flash_ctrl_t * const p_api_ctrl, flash_info_t * const p_info)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl && p_info, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
    p_info->code_flash.num_regions = 1U;
    p_info->code_flash.p_block_array = &p_ctrl->region;
    p_info->data_flash.num_regions = 0U;
    p_info->data_flash.p_block_array = NULL;
    return FSP_SUCCESS;
}

MRAM_CODE_IN_RAM fsp_err_t R_MRAM_StatusGet (flash_ctrl_t * const p_api_ctrl, flash_status_t * const p_status)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl && p_status, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
    *p_status = (p_ctrl->busy || !r_mram_idle()) ? FLASH_STATUS_BUSY : FLASH_STATUS_IDLE;
    return FSP_SUCCESS;
}

fsp_err_t R_MRAM_CallbackSet (flash_ctrl_t * const p_api_ctrl, void (* p_callback)(flash_callback_args_t *),
                              void const * const p_context, flash_callback_args_t * const p_callback_memory)
{
    mram_instance_ctrl_t * p_ctrl = (mram_instance_ctrl_t *) p_api_ctrl;
    MRAM_PRV_ERROR_RETURN(p_ctrl, FSP_ERR_ASSERTION);
    MRAM_PRV_ERROR_RETURN(p_ctrl->opened == MRAM_OPEN, FSP_ERR_NOT_OPEN);
#if MRAM_CFG_PARAM_CHECKING_ENABLE
    MRAM_PRV_ERROR_RETURN(p_callback, FSP_ERR_ASSERTION);
#endif
    MRAM_PRV_ERROR_RETURN(!p_ctrl->busy, FSP_ERR_IN_USE);
    p_ctrl->p_callback = p_callback;
    p_ctrl->p_context = p_context;
    p_ctrl->p_callback_memory = p_callback_memory;
    return FSP_SUCCESS;
}

static MRAM_CODE_IN_RAM bool r_mram_range (mram_instance_ctrl_t const * p_ctrl, uint32_t address, uint32_t length)
{
    uint32_t base = p_ctrl->extend.code_start;
    return length && (address >= base) && (address - base < BSP_FEATURE_MRAM_CODE_SIZE) &&
           (length <= BSP_FEATURE_MRAM_CODE_SIZE - (address - base));
}

static MRAM_CODE_IN_RAM bool r_mram_accessible (uintptr_t address, uint32_t length)
{
    if (!length || (length - 1U > UINTPTR_MAX - address))
    {
        return false;
    }
    uintptr_t end = address + length - 1U;
    /* Both instruction aliases and both data/program aliases address the same physical MRAM. */
    return !((address < BSP_FEATURE_MRAM_CODE_SIZE) ||
             ((address <= 0x100FFFFFUL) && (end >= 0x10000000UL)) ||
             ((address <= 0x020FFFFFUL) && (end >= MRAM_CODE_SECURE_START)) ||
             ((address <= 0x120FFFFFUL) && (end >= MRAM_CODE_NONSECURE_START)));
}

static MRAM_CODE_IN_RAM bool r_mram_idle (void)
{
    return (MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCPS) & (MRAM_PRV_EMPTY | MRAM_PRV_FULL | MRAM_PRV_BUSY)) == MRAM_PRV_EMPTY;
}

static MRAM_CODE_IN_RAM fsp_err_t r_mram_wait (mram_instance_ctrl_t * p_ctrl, uint8_t mask, uint8_t value)
{
    uint32_t start = p_ctrl->extend.p_time_us(p_ctrl->extend.p_context);
    uint32_t remaining = p_ctrl->poll_limit;
    do
    {
        if ((MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCPS) & mask) == value)
        {
            return FSP_SUCCESS;
        }
        if ((uint32_t) (p_ctrl->extend.p_time_us(p_ctrl->extend.p_context) - start) >= p_ctrl->extend.timeout_us)
        {
            break;
        }
    } while (--remaining);
    return FSP_ERR_TIMEOUT;
}

static MRAM_CODE_IN_RAM fsp_err_t r_mram_clear_errors (mram_instance_ctrl_t const * p_ctrl)
{
    uint32_t remaining = p_ctrl->poll_limit;
    do
    {
        if (!(MRAM_PRV_READ8(&MRAM_PRV_REGS->MRCPS) & MRAM_PRV_ERROR))
        {
            return FSP_SUCCESS;
        }
        /* MRCPS error bits are read-one/write-zero, and require a confirming read (60.5.36). */
        MRAM_PRV_WRITE8(&MRAM_PRV_REGS->MRCPS, 0U);
    } while (--remaining);
    return FSP_ERR_TIMEOUT;
}

static MRAM_CODE_IN_RAM fsp_err_t r_mram_acquire (mram_instance_ctrl_t * p_ctrl, uint32_t * p_primask)
{
    MRAM_PRV_ERROR_RETURN(__get_IPSR() == 0U, FSP_ERR_INVALID_MODE);
    MRAM_PRV_ERROR_RETURN(p_ctrl->extend.p_enter && p_ctrl->extend.p_exit && p_ctrl->extend.p_time_us, FSP_ERR_UNSUPPORTED);
    MRAM_PRV_ERROR_RETURN(r_mram_accessible((uintptr_t) R_MRAM_Write, 1U) &&
                     r_mram_accessible((uintptr_t) p_ctrl, sizeof(*p_ctrl)) &&
                     r_mram_accessible((uintptr_t) p_primask, sizeof(*p_primask)) &&
                     r_mram_accessible((uintptr_t) p_ctrl->extend.p_exit, 1U) &&
                     r_mram_accessible((uintptr_t) p_ctrl->extend.p_time_us, 1U) &&
                     (!p_ctrl->extend.p_context ||
                      r_mram_accessible((uintptr_t) p_ctrl->extend.p_context, 1U)), FSP_ERR_INVALID_MODE);
    *p_primask = __get_PRIMASK();
    __disable_irq();
    if (p_ctrl->busy)
    {
        __set_PRIMASK(*p_primask);
        return FSP_ERR_IN_USE;
    }
    p_ctrl->busy = true;
    __set_PRIMASK(*p_primask);
    fsp_err_t err = p_ctrl->extend.p_enter(p_ctrl->extend.p_context);
    __disable_irq();
    if (FSP_SUCCESS == err)
    {
        uint32_t expected_mhz = (p_ctrl->extend.mriclk_hz <= 32768U) ? 0U :
                               (p_ctrl->extend.mriclk_hz + 999999U) / 1000000U;
        if ((SCB->CCR & MRAM_PRV_CACHE_MASK) || MRAM_PRV_REGS->MRCPFB ||
            !r_mram_accessible(SCB->VTOR, 1U) ||
            ((MRAM_PRV_REGS->MRCFREQ & 0x3FFU) != expected_mhz) || (MRAM_PRV_REGS->MRCEECC & 1U))
        {
            r_mram_release(p_ctrl, *p_primask);
            return FSP_ERR_INVALID_MODE;
        }
    }
    if (FSP_SUCCESS != err)
    {
        p_ctrl->busy = false;
        __set_PRIMASK(*p_primask);
    }
    return err;
}

static MRAM_CODE_IN_RAM void r_mram_release (mram_instance_ctrl_t * p_ctrl, uint32_t primask)
{
    bool idle = r_mram_idle();
    if (idle)
    {
        __DSB();
        __ISB();
    }
    p_ctrl->extend.p_exit(p_ctrl->extend.p_context, idle);
    p_ctrl->busy = false;
    if (!idle)
    {
        if (!p_ctrl->interrupts_held)
        {
            p_ctrl->saved_primask = primask;
            p_ctrl->interrupts_held = true;
        }
        __disable_irq();
    }
    else
    {
        if (p_ctrl->interrupts_held)
        {
            primask = p_ctrl->saved_primask;
            p_ctrl->interrupts_held = false;
        }
        __set_PRIMASK(primask);
    }
}

/* These operations have no compatible implementation for the qualified code-MRAM scope. */
fsp_err_t R_MRAM_Erase (flash_ctrl_t * const p_ctrl, uint32_t const address, uint32_t const num_blocks)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(address); FSP_PARAMETER_NOT_USED(num_blocks);
    return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_AccessWindowSet (flash_ctrl_t * const p_ctrl, uint32_t const start_addr, uint32_t const end_addr)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(start_addr); FSP_PARAMETER_NOT_USED(end_addr);
    return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_AccessWindowClear (flash_ctrl_t * const p_ctrl)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_IdCodeSet (flash_ctrl_t * const p_ctrl, uint8_t const * const p_id_bytes, flash_id_code_mode_t mode)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(p_id_bytes); FSP_PARAMETER_NOT_USED(mode);
    return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_UpdateFlashClockFreq (flash_ctrl_t * const p_ctrl)
{
    /* Clock control belongs to the BSP. Reopen with the new frozen profile, never silently change MRAM timings. */
    FSP_PARAMETER_NOT_USED(p_ctrl); return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_StartUpAreaSelect (flash_ctrl_t * const p_ctrl, flash_startup_area_swap_t swap_type, bool is_temporary)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(swap_type); FSP_PARAMETER_NOT_USED(is_temporary);
    return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_BankSwap (flash_ctrl_t * const p_ctrl)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_AntiRollbackCounterIncrement (flash_ctrl_t * const p_ctrl, flash_arc_t counter)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(counter); return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_AntiRollbackCounterRefresh (flash_ctrl_t * const p_ctrl, flash_arc_t counter)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(counter); return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_AntiRollbackCounterRead (flash_ctrl_t * const p_ctrl, flash_arc_t counter, uint32_t * const p_count)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(counter); FSP_PARAMETER_NOT_USED(p_count);
    return FSP_ERR_UNSUPPORTED;
}
fsp_err_t R_MRAM_UserLockableAreaWrite (flash_ctrl_t * const p_ctrl, uint32_t const src_address,
                                       uint32_t const flash_address, uint32_t const num_bytes)
{
    FSP_PARAMETER_NOT_USED(p_ctrl); FSP_PARAMETER_NOT_USED(src_address);
    FSP_PARAMETER_NOT_USED(flash_address); FSP_PARAMETER_NOT_USED(num_bytes);
    return FSP_ERR_UNSUPPORTED;
}
