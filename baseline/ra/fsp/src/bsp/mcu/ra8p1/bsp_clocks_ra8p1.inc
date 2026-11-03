/* SPDX-License-Identifier: BSD-3-Clause
 * RA8P1 clock implementation, R01UH1064EJ0130 sections 9, 59 and 60.
 * Included by the shared BSP clock translation unit for this family only.
 */
#include "bsp_api.h"

static uint32_t bsp_ra8p1_divisor(uint32_t code)
{
    if ((code <= 6U) || ((code >= 8U) && (code <= 11U)))
    {
        return (code & 8U) ? (3U << (code & 7U)) : (1U << code);
    }
    return 0U;
}

uint32_t R_BSP_SourceClockHzGet(fsp_priv_source_clock_t clock)
{
    static uint32_t const hoco_hz[] = {16000000U, 18000000U, 20000000U, 0U, 32000000U, 0U, 0U, 48000000U};
    if (clock == FSP_PRIV_CLOCK_HOCO)
    {
        return hoco_hz[R_SYSTEM->HOCOCR2 & 7U];
    }
    if (clock == FSP_PRIV_CLOCK_MOCO) { return 8000000U; }
    if ((clock == FSP_PRIV_CLOCK_LOCO) || (clock == FSP_PRIV_CLOCK_SUBCLOCK)) { return 32768U; }
    if (clock == FSP_PRIV_CLOCK_MAIN_OSC) { return BSP_CFG_XTAL_HZ; }
    if ((clock < FSP_PRIV_CLOCK_PLL) || (clock > FSP_PRIV_CLOCK_PLL2R)) { return 0U; }
    bool pll2 = (clock == FSP_PRIV_CLOCK_PLL2P) || (clock >= FSP_PRIV_CLOCK_PLL2Q);
    uint32_t control = pll2 ? R_SYSTEM->PLL2CCR : R_SYSTEM->PLLCCR;
    uint32_t outputs = pll2 ? R_SYSTEM->PLL2CCR2 : R_SYSTEM->PLLCCR2;
    uint32_t input_hz = (control & 16U) ? hoco_hz[R_SYSTEM->HOCOCR2 & 7U] : BSP_CFG_XTAL_HZ;
    if ((control & 3U) == 3U) { return 0U; } /* Reserved PLL input divider. */
    uint32_t fraction = (control >> 6) & 3U;
    uint32_t multiplier_sixths = (((control >> 8) & 511U) + 1U) * 6U;
    multiplier_sixths += (fraction == 3U) ? 3U : fraction * 2U;
    uint32_t output = ((clock == FSP_PRIV_CLOCK_PLL1Q) || (clock == FSP_PRIV_CLOCK_PLL2Q)) ? 4U :
                      (((clock == FSP_PRIV_CLOCK_PLL1R) || (clock == FSP_PRIV_CLOCK_PLL2R)) ? 8U : 0U);
    uint32_t divider = (outputs >> output) & 15U;
    /* HWM 9.2.7/9.2.10: P permits /2, /3, /4, /6, /8, /16.
     * Q/R also permit /5, /9 and /1.5 (code 9, not code 0). */
    uint32_t allowed = output ? 0x03BEU : 0x80AEU;
    if (!(allowed & (1U << divider))) { return 0U; }
    uint32_t divider_twice = (divider == 9U) ? 3U : (divider + 1U) * 2U;
    uint64_t frequency = ((uint64_t) input_hz * multiplier_sixths * 2U) /
                         (6U * ((control & 3U) + 1U) * divider_twice);
    return (frequency <= UINT32_MAX) ? (uint32_t) frequency : 0U;
}

uint32_t bsp_prv_clock_source_get(void)
{
    return R_SYSTEM->SCKSCR & 7U;
}

uint32_t bsp_ra8p1_clock_hz_get(uint32_t clock)
{
    if ((clock > 48U) || (clock == 40U) || (clock & 3U)) { return 0U; }
    uint32_t code;
    if (clock >= 32U)
    {
        uint32_t shift = (clock == 32U) ? (4U * BSP_CFG_CPU_CORE) : ((clock == 48U) ? 0U : clock - 32U);
        code = (R_SYSTEM->SCKDIVCR2 >> shift) & 15U;
    }
    else
    {
        code = (R_SYSTEM->SCKDIVCR >> clock) & 15U;
    }
    uint32_t div = bsp_ra8p1_divisor(code);
    return div ? R_BSP_SourceClockHzGet((fsp_priv_source_clock_t) bsp_prv_clock_source_get()) / div : 0U;
}

