/*
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "r_pdm.h"
#include "r_pdm_cfg.h"

/***********************************************************************************************************************
 * R7KA8P1AD PDMIF channel register offsets / sizeof(uint32_t).
 * R01UH1064EJ0120, section 50.2. Channel n begins at 0x100 + 0x100*n.
 * Offsets are local because the SVD uses ranged names (CH0-2) rather than C identifiers.
 **********************************************************************************************************************/
#define PDM_PRV_OPEN          (0x50444DUL)
#define PDM_PRV_ICR           (0x0CU / 4U)
#define PDM_PRV_SDCR          (0x10U / 4U)
#define PDM_PRV_SR            (0x14U / 4U)
#define PDM_PRV_SCR           (0x18U / 4U)
#define PDM_PRV_MDSR          (0x20U / 4U)
#define PDM_PRV_SFCR          (0x24U / 4U)
#define PDM_PRV_COEFFICIENT    (0x28U / 4U)
#define PDM_PRV_DBCR          (0xC0U / 4U)
#define PDM_PRV_DRCR          (0xE0U / 4U)
#define PDM_PRV_DRR           (0xE8U / 4U)
#define PDM_PRV_DSR           (0xECU / 4U)
#define PDM_PRV_IDRE          (1UL << 2)
#define PDM_PRV_IEDE          (1UL << 16)
#define PDM_PRV_ERRORS        (PDM_ERROR_SHORT_CIRCUIT | PDM_ERROR_LOW_LIMIT | PDM_ERROR_HIGH_LIMIT | PDM_ERROR_OVERFLOW)
#define PDM_PRV_CLEAR         (PDM_PRV_ERRORS | 2UL)
#define PDM_PRV_FIFO_USABLE    (30U)
#define PDM_PRV_FIFO_MAX       (31U)
#define PDM_PRV_STOP_POLLS    (1000U)

/* Reset coefficients, explicitly restored on every start (50.2.21-56). No arbitrary filter knobs. */
static uint16_t const g_pdm_coefficients[] =
{
    0x3F61, 0x3EC1, 0x4000, 0xC000,
    0x1FE8, 0x0039, 0x003C, 0x1E56, 0x01DC, 0x06E1, 0x01DC, 0x1E56, 0x003C, 0x0039, 0x1FE8,
    0x0400,
    0x1FF8, 0x000A, 0x1FF0, 0x0018, 0x1FDC, 0x0034, 0x1FB3, 0x0076, 0x1F2E, 0x0289,
    0x0289, 0x1F2E, 0x0076, 0x1FB3, 0x0034, 0x1FDC, 0x0018, 0x1FF0, 0x000A, 0x1FF8
};

/* Shared module stop gate; all instances on this core must use this driver. */
static pdm_instance_ctrl_t * gp_pdm_owner[BSP_FEATURE_PDM_CHANNELS];

void pdm_dat_isr(void);
void pdm_err_isr(void);

static void pdm_module_clock_set(bool enabled);
static uint32_t pdm_frames(pdm_instance_ctrl_t const * p_ctrl);
static void pdm_stop_request(pdm_instance_ctrl_t * p_ctrl);
static bool pdm_stopped(pdm_instance_ctrl_t * p_ctrl);
static void pdm_callback(pdm_instance_ctrl_t * p_ctrl, pdm_event_t event);
static void pdm_interrupt(void);

pdm_api_t const g_pdm_on_pdm =
{
    .open        = R_PDM_Open,
    .read        = R_PDM_Read,
    .start       = R_PDM_Start,
    .stop        = R_PDM_Stop,
    .statusGet   = R_PDM_StatusGet,
    .callbackSet = R_PDM_CallbackSet,
    .close       = R_PDM_Close,
};

/*******************************************************************************************************************//**
 * Configure channel ownership and interrupts. Requires running MOCO/PDMIFCLK and configured pins.
 * Returns IN_USE for overlapping mono/stereo instances, INVALID_ARGUMENT for invalid configuration.
 **********************************************************************************************************************/
