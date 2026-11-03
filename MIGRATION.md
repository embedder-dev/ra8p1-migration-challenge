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

## Start from the clean challenge commit

Use a clean checkout of the designated challenge-baseline commit, in a standalone workspace outside any checkout containing newer FSP sources. Do not copy a previous run's working directory: untracked files and ignored directories can contain implementations, generated projects, tests, captures and session history even when Git reports no tracked changes.

The starting inputs are the original `baseline/` and `reference/` contents, the two baseline-verification scripts in `tools/`, this brief, the host-workflow reference [embedder.md](embedder.md), and the wiring guide and diagrams. The host-workflow reference may guide tool use; it is not firmware implementation or validation evidence. No RA8P1 implementation, solution-specific tools, generated tests or prior-run results belong in the starting workspace. Ignoring such files does not remove them; discard previous-run artifacts before reusing a workspace.

The archives under `reference/` are stored with Git LFS. Ensure they are materialized as real archives, not LFS pointer files. If needed, run `git lfs pull` in the clean checkout. A fresh checkout also needs the nested baseline Git metadata restored from the supplied archive:

```sh
if [ ! -d baseline/.git ]; then
    tar -xzf reference/baseline-git.tar.gz -C baseline .git
fi
```

Keep the nested `baseline/` repository at the original FSP v5.9.0 revision. Preparation commits belong in the outer challenge repository, not inside `baseline/`.

Begin a **fresh chat** with this brief. An old chat contains solution-derived information that deleting files cannot remove. Do not use old chats, previous-run artifacts or a parent checkout containing newer FSP sources as solution evidence.

Before editing the baseline, run these commands from the clean workspace root (Python 3.11 or newer):

```sh
python3 tools/verify_baseline.py --root .
python3 -m unittest discover -s tools -p 'test_*.py' -v
```

The committed `reference/verification.json` is the original pre-change report. To record a new run's pre-change check separately, create `validation/` and run `python3 tools/verify_baseline.py --root . --report validation/pre-change-baseline.json` before editing. Retain that report. An integrity failure after intentional baseline edits does not, by itself, indicate a firmware regression. A failed pre-change check must be resolved before relying on the starting point.

## Bench wiring

See [EK-RA8P1 migration: bench wiring and validation](README-RA8P1-MIGRATION.md)
for the confirmed AD3 D0–D3 connections, the A4 → 1 kΩ → A0 loopback,
photographic diagrams, optional probes, safety precautions and test limitations.
The wiring reference does not supply a driver implementation or establish that
any hardware test has passed.

## Behavioral acceptance

### Integrate as implementation becomes available

Start configurator integration as soon as the minimum device/BSP slice is ready. Continue it alongside driver implementation; do not wait for all drivers or an expanded host-test suite before generating real projects.

- First prove one minimal CPU0 project through candidate packaging, discovery, Generate Project Content, and a clean linked build. Resolve device-header, board defaults, linker and tool-API mismatches at this checkpoint.
- As each driver source and matching MDF become available, immediately generate and build a small project that actually selects that driver. Compile generated configuration, instances and interrupt vectors against the production headers. A bare BSP project or mock-register test does not satisfy this checkpoint.
- Add CPU1 generation when its BSP is ready, and independently verify the selected core, compiler flags, debugger target and disjoint memory allocation. Exercise representative older-device builds as shared changes arrive.
- After each candidate update, verify the hashes at all three layers: installed pack, extracted support resources, and generated project sources. Use the same-version refresh procedure in [embedder.md](embedder.md); a reinstall, regeneration or clean build alone does not prove current sources.
- Report each slice's generated-build result or exact blocker immediately. Fix the owning source, metadata or automation before repeating that slice; continue independent work while it is blocked. Keep host tests, generated builds and hardware results separate.

Use verified APIs and exported build constants from the installed tool host, as documented in [embedder.md](embedder.md). Recheck them when the host changes. Record a successful live invocation before treating a mock-based automation test as evidence that the API works.

### Assess observable behavior

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

> Work from the preserved FSP v5.9.0 baseline in this standalone bundle. Read MIGRATION.md and verify the baseline before editing. Read embedder.md for verified host APIs and same-version pack refresh procedures. Add EK-RA8P1 BSP and driver support, with scope inspired by FSP 6.0.0. The original examples are new r_mram, r_dac_b and r_pdm drivers, and changes to existing r_wdt, r_adc and r_cgc drivers; these are problem examples, not an implementation recipe. No solution is supplied. Integrate incrementally: generate and clean-build the minimum BSP as soon as it is ready, then each driver as its source and MDF arrive; do not defer integration until all host tests pass. Preserve unrelated older-device behavior, use the configurator for application generation, and report build, behavioral and regression evidence separately. Do not use the old chat or parent checkout as solution evidence. Obtain specific authorization before installation or device-changing operations. Do not claim full official FSP 6.0.0 equivalence.
