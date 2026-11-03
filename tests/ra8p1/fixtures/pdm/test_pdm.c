#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "r_pdm.h"
#include "r_pdm_cfg.h"

#define SR (0x14U / 4U)
#define MDSR (0x20U / 4U)
#define SFCR (0x24U / 4U)
#define DRR (0xE8U / 4U)
#define DSR (0xECU / 4U)
#define ICR (0x0CU / 4U)
#define DRCR (0xE0U / 4U)
mock_pdm_t pdm_test_regs;
unsigned int pdm_test_mask;
static void * contexts[128];
static bool enabled[128];
static IRQn_Type current_irq;
static unsigned int module_starts, module_stops, pending, callbacks;
static uint32_t startup_delay, pclk_hz, start_mask;
static bool stuck, clock_stopped, in_isr;
static pdm_callback_args_t result;
static pdm_instance_ctrl_t ctrl, other;
static pdm_extended_cfg_t ext = {.microphone_startup_us = 10000};
static pdm_cfg_t cfg;
static int32_t samples[256];
static int context_cookie;
void pdm_dat_isr(void);
void pdm_err_isr(void);

void pdm_test_module_start(int ip, int ch)
{
    assert(ip == FSP_IP_PDM && ch == 0);
    module_starts++;
}
void pdm_test_module_stop(int ip, int ch)
{
    assert(ip == FSP_IP_PDM && ch == 0);
    for (unsigned i = 0; i < 3; i++) { assert(!(pdm_test_regs.channel[i][SR] & 1)); }
    module_stops++;
}
void R_BSP_IrqCfgEnable(IRQn_Type irq, uint8_t ipl, void * p)
{
    assert(irq >= 0 && irq < 128 && ipl < 16);
    contexts[irq] = p;
    enabled[irq] = true;
}
void R_BSP_IrqDisable(IRQn_Type irq) { enabled[irq] = false; }
void R_BSP_IrqStatusClear(IRQn_Type irq) { assert(irq >= 0 && irq < 128); }
void R_BSP_IrqClearPending(IRQn_Type irq) { assert(irq >= 0 && irq < 128); }
void NVIC_SetPendingIRQ(IRQn_Type irq) { assert(enabled[irq]); pending++; }
IRQn_Type R_FSP_CurrentIrqGet(void) { return current_irq; }
void * R_FSP_IsrContextGet(IRQn_Type irq) { return contexts[irq]; }
uint32_t R_FSP_SystemClockHzGet(int clock) { assert(clock == FSP_PRIV_CLOCK_PCLKB); return pclk_hz; }
void R_BSP_SoftwareDelay(uint32_t count, int units)
{
    assert(units == BSP_DELAY_UNITS_MICROSECONDS && !in_isr);
    if (count >= 4000)
    {
        startup_delay = count;
        assert(pdm_test_regs.PDCSTRTR != 0);
        start_mask = pdm_test_regs.PDCSTRTR;
        for (unsigned i = 0; i < 3; i++)
        {
            if (pdm_test_regs.PDCSTRTR & (1U << i))
            {
                assert(pdm_test_regs.channel[i][ICR] == 0);
                assert(pdm_test_regs.channel[i][SFCR] == 0x057C0001);
                assert(pdm_test_regs.channel[i][0x28 / 4] == 0x3F61);
                assert(pdm_test_regs.channel[i][0xB4 / 4] == 0x1FF8);
                if (!clock_stopped) { pdm_test_regs.channel[i][SR] = 1; }
            }
        }
        pdm_test_regs.PDCSTRTR = 0;
    }
    if (pdm_test_regs.PDCSTPTR && !stuck)
    {
        for (unsigned i = 0; i < 3; i++)
        {
            if (pdm_test_regs.PDCSTPTR & (1U << i)) { pdm_test_regs.channel[i][SR] = 0; }
        }
        pdm_test_regs.PDCSTPTR = 0;
    }
}
#if BSP_TZ_SECURE_BUILD
bool pdm_test_nonsecure_callback;
void * cmse_check_address_range(void * p, size_t size, int flags)
{
    assert(size && flags == CMSE_AU_NONSECURE);
    return pdm_test_nonsecure_callback ? p : NULL;
}
#endif

