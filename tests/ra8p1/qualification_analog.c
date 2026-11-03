/* Standalone generated-HAL fixture: P014 (DAC0) -- 1 kohm -- P001 (AN001).
 * Run with the other core and all other analog/MRAM clients stopped. */
#include "hal_data.h"

#define ANALOG_MAGIC       (0x414E4C47UL)
#define ANALOG_PASSES      (3U)
#define ANALOG_LEVELS      (6U)
#define ANALOG_SAMPLES     (8U)
#define ANALOG_WAIT_US     (100U)
#define ANALOG_SCAN_POLLS  (1000U)
#define ANALOG_CAL_POLLS   (20000U)

typedef struct st_analog_sample
{
    uint32_t raw;
    uint16_t value;
    fsp_err_t error;
} analog_sample_t;

typedef struct st_analog_result
{
    uint32_t magic;
    uint32_t core;
    uint32_t complete;
    uint32_t passed;
    uint32_t phase;
    fsp_err_t error;
    fsp_err_t io_error;
    fsp_err_t adc_close_error;
    fsp_err_t dac_close_error;
    uint32_t calibration_state;
    uint32_t calibration_events;
    uint32_t scan_events;
    uint32_t error_events;
    uint32_t last_event;
    uint32_t reference_ratio_ppm;
    uint32_t tolerance_ppm;
    uint32_t absolute_tolerance_counts;
    uint32_t settling_us;
    uint32_t completed_samples;
    uint32_t monotonic_failures;
    uint32_t ratio_failures;
    uint32_t repeatability_failures;
    uint32_t repeatability_limit_counts;
    uint32_t expected[ANALOG_LEVELS];
    uint32_t mean[ANALOG_PASSES][ANALOG_LEVELS];
    uint32_t measured_ratio_ppm[ANALOG_PASSES][ANALOG_LEVELS];
    int32_t residual_counts[ANALOG_PASSES][ANALOG_LEVELS];
    uint32_t fitted_gain_ppm[ANALOG_PASSES];
    int32_t fitted_offset_counts[ANALOG_PASSES];
    analog_sample_t samples[ANALOG_PASSES][ANALOG_LEVELS][ANALOG_SAMPLES];
} analog_result_t;

volatile analog_result_t g_validation_analog;
/* Optional independently measured VREFH(DAC)/VREFH0(ADC) ratio. Never fitted to samples. */
volatile uint32_t g_analog_reference_ratio_ppm = 1000000U;
static uint16_t const g_analog_levels[ANALOG_LEVELS] = {768U, 1280U, 1792U, 2304U, 2816U, 3328U};
static bsp_io_port_pin_t const g_analog_markers[] =
{
    BSP_IO_PORT_05_PIN_14, BSP_IO_PORT_01_PIN_11, BSP_IO_PORT_08_PIN_06, BSP_IO_PORT_08_PIN_07
};

void analog_validation_callback(adc_callback_args_t * p_args);
void R_BSP_WarmStart(bsp_warm_start_event_t event);

static void analog_marker(uint32_t index, bsp_io_level_t level)
{
    fsp_err_t error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, g_analog_markers[index], level);
    if (FSP_SUCCESS != error)
    {
        g_validation_analog.io_error = error;
    }
}

void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    if (BSP_WARM_START_POST_C == event)
    {
        g_validation_analog.io_error = R_IOPORT_Open(&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);
    }
}

void analog_validation_callback(adc_callback_args_t * p_args)
{
    g_validation_analog.last_event = (uint32_t) p_args->event;
    if ((ADC_EVENT_SCAN_COMPLETE == p_args->event) && (p_args->group_mask & ADC_GROUP_MASK_0))
    {
        g_validation_analog.scan_events++;
    }
    else if (ADC_EVENT_CALIBRATION_COMPLETE == p_args->event)
    {
        g_validation_analog.calibration_events++;
    }
    else
    {
        g_validation_analog.error_events++;
    }
}

