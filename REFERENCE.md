# Completed migration reference

This branch preserves the custom FSP 5.9-derived RA8P1 implementation, target-specific tooling, tests, pack resources and documentation from the completed session. It is a reference snapshot, not an official release or a claim that every qualification passed.

`reference/migration-evidence.tar.gz` is stored with Git LFS. It contains the full `validation/` tree, including generated projects, candidate packs, build logs and physical-test records, plus the project-local SVD inputs, hardware scripts, captures, plan and agent handoffs. After materializing LFS files, unpack it in this reference checkout with:

```sh
tar -xzf reference/migration-evidence.tar.gz
```

Private bootstrap context, workspace account selection, local IDE configuration and caches are not included. The preserved upstream Git metadata remains in `reference/baseline-git.tar.gz`; the nested baseline HEAD is still FSP v5.9.0, with the migration recorded in the outer repository.

See `validation/results.md` inside the archive for evidence and limitations. MRAM programming/retention, controlled PDM overflow recovery and calibrated acoustic response remain unqualified.

The subsequent code review found six unresolved issues; this snapshot intentionally preserves their original implementation:

- Custom clock changes do not disable/drain MRAM prefetch before changing timing.
- Custom MRAM reset leaves read-ECC flags latched.
- Target metadata exposes ADC divisors rejected by the driver and prohibited by hardware.
- Inherited ADC stop-delay calculation indexes sparse groups by list position.
- Inherited early BSP initialization clears only 16 of the target's 21 NMI callback slots.
- Inherited sample-and-hold checking shifts beyond the integer width for internal ADC channels.

Keep this reference separate from clean challenge inputs. The new baseline retains the original FSP sources and reusable host tooling only.
