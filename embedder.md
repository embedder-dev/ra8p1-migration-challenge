# Embedder migration workflow: e2 studio APIs and candidate refresh

Use this reference with [MIGRATION.md](MIGRATION.md). It records the tool-host contracts verified during this migration so integration can start with the first available BSP/driver slice. It is not firmware implementation or hardware-validation evidence.

## Verified host and source of truth

The verified host is **Renesas e2 studio with RA FSP v6.6.0**, using the EASE Py4J engine, GNU 13.2.1 and custom FSP **5.9.0** packs. The IDE version and selected firmware-pack version are independent.

- Ordinary repository tools require Python 3.11 or newer. EASE used `/usr/bin/python3`; discover and verify the host's interpreter separately.
- Use the actual method signatures and exported constants from the installed host. After a host upgrade, repeat engine discovery and a small live generation/build before relying on automation tests.
- Inspect tool-host classes with Java reflection or `javap` when necessary. Java implementation classes can be public but unavailable through EASE's default class loader. Load them through their owning OSGi bundle, as shown below.
- Prefer the current scripts to copying fragments from historical handoffs. The examples below describe their API contract; the scripts contain the full validation and error handling.

| Operation | Current implementation |
| --- | --- |
| Discover boards, cores and toolchains | [tools/discover_fsp.py](tools/discover_fsp.py) |
| Create, generate and build a project | [tools/generate_fsp_validation.py](tools/generate_fsp_validation.py) |
| Inspect the live configuration API | [tools/inspect_fsp_configuration.py](tools/inspect_fsp_configuration.py) |
| Select stacks/properties/pins, regenerate and build | [tools/configure_fsp_validation.py](tools/configure_fsp_validation.py) |
| Build deterministic candidate packs | [tools/build_ra8p1_packs.py](tools/build_ra8p1_packs.py) |
| Install packs with a rollback journal | [tools/install_ra8p1_packs.py](tools/install_ra8p1_packs.py) |
| Refresh installed support resources | [tools/refresh_fsp_candidate.py](tools/refresh_fsp_candidate.py) |
| Check generated-source hashes and ELF evidence | [tools/verify_ra8p1_artifacts.py](tools/verify_ra8p1_artifacts.py) |

These scripts belong to the implemented candidate. A clean challenge checkout may not contain them; use the verified API contract when implementing its automation.

## Launch EASE

Run EASE scripts through the IDE, not standalone Python: `loadModule`, `org`, `com` and `java` are host-provided objects.

```sh
ROOT="$(pwd)"
E2STUDIO="/Applications/Renesas e2 studio with RA FSP v6.6.0/Renesas e2 studio with RA FSP 6.6.0.app/Contents/MacOS/e2studio"
WORKSPACE="$ROOT/validation/ws-integration-cpu0"
PROJECT="integration_cpu0"
PROJECT_DIR="$ROOT/validation/projects/$PROJECT"

"$E2STUDIO" -nosplash -application org.eclipse.ease.listEngines
"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$WORKSPACE" -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/discover_fsp.py" 5.9.0 EK-RA8P1 \
  -vmargs -De2studio.ease.python3=/usr/bin/python3
```

Use an unused project name/location for creation. Configuration must use the workspace where that project is registered. One process owns a workspace at a time; parallel projects need separate workspaces. Coordinate pack installation/refresh separately so it does not change inputs underneath a running generation.

Use the exact GNU toolchain version ID returned by discovery. `13.2.1.arm-13-7` was observed on this host:

```sh
TOOLCHAIN_VERSION="13.2.1.arm-13-7"
"$E2STUDIO" -nosplash -application org.eclipse.ease.runScript \
  -workspace "$WORKSPACE" -engine org.eclipse.ease.lang.python.py4j.engine \
  -script "$ROOT/tools/generate_fsp_validation.py" \
  "$PROJECT" "$PROJECT_DIR" EK-RA8P1 CM85 "$TOOLCHAIN_VERSION" \
  -vmargs -De2studio.ease.python3=/usr/bin/python3
```

After creation, invoke `configure_fsp_validation.py` in the same way, with script arguments `PROJECT ABSOLUTE_PROJECT_DIR --spec ABSOLUTE_SPEC.json`. Selecting a real driver stack and compiling its generated instances/vectors is required for that driver's integration checkpoint.

## Project generation and configuration APIs

Load the modules needed by the script:

```python
loadModule("/FSP/ProjectGen")
loadModule("/FSP/Configuration")
loadModule("/FSP/Build")
```

The verified generation sequence is:

```python
generation = projectGenerationFor("ra", "5.9.0")
generation.projectName(name)
generation.projectLocation(absolute_location)
generation.board(board)
assert core in list(generation.getAvailableCores())
generation.core(core)                       # CM85 or CM33, not a BSP ID
generation.rtos("No RTOS")
generation.projectType("FLAT")
generation.template("Bare Metal - Minimal")
generation.toolchain("GNU ARM Embedded", toolchain_version)
generation.debugger("com.renesas.hardwaredebug.rz.jlink")
validation_status = generation.validate()
# Inspect status severity, children and exceptions; abort on errors.
generation_status = generation.generate(None)
# Inspect this status too, before generating content.
```