void SystemCoreClockUpdate(void)
{
    SystemCoreClock = bsp_ra8p1_clock_hz_get(32U);
}

void bsp_clock_freq_var_init(void)
{
    SystemCoreClockUpdate();
}

fsp_err_t bsp_ra8p1_clock_validate(uint32_t hz, uint32_t sckdivcr, uint16_t sckdivcr2)
{
    uint32_t div[12];
    bool thirds = false;
    bool binary = false;
    if ((hz == 0U) || (hz > 1000000000U)) { return FSP_ERR_INVALID_RATE; }
    for (uint32_t i = 0; i < 12U; i++)
    {
        uint32_t code = (i < 8U) ? ((sckdivcr >> (4U * i)) & 15U) :
                                  ((sckdivcr2 >> (4U * (i - 8U))) & 15U);
        div[i] = bsp_ra8p1_divisor(code);
        if (!div[i]) { return FSP_ERR_INVALID_ARGUMENT; }
        thirds |= code >= 8U;
        binary |= (code > 0U) && (code <= 6U);
    }
    /* MRICK mirror and synchronous clock ratios, HWM 9.2.2/9.2.3. */
    if ((thirds && binary) || (div[10] != div[11]) ||
        (div[6] % div[8]) || (div[6] % div[9]) || (div[6] % div[11]) ||
        (div[11] % div[8]) || (div[11] % div[9]) || (div[7] % div[6]) ||
        (div[4] % div[6]) || (div[3] % div[6]) || (div[2] % div[3]) ||
        (div[3] % div[0]) ||
        ((div[6] % div[1]) && (div[1] % div[6])) ||
        ((div[6] % div[0]) && (div[0] % div[6])) ||
        ((div[6] % div[5]) && (div[5] % div[6])) ||
        ((uint64_t) hz > (uint64_t) 250000000U * div[9]) ||
        ((uint64_t) hz > (uint64_t) 250000000U * div[11]) ||
        ((uint64_t) hz > (uint64_t) 250000000U * div[6]) ||
        ((uint64_t) hz > (uint64_t) 125000000U * div[7]) ||
        ((uint64_t) hz > (uint64_t) 125000000U * div[4]) ||
        ((uint64_t) hz > (uint64_t) 125000000U * div[3]) ||
        ((uint64_t) hz > (uint64_t) 125000000U * div[1]) ||
        ((uint64_t) hz > (uint64_t) 250000000U * div[0]) ||
        ((uint64_t) hz > (uint64_t) 250000000U * div[5]) ||
        ((uint64_t) hz > (uint64_t) 62500000U * div[2]) ||
        (div[7] > 5U * div[11]))
    {
        return FSP_ERR_INVALID_RATE;
    }
    return FSP_SUCCESS;
}

#if BSP_CFG_CPU_CORE == 0
/* All allowed transitions use the reset voltage, VSCR_2 (CPU0 <= 600 MHz,
 * CPU1/ICLK/MRI <= 150 MHz). Do not perform DVFS here: HWM 11.7 forbids
 * CM85 cache/TCM access during that transition, including a TCM stack. */
static bool bsp_ra8p1_clock_exclusive(void)
{
    return (R_SYSTEM->VSCR == 2U) && (R_SYSTEM->OPCCR == 0U) &&
           !(R_CPU_CTRL->CPU1ACTCSR & 0x81U) && !R_CPU_CTRL->CPU1WAITCR &&
           ((R_MSTP->MSTPCRA & 0x00C1000FU) == 0x00010000U) &&
           ((R_MSTP->MSTPCRB & 0xFFCF1B90U) == 0xFFCF1B90U) &&
           ((R_MSTP->MSTPCRC & 0xDD037DD3U) == 0xDD037DD3U) &&
           ((R_MSTP->MSTPCRD & 0x1E787870U) == 0x1E787870U) &&
           ((R_MSTP->MSTPCRE & 0xF83C0300U) == 0xF83C0300U) &&
           !(R_MRAM->MRCPS & 0xC0U) && !((R_MRAM->MRCPC0 | R_MRAM->MRCPC1) & 1U) &&
           !R_MRAM->MENTRYR && !(R_MRAM->MCTRSTATR & 1U);
}

