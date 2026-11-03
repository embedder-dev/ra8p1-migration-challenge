/* Independent CPU0/CPU1 qualification. Build this source once for each selected core. */
#include "hal_data.h"

#if (BSP_CFG_CPU_CORE != 0) && (BSP_CFG_CPU_CORE != 1)
 #error "Dual-core qualification requires CPU0 or CPU1"
#endif
#if BSP_CFG_DCACHE_ENABLED
 #error "Dual-core qualification requires the candidate's disabled data caches"
#endif

#define DUALCORE_MAGIC            (0x4455414CUL)
#define DUALCORE_VERSION          (1U)
#define DUALCORE_START_COMMAND    (0x53544152UL)
#define DUALCORE_CPU1_VECTOR      (0x02080000UL)
#define DUALCORE_CPU1_ROM_END     (0x020FF000UL)
#define DUALCORE_CPU1_RAM_START   (0x220C0000UL)
#define DUALCORE_CPU1_RAM_END     (0x22180000UL)
#define DUALCORE_SHARED_ADDRESS  (0x22180000UL)
#define DUALCORE_SHARED_END      (0x221A0000UL)
#define DUALCORE_TICK_HZ          (1000U)
#define DUALCORE_PROGRESS_TICKS   (10U)
#define DUALCORE_MARKER_TICKS     (100U)
#define DUALCORE_TIMEOUT_TICKS    (1000U)
/* An independent finite bound also catches a stopped SysTick/disabled IRQs. */
#define DUALCORE_STALL_POLLS      (10000000UL)

typedef enum e_dualcore_state
{
    DUALCORE_RESET = 0,
    DUALCORE_WAIT_COMMAND,
    DUALCORE_WAIT_PEER,
    DUALCORE_RUNNING,
    DUALCORE_FAILED
} dualcore_state_t;

typedef struct st_dualcore_shared
{
    /* One writer per field after CPU0 initializes this reservation before release. */
    uint32_t magic;
    uint32_t version;
    uint32_t request;
    uint32_t cpu0_progress;
    uint32_t cpu1_ready;
    uint32_t acknowledgement;
    uint32_t cpu1_progress;
    uint32_t cpu1_error;
} dualcore_shared_t;

typedef struct st_dualcore_result
{
    uint32_t magic;
    uint32_t core;
    uint32_t core_hz;
    uint32_t tick_hz;
    volatile uint32_t ticks;
    volatile uint32_t progress;
    volatile uint32_t state;
    volatile fsp_err_t error;
    volatile uint32_t start_attempts;
    volatile uint32_t cpu1_initial_sp;
    volatile uint32_t cpu1_reset_pc;
    volatile uint32_t confirmed_cpu1_progress;
    volatile uint32_t confirmations;
    volatile uint32_t marker_transitions;
} dualcore_result_t;

/* No ELF section occupies this excluded reservation, and C startup must not clear it. */
#ifndef QUALIFICATION_DUALCORE_SHARED
 #define QUALIFICATION_DUALCORE_SHARED ((volatile dualcore_shared_t *) DUALCORE_SHARED_ADDRESS)
#endif
#ifndef QUALIFICATION_DUALCORE_VECTOR
 #define QUALIFICATION_DUALCORE_VECTOR ((volatile const uint32_t *) DUALCORE_CPU1_VECTOR)
#endif

typedef char dualcore_shared_fits[(sizeof(dualcore_shared_t) <=
                                  DUALCORE_SHARED_END - DUALCORE_SHARED_ADDRESS) ? 1 : -1];

volatile dualcore_result_t g_validation_dualcore;
/* CPU0 only consumes this debugger-written command; CPU1 never reads it. */
volatile uint32_t g_validation_dualcore_command;

static uint32_t g_last_progress_tick;
static uint32_t g_last_tick;
static uint32_t g_stall_polls;
static uint32_t g_deadline_tick;
#if BSP_CFG_CPU_CORE == 0
static uint32_t g_last_marker_tick;
static uint32_t g_pending_request;
static uint32_t g_request_progress;
static bsp_io_level_t g_marker_level;
static bsp_io_level_t g_peer_level;
static bool g_pins_ready;
#endif

void SysTick_Handler(void);
void R_BSP_WarmStart(bsp_warm_start_event_t event);

void SysTick_Handler(void)
{
    g_validation_dualcore.ticks++;
}

void R_BSP_WarmStart(bsp_warm_start_event_t event)
{
    /* In particular, CPU1 must not open IOPORT or reconfigure shared pins. */
    (void) event;
}

static void dualcore_fail(fsp_err_t error)
{
    g_validation_dualcore.error = error;
    g_validation_dualcore.state = DUALCORE_FAILED;
#if BSP_CFG_CPU_CORE == 0
    if (g_pins_ready)
    {
        (void) R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, BSP_IO_PORT_08_PIN_07, BSP_IO_LEVEL_HIGH);
    }
