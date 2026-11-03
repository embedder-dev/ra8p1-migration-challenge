/* Standalone qualification application. Never combine with another hal_entry. */
#include "hal_data.h"
#include "r_mram.h"

#define MRAM_TEST_START (0x020FF000UL)
#define MRAM_TEST_END   (0x02100000UL)
#define MRAM_TEST_SIZE  (MRAM_TEST_END - MRAM_TEST_START)

extern const flash_instance_t g_mram0;
extern const ioport_cfg_t IOPORT_CFG_NAME;
extern uint8_t __mram_scratch_start__[] __attribute__((weak));
extern uint8_t __mram_scratch_end__[] __attribute__((weak));
extern uint8_t __Code_In_RAM_Start[] __attribute__((weak));
extern uint8_t __Code_In_RAM_End[] __attribute__((weak));

typedef enum e_mram_test_stage
{
    MRAM_TEST_INITIAL = 0, MRAM_TEST_BLOCKED_LINKER, MRAM_TEST_BLOCKED_RAM,
    MRAM_TEST_BLOCKED_PLATFORM, MRAM_TEST_SAVED, MRAM_TEST_WRITING,
    MRAM_TEST_RESTORING, MRAM_TEST_PASSED, MRAM_TEST_FAILED, MRAM_TEST_BUSY_PARKED
} mram_test_stage_t;

typedef struct st_mram_test_result
{
    uint32_t magic;
    volatile mram_test_stage_t stage;
    volatile fsp_err_t error;
    volatile fsp_err_t restore_error;
    volatile uint32_t patterns_verified;
    volatile uint32_t bounds_rejected;
    volatile uint32_t original_crc;
    volatile uint32_t restored_crc;
    volatile uint32_t observed_crc;
    volatile uint32_t first_mismatch;
    volatile uint32_t saved;
    volatile uint32_t restore_required;
    volatile uint32_t restore_verified;
    volatile uint32_t platform_owned;
    volatile uint32_t nonidle_exits;
} mram_test_result_t;

volatile mram_test_result_t g_validation_mram;
uint8_t g_validation_mram_original[MRAM_TEST_SIZE];
static uint8_t g_mram_pattern[MRAM_TEST_SIZE];
static flash_ctrl_t * gp_mram_ctrl;

void R_BSP_WarmStart(bsp_warm_start_event_t event);
MRAM_CODE_IN_RAM fsp_err_t validation_mram_enter(void const * context);
MRAM_CODE_IN_RAM void validation_mram_exit(void const * context, bool hardware_idle);
MRAM_CODE_IN_RAM uint32_t validation_mram_time_us(void const * context);
MRAM_CODE_IN_RAM fsp_err_t validation_mram_platform_prepare(void);
MRAM_CODE_IN_RAM void validation_mram_platform_finish(void);
MRAM_CODE_IN_RAM uint32_t validation_mram_platform_time_us(void);

typedef enum e_mram_platform_gate
{
    MRAM_GATE_NONE = 0, MRAM_GATE_CONTEXT, MRAM_GATE_CPU1, MRAM_GATE_DMA1,
    MRAM_GATE_MASTERS, MRAM_GATE_DMA0, MRAM_GATE_CLOCK, MRAM_GATE_EXCEPTION,
    MRAM_GATE_CACHE, MRAM_GATE_TIMER, MRAM_GATE_BUSY
} mram_platform_gate_t;

/* Public debugger evidence. No option-setting memory access or security configuration changes. */
volatile mram_platform_gate_t g_validation_mram_gate;
static uint32_t g_mram_vectors[1024] __attribute__((aligned(4096)));
static struct
{
    uint32_t primask, vtor, ccr, csselr, demcr, dwt_ctrl;
    uint32_t cycles, remainder, microseconds;
    uint8_t prefetch;
    bool acquired;
} g_mram_platform;

static MRAM_CODE_IN_RAM __attribute__((noreturn)) void mram_exception_park(void)
{
    __disable_irq();
    g_validation_mram.stage = MRAM_TEST_BUSY_PARKED;
    g_validation_mram_gate = MRAM_GATE_EXCEPTION;
    /* NMI/fault must never return to interrupted code, even if the controller later becomes idle. */
    for (;;) { __NOP(); }
}

