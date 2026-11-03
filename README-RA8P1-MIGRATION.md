# EK-RA8P1: custom FSP 5.9-derived candidate and validation

## Status and source boundary

This repository contains a **custom FSP 5.9-derived RA8P1 BSP/driver candidate**, not an official Renesas FSP release and not a completed FSP 6.0 upgrade. [MIGRATION.md](MIGRATION.md) remains the unchanged original challenge brief. The candidate changes are a working-tree patch over the preserved FSP 5.9.0 baseline; the nested baseline HEAD remains `5e8fbb10c752e16272790bd3a2587485d0377835`.

Inputs are the preserved `baseline/` sources, [original FSP 5.9 pack archive](reference/vendor/FSP_Packs_v5.9.0.zip), target documentation and the preserved R7KA8P1AD SVD. **No newer FSP source or pack implementation is used.** The installed e2 studio bundle labelled RA FSP v6.6.0 is a tool host only, not the candidate's source version. FSP API/configuration versions remain `5.9.0`; that version label alone does not identify an official package. Use the candidate manifest and hashes.

**Host compilation is not hardware proof. Live target verification remains pending and will be tracked by the parent session in `validation/results.md`.** That parent-owned report is the destination for fresh generation/build evidence, failures, blockers and physical measurements; this document supplies procedures, not invented results. Historical logs or `reference/verification.json` must not be substituted for fresh candidate evidence. The documentation update performs no hardware operations, installation or tests of the firmware.

## Candidate scope and limits

Target: **EK-RA8P1 v1, fitted R7KA8P1KFLCAC**, CPU0 Cortex-M85 and CPU1 Cortex-M33. Both cores have BSP source/configuration support; discovery, generated core selection, compiler flags, debugger selection, image partitioning and successful execution are separate checks. Primary peripheral qualification is on CPU0. CPU1 startup/WDT testing must not concurrently own CPU0's test peripherals or marker pins.

| Area | Candidate implementation | Qualification boundary |
| --- | --- | --- |
| BSP/device/board | [RA8P1 BSP](baseline/ra/fsp/src/bsp/mcu/ra8p1/), [CMSIS header](baseline/ra/fsp/src/bsp/cmsis/Device/RENESAS/Include/R7KA8P1AD.h), [board](baseline/ra/board/ra8p1_ek/) and matching MDF/pack resources | Internal-memory flat builds; not complete TrustZone, RTOS, external-memory or every-peripheral support |
| `r_cgc` | RA8P1 clock readback and constrained startup/source-switch paths | Not an arbitrary runtime PLL/divider/voltage-scaling implementation |
| [`r_mram`](baseline/ra/fsp/inc/instances/r_mram.h) | Blocking code-MRAM operations through `flash_api_t`; software blank check | No erase, extra-MRAM/option/security programming, bank swap or BGO guarantee. Whole-system RAM execution/quiescence hooks are mandatory; the qualification fixture is deliberately blocked until these are proved |
| [`r_dac_b`](baseline/ra/fsp/inc/instances/r_dac_b.h) | DAC_B implementation of `dac_api_t` | DAC0/P014 loopback is the planned test; no ELC startup, collective enable or ADC synchronization support. Open must serialize coefficient reads with MRAM/DAC users across cores |
| `r_adc_b` | RA8P1 ADC_B adaptation of the existing ADC API | Use ADC_B, not classic `r_adc`; planned test is single-ended AN001 with calibration and scan callbacks, not all modes/channels |
| `r_wdt` | Core-aware WDT0/WDT1 selection | Qualification uses register-start reset mode, existing option state and free-running execution; NMI and all window combinations are not promised |
| [`r_pdm`](baseline/ra/fsp/inc/instances/r_pdm.h) | Interrupt-driven buffered PDM capture with fixed filter profile | On-board route is PDM2, P502/P812. Fixed nominal 2 MHz PDM / 8 kframes/s PCM, no DMA or arbitrary sample-rate promise; stereo/audio quality not qualified |

The [pack resource notes](packaging/ra8p1/mcu/README.md) describe the intentionally partial pinmap and internal-memory linker restrictions. A module's presence in [SUPPORTED_SOFTWARE.md](baseline/SUPPORTED_SOFTWARE.md) is not a claim that every module runs on this device.

