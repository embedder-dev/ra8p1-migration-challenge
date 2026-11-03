# EK-RA8P1 migration

Implement EK-RA8P1 support following [MIGRATION.md](MIGRATION.md) and [embedder.md](embedder.md). Verify the baseline, respect source boundaries and preserve older-device behavior.

- **Integrate immediately:** generate and clean-build the minimum BSP, then each driver as it becomes available. Fix owning sources and metadata, not generated files.
- Use verified host APIs and exported constants. Check core, compiler, debugger and memory settings independently.
- After pack updates, verify installed packs, extracted support resources and project-source hashes; refresh the stale layer.
- Delegate distinct files, report blockers promptly and continue independent work. Keep handoffs short and avoid long blocking waits.
- Test edge cases and shared-code regressions. Before hardware tests, verify the image and reset/debugger state; retain authorization and memory-safety boundaries.

Report host tests, generated builds and hardware results separately, with evidence and remaining limitations.