static MRAM_CODE_IN_RAM bool mram_masters_stopped(void)
{
    /* R01UH1064EJ0130 tables 15.1/15.2 and 11.2.6-8: all non-CPU bus masters. */
    uint32_t c = R_MSTP_MSTPCRC_MSTPC4_Msk | R_MSTP_MSTPCRC_MSTPC6_Msk |
                 R_MSTP_MSTPCRC_MSTPC10_Msk | R_MSTP_MSTPCRC_MSTPC11_Msk |
                 R_MSTP_MSTPCRC_MSTPC12_Msk | R_MSTP_MSTPCRC_MSTPC16_Msk |
                 R_MSTP_MSTPCRC_MSTPC17_Msk | R_MSTP_MSTPCRC_MSTPC30_Msk |
                 R_MSTP_MSTPCRC_MSTPC31_Msk;
    uint32_t b = R_MSTP_MSTPCRB_MSTPB11_Msk | R_MSTP_MSTPCRB_MSTPB12_Msk;
    return ((R_MSTP->MSTPCRA & R_MSTP_MSTPCRA_MSTPA16_Msk) != 0U) &&
           ((R_MSTP->MSTPCRB & b) == b) && ((R_MSTP->MSTPCRC & c) == c);
}

static MRAM_CODE_IN_RAM bool mram_dma0_idle(void)
{
    /* Never read a stopped module. Do not stop an engine that might have a transfer in flight. */
    if (R_MSTP->MSTPCRA & R_MSTP_MSTPCRA_MSTPA22_Msk) { return true; }
    return !(R_DMA0->DMAST & R_DMA0_DMAST_DMST_Msk) &&
           !(R_DTC0->DTCST & R_DTC0_DTCST_DTCST_Msk) &&
           !(R_DTC0->DTCSTS & R_DTC0_DTCSTS_ACT_Msk) &&
           !((R_DMAC00->DMSTS | R_DMAC01->DMSTS | R_DMAC02->DMSTS | R_DMAC03->DMSTS |
              R_DMAC04->DMSTS | R_DMAC05->DMSTS | R_DMAC06->DMSTS | R_DMAC07->DMSTS) &
             R_DMAC00_DMSTS_ACT_Msk);
}

static MRAM_CODE_IN_RAM bool mram_clock_is_reset_profile(void)
{
    /* Only the unmodified 8 MHz MOCO profile is qualified here. No live clock retuning. */
    return (SystemCoreClock == 8000000U) && (R_SYSTEM->SCKSCR == 1U) &&
           (R_SYSTEM->SCKDIVCR == 0U) && (R_SYSTEM->SCKDIVCR2 == 0U) &&
           (R_SYSTEM->MOCOCR == 0U) && ((R_MRAM->MRCFREQ & 0x3FFU) == 8U);
}