### Implemented startup profiles

The property is `config.bsp.ra8p1.startup_profile`. CPU0 owns shared clock initialization; CPU1 reads the established clocks and must not retune them.

| Option suffix | CPU0 | CPU1 / ICLK / MRICLK / MRPCLK | PCLKB |
| --- | --- | --- | --- |
| `.moco` (default) | 8 MHz | 8 MHz | 8 MHz |
| `.pll240` | 240 MHz | 60 MHz | 30 MHz |

These are **not 600 MHz or 1 GHz profiles**. PLL240 uses the board's 24 MHz crystal. Peripheral clocks still require their own valid configuration. In particular, `analog.json` selects PCLKA 60 MHz / 2 = ADCLK 30 MHz; MOCO alone cannot satisfy that ADC fixture's clock requirements. The PDM profile retains running 8 MHz MOCO as its independent source under either startup profile. Verify frequencies on the target rather than inferring them from the profile name.

## Deterministic tools and host procedures

Run from the repository root with Python 3.11 or newer. These are reproducible command recipes, **not a record that they passed**. The parent owns the fresh runs and their logs; do not duplicate a running generation/build or use its Eclipse workspace concurrently.

```sh
python3 tools/generate_ra8p1_device.py --check
python3 -m unittest discover -s tools -p 'test_*.py' -v
python3 -B -m unittest discover -s packaging/ra8p1 -p 'test_*.py' -v
python3 -m unittest discover -s tests/ra8p1 -p 'test_*.py' -v
python3 tools/build_ra8p1_packs.py --root . --output validation/packs
```

- [generate_ra8p1_device.py](tools/generate_ra8p1_device.py) checks the header against `.embedder/svd-cache/R7KA8P1AD.json` and its explicit layout corrections; `--check` does not rewrite it.
- [build_ra8p1_packs.py](tools/build_ra8p1_packs.py) verifies the original archive SHA-256, requires the preserved baseline HEAD, overlays candidate sources/MDFs and adds the RA8P1 MCU/board packs. ZIP order, timestamps and permissions are fixed. It emits `manifest.json` with changed-file, pack and entry hashes and `firmware_verified: false`.
- Shared BSP sources also occur in older MCU packs. The manifest includes changed source-owning **5.9** packs, not just the two new RA8P1 packs. Compare manifests and pack bytes from two separate output directories with identical inputs/environment for reproducibility; preserve any evidence directory before rebuilding it.
- `tools/verify_baseline.py --root .` is the pristine-input gate, not a candidate success gate. Intentional baseline edits now make that check fail. Do not weaken it; use the pre-change report and candidate change manifest to account for the patch.
- Host unit tests/mock registers and ARM object compilation do not establish silicon behavior or a complete generated-project link. Preserve failures and distinguish those layers in the report.

### Headless configurator generation

Use [discover_fsp.py](tools/discover_fsp.py), [generate_fsp_validation.py](tools/generate_fsp_validation.py) and [configure_fsp_validation.py](tools/configure_fsp_validation.py), not hand-authored application XML. The latter installs a selected fixture into application `src/hal_entry.c` after Generate Project Content; it does not put application code into generated `ra/`, `ra_cfg/` or `ra_gen/` files.

Example CPU0 flow, after authorized candidate installation/refresh:

```sh
ROOT="$(pwd)"
E2STUDIO="/Applications/Renesas e2 studio with RA FSP v6.6.0/Renesas e2 studio with RA FSP 6.6.0.app/Contents/MacOS/e2studio"
WORKSPACE="$ROOT/validation/ws-example-cpu0"
PROJECT="candidate_ra8p1_example_cpu0"
PROJECT_DIR="$ROOT/validation/projects/$PROJECT"

"$E2STUDIO" -nosplash -application org.eclipse.ease.listEngines
/usr/bin/python3 --version
"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$WORKSPACE" -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/discover_fsp.py" 5.9.0 EK-RA8P1 \
  -vmargs -De2studio.ease.python3=/usr/bin/python3

# Use the GNU version returned by discovery; this host has reported this ID.
TOOLCHAIN_VERSION="13.2.1.arm-13-7"
"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$WORKSPACE" -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/generate_fsp_validation.py" \
  "$PROJECT" "$PROJECT_DIR" EK-RA8P1 CM85 "$TOOLCHAIN_VERSION" \
  -vmargs -De2studio.ease.python3=/usr/bin/python3

"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$WORKSPACE" -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/configure_fsp_validation.py" \
  "$PROJECT" "$PROJECT_DIR" --spec "$ROOT/tests/ra8p1/boot-moco.json" \
  -vmargs -De2studio.ease.python3=/usr/bin/python3
```

