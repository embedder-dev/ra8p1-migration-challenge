/*
 * Copyright (c) 2020 - 2025 Renesas Electronics Corporation and/or its affiliates
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "r_dac_b.h"

/***********************************************************************************************************************
 * Macro definitions
 **********************************************************************************************************************/
#define DAC_B_OPEN                    (0x44414342UL)
#define DAC_B_CHANNEL_COUNT           (2U)
#define DAC_B_TRANSFER_TIMEOUT_US     (1000U)
#define DAC_B_TRANSFER_START          (0x4501U)
#define DAC_B_TRANSFER_LIST           (1U)

/***********************************************************************************************************************
 * Private function prototypes
 **********************************************************************************************************************/
static fsp_err_t dac_b_coefficients_transfer(void);

const dac_api_t g_dac_on_dac_b =
{
    .open  = R_DAC_B_Open,
    .close = R_DAC_B_Close,
    .write = R_DAC_B_Write,
    .start = R_DAC_B_Start,
    .stop  = R_DAC_B_Stop,
};

/*******************************************************************************************************************//**
 * @addtogroup DAC_B
 * @{
 **********************************************************************************************************************/

/*******************************************************************************************************************//**
 * Prepare one DAC channel, leaving its output stopped. Implements @ref dac_api_t::open.
 * Coefficient transfer requires exclusive application ownership of the MRAM controller during this call.
 * @retval FSP_SUCCESS           Channel prepared.
 * @retval FSP_ERR_ASSERTION     A required pointer is NULL.
 * @retval FSP_ERR_ALREADY_OPEN  This instance is already open.
 * @retval FSP_ERR_INVALID_CHANNEL Channel is not 0 or 1.
 * @retval FSP_ERR_INVALID_ARGUMENT Invalid format or voltage mode.
 * @retval FSP_ERR_UNSUPPORTED   ADC synchronization was requested.
 * @retval FSP_ERR_IN_USE        MRAM is not idle in read mode.
 * @retval FSP_ERR_TIMEOUT       Coefficient transfer did not complete within the software bound.
 **********************************************************************************************************************/
fsp_err_t R_DAC_B_Open (dac_ctrl_t * const p_ctrl, dac_cfg_t const * const p_cfg)
{
    dac_b_instance_ctrl_t * p_instance_ctrl = (dac_b_instance_ctrl_t *) p_ctrl;
#if DAC_B_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_instance_ctrl);
    FSP_ASSERT(p_cfg);
    FSP_ASSERT(p_cfg->p_extend);
#endif
    FSP_ERROR_RETURN(DAC_B_OPEN != p_instance_ctrl->open, FSP_ERR_ALREADY_OPEN);
    FSP_ERROR_RETURN(p_cfg->channel < DAC_B_CHANNEL_COUNT, FSP_ERR_INVALID_CHANNEL);
    FSP_ERROR_RETURN(!p_cfg->ad_da_synchronized, FSP_ERR_UNSUPPORTED);

    dac_b_extended_cfg_t const * p_extend = (dac_b_extended_cfg_t const *) p_cfg->p_extend;
    FSP_ERROR_RETURN((DAC_DATA_FORMAT_FLUSH_RIGHT == p_extend->data_format) ||
                     (DAC_DATA_FORMAT_FLUSH_LEFT == p_extend->data_format), FSP_ERR_INVALID_ARGUMENT);
    FSP_ERROR_RETURN((DAC_B_VOLTAGE_MODE_NORMAL == p_extend->voltage_mode) ||
                     (DAC_B_VOLTAGE_MODE_LOW == p_extend->voltage_mode), FSP_ERR_INVALID_ARGUMENT);

    fsp_err_t err = dac_b_coefficients_transfer();
    FSP_ERROR_RETURN(FSP_SUCCESS == err, err);

    R_DAC0_B_Type * p_reg = (0U == p_cfg->channel) ? R_DAC0_B : (R_DAC0_B_Type *) R_DAC1_B;
    R_BSP_MODULE_START(FSP_IP_DAC, p_cfg->channel);

    /* RA8P1 hardware manual 55.3.2: isolate output before configuring the converter. */
    p_reg->DACR0 = R_DAC0_B_DACR0_DAOUTDIS_Msk;
    p_reg->DACR1 = (uint32_t) p_extend->data_format << R_DAC0_B_DACR1_DPSEL_Pos;
    p_reg->DACR2 = (uint32_t) p_extend->voltage_mode << R_DAC0_B_DACR2_OFSSEL_Pos;
    p_instance_ctrl->value = (DAC_B_VOLTAGE_MODE_NORMAL == p_extend->voltage_mode) ? 0x0E0U : 0x1F8U;
    if (DAC_DATA_FORMAT_FLUSH_LEFT == p_extend->data_format)
    {
        p_instance_ctrl->value = (uint16_t) (p_instance_ctrl->value << 4);
    }

    p_reg->DADR                = p_instance_ctrl->value;
    p_instance_ctrl->p_reg     = p_reg;
    p_instance_ctrl->p_cfg     = p_cfg;
    p_instance_ctrl->p_extend  = p_extend;
    p_instance_ctrl->started   = false;
    p_instance_ctrl->open      = DAC_B_OPEN;

    return FSP_SUCCESS;
}

