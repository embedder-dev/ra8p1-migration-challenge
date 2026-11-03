/* Standalone, debugger-commanded WDT fixture. Each command causes at most one
 * reset; the retained result is checked before accepting a new command.
 * Only one core may own the GPIO markers or reset-status registers at a time. */
#include "hal_data.h"

#define WDT_TEST_MAGIC       (0x57445451UL)
#define WDT_TEST_CYCLES      (8U)
/* Refresh takes up to four WDT count clocks (hardware manual, Refresh Operation). */
#define WDT_REFRESH_SETTLE_US(hz) ((5000000UL + (hz) - 1U) / (hz))
#define WDT_COMMAND_LEGAL    (1U)
#define WDT_COMMAND_EXPIRE   (2U)
#define WDT_STAGE_READY      (1U)
#define WDT_STAGE_RUNNING    (2U)
#define WDT_STAGE_LEGAL_RESET (3U)
#define WDT_STAGE_EXPIRY      (4U)
#define WDT_STAGE_COMPLETE    (5U)
#define WDT_STAGE_FAILED      (6U)

#if BSP_CFG_CPU_CORE == 0
 #define WDT_TEST_REG   R_WDT0
 #define WDT_TEST_CAUSE R_SYSC_RSTSR1_WDT0RF_Msk
 #if defined(BSP_CFG_ROM_REG_OFS0) && ((BSP_CFG_ROM_REG_OFS0 & (1UL << 17)) == 0)
  #error Qualification requires register-start WDT; never change option memory in this fixture.
 #endif
#else
 #define WDT_TEST_REG   R_WDT1
 #define WDT_TEST_CAUSE R_SYSC_RSTSR1_WDT1RF_Msk
 #if defined(BSP_CFG_ROM_REG_OFS3) && ((BSP_CFG_ROM_REG_OFS3 & (1UL << 17)) == 0)
  #error Qualification requires register-start WDT; never change option memory in this fixture.
 #endif
#endif

/* R01UH1064EJ0130 sections 7.2.1, 7.2.6 and 7.2.7. These are read-only
 * observations, not defaults for the driver's compile-time OFS configuration. */
#define WDT_OPTION_OFS0_ADDRESS     (0x02C9F040UL)
#define WDT_OPTION_OFS3_ADDRESS     (0x12C9F4C4UL)
#define WDT_OPTION_OFS3_SEC_ADDRESS (0x02C9F0C4UL)
#define WDT_OPTION_OFS3_SEL_ADDRESS (0x02C9F124UL)
#define WDT_OPTION_START_MASK       (1UL << 17)

typedef struct st_wdt_boot_snapshot
{
    uint32_t rstsr0;
    uint32_t rstsr1;
    uint32_t rstsr2;
    uint32_t rstsr3;
} wdt_boot_snapshot_t;

typedef struct st_wdt_validation_result
{
    uint32_t magic;
    uint32_t inverse_magic;
    uint32_t core;
    uint32_t stage;
    uint32_t command;
    uint32_t boot_count;
    uint32_t complete;
    uint32_t legal_passed;
    uint32_t expiry_passed;
    uint32_t reset_cause_verified;
    uint32_t expected_reset_cause;
    uint32_t reset_cause_before_clear;
    wdt_boot_snapshot_t boot;
    fsp_err_t error;
    fsp_err_t io_error;
    uint32_t timeout_clocks;
    uint32_t clock_hz;
    uint32_t timeout_ms;
    uint32_t refreshes;
    uint32_t counter_before[WDT_TEST_CYCLES];
    uint32_t counter_after[WDT_TEST_CYCLES];
    uint32_t wdtcr;
    uint32_t wdtrcr;
    uint32_t wdtcstpr;
    uint32_t last_wdtsr;
    uint32_t option_address;
    uint32_t option_value;
    uint32_t option_selection;
} wdt_validation_result_t;

/* Linker must KEEP .noinit outside every startup zero/ECC-clear range. */
volatile wdt_validation_result_t g_validation_wdt __attribute__((section(".noinit"), aligned(32)));
static volatile wdt_boot_snapshot_t g_wdt_boot __attribute__((section(".noinit")));
/* Write 1 for legal refresh, then after reboot write 2 for intentional expiry. */
volatile uint32_t g_wdt_validation_command;
static fsp_err_t g_wdt_io_error;
static bsp_io_port_pin_t const g_wdt_markers[] =
{
    BSP_IO_PORT_05_PIN_14, BSP_IO_PORT_01_PIN_11, BSP_IO_PORT_08_PIN_06, BSP_IO_PORT_08_PIN_07
};