Choose an unused project location/workspace for each fixture; do not rerun creation over an existing registered project. Configuration uses the **same workspace** in which creation registered the project. Capture stdout/stderr to a fresh log; if using `tee`, enable the shell's `pipefail`. A Python/Java traceback is failure even if the process exit status is zero. Require final zero-error summaries and fresh ELF/map files. The generation script does full/clean/rebuild; the configuration script does clean/build.

For CPU1 use a new name/location/workspace and `CM33`, but do not assume changing that argument fixes all integration: require generated `BSP_CFG_CPU_CORE=1`, actual Cortex-M33 compiler options, debugger `R7KA8P1KF_CPU1`, and a disjoint linker allocation. CPU0 must select core 0/Cortex-M85/`R7KA8P1KF_CPU0`. CPU1 release, image ordering and simultaneous execution remain parent-owned acceptance gates. The generic memory description is not proof of a valid dual-core partition.

Verify each freshly generated image with [verify_ra8p1_artifacts.py](tools/verify_ra8p1_artifacts.py):

```sh
python3 tools/verify_ra8p1_artifacts.py \
  --project "$PROJECT_DIR" --elf "$PROJECT_DIR/Debug/$PROJECT.elf" \
  --core CM85 --manifest validation/packs/manifest.json \
  --reserve 0x020FF000:0x02100000 --reserve 0x22180000:0x221A0000 --pretty
```

Use `--core CM33` for CPU1 and `--other-elf PATH` to reject overlap with the other image before loading both. `--not-before FILE` can bind freshness to a build-start marker; `--output FILE` saves a report and refuses overwrite. The checker compares selected core/version, generated source hashes and compiler version, rejects forbidden option/security sections and invalid/overlapping load ranges, and checks input/ELF freshness. It always reports `runtime_verified: false`. A passing checker does not prove correct reset sequencing, complete memory ownership or runtime safety.

### Install and rollback journals (parent-owned operations)

[install_ra8p1_packs.py](tools/install_ra8p1_packs.py) defaults to dry-run. Set `PACK_DIR` to the **verified existing** host pack directory from discovery/install evidence; do not guess an IDE application directory.

```sh
python3 tools/install_ra8p1_packs.py --root . \
  --destination "$PACK_DIR" --candidate validation/packs
```

Only an authorized installation adds `--apply`. Omitting `--candidate` selects the preserved original 5.9 archive rather than the candidate. The tool permits original 5.9 pack names plus the RA8P1 MCU/board packs, verifies candidate checksums, refuses symlinked destinations and publishes each complete pack atomically. It does not modify 6.6 packs or IDE binaries/plugins.

Same-version extracted resources may remain stale after replacing a `.pack`. The parent's scoped refresh uses [refresh_fsp_candidate.py](tools/refresh_fsp_candidate.py) through EASE:

```sh
"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$ROOT/validation/ws-example-refresh" \
  -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/refresh_fsp_candidate.py" \
  "$ROOT/validation/packs" "$PACK_DIR" \
  -vmargs -De2studio.ease.python3=/usr/bin/python3
```

Refresh **mutates installed support resources** and requires the same authorization as installation. It selects manifest-listed, hash-matched 5.9 packs; its internal vendor API is host-version-dependent and fails if unavailable. Do not substitute an all-version reinstall or copy newer resources when it fails. Verify generated source hashes afterwards.

Two journals have different scopes:

