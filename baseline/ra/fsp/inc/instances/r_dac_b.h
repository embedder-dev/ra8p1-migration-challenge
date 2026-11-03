/*
 * Copyright (c) 2020 - 2025 Renesas Electronics Corporation and/or its affiliates
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */
#ifndef R_DAC_B_H
#define R_DAC_B_H

#include "bsp_api.h"
#include "r_dac_api.h"
#include "r_dac_b_cfg.h"

FSP_HEADER

/*******************************************************************************************************************//**
 * @addtogroup DAC_B
 * @{
 * DAC12 with independent channel control. Configure the pin as analog before starting output.
 * VREFH/VREFL are the external references; this driver does not configure the reference supply.
 * The application must serialize Open with other DAC Open calls and all MRAM operations, including across cores.
 * Open transfers DAC coefficients from extra MRAM in read mode, but never programs extra MRAM or options.
 * ELC startup, collective channel enable, and ADC synchronization are not supported by this implementation.
 **********************************************************************************************************************/
typedef enum e_dac_b_voltage_mode
{
    DAC_B_VOLTAGE_MODE_NORMAL = 0,      ///< VREFH >= 2.7 V.
    DAC_B_VOLTAGE_MODE_LOW    = 1       ///< VREFH < 2.7 V (within the device electrical limits).
} dac_b_voltage_mode_t;

typedef struct st_dac_b_extended_cfg
{
    dac_data_format_t    data_format;
    dac_b_voltage_mode_t voltage_mode;
    bool                 internal_output; ///< Internal comparator/ADC output instead of the external pin.
} dac_b_extended_cfg_t;

/** Private control block. Initialize to zero before the first Open. */
typedef struct st_dac_b_instance_ctrl
{
    uint32_t                     open;
    R_DAC0_B_Type              * p_reg;
    dac_cfg_t const           * p_cfg;
    dac_b_extended_cfg_t const * p_extend;
    uint16_t                     value;
    bool                         started;
} dac_b_instance_ctrl_t;

extern const dac_api_t g_dac_on_dac_b;

fsp_err_t R_DAC_B_Open(dac_ctrl_t * const p_ctrl, dac_cfg_t const * const p_cfg);
fsp_err_t R_DAC_B_Write(dac_ctrl_t * const p_ctrl, uint16_t value);
fsp_err_t R_DAC_B_Start(dac_ctrl_t * const p_ctrl);
fsp_err_t R_DAC_B_Stop(dac_ctrl_t * const p_ctrl);
fsp_err_t R_DAC_B_Close(dac_ctrl_t * const p_ctrl);

/** @} */
FSP_FOOTER
#endif