fsp_err_t R_PDM_Open (pdm_ctrl_t * const p_api_ctrl, pdm_cfg_t const * const p_cfg)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
    FSP_ASSERT(p_cfg);
    FSP_ASSERT(p_cfg->p_extend);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN != p_ctrl->open, FSP_ERR_ALREADY_OPEN);
    FSP_ERROR_RETURN(p_cfg->channel < BSP_FEATURE_PDM_CHANNELS, FSP_ERR_INVALID_ARGUMENT);
    FSP_ERROR_RETURN((PDM_MODE_MONO == p_cfg->mode) ||
                     ((PDM_MODE_STEREO == p_cfg->mode) && (0U == p_cfg->channel)), FSP_ERR_UNSUPPORTED);
    uint8_t channels = (PDM_MODE_STEREO == p_cfg->mode) ? 2U : 1U;
    pdm_extended_cfg_t const * p_extend = p_cfg->p_extend;
    FSP_ERROR_RETURN(p_extend->microphone_startup_us <= 1000000U, FSP_ERR_INVALID_ARGUMENT);
    FSP_ERROR_RETURN(p_cfg->ipl < (1U << __NVIC_PRIO_BITS), FSP_ERR_INVALID_ARGUMENT);
    for (uint32_t i = 0; i < channels; i++)
    {
        FSP_ERROR_RETURN((p_cfg->dat_irq[i] >= 0) && (p_cfg->err_irq[i] >= 0) &&
                         ((uint32_t) p_cfg->dat_irq[i] < BSP_ICU_VECTOR_MAX_ENTRIES) &&
                         ((uint32_t) p_cfg->err_irq[i] < BSP_ICU_VECTOR_MAX_ENTRIES), FSP_ERR_INVALID_ARGUMENT);
        FSP_ERROR_RETURN(p_cfg->dat_irq[i] != p_cfg->err_irq[i], FSP_ERR_INVALID_ARGUMENT);
        if (i > 0U)
        {
            FSP_ERROR_RETURN((p_cfg->dat_irq[i] != p_cfg->dat_irq[0]) &&
                             (p_cfg->dat_irq[i] != p_cfg->err_irq[0]) &&
                             (p_cfg->err_irq[i] != p_cfg->dat_irq[0]) &&
                             (p_cfg->err_irq[i] != p_cfg->err_irq[0]), FSP_ERR_INVALID_ARGUMENT);
        }
    }
#if BSP_TZ_SECURE_BUILD
    /* Cross-security callbacks require an NSC interface, not provided by this module. */
    FSP_ERROR_RETURN((NULL == p_cfg->p_callback) ||
                     (NULL == cmse_check_address_range((void *) p_cfg->p_callback, sizeof(void *), CMSE_AU_NONSECURE)),
                     FSP_ERR_UNSUPPORTED);
#endif

    FSP_CRITICAL_SECTION_DEFINE;
    FSP_CRITICAL_SECTION_ENTER;
    for (uint32_t i = 0; i < channels; i++)
    {
        if (NULL != gp_pdm_owner[p_cfg->channel + i])
        {
            FSP_CRITICAL_SECTION_EXIT;
            return FSP_ERR_IN_USE;
        }
    }
    for (uint32_t n = 0; n < BSP_FEATURE_PDM_CHANNELS; n++)
    {
        pdm_instance_ctrl_t const * p_owner = gp_pdm_owner[n];
        if (NULL != p_owner)
        {
            for (uint32_t i = 0; i < channels; i++)
            {
                for (uint32_t j = 0; j < p_owner->channels; j++)
                {
                    if ((p_cfg->dat_irq[i] == p_owner->p_cfg->dat_irq[j]) ||
                        (p_cfg->dat_irq[i] == p_owner->p_cfg->err_irq[j]) ||
                        (p_cfg->err_irq[i] == p_owner->p_cfg->dat_irq[j]) ||
                        (p_cfg->err_irq[i] == p_owner->p_cfg->err_irq[j]))
                    {
                        FSP_CRITICAL_SECTION_EXIT;
                        return FSP_ERR_IN_USE;
                    }
                }
            }
        }
    }
    pdm_module_clock_set(true);
    p_ctrl->p_cfg             = p_cfg;
    p_ctrl->channels          = channels;
    p_ctrl->channel_mask      = ((1UL << channels) - 1U) << p_cfg->channel;
    p_ctrl->p_dest            = NULL;
    p_ctrl->requested_frames  = 0;
    p_ctrl->received_frames[0] = 0;
    p_ctrl->received_frames[1] = 0;
    p_ctrl->error_status      = 0;
    p_ctrl->state             = PDM_STATE_IDLE;
    p_ctrl->p_callback        = p_cfg->p_callback;
    p_ctrl->p_callback_memory = NULL;
    p_ctrl->p_context         = p_cfg->p_context;
    for (uint32_t i = 0; i < channels; i++)
    {
        gp_pdm_owner[p_cfg->channel + i] = p_ctrl;
        p_ctrl->p_reg[i] = (volatile uint32_t *) ((uintptr_t) R_PDMIF + 0x100U * (1U + p_cfg->channel + i));
        p_ctrl->p_reg[i][PDM_PRV_ICR] = 0;
        R_BSP_IrqCfgEnable(p_cfg->dat_irq[i], p_cfg->ipl, p_ctrl);
        R_BSP_IrqCfgEnable(p_cfg->err_irq[i], p_cfg->ipl, p_ctrl);
    }
    p_ctrl->open = PDM_PRV_OPEN;
    FSP_CRITICAL_SECTION_EXIT;
    return FSP_SUCCESS;
}