MRAM_CODE_IN_RAM fsp_err_t validation_mram_platform_prepare(void)
{
    uint32_t mask = __get_PRIMASK();
    __disable_irq();
    g_validation_mram_gate = MRAM_GATE_CONTEXT;
    if (g_mram_platform.acquired || BSP_CFG_CPU_CORE != 0 || BSP_TZ_NONSECURE_BUILD ||
        __get_IPSR() || (__get_CONTROL() & (CONTROL_nPRIV_Msk | CONTROL_SPSEL_Msk)) ||
        MPU->CTRL || (__get_MSP() < 0x22000400UL) || (__get_MSP() > 0x221A0000UL)) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_CPU1;
    if (R_CPU_CTRL->CPU1ACTCSR & R_CPU_CTRL_CPU1ACTCSR_ACT_Msk) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_DMA1;
    /* DMA1/DTC1 are CPU1-only, same addresses as DMA0/DTC0 (17.2/18.2). CPU0 cannot inspect
     * or safely stop them. Require established module-stop instead of assuming CPU reset stops DMA. */
    if (!(R_MSTP->MSTPCRA & R_MSTP_MSTPCRA_MSTPA23_Msk)) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_MASTERS;
    if (!mram_masters_stopped()) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_DMA0;
    if (!mram_dma0_idle()) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_CLOCK;
    if (!mram_clock_is_reset_profile()) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_EXCEPTION;
    if ((SCB->AIRCR & SCB_AIRCR_BFHFNMINS_Msk) ||
        (R_CPU_CTRL->CPU0LOCKCR & R_CPU_CTRL_CPU0LOCKCR_LCKSVTAIR_Msk)) { goto reject; }
    /* PRIMASK and the RAM table cover only Secure exceptions. Reject every NS IRQ target. */
    for (uint32_t i = 0; i < 16U; i++) { if (NVIC->ITNS[i]) { goto reject; } }
    g_validation_mram_gate = MRAM_GATE_CACHE;
    if (R_CPU_CTRL->CPU0LOCKCR & R_CPU_CTRL_CPU0LOCKCR_LCKDCAIC_Msk) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_BUSY;
    if ((R_MRAM->MRCPS & 0xE0U) != 0x20U) { goto reject; }
    g_validation_mram_gate = MRAM_GATE_TIMER;
    if (DWT->CTRL & DWT_CTRL_NOCYCCNT_Msk) { goto reject; }

    g_mram_platform.primask = mask;
    g_mram_platform.vtor = SCB->VTOR;
    g_mram_platform.ccr = SCB->CCR;
    g_mram_platform.csselr = SCB->CSSELR;
    g_mram_platform.prefetch = R_MRAM->MRCPFB;
    g_mram_platform.demcr = CoreDebug->DEMCR;
    g_mram_platform.dwt_ctrl = DWT->CTRL;
    g_mram_platform.acquired = true;
    /* Commit dirty data before constructing the table: exception vector fetches must not see
     * an old SRAM copy while the new table exists only in D-cache. MRAM is still idle here. */
    SCB_DisableDCache();
    SCB_DisableICache();
    if (SCB->CCR & (SCB_CCR_DC_Msk | SCB_CCR_IC_Msk))
    {
        g_validation_mram_gate = MRAM_GATE_CACHE;
        goto rollback;
    }
    for (uint32_t i = 0; i < 1024U; i++)
    {
        g_mram_vectors[i] = (uint32_t) (uintptr_t) mram_exception_park;
    }
    g_mram_vectors[0] = __get_MSP();
    __DSB();
    SCB->VTOR = (uint32_t) (uintptr_t) g_mram_vectors;
    __DSB();
    __ISB();
    if (SCB->VTOR != (uint32_t) (uintptr_t) g_mram_vectors)
    {
        g_validation_mram_gate = MRAM_GATE_EXCEPTION;
        goto rollback;
    }
    /* CMSIS operations above are forced-inline into .code_in_ram, including their literal pools. */
    R_MRAM->MRCPFB = 0U;
    __DSB();
    __ISB();
    if ((SCB->CCR & (SCB_CCR_DC_Msk | SCB_CCR_IC_Msk)) || R_MRAM->MRCPFB)
    {
        g_validation_mram_gate = MRAM_GATE_CACHE;
        goto rollback;
    }
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    g_mram_platform.cycles = DWT->CYCCNT;
    g_mram_platform.remainder = 0U;
    g_mram_platform.microseconds = 0U;
    for (uint32_t i = 0; i < 100000U; i++)
    {
        if (DWT->CYCCNT != g_mram_platform.cycles)
        {
            g_validation_mram_gate = MRAM_GATE_NONE;
            return FSP_SUCCESS;
        }
    }
    g_validation_mram_gate = MRAM_GATE_TIMER;
rollback:
    validation_mram_platform_finish();
    return FSP_ERR_INVALID_HW_CONDITION;
reject:
    __set_PRIMASK(mask);
    return FSP_ERR_INVALID_MODE;
}