static fsp_err_t analog_wait(bool calibration, uint32_t previous_events)
{
    uint32_t polls = calibration ? ANALOG_CAL_POLLS : ANALOG_SCAN_POLLS;
    adc_b_instance_ctrl_t const * p_ctrl = (adc_b_instance_ctrl_t const *) g_adc0.p_ctrl;
    while (polls-- > 0U)
    {
        uint32_t state = ((volatile adc_b_instance_ctrl_t const *) p_ctrl)->adc_state;
        g_validation_analog.calibration_state = state;
        if ((ADC_B_CONVERTER_STATE_CALIBRATION_FAIL == state) || (g_validation_analog.error_events != 0U))
        {
            return FSP_ERR_INVALID_DATA;
        }
        if (calibration)
        {
            if ((ADC_B_CONVERTER_STATE_READY == state) && (g_validation_analog.calibration_events != 0U))
            {
                return FSP_SUCCESS;
            }
        }
        else if ((g_validation_analog.scan_events != previous_events) && (R_ADC_B->ADSR == 0U))
        {
            return FSP_SUCCESS;
        }
        R_BSP_SoftwareDelay(ANALOG_WAIT_US, BSP_DELAY_UNITS_MICROSECONDS);
    }
    return FSP_ERR_TIMEOUT;
}

static fsp_err_t analog_check_configuration(void)
{
    adc_b_extended_cfg_t const * p_ext = g_adc0.p_cfg->p_extend;
    adc_b_scan_cfg_t const * p_scan = &g_adc0_scan_cfg;
    dac_b_extended_cfg_t const * p_dac = g_dac0.p_cfg->p_extend;
    if ((NULL == p_ext) || (NULL == p_dac) ||
        (g_dac0.p_cfg->channel != 0U) || p_dac->internal_output ||
        (p_dac->data_format != DAC_DATA_FORMAT_FLUSH_RIGHT) ||
        (p_dac->voltage_mode != DAC_B_VOLTAGE_MODE_NORMAL) ||
        (p_ext->adc_b_mode != 0U) || (p_ext->sample_and_hold_enable_mask != 0U) ||
        (p_scan->group_count != 1U) || (NULL == p_scan->p_adc_groups))
    {
        return FSP_ERR_INVALID_ARGUMENT;
    }
    adc_b_group_cfg_t const * p_group = p_scan->p_adc_groups[0];
    if ((NULL == p_group) || (p_group->scan_group_id != ADC_GROUP_ID_0) ||
        (p_group->converter_selection != ADC_B_UNIT_ID_0) || !p_group->scan_group_enable ||
        !p_group->scan_end_interrupt_enable || (p_group->virtual_channel_count != 1U) ||
        (NULL == p_group->p_virtual_channels) || p_group->external_trigger_enable_mask ||
        p_group->elc_trigger_enable_mask || p_group->gpt_trigger_enable_mask)
    {
        return FSP_ERR_INVALID_ARGUMENT;
    }
    adc_b_virtual_channel_cfg_t const * p_channel = p_group->p_virtual_channels[0];
    if ((NULL == p_channel) || (p_channel->channel_id != ADC_B_VIRTUAL_CHANNEL_0) ||
        (p_channel->channel_cfg_bits.channel != ADC_CHANNEL_1) ||
        p_channel->channel_cfg_bits.differential || (p_channel->channel_control_a != 0U) ||
        (p_channel->channel_control_b != 0U) ||
        (p_channel->channel_control_c_bits.channel_data_format != ADC_B_DATA_FORMAT_16_BIT) ||
        !p_channel->channel_control_c_bits.data_is_unsigned)
    {
        return FSP_ERR_INVALID_ARGUMENT;
    }
    return FSP_SUCCESS;
}

