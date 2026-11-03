/* Standalone EK-RA8P1 PDM transport qualification; audio requires external stimulus analysis. */
#include "hal_data.h"
#include "r_pdm.h"

#define PDM_TEST_FRAMES   (8192U)
#define PDM_TEST_TIMEOUT  (3000U)
#define PDM_TEST_GUARD    (0xC35A96E1UL)
#define PDM_CMD_CAPTURE   (1U)
#define PDM_CMD_STOP      (2U)

extern const pdm_instance_t g_pdm0;
extern const ioport_cfg_t IOPORT_CFG_NAME;

typedef struct st_pdm_capture_record
{
    uint32_t sequence;             /* Published last; zero means incomplete. */
    uint32_t label;                /* 0 unspecified, 1 quiet, 2 acoustic stimulus (operator supplied). */
    uint32_t frames;
    uint32_t elapsed_ms;
    uint32_t sample_rate_hz;
    int32_t minimum;
    int32_t maximum;
    int64_t sum;
    uint64_t sum_squares;
    uint32_t changes;
    uint32_t out_of_range;
    uint32_t error_bits;
    uint32_t callbacks;
    uint32_t guard_ok;
    uint32_t cancelled;
    uint32_t transport_ok;
    fsp_err_t error;
} pdm_capture_record_t;

typedef struct st_pdm_test_result
{
    uint32_t magic;
    uint32_t core_hz;
    uint32_t pclkb_hz;
    uint32_t stage;                /* 1 initialization, 2 automatic tests, 3 command-ready, 4 fault. */
    volatile uint32_t ticks;
    volatile uint32_t command;
    volatile uint32_t label;
    volatile uint32_t active;
    volatile uint32_t completed;
    volatile uint32_t overflow_events;
    volatile uint32_t error_events;
    volatile uint32_t callback_frames;
    volatile uint32_t callback_count;
    volatile uint32_t callback_error_bits;
    volatile uint32_t callback_complete;
    uint32_t automatic_ok;
    uint32_t audio_verified;       /* Always zero: varied samples alone do not qualify audio. */
    fsp_err_t error;
    fsp_err_t io_error;
    pdm_capture_record_t records[4];
} pdm_test_result_t;

volatile pdm_test_result_t g_validation_pdm;
struct st_pdm_capture_buffer
{
    uint32_t before;
    int32_t samples[PDM_TEST_FRAMES];
    uint32_t after;
} g_validation_pdm_buffer;

void SysTick_Handler(void);
void R_BSP_WarmStart(bsp_warm_start_event_t event);
void validation_pdm_callback(pdm_callback_args_t * args);