MRAM_CODE_IN_RAM void validation_mram_platform_finish(void)
{
    if (!g_mram_platform.acquired) { return; }
    if ((R_MRAM->MRCPS & 0xE0U) != 0x20U) { mram_exception_park(); }
    DWT->CTRL = g_mram_platform.dwt_ctrl;
    CoreDebug->DEMCR = g_mram_platform.demcr;
    R_MRAM->MRCPFB = g_mram_platform.prefetch;
    if (g_mram_platform.ccr & SCB_CCR_DC_Msk) { SCB_EnableDCache(); }
    if (g_mram_platform.ccr & SCB_CCR_IC_Msk) { SCB_EnableICache(); }
    SCB->CSSELR = g_mram_platform.csselr;
    __DSB();
    SCB->VTOR = g_mram_platform.vtor;
    __DSB();
    __ISB();
    if (SCB->VTOR != g_mram_platform.vtor ||
        (SCB->CCR & (SCB_CCR_DC_Msk | SCB_CCR_IC_Msk)) !=
        (g_mram_platform.ccr & (SCB_CCR_DC_Msk | SCB_CCR_IC_Msk)) ||
        R_MRAM->MRCPFB != g_mram_platform.prefetch)
    {
        mram_exception_park();
    }
    g_mram_platform.acquired = false;
    __set_PRIMASK(g_mram_platform.primask);
}

MRAM_CODE_IN_RAM uint32_t validation_mram_platform_time_us(void)
{
    /* 8 cycles/us, sampled much more often than the 536 s CYCCNT wrap. Retain fractions and
     * accumulate deltas so the microsecond value itself wraps modulo 2^32, as the driver expects. */
    uint32_t now = DWT->CYCCNT;
    uint32_t delta = now - g_mram_platform.cycles;
    uint32_t fraction = (delta & 7U) + g_mram_platform.remainder;
    g_mram_platform.cycles = now;
    g_mram_platform.microseconds += (delta >> 3) + (fraction >> 3);
    g_mram_platform.remainder = fraction & 7U;
    return g_mram_platform.microseconds;
}

MRAM_CODE_IN_RAM fsp_err_t validation_mram_enter(void const * context)
{
    (void) context;
    if (!g_validation_mram.platform_owned || !g_mram_platform.acquired || !__get_PRIMASK() ||
        (R_CPU_CTRL->CPU1ACTCSR & R_CPU_CTRL_CPU1ACTCSR_ACT_Msk) ||
        !(R_MSTP->MSTPCRA & R_MSTP_MSTPCRA_MSTPA23_Msk) ||
        !mram_masters_stopped() || !mram_dma0_idle() || !mram_clock_is_reset_profile() ||
        SCB->VTOR != (uint32_t) (uintptr_t) g_mram_vectors ||
        (SCB->CCR & (SCB_CCR_DC_Msk | SCB_CCR_IC_Msk)) || R_MRAM->MRCPFB)
    {
        return FSP_ERR_INVALID_MODE;
    }
    return FSP_SUCCESS;
}
MRAM_CODE_IN_RAM void validation_mram_exit(void const * context, bool hardware_idle)
{
    (void) context;
    if (!hardware_idle) { g_validation_mram.nonidle_exits++; }
    /* Ownership covers the whole save/test/restore transaction, not individual pages. */
}
MRAM_CODE_IN_RAM uint32_t validation_mram_time_us(void const * context)
{
    (void) context;
    return validation_mram_platform_time_us();
}