Discovery also exposes `getAvailableBoards()`, `getAvailableDevices()`, `getAvailableTemplateNames()`, `getAvailableRtosNames()`, `getAvailableToolchains(True)` and `getAvailableToolchainVersions("GNU ARM Embedded", True)` through the host scripting API. See `discover_fsp.py` for their correct receivers.

For an existing project:

```python
configuration = openDDSCConfiguration(name)
hal_threads = [t for t in configuration.getThreads() if t.isHal()]
assert len(hal_threads) == 1
thread = hal_threads[0]
assert module_id in list(thread.getCreatableModuleIds())
stack = thread.createStack(module_id)
prop = stack.getProperty(property_id)
if prop is None:
    raise ValueError("Unavailable property: " + property_id)
prop.setValue(str(value))
assert str(prop.getValue()) == str(value)
configuration.save()
generateProjectContent(name, None)
```

The current configuration helper additionally:

- Reuses a stack only when its exact module type and requested instance-name property match uniquely. Repeatedly adding the same named stack makes retries ambiguous.
- Discovers valid properties/options from the model and MDF. A serialized default, especially an empty mask, is not necessarily a legal `setValue()` input; read the current value and use an accepted option.
- Selects the unique active, named pin table. Do not assume `getPinConfigurations()[0]` is the board's active table. Pin APIs are `getComponent()`, `getConfig()`/`getPin()`, `getOptions()` and `setValue()`.
- Saves model changes before generation and prints `configuration.getProblems()`. Inspect those problems as well as build output. Fix the owning source/MDF for unresolved selections rather than editing generated `configuration.xml`, `ra/`, `ra_cfg/` or `ra_gen/`.

## Persist the core-aware debugger target

The same physical part is used for both cores. Verify these independently:

| Selection | CPU0 | CPU1 |
| --- | --- | --- |
| Physical part | `R7KA8P1KFLCAC` | `R7KA8P1KFLCAC` |
| Project Core | `CM85` | `CM33` |
| Compiler architecture | `cortex-m85` | `cortex-m33` |
| Generated BSP core | `0` | `1` |
| Debugger command | `R7KA8P1KF_CPU0` | `R7KA8P1KF_CPU1` |

The PG debugger base is **unsuffixed** `R7KA8P1KF`. The host resolver appends the core suffix. The project generator overwrites `#DeviceCommand#` from its device record during creation; an earlier `generation.customSetting()` does not reliably override it.

Correct the configuration model **after `generation.generate(None)` and before `openDDSCConfiguration()`** can cache its settings. The working lookup/class-loading sequence is:

```python
workspace_project = org.eclipse.core.resources.ResourcesPlugin.getWorkspace().getRoot().getProject(name)
project = org.eclipse.core.runtime.Platform.getAdapterManager().loadAdapter(
    workspace_project, "com.renesas.cdt.ddsc.project.IDDSCProject")
if project is None:
    raise RuntimeError("Project is not an FSP project")
loader = com.renesas.cdt.ddsc.contentgen.internal.config.DDSCProjectConfigurationLoader(project)
try:
    loader.init(None)
    if not loader.isInitialised():
        raise RuntimeError("Configuration model did not initialize")
    settings = loader.getConfigurationSettings()
    core = str(settings.getSettingString("Core"))
    bundle = org.eclipse.core.runtime.Platform.getBundle("com.renesas.cdt.ra.contentgen")
    if bundle is None:
        raise RuntimeError("RA content-generation bundle unavailable")
    contributor = bundle.loadClass(
        "com.renesas.cdt.ra.ddsc.contentgen.idesettings.RaIdeSettingsContributor").newInstance()
    resolver = contributor.getDeviceFamilyIdeSettings(project, None, False)
    target = str(resolver.getMultiCoreDebugDeviceName("R7KA8P1KF", core))
    # Validate exact part, core, architecture, pack version and expected target first.
    settings.putSetting("#DeviceCommand#", target)
    loader.save()
finally:
    loader.dispose()
```

Use `correct_ra8p1_debugger()` in the current helpers for the full validation and serialized readback checks. Direct EASE access to `DdscScriptUtils` or `RaIdeSettingsContributor` previously produced `JavaPackage`/class-loading failures. The loader and contributor are verified internal host APIs, not stable cross-version interfaces. Discovery of two cores alone does not prove their compiler flags, BSP binding, debugger selection or linker allocation.

## Build constants and acceptance

Use **`FULL_BUILD` and `CLEAN_BUILD` exported by `/FSP/Build`**. The verified host exports values **6 and 15** respectively. Do not substitute historical Eclipse numeric constants or hard-code these observed values.

```python
print(buildProject(name, CLEAN_BUILD, absolute_clean_log))
print(buildProject(name, FULL_BUILD, absolute_build_log))
```

Creation currently runs full → clean → full; reconfiguration runs clean → full. Require:

