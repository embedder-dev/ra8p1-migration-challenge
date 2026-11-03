/* CPU0 companion for the standalone CPU1 watchdog test. No marker ownership. */
#include "hal_data.h"

#if BSP_CFG_CPU_CORE != 0
 #error "The watchdog launcher must run on CPU0"
#endif

volatile fsp_err_t g_validation_launcher_error;

void hal_entry(void)
{
    /* Both images must be programmed and verified before a whole-system reset. */
    g_validation_launcher_error = R_BSP_SecondaryCoreStart(0x02080000UL);
    for (;;)
    {
        __WFI();
    }
}
