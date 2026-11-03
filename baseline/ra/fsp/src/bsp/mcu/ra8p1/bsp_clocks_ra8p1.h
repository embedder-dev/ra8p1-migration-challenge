/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef BSP_CLOCKS_RA8P1_H
#define BSP_CLOCKS_RA8P1_H

#include "bsp_clock_cfg.h"

#define BSP_CLOCKS_SOURCE_CLOCK_HOCO       (0U)
#define BSP_CLOCKS_SOURCE_CLOCK_MOCO       (1U)
#define BSP_CLOCKS_SOURCE_CLOCK_LOCO       (2U)
#define BSP_CLOCKS_SOURCE_CLOCK_MAIN_OSC   (3U)
#define BSP_CLOCKS_SOURCE_CLOCK_SUBCLOCK   (4U)
#define BSP_CLOCKS_SOURCE_CLOCK_PLL        (5U)
#define BSP_CLOCKS_SOURCE_CLOCK_PLL2       (6U)
#define BSP_CLOCKS_CLOCK_DISABLED          (0xFFU)
#define BSP_PRV_SCKDIVCR_DIV_VALUE(div)    (((div) & 8U) ? (3U << ((div) & 7U)) : (1U << (div)))
#define BSP_PRV_SCKDIVCR_DIV_MASK          (15U)
#define BSP_STARTUP_SOURCE_CLOCK_HZ        (8000000U)
#define BSP_STARTUP_ICLK_HZ                (8000000U)
#define BSP_STARTUP_PCLKB_HZ               (8000000U)

/* PLLCCR.PLLMUL[16:8] and PLLMULNF[7:6]. The shared FSP enum
 * stores the combined fields before their six-bit register shift. */
#define BSP_CLOCKS_PLL_MUL(X, Y)    ((((X) - 1U) << 2U) | ((Y) == 50U ? 3U : ((Y) / 33U)))

/* Fixed 24 MHz crystal profiles. These stay in high-speed mode at VSCR_2.
 * CPU1 must be inactive and peripheral/DMA clocks stopped during changes.
 * Voltage scaling, arbitrary divider paths and live PLL retuning are not supported. */
#define BSP_RA8P1_PLL240_SCKDIVCR     (0x22222322UL)
#define BSP_RA8P1_PLL600_SCKDIVCR     (0x32233432UL)
#define BSP_RA8P1_PLL_SCKDIVCR2       (0x2220U)
#define BSP_RA8P1_PLL240_HZ          (240000000UL)
#define BSP_RA8P1_PLL600_HZ          (600000000UL)

fsp_err_t bsp_ra8p1_clock_start(uint32_t source, uint32_t pll_hz);
fsp_err_t bsp_ra8p1_clock_stop(uint32_t source);
uint32_t bsp_prv_clock_source_get(void);
uint32_t bsp_ra8p1_clock_hz_get(uint32_t clock);
fsp_err_t bsp_ra8p1_clock_validate(uint32_t source_hz, uint32_t sckdivcr, uint16_t sckdivcr2);
fsp_err_t bsp_ra8p1_clock_set(uint32_t source, uint32_t sckdivcr, uint16_t sckdivcr2);
void bsp_clock_freq_var_init(void);
void bsp_clock_init(void);

#endif