/*******************************************************************************************************************//**
 * Cache or output one sample in the configured justification. Implements @ref dac_api_t::write.
 * @retval FSP_SUCCESS Sample accepted. Output settles after at most 3.5 us (normal) or 6 us (low voltage).
 * @retval FSP_ERR_ASSERTION NULL control block.
 * @retval FSP_ERR_NOT_OPEN Channel is not open.
 * @retval FSP_ERR_INVALID_ARGUMENT Nonzero bits outside the selected 12-bit field.
 **********************************************************************************************************************/
fsp_err_t R_DAC_B_Write (dac_ctrl_t * const p_ctrl, uint16_t value)
{
    dac_b_instance_ctrl_t * p_instance_ctrl = (dac_b_instance_ctrl_t *) p_ctrl;
#if DAC_B_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_instance_ctrl);
#endif
    FSP_ERROR_RETURN(DAC_B_OPEN == p_instance_ctrl->open, FSP_ERR_NOT_OPEN);
    uint16_t unused_mask = (DAC_DATA_FORMAT_FLUSH_LEFT == p_instance_ctrl->p_extend->data_format) ? 0x000FU : 0xF000U;
    FSP_ERROR_RETURN(0U == (value & unused_mask), FSP_ERR_INVALID_ARGUMENT);
    p_instance_ctrl->value = value;
    if (p_instance_ctrl->started)
    {
        p_instance_ctrl->p_reg->DADR = value;
    }

    return FSP_SUCCESS;
}

/*******************************************************************************************************************//**
 * Start independent channel output using the manual 55.3 startup sequence. Implements @ref dac_api_t::start.
 * @retval FSP_SUCCESS Output enabled and settled.
 * @retval FSP_ERR_ASSERTION NULL control block.
 * @retval FSP_ERR_NOT_OPEN Channel is not open.
 **********************************************************************************************************************/
fsp_err_t R_DAC_B_Start (dac_ctrl_t * const p_ctrl)
{
    dac_b_instance_ctrl_t * p_instance_ctrl = (dac_b_instance_ctrl_t *) p_ctrl;
#if DAC_B_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_instance_ctrl);
#endif
    FSP_ERROR_RETURN(DAC_B_OPEN == p_instance_ctrl->open, FSP_ERR_NOT_OPEN);
    if (!p_instance_ctrl->started)
    {
        R_DAC0_B_Type * p_reg = p_instance_ctrl->p_reg;
        bool low_voltage = DAC_B_VOLTAGE_MODE_LOW == p_instance_ctrl->p_extend->voltage_mode;
        uint32_t settle_us = low_voltage ? 6U : 4U;
        uint16_t initial_value = low_voltage ? 0x1F8U : 0x0E0U;
        if (DAC_DATA_FORMAT_FLUSH_LEFT == p_instance_ctrl->p_extend->data_format)
        {
            initial_value = (uint16_t) (initial_value << 4);
        }

        p_reg->DACR0 = R_DAC0_B_DACR0_DAOUTDIS_Msk;
        p_reg->DADR  = initial_value;
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MICROSECONDS); /* tSU >= 4 ns. */
        p_reg->DACR0 = R_DAC0_B_DACR0_DAOUTDIS_Msk | R_DAC0_B_DACR0_DACEN_Msk;
        R_BSP_SoftwareDelay(settle_us, BSP_DELAY_UNITS_MICROSECONDS); /* tDISOUT. */
        if (!p_instance_ctrl->p_extend->internal_output)
        {
            p_reg->DACR0 = R_DAC0_B_DACR0_DACEN_Msk;
            R_BSP_SoftwareDelay(settle_us, BSP_DELAY_UNITS_MICROSECONDS); /* tDSLPUP2. */
        }

        p_reg->DADR = p_instance_ctrl->value;
        R_BSP_SoftwareDelay(settle_us, BSP_DELAY_UNITS_MICROSECONDS); /* tDCONV1/2. */
        p_instance_ctrl->started = true;
    }

    return FSP_SUCCESS;
}