static MRAM_CODE_IN_RAM bool mram_ram_range(uintptr_t address, uint32_t size)
{
    return (address >= 0x22000000UL) && (address < 0x221A0000UL) &&
           (size <= 0x221A0000UL - address);
}
static MRAM_CODE_IN_RAM bool mram_ram_function(uintptr_t address)
{
    address &= ~(uintptr_t) 1U;
    return (address >= (uintptr_t) __Code_In_RAM_Start) &&
           (address < (uintptr_t) __Code_In_RAM_End);
}
static MRAM_CODE_IN_RAM uint32_t mram_crc(volatile uint8_t const * data)
{
    uint32_t crc = UINT32_MAX;
    for (uint32_t i = 0; i < MRAM_TEST_SIZE; i++)
    {
        crc ^= data[i];
        for (uint32_t bit = 0; bit < 8U; bit++)
        {
            crc = (crc >> 1) ^ ((0U - (crc & 1U)) & 0xEDB88320UL);
        }
    }
    return ~crc;
}
static MRAM_CODE_IN_RAM bool mram_compare(uint8_t const * expected)
{
    volatile uint8_t const * actual = (volatile uint8_t const *) MRAM_TEST_START;
    for (uint32_t i = 0; i < MRAM_TEST_SIZE; i++)
    {
        if (actual[i] != expected[i])
        {
            g_validation_mram.first_mismatch = i;
            return false;
        }
    }
    g_validation_mram.observed_crc = mram_crc(actual);
    return g_validation_mram.observed_crc == mram_crc(expected);
}
static MRAM_CODE_IN_RAM void mram_require_idle(void)
{
    flash_status_t status;
    if ((FSP_SUCCESS != R_MRAM_StatusGet(gp_mram_ctrl, &status)) || (FLASH_STATUS_IDLE != status))
    {
        /* No forced stop exists. Never release quiescence, read MRAM, or return to flash while busy. */
        g_validation_mram.stage = MRAM_TEST_BUSY_PARKED;
        for (;;) { __NOP(); }
    }
}
static MRAM_CODE_IN_RAM fsp_err_t mram_write(uint32_t address, uint8_t const * source, uint32_t size)
{
    if ((address < MRAM_TEST_START) || (address >= MRAM_TEST_END) || !size ||
        (size > MRAM_TEST_END - address)) { return FSP_ERR_INVALID_ADDRESS; }
    fsp_err_t error = R_MRAM_Write(gp_mram_ctrl, (uint32_t) (uintptr_t) source, address, size);
    if (FSP_SUCCESS != error) { g_validation_mram.error = error; }
    mram_require_idle();
    return error;
}
static MRAM_CODE_IN_RAM void mram_run(void)
{
    fsp_err_t error = validation_mram_platform_prepare();
    if (FSP_SUCCESS != error)
    {
        g_validation_mram.error = error;
        g_validation_mram.stage = MRAM_TEST_BLOCKED_PLATFORM;
        return;
    }
    g_validation_mram.platform_owned = 1U;
    /* Verify a live independent clock before the first write. */
    uint32_t start = validation_mram_platform_time_us();
    uint32_t polls = 100000U;
    while ((start == validation_mram_platform_time_us()) && --polls) {}
    if (!polls)
    {
        g_validation_mram.error = FSP_ERR_TIMEOUT;
        g_validation_mram.stage = MRAM_TEST_BLOCKED_PLATFORM;
        goto finish;
    }
    mram_require_idle();
    for (uint32_t i = 0; i < MRAM_TEST_SIZE; i++)
    {
        g_validation_mram_original[i] = ((volatile uint8_t const *) MRAM_TEST_START)[i];
    }
    g_validation_mram.original_crc = mram_crc(g_validation_mram_original);
    g_validation_mram.saved = 1U;
    g_validation_mram.stage = MRAM_TEST_SAVED;

    /* These are fixture bounds checks: asking the raw driver to write below scratch would be unsafe. */
    if (FSP_ERR_INVALID_ADDRESS == mram_write(MRAM_TEST_START - 1U, g_mram_pattern, 1U))
        { g_validation_mram.bounds_rejected++; }
    if (FSP_ERR_INVALID_ADDRESS == mram_write(MRAM_TEST_END, g_mram_pattern, 1U))
        { g_validation_mram.bounds_rejected++; }
    if (FSP_ERR_INVALID_ADDRESS == mram_write(MRAM_TEST_END - 1U, g_mram_pattern, 2U))
        { g_validation_mram.bounds_rejected++; }
    if (FSP_ERR_INVALID_ADDRESS == mram_write(UINT32_MAX - 7U, g_mram_pattern, 16U))
        { g_validation_mram.bounds_rejected++; }
    if (FSP_ERR_INVALID_ADDRESS == mram_write(MRAM_TEST_START, g_mram_pattern, 0U))
        { g_validation_mram.bounds_rejected++; }
    if (g_validation_mram.bounds_rejected != 5U)
    {
        error = FSP_ERR_INVALID_ARGUMENT;
        goto restore;
    }
    for (uint32_t pattern = 0; pattern < 4U; pattern++)
    {
        for (uint32_t i = 0; i < MRAM_TEST_SIZE; i++)
        {
            g_mram_pattern[i] = (pattern == 0U) ? 0x00U : (pattern == 1U) ? 0xFFU :
                                (pattern == 2U) ? (uint8_t) i : (uint8_t) (i ^ (i >> 8) ^ 0xA5U);
        }
        g_validation_mram.stage = MRAM_TEST_WRITING;
        g_validation_mram.restore_required = 1U; /* A failed call may still have written a prefix. */
        error = mram_write(MRAM_TEST_START, g_mram_pattern, MRAM_TEST_SIZE);
        if (FSP_SUCCESS != error) { goto restore; }
        if (!mram_compare(g_mram_pattern)) { error = FSP_ERR_WRITE_FAILED; goto restore; }
        g_validation_mram.patterns_verified++;
    }
    /* Byte-granular overwrite straddles two 32-byte pages and preserves adjacent bytes. */
    for (uint32_t i = 3U; i < 62U; i++) { g_mram_pattern[i] ^= 0xFFU; }
    error = mram_write(MRAM_TEST_START + 3U, g_mram_pattern + 3U, 59U);
    if ((FSP_SUCCESS == error) && !mram_compare(g_mram_pattern)) { error = FSP_ERR_WRITE_FAILED; }
    if (FSP_SUCCESS == error) { g_validation_mram.patterns_verified++; }
restore:
    g_validation_mram.error = error;
    g_validation_mram.stage = MRAM_TEST_RESTORING;
    /* Recover only while idle, then attempt restoration even after a test failure. */
    g_validation_mram.restore_error = R_MRAM_Reset(gp_mram_ctrl);
    if (FSP_SUCCESS == g_validation_mram.restore_error)
    {
        g_validation_mram.restore_error = mram_write(MRAM_TEST_START, g_validation_mram_original, MRAM_TEST_SIZE);
    }
    if ((FSP_SUCCESS == g_validation_mram.restore_error) && mram_compare(g_validation_mram_original))
    {
        g_validation_mram.restored_crc = g_validation_mram.observed_crc;
        g_validation_mram.restore_verified = 1U;
        g_validation_mram.restore_required = 0U;
    }
    else { g_validation_mram.restore_error = FSP_ERR_WRITE_FAILED; }
    g_validation_mram.stage = ((FSP_SUCCESS == error) && g_validation_mram.restore_verified) ?
                              MRAM_TEST_PASSED : MRAM_TEST_FAILED;
finish:
    mram_require_idle();
    validation_mram_platform_finish();
    g_validation_mram.platform_owned = 0U;
}

