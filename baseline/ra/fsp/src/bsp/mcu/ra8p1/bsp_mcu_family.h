/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef BSP_MCU_FAMILY_RA8P1_H
#define BSP_MCU_FAMILY_RA8P1_H

#include "bsp_mcu_device_pn_cfg.h"
#include "bsp_mcu_device_cfg.h"
#include "bsp_clock_cfg.h"
#include "bsp_override.h"
#include "bsp_mcu_info.h"

#define BSP_MCU_GROUP_RA8P1 (1)
#ifndef BSP_CFG_CPU_CORE
 #error "RA8P1 requires the core selected by the project configuration"
#endif
#if BSP_CFG_CPU_CORE == 0
 #define RENESAS_CORTEX_M85
#elif BSP_CFG_CPU_CORE == 1
 #if defined(__ARM_FEATURE_MVE) || defined(__ARM_FEATURE_PAUTH) || defined(__ARM_FEATURE_BTI)
  #error "RA8P1 CPU1 requires a Cortex-M33 compiler target, not Cortex-M85 extensions"
 #endif
 #define RENESAS_CORTEX_M33
#else
 #error "RA8P1 supports CPU0 (Cortex-M85) and CPU1 (Cortex-M33) only"
#endif
#if defined(_RA_TZ_NONSECURE) || defined(_RA_TZ_SECURE)
 #error "RA8P1 TrustZone partition generation is not implemented"
#endif
#define BSP_TZ_SECURE_BUILD (0)
#define BSP_TZ_NONSECURE_BUILD (0)
#define BSP_TZ_CFG_SKIP_INIT (0)
#define BSP_TZ_CFG_INIT_SECURE_ONLY (0)
#define BSP_TZ_CFG_EXCEPTION_RESPONSE (0)
#define BSP_LOCO_HZ (32768U)
#define BSP_MOCO_HZ (8000000U)
#define BSP_SUB_CLOCK_HZ (32768U)
#define BSP_HOCO_HZ (16000000U)
#define BSP_CORTEX_VECTOR_TABLE_ENTRIES (16U)
#define BSP_VECTOR_TABLE_MAX_ENTRIES (112U)
#define BSP_CFG_INLINE_IRQ_FUNCTIONS (1)
#define BSP_CFG_CLOCK_SETTLING_DELAY_ENABLE (1)
#define BSP_CFG_SLEEP_MODE_DELAY_ENABLE (1)
#define BSP_CFG_MSTP_CHANGE_DELAY_ENABLE (1)
#define BSP_CFG_CLOCK_SETTLING_DELAY_US (150U)
#define BSP_CFG_RTOS_IDLE_SLEEP (0)
#define BSP_CFG_FLL_ENABLE (0)
#define BSP_CFG_SDRAM_ENABLED (0)
#define BSP_CFG_DCACHE_ENABLED (0)
#define BSP_PRV_IELS_ENUM(vector) (ELC_ ## vector)

#endif
