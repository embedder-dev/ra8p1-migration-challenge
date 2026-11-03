#ifndef ANALOG_WDT_TEST_BSP_API_H
#define ANALOG_WDT_TEST_BSP_API_H
#define BSP_API_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "fsp_common_api.h"
#include "analog_registers.h"
typedef int32_t IRQn_Type;
typedef int elc_peripheral_t;
typedef int elc_event_t;
#define __I volatile const
#define __PACKED_STRUCT struct __attribute__((packed))
#define BSP_OVERRIDE_ADC_INCLUDE
#define BSP_CMSE_NONSECURE_CALL
#define BSP_TZ_SECURE_BUILD 0
#define BSP_TZ_NONSECURE_BUILD 0
#define BSP_CFG_PARAM_CHECKING_ENABLE 1
#define BSP_FEATURE_ADC_B_PGA_SUPPORTED 0
#ifndef BSP_FEATURE_ADC_B_CLOCK_SOURCE_ADCCLK
#define BSP_FEATURE_ADC_B_CLOCK_SOURCE_ADCCLK 1
#endif
/* RA8P1 manual tables 54.4 and 54.5; shared inputs must not be scanned by both units. */
#define BSP_FEATURE_ADC_B_UNIT_0_CHANNELS 0x00070371007F503FULL
#define BSP_FEATURE_ADC_B_UNIT_1_CHANNELS 0x00700372007FAFC0ULL
#ifndef BSP_FEATURE_WDT_INSTANCE_COUNT
#define BSP_FEATURE_WDT_INSTANCE_COUNT 2
#endif
#ifndef BSP_CFG_CPU_CORE
#define BSP_CFG_CPU_CORE 0
#endif
#if BSP_FEATURE_WDT_INSTANCE_COUNT != 2
#ifndef BSP_CFG_ROM_REG_OFS0
#define BSP_CFG_ROM_REG_OFS0 0xFFFFFFFFU
#endif
#endif
#define FSP_PRIV_CLOCK_PCLKA 0
#define FSP_PRIV_CLOCK_PCLKB 3
typedef int fsp_priv_source_clock_t;
#define FSP_IP_DAC 0
#define FSP_IP_ADC 1
#define FSP_IP_GPT 2
#define FSP_IP_TSN 2
#define BSP_FEATURE_TSN_CALIBRATION32_MASK 0xFFFFU
#define BSP_FEATURE_TSN_SLOPE 4000
#define ELC_EVENT_NONE 0
static inline uint32_t __RBIT(uint32_t x) { uint32_t r = 0; for (unsigned i = 0; i < 32; i++) { r = (r << 1) | (x & 1U); x >>= 1; } return r; }
static inline unsigned __CLZ(uint32_t x) { return x ? (unsigned)__builtin_clz(x) : 32U; }
#define BSP_DELAY_UNITS_MICROSECONDS 0
#define FSP_CONTEXT_SAVE
#define FSP_CONTEXT_RESTORE
#define FSP_ASSERT(x) do { if (!(x)) { return FSP_ERR_ASSERTION; } } while (0)
#define FSP_ERROR_RETURN(x, e) do { if (!(x)) { return (e); } } while (0)
#define FSP_ERROR_LOG(e) ((void)(e))
#define FSP_REGISTER_READ(r) ((void)(r))
#define FSP_HARDWARE_REGISTER_WAIT(r, v) do { while ((r) != (v)) { test_wait(); } } while (0);
#define R_BSP_MODULE_START(ip, ch) test_module((ip), (ch), 1)
#define R_BSP_MODULE_STOP(ip, ch) test_module((ip), (ch), 0)
#define BSP_GRP_IRQ_WDT_ERROR 1
typedef int bsp_grp_irq_t;
extern void * test_irq_context;
extern IRQn_Type test_irq;
void test_wait(void);
void test_module(unsigned ip, unsigned channel, unsigned start);
void R_BSP_SoftwareDelay(uint32_t delay, int units);
uint32_t R_FSP_SystemClockHzGet(int clock);
uint32_t R_BSP_SourceClockHzGet(fsp_priv_source_clock_t clock);
void R_BSP_IrqCfgEnable(IRQn_Type irq, uint8_t priority, void * context);
void R_BSP_IrqDisable(IRQn_Type irq);
void R_BSP_IrqStatusClear(IRQn_Type irq);
void R_BSP_GroupIrqWrite(bsp_grp_irq_t irq, void (* callback)(bsp_grp_irq_t));
static inline IRQn_Type R_FSP_CurrentIrqGet(void) { return test_irq; }
static inline void * R_FSP_IsrContextGet(IRQn_Type irq) { (void)irq; return test_irq_context; }
#endif
