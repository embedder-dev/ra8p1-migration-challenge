# EK-RA8P1 support challenge: FSP 5.9.0 baseline

## The challenge

Add BSP and driver support for **EK-RA8P1** to the preserved **FSP 5.9.0** baseline. The intended scope is inspired by the RA8P1 support introduced with **FSP 6.0.0**; this is not a request to replace the baseline with a newer release.

The user-supplied examples are:

- New drivers: `r_mram`, `r_dac_b`, `r_pdm`.
- Changes to existing drivers: `r_wdt`, `r_adc`, `r_cgc`.

These examples express the original problem, not a verified implementation inventory or a prescribed solution. Determine what support is actually required from the baseline, target documentation and observed behavior. No implementation, expected patch, solution catalog or newer-release source is supplied.

Keep unrelated FSP 5.9.0 functionality intact. Describe the result as a custom, 5.9-derived RA8P1 support candidate, not as equivalent to the full official FSP 6.0.0 release.

## Preserved starting point

- `baseline/` contains the preserved official FSP v5.9.0 source. It has not been migrated.
- The baseline includes the original v5.9.0 pack module-description files (MDFs). These are baseline inputs, not newly implemented support.
- `reference/vendor/FSP_Packs_v5.9.0.zip` preserves the original baseline pack archive.
- `reference/provenance.json` records baseline provenance only.
- `reference/verification.json` records baseline-integrity verification only.
- `tools/verify_baseline.py` and `tools/test_verify_baseline.py` provide baseline-only verification and its tests.

The verification materials do not establish firmware correctness, successful builds, configurator support or hardware qualification. No implementation is supplied by this bundle.

## Start in an isolated workspace

Copy the entire `fsp-ra8p1-migration/` directory to a standalone location outside the parent checkout, preserving hidden files and the baseline's Git metadata. Open that copy as the workspace and begin a **fresh chat** with this brief.

The old chat contains solution-derived information that cannot be removed by deleting files. The parent checkout contains newer FSP content that can also leak implementation details. Reusing either would undermine the challenge, even if the preserved baseline itself is unchanged. Do not use the old chat or the parent checkout as solution evidence.

Before editing the baseline, run these commands from the copied bundle root:

```sh
python3 tools/verify_baseline.py --root . --report reference/verification.json
python3 -m unittest discover -s tools -p 'test_*.py' -v
```

Retain the pre-change verification report. An integrity failure after intentional baseline edits does not, by itself, indicate a firmware regression. A failed pre-change check must be resolved before relying on the starting point.

## Behavioral acceptance

Assess the candidate against observable requirements rather than resemblance to an undisclosed implementation:

- **Target identification:** establish that the selected board, device and execution configuration are correct for EK-RA8P1. Do not infer them from the parent workspace or an attached probe's label.
- **Configuration and generation:** the candidate supports the intended target and peripheral configurations through the FSP configurator. Generate project content through the configurator before building after configuration changes; do not hand-construct or broadly rewrite application `configuration.xml`.
- **Boot and clocks:** demonstrate reliable startup and correct clock behavior for the tested configurations.
- **Peripheral behavior:** demonstrate the requested functions with meaningful data or signal evidence, including relevant invalid-input and failure cases. A successful build alone is not peripheral validation.
- **Older-device regression:** show that representative devices already supported by FSP 5.9.0 retain their configuration, build and relevant runtime behavior after shared changes.
- **Evidence and limits:** distinguish baseline-integrity checks, host tests, generated-project builds and physical-target results. Record what was tested and identify unavailable tooling, hardware or fixtures as unverified, not passed.

Do not install packs, program or erase a device, or change its security state without specific authorization. Any destructive-memory test requires an explicitly approved test area and protection of existing data.

## Deliverable

Provide the scoped support changes and the evidence for the acceptance criteria above. Explain remaining limitations and intentional behavior changes. Do not claim a complete official FSP 6.0.0 migration or release-equivalence qualification.

## Prompt for the fresh chat

> Work from the preserved FSP v5.9.0 baseline in this standalone bundle. Read MIGRATION.md and verify the baseline before editing. Add EK-RA8P1 BSP and driver support, with scope inspired by FSP 6.0.0. The original examples are new r_mram, r_dac_b and r_pdm drivers, and changes to existing r_wdt, r_adc and r_cgc drivers; these are problem examples, not an implementation recipe. No solution is supplied. Preserve unrelated older-device behavior, use the configurator for application generation, and report build, behavioral and regression evidence separately. Do not use the old chat or parent checkout as solution evidence. Obtain specific authorization before installation or device-changing operations. Do not claim full official FSP 6.0.0 equivalence.
