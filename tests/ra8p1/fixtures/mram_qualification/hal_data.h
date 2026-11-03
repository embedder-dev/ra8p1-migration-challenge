/* Host substitutes for core intrinsics only; SoC layouts and flash types are production headers. */
#ifndef QUALIFICATION_HOST_HAL_DATA_H
#define QUALIFICATION_HOST_HAL_DATA_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#define BSP_API_H
#define __IM volatile
#define __OM volatile
#define __IOM volatile
#include "R7KA8P1AD.h"
#include "fsp_common_api.h"
typedef int IRQn_Type;
#define BSP_CFG_CPU_CORE 0
#define BSP_TZ_NONSECURE_BUILD 0
#define BSP_FEATURE_MRAM_VERSION 1
#define BSP_FEATURE_MRAM_CODE_SIZE 0x100000U
#if defined(__APPLE__)
#define BSP_PLACE_IN_SECTION(n) __attribute__((section("__TEXT,__mram_ram,regular,pure_instructions")))
#else
#define BSP_PLACE_IN_SECTION(n) __attribute__((section(n)))
#endif
#define CONTROL_nPRIV_Msk 1U
#define CONTROL_SPSEL_Msk 2U
#define SCB_AIRCR_BFHFNMINS_Msk (1U << 13)
#define SCB_CCR_DC_Msk (1U << 16)
#define SCB_CCR_IC_Msk (1U << 17)
#define DWT_CTRL_NOCYCCNT_Msk (1U << 25)
#define DWT_CTRL_CYCCNTENA_Msk 1U
#define CoreDebug_DEMCR_TRCENA_Msk (1U << 24)
typedef struct { volatile uint32_t CCR, VTOR, AIRCR, CSSELR; } host_scb_t;
typedef struct { volatile uint32_t CTRL, CYCCNT; } host_dwt_t;
typedef struct { volatile uint32_t CTRL; } host_mpu_t;
typedef struct { volatile uint32_t DEMCR; } host_debug_t;
typedef struct { volatile uint32_t ITNS[16]; } host_nvic_t;
extern host_scb_t host_scb;
extern host_dwt_t host_dwt;
extern host_mpu_t host_mpu;
extern host_debug_t host_debug;
extern host_nvic_t host_nvic;
extern R_MRAM_Type host_mram;
extern R_MSTP_Type host_mstp;
extern R_CPU_CTRL_Type host_cpu;
extern R_SYSC_Type host_sys;
extern R_DMA0_Type host_dma;
extern R_DTC0_Type host_dtc;
extern R_DMAC00_Type host_channel[8];
extern uint32_t host_mask, host_ipsr, host_control, host_stack, SystemCoreClock;
extern bool host_timer_stopped, host_cache_stuck;
host_dwt_t * host_timer(void);
void host_nop(void);
void host_cache_disable(uint32_t mask);
void host_cache_enable(uint32_t mask);
#undef R_MRAM
#undef R_MSTP
#undef R_CPU_CTRL
#undef R_SYSTEM
#undef R_DMA0
#undef R_DTC0
#undef R_DMAC00
#undef R_DMAC01
#undef R_DMAC02
#undef R_DMAC03
#undef R_DMAC04
#undef R_DMAC05
#undef R_DMAC06
#undef R_DMAC07
#define R_MRAM (&host_mram)
#define R_MSTP (&host_mstp)
#define R_CPU_CTRL (&host_cpu)
#define R_SYSTEM (&host_sys)
#define R_DMA0 (&host_dma)
#define R_DTC0 (&host_dtc)
#define R_DMAC00 (&host_channel[0])
#define R_DMAC01 (&host_channel[1])
#define R_DMAC02 (&host_channel[2])
#define R_DMAC03 (&host_channel[3])
#define R_DMAC04 (&host_channel[4])
#define R_DMAC05 (&host_channel[5])
#define R_DMAC06 (&host_channel[6])
#define R_DMAC07 (&host_channel[7])
#define SCB (&host_scb)
#define MPU (&host_mpu)
#define NVIC (&host_nvic)
#define DWT host_timer()
#define CoreDebug (&host_debug)
#define __get_PRIMASK() host_mask
#define __set_PRIMASK(n) (host_mask = (n))
#define __disable_irq() (host_mask = 1U)
#define __get_IPSR() host_ipsr
#define __get_CONTROL() host_control
#define __get_MSP() host_stack
#define __NOP() host_nop()
#define __DSB() ((void)0)
#define __DMB() ((void)0)
#define __ISB() ((void)0)
#define SCB_DisableDCache() host_cache_disable(SCB_CCR_DC_Msk)
#define SCB_DisableICache() host_cache_disable(SCB_CCR_IC_Msk)
#define SCB_EnableDCache() host_cache_enable(SCB_CCR_DC_Msk)
#define SCB_EnableICache() host_cache_enable(SCB_CCR_IC_Msk)
typedef enum { BSP_WARM_START_POST_C } bsp_warm_start_event_t;
typedef uint32_t bsp_io_port_pin_t;
typedef struct { uint32_t unused; } ioport_cfg_t;
extern const ioport_cfg_t host_io_cfg;
extern uint32_t host_io_ctrl;
#define IOPORT_CFG_NAME host_io_cfg
#define IOPORT_CFG_CTRL host_io_ctrl
#define BSP_IO_PORT_05_PIN_14 0U
#define BSP_IO_PORT_01_PIN_11 1U
#define BSP_IO_PORT_08_PIN_06 2U
#define BSP_IO_PORT_08_PIN_07 3U
#define IOPORT_CFG_PORT_DIRECTION_OUTPUT 1U
#define IOPORT_CFG_PORT_OUTPUT_LOW 0U
#define BSP_IO_LEVEL_HIGH 1U
#define BSP_IO_LEVEL_LOW 0U
fsp_err_t R_IOPORT_Open(void *, void const *);
fsp_err_t R_IOPORT_PinCfg(void *, uint32_t, uint32_t);
fsp_err_t R_IOPORT_PinWrite(void *, uint32_t, uint32_t);
#endif