/*******************************************************************************************************************//**
 * Stop the channel without changing the other channel. Implements @ref dac_api_t::stop.
 * @retval FSP_SUCCESS Output stopped.
 * @retval FSP_ERR_ASSERTION NULL control block.
 * @retval FSP_ERR_NOT_OPEN Channel is not open.
 **********************************************************************************************************************/
fsp_err_t R_DAC_B_Stop (dac_ctrl_t * const p_ctrl)
{
    dac_b_instance_ctrl_t * p_instance_ctrl = (dac_b_instance_ctrl_t *) p_ctrl;
#if DAC_B_CFG_PARAM_CHECKING_ENABLE
    FSP_ASSERT(p_instance_ctrl);
#endif
    FSP_ERROR_RETURN(DAC_B_OPEN == p_instance_ctrl->open, FSP_ERR_NOT_OPEN);
    p_instance_ctrl->p_reg->DACR0 = R_DAC0_B_DACR0_DAOUTDIS_Msk;
    p_instance_ctrl->started = false;

    return FSP_SUCCESS;
}

/*******************************************************************************************************************//**
 * Stop and release the channel. Implements @ref dac_api_t::close.
 * @retval FSP_SUCCESS Channel closed.
 * @retval FSP_ERR_ASSERTION NULL control block.
 * @retval FSP_ERR_NOT_OPEN Channel is not open.
 **********************************************************************************************************************/
fsp_err_t R_DAC_B_Close (dac_ctrl_t * const p_ctrl)
{
    dac_b_instance_ctrl_t * p_instance_ctrl = (dac_b_instance_ctrl_t *) p_ctrl;
    fsp_err_t err = R_DAC_B_Stop(p_ctrl);
    FSP_ERROR_RETURN(FSP_SUCCESS == err, err);
    R_BSP_MODULE_STOP(FSP_IP_DAC, p_instance_ctrl->p_cfg->channel);
    p_instance_ctrl->open = 0U;

    return FSP_SUCCESS;
}

/** @} */

/*******************************************************************************************************************//**
 * Transfer only the factory DAC coefficient list (RA8P1 hardware manual 60.10).
 * Do not switch MRAM mode or alter security/option data to make this operation succeed.
 **********************************************************************************************************************/
static fsp_err_t dac_b_coefficients_transfer (void)
{
    FSP_ERROR_RETURN((0U == R_MRAM->MENTRYR) && (0U == R_MRAM->MCTRSTATR) &&
                     (0U == (R_MRAM->MREZS & R_MRAM_MREZS_WHUKEXE_Msk)), FSP_ERR_IN_USE);
    uint8_t saved_list = R_MRAM->MCTRLSR;
    R_MRAM->MCTRLSR   = DAC_B_TRANSFER_LIST;
    R_MRAM->MCTRCNTR  = DAC_B_TRANSFER_START;
    uint32_t remaining = DAC_B_TRANSFER_TIMEOUT_US;
    while ((0U != (R_MRAM->MCTRSTATR & R_MRAM_MCTRSTATR_TRBUSY_Msk)) && (remaining > 0U))
    {
        R_BSP_SoftwareDelay(1U, BSP_DELAY_UNITS_MICROSECONDS);
        remaining--;
    }

    /* A timed-out transfer cannot be cancelled; do not write its list while it is busy. */
    FSP_ERROR_RETURN(0U != remaining, FSP_ERR_TIMEOUT);
    R_MRAM->MCTRLSR = saved_list;
    FSP_ERROR_RETURN((0U == R_MRAM->MCTRSTATR) &&
                     (0U == (R_MRAM->MREZS & R_MRAM_MREZS_WHUKEXE_Msk)), FSP_ERR_INVALID_STATE);

    return FSP_SUCCESS;
}
