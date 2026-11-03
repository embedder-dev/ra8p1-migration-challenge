"""Refresh installed candidate support resources using e2 studio's pack API.

EASE arguments: ABSOLUTE_CANDIDATE_DIRECTORY ABSOLUTE_INSTALLED_PACK_DIRECTORY.
Only manifest-listed, hash-matched 5.9 candidate packs are processed. The API
performs the extraction; this script never writes IDE plugins or 6.x resources.
"""
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

candidate = Path(sys.argv[1])
installed = Path(sys.argv[2])
manifest = json.loads((candidate / "manifest.json").read_text())
factory = com.renesas.cdt.ddsc.packs.io.PackIOManagerFactory.getInstance()
manager = factory.getPackManager("ra")
payloads = {}
for name, record in sorted(manifest["packs"].items()):
    if "/" in name or "\\" in name or "5.9.0" not in name:
        raise ValueError("Not a candidate 5.9 pack: " + name)
    path = installed / name
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("Installed candidate hash mismatch: " + name)
    payloads[name] = path.read_bytes()

# The public reinstall API operates on every installed version, while
# extractSupportFiles exports every pack to an external directory. Neither is
# scoped to this candidate. Use the same internal installer as reinstall, with
# only the hash-verified candidate entries and its force-reprocess flag.
selected = java.util.ArrayList()
for entry in manager.getPackInfoMap(None).entrySet():
    info = entry.getValue()
    name = Path(str(info.getFilename())).name
    if name in payloads:
        selected.add(info)
        print("REFRESH_CANDIDATE " + name)
if selected.size() != len(payloads):
    raise RuntimeError("Not every candidate pack was found in the manager")
support = manager.getPackSupportFileManager()
methods = [method for method in support.getClass().getDeclaredMethods()
           if method.getName() == "checkAndInstallInternal" and method.getParameterCount() == 3]
if len(methods) != 1:
    raise RuntimeError("Installed configurator has no scoped support-file installer")
method = methods[0]
method.setAccessible(True)
arguments = java.lang.reflect.Array.newInstance(java.lang.Class.forName("java.lang.Object"), 3)
arguments[0] = selected
arguments[1] = True
arguments[2] = None
method.invoke(support, arguments)

# Same-version entries are deliberately retained by the normal installer. Use
# its single-file extraction API for changed, version-specific candidate files.
# Shared unversioned resources and every non-5.9 entry remain untouched.
def invoke(method_name, values):
    matches = [m for m in support.getClass().getDeclaredMethods()
               if m.getName() == method_name and m.getParameterCount() == len(values)
               and (method_name != "extractPackSupportFile"
                    or m.getParameterTypes()[3].getName() == "java.util.EnumSet")]
    if len(matches) != 1:
        raise RuntimeError("Ambiguous vendor method: " + method_name)
    target_method = matches[0]
    target_method.setAccessible(True)
    args = java.lang.reflect.Array.newInstance(java.lang.Class.forName("java.lang.Object"), len(values))
    for index, value in enumerate(values):
        args[index] = value
    return target_method.invoke(support, args)

field = support.getClass().getDeclaredField("supportLocations")
field.setAccessible(True)
locations = field.get(support)
flag_type = getattr(com.renesas.cdt.ddsc.internal.ddsccommon.utils,
                    "CommonUnzipUtils$UnzipOverwriteFlags")
force_flag = flag_type.valueOf("OVERWRITE_FORCE")
flags = java.util.EnumSet.of(force_flag)
backup = candidate.parent / "support-file-backup"
backup.mkdir(parents=True, exist_ok=True)
journal_path = backup / "journal.json"
journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
for info in selected:
    name = Path(str(info.getFilename())).name
    archive = zipfile.ZipFile(io.BytesIO(payloads[name]))
    tooling = support.getToolingManifest(info.getShortName())
    for entry in invoke("getToolingManifestEntries", [tooling]):
        if str(entry.getVersion()) != "5.9.0":
            continue
        folder = locations.constructDestSupportFolder(entry.getDestPathBase(),
                    entry.getDestPathSupplementary(), info.getPackVersion(), True)
        if folder is None:
            continue
        destination = Path(str(folder)) / str(entry.getSupportFileName())
        if "/5.9/" not in str(destination) and not destination.name.endswith(
                ("##5.9.0.xml", "##5.9.0##configuration.xml")):
            continue
        if destination.is_symlink():
            raise RuntimeError("Refusing symlinked support file: " + str(destination))
        source = str(entry.getPackBasedPath())
        expected = archive.read(source)
        previous = destination.read_bytes() if destination.exists() else None
        if previous == expected:
            continue
        key = str(destination)
        if key not in journal:
            saved_name = hashlib.sha256(key.encode()).hexdigest() + ".original"
            journal[key] = {"backup": saved_name if previous is not None else None,
                            "sha256": hashlib.sha256(previous).hexdigest() if previous is not None else None}
            if previous is not None:
                (backup / saved_name).write_bytes(previous)
            journal_path.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n")
        result = invoke("extractPackSupportFile", [info, source, str(folder), flags])
        if result.getResult() < 0 or not destination.exists() or destination.read_bytes() != expected:
            raise RuntimeError("Support extraction mismatch: " + str(destination))
        print("UPDATED_SUPPORT " + str(destination))
    archive.close()
print("CANDIDATE_REFRESH_COMPLETE")
