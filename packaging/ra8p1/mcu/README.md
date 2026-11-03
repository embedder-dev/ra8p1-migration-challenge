# Custom RA8P1 pack resources

These inputs are a custom, limited FSP 5.9-derived candidate, not an official
Renesas release. The original `reference/vendor/FSP_Packs_v5.9.0.zip` is the
schema/source precedent. No later FSP implementation or pack was used.

## Resource contract

- MCU pack: `Renesas.RA_mcu_ra8p1`, version `5.9.0`.
- Board pack: `Renesas.RA_board_ra8p1_ek`, version `5.9.0`.
- MCU family/component group: `ra8p1`; part: `R7KA8P1KFLCAC`.
- Family header: `ra/fsp/src/bsp/cmsis/Device/RENESAS/Include/R7KA8P1AD.h`.
  The loaded SVD is named `R7KA8P1AD`, revision `1.10.02`; the part suffix is
  not a different pack header filename.
- Pinmapper device: `renesas.ra8p1_ac`; package: `renesas.289bga`.
- Board MDF identifier: `board.ra8p1ek`; board source folder: `ra8p1_ek`.
- Tooling registration expects the aggregate-device, exact-part, family, event,
  board and board-configuration MDFs in `baseline/module_descriptions/`.
- Source/header entries in the PDSC are resolved from `baseline/` by the pack
  builder. This includes `bsp_mcu_family.h`, `bsp_clocks_ra8p1.h` and
  `bsp_clocks_ra8p1.inc`, in addition to the normal family files.

## Verified hardware representation

The memory XMI uses exclusive end addresses, matching the original 5.9 files:
code MRAM `[0x02000000, 0x02100000)` and shared SRAM
`[0x22000000, 0x221A0000)`. Source: **RA8P1 User's Manual Hardware,
R01UH1064EJ0130 Rev.1.30, Table 5.1, page 239**. The remaining memory in the
advertised 2 MiB total SRAM is not represented as contiguous shared SRAM.
TCM and external memory are deliberately excluded from initial linking.

The pinmap contains only the 19 explicitly verified GPIO balls recorded in
`resource-provenance.json`. Locations were checked across all quadrants of
**the same manual, Figure 1.3, page 88**. This is a partial package map; it
must not be treated as a full BGA escape or electrical design reference.
Supported modes are disabled/input/output, pull-up and open-drain, plus
analog mode on P001/P014, JTAG/SWD on P208–P211, and the onboard microphone
route **P502 = PDMDAT2 (input), P812 = PDMCLK2 (output)**. The PDM2 component
exposes only those two pins; enabling it selects both reciprocal connections.
The RA8P1 manual's Table 21.12 (page 872) and Table 21.15 (page 875) specify
PSEL = `11011b`. With PSEL in bits 28:24 and PMR at bit 16 (section 21.2.5),
the pinmap emits selector `0x1B000000` plus `IOPORT_CFG_PERIPHERAL_PIN`, giving
`0x1B010000` with default electrical settings. The literal selector avoids
requiring an absent FSP 5.9 PDM enum; no older MCU mux value is reused.

This is physical **channel 2**, not channel 0. A generated fixture must select
`pdm2`; a driver mode restricted to input 0 cannot use this onboard route.
No stereo filter pairing or input remapping is implied. Other peripheral mux
selections, drive strengths, IRQ channel routing and unlisted pins are not
advertised. UART, ADC-channel and DAC-channel component bindings are not
inferred from an older part's pinmap.

The default `.pincfg` preserves SWD on P210/P211. It does not configure the
LEDs or attach the board's optional external peripherals. Board-profile
settings must reference alternatives actually present in this pinmap.

## Linker and debugger limits

`script/fsp.ld` preserves the original 5.9 GNU runtime sections and exported
symbols, while excluding `.id_code*`, `.option_setting*` and
`.rom_registers*`. It rejects nonzero option/security, TCM and external-memory
regions and code/data regions outside the verified internal memories.
The legacy flash-at-offset-0x400 ROM-register reservation is removed.
No protected-memory address or writable debug option monitor is included.

Applications must supply `memory_regions.ld` through the normal configurator
flow. **The memory XMI is a physical memory description, not a dual-core
partition.** Before loading two images, assign disjoint per-core regions,
reserve any MRAM scratch/shared-mailbox space, and inspect both ELF load maps.
The pack does not silently choose a CPU1 boot address, release sequence,
security attribution, or memory ownership policy.

PG metadata exposes CPU0/Cortex-M85 using the original 5.9 single-core
vocabulary. The two debugger records name `R7KA8P1KF_CPU0` and
`R7KA8P1KF_CPU1`; these spellings were confirmed by a read-only search of the
SEGGER 9.42 host library's device-name data, not by connecting a target.
Minimal GDB records intentionally omit the old RA8M1 `.mcu` filename,
part-number addresses, writable option monitors and E2 firmware references.
A correct CPU1 compiler/debugger selection still requires configurator
integration evidence. Merely switching a BSP core property while compiling
for Cortex-M85 is not a valid CPU1 project.

Only the GNU linker is qualified here. IAR/Armclang/LLVM scripts, TrustZone
partitioning, CMSIS-Zone resources, E2/E2Lite firmware and SVD XML export are
not supplied. No installed newer FSP resource should be used as a fallback.

## Checks

Run the repeatable pinmap checks from the repository root with
`python3 -B packaging/ra8p1/test_pinmap.py -v`. They cover reference closure,
unchanged ball locations and SWD defaults, PDM2-only routing, selector encoding,
reciprocal connections and enabled/disabled groups against the original 5.9
vocabulary. They do not substitute for configurator generation or a microphone
capture on the target.

Resource checks cover XML well-formedness, original-5.9 element/attribute
vocabulary, PDSC/tooling identity, pinmap reference closure, default pin
settings, unique verified balls and file references. They are structural
checks, not full validation against an unavailable vendor XSD/Ecore schema.

A GNU Arm 13.2.Rel1 Cortex-M85 fixture linked successfully with the full
candidate script. `readelf -lSW` showed initialized bytes only in code MRAM,
plus the expected no-load SRAM segment; `nm` confirmed injected option/ID/ROM
symbols were absent. Negative fixtures rejected oversized code MRAM,
nonzero option memory, nonzero DTCM and nonzero SDRAM. This is host-side linker
evidence, not a generated-project or hardware test.
