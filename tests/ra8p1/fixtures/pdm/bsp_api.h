#ifndef PDM_TEST_BSP_API_H
#define PDM_TEST_BSP_API_H
#define BSP_API_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "fsp_common_api.h"

#include "bsp_exceptions.h"
#define BSP_FEATURE_PDM_CHANNELS 3
#define BSP_ICU_VECTOR_MAX_ENTRIES (128U)
#define __NVIC_PRIO_BITS 4
#define FSP_IP_PDM 250
#define BSP_DELAY_UNITS_MICROSECONDS 1
#define FSP_PRIV_CLOCK_PCLKB 0
uint32_t R_FSP_SystemClockHzGet(int clock);
#define FSP_ASSERT(x) do { if (!(x)) { return FSP_ERR_ASSERTION; } } while (0)
#define FSP_ERROR_RETURN(x, e) do { if (!(x)) { return (e); } } while (0)
#define FSP_CONTEXT_SAVE
#define FSP_CONTEXT_RESTORE
#define FSP_CRITICAL_SECTION_DEFINE unsigned int pdm_saved = 0
#define FSP_CRITICAL_SECTION_ENTER do { pdm_saved = pdm_test_mask; pdm_test_mask = 1; } while (0)
#define FSP_CRITICAL_SECTION_EXIT do { pdm_test_mask = pdm_saved; } while (0)

typedef struct st_mock_pdm
{
    volatile uint32_t PDCSTRTR;
    volatile uint32_t PDCSTPTR;
    volatile uint32_t reserved0[7];
    volatile uint32_t PDCDRCR;
    volatile uint32_t reserved1[54];
    volatile uint32_t channel[3][64];
} mock_pdm_t;
extern mock_pdm_t pdm_test_regs;
extern unsigned int pdm_test_mask;
#define R_PDMIF (&pdm_test_regs)
void pdm_test_module_start(int ip, int ch);
void pdm_test_module_stop(int ip, int ch);
#define R_BSP_MODULE_START(ip, ch) do { FSP_CRITICAL_SECTION_DEFINE; FSP_CRITICAL_SECTION_ENTER; \
    pdm_test_module_start(ip, ch); FSP_CRITICAL_SECTION_EXIT; } while (0)
#define R_BSP_MODULE_STOP(ip, ch) do { FSP_CRITICAL_SECTION_DEFINE; FSP_CRITICAL_SECTION_ENTER; \
    pdm_test_module_stop(ip, ch); FSP_CRITICAL_SECTION_EXIT; } while (0)
void R_BSP_IrqCfgEnable(IRQn_Type irq, uint8_t ipl, void * p_context);
void R_BSP_IrqDisable(IRQn_Type irq);
void R_BSP_IrqStatusClear(IRQn_Type irq);
void R_BSP_IrqClearPending(IRQn_Type irq);
void R_BSP_SoftwareDelay(uint32_t count, int units);
void NVIC_SetPendingIRQ(IRQn_Type irq);
IRQn_Type R_FSP_CurrentIrqGet(void);
void * R_FSP_IsrContextGet(IRQn_Type irq);
#if BSP_TZ_SECURE_BUILD
#define CMSE_AU_NONSECURE 1
extern bool pdm_test_nonsecure_callback;
void * cmse_check_address_range(void * p, size_t size, int flags);
#endif
#endif
