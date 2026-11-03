/* SPDX-License-Identifier: BSD-3-Clause
 * RA8P1 fixed-profile CGC. Clock ownership and transition sequencing reside
 * in the BSP; the public 5.9 API only accepts the supported PLL configurations.
 */
#define CGC_RA8P1_OPEN (0x00434743U)

static fsp_err_t cgc_ra8p1_open_check(cgc_ctrl_t * ctrl)
{
    if (!ctrl) { return FSP_ERR_ASSERTION; }
    return (((cgc_instance_ctrl_t *) ctrl)->open == CGC_RA8P1_OPEN) ? FSP_SUCCESS : FSP_ERR_NOT_OPEN;
}

fsp_err_t R_CGC_Open(cgc_ctrl_t * const ctrl, cgc_cfg_t const * const cfg)
{
    if (!ctrl || !cfg) { return FSP_ERR_ASSERTION; }
    cgc_instance_ctrl_t * instance = (cgc_instance_ctrl_t *) ctrl;
    if (instance->open == CGC_RA8P1_OPEN) { return FSP_ERR_ALREADY_OPEN; }
    instance->p_callback = cfg->p_callback;
    instance->p_context = cfg->p_context;
    instance->p_callback_memory = NULL;
    instance->open = CGC_RA8P1_OPEN;
    return FSP_SUCCESS;
}

fsp_err_t R_CGC_Close(cgc_ctrl_t * const ctrl)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    if (err == FSP_SUCCESS) { ((cgc_instance_ctrl_t *) ctrl)->open = 0U; }
    return err;
}

fsp_err_t R_CGC_SystemClockSet(cgc_ctrl_t * const ctrl, cgc_clock_t source,
                              cgc_divider_cfg_t const * const dividers)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    if (err != FSP_SUCCESS) { return err; }
    if (!dividers) { return FSP_ERR_ASSERTION; }
    return bsp_ra8p1_clock_set((uint32_t) source, dividers->sckdivcr_w, dividers->sckdivcr2);
}

fsp_err_t R_CGC_SystemClockGet(cgc_ctrl_t * const ctrl, cgc_clock_t * const source,
                              cgc_divider_cfg_t * const dividers)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    if (err != FSP_SUCCESS) { return err; }
    if (!source || !dividers) { return FSP_ERR_ASSERTION; }
    *source = (cgc_clock_t) bsp_prv_clock_source_get();
    dividers->sckdivcr_w = R_SYSTEM->SCKDIVCR;
    dividers->sckdivcr2 = R_SYSTEM->SCKDIVCR2;
    return FSP_SUCCESS;
}

fsp_err_t R_CGC_ClockCheck(cgc_ctrl_t * const ctrl, cgc_clock_t source)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    if (err != FSP_SUCCESS) { return err; }
    switch (source)
    {
        case CGC_CLOCK_HOCO:
            if (R_SYSTEM->HOCOCR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 1U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        case CGC_CLOCK_MOCO: return R_SYSTEM->MOCOCR ? FSP_ERR_CLOCK_INACTIVE : FSP_SUCCESS;
        case CGC_CLOCK_LOCO: return R_SYSTEM->LOCOCR ? FSP_ERR_CLOCK_INACTIVE : FSP_SUCCESS;
        case CGC_CLOCK_MAIN_OSC:
            if (R_SYSTEM->MOSCCR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 8U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        case CGC_CLOCK_PLL:
            if (R_SYSTEM->PLLCR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 32U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        case CGC_CLOCK_PLL2:
            if (R_SYSTEM->PLL2CR) { return FSP_ERR_CLOCK_INACTIVE; }
            return (R_SYSTEM->OSCSF & 64U) ? FSP_SUCCESS : FSP_ERR_NOT_STABILIZED;
        case CGC_CLOCK_SUBCLOCK: return FSP_ERR_UNSUPPORTED;
        default: return FSP_ERR_INVALID_ARGUMENT;
    }
}

fsp_err_t R_CGC_ClockStart(cgc_ctrl_t * const ctrl, cgc_clock_t source,
                          cgc_pll_cfg_t const * const pll)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    if (err != FSP_SUCCESS) { return err; }
    if (source == CGC_CLOCK_MAIN_OSC) { return bsp_ra8p1_clock_start((uint32_t) source, 0U); }
    if (source != CGC_CLOCK_PLL) { return FSP_ERR_UNSUPPORTED; }
    if (!pll) { return FSP_ERR_ASSERTION; }
    if ((pll->source_clock != CGC_CLOCK_MAIN_OSC) || (pll->divider != CGC_PLL_DIV_1) ||
        (pll->out_div_q != CGC_PLL_OUT_DIV_4) || (pll->out_div_r != CGC_PLL_OUT_DIV_4))
    { return FSP_ERR_UNSUPPORTED; }
    uint32_t hz;
    if ((pll->multiplier == BSP_CLOCKS_PLL_MUL(40U, 0U)) && (pll->out_div_p == CGC_PLL_OUT_DIV_4))
    { hz = BSP_RA8P1_PLL240_HZ; }
    else if ((pll->multiplier == BSP_CLOCKS_PLL_MUL(50U, 0U)) && (pll->out_div_p == CGC_PLL_OUT_DIV_2))
    { hz = BSP_RA8P1_PLL600_HZ; }
    else { return FSP_ERR_UNSUPPORTED; }
    return bsp_ra8p1_clock_start((uint32_t) source, hz);
}

fsp_err_t R_CGC_ClockStop(cgc_ctrl_t * const ctrl, cgc_clock_t source)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    return (err == FSP_SUCCESS) ? bsp_ra8p1_clock_stop((uint32_t) source) : err;
}

