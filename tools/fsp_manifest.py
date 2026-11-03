"""Generic manifest, pack and project-source checks. Compatible with EASE Python 3.9."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import xml.etree.ElementTree as ET
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid SHA-256")
    return value


def safe_name(name):
    if not isinstance(name, str) or not name:
        raise ValueError("Empty path")
    parsed = PurePosixPath(name)
    if (parsed.is_absolute() or ".." in parsed.parts or "\\" in name or ":" in name
            or str(parsed) != name):
        raise ValueError("Unsafe path: " + name)
    return name


def safe_path(root, relative):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Symlinked root: " + str(root))
    path = root
    for part in PurePosixPath(safe_name(relative)).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlinked path: " + str(path))
    if root.resolve() not in path.resolve().parents:
        raise ValueError("Path escapes root: " + str(path))
    return path


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML entities/DTDs forbidden")
    return ET.fromstring(data)


def read_zip(data):
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        seen = set()
        for item in archive.infolist():
            name = item.filename.rstrip("/") if item.is_dir() else item.filename
            safe_name(name)
            if name in seen:
                raise ValueError("Duplicate archive path: " + name)
            seen.add(name)
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Unsupported archive entry: " + name)
            if not item.is_dir():
                files[name] = archive.read(item)
    return files


def read_manifest(path):
    path = Path(path)
    if path.is_dir():
        path = path / "manifest.json"
    if path.is_symlink():
        raise ValueError("Symlinked manifest")
    manifest = json.loads(path.read_text())
    packs = manifest.get("packs")
    if not isinstance(packs, dict) or not packs:
        raise ValueError("Manifest needs a nonempty packs inventory")
    contents = {}
    for name, record in packs.items():
        safe_name(name)
        if "/" in name or not name.endswith(".pack"):
            raise ValueError("Expected pack basename: " + name)
        if not isinstance(record, dict) or not isinstance(record.get("entries"), dict):
            raise ValueError("Missing pack entry inventory: " + name)
        raw = safe_path(path.parent, name).read_bytes()
        if digest(raw) != hash_value(record.get("sha256")):
            raise ValueError("Pack hash mismatch: " + name)
        entries = read_zip(raw)
        if set(entries) != set(record["entries"]):
            raise ValueError("Pack entry inventory mismatch: " + name)
        for entry, data in entries.items():
            if digest(data) != hash_value(record["entries"][entry]):
                raise ValueError("Pack entry hash mismatch: " + entry)
        contents[name] = entries
    return path, manifest, contents


def project_source(name):
    safe_name(name)
    if name.startswith("ra~1/"):
        return "ra/fsp/src/bsp/cmsis/Device/RENESAS/" + name[5:]
    return name if name.startswith(("ra/", "script/")) else None


def selected_sources(project, manifest, contents):
    root = xml(safe_path(project, "configuration.xml").read_bytes())
    selected = root.findall("./raComponentSelection/component")
    expected = {}
    owners = set()
    attributes = (("class", "Cclass"), ("group", "Cgroup"), ("subgroup", "Csub"),
                  ("variant", "Cvariant"), ("version", "Cversion"))
    for selection in selected:
        name = selection.findtext("originalPack")
        if name not in contents:
            continue  # Unchanged, external packs need not be in a candidate manifest.
        owners.add(name)
        entries = contents[name]
        descriptors = [data for key, data in entries.items() if key.endswith(".pdsc")]
        if len(descriptors) != 1:
            raise ValueError("Expected one PDSC: " + name)
        pdsc = xml(descriptors[0])
        matches = [node for node in pdsc.findall("./components/component") if all(
            node.get(pack_key, "") == selection.get(config_key, "")
            for config_key, pack_key in attributes)]
        if len(matches) != 1:
            raise ValueError("Selected component is not unique in pack: " + name)
        for item in matches[0].findall("./files/file"):
            source = item.get("name", "")
            if not source or source.endswith("/") or item.get("attr") == "template":
                continue
            relative = project_source(source)
            if relative is None:
                continue
            if source not in entries:
                raise ValueError("Missing component source: " + source)
            value = manifest["packs"][name]["entries"][source]
            if relative in expected and expected[relative] != value:
                raise ValueError("Conflicting source owners: " + relative)
            expected[relative] = value
    if not owners or not expected:
        raise ValueError("No selected candidate source inventory")
    return expected, owners


def refresh_project_sources(project, manifest_path, previous_manifests, installed, generate):
    """Replace only selected sources proven to belong to a prior candidate.

    All manifests and archive bytes are checked before mutation. Unknown local
    edits are preserved. The configurator, not this helper, extracts new bytes.
    """
    project = Path(project)
    _, current, contents = read_manifest(manifest_path)
    expected, owners = selected_sources(project, current, contents)
    for name in owners:
        raw = safe_path(installed, name).read_bytes()
        if digest(raw) != current["packs"][name]["sha256"]:
            raise ValueError("Installed pack hash mismatch: " + name)
    historical = {}
    for path in previous_manifests:
        _, old, old_contents = read_manifest(path)
        old_sources, _ = selected_sources(project, old, old_contents)
        for name, value in old_sources.items():
            historical.setdefault(name, set()).add(value)
    changes = []
    for name, value in sorted(expected.items()):
        path = safe_path(project, name)
        if not path.exists():
            continue  # Conditional files are selected by the configurator.
        old_value = digest(path.read_bytes())
        if old_value == value:
            continue
        if old_value not in historical.get(name, set()):
            raise ValueError("Preserving unknown local source: " + name)
        backup = safe_path(project, ".fsp-source-backup/" + old_value + "/" + name)
        if backup.exists() and digest(backup.read_bytes()) != old_value:
            raise ValueError("Source backup conflict: " + name)
        changes.append((name, path, backup, old_value))
    removed = []
    try:
        for name, path, backup, old_value in changes:
            if digest(path.read_bytes()) != old_value:
                raise ValueError("Source changed during refresh: " + name)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copyfile(path, backup)
            if digest(backup.read_bytes()) != old_value:
                raise ValueError("Source backup verification failed: " + name)
            path.unlink()
            removed.append((path, backup))
        generate()
        for name, value in expected.items():
            path = safe_path(project, name)
            if path.exists() and digest(path.read_bytes()) != value:
                raise ValueError("Stale extraction: " + name)
        for path, _ in removed:
            if not path.is_file():
                raise ValueError("Configurator did not replace removed source: " + str(path))
    except BaseException:
        for path, backup in removed:
            if not path.exists() and not path.is_symlink():
                shutil.copyfile(backup, path)
        raise
    return {"checked_sources": len(expected), "refreshed_sources": [row[0] for row in changes]}


def versioned_support_path(path, version):
    """Keep explicit refresh away from unversioned/shared support files."""
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[+.-][A-Za-z0-9_.+-]+)?", version):
        return False
    short = ".".join(version.split(".")[:2])
    return (version in path.parent.parts or short in path.parent.parts or
            path.name.endswith(("##" + version + ".xml", "##" + version + "##configuration.xml")))
