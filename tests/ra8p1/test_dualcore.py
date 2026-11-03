"""No EASE or hardware: two compiled fixture cores share a host mailbox.

Host scheduling models interleavings, not cache coherence or electrical timing.
Production checks compile the unchanged fixture against real generated headers.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/ra8p1/qualification_dualcore.c"
FSP = ROOT / "baseline/ra/fsp"

HOST_HEADER = r"""
#ifndef HOST_HAL_DATA_H
#define HOST_HAL_DATA_H
#include <stdbool.h>
#include <stdint.h>
#define BSP_CFG_DCACHE_ENABLED 0
#define BSP_CORTEX_VECTOR_TABLE_ENTRIES 16U
#define BSP_IO_PORT_05_PIN_14 0
#define BSP_IO_PORT_01_PIN_11 1
#define BSP_IO_PORT_08_PIN_06 2
#define BSP_IO_PORT_08_PIN_07 3
#define IOPORT_CFG_PORT_DIRECTION_OUTPUT 1U
#define IOPORT_CFG_PORT_OUTPUT_LOW 0U
#define IOPORT_CFG_CTRL g_mock_ctrl
#define IOPORT_CFG_NAME g_mock_cfg
typedef enum { FSP_SUCCESS, FSP_ERR_INVALID_ARGUMENT, FSP_ERR_TIMEOUT,
               FSP_ERR_INVALID_RATE, FSP_ERR_IN_USE, FSP_ERR_ABORTED } fsp_err_t;