fsp_err_t R_CGC_ClocksCfg(cgc_ctrl_t * const ctrl, cgc_clocks_cfg_t const * const cfg)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    if (err != FSP_SUCCESS) { return err; }
    if (!cfg) { return FSP_ERR_ASSERTION; }
    if ((cfg->loco_state != CGC_CLOCK_CHANGE_NONE) || (cfg->moco_state != CGC_CLOCK_CHANGE_NONE) ||
        (cfg->hoco_state != CGC_CLOCK_CHANGE_NONE) || (cfg->mainosc_state != CGC_CLOCK_CHANGE_NONE) ||
        (cfg->pll_state != CGC_CLOCK_CHANGE_NONE) || (cfg->pll2_state != CGC_CLOCK_CHANGE_NONE) ||
        (cfg->subosc_state != CGC_CLOCK_CHANGE_NONE)) { return FSP_ERR_UNSUPPORTED; }
    return R_CGC_SystemClockSet(ctrl, cfg->system_clock, &cfg->divider_cfg);
}

fsp_err_t R_CGC_OscStopDetectEnable(cgc_ctrl_t * const ctrl)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    return (err == FSP_SUCCESS) ? FSP_ERR_UNSUPPORTED : err;
}
fsp_err_t R_CGC_OscStopDetectDisable(cgc_ctrl_t * const ctrl)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    return (err == FSP_SUCCESS) ? FSP_ERR_UNSUPPORTED : err;
}
fsp_err_t R_CGC_OscStopStatusClear(cgc_ctrl_t * const ctrl)
{
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    return (err == FSP_SUCCESS) ? FSP_ERR_UNSUPPORTED : err;
}

fsp_err_t R_CGC_CallbackSet(cgc_ctrl_t * const ctrl, void (*callback)(cgc_callback_args_t *),
                           void const * const context, cgc_callback_args_t * const memory)
{
    (void) callback; (void) context; (void) memory;
    fsp_err_t err = cgc_ra8p1_open_check(ctrl);
    return (err == FSP_SUCCESS) ? FSP_ERR_UNSUPPORTED : err;
}

const cgc_api_t g_cgc_on_cgc = {
    .open = R_CGC_Open, .close = R_CGC_Close, .clocksCfg = R_CGC_ClocksCfg,
    .clockStart = R_CGC_ClockStart, .clockStop = R_CGC_ClockStop, .clockCheck = R_CGC_ClockCheck,
    .systemClockSet = R_CGC_SystemClockSet, .systemClockGet = R_CGC_SystemClockGet,
    .oscStopDetectEnable = R_CGC_OscStopDetectEnable, .oscStopDetectDisable = R_CGC_OscStopDetectDisable,
    .oscStopStatusClear = R_CGC_OscStopStatusClear, .callbackSet = R_CGC_CallbackSet
};
