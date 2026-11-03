#include "hal_data.h"
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <string.h>
#include "../../qualification_mram.c"

host_scb_t host_scb;
host_dwt_t host_dwt;
host_mpu_t host_mpu;
host_debug_t host_debug;
host_nvic_t host_nvic;
R_MRAM_Type host_mram;
R_MSTP_Type host_mstp;
R_CPU_CTRL_Type host_cpu;
R_SYSC_Type host_sys;
R_DMA0_Type host_dma;
R_DTC0_Type host_dtc;
R_DMAC00_Type host_channel[8];
uint32_t host_mask, host_ipsr, host_control, host_stack, SystemCoreClock;
bool host_timer_stopped, host_cache_stuck;
const ioport_cfg_t host_io_cfg = {0};
uint32_t host_io_ctrl;
const flash_instance_t g_mram0 = {0};
static uint32_t writes, disables, enables;
static jmp_buf parked;
/* Host placeholders deliberately do not constitute the target linker reservation. */
uint8_t __Code_In_RAM_Start[1], __Code_In_RAM_End[1];
uint8_t __mram_scratch_start__[1], __mram_scratch_end__[1];

host_dwt_t * host_timer(void)
{
    if (!host_timer_stopped && (host_dwt.CTRL & DWT_CTRL_CYCCNTENA_Msk)) { host_dwt.CYCCNT += 8U; }
    return &host_dwt;
}
void host_nop(void) { longjmp(parked, 1); }
void host_cache_disable(uint32_t mask)
{
    assert(host_mask && host_scb.VTOR == 0x02000000U);
    if (!host_cache_stuck) { host_scb.CCR &= ~mask; }
    disables++;
}
void host_cache_enable(uint32_t mask) { host_scb.CCR |= mask; enables++; }
fsp_err_t R_MRAM_Open(flash_ctrl_t * const c, flash_cfg_t const * const f) { (void)c;(void)f;return FSP_SUCCESS; }
fsp_err_t R_MRAM_Write(flash_ctrl_t * const c, uint32_t const s, uint32_t const a, uint32_t const n)
{ (void)c;(void)s;(void)a;(void)n;writes++;return FSP_SUCCESS; }
fsp_err_t R_MRAM_Reset(flash_ctrl_t * const c) { (void)c;return FSP_SUCCESS; }
fsp_err_t R_MRAM_Close(flash_ctrl_t * const c) { (void)c;return FSP_SUCCESS; }
fsp_err_t R_MRAM_StatusGet(flash_ctrl_t * const c, flash_status_t * const s)
{ (void)c;*s=FLASH_STATUS_IDLE;return FSP_SUCCESS; }
fsp_err_t R_IOPORT_Open(void * c, void const * f) { (void)c;(void)f;return FSP_SUCCESS; }
fsp_err_t R_IOPORT_PinCfg(void * c, uint32_t p, uint32_t f) { (void)c;(void)p;(void)f;return FSP_SUCCESS; }
fsp_err_t R_IOPORT_PinWrite(void * c, uint32_t p, uint32_t l) { (void)c;(void)p;(void)l;return FSP_SUCCESS; }