| Journal | Recorded state | Rollback use |
| --- | --- | --- |
| `validation/pack-install-backup/<destination-hash>/journal.json` | Exact destination, original SHA-256 and `.original` file, or `null` for prior absence; retained across later installs | Restore the saved pack bytes after checking their hash, or remove only the exact journalled pack that was originally absent |
| `validation/support-file-backup/journal.json` (with the example candidate directory) | Absolute support-file paths and saved-byte hashes/backup names, or `null` for prior absence, before explicit same-version extraction | Restore those exact saved files, or remove only journalled files that were absent |

There is **no automated rollback command**. Before manual rollback, stop users of that installation, verify journal destination/path ownership, inspect intervening changes and obtain authorization for replacements/removals. Check every restored byte hash and re-run discovery/generation in a fresh workspace. Reinstalling the original archive alone does not remove newly introduced RA8P1 packs or restore extracted support files.

The support journal is not a whole-installation snapshot: the vendor installer runs before the explicit per-file journalled extraction and can create resources there. Preserve the parent's separate pre-refresh inventory/backups for changes not represented by that journal. Never delete an entire cache/support directory or infer that a missing journal entry means safe removal. Keep both journals and their `.original` files together; do not claim rollback was tested merely because a journal exists.

## Confirmed Saleae harness

The user confirmed this **Saleae** mapping and common ground. Channels are analyzer inputs, not Arduino D0–D3. Recheck continuity and board markings before a new run; the confirmation is not a measured waveform or GPIO configuration proof.

| Saleae input | MCU port | EK contact | Status |
| --- | --- | --- | --- |
| CH0 | P514 | J1-1 | Confirmed |
| CH1 | P111 | J1-3 | Confirmed |
| CH2 | P806 | J1-9 | Confirmed |
| CH3 | P807 | J1-11 | Confirmed |
| GND | GND | J1-39 | Confirmed common ground |

Confirmed loopback: **A4 / P014 / DAC0 (J19-5) → 1 kΩ → A0 / P001 / AN001 (J19-1)**. A0 remains an analog input, never a competing output. This tests one DAC/ADC path, not DAC1 or other ADC channels. Saleae digital markers do not measure its analog voltage. An independent analog probe on the ADC side of the resistor is optional and **not confirmed installed**; no absolute voltage/accuracy claim is possible without appropriate measurement/reference evidence.

### Historical diagrams, not the current instrument assignment

[PNG](diagrams/ek-ra8p1-ad3-wiring.png), [HTML](diagrams/ek-ra8p1-ad3-wiring.html) and [SVG](diagrams/ek-ra8p1-ad3-wiring.svg) retain the earlier **AD3-labelled** overlays. They are historical pin-location aids only. The Saleae table above supersedes their instrument/channel labels; dashed scope leads remain proposals. Follow connector numbers, not photograph orientation.

### Safe setup and capture

1. Power off before rewiring. Confirm EK-RA8P1 v1/R7KA8P1KFLCAC, normal boot/debug routing and common ground. Leave the parallel display disconnected from J1 and disable any other user of the marker pins.
2. Use the board's supported power/debug supply, not analyzer outputs. Use Saleae inputs only. If AD3 is also present, leave W1/W2/V+/V− and all pattern/static output functions disconnected/disabled.
3. Check analog routing, references, voltage limits and the board manual's SW4 sharing rules. The resistor does not protect against out-of-range voltage. Do not change boot/security/OTP/option settings to make a test pass.
4. Inspect fresh ELF load ranges and fixture preconditions before the parent flashes anything. Establish ownership of the intended J-Link and actual Logic device through the hardware arbiter; inventory detection alone is not an active connection or target identity check.
5. Use the `debug-saleae` workflow and `hardware_script_run`/`la_*` helpers for an authorized capture. Select a supported rate with adequate oversampling, record the actual rate and channel list, and do not request a configurable digital threshold on Logic 8. Save `.sal`, raw CSV and edge/timing analysis; PDM is not I2S.
6. Capture long enough to include watchdog expiry and reboot, or use shorter higher-rate captures for the optional PDM bus. Associate each capture with the exact ELF/manifest hashes, core, clock profile, stimulus and debugger state. A screenshot or blinking marker alone is insufficient.

