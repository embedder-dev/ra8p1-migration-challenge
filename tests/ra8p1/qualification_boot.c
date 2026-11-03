/* Qualification fixture for the custom FSP 5.9-derived EK-RA8P1 candidate.
 * This is a clock/startup test, not the application's peripheral qualification. */
#include "hal_data.h"

#define VALIDATION_MAGIC        (0x52413850UL)
#define VALIDATION_TICK_HZ      (1000U)
#define VALIDATION_MARKER_TICKS (100U)

typedef struct st_validation_boot_result
{
    uint32_t magic;
    uint32_t core;
    uint32_t core_hz;
    uint32_t tick_hz;
    volatile uint32_t ticks;
    volatile uint32_t transitions;
    volatile fsp_err_t error;
} validation_boot_result_t;

volatile validation_boot_result_t g_validation_boot;

void SysTick_Handler(void);
void R_BSP_WarmStart(bsp_warm_start_event_t event);

void SysTick_Handler(void)
{
    g_validation_boot.ticks++;
}

void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    if (BSP_WARM_START_POST_C == event)
    {
        g_validation_boot.error = R_IOPORT_Open(&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);
    }
}

void hal_entry(void)
{
    SystemCoreClockUpdate();
    g_validation_boot.magic = VALIDATION_MAGIC;
    g_validation_boot.core = BSP_CFG_CPU_CORE;
    g_validation_boot.core_hz = SystemCoreClock;
    g_validation_boot.tick_hz = VALIDATION_TICK_HZ;
    if (FSP_SUCCESS != g_validation_boot.error)
    {
        return;
    }

#if BSP_CFG_CPU_CORE == 0
    static const bsp_io_port_pin_t pins[] =
    {
        BSP_IO_PORT_05_PIN_14, /* Saleae CH0: boot complete. */
        BSP_IO_PORT_01_PIN_11, /* Saleae CH1: 100 ms transitions. */
        BSP_IO_PORT_08_PIN_06, /* Saleae CH2: complementary reference. */
        BSP_IO_PORT_08_PIN_07  /* Saleae CH3: failure latch. */
    };
    for (uint32_t index = 0U; index < sizeof(pins) / sizeof(pins[0]); index++)
    {
        fsp_err_t error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, pins[index],
                                         IOPORT_CFG_PORT_DIRECTION_OUTPUT | IOPORT_CFG_PORT_OUTPUT_LOW);
        if (FSP_SUCCESS != error)
        {
            g_validation_boot.error = error;
            return;
        }
    }
    g_validation_boot.error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[0], BSP_IO_LEVEL_HIGH);
    if (FSP_SUCCESS != g_validation_boot.error)
    {
        return;
    }
#endif

    if ((0U != SystemCoreClock % VALIDATION_TICK_HZ) ||
        (0U != SysTick_Config(SystemCoreClock / VALIDATION_TICK_HZ)))
    {
        g_validation_boot.error = FSP_ERR_INVALID_RATE;
#if BSP_CFG_CPU_CORE == 0
        (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[3], BSP_IO_LEVEL_HIGH);
#endif
        return;
    }

    uint32_t previous = g_validation_boot.ticks;
    bsp_io_level_t level = BSP_IO_LEVEL_LOW;
    while (true)
    {
        if ((uint32_t) (g_validation_boot.ticks - previous) >= VALIDATION_MARKER_TICKS)
        {
            previous += VALIDATION_MARKER_TICKS;
            level = (BSP_IO_LEVEL_LOW == level) ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW;
#if BSP_CFG_CPU_CORE == 0
            fsp_err_t error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[1], level);
            if (FSP_SUCCESS == error)
            {
                error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[2],
                                         BSP_IO_LEVEL_LOW == level ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW);
            }
            if (FSP_SUCCESS != error)
            {
                g_validation_boot.error = error;
                (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, pins[3], BSP_IO_LEVEL_HIGH);
                return;
            }
#endif
            g_validation_boot.transitions++;
        }
        __WFI();
    }
}