static void reset_model(void)
{
    memset(&g_mram_platform, 0, sizeof(g_mram_platform));
    memset((void*)&g_validation_mram, 0, sizeof(g_validation_mram));
    memset(&host_mram, 0, sizeof(host_mram));
    memset(&host_cpu, 0, sizeof(host_cpu));
    memset(&host_sys, 0, sizeof(host_sys));
    memset(&host_scb, 0, sizeof(host_scb));
    memset(&host_dwt, 0, sizeof(host_dwt));
    memset(&host_debug, 0, sizeof(host_debug));
    memset(&host_nvic, 0, sizeof(host_nvic));
    memset(&host_dma, 0, sizeof(host_dma));
    memset(&host_dtc, 0, sizeof(host_dtc));
    memset(&host_channel, 0, sizeof(host_channel));
    host_mpu.CTRL=0U; host_ipsr=0U; host_control=0U;
    host_mask=0U; host_stack=0x2219F000U; SystemCoreClock=8000000U;
    host_mstp.MSTPCRA=UINT32_MAX & ~R_MSTP_MSTPCRA_MSTPA22_Msk; /* idle DMA0/DTC0 clocked */
    host_mstp.MSTPCRB=UINT32_MAX; host_mstp.MSTPCRC=UINT32_MAX;
    host_sys.SCKSCR=1U; host_mram.MRCFREQ=8U; host_mram.MRCPS=0x20U;
    host_mram.MRCPFB=1U; host_scb.VTOR=0x02000000U;
    host_scb.CCR=SCB_CCR_DC_Msk|SCB_CCR_IC_Msk; host_scb.CSSELR=2U;
    host_debug.DEMCR=0x100U; host_timer_stopped=false; host_cache_stuck=false;
    writes=0U; disables=0U; enables=0U;
}
static void reject(mram_platform_gate_t gate)
{
    mram_run();
    assert(g_validation_mram.stage==MRAM_TEST_BLOCKED_PLATFORM);
    assert(g_validation_mram_gate==gate && writes==0U && host_mask==0U);
    assert(!g_mram_platform.acquired && disables==0U && enables==0U);
}
int main(void)
{
    reset_model(); host_cpu.CPU1ACTCSR=0x80U; reject(MRAM_GATE_CPU1);
    reset_model(); host_mstp.MSTPCRA &= ~R_MSTP_MSTPCRA_MSTPA23_Msk; reject(MRAM_GATE_DMA1);
    reset_model(); host_mstp.MSTPCRA &= ~R_MSTP_MSTPCRA_MSTPA16_Msk; reject(MRAM_GATE_MASTERS);
    uint32_t cmasters[]={4,6,10,11,12,16,17,30,31};
    for (unsigned i=0;i<sizeof(cmasters)/sizeof(cmasters[0]);i++)
    { reset_model();host_mstp.MSTPCRC &= ~(1UL<<cmasters[i]);reject(MRAM_GATE_MASTERS); }
    reset_model();host_mstp.MSTPCRB &= ~(1UL<<11);reject(MRAM_GATE_MASTERS);
    reset_model();host_mstp.MSTPCRB &= ~(1UL<<12);reject(MRAM_GATE_MASTERS);
    reset_model();host_dma.DMAST=1U;reject(MRAM_GATE_DMA0);
    reset_model();host_dtc.DTCST=1U;reject(MRAM_GATE_DMA0);
    reset_model();host_dtc.DTCSTS=0x8000U;reject(MRAM_GATE_DMA0);
    for (unsigned i=0;i<8;i++) { reset_model();host_channel[i].DMSTS=0x80U;reject(MRAM_GATE_DMA0); }
    reset_model();host_control=1U;reject(MRAM_GATE_CONTEXT);
    reset_model();host_control=2U;reject(MRAM_GATE_CONTEXT);
    reset_model();host_ipsr=2U;reject(MRAM_GATE_CONTEXT);
    reset_model();host_mpu.CTRL=1U;reject(MRAM_GATE_CONTEXT);
    reset_model();host_stack=0x22000100U;reject(MRAM_GATE_CONTEXT);
    reset_model();SystemCoreClock=240000000U;reject(MRAM_GATE_CLOCK);
    reset_model();host_sys.SCKDIVCR=1U;reject(MRAM_GATE_CLOCK);
    reset_model();host_sys.SCKDIVCR2=1U;reject(MRAM_GATE_CLOCK);
    reset_model();host_sys.MOCOCR=1U;reject(MRAM_GATE_CLOCK);
    reset_model();host_mram.MRCFREQ=60U;reject(MRAM_GATE_CLOCK);
    reset_model();host_scb.AIRCR=SCB_AIRCR_BFHFNMINS_Msk;reject(MRAM_GATE_EXCEPTION);
    reset_model();host_nvic.ITNS[15]=1U;reject(MRAM_GATE_EXCEPTION);
    reset_model();host_cpu.CPU0LOCKCR=1U;reject(MRAM_GATE_EXCEPTION);
    reset_model();host_cpu.CPU0LOCKCR=32U;reject(MRAM_GATE_CACHE);
    reset_model();host_mram.MRCPS=0x80U;reject(MRAM_GATE_BUSY);
    reset_model();host_dwt.CTRL=DWT_CTRL_NOCYCCNT_Msk;reject(MRAM_GATE_TIMER);
    reset_model();host_cache_stuck=true;
    assert(validation_mram_platform_prepare()==FSP_ERR_INVALID_HW_CONDITION);
    assert(g_validation_mram_gate==MRAM_GATE_CACHE && writes==0U && !host_mask);
    assert(host_scb.VTOR==0x02000000U && !g_mram_platform.acquired);
    reset_model();host_timer_stopped=true;
    assert(validation_mram_platform_prepare()==FSP_ERR_INVALID_HW_CONDITION);
    assert(g_validation_mram_gate==MRAM_GATE_TIMER && !host_mask && writes==0U);
    assert(!g_mram_platform.acquired && host_scb.VTOR==0x02000000U && host_mram.MRCPFB==1U);
    assert(disables==2U && enables==2U && host_dwt.CTRL==0U && host_debug.DEMCR==0x100U);

    reset_model();
    assert(validation_mram_platform_prepare()==FSP_SUCCESS);
    assert(g_validation_mram_gate==MRAM_GATE_NONE && host_mask==1U && disables==2U);
    assert(host_scb.VTOR==(uint32_t)(uintptr_t)g_mram_vectors && host_scb.CCR==0U && host_mram.MRCPFB==0U);
    for (unsigned i=1;i<1024;i++) { assert(g_mram_vectors[i]==(uint32_t)(uintptr_t)mram_exception_park); }
    g_validation_mram.platform_owned=1U;
    assert(validation_mram_enter(NULL)==FSP_SUCCESS);
    host_cpu.CPU1ACTCSR=0x80U;assert(validation_mram_enter(NULL)!=FSP_SUCCESS);host_cpu.CPU1ACTCSR=0U;
    uint32_t time=validation_mram_platform_time_us();
    assert(validation_mram_platform_time_us()>time);
    host_timer_stopped=true;
    g_mram_platform.cycles=UINT32_MAX-7U;host_dwt.CYCCNT=8U;g_mram_platform.microseconds=UINT32_MAX;
    assert(validation_mram_platform_time_us()==1U);
    g_mram_platform.remainder=7U;host_dwt.CYCCNT++;
    assert(validation_mram_platform_time_us()==2U);
    validation_mram_exit(NULL,false);assert(host_mask==1U && g_mram_platform.acquired);
    validation_mram_platform_finish();
    assert(!host_mask && !g_mram_platform.acquired && host_scb.CSSELR==2U);
    assert(host_scb.VTOR==0x02000000U && enables==2U && host_mram.MRCPFB==1U);
    assert(host_dwt.CTRL==0U && host_debug.DEMCR==0x100U);

    reset_model();host_scb.CCR=0U;
    assert(validation_mram_platform_prepare()==FSP_SUCCESS);
    validation_mram_platform_finish();assert(enables==0U && host_scb.CCR==0U);
    reset_model();host_mask=1U;
    assert(validation_mram_platform_prepare()==FSP_SUCCESS);
    validation_mram_platform_finish();assert(host_mask==1U);
    reset_model();host_mstp.MSTPCRA |= R_MSTP_MSTPCRA_MSTPA22_Msk;
    host_dma.DMAST=1U; /* inaccessible while stopped: must not be consulted */
    assert(validation_mram_platform_prepare()==FSP_SUCCESS);
    host_mram.MRCPS=0x80U;
    if (!setjmp(parked)) { validation_mram_platform_finish();assert(0); }
    assert(host_mask==1U && g_mram_platform.acquired && enables==0U);
    assert(g_validation_mram.stage==MRAM_TEST_BUSY_PARKED);
    reset_model();
    assert(mram_write(0x020FEFFFU,g_mram_pattern,1U)==FSP_ERR_INVALID_ADDRESS);
    assert(mram_write(0x02100000U,g_mram_pattern,1U)==FSP_ERR_INVALID_ADDRESS);
    assert(mram_write(0x020FFFFFU,g_mram_pattern,2U)==FSP_ERR_INVALID_ADDRESS);
    assert(mram_write(UINT32_MAX-7U,g_mram_pattern,16U)==FSP_ERR_INVALID_ADDRESS);
    assert(mram_write(0x020FF000U,g_mram_pattern,0U)==FSP_ERR_INVALID_ADDRESS);
    assert(writes==0U);
    hal_entry();
    assert(g_validation_mram.stage==MRAM_TEST_BLOCKED_LINKER && writes==0U);
    puts("MRAM qualification platform gates and restoration passed");
    return 0;
}