static void callback(pdm_callback_args_t * args)
{
    assert(in_isr);
    assert(args->p_context == &context_cookie);
    callbacks++;
    result = *args;
    pdm_status_t status;
    assert(g_pdm_on_pdm.statusGet(&ctrl, &status) == FSP_SUCCESS);
    assert(status.frames == args->frames);
}
static void irq(unsigned ch, uint32_t count, uint32_t sample, uint32_t error, bool err_irq)
{
    pdm_test_regs.channel[ch][DSR] = count;
    pdm_test_regs.channel[ch][DRR] = sample;
    pdm_test_regs.channel[ch][SR] = 1 | error;
    current_irq = err_irq ? cfg.err_irq[ch - cfg.channel] : cfg.dat_irq[ch - cfg.channel];
    in_isr = true;
    if (err_irq) { pdm_err_isr(); } else { pdm_dat_isr(); }
    in_isr = false;
    pdm_test_regs.channel[ch][DSR] = 0;
}
static void reset(void)
{
    memset(&pdm_test_regs, 0, sizeof(pdm_test_regs));
    memset(&ctrl, 0, sizeof(ctrl));
    memset(&other, 0, sizeof(other));
    memset(contexts, 0, sizeof(contexts));
    memset(enabled, 0, sizeof(enabled));
    memset(samples, 0xA5, sizeof(samples));
    callbacks = pending = module_starts = module_stops = 0;
    stuck = clock_stopped = in_isr = false;
    pclk_hz = 100000000;
    start_mask = 0;
    cfg = (pdm_cfg_t) {.channel = 0, .mode = PDM_MODE_MONO, .ipl = 2,
                      .dat_irq = {0, 2}, .err_irq = {1, 3},
                      .p_callback = callback, .p_context = &context_cookie, .p_extend = &ext};
}
static void close_ok(void)
{
    stuck = false;
    assert(g_pdm_on_pdm.close(&ctrl) == FSP_SUCCESS);
    assert(module_stops == 1);
    assert(!enabled[cfg.dat_irq[0]] && !enabled[cfg.err_irq[0]]);
}
static void begin(uint32_t frames)
{
    assert(g_pdm_on_pdm.read(&ctrl, samples, frames) == FSP_SUCCESS);
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_SUCCESS);
    assert(startup_delay == 14000);
}

static void test_invalid(void)
{
    reset();
#if PDM_CFG_PARAM_CHECKING_ENABLE
    assert(g_pdm_on_pdm.open(NULL, &cfg) == FSP_ERR_ASSERTION);
    assert(g_pdm_on_pdm.open(&ctrl, NULL) == FSP_ERR_ASSERTION);
    void const * save = cfg.p_extend; cfg.p_extend = NULL;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_ASSERTION); cfg.p_extend = save;
    assert(g_pdm_on_pdm.read(NULL, samples, 1) == FSP_ERR_ASSERTION);
    assert(g_pdm_on_pdm.statusGet(NULL, NULL) == FSP_ERR_ASSERTION);
#endif
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_NOT_OPEN);
    cfg.channel = 3; assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.channel = 1; cfg.mode = PDM_MODE_STEREO;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_UNSUPPORTED);
    cfg.mode = (pdm_mode_t) 7; assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_UNSUPPORTED);
    cfg.channel = 0; cfg.mode = PDM_MODE_MONO;
    cfg.dat_irq[0] = -1; assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.dat_irq[0] = BSP_ICU_VECTOR_MAX_ENTRIES;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.dat_irq[0] = 0; cfg.err_irq[0] = -1;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.err_irq[0] = BSP_ICU_VECTOR_MAX_ENTRIES;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.err_irq[0] = 1;
    cfg.dat_irq[0] = 1; assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.dat_irq[0] = 0; cfg.ipl = 16; assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    cfg.ipl = 2; ext.microphone_startup_us = 1000001;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    ext.microphone_startup_us = 10000;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_ALREADY_OPEN);
    assert(g_pdm_on_pdm.open(&other, &cfg) == FSP_ERR_IN_USE);
    pdm_cfg_t second = cfg; second.channel = 2;
    assert(g_pdm_on_pdm.open(&other, &second) == FSP_ERR_IN_USE); /* IRQ collision */
#if PDM_CFG_PARAM_CHECKING_ENABLE
    assert(g_pdm_on_pdm.read(&ctrl, NULL, 1) == FSP_ERR_ASSERTION);
    assert(g_pdm_on_pdm.statusGet(&ctrl, NULL) == FSP_ERR_ASSERTION);