1. No Python traceback, Java exception, compiler error or failed generation status. EASE process exit zero by itself is insufficient.
2. Clean actually removes the previous ELF. Clean need not print a normal full-build summary.
3. The full build finishes with zero errors and produces a fresh expected ELF/map. Investigate warnings; the known unused LLVM manifest diagnostic is not a GNU build result.
4. Generated-source hashes, core selection and ELF ranges pass the artifact checker. Include the companion ELF for dual-core overlap checks.

Choose a complete ARM compiler with libc headers. The debugger-bundled compiler encountered during the session lacked `assert.h`/`string.h`. Probe required headers and both CPU targets before running a large compile matrix; retain the compiler path/version in the evidence.

## Same-version candidate updates: three separate layers

Replacing bytes inside a `5.9.0` pack does not invalidate every cache. Diagnose which layer is stale before retrying generation:

| Layer | What to verify | Remedy |
| --- | --- | --- |
| Installed `.pack` | Archive SHA-256 equals the candidate manifest | Install the verified candidate into the actual pack-manager directory |
| Extracted support resources | MDFs, board profiles, device/zone/pin/linker resources equal their pack entries | Scoped support-file refresh |
| Project sources | Selected component files in project `ra/` equal current pack entries | Fresh isolated project, or verified historical-source removal followed by configurator extraction |

### Update procedure

1. Preserve the previous candidate manifest **and archive bytes** before rebuilding into `validation/packs`. The current generation helpers read that location. Keep the prior snapshot under a distinct name for ownership checks and rollback.
2. Build the candidate and validate its manifest/pack hashes. Discover the real installed pack directory; do not guess a root-owned directory under `/Applications`. Inside EASE it is available as:

   ```python
   manager = com.renesas.cdt.ddsc.packs.io.PackIOManagerFactory.getInstance().getPackManager("ra")
   print(str(manager.getPacksFolder().toOSString()))
   ```

3. Run `install_ra8p1_packs.py --root ROOT --destination PACK_DIR --candidate ROOT/validation/packs` as a dry run. For an authorized installation add `--apply`. Retain its destination-specific rollback journal under `validation/pack-install-backup/`.
4. Refresh support resources through EASE using `refresh_fsp_candidate.py ABSOLUTE_CANDIDATE_DIR ABSOLUTE_PACK_DIR`. It selects manifest-listed, hash-matched candidate packs through the pack manager. Its verified internal `checkAndInstallInternal` and single-file `extractPackSupportFile` calls are host-version-dependent; fail if the expected signatures are absent.
5. Confirm changed support-file bytes match the archive, including board defaults ending in **`##5.9.0##configuration.xml`**, not only ordinary MDFs ending in `##5.9.0.xml`. Retain `validation/support-file-backup/journal.json` and its saved originals. The vendor installer runs before explicit per-file journaling, so this journal alone is not a complete installation snapshot.
6. Generate a fresh isolated project for the current slice, or refresh proven historical project sources as described below. Then clean/full build and run the unchanged strict artifact checker. A stale hash is a blocker even if the ELF links successfully.

### Existing project source extraction

The inspected host `PackIOExtraction` uses **`OVERWRITE_NONE`** when the component version has not changed. `generateProjectContent(name, None)` and a clean build can therefore preserve and recompile old source bytes. The builder's additional boolean overloads are not a force-overwrite API; support-resource refresh does not replace this project-source layer.

The current helpers' `generate_candidate_content()` validates selected components against the PDSCs, candidate/installed pack hashes, source-entry hashes and paths before changing anything. An already-current file is retained. For a proven historical candidate file, it verifies a backup, removes that exact old file, lets the configurator extract its replacement, and verifies the new hash before building. Failed generation restores missing originals and blocks the build.

**Current limit:** the implementation allows only one exact historical `bsp_elc.h` fingerprint at its exact path. It is not a general overwrite facility. For future updates, preserve prior candidate archives and add only independently verified historical ownership, or use a fresh project. Unknown modified bytes must remain untouched. Do not delete all generated sources, overwrite them from `baseline/`, or relax the verifier to make extraction appear current.

## Hardware fixture prerequisites learned during integration

- A debugger reset can leave CPU1 or prior peripheral state active. Check reset scope, the current PC, breakpoints and both-core state before interpreting a new fixture. Stopping at `main` is not execution of `hal_entry`; a capture with no edges requires checking execution state first.
- Establish a known reset state between image families. Preserve the shared-clock guard when CPU1 is active; use the documented reset procedure or a user-assisted full power cycle as needed.
- WDT refresh readback can take up to four watchdog count cycles. The fixture waits five count cycles before checking reload; an immediate read is not a valid refresh test.
- `DBGSTOPCR` can freeze the selected watchdog even while the CPU runs under a debugger. During the authorized measurement, clear only that watchdog's mask, run freely, then restore the prior bit. Determine actual register-start options read-only; do not invent option macros or program options to satisfy the fixture.

## Regression references

The current [FSP build-harness tests](tools/test_fsp_build_harness.py) cover exported build kinds, fresh artifacts, core/debugger persistence, named-stack retries, active pin selection and source-refresh failure paths. [Artifact tests](tools/test_verify_ra8p1_artifacts.py) cover source identity, freshness and memory/core checks. Mocks preserve a verified contract; they do not replace a live configurator invocation after changing the host API or candidate metadata.