/*******************************************************************************************************************//** Queue a destination in complete frames, not bytes. */
fsp_err_t R_PDM_Read (pdm_ctrl_t * const p_api_ctrl, int32_t * const p_dest, uint32_t frames)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
    FSP_ASSERT(p_dest);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN == p_ctrl->open, FSP_ERR_NOT_OPEN);
    FSP_ERROR_RETURN(0U == ((uintptr_t) p_dest & 3U), FSP_ERR_INVALID_ARGUMENT);
    FSP_ERROR_RETURN((frames > 0U) && (frames <= UINT32_MAX / (4U * p_ctrl->channels)), FSP_ERR_INVALID_ARGUMENT);
    uint32_t bytes = frames * 4U * p_ctrl->channels;
    FSP_ERROR_RETURN((uintptr_t) p_dest <= UINTPTR_MAX - bytes, FSP_ERR_INVALID_ARGUMENT);
    if (PDM_STATE_STOPPING == p_ctrl->state)
    {
        (void) pdm_stopped(p_ctrl);
    }
    FSP_ERROR_RETURN(PDM_STATE_IDLE == p_ctrl->state, FSP_ERR_IN_USE);
    p_ctrl->p_dest = p_dest;
    p_ctrl->requested_frames = frames;
    p_ctrl->received_frames[0] = 0;
    p_ctrl->received_frames[1] = 0;
    p_ctrl->state = PDM_STATE_READY;
    return FSP_SUCCESS;
}

