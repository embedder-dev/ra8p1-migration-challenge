"""EASE: CANDIDATE_MANIFEST INSTALLED_PACK_DIR --backup DIR [--apply].

Refresh only manifest-listed packs. Internal vendor APIs must be revalidated
after a host upgrade. Dry run is the default; it does not invoke extraction.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
from fsp_manifest import digest, read_manifest, safe_path, versioned_support_path
from install_fsp_packs import atomic_write


def refresh(api, manifest_path, installed, backup, apply=False):
    _, manifest, contents = read_manifest(manifest_path)
    installed, backup = Path(installed), Path(backup)
    for name, record in manifest["packs"].items():
        if digest(safe_path(installed, name).read_bytes()) != record["sha256"]:
            raise ValueError("Installed pack hash mismatch: " + name)
    manager = api["com"].renesas.cdt.ddsc.packs.io.PackIOManagerFactory.getInstance().getPackManager("ra")
    if Path(str(manager.getPacksFolder().toOSString())).resolve() != installed.resolve():
        raise ValueError("Requested installed directory is not this host's pack directory")
    selected = api["java"].util.ArrayList()
    names = set()
    for entry in manager.getPackInfoMap(None).entrySet():
        info = entry.getValue()
        name = Path(str(info.getFilename())).name
        if name in contents:
            if name in names:
                raise RuntimeError("Duplicate installed pack record: " + name)
            selected.add(info)
            names.add(name)
    if names != set(contents):
        raise RuntimeError("Not every candidate pack was found in the manager")
    if not apply:
        return {"applied": False, "packs": sorted(names)}
    support = manager.getPackSupportFileManager()

    def invoke(method_name, values):
        matches = [m for m in support.getClass().getDeclaredMethods()
                   if m.getName() == method_name and m.getParameterCount() == len(values)
                   and (method_name != "extractPackSupportFile"
                        or m.getParameterTypes()[3].getName() == "java.util.EnumSet")]
        if len(matches) != 1:
            raise RuntimeError("Missing or ambiguous vendor method: " + method_name)
        method = matches[0]
        method.setAccessible(True)
        args = api["java"].lang.reflect.Array.newInstance(
            api["java"].lang.Class.forName("java.lang.Object"), len(values))
        for index, value in enumerate(values):
            args[index] = value
        return method.invoke(support, args)

    # This vendor pass can create support resources before the explicit journal
    # below. Preserve a pre-refresh installation inventory for full rollback.
    invoke("checkAndInstallInternal", [selected, True, None])
    field = support.getClass().getDeclaredField("supportLocations")
    field.setAccessible(True)
    locations = field.get(support)
    flag_type = getattr(api["com"].renesas.cdt.ddsc.internal.ddsccommon.utils,
                        "CommonUnzipUtils$UnzipOverwriteFlags")
    flags = api["java"].util.EnumSet.of(flag_type.valueOf("OVERWRITE_FORCE"))
    journal_path = safe_path(backup, "journal.json")
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
    changes = []
    for info in selected:
        name = Path(str(info.getFilename())).name
        tooling = support.getToolingManifest(info.getShortName())
        for entry in invoke("getToolingManifestEntries", [tooling]):
            version = str(entry.getVersion())
            folder = locations.constructDestSupportFolder(entry.getDestPathBase(),
                        entry.getDestPathSupplementary(), info.getPackVersion(), True)
            if folder is None:
                continue
            destination = safe_path(Path(str(folder)), str(entry.getSupportFileName()))
            if not versioned_support_path(destination, version):
                continue
            source = str(entry.getPackBasedPath())
            if source not in contents[name]:
                raise RuntimeError("Support source absent from verified pack: " + source)
            expected = contents[name][source]
            previous = destination.read_bytes() if destination.exists() else None
            if previous == expected:
                continue
            key = str(destination)
            if key in journal and journal[key].get("backup") is not None:
                saved = safe_path(backup, journal[key]["backup"])
                if digest(saved.read_bytes()) != journal[key]["sha256"]:
                    raise ValueError("Support rollback backup mismatch: " + key)
            changes.append((info, source, Path(str(folder)), destination, expected, previous))
    backup.mkdir(parents=True, exist_ok=True)
    updated = []
    for info, source, folder, destination, expected, previous in changes:
        now = destination.read_bytes() if destination.exists() else None
        if now != previous:
            raise RuntimeError("Support file changed during refresh: " + str(destination))
        key = str(destination)
        if key not in journal:
            saved_name = digest(key.encode()) + ".original" if previous is not None else None
            if saved_name:
                atomic_write(safe_path(backup, saved_name), previous)
            journal[key] = {"backup": saved_name, "sha256": digest(previous) if previous is not None else None}
            atomic_write(journal_path, (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode())
        result = invoke("extractPackSupportFile", [info, source, str(folder), flags])
        if result.getResult() < 0 or not destination.is_file() or destination.read_bytes() != expected:
            raise RuntimeError("Support extraction mismatch: " + key)
        updated.append(key)
    return {"applied": True, "packs": sorted(names), "updated": updated,
            "rollback_journal": str(journal_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("installed", type=Path)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(list(sys.argv[1:]))
    print(json.dumps(refresh(globals(), args.manifest, args.installed, args.backup, args.apply), indent=2))


if __name__ == "__main__":
    main()