static bool bsp_ra8p1_clock_profile(uint32_t source, uint32_t hz, uint32_t d1, uint16_t d2)
{
    if ((source == BSP_CLOCKS_SOURCE_CLOCK_MOCO) || (source == BSP_CLOCKS_SOURCE_CLOCK_MAIN_OSC) ||
        (source == BSP_CLOCKS_SOURCE_CLOCK_HOCO))
    {
        return (hz > 0U) && (hz <= 24000000U) && !d1 && !d2;
    }
    return (source == BSP_CLOCKS_SOURCE_CLOCK_PLL) && (d2 == BSP_RA8P1_PLL_SCKDIVCR2) &&
           (((hz == BSP_RA8P1_PLL240_HZ) && (d1 == BSP_RA8P1_PLL240_SCKDIVCR)) ||
            ((hz == BSP_RA8P1_PLL600_HZ) && (d1 == BSP_RA8P1_PLL600_SCKDIVCR)));
}

static fsp_err_t bsp_ra8p1_clock_ready(uint32_t source)
{
    switch (source)
    {
        case BSP_CLOCKS_SOURCE_CLOCK_MOCO:
            return R_SYSTEM->MOCOCR ? FSP_ERR_CLOCK_INACTIVE : FSP_SUCCESS;
        case BSP_CLOCKS_SOURCE_CLOCK_HOCO:
            if (R_SYSTEM->HOCOCR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 1U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        case BSP_CLOCKS_SOURCE_CLOCK_MAIN_OSC:
            if (R_SYSTEM->MOSCCR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 8U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        case BSP_CLOCKS_SOURCE_CLOCK_PLL:
            if (R_SYSTEM->PLLCR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 32U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        default: return FSP_ERR_UNSUPPORTED;
    }
}

static fsp_err_t bsp_ra8p1_wait8(volatile const uint8_t * reg, uint8_t mask, uint8_t expected)
{
    uint32_t count = 1000000U;
    while ((*reg & mask) != expected)
    {
        if (--count == 0U) { return FSP_ERR_TIMEOUT; }
    }
    return FSP_SUCCESS;
}

static void bsp_ra8p1_pll_settle(void)
{
    /* HWM Figs. 9.14-16: no interrupt may shorten the CPU0 NOP interval.
     * 18000 NOPs alone take >= 30 us at the supported 600 MHz ceiling;
     * loop overhead only increases the interval. Also safe in external VDD mode. */
    for (uint32_t i = 0U; i < 18000U; ++i) { __NOP(); }
}

static void bsp_ra8p1_source_write(uint32_t source)
{
    R_SYSTEM->SCKSCR = (uint8_t) source;
    (void) R_SYSTEM->SCKSCR;
    __DSB();
    __ISB();
}

static void bsp_ra8p1_dividers_write(uint32_t d1, uint16_t d2)
{
    /* Via the /4 bridge, every individual register write satisfies 9.2.2/3.
     * In particular, changing MRP directly to /8 while MRI is /1 is illegal. */
    R_SYSTEM->SCKDIVCR = BSP_RA8P1_PLL240_SCKDIVCR;
    (void) R_SYSTEM->SCKDIVCR;
    R_SYSTEM->SCKDIVCR2 = d2;
    (void) R_SYSTEM->SCKDIVCR2;
    R_SYSTEM->SCKDIVCR = d1;
    (void) R_SYSTEM->SCKDIVCR;
    __DSB();
}

static fsp_err_t bsp_ra8p1_mram_timing(uint32_t mri, uint32_t mrp)
{
    /* HWM 60.4.3: verify acceptance; MRAM can defer frequency writes while busy.
     * Update extra first when raising, code first when lowering to preserve
     * MRCMHZ / MREMHZ <= 5 even between writes. Keys read back as zero. */
    if (mri >= (R_MRAM->MRCFREQ & 1023U))
    {
        R_MRAM->MREFREQ = 0xE1000000U | mrp;
        R_MRAM->MRCFREQ = 0x1E000000U | mri;
    }
    else
    {
        R_MRAM->MRCFREQ = 0x1E000000U | mri;
        R_MRAM->MREFREQ = 0xE1000000U | mrp;
    }
    uint32_t count = 1000000U;
    while (((R_MRAM->MRCFREQ & 1023U) != mri) || ((R_MRAM->MREFREQ & 255U) != mrp))
    {
        if (--count == 0U) { return FSP_ERR_TIMEOUT; }
    }
    return FSP_SUCCESS;
}

static fsp_err_t bsp_ra8p1_sram_wait(void)
{
    /* A single wait is valid for every supported profile. Retain it on slowing
     * down; never reduce a user's existing memory margin. HWM 59.2.4/6, 59.3.7. */
    uint16_t protect = R_SRAM->SRAMPRCR_S;
    R_SRAM->SRAMPRCR_S = 0xA501U;
    R_SRAM->SRAMWTSC = 1U;
    fsp_err_t err = (R_SRAM->SRAMWTSC == 1U) ? FSP_SUCCESS : FSP_ERR_TIMEOUT;
    R_SRAM->SRAMPRCR_S = (uint16_t) (0xA500U | (protect & 1U));
    __DSB();
    return err;
}

static fsp_err_t bsp_ra8p1_main_start(void)
{
    if (BSP_CFG_XTAL_HZ != 24000000U) { return FSP_ERR_UNSUPPORTED; }
    if (R_SYSTEM->MOSCCR)
    {
        R_SYSTEM->MOMCR = (uint8_t) ((R_SYSTEM->MOMCR &
                                    ~(R_SYSTEM_MOMCR_MODRV0_Msk | R_SYSTEM_MOMCR_MOSEL_Msk)) |
                                   (3U << R_SYSTEM_MOMCR_MODRV0_Pos));
        R_SYSTEM->MOSCWTCR = 8U;
        R_SYSTEM->MOSCCR = 0U;
    }
    return bsp_ra8p1_wait8(&R_SYSTEM->OSCSF, 8U, 8U);
}
#endif

fsp_err_t bsp_ra8p1_clock_start(uint32_t source, uint32_t pll_hz)
{
#if BSP_CFG_CPU_CORE == 1
    (void) source; (void) pll_hz;
    return FSP_ERR_UNSUPPORTED;
#else
    if ((source != BSP_CLOCKS_SOURCE_CLOCK_MAIN_OSC) && (source != BSP_CLOCKS_SOURCE_CLOCK_PLL))
    { return FSP_ERR_UNSUPPORTED; }
    if ((source == BSP_CLOCKS_SOURCE_CLOCK_PLL) &&
        (pll_hz != BSP_RA8P1_PLL240_HZ) && (pll_hz != BSP_RA8P1_PLL600_HZ))
    { return FSP_ERR_INVALID_RATE; }
    uint32_t irq = __get_PRIMASK();
    __disable_irq();
    fsp_err_t err = FSP_ERR_UNSUPPORTED;
    if (bsp_ra8p1_clock_exclusive())
    {
        uint16_t protect = R_SYSTEM->PRCR;
        R_SYSTEM->PRCR = (uint16_t) (0xA500U | (protect & 0x3FU) | 1U);
        err = bsp_ra8p1_main_start();
        if ((err == FSP_SUCCESS) && (source == BSP_CLOCKS_SOURCE_CLOCK_PLL))
        {
            uint32_t control = ((pll_hz == BSP_RA8P1_PLL600_HZ) ? 49U : 39U) <<
                               R_SYSTEM_PLLCCR_PLLMUL_Pos;
            uint16_t output = (pll_hz == BSP_RA8P1_PLL600_HZ) ? 0x0331U : 0x0333U;
            if (!R_SYSTEM->PLLCR)
            {
                /* Never retune a running PLL, even if it is not the system source. */
                err = ((R_SYSTEM->PLLCCR == control) && (R_SYSTEM->PLLCCR2 == output)) ?
                      bsp_ra8p1_clock_ready(source) : FSP_ERR_IN_USE;
            }
            else
            {
                /* OSCSF must acknowledge stop before PLL register writes. */
                err = bsp_ra8p1_wait8(&R_SYSTEM->OSCSF, 32U, 0U);
                if (err == FSP_SUCCESS)
                {
                    R_SYSTEM->PLLCCR = control;
                    R_SYSTEM->PLLCCR2 = output;
                    R_SYSTEM->PLLCR = 0U;
                    err = bsp_ra8p1_wait8(&R_SYSTEM->OSCSF, 32U, 32U);
                }
            }
        }
        R_SYSTEM->PRCR = (uint16_t) (0xA500U | (protect & 0x3FU));
    }
    __set_PRIMASK(irq);
    return err;
#endif
}

fsp_err_t bsp_ra8p1_clock_stop(uint32_t source)
{
#if BSP_CFG_CPU_CORE == 1
    (void) source;
    return FSP_ERR_UNSUPPORTED;
#else
    if (source != BSP_CLOCKS_SOURCE_CLOCK_PLL) { return FSP_ERR_UNSUPPORTED; }
    uint32_t irq = __get_PRIMASK();
    __disable_irq();
    fsp_err_t err = FSP_ERR_UNSUPPORTED;
    if (bsp_ra8p1_clock_exclusive())
    {
        err = FSP_ERR_IN_USE;
        if (bsp_prv_clock_source_get() != source)
        {
            uint16_t protect = R_SYSTEM->PRCR;
            R_SYSTEM->PRCR = (uint16_t) (0xA500U | (protect & 0x3FU) | 1U);
            R_SYSTEM->PLLCR = 1U;
            err = bsp_ra8p1_wait8(&R_SYSTEM->OSCSF, 32U, 0U);
            R_SYSTEM->PRCR = (uint16_t) (0xA500U | (protect & 0x3FU));
        }
    }
    __set_PRIMASK(irq);
    return err;
#endif
}

fsp_err_t bsp_ra8p1_clock_set(uint32_t source, uint32_t sckdivcr, uint16_t sckdivcr2)
{
#if BSP_CFG_CPU_CORE == 1
    (void) source; (void) sckdivcr; (void) sckdivcr2;
    return FSP_ERR_UNSUPPORTED;
#else
    if (source > BSP_CLOCKS_SOURCE_CLOCK_PLL) { return FSP_ERR_INVALID_ARGUMENT; }
    uint32_t hz = R_BSP_SourceClockHzGet((fsp_priv_source_clock_t) source);
    fsp_err_t err = bsp_ra8p1_clock_validate(hz, sckdivcr, sckdivcr2);
    if (err != FSP_SUCCESS) { return err; }
    if (!bsp_ra8p1_clock_profile(source, hz, sckdivcr, sckdivcr2)) { return FSP_ERR_UNSUPPORTED; }
    uint32_t irq = __get_PRIMASK();
    __disable_irq();
    uint32_t current = bsp_prv_clock_source_get();
    uint32_t old_hz = R_BSP_SourceClockHzGet((fsp_priv_source_clock_t) current);
    if (!bsp_ra8p1_clock_exclusive() ||
        !bsp_ra8p1_clock_profile(current, old_hz, R_SYSTEM->SCKDIVCR, R_SYSTEM->SCKDIVCR2))
    { err = FSP_ERR_UNSUPPORTED; }
    else { err = bsp_ra8p1_clock_ready(source); }
    /* MOCO remains running as the ratio-preserving transition bridge. */
    if ((err == FSP_SUCCESS) && R_SYSTEM->MOCOCR) { err = FSP_ERR_CLOCK_INACTIVE; }
    if (err != FSP_SUCCESS) { __set_PRIMASK(irq); return err; }
    if ((current == source) && (sckdivcr == R_SYSTEM->SCKDIVCR) &&
        (sckdivcr2 == R_SYSTEM->SCKDIVCR2)) { __set_PRIMASK(irq); return FSP_SUCCESS; }

    uint16_t protect = R_SYSTEM->PRCR;
    R_SYSTEM->PRCR = (uint16_t) (0xA500U | (protect & 0x3FU) | 1U);
    /* HWM Fig. 9.16, Table 9.9: wait, then increase the division ratios
     * before leaving PLL. Both allowed PLL profiles have binary dividers;
     * increasing each encoding by one doubles its divisor without overflow.
     * Slowing ICLK/MRP first preserves the CPU/MRI relationships. */
    if (current == BSP_CLOCKS_SOURCE_CLOCK_PLL)
    {
        bsp_ra8p1_pll_settle();
        R_SYSTEM->SCKDIVCR += 0x11111111U;
        (void) R_SYSTEM->SCKDIVCR;
        R_SYSTEM->SCKDIVCR2 = (uint16_t) (R_SYSTEM->SCKDIVCR2 + 0x1111U);
        (void) R_SYSTEM->SCKDIVCR2;
        __DSB();
    }
    if (current != BSP_CLOCKS_SOURCE_CLOCK_MOCO) { bsp_ra8p1_source_write(BSP_CLOCKS_SOURCE_CLOCK_MOCO); }
    if (current == BSP_CLOCKS_SOURCE_CLOCK_PLL)
    {
        /* At MOCO, restore CPU/MRI before ICLK so that MRI >= ICLK. */
        R_SYSTEM->SCKDIVCR2 = BSP_RA8P1_PLL_SCKDIVCR2;
        (void) R_SYSTEM->SCKDIVCR2;
    }
    if (R_SYSTEM->SCKDIVCR || R_SYSTEM->SCKDIVCR2) { bsp_ra8p1_dividers_write(0U, 0U); }
    SystemCoreClockUpdate();

    uint32_t mri = (hz / bsp_ra8p1_divisor((sckdivcr2 >> 12) & 15U) + 999999U) / 1000000U;
    uint32_t mrp = (hz / bsp_ra8p1_divisor((sckdivcr >> 28) & 15U) + 999999U) / 1000000U;
    if (mri < 8U) { mri = 8U; }
    if (mrp < 8U) { mrp = 8U; }
    err = bsp_ra8p1_sram_wait();
    if (err == FSP_SUCCESS) { err = bsp_ra8p1_mram_timing(mri, mrp); }
    if (err == FSP_SUCCESS)
    {
        if (sckdivcr || sckdivcr2) { bsp_ra8p1_dividers_write(sckdivcr, sckdivcr2); }
        if (source != BSP_CLOCKS_SOURCE_CLOCK_MOCO) { bsp_ra8p1_source_write(source); }
        /* HWM Fig. 9.15: wait after selecting PLL before resuming normal work. */
        if (source == BSP_CLOCKS_SOURCE_CLOCK_PLL) { bsp_ra8p1_pll_settle(); }
    }
    /* On a timing failure remain on MOCO, with conservative SRAM waits. */
    SystemCoreClockUpdate();
    R_SYSTEM->PRCR = (uint16_t) (0xA500U | (protect & 0x3FU));
    __set_PRIMASK(irq);
    return err;
#endif
}

void bsp_clock_init(void)
{
    /* No writable clock-tree access from CPU1, including after a local reset. */
#if BSP_CFG_CPU_CORE == 0
    uint32_t d1 = (BSP_CFG_PCLKD_DIV << 0) | (BSP_CFG_PCLKC_DIV << 4) |
                  (BSP_CFG_PCLKB_DIV << 8) | (BSP_CFG_PCLKA_DIV << 12) |
                  (BSP_CFG_BCLK_DIV << 16) | (BSP_CFG_PCLKE_DIV << 20) |
                  (BSP_CFG_ICLK_DIV << 24) | (BSP_CFG_MRPCLK_DIV << 28);
    uint16_t d2 = (uint16_t) ((BSP_CFG_CPUCLK_DIV << 0) | (BSP_CFG_CPU1CLK_DIV << 4) |
                             (BSP_CFG_MRICLK_DIV << 8) | (BSP_CFG_MRICLK_DIV << 12));
    fsp_err_t err = FSP_SUCCESS;
    if ((BSP_CFG_RA8P1_STARTUP_PROFILE == 1) || (BSP_CFG_RA8P1_STARTUP_PROFILE == 2))
    {
        err = bsp_ra8p1_clock_start(BSP_CLOCKS_SOURCE_CLOCK_PLL,
                                  (BSP_CFG_RA8P1_STARTUP_PROFILE == 2) ? BSP_RA8P1_PLL600_HZ : BSP_RA8P1_PLL240_HZ);
    }
    else if (BSP_CFG_CLOCK_SOURCE == BSP_CLOCKS_SOURCE_CLOCK_MAIN_OSC)
    {
        err = bsp_ra8p1_clock_start(BSP_CLOCKS_SOURCE_CLOCK_MAIN_OSC, 0U);
    }
    if (err == FSP_SUCCESS) { err = bsp_ra8p1_clock_set(BSP_CFG_CLOCK_SOURCE, d1, d2); }
    if (err != FSP_SUCCESS) { BSP_CFG_HANDLE_UNRECOVERABLE_ERROR(0); }
#endif
    SystemCoreClockUpdate();
}
