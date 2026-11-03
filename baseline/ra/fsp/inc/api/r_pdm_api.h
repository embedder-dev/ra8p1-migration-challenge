/*
 * SPDX-License-Identifier: BSD-3-Clause
 */

/*******************************************************************************************************************//**
 * @ingroup RENESAS_CONNECTIVITY_INTERFACES
 * @defgroup PDM_API PDM Interface
 * @brief Bounded, interrupt-driven PDM microphone acquisition.
 * @{
 **********************************************************************************************************************/
#ifndef R_PDM_API_H
#define R_PDM_API_H

#include "bsp_api.h"

FSP_HEADER

/** A frame contains one sample per selected edge, rising first, then falling. */
typedef enum e_pdm_mode
{
    PDM_MODE_MONO   = 0,                ///< Rising edge of the selected input channel
    PDM_MODE_STEREO = 1,                ///< Input 0 rising and falling edges, using filter channels 0 and 1
} pdm_mode_t;

typedef enum e_pdm_state
{
    PDM_STATE_IDLE = 0,
    PDM_STATE_READY,                    ///< Destination queued, clock not running
    PDM_STATE_CAPTURING,
    PDM_STATE_STOPPING,                 ///< Buffer released; hardware stop acknowledgement pending
    PDM_STATE_FAULT,                    ///< Stop timed out; retry stop before reuse/close
} pdm_state_t;

typedef enum e_pdm_event
{
    PDM_EVENT_CAPTURE_COMPLETE = 0,
    PDM_EVENT_OVERFLOW,
    PDM_EVENT_ERROR,
} pdm_event_t;

typedef struct st_pdm_callback_args
{
    void const * p_context;             ///< User context (FSP 5.9 const semantics)
    pdm_event_t event;
    uint32_t frames;                    ///< Complete valid frames, not bytes or individual stereo samples
    uint32_t error_status;              ///< OR of channel PDSR error bits; see instance header
} pdm_callback_args_t;

typedef void pdm_ctrl_t;

typedef struct st_pdm_status
{
    pdm_state_t state;
    uint32_t frames;                    ///< Complete frames written in the most recent request
    uint32_t error_status;              ///< Latched until the next successful start
    uint32_t sample_rate_hz;            ///< Nominal frame rate; actual rate follows MOCO tolerance
} pdm_status_t;

/** Configuration and callback storage must persist until close. */
typedef struct st_pdm_cfg
{
    uint8_t channel;                    ///< Physical input 0..2; stereo is supported only on input 0
    pdm_mode_t mode;
    uint8_t ipl;                        ///< Same priority for every data/error IRQ in this instance
    IRQn_Type dat_irq[2];               ///< Rising filter IRQ, then falling filter IRQ (stereo only)
    IRQn_Type err_irq[2];
    void (* p_callback)(pdm_callback_args_t * p_args); ///< Optional; status remains usable without it
    void const * p_context;
    void const * p_extend;              ///< Required pdm_extended_cfg_t
} pdm_cfg_t;

/** All calls for an instance must be serialized by the application. No heap or RTOS is used.
 * Call open/read/start/stop/close/callbackSet from thread context on the owning core, never from a callback.
 * statusGet is nonblocking and may be called from the callback. IRQs must remain enabled during capture.
 */
typedef struct st_pdm_api
{
    fsp_err_t (* open)(pdm_ctrl_t * const p_ctrl, pdm_cfg_t const * const p_cfg);

    /** Queue a fresh capture while idle. p_dest is a four-byte-aligned array of frames * channels int32_t values.
     * Samples are signed 20-bit two's-complement PCM sign-extended to int32_t, NOT full-scale 32-bit PCM.
     * Buffer must remain valid and untouched until completion/error callback, stop return, or STOPPING status.
     * Only status.frames complete interleaved frames are valid after an error or cancellation.
     */
    fsp_err_t (* read)(pdm_ctrl_t * const p_ctrl, int32_t * const p_dest, uint32_t frames);

    /** Start the queued request. Blocks for microphone startup plus digital filter settling.
     * Completion/error releases the buffer, requests hardware stop and calls the callback in IRQ context.
     * Before restarting, poll statusGet until IDLE or use stop to wait with a bounded timeout.
     */
    fsp_err_t (* start)(pdm_ctrl_t * const p_ctrl);

    /** Cancel acquisition and wait at most 10 ms for stop acknowledgement. No cancellation callback.
     * A timeout leaves FAULT state and keeps the module clock enabled; retry stop, not close or start.
     */
    fsp_err_t (* stop)(pdm_ctrl_t * const p_ctrl);
    fsp_err_t (* statusGet)(pdm_ctrl_t * const p_ctrl, pdm_status_t * const p_status);
    fsp_err_t (* callbackSet)(pdm_ctrl_t * const p_ctrl, void (* p_callback)(pdm_callback_args_t *),
                             void const * const p_context, pdm_callback_args_t * const p_callback_memory);
    fsp_err_t (* close)(pdm_ctrl_t * const p_ctrl);
} pdm_api_t;

typedef struct st_pdm_instance
{
    pdm_ctrl_t * p_ctrl;
    pdm_cfg_t const * p_cfg;
    pdm_api_t const * p_api;
} pdm_instance_t;

FSP_FOOTER
#endif
/** @} */