void R_BSP_WarmStart(bsp_warm_start_event_t event);

static void wdt_persist(void)
{
#if defined(__DCACHE_PRESENT) && (__DCACHE_PRESENT == 1U)
    SCB_CleanDCache();
#endif
    __DSB();
}

static void wdt_marker(uint32_t index, bsp_io_level_t level)
{
    fsp_err_t error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, g_wdt_markers[index], level);
    if (FSP_SUCCESS != error) { g_validation_wdt.io_error = error; }
}

void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    if (BSP_WARM_START_RESET == event)
    {
        /* Before C initialization and before anybody clears sticky reset flags. */
        g_wdt_boot.rstsr0 = R_SYSTEM->RSTSR0;
        g_wdt_boot.rstsr1 = R_SYSTEM->RSTSR1;
        g_wdt_boot.rstsr2 = R_SYSTEM->RSTSR2;
        g_wdt_boot.rstsr3 = R_SYSTEM->RSTSR3;
    }
    if (BSP_WARM_START_POST_C == event)
    {
        g_wdt_io_error = R_IOPORT_Open(&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);
    }
}

static void wdt_failure(fsp_err_t error, bool running)
{
    g_validation_wdt.error = error;
    g_validation_wdt.stage = WDT_STAGE_FAILED;
    g_validation_wdt.complete = 1U;
    wdt_marker(3U, BSP_IO_LEVEL_HIGH);
    wdt_persist();
    if (running)
    {
        /* A register-start WDT cannot be stopped. One software reset disarms it;
         * FAILED never automatically rearms on the next boot. */
        (void) R_WDT_Refresh(g_wdt0.p_ctrl);
        NVIC_SystemReset();
    }
}

static uint32_t wdt_read_option(uintptr_t address)
{
    return *(uint32_t const volatile *) address;
}

static bool wdt_register_start_selected(void)
{
#if BSP_CFG_CPU_CORE == 0
    g_validation_wdt.option_selection = 0U;
    g_validation_wdt.option_address = WDT_OPTION_OFS0_ADDRESS;
#else
    g_validation_wdt.option_selection = wdt_read_option(WDT_OPTION_OFS3_SEL_ADDRESS);
    g_validation_wdt.option_address = (g_validation_wdt.option_selection & WDT_OPTION_START_MASK) ?
                                      WDT_OPTION_OFS3_ADDRESS : WDT_OPTION_OFS3_SEC_ADDRESS;
#endif
    g_validation_wdt.option_value = wdt_read_option(g_validation_wdt.option_address);
    return (g_validation_wdt.option_value & WDT_OPTION_START_MASK) != 0U;
}

static fsp_err_t wdt_refresh_cycle(uint32_t index)
{
    uint32_t count = 0U;
    uint32_t polls = g_validation_wdt.timeout_ms * 10U;
    fsp_err_t error;
    do
    {
        error = R_WDT_CounterGet(g_wdt0.p_ctrl, &count);
        if (error != FSP_SUCCESS) { return error; }
        g_validation_wdt.last_wdtsr = WDT_TEST_REG->WDTSR;
        if (g_validation_wdt.last_wdtsr & (R_WDT0_WDTSR_UNDFF_Msk | R_WDT0_WDTSR_REFEF_Msk))
        {
            return FSP_ERR_INVALID_DATA;
        }
        if (count <= g_validation_wdt.timeout_clocks / 2U) { break; }
        R_BSP_SoftwareDelay(100U, BSP_DELAY_UNITS_MICROSECONDS);
    } while (--polls != 0U);
    if ((polls == 0U) || (count < g_validation_wdt.timeout_clocks / 4U))
    {
        return FSP_ERR_TIMEOUT;
    }
    g_validation_wdt.counter_before[index] = count;
    error = R_WDT_Refresh(g_wdt0.p_ctrl);
    if (error != FSP_SUCCESS) { return error; }
    g_validation_wdt.refreshes++;
    wdt_marker(1U, (index & 1U) ? BSP_IO_LEVEL_LOW : BSP_IO_LEVEL_HIGH);
    R_BSP_SoftwareDelay(WDT_REFRESH_SETTLE_US(g_validation_wdt.clock_hz), BSP_DELAY_UNITS_MICROSECONDS);
    error = R_WDT_CounterGet(g_wdt0.p_ctrl, &count);
    g_validation_wdt.counter_after[index] = count;
    if (error != FSP_SUCCESS) { return error; }
    if (count <= (g_validation_wdt.counter_before[index] + g_validation_wdt.timeout_clocks / 4U))
    {
        return FSP_ERR_INVALID_DATA;
    }
    return g_validation_wdt.io_error;
}