#else
    QUALIFICATION_DUALCORE_SHARED->cpu1_error = (uint32_t) error;
    __DMB();
#endif
}

static bool dualcore_initialize(void)
{
    SystemCoreClockUpdate(); /* Reads clock registers; does not change shared clocks. */
    g_validation_dualcore.magic = DUALCORE_MAGIC;
    g_validation_dualcore.core = BSP_CFG_CPU_CORE;
    g_validation_dualcore.core_hz = SystemCoreClock;
    g_validation_dualcore.tick_hz = DUALCORE_TICK_HZ;
#if BSP_CFG_CPU_CORE == 0
    static const bsp_io_port_pin_t pins[] =
    {
        BSP_IO_PORT_05_PIN_14, BSP_IO_PORT_01_PIN_11,
        BSP_IO_PORT_08_PIN_06, BSP_IO_PORT_08_PIN_07
    };
    fsp_err_t error = R_IOPORT_Open(&IOPORT_CFG_CTRL, &IOPORT_CFG_NAME);
    if (FSP_SUCCESS != error)
    {
        dualcore_fail(error);
        return false;
    }
    for (uint32_t index = 0U; index < sizeof(pins) / sizeof(pins[0]); index++)
    {
        error = R_IOPORT_PinCfg(&IOPORT_CFG_CTRL, pins[index],
                               IOPORT_CFG_PORT_DIRECTION_OUTPUT | IOPORT_CFG_PORT_OUTPUT_LOW);
        if (FSP_SUCCESS != error)
        {
            dualcore_fail(error);
            return false;
        }
    }
    g_pins_ready = true;
#endif
    if ((0U == SystemCoreClock) || (0U != SystemCoreClock % DUALCORE_TICK_HZ) ||
        (0U != SysTick_Config(SystemCoreClock / DUALCORE_TICK_HZ)))
    {
        dualcore_fail(FSP_ERR_INVALID_RATE);
        return false;
    }
#if BSP_CFG_CPU_CORE == 0
    g_validation_dualcore.state = DUALCORE_WAIT_COMMAND;
    error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, BSP_IO_PORT_05_PIN_14, BSP_IO_LEVEL_HIGH);
    if (FSP_SUCCESS != error)
    {
        dualcore_fail(error);
        return false;
    }
#else
    g_validation_dualcore.state = DUALCORE_WAIT_PEER;
#endif
    return true;
}

#if BSP_CFG_CPU_CORE == 0
static void dualcore_start(void)
{
    g_validation_dualcore_command = 0U;
    __DMB();
    volatile const uint32_t * vectors = QUALIFICATION_DUALCORE_VECTOR;
    uint32_t sp = vectors[0];
    uint32_t pc = vectors[1];
    g_validation_dualcore.cpu1_initial_sp = sp;
    g_validation_dualcore.cpu1_reset_pc = pc;
    /* The BSP validates VTOR/activation; these additional bounds enforce OUR image partitions. */
    if ((sp <= DUALCORE_CPU1_RAM_START) || (sp > DUALCORE_CPU1_RAM_END) || (sp & 7U) ||
        !(pc & 1U) || ((pc & ~1UL) < DUALCORE_CPU1_VECTOR + 4U * BSP_CORTEX_VECTOR_TABLE_ENTRIES) ||
        ((pc & ~1UL) >= DUALCORE_CPU1_ROM_END))
    {
        dualcore_fail(FSP_ERR_INVALID_ARGUMENT);
        return;
    }

    volatile dualcore_shared_t * shared = QUALIFICATION_DUALCORE_SHARED;
    shared->magic = 0U;
    shared->version = DUALCORE_VERSION;
    shared->request = 1U;
    shared->cpu0_progress = g_validation_dualcore.progress;
    shared->cpu1_ready = 0U;
    shared->acknowledgement = 0U;
    shared->cpu1_progress = 0U;
    shared->cpu1_error = 0U;
    __DMB();
    shared->magic = DUALCORE_MAGIC;
    __DSB();
    g_pending_request = 1U;
    g_request_progress = 0U;
    g_deadline_tick = g_validation_dualcore.ticks;
    g_validation_dualcore.state = DUALCORE_WAIT_PEER;
    g_validation_dualcore.start_attempts++;
    fsp_err_t error = R_BSP_SecondaryCoreStart(DUALCORE_CPU1_VECTOR);
    if (FSP_SUCCESS != error)
    {
        dualcore_fail(error);
    }
}
#endif

