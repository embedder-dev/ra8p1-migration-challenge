"""RA8P1 BSP contract checks, using only the preserved FSP 5.9 CMSIS core."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FSP = ROOT / 'baseline/ra/fsp'
FAMILY = FSP / 'src/bsp/mcu/ra8p1'
MDF = ROOT / 'baseline/module_descriptions'


def run(*args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def arm_gcc_candidates():
    override = os.environ.get('ARM_NONE_EABI_GCC')
    if override:
        return [override]
    # Prefer the toolchain used by e2 studio, including its bundled newlib headers.
    bundled = sorted(Path('/Applications').glob('Renesas*/toolchains/*/bin/arm-none-eabi-gcc'))
    bundled.sort(key=lambda path: ('13.2' not in str(path), str(path)))
    standalone = sorted(Path('/Applications/ArmGNUToolchain').glob(
        '*/arm-none-eabi/bin/arm-none-eabi-gcc'), reverse=True)
    paths = [str(path) for path in bundled + standalone]
    on_path = shutil.which('arm-none-eabi-gcc')
    if on_path:
        paths.append(on_path)
    return list(dict.fromkeys(paths))


def select_arm_gcc(candidates):
    failures = []
    for compiler in candidates:
        for cpu, fpu in (('cortex-m85', 'fpv5-d16'), ('cortex-m33', 'fpv5-sp-d16')):
            try:
                result = run(compiler, '-mthumb', '-mcpu=' + cpu, '-mfloat-abi=hard', '-mfpu=' + fpu,
                             '-fsyntax-only', '-xc', '-include', 'assert.h', '-include', 'string.h',
                             os.devnull)
            except OSError as error:
                failures.append(f'{compiler}: {error}')
                break
            if result.returncode:
                failures.append(f'{compiler} ({cpu}): {result.stdout.strip()}')
                break
        else:
            return compiler
    raise RuntimeError('No complete ARM GCC installation supports both cores and libc headers. '
                       'Set ARM_NONE_EABI_GCC to the full e2 studio toolchain compiler.\n' +
                       '\n'.join(failures))


class BspTests(unittest.TestCase):
    def test_compiler_selection_rejects_incomplete_toolchains(self):
        def probe(*args):
            self.assertIn('assert.h', args)
            self.assertIn('string.h', args)
            incomplete = args[0] == 'incomplete' and '-mcpu=cortex-m33' in args
            return subprocess.CompletedProcess(args, int(incomplete),
                                               'fatal error: string.h: No such file' if incomplete else '')

        with patch(__name__ + '.run', side_effect=probe) as mocked:
            self.assertEqual(select_arm_gcc(['incomplete', 'complete']), 'complete')
            self.assertEqual(mocked.call_count, 4)
            with self.assertRaisesRegex(RuntimeError, 'fatal error: string.h'):
                select_arm_gcc(['incomplete'])

    def test_compiler_override_is_not_silently_replaced(self):
        with patch.dict(os.environ, {'ARM_NONE_EABI_GCC': '/explicit/compiler'}):
            self.assertEqual(arm_gcc_candidates(), ['/explicit/compiler'])

    def test_reproducible_device_header(self):
        result = run('python3', str(ROOT / 'tools/generate_ra8p1_device.py'), '--check')
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_metadata_references(self):
        files = list(MDF.glob('*ra8p1*.xml'))
        self.assertGreaterEqual(len(files), 5)
        docs = [ET.parse(path).getroot() for path in files]
        ids = {item.get('id') for doc in docs for item in doc.iter() if item.get('id')}
        for doc in docs:
            for select in doc.iter('select'):
                self.assertTrue(select.get('bsp') == 'bsp.ra' or select.get('bsp') in ids)
            for node in doc.iter('bsp'):
                self.assertIn(node.get('config'), ids)

        devices = {node.get('id'): node for doc in docs for node in doc.findall('device')}
        bsps = {node.get('id'): node for doc in docs for node in doc.findall('bsp')}
        configs = {node.get('id'): node for doc in docs for node in doc.findall('config')}
        boards = {node.get('id'): node for doc in docs for node in doc.findall('board')}
        self.assertNotIn('config.bsp.ra8p1.cpu', ids)
        self.assertFalse(any(node.get('interface', '').startswith('interface.mcu.core.')
                             for node in bsps['bsp.ra8p1'].findall('provides')))
        self.assertEqual(boards['board.ra8p1ek'].get('device'), 'R7KA8P1KFLCAC')
        for core, target, interface in (
                (0, 'R7KA8P1KFLCAC', 'interface.mcu.core.cm85'),
                (1, 'R7KA8P1KFLCAC_CPU1', 'interface.mcu.core.cm33')):
            with self.subTest(core=core):
                core_name = 'CM85' if core == 0 else 'CM33'
                matches = [node for node in devices.values()
                           if node.get('device') == 'R7KA8P1KFLCAC' and node.get('core') == core_name]
                self.assertEqual([node.get('id') for node in matches], [target])
                selected = [bsps[node.get('bsp')] for node in matches[0].findall('select')
                            if node.get('bsp') != 'bsp.ra']
                cores = [node for node in selected if node.get('id').startswith('bsp.ra8p1.cpu')]
                self.assertEqual(len(cores), 1)
                self.assertEqual([p.get('interface') for p in cores[0].findall('provides')], [interface])
                config = configs[cores[0].get('config')]
                self.assertIn(f'#define BSP_CFG_CPU_CORE_FROM_DEVICE ({core})', config.findtext('content'))

    def test_board_profile_is_registered_and_preserves_swd(self):
        name = 'Renesas##BSP##Board##ra8p1_ek####5.9.0##configuration.xml'
        profile = ET.parse(MDF / name).getroot()
        self.assertEqual(profile.tag, 'raConfiguration')
        self.assertEqual(profile.get('version'), '4')
        properties = {p.get('id'): p.get('value') for p in profile.findall('./raBspConfiguration/config/property')}
        self.assertEqual(properties['config.bsp.common.main_osc_populated'],
                         'config.bsp.common.main_osc_populated.enabled')
        self.assertEqual(properties['config.bsp.common.main_osc_clock_source'],
                         'config.bsp.common.main_osc_clock_source.crystal')
        pins = profile.find('./raPinConfiguration/pincfg')
        self.assertIsNotNone(pins)
        self.assertEqual(pins.get('active'), 'true')
        self.assertEqual(pins.get('selected'), 'true')
        self.assertEqual(pins.get('symbol'), 'g_bsp_pin_cfg')
        settings = {p.get('configurationId'): p.get('altId') for p in pins.findall('configSetting')}
        self.assertEqual(settings['p210'], 'p210.jtag_fslash_swd.swdio')
        self.assertEqual(settings['p211'], 'p211.jtag_fslash_swd.swclk')
        self.assertEqual(settings['jtag_fslash_swd.mode'], 'jtag_fslash_swd.mode.swd.free')
        tooling = ET.parse(ROOT / 'packaging/ra8p1/board/toolingSupport.xml').getroot()
        self.assertIn('.module_descriptions/' + name,
                      [(entry.text or '').strip() for entry in tooling.findall('file')])

    def test_clock_ratio_boundaries(self):
        compiler = shutil.which('cc')
        self.assertIsNotNone(compiler, 'A host C compiler is required')
        source = (FAMILY / 'bsp_clocks_ra8p1.inc').read_text()
        divisor = source[source.index('static uint32_t bsp_ra8p1_divisor'):source.index('uint32_t R_BSP_SourceClockHzGet')]
        validate_start = source.index('fsp_err_t bsp_ra8p1_clock_validate')
        validate = source[validate_start:source.index('#if BSP_CFG_CPU_CORE == 0', validate_start)]
        with tempfile.TemporaryDirectory(prefix='ra8p1-ratios-') as temp:
            temp = Path(temp)
            c_file = temp / 'ratios.c'
            # The pure validator needs FSP's real error enum, not board/CMSIS definitions.
            (temp / 'bsp_api.h').write_text('/* No board dependencies in this host-only validator test. */\n')
            c_file.write_text('#include <stdbool.h>\n#include "fsp_common_api.h"\n' + divisor + validate)
            library = temp / 'ratios.dylib'
            result = run(compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                         '-I' + str(temp), '-I' + str(FSP / 'inc/api'), '-I' + str(FSP / 'inc'),
                         str(c_file), '-o', str(library))
            self.assertEqual(result.returncode, 0, result.stdout)
            function = ctypes.CDLL(str(library)).bsp_ra8p1_clock_validate
            function.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint16]
            function.restype = ctypes.c_int

            def check(hz, fields, cpu, expected):
                d1 = sum(value << (4 * i) for i, value in enumerate(fields))
                d2 = sum(value << (4 * i) for i, value in enumerate(cpu))
                self.assertEqual(function(hz, d1, d2), expected, (hz, fields, cpu))

            # Manual 9.2.2/3 limits: CPU1/MRI/ICLK 250 MHz, MR/PCLKA 125 MHz,
            # PCLKB 62.5 MHz; CPU and MRAM clocks are integer multiples of ICLK.
            check(8000000, [0] * 8, [0] * 4, 0)
            # Each stage of the initial 240 MHz profile remains ratio-valid.
            check(8000000, [2, 2, 3, 2, 2, 2, 2, 2], [0] * 4, 0)
            check(8000000, [2, 2, 3, 2, 2, 2, 2, 2], [0, 2, 2, 2], 0)
            check(240000000, [2, 2, 3, 2, 2, 2, 2, 2], [0, 2, 2, 2], 0)
            check(8000000, [2, 3, 4, 3, 3, 2, 2, 3], [0, 2, 2, 2], 0)
            check(600000000, [2, 3, 4, 3, 3, 2, 2, 3], [0, 2, 2, 2], 0)
            check(1000000000, [2, 3, 4, 3, 3, 2, 2, 3], [0, 2, 2, 2], 0)
            check(1000000001, [2, 3, 4, 3, 3, 2, 2, 3], [0, 2, 2, 2], 17)
            check(0, [0] * 8, [0] * 4, 17)
            check(8000000, [7] + [0] * 7, [0] * 4, 3)
            check(8000000, [8, 1] + [0] * 6, [0] * 4, 17)
            check(8000000, [0] * 8, [0, 0, 0, 1], 17)
            check(250000001, [0] * 8, [0] * 4, 17)
            check(62500000, [0] * 8, [0] * 4, 0)
            check(62500001, [0] * 8, [0] * 4, 17)

    def test_clock_readback_and_rejection(self):
        compiler = shutil.which('cc')
        self.assertIsNotNone(compiler, 'A host C compiler is required')
        common = (FSP / 'src/bsp/mcu/all/bsp_common.h').read_text()
        enums = common[common.index('typedef enum e_fsp_priv_clock'):common.index('typedef struct st_bsp_unique_id')]
        clock_header = (FSP / 'src/bsp/mcu/all/bsp_clocks.h').read_text()
        pll_enums = clock_header[clock_header.index('typedef enum e_cgc_pll_mul'):
                                 clock_header.index('} cgc_pll_mul_t;') + len('} cgc_pll_mul_t;')]
        common_c = (FSP / 'src/bsp/mcu/all/bsp_common.c').read_text()
        secondary_core = common_c[common_c.index('#if BSP_MCU_GROUP_RA8P1'):
                                  common_c.index('/*******************************************************************************************************************//**\n * Get the FSP version')]
        with tempfile.TemporaryDirectory(prefix='ra8p1-clock-mock-') as temp:
            temp = Path(temp)
            (temp / 'bsp_api.h').write_text('/* Host-only dependency boundary. */\n')
            (temp / 'bsp_clock_cfg.h').write_text('''
#define BSP_CFG_RA8P1_STARTUP_PROFILE 0
#define BSP_CFG_XTAL_HZ 24000000U
#define BSP_CFG_CLOCK_SOURCE 1U
#define BSP_CFG_CPUCLK_DIV 0U
#define BSP_CFG_CPU1CLK_DIV 0U
#define BSP_CFG_MRICLK_DIV 0U
#define BSP_CFG_MRPCLK_DIV 0U
#define BSP_CFG_ICLK_DIV 0U
#define BSP_CFG_PCLKA_DIV 0U
#define BSP_CFG_PCLKB_DIV 0U
#define BSP_CFG_PCLKC_DIV 0U
#define BSP_CFG_PCLKD_DIV 0U
#define BSP_CFG_PCLKE_DIV 0U
#define BSP_CFG_BCLK_DIV 0U
''')
            core_stub = '#define __IM volatile const\n#define __OM volatile\n#define __IOM volatile\n'
            (temp / 'core_cm85.h').write_text(core_stub)
            (temp / 'core_cm33.h').write_text(core_stub)
            (temp / 'r_cgc_cfg.h').write_text('/* Host CGC configuration. */\n')
            source = temp / 'clock.c'
            source.write_text('''
#include <stdbool.h>
#include <assert.h>
#include <string.h>
#include "fsp_common_api.h"
#include "R7KA8P1AD.h"
#include "bsp_clocks_ra8p1.h"
''' + enums + '''
static R_SYSC_Type system_regs;
static R_MRAM_Type mram_regs;
static uint32_t irq_mask;
static uint32_t nop_count;
#if BSP_CFG_CPU_CORE == 0
static R_CPU_CTRL_Type cpu_regs;
static R_MSTP_Type mstp_regs;
static R_SRAM_Type sram_regs;
#endif
#undef R_SYSTEM
#undef R_MRAM
#undef R_CPU_CTRL
#undef R_MSTP
#undef R_SRAM
#if BSP_CFG_CPU_CORE == 0
static bool main_osc_ready;
static bool pll_ready;
static bool observe_startup;
static bool reject_mram;
static bool reject_sram;
static uint8_t previous_source;
static uint32_t source_changes;
fsp_err_t bsp_ra8p1_clock_validate(uint32_t, uint32_t, uint16_t);
static void observe(void)
{
    if (!observe_startup) { return; }
    if (system_regs.MOSCCR == 0 && main_osc_ready) { *((uint8_t *) &system_regs.OSCSF) |= 8U; }
    if (system_regs.PLLCR == 0 && pll_ready) { *((uint8_t *) &system_regs.OSCSF) |= 32U; }
    if (system_regs.PLLCR != 0) { *((uint8_t *) &system_regs.OSCSF) &= ~32U; }
    if (reject_sram) { sram_regs.SRAMWTSC = 0; }
    if (reject_mram) { mram_regs.MRCFREQ = 8; mram_regs.MREFREQ = 8; }
    uint32_t hz = system_regs.SCKSCR == 5 ?
        ((system_regs.PLLCCR >> 8) == 49U ? 600000000U : 240000000U) :
        (system_regs.SCKSCR == 3 ? 24000000U : 8000000U);
    /* Check every observed intermediate register state, not just the final profile. */
    assert(bsp_ra8p1_clock_validate(hz, system_regs.SCKDIVCR, system_regs.SCKDIVCR2) == FSP_SUCCESS);
    assert(system_regs.VSCR == 2 && system_regs.OPCCR == 0);
    uint32_t mri_hz = hz / (1U << ((system_regs.SCKDIVCR2 >> 12) & 15U));
    uint32_t mrp_hz = hz / (1U << ((system_regs.SCKDIVCR >> 28) & 15U));
    assert((mram_regs.MRCFREQ & 1023U) >= (mri_hz + 999999U) / 1000000U);
    assert((mram_regs.MREFREQ & 255U) >= (mrp_hz + 999999U) / 1000000U);
    assert((mram_regs.MRCFREQ & 1023U) <= 5U * (mram_regs.MREFREQ & 255U));
    if (system_regs.SCKSCR != previous_source)
    {
        assert(irq_mask == 1);
        if (previous_source == 5)
        {
            assert(nop_count >= 18000);
            assert(system_regs.SCKDIVCR2 == 0x3331U);
            assert(system_regs.SCKDIVCR == 0x33333433U || system_regs.SCKDIVCR == 0x43344543U);
        }
        previous_source = system_regs.SCKSCR;
        nop_count = 0;
        ++source_changes;
    }
    if (system_regs.SCKSCR == 5)
    {
        assert((system_regs.OSCSF & 40U) == 40U);
        assert((mram_regs.MRCFREQ & 1023U) >= hz / 4 / 1000000);
        assert((mram_regs.MREFREQ & 255U) >= hz / (hz == 600000000 ? 8 : 4) / 1000000);
        assert(sram_regs.SRAMWTSC == 1);
    }
}
static R_SYSC_Type * system_access(void) { observe(); return &system_regs; }
static R_MRAM_Type * mram_access(void) { observe(); return &mram_regs; }
static R_SRAM_Type * sram_access(void) { observe(); return &sram_regs; }
#define R_SYSTEM system_access()
#define R_MRAM mram_access()
#define R_SRAM sram_access()
#else
#define R_SYSTEM (&system_regs)
#define R_MRAM (&mram_regs)
#endif
#define R_CPU_CTRL (&cpu_regs)
#define R_MSTP (&mstp_regs)
#define BSP_CFG_HANDLE_UNRECOVERABLE_ERROR(x) assert(!(x))
static uint32_t __get_PRIMASK(void) { return irq_mask; }
static void __disable_irq(void) { irq_mask = 1; }
static void __set_PRIMASK(uint32_t mask) { irq_mask = mask; }
static void __DSB(void) {}
static void __ISB(void) {}
static void __NOP(void) { ++nop_count; }
uint32_t SystemCoreClock;
#include "bsp_clocks_ra8p1.inc"
#define BSP_API_H
#include "bsp_override.h"
''' + pll_enums + '''
#include "r_cgc.h"
#include "r_cgc_ra8p1.inc"
''' + secondary_core + '''
int main(void)
{
#if defined(TEST_CLOCK_INIT)
    system_regs.SCKSCR = 1;
    system_regs.VSCR = 2;
    system_regs.MOSCCR = system_regs.PLLCR = 1;
    mram_regs.MRCFREQ = mram_regs.MREFREQ = 10;
    mstp_regs.MSTPCRA = 0xFF3FFFF0U;
    mstp_regs.MSTPCRB = mstp_regs.MSTPCRC = mstp_regs.MSTPCRD = mstp_regs.MSTPCRE = UINT32_MAX;
    previous_source = 1;
    main_osc_ready = pll_ready = observe_startup = true;
    bsp_clock_init();
    assert(SystemCoreClock == (BSP_CFG_RA8P1_STARTUP_PROFILE == 2 ? 600000000U :
                              (BSP_CFG_RA8P1_STARTUP_PROFILE == 1 ? 240000000U : 8000000U)));
    assert(irq_mask == 0);
#else
    system_regs.SCKSCR = 1;
    system_regs.HOCOCR2 = 4;
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_HOCO) == 32000000);
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_MOCO) == 8000000);
    assert(R_BSP_SourceClockHzGet((fsp_priv_source_clock_t) 11) == 0);
    assert(bsp_ra8p1_clock_hz_get(40) == 0);
    assert(bsp_ra8p1_clock_hz_get(1) == 0);
    assert(bsp_ra8p1_clock_hz_get(UINT32_MAX) == 0);
    system_regs.PLLCCR = (124U << 8) | 2U;
    system_regs.PLLCCR2 = 1U | (3U << 4) | (7U << 8);
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1P) == 500000000);
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1Q) == 250000000);
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1R) == 125000000);
    uint32_t p_dividers[16] = {0, 4, 6, 8, 0, 12, 0, 16, 0, 0, 0, 0, 0, 0, 0, 32};
    uint32_t qr_dividers[16] = {0, 4, 6, 8, 10, 12, 0, 16, 18, 3, 0, 0, 0, 0, 0, 0};
    system_regs.PLL2CCR = system_regs.PLLCCR;
    for (uint32_t code = 0; code < 16U; code++)
    {
        system_regs.PLLCCR2 = system_regs.PLL2CCR2 = (uint16_t) (code * 0x111U);
        uint32_t p_hz = p_dividers[code] ? 2000000000U / p_dividers[code] : 0;
        uint32_t qr_hz = qr_dividers[code] ? 2000000000U / qr_dividers[code] : 0;
        assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1P) == p_hz);
        assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL2P) == p_hz);
        assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1Q) == qr_hz);
        assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1R) == qr_hz);
        assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL2Q) == qr_hz);
        assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL2R) == qr_hz);
    }
    system_regs.PLLCCR2 = 1U | (3U << 4) | (7U << 8);
    system_regs.PLLCCR |= 3U;
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1P) == 0);
    system_regs.PLLCCR = 511U << 8;
    assert(R_BSP_SourceClockHzGet(FSP_PRIV_CLOCK_PLL1P) == 0); /* No wrapped frequency. */
    system_regs.PLLCCR = (124U << 8) | 2U;
    system_regs.SCKSCR = 5;
    system_regs.SCKDIVCR = 0x33233433U;
    system_regs.SCKDIVCR2 = 0x2210U;
    SystemCoreClockUpdate();
    assert(SystemCoreClock == (BSP_CFG_CPU_CORE == 0 ? 500000000U : 250000000U));
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_MRICLK) == 125000000U);
    system_regs.SCKDIVCR = 7;
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_PCLKD) == 0);
    system_regs.SCKSCR = 1;
    system_regs.SCKDIVCR = 0;
    system_regs.SCKDIVCR2 = 0;
    system_regs.PRCR = 0x12;
    mram_regs.MRCFREQ = 8;
    mram_regs.MREFREQ = 8;
#if BSP_CFG_CPU_CORE == 0
    system_regs.VSCR = 2;
    mstp_regs.MSTPCRA = 0xFF3FFFF0U;
    mstp_regs.MSTPCRB = mstp_regs.MSTPCRC = mstp_regs.MSTPCRD = mstp_regs.MSTPCRE = UINT32_MAX;
    assert(bsp_ra8p1_clock_set(1, 0, 0) == FSP_SUCCESS);
    assert(mram_regs.MRCFREQ == 8);
    assert(bsp_ra8p1_clock_set(3, 0, 0) == FSP_ERR_NOT_STABILIZED);
    assert(bsp_ra8p1_clock_set(5, 0, 0) == FSP_ERR_INVALID_RATE);
    assert(bsp_ra8p1_clock_set(1, 0x11111111U, 0x1111U) == FSP_ERR_UNSUPPORTED);
    assert(bsp_ra8p1_clock_set(1, 7, 0) == FSP_ERR_INVALID_ARGUMENT);
    assert(bsp_ra8p1_clock_set(0, 0, 0) == FSP_ERR_UNSUPPORTED);
    assert(system_regs.SCKSCR == 1 && mram_regs.MRCFREQ == 8 && mram_regs.MREFREQ == 8);
    assert(system_regs.PRCR == 0x12 && irq_mask == 0);
    system_regs.HOCOCR2 = 0;
    /* OSCSF and ACT are read-only hardware status; seed only in this host model. */
    *((uint8_t *) &system_regs.OSCSF) = 1;
    *((uint16_t *) &cpu_regs.CPU1ACTCSR) = 0x80;
    assert(bsp_ra8p1_clock_set(0, 0, 0) == FSP_ERR_UNSUPPORTED);
    *((uint16_t *) &cpu_regs.CPU1ACTCSR) = 0;
    assert(bsp_ra8p1_clock_set(0, 0, 0) == FSP_SUCCESS);
    assert(system_regs.SCKSCR == 0 && SystemCoreClock == 16000000);
    assert((mram_regs.MRCFREQ & 1023U) == 16 && (mram_regs.MREFREQ & 255U) == 16);
    assert(system_regs.PRCR == 0xA512 && irq_mask == 0);
    assert(bsp_ra8p1_clock_set(1, 0, 0) == FSP_SUCCESS);
    assert(SystemCoreClock == 8000000);
    assert((mram_regs.MRCFREQ & 1023U) == 8 && (mram_regs.MREFREQ & 255U) == 8);
    assert(R_BSP_SecondaryCoreStart(0x12000000U) == FSP_ERR_INVALID_ARGUMENT);
    assert(R_BSP_SecondaryCoreStart(0x02100000U) == FSP_ERR_INVALID_ARGUMENT);
    assert(R_BSP_SecondaryCoreStart(0x02000080U) == FSP_ERR_INVALID_ARGUMENT);
    assert(cpu_regs.CPU1INITVTOR == 0);
    *((uint16_t *) &cpu_regs.CPU1ACTCSR) = 0x80;
    assert(R_BSP_SecondaryCoreStart(0x02080000U) == FSP_ERR_IN_USE);
    *((uint16_t *) &cpu_regs.CPU1ACTCSR) = 0;
    cpu_regs.CPU1WAITCR = 1;
    assert(R_BSP_SecondaryCoreStart(0x02080000U) == FSP_ERR_UNSUPPORTED);
    assert(cpu_regs.CPU1INITVTOR == 0 && irq_mask == 0);
    cpu_regs.CPU1WAITCR = 0;
    /* The model has no hardware activation response: request must time out, never report success. */
    assert(R_BSP_SecondaryCoreStart(0x02080000U) == FSP_ERR_TIMEOUT);
    assert(cpu_regs.CPU1INITVTOR == 0x02080000U && cpu_regs.CPU1ACTCSR == 0xA501U);
    assert(irq_mask == 0);
    memset(&system_regs, 0, sizeof(system_regs));
    memset(&cpu_regs, 0, sizeof(cpu_regs));
    system_regs.SCKSCR = 1;
    system_regs.VSCR = 2;
    system_regs.MOSCCR = system_regs.PLLCR = 1;
    system_regs.MOMCR = 0x1A;
    system_regs.PRCR = 0x12;
    mstp_regs.MSTPCRA = 0xFF3FFFF0U;
    mstp_regs.MSTPCRB = mstp_regs.MSTPCRC = mstp_regs.MSTPCRD = mstp_regs.MSTPCRE = UINT32_MAX;
    cgc_instance_ctrl_t ctrl = {0};
    cgc_cfg_t cfg = {0};
    cgc_pll_cfg_t pll = {.source_clock = CGC_CLOCK_MAIN_OSC, .divider = CGC_PLL_DIV_1,
        .multiplier = (cgc_pll_mul_t) BSP_CLOCKS_PLL_MUL(40, 0),
        .out_div_p = CGC_PLL_OUT_DIV_4, .out_div_q = CGC_PLL_OUT_DIV_4, .out_div_r = CGC_PLL_OUT_DIV_4};
    cgc_divider_cfg_t fast = {.sckdivcr_w = BSP_RA8P1_PLL240_SCKDIVCR,
                              .sckdivcr2 = BSP_RA8P1_PLL_SCKDIVCR2};
    cgc_divider_cfg_t slow = {0};
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_ERR_NOT_OPEN);
    assert(R_CGC_Open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, NULL) == FSP_ERR_ASSERTION);
    pll.out_div_q = CGC_PLL_OUT_DIV_2;
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_ERR_UNSUPPORTED);
    assert(system_regs.MOSCCR == 1 && system_regs.PLLCR == 1);
    pll.out_div_q = CGC_PLL_OUT_DIV_4;
    assert(R_CGC_ClockStop(&ctrl, CGC_CLOCK_MAIN_OSC) == FSP_ERR_UNSUPPORTED);
    mstp_regs.MSTPCRC &= ~(1U << 24); /* Running PDM/DMA/CPU1 cannot share this transaction. */
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_ERR_UNSUPPORTED);
    assert(system_regs.MOSCCR == 1 && system_regs.PLLCR == 1 && system_regs.PRCR == 0x12);
    mstp_regs.MSTPCRC = UINT32_MAX;
    previous_source = 1;
    observe_startup = true;
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_ERR_TIMEOUT);
    assert(system_regs.SCKSCR == 1 && system_regs.SCKDIVCR == 0 && system_regs.SCKDIVCR2 == 0);
    assert(system_regs.PLLCR == 1 && system_regs.PRCR == 0xA512 && irq_mask == 0);
    main_osc_ready = true;
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_ERR_TIMEOUT);
    assert(system_regs.SCKSCR == 1 && system_regs.SCKDIVCR == 0 && system_regs.SCKDIVCR2 == 0);
    assert(system_regs.PRCR == 0xA512 && irq_mask == 0);
    /* Timeout left PLL running but unstabilized; matching retry waits/checks, never retunes. */
    pll_ready = true;
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_SUCCESS);
#line 1 "clock-profile240"
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_SUCCESS);
    assert(nop_count >= 18000 && irq_mask == 0);
    assert(system_regs.SCKDIVCR2 == 0x2220U && SystemCoreClock == 240000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_CPUCLK1) == 60000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_ICLK) == 60000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_MRICLK) == 60000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_MRPCLK) == 60000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_PCLKB) == 30000000);
    assert((mram_regs.MRCFREQ & 1023U) == 60 && (mram_regs.MREFREQ & 255U) == 60);
    assert(system_regs.MOMCR == 0x16 && system_regs.PRCR == 0xA512 && irq_mask == 0);
    assert(sram_regs.SRAMPRCR_S == 0xA500 && sram_regs.SRAMWTSC == 1);
    assert(R_CGC_ClockStop(&ctrl, CGC_CLOCK_PLL) == FSP_ERR_IN_USE);
    nop_count = 0;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_MAIN_OSC, &slow) == FSP_SUCCESS);
    assert(SystemCoreClock == 24000000 && sram_regs.SRAMWTSC == 1);
    assert((mram_regs.MRCFREQ & 1023U) == 24 && (mram_regs.MREFREQ & 255U) == 24);
    pll.multiplier = (cgc_pll_mul_t) BSP_CLOCKS_PLL_MUL(50, 0);
    pll.out_div_p = CGC_PLL_OUT_DIV_2;
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_ERR_IN_USE);
    assert(R_CGC_ClockStop(&ctrl, CGC_CLOCK_PLL) == FSP_SUCCESS);
    assert(R_CGC_ClockStart(&ctrl, CGC_CLOCK_PLL, &pll) == FSP_SUCCESS);
    fast.sckdivcr_w = BSP_RA8P1_PLL600_SCKDIVCR;
#line 1 "clock-profile600"
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_SUCCESS);
    assert(SystemCoreClock == 600000000 && nop_count >= 18000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_CPUCLK1) == 150000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_ICLK) == 150000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_MRICLK) == 150000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_MRPCLK) == 75000000);
    assert(bsp_ra8p1_clock_hz_get(FSP_PRIV_CLOCK_PCLKB) == 37500000);
    assert((mram_regs.MRCFREQ & 1023U) == 150 && (mram_regs.MREFREQ & 255U) == 75);
    uint32_t unchanged = source_changes;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_SUCCESS);
    assert(source_changes == unchanged); /* No redundant SCKSCR write. */
    nop_count = 0;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_MOCO, &slow) == FSP_SUCCESS);
    assert(SystemCoreClock == 8000000 && (mram_regs.MRCFREQ & 1023U) == 8);
    assert(sram_regs.SRAMWTSC == 1 && system_regs.VSCR == 2);
    reject_mram = true;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_TIMEOUT);
    assert(SystemCoreClock == 8000000 && system_regs.SCKSCR == 1);
    assert(system_regs.SCKDIVCR == 0 && system_regs.SCKDIVCR2 == 0);
    assert(system_regs.PRCR == 0xA512 && irq_mask == 0);
    reject_mram = false;
    reject_sram = true;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_TIMEOUT);
    assert(system_regs.SCKSCR == 1 && SystemCoreClock == 8000000 && irq_mask == 0);
    reject_sram = false;
    *((uint8_t *) &mram_regs.MRCPS) = 0x80;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    *((uint8_t *) &mram_regs.MRCPS) = 0;
    mram_regs.MRCPC1 = 1;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    mram_regs.MRCPC1 = 0;
    mram_regs.MENTRYR = 1;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    mram_regs.MENTRYR = 0;
    *((uint8_t *) &mram_regs.MCTRSTATR) = 1;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    *((uint8_t *) &mram_regs.MCTRSTATR) = 0;
    mstp_regs.MSTPCRC &= ~(1U << 24);
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    mstp_regs.MSTPCRC = UINT32_MAX;
    *((uint16_t *) &cpu_regs.CPU1ACTCSR) = 0x80;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    *((uint16_t *) &cpu_regs.CPU1ACTCSR) = 0;
    observe_startup = false;
    system_regs.VSCR = 1;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_PLL, &fast) == FSP_ERR_UNSUPPORTED);
    assert(system_regs.VSCR == 1 && system_regs.SCKSCR == 1 && irq_mask == 0);
    system_regs.VSCR = 2;
    irq_mask = 1;
    assert(R_CGC_SystemClockSet(&ctrl, CGC_CLOCK_MAIN_OSC, &slow) == FSP_SUCCESS);
    assert(irq_mask == 1 && system_regs.PRCR == 0xA512);
    assert(R_CGC_Close(&ctrl) == FSP_SUCCESS);
#else
    assert(R_BSP_SecondaryCoreStart(0x02080000U) == FSP_ERR_UNSUPPORTED);
    assert(bsp_ra8p1_clock_set(1, 0, 0) == FSP_ERR_UNSUPPORTED);
    assert(bsp_ra8p1_clock_set(0, 0, 0) == FSP_ERR_UNSUPPORTED);
    assert(system_regs.SCKSCR == 1 && system_regs.PRCR == 0x12);
    assert(mram_regs.MRCFREQ == 8 && mram_regs.MREFREQ == 8);
#endif
#endif
    return 0;
}
''')
            paths = [temp, FAMILY, FSP / 'inc/api', FSP / 'inc', FSP / 'inc/instances',
                     FSP / 'src/r_cgc', FSP / 'src/bsp/cmsis/Device/RENESAS/Include']
            for core in (0, 1):
                with self.subTest(core=core):
                    executable = temp / ('clock' + str(core))
                    command = [compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                               '-Wno-unused-function', '-DBSP_MCU_GROUP_RA8P1=1',
                               '-DBSP_CFG_CPU_CORE=' + str(core)]
                    command += ['-I' + str(path) for path in paths]
                    result = run(*command, str(source), '-o', str(executable))
                    self.assertEqual(result.returncode, 0, result.stdout)
                    result = run(str(executable))
                    self.assertEqual(result.returncode, 0, result.stdout)
            for profile, d1, d2 in ((0, 0, 0), (1, 0x22222322, 0x2220), (2, 0x32233432, 0x2220)):
                with self.subTest(startup_profile=profile):
                    fields = ['PCLKD', 'PCLKC', 'PCLKB', 'PCLKA', 'BCLK', 'PCLKE', 'ICLK', 'MRPCLK']
                    configuration = [f'#define BSP_CFG_RA8P1_STARTUP_PROFILE {profile}',
                                     '#define BSP_CFG_XTAL_HZ 24000000U',
                                     f'#define BSP_CFG_CLOCK_SOURCE {5 if profile else 1}U']
                    configuration += [f'#define BSP_CFG_{name}_DIV {(d1 >> (4 * i)) & 15}U'
                                      for i, name in enumerate(fields)]
                    configuration += [f'#define BSP_CFG_{name}_DIV {(d2 >> (4 * i)) & 15}U'
                                      for i, name in enumerate(['CPUCLK', 'CPU1CLK', 'MRICLK'])]
                    (temp / 'bsp_clock_cfg.h').write_text('\n'.join(configuration) + '\n')
                    executable = temp / ('startup' + str(profile))
                    command = [compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                               '-Wno-unused-function', '-DBSP_MCU_GROUP_RA8P1=1',
                               '-DBSP_CFG_CPU_CORE=0', '-DTEST_CLOCK_INIT=1']
                    command += ['-I' + str(path) for path in paths]
                    result = run(*command, str(source), '-o', str(executable))
                    self.assertEqual(result.returncode, 0, result.stdout)
                    result = run(str(executable))
                    self.assertEqual(result.returncode, 0, result.stdout)

    def test_port_layout_and_nmi_high_bits(self):
        compiler = shutil.which('cc')
        self.assertIsNotNone(compiler, 'A host C compiler is required')
        with tempfile.TemporaryDirectory(prefix='ra8p1-port-nmi-') as temp:
            temp = Path(temp)
            core_stub = '#define __IM volatile const\n#define __OM volatile\n#define __IOM volatile\n'
            for core in ('core_cm85.h', 'core_cm33.h'):
                (temp / core).write_text(core_stub)
            (temp / 'bsp_api.h').write_text('''
#ifndef TEST_BSP_API_H
#define TEST_BSP_API_H
#include <stddef.h>
#include <stdint.h>
#include "fsp_common_api.h"
#include "bsp_override.h"
#include "bsp_feature.h"
#include "bsp_group_irq.h"
#include "R7KA8P1AD.h"
#undef R_ICU
extern R_ICU_Type test_icu;
#define R_ICU (&test_icu)
#define BSP_CFG_PARAM_CHECKING_ENABLE 1
#define BSP_CFG_MCU_PART_SERIES 8
#define BSP_SECTION_EARLY_INIT
#define BSP_SECTION_FLASH_GAP
#define FSP_ASSERT(x) do { if (!(x)) { return FSP_ERR_ASSERTION; } } while (0)
void test_nmi_wait(uint32_t status, uint32_t expected);
#define FSP_HARDWARE_REGISTER_WAIT(reg, value) test_nmi_wait((reg), (value))
#endif
''')
            source = temp / 'port_nmi.c'
            source.write_text('''
#include <assert.h>
#include <string.h>
#include "bsp_api.h"
R_ICU_Type test_icu;
static uint32_t seen;
static uint32_t expected_flags;
void NMI_Handler(void);
fsp_err_t R_BSP_GroupIrqWrite(bsp_grp_irq_t irq, void (*callback)(bsp_grp_irq_t));
static void callback(bsp_grp_irq_t irq)
{
    assert((unsigned) irq <= 20U);
    assert(!(seen & (1U << irq)));
    seen |= 1U << irq;
}
void test_nmi_wait(uint32_t status, uint32_t expected)
{
    assert(status == expected_flags && expected == 0U);
    assert(test_icu.NMICLR == expected_flags);
    uint32_t cleared = 0;
    memcpy((void *) &test_icu.NMISR, &cleared, sizeof(cleared));
}
int main(void)
{
    _Static_assert(BSP_FEATURE_ICU_NMIER_MAX_INDEX == 20, "last implemented NMI");
    R_PORT0_Type port = {0};
    port.PCNTR1_b.PDR = 0x5AA5;
    port.PCNTR1_b.PODR = 0xA55A;
    assert(port.PCNTR1 == 0xA55A5AA5U);
    assert(port.PDR == 0x5AA5 && port.PODR == 0xA55A);
    uint32_t input = 0x87654321U;
    memcpy((void *) &port.PCNTR2, &input, sizeof(input));
    assert(port.PCNTR2_b.PIDR == 0x4321 && port.PCNTR2_b.EIDR == 0x8765);
    assert(port.PIDR == 0x4321 && port.EIDR == 0x8765);
    port.PCNTR3_b.POSR = 0x1234;
    port.PCNTR3_b.PORR = 0xABCD;
    assert(port.PCNTR3 == 0xABCD1234U);
    port.PCNTR4_b.EOSR = 0x2468;
    port.PCNTR4_b.EORR = 0x1357;
    assert(port.PCNTR4 == 0x13572468U);
    assert(port.EOSR == 0x2468 && port.EORR == 0x1357);
    assert((uintptr_t) (R_PORT0 + 13) == R_PORTD_BASE);

    unsigned const bits[] = {1, 16, 17, 18, 20};
    uint32_t all = 0;
    for (unsigned i = 0; i < sizeof(bits) / sizeof(bits[0]); i++)
    {
        assert(R_BSP_GroupIrqWrite((bsp_grp_irq_t) bits[i], callback) == FSP_SUCCESS);
        all |= 1U << bits[i];
    }
    for (unsigned i = 0; i <= sizeof(bits) / sizeof(bits[0]); i++)
    {
        expected_flags = i == sizeof(bits) / sizeof(bits[0]) ? all : 1U << bits[i];
        test_icu.NMIER = expected_flags;
        /* Disabled source bit 2 must neither invoke a callback nor be cleared. */
        uint32_t pending = expected_flags | (1U << 2);
        memcpy((void *) &test_icu.NMISR, &pending, sizeof(pending));
        seen = 0;
        NMI_Handler();
        assert(seen == expected_flags);
    }
    return 0;
}
''')
            paths = [temp, FAMILY, FSP / 'inc', FSP / 'inc/api', FSP / 'src/bsp/mcu/all',
                     FSP / 'src/bsp/cmsis/Device/RENESAS/Include']
            for core in (0, 1):
                with self.subTest(core=core):
                    executable = temp / f'port-nmi-{core}'
                    args = [compiler, '-std=c11', '-Wall', '-Wextra', '-Wconversion', '-Werror',
                            f'-DBSP_CFG_CPU_CORE={core}']
                    args += ['-I' + str(path) for path in paths]
                    result = run(*args, str(source), str(FSP / 'src/bsp/mcu/all/bsp_group_irq.c'),
                                 '-o', str(executable))
                    self.assertEqual(result.returncode, 0, result.stdout)
                    result = run(str(executable))
                    self.assertEqual(result.returncode, 0, result.stdout)

    def test_compile_bsp_both_cores(self):
        # Reject compiler-only distributions instead of manufacturing libc header stubs.
        compiler = select_arm_gcc(arm_gcc_candidates())
        with tempfile.TemporaryDirectory(prefix='ra8p1-bsp-') as temp:
            temp = Path(temp)
            # Render the same configuration content consumed by the configurator.
            core_configs = {}
            for path in MDF.glob('*ra8p1*.xml'):
                doc = ET.parse(path).getroot()
                for config in doc.findall('config'):
                    content = config.find('content')
                    if content is None:
                        continue
                    values = {}
                    for prop in config.findall('property'):
                        default = prop.get('default', '')
                        option = next((o for o in prop.findall('option') if o.get('id') == default), None)
                        values[prop.get('id')] = option.get('value') if option is not None else default
                    text = content.text or ''
                    for key, value in values.items():
                        text = text.replace('${' + key + '}', value)
                    if '${' in text:
                        continue
                    name = Path(config.get('path')).name
                    text = text.replace('../../../ra/', str(ROOT / 'baseline/ra') + '/')
                    if config.get('id') in ('config.bsp.ra8p1.cpu0', 'config.bsp.ra8p1.cpu1'):
                        core_configs[int(config.get('id')[-1])] = text
                    else:
                        (temp / name).write_text(text)
            (temp / 'bsp_cfg.h').write_text('''
#include "bsp_mcu_family_cfg.h"
#include "board_cfg.h"
#include "vector_data.h"
#define BSP_ICU_VECTOR_NUM_ENTRIES 96
#define BSP_CFG_RTOS 0
#define BSP_CFG_ASSERT 0
#ifndef BSP_CFG_PARAM_CHECKING_ENABLE
#define BSP_CFG_PARAM_CHECKING_ENABLE 1
#endif
#define BSP_CFG_STACK_MAIN_BYTES 1024
#define BSP_CFG_HEAP_BYTES 0
#define BSP_CFG_PFS_PROTECT 1
#define BSP_CFG_C_RUNTIME_INIT 1
#define BSP_CFG_EARLY_INIT 0
#define BSP_CFG_STARTUP_CLOCK_REG_NOT_RESET 0
''')
            (temp / 'vector_data.h').write_text('#define VECTOR_DATA_IRQ_COUNT 0\n')
            for driver, macro in (('cgc', 'CGC'), ('mram', 'MRAM'), ('pdm', 'PDM'), ('ioport', 'IOPORT')):
                (temp / f'r_{driver}_cfg.h').write_text(
                    f'#ifndef {macro}_CFG_PARAM_CHECKING_ENABLE\n'
                    f'#define {macro}_CFG_PARAM_CHECKING_ENABLE 1\n#endif\n')
            paths = [temp, FAMILY, FSP / 'inc', FSP / 'inc/api', FSP / 'inc/instances',
                     ROOT / 'baseline/ra/arm/CMSIS_6/CMSIS/Core/Include']
            # Locate CMSIS rather than substituting a host mock for CPU compile checks.
            cmsis = list((ROOT / 'baseline/ra').rglob('core_cm85.h'))
            if not cmsis:
                cmsis = list((ROOT / 'validation/projects/original_ra8m1/ra').rglob('core_cm85.h'))
            self.assertTrue(cmsis, 'CMSIS from preserved FSP 5.9 must be generated before this check')
            paths[-1] = cmsis[0].parent
            sources = [FSP / 'src/bsp/mcu/all' / name for name in
                       ('bsp_clocks.c', 'bsp_common.c', 'bsp_delay.c', 'bsp_group_irq.c', 'bsp_rom_registers.c',
                        'bsp_io.c', 'bsp_irq.c', 'bsp_register_protection.c')]
            sources += list((FSP / 'src/bsp/cmsis/Device/RENESAS/Source').glob('*.c'))
            sources += [FSP / f'src/r_{driver}/r_{driver}.c' for driver in ('cgc', 'mram', 'pdm', 'ioport')]
            sources += list((ROOT / 'baseline/ra/board/ra8p1_ek').glob('*.c'))
            spec = importlib.util.spec_from_file_location('device_generator', ROOT / 'tools/generate_ra8p1_device.py')
            generator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generator)
            svd = json.loads((ROOT / '.embedder/svd-cache/R7KA8P1AD.json').read_text())
            checks = ['#include "bsp_api.h"', '#include <stddef.h>']
            for peripheral in svd['peripherals']:
                used = set()
                for register in peripheral['registers']:
                    name = generator.scalar_name(register['name'])
                    if name in used:
                        name = generator.ident(register['name'])
                    used.add(name)
                    if name.startswith('CPU0') and '-' in register['name']:
                        continue  # Expanded per-core views are checked below.
                    checks.append(f'_Static_assert(offsetof(__typeof__(*R_{peripheral["name"]}), '
                                  f'{name}) == {register["addressOffset"]}, "{peripheral["name"]}.{name}");')
            checks += [
                '_Static_assert(offsetof(R_SYSC_Type, SCKDIVCR2) == 0x24, "SCKDIVCR2");',
                '_Static_assert(sizeof(((R_SYSC_Type *) 0)->SCKDIVCR2) == 2, "SCKDIVCR2 width");',
                '_Static_assert(offsetof(R_CPU_CTRL_Type, CPU1INITVTOR) == 0x44, "CPU1 VTOR");',
                '_Static_assert(offsetof(R_CPU_CTRL_Type, CPU1WAITCR) == 0x54, "CPU1 WAIT");',
                '_Static_assert(offsetof(R_ICU_Type, IELSR[95]) == 0x47C, "last vector");',
                '_Static_assert(sizeof(((R_ICU_Type *) 0)->NMIER) == 4, "NMIER width");',
                '_Static_assert(sizeof(((R_ICU_Type *) 0)->NMISR) == 4, "NMISR width");',
                '_Static_assert(offsetof(R_PORT0_Type, EIDR) == 6, "PORT0 EIDR");',
                '_Static_assert(sizeof(((R_PORT0_Type *) 0)->EIDR) == 2, "EIDR width");',
                '_Static_assert(offsetof(R_PORT1_Type, PCNTR4) == 12, "event output word");',
                '_Static_assert(sizeof(((R_PORT1_Type *) 0)->PCNTR4) == 4, "PCNTR4 width");',
                '_Static_assert(R_PORT1_BASE - R_PORT0_BASE == 0x20, "port byte stride");',
                '_Static_assert(offsetof(R_PFS_Type, PORT[13].PIN[15].PmnPFS) == 0x37C, "PD15");',
                '_Static_assert(sizeof(R_PORT0_Type) == 0x20, "port stride");',
                '_Static_assert(__builtin_types_compatible_p(__typeof__(*R_PORT0), __typeof__(*R_PORT1)), "common port type");',
                '_Static_assert((uintptr_t) (R_PORT0 + 13) == R_PORTD_BASE, "port D address");',
                '_Static_assert(offsetof(R_PORT0_Type, PCNTR2) == 4, "PCNTR2");',
                '_Static_assert(offsetof(R_PORT0_Type, PCNTR3) == 8, "PCNTR3");',
                '_Static_assert(offsetof(R_PORT0_Type, PCNTR4) == 12, "PCNTR4");',
                '_Static_assert(BSP_FEATURE_ICU_NMIER_MAX_INDEX == 20, "NMI includes IPC");',
                '_Static_assert(BSP_FEATURE_IOPORT_ELC_PORTS == 0x1E, "event ports 1-4");',
                '_Static_assert(BSP_MSTP_BIT_FSP_IP_ADC(0) == (1U << 21), "ADC module stop");',
                '_Static_assert(BSP_MSTP_BIT_FSP_IP_PDM(0) == (1U << 24), "PDM module stop");',
                '_Static_assert(BSP_PRV_VECT_ENUM(EVENT_PDM_DAT2, GROUP0) == ELC_EVENT_PDM_DAT2, "generated data vector");',
                '_Static_assert(BSP_PRV_VECT_ENUM(EVENT_PDM_ERR2, GROUP1) == ELC_EVENT_PDM_ERR2, "generated error vector");' ,
                '_Static_assert(BSP_FEATURE_MRAM_VERSION == 1U, "MRAM version");',
                '_Static_assert(BSP_FEATURE_MRAM_CODE_SIZE == 0x100000U, "MRAM code capacity");',
                '_Static_assert(CGC_PLL_MUL_100_0 == 396U, "5.9 PLL integer enum");',
                '_Static_assert(CGC_PLL_MUL_100_33 == 397U, "5.9 PLL third enum");',
                '_Static_assert(CGC_PLL_MUL_100_5 == 399U, "5.9 PLL half enum");',
            ]
            check_source = temp / 'svd_layout.c'
            check_source.write_text('\n'.join(checks))
            sources.append(check_source)
            variants = [(core, cpu, checking, profile)
                        for core, cpu in ((0, 'cortex-m85'), (1, 'cortex-m33'))
                        for checking in (0, 1) for profile in (0, 1)]
            for core, cpu, checking, profile in variants:
                (temp / 'bsp_mcu_core_cfg.h').write_text(core_configs[core])
                for src in sources:
                    with self.subTest(core=core, checking=checking, profile=profile, source=src.name):
                        command = [compiler, '-std=c11', '-mthumb', '-mcpu=' + cpu,
                                   '-mfloat-abi=hard', '-mfpu=' + ('fpv5-d16' if core == 0 else 'fpv5-sp-d16'),
                                    '-Wall', '-Wextra', '-Werror',
                                   '-DBSP_CFG_PARAM_CHECKING_ENABLE=' + str(checking),
                                   '-DCGC_CFG_PARAM_CHECKING_ENABLE=' + str(checking),
                                   '-DMRAM_CFG_PARAM_CHECKING_ENABLE=' + str(checking),
                                    '-DPDM_CFG_PARAM_CHECKING_ENABLE=' + str(checking),
                                    '-DIOPORT_CFG_PARAM_CHECKING_ENABLE=' + str(checking),
                                    '-DBSP_CFG_RA8P1_STARTUP_PROFILE=' + str(profile)]
                        if src.name in ('r_ioport.c', 'bsp_group_irq.c'):
                            command += ['-Wunused', '-Wuninitialized', '-Wmissing-declarations',
                                        '-Wconversion', '-Wpointer-arith', '-Wshadow', '-Wlogical-op',
                                        '-Waggregate-return', '-Wfloat-equal']
                        command += ['-I' + str(p) for p in paths]
                        command += ['-c', str(src), '-o', str(temp / 'test.o')]
                        result = run(*command)
                        self.assertEqual(result.returncode, 0, result.stdout)

            for core, wrong_cpu in ((0, 'cortex-m33'), (1, 'cortex-m85')):
                (temp / 'bsp_mcu_core_cfg.h').write_text(core_configs[core])
                if core == 1:
                    command = [compiler, '-E', '-mcpu=' + wrong_cpu, '-mthumb']
                    command += ['-I' + str(path) for path in paths]
                    result = run(*command, str(check_source))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('requires a Cortex-M33 compiler target', result.stdout)
                # Command-line overrides must not change the core selected by the device.
                command = [compiler, '-E', '-mcpu=' + ('cortex-m85' if core == 0 else 'cortex-m33'),
                           '-mthumb', '-DBSP_CFG_CPU_CORE=' + str(1 - core)]
                command += ['-I' + str(path) for path in paths]
                result = run(*command, str(check_source))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('core macro conflicts with the selected device target', result.stdout)


if __name__ == '__main__':
    unittest.main()