void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    if (BSP_WARM_START_POST_C == event)
    {
        g_validation_mram.error = R_IOPORT_Open(&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);
    }
}
void hal_entry(void)
{
    g_validation_mram.magic = 0x4D52414DUL;
    g_validation_mram.first_mismatch = UINT32_MAX;
    gp_mram_ctrl = g_mram0.p_ctrl;
    if (FSP_SUCCESS != g_validation_mram.error) { g_validation_mram.stage = MRAM_TEST_FAILED; return; }
    if ((uintptr_t) __mram_scratch_start__ != MRAM_TEST_START ||
        (uintptr_t) __mram_scratch_end__ != MRAM_TEST_END)
    {
        g_validation_mram.stage = MRAM_TEST_BLOCKED_LINKER;
        g_validation_mram.error = FSP_ERR_INVALID_ADDRESS;
        return;
    }
    uintptr_t stack = (uintptr_t) &stack;
    if ((BSP_CFG_CPU_CORE != 0) || !mram_ram_range((uintptr_t) __Code_In_RAM_Start, 1U) ||
        ((uintptr_t) __Code_In_RAM_End > 0x221A0000UL) ||
        ((uintptr_t) __Code_In_RAM_End <= (uintptr_t) __Code_In_RAM_Start) ||
        !mram_ram_range((uintptr_t) g_mram_vectors, sizeof(g_mram_vectors)) ||
        !mram_ram_range((uintptr_t) &g_mram_platform, sizeof(g_mram_platform)) ||
        !mram_ram_range((uintptr_t) &g_validation_mram_gate, sizeof(g_validation_mram_gate)) ||
        !mram_ram_range((uintptr_t) &gp_mram_ctrl, sizeof(gp_mram_ctrl)) ||
        !mram_ram_range((uintptr_t) &SystemCoreClock, sizeof(SystemCoreClock)) ||
        !mram_ram_range(stack, sizeof(stack)) ||
        !mram_ram_range((uintptr_t) g_validation_mram_original, MRAM_TEST_SIZE) ||
        !mram_ram_range((uintptr_t) g_mram_pattern, MRAM_TEST_SIZE) ||
        !mram_ram_range((uintptr_t) &g_validation_mram, sizeof(g_validation_mram)) ||
        !mram_ram_range((uintptr_t) gp_mram_ctrl, sizeof(mram_instance_ctrl_t)) ||
        !mram_ram_function((uintptr_t) mram_run) || !mram_ram_function((uintptr_t) R_MRAM_Write) ||
        !mram_ram_function((uintptr_t) R_MRAM_StatusGet) || !mram_ram_function((uintptr_t) R_MRAM_Reset) ||
        !mram_ram_function((uintptr_t) mram_exception_park) ||
        !mram_ram_function((uintptr_t) validation_mram_enter) ||
        !mram_ram_function((uintptr_t) validation_mram_exit) ||
        !mram_ram_function((uintptr_t) validation_mram_time_us) ||
        !mram_ram_function((uintptr_t) validation_mram_platform_prepare) ||
        !mram_ram_function((uintptr_t) validation_mram_platform_finish) ||
        !mram_ram_function((uintptr_t) validation_mram_platform_time_us))
    {
        g_validation_mram.stage = MRAM_TEST_BLOCKED_RAM;
        g_validation_mram.error = FSP_ERR_INVALID_MODE;
        return;
    }
    mram_extended_cfg_t const * config = g_mram0.p_cfg->p_extend;
    if (!config || config->code_start != MRAM_CODE_SECURE_START ||
        config->mriclk_hz != 8000000U || config->mrpclk_hz != 8000000U ||
        config->p_enter != validation_mram_enter || config->p_exit != validation_mram_exit ||
        config->p_time_us != validation_mram_time_us)
    {
        g_validation_mram.stage = MRAM_TEST_BLOCKED_PLATFORM;
        g_validation_mram.error = FSP_ERR_INVALID_ARGUMENT;
        return;
    }
    g_validation_mram.error = R_MRAM_Open(gp_mram_ctrl, g_mram0.p_cfg);
    if (FSP_SUCCESS != g_validation_mram.error) { g_validation_mram.stage = MRAM_TEST_FAILED; return; }
    static const bsp_io_port_pin_t pins[] =
    {
        BSP_IO_PORT_05_PIN_14, BSP_IO_PORT_01_PIN_11, BSP_IO_PORT_08_PIN_06, BSP_IO_PORT_08_PIN_07
    };
    for (uint32_t i = 0; i < 4U; i++)
    {
        g_validation_mram.error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, pins[i],
                                                 IOPORT_CFG_PORT_DIRECTION_OUTPUT | IOPORT_CFG_PORT_OUTPUT_LOW);
        if (FSP_SUCCESS != g_validation_mram.error) { g_validation_mram.stage = MRAM_TEST_FAILED; return; }
    }
    (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[0], BSP_IO_LEVEL_HIGH);
    mram_run();
    (void) R_MRAM_Close(gp_mram_ctrl);
    (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[1],
                            g_validation_mram.stage == MRAM_TEST_PASSED ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
    (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[2],
                            g_validation_mram.restore_verified ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
    (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[3],
                            g_validation_mram.stage != MRAM_TEST_PASSED ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
}
