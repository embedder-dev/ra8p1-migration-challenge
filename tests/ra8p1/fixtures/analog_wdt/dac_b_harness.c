#include <assert.h>
#include <stdio.h>
#include "r_dac_b.h"
extern unsigned test_delay_count, test_delays[], test_dac_states[], test_module_state[3][2];
extern bool test_mram_transfer_stuck;
int main(void)
{
    dac_b_instance_ctrl_t ctrl = {0}, other = {0};
    dac_b_extended_cfg_t ext = {DAC_DATA_FORMAT_FLUSH_RIGHT, DAC_B_VOLTAGE_MODE_NORMAL, false};
    dac_cfg_t cfg = {.channel = 0, .p_extend = &ext};
    assert(g_dac_on_dac_b.open == R_DAC_B_Open);
#if DAC_B_CFG_PARAM_CHECKING_ENABLE
    assert(R_DAC_B_Open(NULL, &cfg) == FSP_ERR_ASSERTION);
#endif
    assert(R_DAC_B_Start(&ctrl) == FSP_ERR_NOT_OPEN);
    cfg.channel = 2;
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_ERR_INVALID_CHANNEL);
    cfg.channel = 0; cfg.ad_da_synchronized = true;
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_ERR_UNSUPPORTED);
    cfg.ad_da_synchronized = false;
    test_mram.MENTRYR = 1;
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_ERR_IN_USE);
    assert(test_module_state[FSP_IP_DAC][0] == 0);
    test_mram.MENTRYR = 0; test_mram.MCTRLSR = 7;
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(test_mram.MCTRLSR == 7 && test_mram.MCTRCNTR == 0x4501);
    assert(test_dac[0].DACR0 == 0x80000000U && test_dac[0].DADR == 0xE0);
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_ERR_ALREADY_OPEN);
    assert(R_DAC_B_Write(&ctrl, 0x1000) == FSP_ERR_INVALID_ARGUMENT);
    assert(R_DAC_B_Write(&ctrl, 0xABC) == FSP_SUCCESS);
    assert(test_dac[0].DADR == 0xE0);
    assert(R_DAC_B_Start(&ctrl) == FSP_SUCCESS);
    assert(test_delay_count == 4 && test_delays[0] == 1 && test_delays[1] == 4);
    assert(test_dac_states[0] == 0x80000000U && test_dac_states[1] == 0x80000001U);
    assert(test_dac_states[2] == 1 && test_dac_states[3] == 1);
    assert(test_dac[0].DADR == 0xABC && test_dac[0].DACR0 == 1);
    assert(R_DAC_B_Start(&ctrl) == FSP_SUCCESS && test_delay_count == 4);
    assert(R_DAC_B_Write(&ctrl, 0xFFF) == FSP_SUCCESS && test_dac[0].DADR == 0xFFF);
    dac_cfg_t other_cfg = {.channel = 1, .p_extend = &ext};
    assert(R_DAC_B_Open(&other, &other_cfg) == FSP_SUCCESS);
    assert(R_DAC_B_Stop(&ctrl) == FSP_SUCCESS && test_dac[0].DACR0 == 0x80000000U);
    assert(other.open != 0 && test_module_state[FSP_IP_DAC][1] == 1);
    assert(R_DAC_B_Close(&ctrl) == FSP_SUCCESS && test_module_state[FSP_IP_DAC][0] == 0);
    assert(R_DAC_B_Close(&ctrl) == FSP_ERR_NOT_OPEN);
    assert(R_DAC_B_Close(&other) == FSP_SUCCESS);
    cfg.channel = 0; ext.data_format = DAC_DATA_FORMAT_FLUSH_LEFT;
    ext.voltage_mode = DAC_B_VOLTAGE_MODE_LOW; ext.internal_output = true;
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_SUCCESS);
    assert(test_dac[0].DADR == 0x1F80);
    assert(R_DAC_B_Write(&ctrl, 0xFFF1) == FSP_ERR_INVALID_ARGUMENT);
    assert(R_DAC_B_Write(&ctrl, 0xABC0) == FSP_SUCCESS);
    test_delay_count = 0;
    assert(R_DAC_B_Start(&ctrl) == FSP_SUCCESS);
    assert(test_delay_count == 3 && test_delays[1] == 6 && test_delays[2] == 6);
    assert(test_dac[0].DACR0 == 0x80000001U && test_dac[0].DADR == 0xABC0);
    assert(R_DAC_B_Close(&ctrl) == FSP_SUCCESS);
    test_mram.MCTRCNTR = 0;
    test_mram_transfer_stuck = true;
    assert(R_DAC_B_Open(&ctrl, &cfg) == FSP_ERR_TIMEOUT);
    assert(ctrl.open == 0 && test_module_state[FSP_IP_DAC][0] == 0);
    assert(test_mram.MCTRLSR == 1); /* Do not change the list during an active transfer. */
    puts("DAC_B behavior passed");
}
