#include "r_mram.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

_Static_assert(offsetof(mram_test_regs_t, MRCPC0) == 0x3000, "SVD MRCPC0 offset");
_Static_assert(offsetof(mram_test_regs_t, MRCPC1) == 0x3004, "SVD MRCPC1 offset");
_Static_assert(offsetof(mram_test_regs_t, MRCPS) == 0x3010, "SVD MRCPS offset");
_Static_assert(offsetof(mram_test_regs_t, MRCFLR) == 0x3030, "SVD MRCFLR offset");
_Static_assert(offsetof(mram_test_regs_t, MRCEECC) == 0x3804, "SVD MRCEECC offset");

uint32_t SystemCoreClock = 1000000;
mram_test_scb_t g_scb;
mram_test_regs_t g_regs;
static uint8_t memory[0x100000], source[256], buffer[32];
static uint32_t buffer_line, valid, tick, mask, ipsr;
static unsigned stores, flushes, commits, phase, reads, enters, exits, callbacks, barriers;
static unsigned protection_writes, last_protection;
static bool data_ordered, exclusive, stuck, frozen_time, stuck_error, corrupt, recursive, reject_enable;
static uint8_t inject_error;
static fsp_err_t enter_result;
static mram_instance_ctrl_t ctrl;
static uint32_t base;
#define SOURCE 0x20001000U