typedef enum { BSP_IO_LEVEL_LOW, BSP_IO_LEVEL_HIGH } bsp_io_level_t;
typedef unsigned bsp_io_port_pin_t;
typedef unsigned bsp_warm_start_event_t;
extern int g_mock_ctrl, g_mock_cfg;
extern uint32_t SystemCoreClock;
struct st_dualcore_shared;
extern volatile struct st_dualcore_shared g_host_shared;
extern uint32_t g_vectors[2];
#define QUALIFICATION_DUALCORE_SHARED (&g_host_shared)
#define QUALIFICATION_DUALCORE_VECTOR (g_vectors)
void SystemCoreClockUpdate(void);
uint32_t SysTick_Config(uint32_t ticks);
fsp_err_t R_IOPORT_Open(int * ctrl, const int * cfg);
fsp_err_t R_IOPORT_PinCfg(int * ctrl, bsp_io_port_pin_t pin, uint32_t cfg);
fsp_err_t R_IOPORT_PinWrite(int * ctrl, bsp_io_port_pin_t pin, bsp_io_level_t level);
fsp_err_t R_BSP_SecondaryCoreStart(uint32_t vector);
void mock_barrier(void);
#define __DMB() mock_barrier()
#define __DSB() mock_barrier()
#define __NOP() ((void) 0)
#endif
"""

WRAPPER = r"""
#include <string.h>
#define g_validation_dualcore CORE_SYMBOL(result)
#define g_validation_dualcore_command CORE_SYMBOL(command_value)
#define hal_entry CORE_SYMBOL(hal_entry)
#define SysTick_Handler CORE_SYMBOL(tick)
#define R_BSP_WarmStart CORE_SYMBOL(warm_start)
#include "qualification_dualcore.c"
void CORE_SYMBOL(reset)(void)
{
    memset((void *) &g_validation_dualcore, 0, sizeof(g_validation_dualcore));
    g_validation_dualcore_command = 0;
    g_last_progress_tick = g_last_tick = g_stall_polls = g_deadline_tick = 0;
#if BSP_CFG_CPU_CORE == 0
    g_last_marker_tick = g_pending_request = g_request_progress = 0;
    g_marker_level = g_peer_level = BSP_IO_LEVEL_LOW;
    g_pins_ready = false;
#endif
}
int CORE_SYMBOL(init)(void) { return dualcore_initialize(); }
void CORE_SYMBOL(step)(void) { dualcore_step(); }
void CORE_SYMBOL(command)(uint32_t value) { g_validation_dualcore_command = value; }
uint32_t CORE_SYMBOL(state)(void) { return g_validation_dualcore.state; }
uint32_t CORE_SYMBOL(error)(void) { return (uint32_t) g_validation_dualcore.error; }
uint32_t CORE_SYMBOL(progress)(void) { return g_validation_dualcore.progress; }
uint32_t CORE_SYMBOL(confirmations)(void) { return g_validation_dualcore.confirmations; }
uint32_t CORE_SYMBOL(command_read)(void) { return g_validation_dualcore_command; }
void CORE_SYMBOL(stall)(void) { g_stall_polls = DUALCORE_STALL_POLLS - 1; }
void CORE_SYMBOL(wrap)(void)
{
    g_validation_dualcore.ticks = UINT32_MAX - 50;
    g_last_tick = g_last_progress_tick = g_deadline_tick = UINT32_MAX - 50;
#if BSP_CFG_CPU_CORE == 0
    g_last_marker_tick = UINT32_MAX - 50;
#endif
}
"""

HARNESS = r"""
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "hal_data.h"
struct st_dualcore_shared {
    uint32_t magic, version, request, cpu0_progress;
    uint32_t cpu1_ready, acknowledgement, cpu1_progress, cpu1_error;
};
volatile struct st_dualcore_shared g_host_shared;
uint32_t g_vectors[2], SystemCoreClock = 8000000U;
int g_mock_ctrl, g_mock_cfg;
static unsigned current_core, starts, io_calls[2], barriers, pin_edges[4], systick_calls[2];
static bsp_io_level_t levels[4];
static fsp_err_t start_error, io_error;
static uint32_t systick_error;
#define DECLARE(N) \
void cpu##N##_reset(void); int cpu##N##_init(void); void cpu##N##_step(void); \
void cpu##N##_tick(void); void cpu##N##_warm_start(unsigned event); \
void cpu##N##_command(uint32_t value); uint32_t cpu##N##_command_read(void); \
uint32_t cpu##N##_state(void); uint32_t cpu##N##_error(void); \
uint32_t cpu##N##_progress(void); uint32_t cpu##N##_confirmations(void); \
void cpu##N##_stall(void); void cpu##N##_wrap(void)
DECLARE(0); DECLARE(1);
void mock_barrier(void) { barriers++; }
void SystemCoreClockUpdate(void) {}
uint32_t SysTick_Config(uint32_t ticks)
{
    assert(ticks == 8000U); systick_calls[current_core]++; return systick_error;
}
fsp_err_t R_IOPORT_Open(int * ctrl, const int * cfg)
{ assert(ctrl && cfg); io_calls[current_core]++; return io_error; }
fsp_err_t R_IOPORT_PinCfg(int * ctrl, bsp_io_port_pin_t pin, uint32_t cfg)
{ assert(ctrl && pin < 4 && cfg == 1U); io_calls[current_core]++; return io_error; }
fsp_err_t R_IOPORT_PinWrite(int * ctrl, bsp_io_port_pin_t pin, bsp_io_level_t level)
{
    assert(ctrl && pin < 4); io_calls[current_core]++;
    if (FSP_SUCCESS == io_error) {
        if (levels[pin] != level) { pin_edges[pin]++; }
        levels[pin] = level;
    }
    return io_error;
}
fsp_err_t R_BSP_SecondaryCoreStart(uint32_t vector)
{
    assert(current_core == 0 && vector == 0x02080000U);
    assert(g_host_shared.magic == 0x4455414CU && g_host_shared.request == 1);
    assert(g_host_shared.acknowledgement == 0 && g_host_shared.cpu1_progress == 0);
    assert(barriers > 0);
    starts++;
    return start_error;
}
static void reset(void)
{
    cpu0_reset(); cpu1_reset(); memset((void *) &g_host_shared, 0, sizeof(g_host_shared));
    memset(io_calls, 0, sizeof(io_calls)); memset(pin_edges, 0, sizeof(pin_edges));
    memset(systick_calls, 0, sizeof(systick_calls)); memset(levels, 0, sizeof(levels));
    starts = barriers = 0; current_core = 0; start_error = io_error = FSP_SUCCESS;
    systick_error = 0; SystemCoreClock = 8000000U;
    g_vectors[0] = 0x220C4000U; g_vectors[1] = 0x02081001U;
}
static void step0(void) { current_core = 0; cpu0_step(); }
static void step1(void) { current_core = 1; cpu1_step(); }
static void start(void)
{
    current_core = 0; assert(cpu0_init()); cpu0_command(0x53544152U); step0();
    assert(starts == 1 && cpu0_state() == 2 && cpu0_command_read() == 0);
    current_core = 1; assert(cpu1_init()); step1();
}
static void advance(unsigned ticks, int run0, int run1)
{
    for (unsigned i = 0; i < ticks; i++) {
        if (run0) { cpu0_tick(); step0(); }
        if (run1) { cpu1_tick(); step1(); }
    }
}
int main(void)
{
    /* CPU0 alone stays healthy, emits only CH0/CH1, and never touches the mailbox. */
    reset(); memset((void *) &g_host_shared, 0xA5, sizeof(g_host_shared));
    current_core = 0; cpu0_warm_start(0); assert(cpu0_init()); advance(1500, 1, 0);
    assert(cpu0_state() == 1 && starts == 0 && g_host_shared.magic == 0xA5A5A5A5U);
    assert(levels[0] == 1 && pin_edges[1] == 15 && pin_edges[2] == 0 && levels[3] == 0);
    cpu0_command(1); step0(); assert(cpu0_state() == 4 && starts == 0 && levels[3] == 1);

    /* Rejected vectors never reach the BSP, including blank and non-Thumb images. */
    const uint32_t invalid[][2] = {
        {0xFFFFFFFFU, 0xFFFFFFFFU}, {0U, 0U}, {0x220C0000U, 0x02081001U},
        {0x22180008U, 0x02081001U}, {0x220C4004U, 0x02081001U},
        {0x220C4000U, 0x02081000U}, {0x220C4000U, 0x02000101U},
        {0x220C4000U, 0x02080001U}, {0x220C4000U, 0x020FF001U}
    };
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        reset(); assert(cpu0_init()); memcpy(g_vectors, invalid[i], sizeof(g_vectors));
        cpu0_command(0x53544152U); step0();
        assert(cpu0_error() == FSP_ERR_INVALID_ARGUMENT && starts == 0 && levels[3] == 1);
    }
    reset(); assert(cpu0_init()); g_vectors[0] = 0x22180000U;
    memset((void *) &g_host_shared, 0xA5, sizeof(g_host_shared));
    cpu0_command(0x53544152U); step0(); assert(starts == 1); /* Top-of-stack is exclusive. */
    reset(); assert(cpu0_init()); g_vectors[1] = 0x02080041U;
    cpu0_command(0x53544152U); step0(); assert(starts == 1); /* No IRQ table in a bare project. */

    /* BSP activation failures are latched, not mistaken for application startup. */
    for (unsigned e = FSP_ERR_TIMEOUT; e <= FSP_ERR_IN_USE; e += 2) {
        reset(); assert(cpu0_init()); start_error = (fsp_err_t) e;
        cpu0_command(0x53544152U); step0();
        assert(cpu0_state() == 4 && cpu0_error() == e && levels[3] == 1);
    }
    reset(); assert(cpu0_init()); io_error = FSP_ERR_ABORTED; advance(100, 1, 0);
    assert(cpu0_error() == FSP_ERR_ABORTED && starts == 0);
    reset(); systick_error = 1; assert(!cpu0_init()); assert(levels[3] == 1);

    /* A missing peer, stale ack, or ack without timer progress cannot light CH2. */
    reset(); assert(cpu0_init()); cpu0_command(0x53544152U); step0();
    g_host_shared.cpu1_ready = 0x4455414CU; g_host_shared.acknowledgement = 1;
    advance(1000, 1, 0); assert(cpu0_error() == FSP_ERR_TIMEOUT && pin_edges[2] == 0);
    reset(); assert(cpu0_init()); cpu0_command(0x53544152U); step0();
    g_host_shared.cpu1_ready = 0x4455414CU; g_host_shared.acknowledgement = 99;
    g_host_shared.cpu1_progress = 1;
    advance(1000, 1, 0); assert(cpu0_error() == FSP_ERR_TIMEOUT && pin_edges[2] == 0);

    /* Both images execute the fixture protocol with independent tick schedules. */
    reset(); start(); advance(650, 1, 1);
    assert(cpu0_progress() == 65 && cpu1_progress() == 65);
    assert(cpu0_confirmations() >= 6 && pin_edges[2] == cpu0_confirmations());
    assert(pin_edges[1] == 6 && levels[3] == 0 && io_calls[1] == 0);
    assert(systick_calls[0] == 1 && systick_calls[1] == 1);
    cpu0_command(0x53544152U); advance(20, 1, 1); assert(starts == 1); /* No second release. */
    unsigned edges = pin_edges[2]; advance(1100, 1, 0);
    assert(cpu0_error() == FSP_ERR_TIMEOUT && levels[3] == 1 && pin_edges[2] == edges);

    reset(); start(); advance(250, 1, 1); advance(1100, 0, 1);
    assert(cpu1_error() == FSP_ERR_TIMEOUT && g_host_shared.cpu1_error == FSP_ERR_TIMEOUT);
    step0(); assert(cpu0_error() == FSP_ERR_ABORTED && levels[3] == 1);
    reset(); current_core = 1; assert(cpu1_init()); advance(1000, 0, 1);
    assert(cpu1_error() == FSP_ERR_TIMEOUT && io_calls[1] == 0);
    reset(); start(); cpu0_stall(); step0(); assert(cpu0_error() == FSP_ERR_TIMEOUT);
    reset(); start(); cpu1_stall(); step1(); assert(cpu1_error() == FSP_ERR_TIMEOUT);

    reset(); start();
    for (unsigned i = 0; i < 650; i++) {
        cpu0_tick(); step0();
        if (i % 2U == 0U) { cpu1_tick(); step1(); }
    }
    assert(cpu0_progress() == 65 && cpu1_progress() == 32);
    assert(cpu0_state() != 4 && cpu1_state() != 4 && cpu0_confirmations() >= 6);
    reset(); start(); cpu0_wrap(); cpu1_wrap(); advance(550, 1, 1);
    assert(cpu0_state() != 4 && cpu1_state() != 4 && cpu0_confirmations() >= 5);
    assert(levels[3] == 0 && io_calls[1] == 0);
    puts("dual-core host protocol and failure cases passed");
    return 0;
}
"""


class DualcoreTests(unittest.TestCase):
    def run_checked(self, args, **kwargs):
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_host_two_core_protocol(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix="dualcore-host-") as temp:
            temp = Path(temp)
            (temp / "hal_data.h").write_text(HOST_HEADER)
            (temp / "harness.c").write_text(HARNESS)
            for opt in ("-O0", "-O2"):
                objects = []
                flags = [compiler, "-std=c11", opt, "-Wall", "-Wextra", "-Werror", "-Wconversion",
                         "-Wshadow", "-fsanitize=undefined", "-fno-sanitize-recover=all",
                         "-I", str(temp), "-I", str(SOURCE.parent)]
                for core in (0, 1):
                    wrapper = temp / f"core{core}.c"
                    wrapper.write_text(f"#define CORE_SYMBOL(x) cpu{core}_##x\n" + WRAPPER)
                    obj = temp / f"core{core}.o"
                    self.run_checked(flags + [f"-DBSP_CFG_CPU_CORE={core}", "-c", str(wrapper), "-o", str(obj)])
                    objects.append(str(obj))
                executable = temp / "test-dualcore"
                self.run_checked(flags + [str(temp / "harness.c"), *objects, "-o", str(executable)])
                self.assertIn("dual-core host protocol and failure cases passed", self.run_checked([str(executable)]))

    def test_spec_and_reserved_partition(self):
        spec = json.loads((SOURCE.parent / "dualcore.json").read_text())
        boot = json.loads((SOURCE.parent / "boot-moco.json").read_text())
        self.assertEqual(spec["source"], SOURCE.name)
        self.assertEqual(spec["bsp"], boot["bsp"])
        self.assertEqual(spec["pins"], boot["pins"])
        pinmap = ET.parse(ROOT / "packaging/ra8p1/mcu/.mcu/.pinmapping/PinCfgR7KA8P1KFLCAC.xml")
        for pin in spec["pins"]:
            component = pinmap.find(f".//component[@id='{pin['component']}']")
            self.assertIsNotNone(component)
            for key, value in {**pin["configs"], **pin["connections"]}.items():
                self.assertIsNotNone(component.find(f".//configuration[@id='{key}']/alt[@id='{value}']"))
        source = SOURCE.read_text()
        constants = {key: int(value, 16) for key, value in re.findall(
            r"#define\s+(DUALCORE_\w+)\s+\((0x[0-9A-F]+)UL\)", source)}
        linker = (ROOT / "packaging/ra8p1/mcu/script/fsp.ld").read_text()
        self.assertRegex(linker, r"FLASH_START = .*\? 0x02000000 : 0x02080000;")
        self.assertRegex(linker, r"FLASH_LENGTH = .*\? 0x00080000 : 0x0007F000;")
        self.assertRegex(linker, r"RAM_START = .*\? 0x22000000 : 0x220C0000;")
        self.assertIn("RAM_LENGTH = 0x000C0000;", linker)
        self.assertEqual(constants["DUALCORE_CPU1_VECTOR"], 0x02080000)
        self.assertEqual(constants["DUALCORE_CPU1_ROM_END"], 0x02080000 + 0x7F000)
        self.assertEqual(constants["DUALCORE_CPU1_RAM_START"], 0x220C0000)
        self.assertEqual(constants["DUALCORE_CPU1_RAM_END"], 0x220C0000 + 0xC0000)
        self.assertEqual(constants["DUALCORE_SHARED_ADDRESS"], constants["DUALCORE_CPU1_RAM_END"])
        self.assertEqual(constants["DUALCORE_SHARED_END"], 0x221A0000)
        metadata = (ROOT / "packaging/ra8p1/mcu/README.md").read_text()
        self.assertIn("[0x22000000, 0x221A0000)", metadata)
        for word in ("malloc(", "calloc(", "realloc(", "free(", "R_CGC_"):
            self.assertNotIn(word, source)

    def test_production_headers_both_cores(self):
        compilers = sorted(Path("/Applications").glob("**/arm-gnu-toolchain-13.2*/bin/arm-none-eabi-gcc"))
        if not compilers:
            self.skipTest("Installed Arm GNU toolchain required for production-header compile")
        compiler = compilers[0]
        projects = {
            0: ROOT / "validation/projects/candidate_ra8p1_boot0_v9",
            1: ROOT / "validation/projects/candidate_ra8p1_core1_v1",
        }
        with tempfile.TemporaryDirectory(prefix="dualcore-arm-") as temp:
            temp = Path(temp)
            for core, project in projects.items():
                self.assertTrue((project / "ra_gen/hal_data.h").exists(), str(project))
                paths = [project / "ra_cfg/fsp_cfg/bsp", project / "ra_gen", project / "ra_cfg/fsp_cfg",
                         FSP / "inc", FSP / "inc/api", FSP / "inc/instances",
                         project / "ra/arm/CMSIS_6/CMSIS/Core/Include"]
                for opt in ("-O0", "-O2"):
                    obj = temp / f"cpu{core}{opt}.o"
                    args = [str(compiler), "-std=c11", opt, "-mthumb", "-mfloat-abi=hard",
                            "-mcpu=" + ("cortex-m85+nopacbti" if core == 0 else "cortex-m33"),
                            "-mfpu=" + ("fpv5-d16" if core == 0 else "fpv5-sp-d16"),
                            "-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wshadow",
                            "-Wmissing-declarations", "-Wpointer-arith", "-D_RENESAS_RA_",
                            "-D_RA_CORE=" + ("CM85" if core == 0 else "CM33"), "-D_RA_ORDINAL=1"]
                    args += [a for path in paths for a in ("-I", str(path))]
                    self.run_checked(args + ["-c", str(SOURCE), "-o", str(obj)])
                    symbols = self.run_checked([str(compiler.with_name("arm-none-eabi-nm")), "-u", str(obj)])
                    if core == 0:
                        self.assertIn("R_BSP_SecondaryCoreStart", symbols)
                        self.assertIn("R_IOPORT_PinWrite", symbols)
                    else:
                        self.assertNotIn("R_BSP_SecondaryCoreStart", symbols)
                        self.assertNotIn("R_IOPORT_", symbols)
                    self.assertNotRegex(symbols, r"\b(malloc|calloc|realloc|free)\b")


if __name__ == "__main__":
    unittest.main()