/*******************************************************************************************************************//** Start a fresh, bounded acquisition with the documented 8 kHz profile. */
fsp_err_t R_PDM_Start (pdm_ctrl_t * const p_api_ctrl)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN == p_ctrl->open, FSP_ERR_NOT_OPEN);
    FSP_ERROR_RETURN(PDM_STATE_READY == p_ctrl->state, FSP_ERR_INVALID_STATE);
    /* Section 50.1 defines PCLK as PCLKB; Table 50.5 guarantees only 1..100 MHz. */
    uint32_t pclk_hz = R_FSP_SystemClockHzGet(FSP_PRIV_CLOCK_PCLKB);
    FSP_ERROR_RETURN((pclk_hz >= 1000000U) && (pclk_hz <= 100000000U), FSP_ERR_UNSUPPORTED);
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        FSP_ERROR_RETURN(0U == (p_ctrl->p_reg[i][PDM_PRV_SR] & 1U), FSP_ERR_IN_USE);
    }
    p_ctrl->error_status = 0;
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        volatile uint32_t * p_reg = p_ctrl->p_reg[i];
        p_reg[PDM_PRV_ICR]  = 0;
        p_reg[PDM_PRV_SDCR] = 0;
        p_reg[PDM_PRV_DRCR] = 0;
        p_reg[PDM_PRV_MDSR] = i;       /* INPSEL: rising local input, or falling previous input. */
        p_reg[PDM_PRV_SFCR] = 0x057C0001UL;
        for (uint32_t j = 0; j < sizeof(g_pdm_coefficients) / sizeof(g_pdm_coefficients[0]); j++)
        {
            p_reg[PDM_PRV_COEFFICIENT + j] = g_pdm_coefficients[j];
        }
        p_reg[PDM_PRV_DBCR] = 0;       /* One sample threshold: every tail length can complete. */
    }
    /* Simultaneous start is mandatory for stereo (50.3.5). */
    R_PDMIF->PDCSTRTR = p_ctrl->channel_mask;
    /* Table 50.16: complete filter chain settles in < 3.4 ms at D=4, M=125, K=4. */
    pdm_extended_cfg_t const * p_extend = p_ctrl->p_cfg->p_extend;
    R_BSP_SoftwareDelay(4000U + p_extend->microphone_startup_us, BSP_DELAY_UNITS_MICROSECONDS);
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        if (0U == (p_ctrl->p_reg[i][PDM_PRV_SR] & 1U))
        {
            pdm_stop_request(p_ctrl);
            p_ctrl->state = PDM_STATE_FAULT;
            return FSP_ERR_TIMEOUT;
        }
    }
    /* Enable both FIFO read pointers together; preserve unrelated active channels. */
    FSP_CRITICAL_SECTION_DEFINE;
    FSP_CRITICAL_SECTION_ENTER;
    R_PDMIF->PDCDRCR |= p_ctrl->channel_mask;
    FSP_CRITICAL_SECTION_EXIT;
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        volatile uint32_t * p_reg = p_ctrl->p_reg[i];
        p_reg[PDM_PRV_SCR]  = PDM_PRV_CLEAR;
        /* DATRE 0->1 exposes 30 historical entries, not 30 newly acquired samples (50.3.7). */
        for (uint32_t j = 0; j < PDM_PRV_FIFO_USABLE; j++)
        {
            (void) p_reg[PDM_PRV_DRR];
        }
        p_reg[PDM_PRV_SCR] = PDM_PRV_CLEAR;
        p_reg[PDM_PRV_SDCR] = PDM_ERROR_OVERFLOW;
        R_BSP_IrqClearPending(p_ctrl->p_cfg->dat_irq[i]);
        R_BSP_IrqClearPending(p_ctrl->p_cfg->err_irq[i]);
    }
    FSP_CRITICAL_SECTION_ENTER;
    p_ctrl->state = PDM_STATE_CAPTURING;
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        p_ctrl->p_reg[i][PDM_PRV_ICR] = PDM_PRV_IDRE | PDM_PRV_IEDE;
    }
    FSP_CRITICAL_SECTION_EXIT;
    return FSP_SUCCESS;
}

/*******************************************************************************************************************//** Cancel capture; do not gate the module clock before STATE clears. */
fsp_err_t R_PDM_Stop (pdm_ctrl_t * const p_api_ctrl)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN == p_ctrl->open, FSP_ERR_NOT_OPEN);
    FSP_CRITICAL_SECTION_DEFINE;
    FSP_CRITICAL_SECTION_ENTER;
    if (PDM_STATE_READY == p_ctrl->state)
    {
        p_ctrl->state = PDM_STATE_IDLE;
        p_ctrl->p_dest = NULL;
    }
    else if (PDM_STATE_IDLE != p_ctrl->state)
    {
        pdm_stop_request(p_ctrl);
    }
    FSP_CRITICAL_SECTION_EXIT;
    for (uint32_t i = 0; i < PDM_PRV_STOP_POLLS; i++)
    {
        if (pdm_stopped(p_ctrl))
        {
            return FSP_SUCCESS;
        }
        R_BSP_SoftwareDelay(10U, BSP_DELAY_UNITS_MICROSECONDS);
    }
    p_ctrl->state = PDM_STATE_FAULT;
    return FSP_ERR_TIMEOUT;
}

fsp_err_t R_PDM_StatusGet (pdm_ctrl_t * const p_api_ctrl, pdm_status_t * const p_status)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
    FSP_ASSERT(p_status);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN == p_ctrl->open, FSP_ERR_NOT_OPEN);
    FSP_CRITICAL_SECTION_DEFINE;
    FSP_CRITICAL_SECTION_ENTER;
    if (PDM_STATE_STOPPING == p_ctrl->state)
    {
        (void) pdm_stopped(p_ctrl);
    }
    p_status->state = p_ctrl->state;
    p_status->frames = pdm_frames(p_ctrl);
    p_status->error_status = p_ctrl->error_status;
    p_status->sample_rate_hz = 8000U;
    FSP_CRITICAL_SECTION_EXIT;
    return FSP_SUCCESS;
}

fsp_err_t R_PDM_CallbackSet (pdm_ctrl_t * const p_api_ctrl, void (* p_callback)(pdm_callback_args_t *),
                           void const * const p_context, pdm_callback_args_t * const p_callback_memory)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN == p_ctrl->open, FSP_ERR_NOT_OPEN);
