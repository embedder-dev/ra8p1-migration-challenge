#ifndef MRAM_TEST_MOCK_H
#define MRAM_TEST_MOCK_H
#include "bsp_api.h"
uint8_t mram_test_read8(uintptr_t address);
uint16_t mram_test_read16(uintptr_t address);
void mram_test_write8(uintptr_t address, uint8_t value);
void mram_test_write16(uintptr_t address, uint16_t value);
#define MRAM_PRV_READ8(p) mram_test_read8((uintptr_t) (p))
#define MRAM_PRV_READ16(p) mram_test_read16((uintptr_t) (p))
#define MRAM_PRV_WRITE8(p, v) mram_test_write8((uintptr_t) (p), (uint8_t) (v))
#define MRAM_PRV_WRITE16(p, v) mram_test_write16((uintptr_t) (p), (uint16_t) (v))
#endif