Optional, **unconfirmed** additional input-only probes: CH4 → P812/PDMCLK2, CH5 → P502/PDMDAT2. Locate these using the board's labelled access points and matching documentation; no header pin number is asserted. Keep CH0–CH3 in place. Bus activity alone does not prove PCM conversion or acoustic response.

## Fixture procedures and expected evidence

Use **one fixture per application**: each defines `hal_entry`/`R_BSP_WarmStart`, and boot/PDM also own SysTick. JSON specs are inputs to `configure_fsp_validation.py --spec`, not test-result files. Passing metadata/host checks does not establish live configurator acceptance; preserve unresolved properties, generation failures and stale-source mismatches as blockers.

| Fixture / spec | Intended run and marker interpretation | Required evidence / limits |
| --- | --- | --- |
| [qualification_boot.c](tests/ra8p1/qualification_boot.c), [boot-moco.json](tests/ra8p1/boot-moco.json), [boot-pll240.json](tests/ra8p1/boot-pll240.json) | CPU0: CH0 high after boot setup; CH1/CH2 complementary transitions every 100 SysTick ticks, nominally 100 ms (200 ms full cycle); CH3 failure. CPU1 updates its own record/ticks but does not drive these markers | Compare `g_validation_boot` core/frequency/error/ticks with measured periods for each frozen profile, warm resets and user-assisted cold starts. The 1 kHz SysTick is derived from `SystemCoreClock`; this cross-check is not an independent measurement of every clock domain or proof of CPU1 handshake |
| [qualification_analog.c](tests/ra8p1/qualification_analog.c), [analog.json](tests/ra8p1/analog.json) | CPU0 PLL240, calibrated ADC_B and DAC0: 3 ascending passes × 6 levels × 8 samples = 144 samples. CH1 changes per level; CH2 pulses after each valid sample; CH3 failure | Inspect `g_validation_analog`: calibration and fresh scan callbacks, raw/API readings, monotonicity, expected ratio, repeatability and cleanup errors. Default acceptance is 128 ADC counts + 5% of expected and repeated means within 128 counts. Reference ratio defaults to unity; change only from an independent measurement. These broad functional limits are not datasheet accuracy qualification |
| [qualification_wdt.c](tests/ra8p1/qualification_wdt.c), [wdt.json](tests/ra8p1/wdt.json) | Separate WDT0/CPU0 and WDT1/CPU1 runs. CH1 alternates on refresh, CH2 marks stage/expiry, CH3 failure | Follow the commanded sequence below; validate retained record, reset causes and measured timeout without debugger halt/freeze |
| [qualification_pdm.c](tests/ra8p1/qualification_pdm.c), [pdm.json](tests/ra8p1/pdm.json) | CPU0, PDM2 mono; CH0 ready, CH1 capture active, CH2 toggles on completion, CH3 failure. Automatic full/cancel/restart sequence; then commanded captures | Inspect `g_validation_pdm`, 8192-frame buffer/counts, guards, callbacks/errors and duration. Compare quiet versus controlled acoustic stimulus externally. `transport_ok` is not audio verification and `audio_verified` remains zero |
| [qualification_mram.c](tests/ra8p1/qualification_mram.c), [mram.json](tests/ra8p1/mram.json) | **Blocked by default**. CH0 start, CH1 all tests/restoration passed, CH2 original bytes verified, CH3 failure/blocker when marker setup was reached | Do not enable writes without the safety integration below. No tested writes, readback/restoration or retention result is claimed |

### Watchdog sequence

Require actual silicon register-start option state, and keep the other core/reset-status users idle. RA8P1 reads OFS0 or the selected OFS3/OFS3_SEC value at runtime; a generated option payload is neither required nor programmed. `DBGSTOPCR` defaults to masking watchdog counters even in OCD run mode: clear only the tested watchdog's debug mask during measurement and restore it afterward. Wait at least four watchdog count cycles after refreshing before expecting the counter readback to reload. The fixture does not program options. Its `.noinit` record must survive reset and be outside startup zeroing **and ECC initialization**; verify linker/startup ranges before interpreting retained results.