#endif
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_INVALID_STATE);
    assert(g_pdm_on_pdm.read(&ctrl, samples, 0) == FSP_ERR_INVALID_ARGUMENT);
    assert(g_pdm_on_pdm.read(&ctrl, samples, UINT32_MAX) == FSP_ERR_INVALID_ARGUMENT);
    assert(g_pdm_on_pdm.read(&ctrl, (int32_t *) ((uintptr_t) samples + 1), 1) == FSP_ERR_INVALID_ARGUMENT);
    assert(g_pdm_on_pdm.read(&ctrl, (int32_t *) (UINTPTR_MAX - 3), 2) == FSP_ERR_INVALID_ARGUMENT);
    assert(g_pdm_on_pdm.read(&ctrl, samples, 1) == FSP_SUCCESS);
    assert(g_pdm_on_pdm.read(&ctrl, samples, 2) == FSP_ERR_IN_USE);
    pclk_hz = 100000001;
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_UNSUPPORTED);
    pclk_hz = 999999;
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_UNSUPPORTED);
    pclk_hz = 100000000;
    pdm_test_regs.channel[0][SR] = 1;
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_IN_USE);
    assert(ctrl.state == PDM_STATE_READY);
    pdm_test_regs.channel[0][SR] = 0;
    assert(g_pdm_on_pdm.stop(&ctrl) == FSP_SUCCESS);
    close_ok();
    assert(g_pdm_on_pdm.close(&ctrl) == FSP_ERR_NOT_OPEN);
}

static void test_mono(void)
{
    reset();
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_SUCCESS);
    pdm_callback_args_t memory = {NULL, PDM_EVENT_ERROR, 123, 456};
    assert(g_pdm_on_pdm.callbackSet(&ctrl, callback, &context_cookie, &memory) == FSP_SUCCESS);
    begin(33);
    irq(0, 0, 123, 0, false); /* Spurious/empty IRQ: no FIFO read, no completion. */
    assert(ctrl.received_frames[0] == 0 && callbacks == 0);
    irq(0, 31, 0x7FFFF, 0, false); /* Valid transient count; work is capped at 30. */
    assert(ctrl.received_frames[0] == 30 && callbacks == 0);
    assert(pending == 1); /* Level still high in the simple register model: explicitly repend. */
    for (unsigned i = 0; i < 30; i++) { assert(samples[i] == 524287); }
    assert(g_pdm_on_pdm.read(&ctrl, samples, 1) == FSP_ERR_IN_USE);
    irq(0, 10, 0x80000, 0, false); /* Truncate to requested length, preserving destination guard. */
    assert(callbacks == 1 && result.frames == 33 && result.event == PDM_EVENT_CAPTURE_COMPLETE);
    assert(samples[30] == -524288 && samples[32] == -524288);
    assert((uint32_t) samples[33] == 0xA5A5A5A5);
    assert(memory.frames == 123 && memory.error_status == 456 && memory.p_context == NULL);
    irq(0, 1, 5, 0, false); assert(callbacks == 1); /* Stale IRQ cannot write/reenter callback. */
    pdm_test_regs.channel[0][SR] = 0; /* Hardware acknowledges the asynchronous stop. */
    pdm_test_regs.PDCSTPTR = 0;
    pdm_status_t status;
    assert(g_pdm_on_pdm.statusGet(&ctrl, &status) == FSP_SUCCESS);
    assert(status.state == PDM_STATE_IDLE && status.frames == 33 && status.sample_rate_hz == 8000);
    begin(1);
    irq(0, 1, 0xFFFFFFFF, 0, false); assert(samples[0] == -1 && result.frames == 1);
    assert(g_pdm_on_pdm.stop(&ctrl) == FSP_SUCCESS);
    assert(g_pdm_on_pdm.callbackSet(&ctrl, NULL, NULL, NULL) == FSP_SUCCESS);
    begin(1); irq(0, 1, 0, 0, false); assert(callbacks == 2 && samples[0] == 0);
    close_ok();
}

