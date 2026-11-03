# FSP host automation

Read [MIGRATION.md](MIGRATION.md) for scope and permissions. These tools preserve verified host API usage; target firmware, pack construction, memory layouts and qualification remain the migration agent's work.

## Host contract

Verified API precedent: Renesas e2 studio with RA FSP v6.6.0, EASE Py4J engine `org.eclipse.ease.lang.python.py4j.engine`, GNU 13.2.1. IDE version and selected firmware-pack version are independent. Revalidate the API after host upgrades.

Ordinary tools/tests require Python 3.11+. EASE entry points and their shared modules are compatible with Python 3.9, used by this host's `/usr/bin/python3`. Run EASE scripts through the IDE: standalone Python does not provide `loadModule`, `org`, `com` or `java`.

```sh
"$E2STUDIO" -nosplash -application org.eclipse.ease.listEngines
"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$WORKSPACE" -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/discover_fsp.py" 5.9.0 \
  -vmargs -De2studio.ease.python3=/usr/bin/python3
```

Set `E2STUDIO` to the actual host executable and `ROOT` to this checkout. Use a new workspace/project location for creation and the same workspace for later configuration. One process owns a workspace at a time. Coordinate pack refresh so generation does not consume changing inputs.

## Available tools

| Tool | Interface / purpose |
| --- | --- |
| [discover_fsp.py](tools/discover_fsp.py) | EASE: `VERSION [BOARD]`; list boards, cores, toolchains, templates and API methods |
| [inspect_fsp_configuration.py](tools/inspect_fsp_configuration.py) | EASE: `PROJECT [MODULE_ID]`; inspect public configuration methods; temporary stack inspection is not saved |
| [generate_fsp_validation.py](tools/generate_fsp_validation.py) | EASE: `NAME ABSOLUTE_LOCATION BOARD CORE TOOLCHAIN_VERSION [options]` |
| [configure_fsp_validation.py](tools/configure_fsp_validation.py) | EASE: `NAME ABSOLUTE_LOCATION [MODULE_ID ...]` or `NAME ABSOLUTE_LOCATION --spec ABSOLUTE_JSON` |
| [install_fsp_packs.py](tools/install_fsp_packs.py) | Ordinary Python: install explicit manifest packs or hash-verified original release packs; dry run by default |
| [refresh_fsp_candidate.py](tools/refresh_fsp_candidate.py) | EASE: `MANIFEST INSTALLED_PACK_DIR --backup DIR [--apply]`; scoped support refresh; dry run by default |

[fsp_host.py](tools/fsp_host.py) contains shared orchestration; [fsp_manifest.py](tools/fsp_manifest.py) validates pack inventories and refreshes proven historical project sources. Entry points import these from their own tool directory.

Generation options are `--fsp-version VERSION` (default `5.9.0`), `--expected-settings JSON`, `--debugger-base BASE`, `--manifest MANIFEST` and repeatable `--previous-manifest MANIFEST`. Supply current/prior manifests for candidate generation; omit them only for ordinary stock-pack work.

Configuration specs support:

```json
{
  "expected_settings": {"#TargetName#": "<physical part>", "Core": "<core>"},
  "debugger_base": "<unsuffixed debugger base, when required>",
  "candidate_manifest": "../packs/manifest.json",
  "previous_manifests": ["../packs-previous/manifest.json"],
  "bsp": {},
  "modules": [{"id": "<module factory ID>", "properties": {}}],
  "pins": [],
  "source": "application.c"
}
```

All fields are optional. Paths within a spec are relative to that spec's directory unless absolute. Populate actual settings/options from the selected device/model, not these placeholders. Add expected architecture, FSP version and debugger command when checking a target's complete identity.

## Verified API sequence

Load `/FSP/ProjectGen`, `/FSP/Configuration` and `/FSP/Build` as needed. Use `projectGenerationFor("ra", version)`, set project name/location, board, core, bare-metal FLAT template, discovered GNU toolchain and debugger, then inspect the statuses from `validate()` and `generate(None)`.

For existing configurations, use `openDDSCConfiguration(name)`, the unique `isHal()` thread, `getCreatableModuleIds()`, `createStack(module_id)`, `getProperty(id)` and `setValue(str(value))`. Save the model before `generateProjectContent(name, None)`. Named stacks are reused only when type and requested instance name match uniquely. Empty serialized defaults may not be valid setter inputs.

Choose the unique active named pin table, not the first table. Model methods are `getComponent()`, `getConfig()`/`getPin()`, `getOptions()` and `setValue()`.