static void dualcore_step(void)
{
    if (DUALCORE_FAILED == g_validation_dualcore.state)
    {
        return;
    }
    uint32_t now = g_validation_dualcore.ticks;
    if (now == g_last_tick)
    {
        if (++g_stall_polls >= DUALCORE_STALL_POLLS)
        {
            dualcore_fail(FSP_ERR_TIMEOUT);
            return;
        }
    }
    else
    {
        g_last_tick = now;
        g_stall_polls = 0U;
    }
    bool progressed = (uint32_t) (now - g_last_progress_tick) >= DUALCORE_PROGRESS_TICKS;
    if (progressed)
    {
        g_last_progress_tick = now;
        g_validation_dualcore.progress++;
    }
    volatile dualcore_shared_t * shared = QUALIFICATION_DUALCORE_SHARED;
#if BSP_CFG_CPU_CORE == 0
    if ((uint32_t) (now - g_last_marker_tick) >= DUALCORE_MARKER_TICKS)
    {
        g_last_marker_tick = now;
        g_marker_level = (BSP_IO_LEVEL_LOW == g_marker_level) ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW;
        fsp_err_t error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, BSP_IO_PORT_01_PIN_11, g_marker_level);
        if (FSP_SUCCESS != error)
        {
            dualcore_fail(error);
            return;
        }
        g_validation_dualcore.marker_transitions++;
    }
    if (DUALCORE_WAIT_COMMAND == g_validation_dualcore.state)
    {
        uint32_t command = g_validation_dualcore_command;
        if (DUALCORE_START_COMMAND == command)
        {
            dualcore_start();
        }
        else if (0U != command)
        {
            g_validation_dualcore_command = 0U;
            dualcore_fail(FSP_ERR_INVALID_ARGUMENT);
        }
        return;
    }
    shared->cpu0_progress = g_validation_dualcore.progress;
    __DMB();
    uint32_t ready = shared->cpu1_ready;
    uint32_t acknowledgement = shared->acknowledgement;
    __DMB();
    uint32_t peer_progress = shared->cpu1_progress;
    uint32_t peer_error = shared->cpu1_error;
    if (0U != peer_error)
    {
        dualcore_fail(FSP_ERR_ABORTED);
        return;
    }
    if ((DUALCORE_MAGIC == ready) && (acknowledgement == g_pending_request) &&
        (peer_progress != g_request_progress))
    {
        /* An ack alone is not progress: require a fresh independent CPU1 timer interval. */
        if (DUALCORE_WAIT_PEER == g_validation_dualcore.state)
        {
            g_peer_level = (BSP_IO_LEVEL_LOW == g_peer_level) ? BSP_IO_LEVEL_HIGH : BSP_IO_LEVEL_LOW;
            fsp_err_t error = R_IOPORT_PinWrite(&IOPORT_CFG_CTRL, BSP_IO_PORT_08_PIN_06, g_peer_level);
            if (FSP_SUCCESS != error)
            {
                dualcore_fail(error);
                return;
            }
            g_validation_dualcore.confirmed_cpu1_progress = peer_progress;
            g_validation_dualcore.confirmations++;
            g_validation_dualcore.state = DUALCORE_RUNNING;
        }
        if ((uint32_t) (now - g_deadline_tick) >= DUALCORE_MARKER_TICKS)
        {
            g_pending_request++;
            if (0U == g_pending_request)
            {
                g_pending_request = 1U;
            }
            g_request_progress = peer_progress;
            g_deadline_tick = now;
            g_validation_dualcore.state = DUALCORE_WAIT_PEER;
            __DMB();
            shared->request = g_pending_request;
        }
    }
    if ((uint32_t) (now - g_deadline_tick) >= DUALCORE_TIMEOUT_TICKS)
    {
        dualcore_fail(FSP_ERR_TIMEOUT);
    }
#else
    if (DUALCORE_WAIT_PEER == g_validation_dualcore.state)
    {
        uint32_t magic = shared->magic;
        __DMB();
        if ((DUALCORE_MAGIC != magic) || (DUALCORE_VERSION != shared->version) || (0U == shared->request))
        {
            if ((uint32_t) (now - g_deadline_tick) >= DUALCORE_TIMEOUT_TICKS)
            {
                dualcore_fail(FSP_ERR_TIMEOUT);
            }
            return;
        }
        shared->cpu1_error = 0U;
        __DMB();
        shared->cpu1_ready = DUALCORE_MAGIC;
        g_validation_dualcore.state = DUALCORE_RUNNING;
        g_deadline_tick = now;
    }
    if (progressed)
    {
        uint32_t request = shared->request;
        __DMB();
        shared->cpu1_progress = g_validation_dualcore.progress;
        __DMB();
        shared->acknowledgement = request;
        __DMB();
        if (request != g_validation_dualcore.confirmations)
        {
            g_validation_dualcore.confirmations = request;
            g_deadline_tick = now;
        }
    }
    if ((uint32_t) (now - g_deadline_tick) >= DUALCORE_TIMEOUT_TICKS)
    {
        dualcore_fail(FSP_ERR_TIMEOUT);
    }
#endif
}

void hal_entry(void)
{
    if (!dualcore_initialize())
    {
        return;
    }
    while (DUALCORE_FAILED != g_validation_dualcore.state)
    {
        dualcore_step();
        /* Deliberate polling: a stopped local interrupt must still hit the finite guard. */
        __NOP();
    }
}