static void test_stereo_and_channels(void)
{
    reset(); cfg.mode = PDM_MODE_STEREO;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_SUCCESS);
    pdm_cfg_t second = cfg; second.mode = PDM_MODE_MONO; second.channel = 1;
    second.dat_irq[0] = 4; second.err_irq[0] = 5;
    assert(g_pdm_on_pdm.open(&other, &second) == FSP_ERR_IN_USE);
    begin(2);
    assert(pdm_test_regs.channel[0][MDSR] == 0 && pdm_test_regs.channel[1][MDSR] == 1);
    irq(0, 2, 17, 0, false);
    assert(callbacks == 0 && ctrl.received_frames[0] == 2 && ctrl.received_frames[1] == 0);
    assert(pdm_test_regs.channel[0][ICR] == 0);
    irq(1, 2, 0xFFFFE, 0, false);
    assert(callbacks == 1 && result.frames == 2);
    assert(samples[0] == 17 && samples[1] == -2 && samples[2] == 17 && samples[3] == -2);
    close_ok();
    reset(); cfg.channel = 2;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(ctrl.channels == 1 && ctrl.channel_mask == (1U << 2));
    assert(ctrl.p_reg[0] == pdm_test_regs.channel[2]);
    second = cfg; second.channel = 0; second.dat_irq[0] = 4; second.err_irq[0] = 5;
    assert(g_pdm_on_pdm.open(&other, &second) == FSP_SUCCESS);
    assert(g_pdm_on_pdm.close(&other) == FSP_SUCCESS && module_stops == 0);
    begin(1);
    assert(pdm_test_regs.channel[2][MDSR] == 0); /* Local rising edge on physical input 2. */
    assert(start_mask == (1U << 2));
    irq(2, 1, 42, 0, false);
    assert(samples[0] == 42 && callbacks == 1 && result.frames == 1);
    close_ok();
}

static void test_errors(void)
{
    reset(); assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_SUCCESS);
    begin(10); irq(0, 3, 11, 0, false);
    irq(0, 30, 12, PDM_ERROR_OVERFLOW, true);
    assert(result.event == PDM_EVENT_OVERFLOW && result.frames == 3);
    assert(result.error_status == PDM_ERROR_OVERFLOW);
    assert(pdm_test_regs.channel[0][ICR] == 0 && pdm_test_regs.channel[0][DRCR] == 0);
    assert(g_pdm_on_pdm.stop(&ctrl) == FSP_SUCCESS);
    begin(1); assert(ctrl.error_status == 0);
    irq(0, 32, 0, 0, false); /* Impossible DATNUM fails safely, no overrun. */
    assert(result.event == PDM_EVENT_OVERFLOW && result.frames == 0);
    assert(g_pdm_on_pdm.stop(&ctrl) == FSP_SUCCESS);
    begin(1); irq(0, 1, 0, PDM_ERROR_SHORT_CIRCUIT, true);
    assert(result.event == PDM_EVENT_ERROR && result.error_status == PDM_ERROR_SHORT_CIRCUIT);
    assert(g_pdm_on_pdm.stop(&ctrl) == FSP_SUCCESS);
    begin(2); stuck = true;
    assert(g_pdm_on_pdm.stop(&ctrl) == FSP_ERR_TIMEOUT);
    assert(ctrl.state == PDM_STATE_FAULT && module_stops == 0);
    assert(g_pdm_on_pdm.close(&ctrl) == FSP_ERR_TIMEOUT && ctrl.open != 0);
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_INVALID_STATE);
    stuck = false; assert(g_pdm_on_pdm.stop(&ctrl) == FSP_SUCCESS);
    clock_stopped = true;
    assert(g_pdm_on_pdm.read(&ctrl, samples, 1) == FSP_SUCCESS);
    assert(g_pdm_on_pdm.start(&ctrl) == FSP_ERR_TIMEOUT);
    assert(ctrl.state == PDM_STATE_FAULT);
    close_ok();
}

int main(void)
{
    _Static_assert(offsetof(mock_pdm_t, channel) == 0x100, "channel base");
    _Static_assert(offsetof(mock_pdm_t, PDCDRCR) == 0x24, "common read enable");
    test_invalid(); test_mono(); test_stereo_and_channels(); test_errors();
#if BSP_TZ_SECURE_BUILD
    reset(); pdm_test_nonsecure_callback = true;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_ERR_UNSUPPORTED);
    pdm_test_nonsecure_callback = false;
    assert(g_pdm_on_pdm.open(&ctrl, &cfg) == FSP_SUCCESS);
    pdm_test_nonsecure_callback = true;
    assert(g_pdm_on_pdm.callbackSet(&ctrl, callback, NULL, NULL) == FSP_ERR_UNSUPPORTED);
    pdm_test_nonsecure_callback = false; close_ok();
#endif
    assert(pdm_test_mask == 0);
    puts("PDM host paths passed");
    return 0;
}