1. Stop at `hal_entry`, inspect `g_validation_wdt`, then run into the 30-second command window. No command means no WDT Open/Refresh/reset.
2. Set `g_wdt_validation_command=1` in the window. Let it run freely: initial refresh plus eight validated refresh cycles, then one deliberate software reset to disarm the non-stoppable watchdog. Only the expected software-reset cause and retained stage can pass the legal-refresh stage.
3. In the next command window set `g_wdt_validation_command=2`. It requires legal-stage success, repeats refresh checks, then stops refreshing. Compare CH1/CH2/boot timing with the recorded timeout and the correct core's latched WDT reset flag.
4. Read `stage`, `boot`, errors and both pass flags together; failures/timeouts must not be relabelled pass or repeatedly rearmed. With the current PLL240 spec, PCLKB is 30 MHz and the API's integer calculation gives about **1119 ms**, not 559 ms. Record the actual reported and measured values.

### PDM acoustic sequence

After the automatic sequence reaches command-ready stage 3, set `g_validation_pdm.label=1` (quiet) or `2` (known stimulus), then `g_validation_pdm.command=1` for a nominal 1.024-second capture. Command 2 cancels an active capture. Snapshot `g_validation_pdm_buffer.samples` before reuse and match the published record sequence before/after extraction. Require valid signed 20-bit samples, correct count/duration and zero error/overflow flags. A repeatable expected spectral/amplitude change under controlled sound is needed beyond nonconstant noise. Direct PDM clock/data evidence requires the unconfirmed optional probes.

### MRAM safety gate

The linker reserves **[0x020FF000, 0x02100000)** outside both image allocations and exports matching scratch bounds. The fixture checks executable SRAM placement for `.code_in_ram`, its callees/literals, buffers, stacks, vectors and reachable handlers. A debugger flag or scratch symbol alone cannot bypass the safety gates.

Concrete `validation_mram_platform_prepare`, `validation_mram_platform_finish` and `validation_mram_platform_time_us` hooks check whole-system exclusive ownership, freeze clocks/standby, preserve caches/prefetch, relocate exception paths and supply an interrupt-independent running timer. CPU0 cannot inspect CPU1-local DMA/DTC, so DMA1/DTC1 must already be module-stopped. On the tested board this prerequisite was not met: the fixture stopped at `MRAM_GATE_DMA1` without writing, and an independent readback matched the original 4096-byte backup. Finish restores state only after hardware is idle. A busy failure parks in RAM retaining quiescence.

Only after those gates are independently reviewed may the parent run save/pattern/overwrite/readback/CRC/restore checks, preserving original bytes and verifying restoration. Read `g_validation_mram` stages/errors, not just GPIOs. Power-cycle retention is a separate authorized/user-assisted test. **The present fixture is not evidence of any successful hardware write.**

## Evidence ownership and remaining gates

The parent session owns `validation/results.md`, hardware, fresh host logs, install/refresh and rollback execution. Record **not run / blocked / failed / passed** separately for integrity, host unit tests, object compilation, configurator generation, linked-artifact checks and physical behavior. For each result retain source/manifest/ELF hashes, exact part/core/configuration, tool versions, wiring/jumpers, stimulus, expected/observed values, timing tolerance and raw log/capture paths.

See [validation/results.md](validation/results.md) for completed generated builds, both-core execution, Saleae clock/reset timing, analog loopback and PDM transport checks. Matched preserved-versus-candidate builds passed for RA8M1 (BSP/CGC/WDT), RA6T2 (ADC_B), and RA6M5 (classic ADC/DAC/WDT); no older-device runtime comparison is claimed. Remaining gaps include safe MRAM writes/retention, controlled PDM overflow recovery, known-tone audio response, independent analog voltage accuracy and untested configurations. No blanket peripheral, board-family or official-release equivalence is promised.

Board reference: [EK-RA8P1 v1 User's Manual](https://www.renesas.com/en/document/mat/ek-ra8p1-v1-users-manual), Arduino/J1 connector tables and microphone section, with the matching board/MCU errata. Use RA8P1 documents/SVD, not RA8T2 or RA8M1 register/pin substitutions.