#if BSP_TZ_SECURE_BUILD
    FSP_ERROR_RETURN((NULL == p_callback) ||
                     (NULL == cmse_check_address_range((void *) p_callback, sizeof(void *), CMSE_AU_NONSECURE)),
                     FSP_ERR_UNSUPPORTED);
#endif
    FSP_CRITICAL_SECTION_DEFINE;
    FSP_CRITICAL_SECTION_ENTER;
    p_ctrl->p_callback = p_callback;
    p_ctrl->p_context = p_context;
    p_ctrl->p_callback_memory = p_callback_memory;
    FSP_CRITICAL_SECTION_EXIT;
    return FSP_SUCCESS;
}

fsp_err_t R_PDM_Close (pdm_ctrl_t * const p_api_ctrl)
{
    pdm_instance_ctrl_t * p_ctrl = (pdm_instance_ctrl_t *) p_api_ctrl;
#if PDM_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_ctrl);
#endif
    FSP_ERROR_RETURN(PDM_PRV_OPEN == p_ctrl->open, FSP_ERR_NOT_OPEN);
    fsp_err_t err = R_PDM_Stop(p_api_ctrl);
    FSP_ERROR_RETURN(FSP_SUCCESS == err, err);
    FSP_CRITICAL_SECTION_DEFINE;
    FSP_CRITICAL_SECTION_ENTER;
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        R_BSP_IrqDisable(p_ctrl->p_cfg->dat_irq[i]);
        R_BSP_IrqDisable(p_ctrl->p_cfg->err_irq[i]);
        R_BSP_IrqClearPending(p_ctrl->p_cfg->dat_irq[i]);
        R_BSP_IrqClearPending(p_ctrl->p_cfg->err_irq[i]);
        gp_pdm_owner[p_ctrl->p_cfg->channel + i] = NULL;
    }
    bool in_use = false;
    for (uint32_t i = 0; i < BSP_FEATURE_PDM_CHANNELS; i++)
    {
        in_use = in_use || (NULL != gp_pdm_owner[i]);
    }
    if (!in_use)
    {
        pdm_module_clock_set(false);
    }
    p_ctrl->open = 0;
    FSP_CRITICAL_SECTION_EXIT;
    return FSP_SUCCESS;
}

/* BSP module macros declare their own saved interrupt mask; keep their locals in a separate function scope. */
static void pdm_module_clock_set (bool enabled)
{
    if (enabled)
    {
        R_BSP_MODULE_START(FSP_IP_PDM, 0);
    }
    else
    {
        R_BSP_MODULE_STOP(FSP_IP_PDM, 0);
    }
}

static uint32_t pdm_frames (pdm_instance_ctrl_t const * p_ctrl)
{
    uint32_t frames = p_ctrl->received_frames[0];
    if ((p_ctrl->channels > 1U) && (p_ctrl->received_frames[1] < frames))
    {
        frames = p_ctrl->received_frames[1];
    }
    return frames;
}

static void pdm_stop_request (pdm_instance_ctrl_t * p_ctrl)
{
    R_PDMIF->PDCSTPTR = p_ctrl->channel_mask;
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        p_ctrl->p_reg[i][PDM_PRV_ICR] = 0;
        p_ctrl->p_reg[i][PDM_PRV_DRCR] = 0;
    }
    p_ctrl->p_dest = NULL;
    p_ctrl->state = PDM_STATE_STOPPING;
}

static bool pdm_stopped (pdm_instance_ctrl_t * p_ctrl)
{
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        if (0U != (p_ctrl->p_reg[i][PDM_PRV_SR] & 1U))
        {
            return false;
        }
    }
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        p_ctrl->p_reg[i][PDM_PRV_SDCR] = 0;
        p_ctrl->p_reg[i][PDM_PRV_SCR] = PDM_PRV_CLEAR;
    }
    p_ctrl->state = PDM_STATE_IDLE;
    return true;
}

