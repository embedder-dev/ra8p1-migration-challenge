#include <assert.h>
#include <stdio.h>
#include "r_wdt.h"
extern uint32_t test_clocks[];
extern void (*test_nmi_callback)(bsp_grp_irq_t);
#include <string.h>
#ifndef TEST_OFS0
#define TEST_OFS0 0xFFFFFFFFU
#endif
#ifndef TEST_OFS3
#define TEST_OFS3 0xFFFFFFFFU
#endif
#ifndef TEST_OFS3_SEC
#define TEST_OFS3_SEC 0xFFFFFFFFU
#endif
#ifndef TEST_OFS3_SEL
#define TEST_OFS3_SEL 0U
#endif
#ifdef TEST_AUTOSTART
#define TEST_NMI_EXPECTED TEST_AUTOSTART_NMI
#else
#define TEST_NMI_EXPECTED WDT_CFG_REGISTER_START_NMI_SUPPORTED
#endif
static unsigned calls;
static void callback(wdt_callback_args_t * args) { assert(args->p_context == &calls); calls++; }
int main(void)
{
    uint32_t const options[] = {TEST_OFS0, TEST_OFS3, TEST_OFS3_SEC, TEST_OFS3_SEL};
    memcpy(test_ofs, options, sizeof(options));
    unsigned core = BSP_FEATURE_WDT_INSTANCE_COUNT == 2 ? BSP_CFG_CPU_CORE : 0;
    wdt_instance_ctrl_t ctrl = {0};
    wdt_cfg_t cfg = {.timeout = WDT_TIMEOUT_4096, .clock_division = WDT_CLOCK_DIVISION_128,
                    .window_start = WDT_WINDOW_START_100, .window_end = WDT_WINDOW_END_0,
                    .reset_control = WDT_RESET_CONTROL_RESET, .stop_control = WDT_STOP_CONTROL_ENABLE};
    wdt_timeout_values_t timeout;
    assert(g_wdt_on_wdt.refresh == R_WDT_Refresh);
#if BSP_FEATURE_WDT_INSTANCE_COUNT == 2
    assert(R_WDT_Open(NULL, &cfg) == FSP_ERR_ASSERTION);
#ifndef TEST_AUTOSTART
    cfg.timeout = (wdt_timeout_t) 99;
    assert(R_WDT_Open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.timeout = WDT_TIMEOUT_4096;
    cfg.clock_division = WDT_CLOCK_DIVISION_16;
    assert(R_WDT_Open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.clock_division = WDT_CLOCK_DIVISION_128;
    cfg.window_start = WDT_WINDOW_START_25; cfg.window_end = WDT_WINDOW_END_75;
    assert(R_WDT_Open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.window_start = WDT_WINDOW_START_100; cfg.window_end = WDT_WINDOW_END_0;
#endif
#endif
#if TEST_NMI_EXPECTED
    cfg.reset_control = WDT_RESET_CONTROL_NMI; cfg.p_callback = callback; cfg.p_context = &calls;
#else
    (void)callback;
#endif
    assert(R_WDT_Open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(R_WDT_TimeoutGet(&ctrl, &timeout) == FSP_SUCCESS);
#ifndef TEST_AUTOSTART
    assert(test_wdt[core].WDTCR == 0x33F1);
    assert(timeout.timeout_clocks == 4096 && timeout.clock_frequency_hz == (test_clocks[3] >> 7));
    assert(test_wdt[core ^ 1].WDTCR == 0);
#else
    /* OFS3 selects /4, 1024 cycles, full refresh window; OFS0 is different. */
    assert(timeout.timeout_clocks == 1024 && timeout.clock_frequency_hz == (test_clocks[3] >> 2));
    assert(test_wdt[core].WDTCR == 0);
    assert(test_wdt[core].WDTRCR == 0 && test_wdt[core].WDTCSTPR == 0);
#endif
#if BSP_FEATURE_WDT_INSTANCE_COUNT == 2
#ifdef TEST_AUTOSTART
    /* Prohibited clock encodings must not become invented timeout defaults. */
    test_ofs[0] &= ~0x00F00000U;
    test_ofs[1] &= ~0x00F00000U;
    test_ofs[2] &= ~0x00F00000U;
    assert(R_WDT_TimeoutGet(&ctrl, &timeout) == FSP_ERR_INVALID_STATE);
    memcpy(test_ofs, options, sizeof(options));
#else
    uint16_t saved_wdtcr = test_wdt[core].WDTCR;
    test_wdt[core].WDTCR &= (uint16_t) ~0x00F0U;
    assert(R_WDT_TimeoutGet(&ctrl, &timeout) == FSP_ERR_INVALID_STATE);
    test_wdt[core].WDTCR = saved_wdtcr;
#endif
#endif
    assert(R_WDT_Refresh(&ctrl) == FSP_SUCCESS && test_wdt[core].WDTRR == 0xFF);
    test_wdt[core].WDTSR = 0xC123;
    uint32_t count = 0;
    assert(R_WDT_CounterGet(&ctrl, &count) == FSP_SUCCESS && count == 0x123);
#if TEST_NMI_EXPECTED
    wdt_status_t status;
    assert(R_WDT_StatusGet(&ctrl, &status) == FSP_SUCCESS && (unsigned)status == 3);
    assert(test_nmi_callback != NULL);
    test_nmi_callback(BSP_GRP_IRQ_WDT_ERROR);
    assert(calls == 1);
    assert(R_WDT_StatusClear(&ctrl, (wdt_status_t) 3) == FSP_SUCCESS);
    assert((test_wdt[core].WDTSR & 0xC000) == 0);
#if BSP_FEATURE_WDT_INSTANCE_COUNT == 2
    assert(R_WDT_StatusClear(&ctrl, (wdt_status_t) 4) == FSP_ERR_INVALID_ARGUMENT);
#endif
#endif
#if !TEST_NMI_EXPECTED
    wdt_status_t status;
    assert(R_WDT_StatusGet(&ctrl, &status) == FSP_ERR_UNSUPPORTED);
    assert(R_WDT_StatusClear(&ctrl, (wdt_status_t) 3) == FSP_ERR_UNSUPPORTED);
    assert(test_nmi_callback == NULL);
#endif
    assert(memcmp(test_ofs, options, sizeof(options)) == 0);
    puts("WDT behavior passed");
}