void hal_entry(void)
{
    bool adc_open = false;
    bool dac_open = false;
    fsp_err_t error = g_validation_analog.io_error;
    g_validation_analog.magic = ANALOG_MAGIC;
    g_validation_analog.core = BSP_CFG_CPU_CORE;
    g_validation_analog.reference_ratio_ppm = g_analog_reference_ratio_ppm;
    g_validation_analog.tolerance_ppm = 50000U;
    g_validation_analog.absolute_tolerance_counts = 128U;
    g_validation_analog.repeatability_limit_counts = 128U;
    g_validation_analog.settling_us = 1000U;
    if (FSP_SUCCESS != error) { goto finish; }
    for (uint32_t i = 0U; i < 4U; i++)
    {
        error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, g_analog_markers[i],
                               IOPORT_CFG_PORT_DIRECTION_OUTPUT | IOPORT_CFG_PORT_OUTPUT_LOW);
        if (FSP_SUCCESS != error) { goto finish; }
    }
    analog_marker(0U, BSP_IO_LEVEL_HIGH);
    error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, BSP_IO_PORT_00_PIN_14, IOPORT_CFG_ANALOG_ENABLE);
    if (FSP_SUCCESS != error) { goto finish; }
    error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, BSP_IO_PORT_00_PIN_01, IOPORT_CFG_ANALOG_ENABLE);
    if (FSP_SUCCESS != error) { goto finish; }
    if ((g_validation_analog.reference_ratio_ppm < 900000U) ||
        (g_validation_analog.reference_ratio_ppm > 1100000U))
    {
        error = FSP_ERR_INVALID_ARGUMENT;
        goto finish;
    }
    error = analog_check_configuration();
    if (FSP_SUCCESS != error) { goto finish; }
    g_validation_analog.phase = 1U;
    error = R_DAC_B_Open(g_dac0.p_ctrl, g_dac0.p_cfg);
    if (FSP_SUCCESS != error) { goto finish; }
    dac_open = true;
    error = R_DAC_B_Start(g_dac0.p_ctrl);
    if (FSP_SUCCESS != error) { goto finish; }
    error = R_ADC_B_Open(g_adc0.p_ctrl, g_adc0.p_cfg);
    if (FSP_SUCCESS != error) { goto finish; }
    adc_open = true;
    error = R_ADC_B_CallbackSet(g_adc0.p_ctrl, analog_validation_callback, NULL, NULL);
    if (FSP_SUCCESS != error) { goto finish; }
    error = R_ADC_B_ScanCfg(g_adc0.p_ctrl, &g_adc0_scan_cfg);
    if (FSP_SUCCESS != error) { goto finish; }
    g_validation_analog.phase = 2U;
    error = R_ADC_B_Calibrate(g_adc0.p_ctrl, NULL);
    if (FSP_SUCCESS != error) { goto finish; }
    error = analog_wait(true, 0U);
    if (FSP_SUCCESS != error) { goto finish; }
    g_validation_analog.phase = 3U;
    for (uint32_t pass = 0U; pass < ANALOG_PASSES; pass++)
    {
        for (uint32_t level = 0U; level < ANALOG_LEVELS; level++)
        {
            uint32_t sum = 0U;
            uint32_t ideal = (uint32_t) (((uint64_t) g_analog_levels[level] * 16U *
                                        g_validation_analog.reference_ratio_ppm + 500000U) / 1000000U);
            g_validation_analog.expected[level] = ideal;
            error = R_DAC_B_Write(g_dac0.p_ctrl, g_analog_levels[level]);
            if (FSP_SUCCESS != error) { goto finish; }
            analog_marker(1U, (level & 1U) ? BSP_IO_LEVEL_LOW : BSP_IO_LEVEL_HIGH);
            R_BSP_SoftwareDelay(g_validation_analog.settling_us, BSP_DELAY_UNITS_MICROSECONDS);
            for (uint32_t sample = 0U; sample < ANALOG_SAMPLES; sample++)
            {
                uint16_t value = 0U;
                uint32_t events = g_validation_analog.scan_events;
                error = R_ADC_B_ScanGroupStart(g_adc0.p_ctrl, ADC_GROUP_MASK_0);
                if (FSP_SUCCESS != error) { goto finish; }
                error = analog_wait(false, events);
                if (FSP_SUCCESS != error) { goto finish; }
                /* ADDR1 is the second word of ADDR0-22 (manual 54.2.13.1). */
                g_validation_analog.samples[pass][level][sample].raw =
                    *(volatile uint32_t const *) ((uintptr_t) &R_ADC_B->ADDR0 + sizeof(uint32_t));
                error = R_ADC_B_Read(g_adc0.p_ctrl, ADC_CHANNEL_1, &value);
                g_validation_analog.samples[pass][level][sample].value = value;
                g_validation_analog.samples[pass][level][sample].error = error;
                if (FSP_SUCCESS != error) { goto finish; }
                g_validation_analog.completed_samples++;
                sum += value;
                uint32_t difference = (value > ideal) ? value - ideal : ideal - value;
                uint32_t tolerance = g_validation_analog.absolute_tolerance_counts +
                    (uint32_t) (((uint64_t) ideal * g_validation_analog.tolerance_ppm) / 1000000U);
                if (difference > tolerance) { g_validation_analog.ratio_failures++; }
                analog_marker(2U, BSP_IO_LEVEL_HIGH);
                R_BSP_SoftwareDelay(10U, BSP_DELAY_UNITS_MICROSECONDS);
                analog_marker(2U, BSP_IO_LEVEL_LOW);
            }
            uint32_t mean = (sum + ANALOG_SAMPLES / 2U) / ANALOG_SAMPLES;
            g_validation_analog.mean[pass][level] = mean;
            g_validation_analog.measured_ratio_ppm[pass][level] =
                (uint32_t) (((uint64_t) mean * 1000000U) / (16U * g_analog_levels[level]));
            g_validation_analog.residual_counts[pass][level] = (int32_t) mean - (int32_t) ideal;
            if ((level != 0U) && (mean <= g_validation_analog.mean[pass][level - 1U]))
            {
                g_validation_analog.monotonic_failures++;
            }
            if (pass != 0U)
            {
                uint32_t previous = g_validation_analog.mean[0][level];
                uint32_t difference = mean > previous ? mean - previous : previous - mean;
                if (difference > g_validation_analog.repeatability_limit_counts)
                {
                    g_validation_analog.repeatability_failures++;
                }
            }
        }
        int32_t span = (int32_t) g_validation_analog.mean[pass][ANALOG_LEVELS - 1U] -
                       (int32_t) g_validation_analog.mean[pass][0];
        if (span > 0)
        {
            uint32_t gain = (uint32_t) (((uint64_t) span * 1000000U) /
                                       (16U * (g_analog_levels[ANALOG_LEVELS - 1U] - g_analog_levels[0])));
            g_validation_analog.fitted_gain_ppm[pass] = gain;
            g_validation_analog.fitted_offset_counts[pass] =
                (int32_t) g_validation_analog.mean[pass][0] -
                (int32_t) (((uint64_t) g_analog_levels[0] * 16U * gain) / 1000000U);
        }
    }
    if (g_validation_analog.ratio_failures || g_validation_analog.monotonic_failures ||
        g_validation_analog.repeatability_failures) { error = FSP_ERR_INVALID_DATA; }
finish:
    g_validation_analog.error = error;
    if (adc_open) { g_validation_analog.adc_close_error = R_ADC_B_Close(g_adc0.p_ctrl); }
    if (dac_open) { g_validation_analog.dac_close_error = R_DAC_B_Close(g_dac0.p_ctrl); }
    analog_marker(1U, BSP_IO_LEVEL_LOW);
    g_validation_analog.passed = (error == FSP_SUCCESS) && (g_validation_analog.io_error == FSP_SUCCESS) &&
        (g_validation_analog.adc_close_error == FSP_SUCCESS) && (g_validation_analog.dac_close_error == FSP_SUCCESS) &&
        (g_validation_analog.completed_samples == ANALOG_PASSES * ANALOG_LEVELS * ANALOG_SAMPLES);
    if (!g_validation_analog.passed) { analog_marker(3U, BSP_IO_LEVEL_HIGH); }
    g_validation_analog.complete = 1U;
    __DSB();
}
