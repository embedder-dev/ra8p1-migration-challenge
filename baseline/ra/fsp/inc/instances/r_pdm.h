/*
 * SPDX-License-Identifier: BSD-3-Clause
 */
#ifndef R_PDM_H
#define R_PDM_H

#include "r_pdm_api.h"

FSP_HEADER

/** RA8P1 PDSRCHn error bits, retained in status and callbacks. */
#define PDM_ERROR_SHORT_CIRCUIT    (1UL << 16)
#define PDM_ERROR_LOW_LIMIT        (1UL << 17)
#define PDM_ERROR_HIGH_LIMIT       (1UL << 18)
#define PDM_ERROR_OVERFLOW         (1UL << 27)

/**
 * Fixed software profile: MOCO/PDMIFCLK = 8 MHz, CKDIV = 1 (/4), M = 125,
 * fourth-order sinc, SINCRNG = 5, default HPF/compensation/half-band coefficients,
 * DBIS = 0 (20-bit), giving 2 MHz PDM and 8000 frames/s nominal.
 * See R01UH1064EJ0120 sections 9.10.34, 50.2.19-20, 50.3.1, 50.3.4, 50.3.7 and 50.4.
 * The caller must supply a running MOCO, PCLKB in 1..100 MHz, correct PDM pin mux and
 * exclusive ownership on one CPU. The driver does not retune shared clocks or pins.
 * This is not a claim of analog/audio qualification on the physical board.
 */
typedef struct st_pdm_extended_cfg
{
    uint32_t microphone_startup_us;     ///< Datasheet wake-up/settling requirement, 0..1000000 us
} pdm_extended_cfg_t;

/** Instance storage. Initialize to zero before first open; thereafter owned by the driver. */
typedef struct st_pdm_instance_ctrl
{
    uint32_t open;
    pdm_cfg_t const * p_cfg;
    volatile uint32_t * p_reg[2];      ///< Channel windows derived from R_PDMIF and exact SVD offsets
    uint32_t channel_mask;
    uint8_t channels;
    volatile pdm_state_t state;
    int32_t * p_dest;
    uint32_t requested_frames;
    volatile uint32_t received_frames[2];
    volatile uint32_t error_status;
    void (* p_callback)(pdm_callback_args_t * p_args);
    pdm_callback_args_t * p_callback_memory;
    void const * p_context;
} pdm_instance_ctrl_t;

extern pdm_api_t const g_pdm_on_pdm;

fsp_err_t R_PDM_Open(pdm_ctrl_t * const p_ctrl, pdm_cfg_t const * const p_cfg);
fsp_err_t R_PDM_Read(pdm_ctrl_t * const p_ctrl, int32_t * const p_dest, uint32_t frames);
fsp_err_t R_PDM_Start(pdm_ctrl_t * const p_ctrl);
fsp_err_t R_PDM_Stop(pdm_ctrl_t * const p_ctrl);
fsp_err_t R_PDM_StatusGet(pdm_ctrl_t * const p_ctrl, pdm_status_t * const p_status);
fsp_err_t R_PDM_CallbackSet(pdm_ctrl_t * const p_ctrl, void (* p_callback)(pdm_callback_args_t *),
                          void const * const p_context, pdm_callback_args_t * const p_callback_memory);
fsp_err_t R_PDM_Close(pdm_ctrl_t * const p_ctrl);

FSP_FOOTER
#endif