void SysTick_Handler(void) { g_validation_pdm.ticks++; }
void validation_pdm_callback(pdm_callback_args_t * args)
{
    g_validation_pdm.callback_frames = args->frames;
    g_validation_pdm.callback_error_bits |= args->error_status;
    g_validation_pdm.callback_count++;
    if (args->event == PDM_EVENT_OVERFLOW) { g_validation_pdm.overflow_events++; }
    else if (args->event == PDM_EVENT_ERROR) { g_validation_pdm.error_events++; }
    else if (args->event == PDM_EVENT_CAPTURE_COMPLETE) { g_validation_pdm.callback_complete = 1U; }
}
void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    if (BSP_WARM_START_POST_C == event)
    {
        g_validation_pdm.io_error = R_IOPORT_Open(&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);
    }
}
static void pdm_marker(bsp_io_port_pin_t pin, bool high)
{
    fsp_err_t error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pin, high ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
    if (FSP_SUCCESS != error) { g_validation_pdm.io_error = error; }
}
static bool pdm_capture(bool cancel_automatically)
{
    pdm_capture_record_t record = {0};
    record.label = g_validation_pdm.label;
    g_validation_pdm.callback_frames = 0U;
    g_validation_pdm.callback_count = 0U;
    g_validation_pdm.callback_complete = 0U;
    g_validation_pdm.callback_error_bits = 0U;
    g_validation_pdm_buffer.before = PDM_TEST_GUARD;
    g_validation_pdm_buffer.after = PDM_TEST_GUARD;
    for (uint32_t i = 0; i < PDM_TEST_FRAMES; i++) { g_validation_pdm_buffer.samples[i] = INT32_MIN; }
    record.error = R_PDM_Read(g_pdm0.p_ctrl, g_validation_pdm_buffer.samples, PDM_TEST_FRAMES);
    if (FSP_SUCCESS != record.error) { g_validation_pdm.error = record.error; return false; }
    uint32_t start = g_validation_pdm.ticks;
    g_validation_pdm.active = 1U;
    pdm_marker(BSP_IO_PORT_01_PIN_11, true);
    record.error = R_PDM_Start(g_pdm0.p_ctrl);
    pdm_status_t status = {0};
    while (FSP_SUCCESS == record.error)
    {
        record.error = R_PDM_StatusGet(g_pdm0.p_ctrl, &status);
        if (FSP_SUCCESS != record.error) { break; }
        if (g_validation_pdm.callback_count || status.state == PDM_STATE_FAULT) { break; }
        if ((cancel_automatically && status.frames >= 64U) || g_validation_pdm.command == PDM_CMD_STOP)
        {
            g_validation_pdm.command = 0U;
            record.cancelled = 1U;
            break;
        }
        if ((uint32_t) (g_validation_pdm.ticks - start) >= PDM_TEST_TIMEOUT)
        {
            record.error = FSP_ERR_TIMEOUT;
            break;
        }
        __WFI();
    }
    fsp_err_t stop_error = R_PDM_Stop(g_pdm0.p_ctrl);
    if (FSP_SUCCESS != stop_error) { record.error = stop_error; }
    fsp_err_t status_error = R_PDM_StatusGet(g_pdm0.p_ctrl, &status);
    if (FSP_SUCCESS != status_error) { record.error = status_error; }
    record.frames = status.frames;
    record.elapsed_ms = g_validation_pdm.ticks - start;
    record.sample_rate_hz = status.sample_rate_hz;
    record.error_bits = status.error_status | g_validation_pdm.callback_error_bits;
    record.callbacks = g_validation_pdm.callback_count;
    record.guard_ok = (g_validation_pdm_buffer.before == PDM_TEST_GUARD) &&
                      (g_validation_pdm_buffer.after == PDM_TEST_GUARD);
    if (record.frames > PDM_TEST_FRAMES) { record.error = FSP_ERR_OVERFLOW; record.frames = 0U; }
    if (FSP_SUCCESS == stop_error)
    {
        record.minimum = INT32_MAX;
        record.maximum = INT32_MIN;
        for (uint32_t i = 0; i < record.frames; i++)
        {
            int32_t sample = g_validation_pdm_buffer.samples[i];
            if (sample < record.minimum) { record.minimum = sample; }
            if (sample > record.maximum) { record.maximum = sample; }
            record.sum += sample;
            record.sum_squares += (uint64_t) ((int64_t) sample * sample);
            if (sample < -524288 || sample > 524287) { record.out_of_range++; }
            if (i && sample != g_validation_pdm_buffer.samples[i - 1U]) { record.changes++; }
        }
    }
    record.transport_ok = (FSP_SUCCESS == record.error) && (status.state == PDM_STATE_IDLE) &&
                          record.guard_ok && !record.error_bits && !record.out_of_range &&
                          (record.sample_rate_hz == 8000U) &&
                          (record.cancelled ? (record.frames > 0U && record.frames < PDM_TEST_FRAMES &&
                                               record.callbacks == 0U) :
                           (record.frames == PDM_TEST_FRAMES && record.callbacks == 1U &&
                            g_validation_pdm.callback_complete && g_validation_pdm.callback_frames == record.frames));
    if (!record.transport_ok && FSP_SUCCESS == record.error) { record.error = FSP_ERR_INTERNAL; }
    uint32_t sequence = g_validation_pdm.completed + 1U;
    uint32_t slot = (sequence - 1U) % 4U;
    g_validation_pdm.records[slot].sequence = 0U;
    g_validation_pdm.records[slot] = record;
    __DMB();
    g_validation_pdm.records[slot].sequence = sequence;
    g_validation_pdm.completed = sequence;
    g_validation_pdm.error = record.error;
    g_validation_pdm.active = 0U;
    pdm_marker(BSP_IO_PORT_01_PIN_11, false);
    pdm_marker(BSP_IO_PORT_08_PIN_06, (sequence & 1U) != 0U);
    if (!record.transport_ok) { pdm_marker(BSP_IO_PORT_08_PIN_07, true); }
    return record.transport_ok != 0U;
}
void hal_entry(void)
{
    g_validation_pdm.magic = 0x50444D32UL;
    g_validation_pdm.stage = 1U;
    SystemCoreClockUpdate();
    g_validation_pdm.core_hz = SystemCoreClock;
    g_validation_pdm.pclkb_hz = R_FSP_SystemClockHzGet(FSP_PRIV_CLOCK_PCLKB);
    if (BSP_CFG_CPU_CORE != 0 || g_pdm0.p_cfg->channel != 2U || g_pdm0.p_cfg->mode != PDM_MODE_MONO ||
        R_SYSTEM->MOCOCR != 0U || g_validation_pdm.pclkb_hz < 1000000U ||
        g_validation_pdm.pclkb_hz > 100000000U || __get_PRIMASK() ||
        SystemCoreClock % 1000U || SysTick_Config(SystemCoreClock / 1000U))
    {
        g_validation_pdm.error = FSP_ERR_INVALID_MODE;
        g_validation_pdm.stage = 4U;
        return;
    }
    static const bsp_io_port_pin_t pins[] =
    {
        BSP_IO_PORT_05_PIN_14, BSP_IO_PORT_01_PIN_11, BSP_IO_PORT_08_PIN_06, BSP_IO_PORT_08_PIN_07
    };
    for (uint32_t i = 0; i < 4U; i++)
    {
        fsp_err_t error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, pins[i],
                                         IOPORT_CFG_PORT_DIRECTION_OUTPUT | IOPORT_CFG_PORT_OUTPUT_LOW);
        if (FSP_SUCCESS != error) { g_validation_pdm.io_error = error; }
    }
    if (FSP_SUCCESS != g_validation_pdm.io_error) { g_validation_pdm.stage = 4U; return; }
    /* RA8P1 pinmap: PSEL=11011b on P502/P812. FSP 5.9 has no named PDM selector. */
    g_validation_pdm.io_error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, BSP_IO_PORT_05_PIN_02,
                                               0x1B000000UL | IOPORT_CFG_PERIPHERAL_PIN);
    if (FSP_SUCCESS == g_validation_pdm.io_error)
    {
        g_validation_pdm.io_error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, BSP_IO_PORT_08_PIN_12,
                                                   0x1B000000UL | IOPORT_CFG_PERIPHERAL_PIN);
    }
    if (FSP_SUCCESS != g_validation_pdm.io_error) { g_validation_pdm.stage = 4U; return; }
    g_validation_pdm.error = R_PDM_Open(g_pdm0.p_ctrl, g_pdm0.p_cfg);
    if (FSP_SUCCESS == g_validation_pdm.error)
    {
        g_validation_pdm.error = R_PDM_CallbackSet(g_pdm0.p_ctrl, validation_pdm_callback, NULL, NULL);
    }
    if (FSP_SUCCESS != g_validation_pdm.error) { g_validation_pdm.stage = 4U; return; }
    pdm_marker(BSP_IO_PORT_05_PIN_14, true);
    g_validation_pdm.stage = 2U;
    /* Full capture, active cancellation, then full capture verifies the restart path. */
    if (!pdm_capture(false) || !pdm_capture(true) || !pdm_capture(false))
    {
        g_validation_pdm.stage = 4U;
        return;
    }
    g_validation_pdm.automatic_ok = 1U;
    g_validation_pdm.stage = 3U;
    for (;;)
    {
        if (g_validation_pdm.command == PDM_CMD_CAPTURE)
        {
            g_validation_pdm.command = 0U;
            if (!pdm_capture(false)) { g_validation_pdm.stage = 4U; return; }
        }
        else if (g_validation_pdm.command == PDM_CMD_STOP) { g_validation_pdm.command = 0U; }
        __WFI();
    }
}
