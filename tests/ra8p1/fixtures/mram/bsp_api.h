#ifndef MRAM_TEST_BSP_API_H
#define MRAM_TEST_BSP_API_H
#define BSP_API_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "fsp_common_api.h"
typedef int IRQn_Type;
#define BSP_FEATURE_MRAM_VERSION 1
#define BSP_FEATURE_MRAM_CODE_SIZE 0x100000U
#if defined(__APPLE__)
#define BSP_PLACE_IN_SECTION(name) __attribute__((section("__TEXT,__mram_ram")))
#else
#define BSP_PLACE_IN_SECTION(name) __attribute__((section(name)))
#endif
#define FSP_ERROR_RETURN(condition, error) do { if (!(condition)) { return (error); } } while (0)
extern uint32_t SystemCoreClock;
typedef struct { uint32_t CCR; uint32_t VTOR; } mram_test_scb_t;
extern mram_test_scb_t g_scb;
#define SCB (&g_scb)
/* SVD offsets and access widths, independent of the driver implementation. */
typedef struct
{
    volatile uint8_t MRCPFB;
    uint8_t reserved0[3];
    volatile uint32_t MRCFREQ;
    uint8_t reserved1[0x10];
    volatile uint8_t MRCRAES;
    uint8_t reserved2[0x3000 - 0x19];
    volatile uint16_t MRCPC0;
    uint8_t reserved3[2];
    volatile uint16_t MRCPC1;
    uint8_t reserved4[10];
    volatile uint8_t MRCPS;
    uint8_t reserved5[0x3030 - 0x3011];
    volatile uint16_t MRCFLR;
    uint8_t reserved6[0x3804 - 0x3032];
    volatile uint16_t MRCEECC;
} mram_test_regs_t;
extern mram_test_regs_t g_regs;
#if BSP_TZ_NONSECURE_BUILD
#define R_MRAM_NS (&g_regs)
#else
#define R_MRAM (&g_regs)
#endif
uint32_t mram_test_primask(void);
void mram_test_mask(uint32_t value);
uint32_t mram_test_ipsr(void);
void mram_test_barrier(char kind);
#define __get_PRIMASK() mram_test_primask()
#define __set_PRIMASK(v) mram_test_mask(v)
#define __disable_irq() mram_test_mask(1)
#define __get_IPSR() mram_test_ipsr()
#define __DMB() mram_test_barrier('M')
#define __DSB() mram_test_barrier('S')
#define __ISB() mram_test_barrier('I')
#endif