For core-aware debugger persistence, `prepare_project()` adapts the workspace project through `Platform.getAdapterManager().loadAdapter(..., "com.renesas.cdt.ddsc.project.IDDSCProject")`. It uses `DDSCProjectConfigurationLoader`, always disposes the loader, and verifies serialized settings afterward. If a debugger base is supplied, it loads `RaIdeSettingsContributor` through the `com.renesas.cdt.ra.contentgen` OSGi bundle and calls `getMultiCoreDebugDeviceName(base, selected_core)`. Persist this correction after creation and before opening the cached configuration model. Direct access through EASE's default Java class loader previously failed.

These loader/resolver APIs are internal and host-version-dependent. The helpers contain no target mapping: the caller supplies expected settings and debugger base; the host resolves the command. Independently check actual compiler flags, BSP core, debugger and linker allocation.

Use `FULL_BUILD` and `CLEAN_BUILD` exported by `/FSP/Build`, not numeric literals. Observed values were 6 and 15. Creation runs full → clean → full; reconfiguration runs clean → full. Clean must remove the previous ELF; a full build needs a zero-error summary and a fresh ELF. Process exit zero alone is insufficient. Inspect configuration problems and warnings as well as Python/Java failures. Select a complete compiler with libc headers before running a compile matrix.

## Pack manifests and same-version updates

The migration agent creates a manifest with a nonempty `packs` object. Each key is a pack basename; its record contains `sha256` for the whole archive and `entries`, mapping every regular archive entry to its SHA-256. Extra/missing entries, mismatched hashes, unsafe paths and archive symlinks are rejected. Keep old manifests **with their matching archive bytes** in separate directories before updating a candidate.

Three independent layers can be stale:

| Layer | Check | Update |
| --- | --- | --- |
| Installed packs | Archive hashes against manifest | Explicit manifest installation |
| Extracted support resources | Metadata/profile/device resources against pack entries | Scoped support refresh |
| Project sources | Selected PDSC-owned source/linker files against pack entries | New isolated project or verified historical-source refresh |

Discover the actual pack directory in EASE through `PackIOManagerFactory.getInstance().getPackManager("ra").getPacksFolder().toOSString()`. Do not guess a root-owned directory under `/Applications`.

```sh
python3 tools/install_fsp_packs.py --manifest "$MANIFEST" \
  --destination "$PACK_DIR" --backup "$BACKUP_DIR"
```

Add `--apply` only for an authorized installation. For original release packs use `--archive ZIP --archive-sha256 SHA256 --fsp-version VERSION` instead of `--manifest`; obtain the original checksum from `reference/provenance.json`. The installer validates all destinations before publication, writes packs atomically and preserves the first rollback point in a destination-specific journal. It does not build packs or remove unlisted packs.

Run support refresh through EASE with the interface above. Its applied path uses the verified scoped `checkAndInstallInternal` and `extractPackSupportFile` APIs. Explicit extraction is limited to versioned resources declared by the verified selected packs, including board filenames ending in `##VERSION##configuration.xml`. Missing/ambiguous vendor methods abort. The vendor installer can create support files before explicit journaling, so preserve a pre-refresh installation inventory/backup for complete rollback; the per-file journal alone is not a whole-installation snapshot.

For unchanged component versions, inspected `PackIOExtraction` selects `OVERWRITE_NONE`. Regeneration and a clean build can therefore recompile old bytes. The generation/configuration helpers use caller-supplied current and previous manifests to establish selected PDSC source ownership, back up exact historical bytes, remove only proven old files, invoke configurator extraction, and check the replacement. Unknown edits are preserved; failed extraction blocks the build and restores missing originals. There are no built-in historical fingerprints or automatic target-specific exceptions.

After refresh, run the new candidate's own source/ELF verifier. These host helpers do not know target memory limits or establish hardware correctness. For a new challenge run, isolate or explicitly restore any installed prior candidate before generation; a clean repository does not clear IDE caches.

## Hardware and evidence

Check both-core, peripheral and debugger state between fixtures. A debugger reset may preserve state, and stopping at `main` does not execute `hal_entry`. Establish the appropriate reset state before diagnosing absent waveforms. For watchdog fixtures, account for refresh synchronization and debugger stop masks from the target manual. Reuse confirmed wiring/authorization unless conditions change, and retain scratch-memory safety gates.

Run `python3 -m unittest discover -s tools -p 'test_*.py' -v`. Tests cover manifest integrity, historical ownership, backups, scoped support calls, exported build kinds, debugger persistence and configuration retries. They validate host automation; generated builds and physical measurements remain separate evidence.