static void pdm_callback (pdm_instance_ctrl_t * p_ctrl, pdm_event_t event)
{
    if (NULL != p_ctrl->p_callback)
    {
        pdm_callback_args_t args = {p_ctrl->p_context, event, pdm_frames(p_ctrl), p_ctrl->error_status};
        pdm_callback_args_t * p_args = &args;
        pdm_callback_args_t saved;
        if (NULL != p_ctrl->p_callback_memory)
        {
            p_args = p_ctrl->p_callback_memory;
            saved = *p_args;
            *p_args = args;
        }
        p_ctrl->p_callback(p_args);
        if (NULL != p_ctrl->p_callback_memory)
        {
            *p_args = saved;
        }
    }
}

/* Each entry services at most 30 samples per filter. Explicit repending handles the level-to-edge ICU bridge. */
static void pdm_interrupt (void)
{
    IRQn_Type irq = R_FSP_CurrentIrqGet();
    pdm_instance_ctrl_t * p_ctrl = R_FSP_IsrContextGet(irq);
    R_BSP_IrqStatusClear(irq);
    if ((NULL == p_ctrl) || (PDM_PRV_OPEN != p_ctrl->open) || (PDM_STATE_CAPTURING != p_ctrl->state))
    {
        return;
    }
    for (uint32_t i = 0; i < p_ctrl->channels; i++)
    {
        p_ctrl->error_status |= p_ctrl->p_reg[i][PDM_PRV_SR] & PDM_PRV_ERRORS;
    }
    if (0U == p_ctrl->error_status)
    {
        for (uint32_t i = 0; i < p_ctrl->channels; i++)
        {
            volatile uint32_t * p_reg = p_ctrl->p_reg[i];
            uint32_t before = p_ctrl->received_frames[i];
            uint32_t available = p_reg[PDM_PRV_DSR] & 0xFFU;
            /* DATNUM may transiently reach 31 (50.2.66); still cap work per IRQ at 30. */
            if (available > PDM_PRV_FIFO_MAX)
            {
                p_ctrl->error_status |= PDM_ERROR_OVERFLOW;
                break;
            }
            if (available > PDM_PRV_FIFO_USABLE)
            {
                available = PDM_PRV_FIFO_USABLE;
            }
            uint32_t remaining = p_ctrl->requested_frames - before;
            if (available > remaining)
            {
                available = remaining;
            }
            for (uint32_t j = 0; j < available; j++)
            {
                uint32_t sample = p_reg[PDM_PRV_DRR] & 0xFFFFFU;
                /* Portable sign extension without implementation-defined unsigned-to-signed conversion. */
                int32_t pcm = (int32_t) (sample & 0x7FFFFU) - (int32_t) (sample & 0x80000U);
                p_ctrl->p_dest[(before + j) * p_ctrl->channels + i] = pcm;
            }
            p_ctrl->error_status |= p_reg[PDM_PRV_SR] & PDM_PRV_ERRORS;
            if (0U != p_ctrl->error_status)
            {
                break;                /* This batch may have crossed an overwrite; don't count it. */
            }
            p_ctrl->received_frames[i] += available;
        }
    }
    if (0U != p_ctrl->error_status)
    {
        pdm_event_t event = (0U != (p_ctrl->error_status & PDM_ERROR_OVERFLOW)) ?
                            PDM_EVENT_OVERFLOW : PDM_EVENT_ERROR;
        pdm_stop_request(p_ctrl);
        pdm_callback(p_ctrl, event);
    }
    else if (pdm_frames(p_ctrl) == p_ctrl->requested_frames)
    {
        pdm_stop_request(p_ctrl);
        pdm_callback(p_ctrl, PDM_EVENT_CAPTURE_COMPLETE);
    }
    else
    {
        for (uint32_t i = 0; i < p_ctrl->channels; i++)
        {
            if (p_ctrl->received_frames[i] == p_ctrl->requested_frames)
            {
                p_ctrl->p_reg[i][PDM_PRV_ICR] = 0;
                p_ctrl->p_reg[i][PDM_PRV_DRCR] = 0;
            }
            else if (0U != (p_ctrl->p_reg[i][PDM_PRV_DSR] & 0xFFU))
            {
                NVIC_SetPendingIRQ(p_ctrl->p_cfg->dat_irq[i]);
            }
        }
    }
}

void pdm_dat_isr (void)
{
    FSP_CONTEXT_SAVE;
    pdm_interrupt();
    FSP_CONTEXT_RESTORE;
}

void pdm_err_isr (void)
{
    FSP_CONTEXT_SAVE;
    pdm_interrupt();
    FSP_CONTEXT_RESTORE;
}