void hal_entry(void)
{
    if ((g_validation_wdt.magic != WDT_TEST_MAGIC) ||
        (g_validation_wdt.inverse_magic != (uint32_t) ~WDT_TEST_MAGIC) ||
        (g_validation_wdt.core != BSP_CFG_CPU_CORE))
    {
        volatile unsigned char * p = (volatile unsigned char *) &g_validation_wdt;
        for (uint32_t i = 0U; i < sizeof(g_validation_wdt); i++) { p[i] = 0U; }
        g_validation_wdt.magic = WDT_TEST_MAGIC;
        g_validation_wdt.inverse_magic = (uint32_t) ~WDT_TEST_MAGIC;
        g_validation_wdt.core = BSP_CFG_CPU_CORE;
        g_validation_wdt.stage = WDT_STAGE_READY;
    }
    g_validation_wdt.boot_count++;
    g_validation_wdt.boot.rstsr0 = g_wdt_boot.rstsr0;
    g_validation_wdt.boot.rstsr1 = g_wdt_boot.rstsr1;
    g_validation_wdt.boot.rstsr2 = g_wdt_boot.rstsr2;
    g_validation_wdt.boot.rstsr3 = g_wdt_boot.rstsr3;
    g_validation_wdt.io_error = g_wdt_io_error;
    if (g_wdt_io_error != FSP_SUCCESS) { wdt_failure(g_wdt_io_error, false); return; }
    for (uint32_t i = 0U; i < 4U; i++)
    {
        fsp_err_t error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, g_wdt_markers[i],
                                        IOPORT_CFG_PORT_DIRECTION_OUTPUT | IOPORT_CFG_PORT_OUTPUT_LOW);
        if (error != FSP_SUCCESS) { wdt_failure(error, false); return; }
    }
    wdt_marker(0U, BSP_IO_LEVEL_HIGH);
    if (g_validation_wdt.io_error != FSP_SUCCESS)
    {
        wdt_failure(g_validation_wdt.io_error, false);
        return;
    }
    /* A generated macro cannot prove the latched hardware start mode. Refuse
     * even the command window before any WDT API or reset if auto-start is set. */
    if (!wdt_register_start_selected())
    {
        wdt_failure(FSP_ERR_INVALID_STATE, false);
        return;
    }
    uint32_t stage = g_validation_wdt.stage;
    if ((stage == WDT_STAGE_LEGAL_RESET) || (stage == WDT_STAGE_EXPIRY))
    {
        bool verified = (g_wdt_boot.rstsr1 == g_validation_wdt.expected_reset_cause) &&
                        (g_validation_wdt.error == FSP_SUCCESS) &&
                        (g_validation_wdt.refreshes == WDT_TEST_CYCLES + 1U);
        g_validation_wdt.reset_cause_verified = verified;
        if (!verified) { wdt_failure(FSP_ERR_INVALID_DATA, false); return; }
        wdt_marker(2U, BSP_IO_LEVEL_HIGH);
        if (g_validation_wdt.io_error != FSP_SUCCESS)
        {
            wdt_failure(g_validation_wdt.io_error, false);
            return;
        }
        if (stage == WDT_STAGE_LEGAL_RESET) { g_validation_wdt.legal_passed = 1U; }
        else { g_validation_wdt.expiry_passed = 1U; }
        g_validation_wdt.stage = WDT_STAGE_COMPLETE;
        g_validation_wdt.complete = 1U;
        wdt_persist();
    }
    else if (stage == WDT_STAGE_RUNNING)
    {
        wdt_failure(FSP_ERR_INVALID_DATA, false);
        return;
    }
    else if (stage == WDT_STAGE_FAILED)
    {
        wdt_marker(3U, BSP_IO_LEVEL_HIGH);
        return;
    }
    /* Finite command window; doing nothing never starts the watchdog. */
    uint32_t command = 0U;
    for (uint32_t i = 0U; i < 30000U; i++)
    {
        command = g_wdt_validation_command;
        if (command != 0U) { break; }
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
    }
    if (command == 0U) { return; }
    g_wdt_validation_command = 0U;
    if (((command != WDT_COMMAND_LEGAL) && (command != WDT_COMMAND_EXPIRE)) ||
        ((command == WDT_COMMAND_EXPIRE) && !g_validation_wdt.legal_passed))
    {
        wdt_failure(FSP_ERR_INVALID_ARGUMENT, false);
        return;
    }
    if ((g_wdt0.p_cfg->window_start != WDT_WINDOW_START_100) ||
        (g_wdt0.p_cfg->window_end != WDT_WINDOW_END_0) ||
        (g_wdt0.p_cfg->reset_control != WDT_RESET_CONTROL_RESET) ||
        (g_wdt0.p_cfg->stop_control != WDT_STOP_CONTROL_DISABLE))
    {
        wdt_failure(FSP_ERR_INVALID_ARGUMENT, false);
        return;
    }
    fsp_err_t error = R_WDT_Open(g_wdt0.p_ctrl, g_wdt0.p_cfg);
    if (error != FSP_SUCCESS) { wdt_failure(error, false); return; }
    wdt_timeout_values_t timeout;
    error = R_WDT_TimeoutGet(g_wdt0.p_ctrl, &timeout);
    if ((error != FSP_SUCCESS) || (timeout.clock_frequency_hz == 0U))
    {
        wdt_failure(error != FSP_SUCCESS ? error : FSP_ERR_INVALID_RATE, false);
        return;
    }
    uint32_t timeout_ms = (uint32_t) (((uint64_t) timeout.timeout_clocks * 1000U +
                                     timeout.clock_frequency_hz - 1U) / timeout.clock_frequency_hz);
    if ((timeout_ms < 100U) || (timeout_ms > 5000U))
    {
        wdt_failure(FSP_ERR_INVALID_RATE, false);
        return;
    }
    g_validation_wdt.command = command;
    if (command == WDT_COMMAND_LEGAL) { g_validation_wdt.legal_passed = 0U; }
    g_validation_wdt.expiry_passed = 0U;
    g_validation_wdt.complete = 0U;
    g_validation_wdt.error = FSP_SUCCESS;
    g_validation_wdt.reset_cause_verified = 0U;
    g_validation_wdt.timeout_clocks = timeout.timeout_clocks;
    g_validation_wdt.clock_hz = timeout.clock_frequency_hz;
    g_validation_wdt.timeout_ms = timeout_ms;
    g_validation_wdt.refreshes = 0U;
    g_validation_wdt.wdtcr = WDT_TEST_REG->WDTCR;
    g_validation_wdt.wdtrcr = WDT_TEST_REG->WDTRCR;
    g_validation_wdt.wdtcstpr = WDT_TEST_REG->WDTCSTPR;
    g_validation_wdt.reset_cause_before_clear = R_SYSTEM->RSTSR1;
    /* Manual 6.2.3: read flags before writing zero. All causes are retained above. */
    R_SYSTEM->RSTSR1 = 0U;
    if (R_SYSTEM->RSTSR1 != 0U) { wdt_failure(FSP_ERR_INVALID_STATE, false); return; }
    g_validation_wdt.stage = WDT_STAGE_RUNNING;
    wdt_persist();
    error = R_WDT_Refresh(g_wdt0.p_ctrl);
    if (error != FSP_SUCCESS) { wdt_failure(error, true); return; }
    g_validation_wdt.refreshes++;
    R_BSP_SoftwareDelay(WDT_REFRESH_SETTLE_US(g_validation_wdt.clock_hz), BSP_DELAY_UNITS_MICROSECONDS);
    for (uint32_t i = 0U; i < WDT_TEST_CYCLES; i++)
    {
        error = wdt_refresh_cycle(i);
        if (error != FSP_SUCCESS) { wdt_failure(error, true); return; }
    }
    wdt_marker(2U, BSP_IO_LEVEL_HIGH);
    if (g_validation_wdt.io_error != FSP_SUCCESS) { wdt_failure(g_validation_wdt.io_error, true); return; }
    if (command == WDT_COMMAND_LEGAL)
    {
        g_validation_wdt.expected_reset_cause = R_SYSC_RSTSR1_SWRF_Msk;
        g_validation_wdt.stage = WDT_STAGE_LEGAL_RESET;
        wdt_persist();
        /* Last cycle just refreshed; software reset terminates legal-only stage. */
        NVIC_SystemReset();
    }
    g_validation_wdt.expected_reset_cause = WDT_TEST_CAUSE;
    g_validation_wdt.stage = WDT_STAGE_EXPIRY;
    wdt_persist();
    /* No refresh, halt, WFI or low-power request in this bounded expiry window. */
    for (uint32_t i = 0U; i < 2U * timeout_ms + 100U; i++)
    {
        g_validation_wdt.last_wdtsr = WDT_TEST_REG->WDTSR;
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MILLISECONDS);
    }
    wdt_failure(FSP_ERR_TIMEOUT, true);
}