static void commit_start(void)
{
    assert(valid);
    commits++;
    phase = 2;
    g_regs.MRCPS = 0x80; /* Buffer not yet released. */
}
static void commit_finish(void)
{
    for (unsigned i = 0; i < 32; i++)
    {
        if ((valid >> i) & 1U) { memory[buffer_line + i] = buffer[i]; }
    }
    if (corrupt) { memory[buffer_line] ^= 1; }
    valid = 0;
    g_regs.MRCPS = (uint8_t) (0x20 | inject_error);
}
uint8_t mram_test_read8(uintptr_t address)
{
    if (address == (uintptr_t) &g_regs.MRCPS)
    {
        reads++;
        if (phase && !stuck)
        {
            if (--phase) { g_regs.MRCPS = 0xA0; }
            else { commit_finish(); }
        }
        return g_regs.MRCPS;
    }
    if (address >= base && address - base < sizeof(memory))
    {
        assert(exclusive && (g_regs.MRCPS & 0xE0) == 0x20);
        return memory[address - base];
    }
    if (address >= SOURCE && address - SOURCE < sizeof(source)) { return source[address - SOURCE]; }
    assert(address == (uintptr_t) &g_regs.MRCRAES);
    return g_regs.MRCRAES;
}
uint16_t mram_test_read16(uintptr_t address)
{
    assert(address == (uintptr_t) &g_regs.MRCPC0 || address == (uintptr_t) &g_regs.MRCPC1);
    return *(uint16_t *) address;
}
void mram_test_write8(uintptr_t address, uint8_t value)
{
    if (address == (uintptr_t) &g_regs.MRCPS)
    {
        assert(value == 0 && (g_regs.MRCPS & 3)); /* Read-one, write-zero; do not clear unobserved errors. */
        if (!stuck_error) { g_regs.MRCPS &= (uint8_t) ~3U; }
        return;
    }
    assert(address >= base && address - base < sizeof(memory));
    assert(exclusive && mask && !(g_scb.CCR & (3U << 16)));
    assert((base == MRAM_CODE_SECURE_START ? g_regs.MRCPC1 : g_regs.MRCPC0) == 1);
    assert(!(g_regs.MRCPS & 0xC0));
    uint32_t line = (uint32_t) (address - base) & ~31U;
    if (valid) { assert(line == buffer_line); }
    else { buffer_line = line; }
    unsigned pos = (unsigned) (address - base) & 31U;
    valid |= 1U << pos;
    buffer[pos] = value;
    stores++;
    data_ordered = false;
    g_regs.MRCPS = 0;
    if (valid == UINT32_MAX) { commit_start(); }
}
void mram_test_write16(uintptr_t address, uint16_t value)
{
    assert(exclusive);
    if (address == (uintptr_t) &g_regs.MRCFLR)
    {
        assert(value == 0xC301 && data_ordered && valid && !phase);
        flushes++;
        commit_start();
        return;
    }
    assert(address == (uintptr_t) &g_regs.MRCPC0 || address == (uintptr_t) &g_regs.MRCPC1);
    assert((value & 0xFFFE) == (address == (uintptr_t) &g_regs.MRCPC0 ? 0x8600 : 0x6800));
    protection_writes++;
    last_protection = value;
    if (!reject_enable || !(value & 1U)) { *(uint16_t *) address = value & 1; }
}
uint32_t mram_test_primask(void) { return mask; }
void mram_test_mask(uint32_t value) { mask = value; }
uint32_t mram_test_ipsr(void) { return ipsr; }
void mram_test_barrier(char kind)
{
    barriers++;
    if (kind == 'M') { data_ordered = true; }
    if (kind == 'S') { assert((g_regs.MRCPS & 0xE0) == 0x20); }
}
static fsp_err_t enter(void const * context)
{
    assert(context == source);
    enters++;
    if (recursive)
    {
        recursive = false;
        assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_IN_USE);
    }
    if (enter_result != FSP_SUCCESS) { return enter_result; }
    assert(!exclusive);
    exclusive = true;
    return FSP_SUCCESS;
}
static void leave(void const * context, bool idle)
{
    assert(context == source && exclusive);
    assert(idle == ((g_regs.MRCPS & 0xE0) == 0x20));
    exits++;
    exclusive = false; /* The host models the reported state; target hook must retain system quiescence if !idle. */
}
static uint32_t time_us(void const * context)
{
    assert(context == source && exclusive);
    if (!frozen_time) { tick += 30; }
    return tick;
}
static void callback(flash_callback_args_t * args) { (void) args; callbacks++; }
static mram_extended_cfg_t config(void)
{
    mram_extended_cfg_t ext = {base, 250000000, 125000000, 1000, enter, leave, time_us, source};
    return ext;
}
static void initialize(void)
{
    memset(&ctrl, 0, sizeof(ctrl));
    memset(&g_regs, 0, sizeof(g_regs));
    memset(memory, 0xFF, sizeof(memory));
    for (unsigned i = 0; i < sizeof(source); i++) { source[i] = (uint8_t) (i ^ 0xAC); }
    g_regs.MRCPS = 0x20;
    g_regs.MRCFREQ = 250;
    g_scb.CCR = 0;
    g_scb.VTOR = 0x20000000;
    stores = flushes = commits = phase = reads = enters = exits = callbacks = barriers = 0;
    protection_writes = last_protection = 0;
    valid = tick = mask = ipsr = 0;
    data_ordered = exclusive = stuck = frozen_time = stuck_error = corrupt = recursive = reject_enable = false;
    inject_error = 0;
    enter_result = FSP_SUCCESS;
}
static void open_driver(void)
{
    mram_extended_cfg_t ext = config();
    flash_cfg_t cfg = {.p_extend = &ext, .p_callback = callback, .p_context = source};
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(ctrl.extend.p_enter == enter && ctrl.extend.p_context == source);
}
static void close_driver(void)
{
    assert(g_flash_on_mram.close(&ctrl) == FSP_SUCCESS);
    assert(!exclusive && mask == 0 && callbacks == 0);
}
static void test_lifecycle(void)
{
    initialize();
    mram_extended_cfg_t ext = config();
    flash_cfg_t cfg = {.p_extend = &ext};
    assert(g_flash_on_mram.open(NULL, &cfg) == FSP_ERR_ASSERTION);
    assert(g_flash_on_mram.open(&ctrl, NULL) == FSP_ERR_ASSERTION);
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_NOT_OPEN);
    cfg.data_flash_bgo = true;
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_UNSUPPORTED);
    cfg.data_flash_bgo = false;
    ext.code_start = 0;
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_INVALID_ADDRESS);
    ext = config(); ext.timeout_us = 1;
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    ext = config(); ext.mriclk_hz = 0;
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT);
    ext = config(); ext.mriclk_hz = 32768; ext.mrpclk_hz = 32768;
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_INVALID_ARGUMENT); /* 1 ms cannot cover slow clock. */
    ext = config();
#if BSP_TZ_NONSECURE_BUILD
    ext.code_start = MRAM_CODE_SECURE_START;
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_INVALID_ADDRESS);
    ext = config();
