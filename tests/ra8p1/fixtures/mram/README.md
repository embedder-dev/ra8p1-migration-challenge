# MRAM host fixture and evidence

`test_mram.py` compiles the real driver and unchanged FSP 5.9 flash API, then
executes `mram_harness.c`. It is not a string-matching substitute for driver tests.
The host model observes the widths/order of data, status, protection and flush
accesses. Both parameter-check modes and secure/non-secure register paths run.
A separate compile uses the driver's actual volatile memory-access macros.
The fixture's BSP is only a host adapter; it is not production BSP support.

## Hardware authority

No newer FSP implementation is used. Hardware behavior comes from R7KA8P1AD SVD
1.10.02 and **RA8P1 User's Manual: Hardware, R01UH1064EJ0130 Rev.1.30,
February 27, 2026**:

- Table 60.2 / Figure 60.2, p.3555: 1 MB code data/program windows
  `0x02000000..0x020fffff` (secure) and `0x12000000..0x120fffff`
  (non-secure). Instruction aliases are deliberately not programming destinations.
- Section 60.4.2, Figures 60.3/60.4, pp.3556–3558: 32-byte buffer;
  byte-granular writes grouped within each 32-byte boundary; full page triggers
  automatically; partial page uses explicit flush; wait for `ABUFEMP=1` and
  `PRGBSYC=0` before checking errors. No programming while `ABUFFULL=1`.
- Sections 60.5.32–33, pp.3585–3586: 16-bit keyed enable/restore,
  MRCPC0 key `0x86`, MRCPC1 key `0x68`. Block-protection cancel registers are
  never written.
- Section 60.5.36, pp.3587–3588: MRCPS is 8-bit; program/ECC error bits
  0/1 are read-one/write-zero, with confirming read/retry. Status bits 5/6/7
  are empty/full/busy. Reset does not invent a code-MRAM abort operation.
- Section 60.5.39, p.3590: MRCFLR is 16-bit; flush key `0xc3`, bit 0.
  Flush is legal only while the address buffer is not empty.
- Section 60.5.2, p.3562: MRCFREQ is rounded up in MHz, except frequencies
  at or below 32.768 kHz use zero. Clock ownership remains with the BSP.
- Sections 60.13/60.14, pp.3633–3634: RAM-resident programming routines and
  exception safety; data writes, DMB, flush, completion synchronization;
  no frequency/standby transition during programming.
- Table 70.124, pp.4267–4268: normal-mode maximum `tPMC = 1879/F_MRICLK
  + 78.75 us` (clock in MHz), plus `1/F_MRPCLK us` below 125 MHz. Open
  requires this bound plus 25% and 2 us margin. It also builds a finite
  CPU-cycle-derived fallback bound for a stopped application time source.

## Runtime contract not proved by host tests

The application must link/copy `.code_in_ram`, provide an independent RAM-resident
time source, and provide system-wide ownership hooks that quiesce CPU1/DMA,
prevent clock/standby changes, make exceptions safe, and maintain both cores'
cache coherency. The driver verifies local cache/prefetch disable, RAM vectors,
RAM control/stack/call sites, ECC encode mode, and the configured clock notification.
It excludes MRAM-backed control/stack/call sites and preserves local PRIMASK;
a still-busy timeout holds IRQs disabled until recovery
confirms idle. The exit hook receives whether hardware is idle and must not release
other masters or restore caches while it is not. Memory contents may have a
successfully written prefix after failure; no atomic transaction is promised.

This fixture does not verify a linker image, MPU/security attribution, physical
retention, bus timing, cache hardware, or successful target programming. These
remain integration/physical-device tests owned by the parent task. The optional
full-SVD consistency test needs the project's `.embedder/svd-cache/R7KA8P1AD.json`.
