/*
 * Copyright (c) 2026 Renesas Electronics Corporation and/or its affiliates
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */
#ifndef R_MRAM_H
#define R_MRAM_H

#include "bsp_api.h"
#include "r_flash_api.h"
#include "r_mram_cfg.h"

FSP_HEADER

/*******************************************************************************************************************//**
 * @ingroup HAL_Library
 * @defgroup MRAM Code MRAM (r_mram)
 * @brief Blocking RA8P1 code MRAM programming through the FSP 5.9 flash interface.
 * @{
 *
 * Only the code-MRAM data aliases are accepted, never the instruction aliases or extra MRAM. Writes can start at any
 * byte and have any nonzero byte length; the driver splits them at 32-byte boundaries. Erase is not required or supported.
 * BlankCheck compares against 0xFF in software. Synchronous calls do not issue BGO callbacks. An error may leave an
 * already-programmed prefix of the request; there is no rollback. Reopen after changing the BSP clock profile.
 *
 * Link .code_in_ram into initialized executable RAM, including its literal pools. Write, Reset, the calling code and
 * their complete call chains must execute outside every code-MRAM alias. The source, control block, stack, vector table
 * and exception handlers must remain accessible without reading MRAM. Write checks the directly observable conditions,
 * not the contents of every possible exception handler. No driver can discover all DMA masters or the other CPU's state.
 *
 * Before Write/BlankCheck/Reset, p_enter must acquire a system-wide lock, quiesce the other core and all MRAM bus masters,
 * freeze clocks/standby, and clean/disable instruction and data caches and MRAM prefetch on both cores. It must ensure
 * NMI/fault handlers and vectors reside outside MRAM. p_exit releases that ownership and restores cache state only when
 * hardware_idle is true. If false, retain the quiesced state and perform application-level fault recovery outside MRAM;
 * there is no code-MRAM forced-stop command. Local IRQs remain masked until a later idle Reset restores the saved
 * PRIMASK (any later memory operation that confirms idle can release it). While hardware is busy, use direct RAM-resident StatusGet
 * and Reset calls, not an API table stored in MRAM. No option, protection-cancel or security registers are modified.
 */

#if defined(__GNUC__)
 #define MRAM_CODE_IN_RAM    __attribute__((noinline)) BSP_PLACE_IN_SECTION(".code_in_ram")
#else
 #define MRAM_CODE_IN_RAM    BSP_PLACE_IN_SECTION(".code_in_ram")
#endif

#define MRAM_CODE_SECURE_START       (0x02000000UL)
#define MRAM_CODE_NONSECURE_START    (0x12000000UL)
#define MRAM_PROGRAM_BUFFER_SIZE     (32U)

/** Platform coordination, mandatory for memory accesses. Clock values must describe the frozen BSP clock profile.
 * The driver verifies MRCFREQ but does not change it. Configure memory timing through the BSP before changing clocks.
 */
typedef struct st_mram_extended_cfg
{
    uint32_t code_start;              ///< One of MRAM_CODE_SECURE_START or MRAM_CODE_NONSECURE_START.
    uint32_t mriclk_hz;               ///< Actual MRICLK frequency, not the CPU clock.
    uint32_t mrpclk_hz;               ///< Actual MRPCLK frequency.
    uint32_t timeout_us;              ///< Per-page timeout, at least the calculated worst-case time plus margin.
    fsp_err_t (* p_enter)(void const * p_context);
    void (* p_exit)(void const * p_context, bool hardware_idle);
    uint32_t (* p_time_us)(void const * p_context); ///< Free-running wrapping microseconds, independent of interrupts.
    void const * p_context;
} mram_extended_cfg_t;

/** Private state. Allocate zero-initialized writable RAM; do not modify while open. */
typedef struct st_mram_instance_ctrl
{
    uint32_t opened;
    bool busy;
    bool faulted;
    bool interrupts_held;
    uint32_t saved_primask;
    uint32_t poll_limit;
    mram_extended_cfg_t extend;
    flash_block_info_t region;
    void (* p_callback)(flash_callback_args_t * p_args);
    void const * p_context;
    flash_callback_args_t * p_callback_memory;
} mram_instance_ctrl_t;

extern flash_api_t const g_flash_on_mram;

fsp_err_t R_MRAM_Open(flash_ctrl_t * const p_ctrl, flash_cfg_t const * const p_cfg);
MRAM_CODE_IN_RAM fsp_err_t R_MRAM_Write(flash_ctrl_t * const p_ctrl, uint32_t const src_address,
                                       uint32_t const flash_address, uint32_t const num_bytes);
MRAM_CODE_IN_RAM fsp_err_t R_MRAM_Close(flash_ctrl_t * const p_ctrl);
fsp_err_t R_MRAM_InfoGet(flash_ctrl_t * const p_ctrl, flash_info_t * const p_info);
MRAM_CODE_IN_RAM fsp_err_t R_MRAM_StatusGet(flash_ctrl_t * const p_ctrl, flash_status_t * const p_status);
MRAM_CODE_IN_RAM fsp_err_t R_MRAM_BlankCheck(flash_ctrl_t * const p_ctrl, uint32_t const address,
                                            uint32_t const num_bytes, flash_result_t * const p_result);
MRAM_CODE_IN_RAM fsp_err_t R_MRAM_Reset(flash_ctrl_t * const p_ctrl);
fsp_err_t R_MRAM_CallbackSet(flash_ctrl_t * const p_ctrl, void (* p_callback)(flash_callback_args_t *),
                             void const * const p_context, flash_callback_args_t * const p_callback_memory);
fsp_err_t R_MRAM_Erase(flash_ctrl_t * const p_ctrl, uint32_t const address, uint32_t const num_blocks);
fsp_err_t R_MRAM_AccessWindowSet(flash_ctrl_t * const p_ctrl, uint32_t const start_addr, uint32_t const end_addr);
fsp_err_t R_MRAM_AccessWindowClear(flash_ctrl_t * const p_ctrl);
fsp_err_t R_MRAM_IdCodeSet(flash_ctrl_t * const p_ctrl, uint8_t const * const p_id_bytes, flash_id_code_mode_t mode);
fsp_err_t R_MRAM_UpdateFlashClockFreq(flash_ctrl_t * const p_ctrl);
fsp_err_t R_MRAM_StartUpAreaSelect(flash_ctrl_t * const p_ctrl, flash_startup_area_swap_t swap_type, bool is_temporary);
fsp_err_t R_MRAM_BankSwap(flash_ctrl_t * const p_ctrl);
fsp_err_t R_MRAM_AntiRollbackCounterIncrement(flash_ctrl_t * const p_ctrl, flash_arc_t counter);
fsp_err_t R_MRAM_AntiRollbackCounterRefresh(flash_ctrl_t * const p_ctrl, flash_arc_t counter);
fsp_err_t R_MRAM_AntiRollbackCounterRead(flash_ctrl_t * const p_ctrl, flash_arc_t counter, uint32_t * const p_count);
fsp_err_t R_MRAM_UserLockableAreaWrite(flash_ctrl_t * const p_ctrl, uint32_t const src_address,
                                      uint32_t const flash_address, uint32_t const num_bytes);

#if !BSP_FEATURE_MRAM_VERSION
 #error "r_mram requires a code MRAM peripheral."
#endif

/** @} */
FSP_FOOTER
#endif