#endif
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(g_flash_on_mram.open(&ctrl, &cfg) == FSP_ERR_ALREADY_OPEN);
    mram_instance_ctrl_t second = {0};
    assert(g_flash_on_mram.open(&second, &cfg) == FSP_ERR_IN_USE);
    flash_info_t info;
    assert(g_flash_on_mram.infoGet(&ctrl, &info) == FSP_SUCCESS);
    assert(info.code_flash.num_regions == 1 && info.data_flash.num_regions == 0);
    assert(info.data_flash.p_block_array == NULL);
    assert(info.code_flash.p_block_array->block_section_st_addr == base);
    assert(info.code_flash.p_block_array->block_section_end_addr == base + 0xFFFFF);
    assert(info.code_flash.p_block_array->block_size == 0 && info.code_flash.p_block_array->block_size_write == 1);
    flash_callback_args_t callback_mem;
    assert(g_flash_on_mram.callbackSet(&ctrl, callback, source, &callback_mem) == FSP_SUCCESS);
    assert(ctrl.p_callback_memory == &callback_mem && ctrl.p_context == source);
    close_driver();
    assert(g_flash_on_mram.close(&ctrl) == FSP_ERR_NOT_OPEN);
    puts("lifecycle/info/callback signature passed");
}
static void test_ranges(void)
{
    initialize(); open_driver();
    uint32_t bad[][2] = {{base - 1, 1}, {base, 0}, {base, UINT32_MAX},
        {base + 0xFFFFF, 2}, {base + 0x100000, 1}, {0, 1}, {0x10000000, 1},
        {0x01000000, 1}, {UINT32_MAX, 2}};
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++)
    {
        assert(g_flash_on_mram.write(&ctrl, SOURCE, bad[i][0], bad[i][1]) == FSP_ERR_INVALID_ADDRESS);
    }
    uint32_t bad_sources[] = {0, base, 0x10000000, 0x02000000, 0x12000000, UINT32_MAX};
    for (unsigned i = 0; i < sizeof(bad_sources) / sizeof(bad_sources[0]); i++)
    {
        assert(g_flash_on_mram.write(&ctrl, bad_sources[i], base, 2) == FSP_ERR_INVALID_ADDRESS);
    }
    assert(stores == 0 && enters == 0 && protection_writes == 0);
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base + 0xFFFFF, 1) == FSP_SUCCESS);
    assert(memory[0xFFFFF] == source[0] && flushes == 1);
    close_driver(); puts("range/overflow/last byte passed");
}
static void test_programming(void)
{
    initialize(); open_driver();
    recursive = true;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base + 1, 94) == FSP_SUCCESS);
    assert(stores == 94 && flushes == 2 && commits == 3);
    assert(memory[0] == 0xFF && memory[95] == 0xFF);
    assert(memcmp(memory + 1, source, 94) == 0 && reads >= 6);
    assert(enters == 1 && exits == 1); /* Recursive caller is rejected before the ownership hook. */
    assert(last_protection == (base == MRAM_CODE_SECURE_START ? 0x6800 : 0x8600));
    assert(g_regs.MRCPC0 == 0 && g_regs.MRCPC1 == 0);
    memset(source, 0x19, 64);
    if (base == MRAM_CODE_SECURE_START) { g_regs.MRCPC1 = 1; } else { g_regs.MRCPC0 = 1; }
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 64) == FSP_SUCCESS);
    assert(flushes == 2 && commits == 5 && memcmp(memory, source, 64) == 0);
    assert((last_protection & 1) == 1); /* Preserve initially enabled protection state. */
    flash_result_t result = FLASH_RESULT_BGO_ACTIVE;
    assert(g_flash_on_mram.blankCheck(&ctrl, base, 100, &result) == FSP_SUCCESS && result == FLASH_RESULT_NOT_BLANK);
    assert(g_flash_on_mram.blankCheck(&ctrl, base + 100, 100, &result) == FSP_SUCCESS && result == FLASH_RESULT_BLANK);
    close_driver(); puts("partial/full page ordering/rewrite/readback/blank check passed");
}
static void test_errors(void)
{
    initialize(); open_driver();
    for (unsigned error = 1; error <= 2; error++)
    {
        inject_error = (uint8_t) error;
        assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_WRITE_FAILED);
        assert(last_protection == (base == MRAM_CODE_SECURE_START ? 0x6800 : 0x8600));
    }
    inject_error = 0;
    assert(g_flash_on_mram.reset(&ctrl) == FSP_SUCCESS);
    reject_enable = true;
    unsigned before = stores;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_WRITE_FAILED);
    assert(stores == before && (last_protection & 1) == 0);
    reject_enable = false;
    corrupt = true;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_WRITE_FAILED);
    corrupt = false;
    g_regs.MRCRAES = 2;
    flash_result_t result = FLASH_RESULT_BGO_ACTIVE;
    assert(g_flash_on_mram.blankCheck(&ctrl, base, 1, &result) == FSP_ERR_WRITE_FAILED);
    assert(result == FLASH_RESULT_BGO_ACTIVE);
    g_regs.MRCRAES = 0;
    g_regs.MRCPS |= 3;
    assert(g_flash_on_mram.reset(&ctrl) == FSP_SUCCESS && (g_regs.MRCPS & 3) == 0);
    close_driver(); puts("program/ECC/readback/read error reporting and reset passed");
}
static void test_timeout(void)
{
    initialize(); open_driver();
    tick = UINT32_MAX - 10;
    stuck = true;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_TIMEOUT);
    assert(ctrl.faulted && !ctrl.busy && !exclusive && mask == 1 && ctrl.interrupts_held);
    assert(flushes == 1 && stores == 1 && (last_protection & 1) == 0);
    flash_status_t status;
    assert(g_flash_on_mram.statusGet(&ctrl, &status) == FSP_SUCCESS && status == FLASH_STATUS_BUSY);
    assert(g_flash_on_mram.reset(&ctrl) == FSP_ERR_IN_USE);
    assert(g_flash_on_mram.close(&ctrl) == FSP_ERR_IN_USE);
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    stuck = false; /* Model eventual completion, not a driver-invented abort. */
    while (phase) { (void) mram_test_read8((uintptr_t) &g_regs.MRCPS); }
    assert(g_flash_on_mram.reset(&ctrl) == FSP_SUCCESS && !ctrl.faulted);
    assert(mask == 0 && !ctrl.interrupts_held);
    frozen_time = true; stuck_error = true; g_regs.MRCPS |= 3;
    unsigned before = reads;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_TIMEOUT);
    assert(reads - before < 1100 && stores == 1); /* Finite fallback when timer stops. */
    stuck_error = false;
    assert(g_flash_on_mram.reset(&ctrl) == FSP_SUCCESS);
    g_regs.MRCPS = 0xE0; /* A foreign master already has a full, busy buffer. */
    before = stores;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_TIMEOUT);
    assert(stores == before && mask == 1);
    g_regs.MRCPS = 0x20;
    assert(g_flash_on_mram.reset(&ctrl) == FSP_SUCCESS && mask == 0);
    close_driver(); puts("timeout/wrapping clock/stopped clock/fault recovery passed");
}
static void test_hazards(void)
{
    initialize(); open_driver();
    g_scb.CCR = 1U << 16;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    g_scb.CCR = 1U << 17;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    g_scb.CCR = 0; g_scb.VTOR = 0;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    g_scb.VTOR = 0x20000000; g_regs.MRCPFB = 1;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    g_regs.MRCPFB = 0; g_regs.MRCFREQ = 125;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    g_regs.MRCFREQ = 250; g_regs.MRCEECC = 1;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    g_regs.MRCEECC = 0; ipsr = 1;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    ipsr = 0; enter_result = FSP_ERR_IN_USE;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_IN_USE);
    enter_result = FSP_SUCCESS;
    ctrl.extend.p_time_us = (uint32_t (*)(void const *)) (uintptr_t) 0x02000101;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_INVALID_MODE);
    ctrl.extend.p_time_us = NULL;
    assert(g_flash_on_mram.write(&ctrl, SOURCE, base, 1) == FSP_ERR_UNSUPPORTED);
    ctrl.extend.p_time_us = time_us;
    assert(!stores && !exclusive && !mask && !protection_writes);
    close_driver(); puts("cache/vector/clock/ECC mode/context/ownership hazards passed");
}
static void test_unsupported(void)
{
    initialize(); open_driver();
    assert(g_flash_on_mram.erase(&ctrl, base, 1) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.accessWindowSet(&ctrl, base, base + 32) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.accessWindowClear(&ctrl) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.idCodeSet(&ctrl, source, FLASH_ID_CODE_MODE_UNLOCKED) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.updateFlashClockFreq(&ctrl) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.startupAreaSelect(&ctrl, FLASH_STARTUP_AREA_BLOCK0, true) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.bankSwap(&ctrl) == FSP_ERR_UNSUPPORTED);
    uint32_t count = 0xABCDEF;
    assert(g_flash_on_mram.antiRollbackCounterIncrement(&ctrl, FLASH_ARC_SEC) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.antiRollbackCounterRefresh(&ctrl, FLASH_ARC_SEC) == FSP_ERR_UNSUPPORTED);
    assert(g_flash_on_mram.antiRollbackCounterRead(&ctrl, FLASH_ARC_SEC, &count) == FSP_ERR_UNSUPPORTED);
    assert(count == 0xABCDEF);
    assert(g_flash_on_mram.userLockableAreaWrite(&ctrl, SOURCE, base, 32) == FSP_ERR_UNSUPPORTED);
    assert(!stores && !protection_writes && !enters);
    close_driver(); puts("all unsupported operations fail without side effects passed");
}
int main(void)
{
    base = MRAM_CODE_NONSECURE_START;
    test_lifecycle(); test_ranges(); test_programming(); test_errors(); test_timeout(); test_hazards(); test_unsupported();
#if !BSP_TZ_NONSECURE_BUILD
    base = MRAM_CODE_SECURE_START;
    test_ranges(); test_programming(); test_errors(); test_timeout(); test_hazards();
#endif
    puts("MRAM C behavior tests passed");
    return 0;
}
