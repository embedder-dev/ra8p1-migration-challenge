#include <assert.h>
#include "bsp_api.h"
R_ADC_B_Type test_adc;
R_DAC0_B_Type test_dac[2];
R_MRAM_Type test_mram;
bool test_mram_transfer_stuck;
R_MRAM_Type * test_mram_access(void)
{
    if (test_mram_transfer_stuck && test_mram.MCTRCNTR == 0x4501)
    {
        test_mram.MCTRSTATR = R_MRAM_MCTRSTATR_TRBUSY_Msk;
    }
    return &test_mram;
}
R_WDT0_Type test_wdt[2];
uint32_t test_ofs[4];
R_ICU_Type test_icu;
R_TSN_Type test_tsn;
R_TSD_Type test_tsd;
R_SYSC_Type test_system;
void * test_irq_context;
IRQn_Type test_irq;
uint32_t test_clocks[] = {100000000, 50000000, 100000000, 50000000};
unsigned test_delay_count;
unsigned test_delays[4096];
unsigned test_dac_states[4096];
bool test_clock_stuck;
unsigned test_module_state[3][2];
void (*test_nmi_callback)(bsp_grp_irq_t);
void test_wait(void) { assert(!"unexpected unbounded wait in host fixture"); }
void test_module(unsigned ip, unsigned channel, unsigned start)
{
    assert(ip < 3 && channel < 2);
    test_module_state[ip][channel] = start;
}
void R_BSP_SoftwareDelay(uint32_t delay, int units)
{
    (void) units;
    assert(test_delay_count < 4096);
    test_delays[test_delay_count] = delay;
    test_dac_states[test_delay_count++] = test_dac[0].DACR0;
    if (!test_clock_stuck) { test_adc.ADCLKSR = test_adc.ADCLKENR; }
}
uint32_t R_FSP_SystemClockHzGet(int clock) { assert(clock >= 0 && clock < 4); return test_clocks[clock]; }
uint32_t R_BSP_SourceClockHzGet(fsp_priv_source_clock_t clock)
{
    assert(clock == 5 || clock == 6);
    return test_clocks[clock == 5 ? 1 : 2];
}
void R_BSP_IrqCfgEnable(IRQn_Type irq, uint8_t priority, void * context)
{ (void)irq; (void)priority; test_irq_context = context; }
void R_BSP_IrqDisable(IRQn_Type irq) { (void)irq; }
void R_BSP_IrqStatusClear(IRQn_Type irq) { (void)irq; }
void R_BSP_GroupIrqWrite(bsp_grp_irq_t irq, void (* callback)(bsp_grp_irq_t))
{ assert(irq == BSP_GRP_IRQ_WDT_ERROR); test_nmi_callback = callback; }
