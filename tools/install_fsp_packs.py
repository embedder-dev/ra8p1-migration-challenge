#!/usr/bin/env python3
"""Install manifest-listed packs or selected original release packs. Dry run by default."""
import argparse
import json
import os
from pathlib import Path
import stat
import tempfile

from fsp_manifest import digest, hash_value, read_manifest, read_zip, safe_path


def atomic_write(path, data, mode=0o644):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fsp-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def archive_packs(path, expected_hash, version):
    if not version or any(c not in "0123456789." for c in version):
        raise ValueError("Expected numeric FSP version")
    data = Path(path).read_bytes()
    if digest(data) != hash_value(expected_hash):
        raise ValueError("Release archive hash mismatch")
    entries = read_zip(data)
    prefix = "internal/projectgen/ra/packs/"
    result = {}
    for name, raw in entries.items():
        if name.startswith(prefix) and name.endswith("." + version + ".pack"):
            basename = name[len(prefix):]
            if "/" in basename:
                raise ValueError("Nested pack path in release archive")
            read_zip(raw)
            result[basename] = raw
    if not result:
        raise ValueError("No packs for requested FSP version")
    return result


def install(packs, destination, backup, apply=False):
    destination, backup = Path(destination), Path(backup)
    if destination.is_symlink() or backup.is_symlink() or not destination.is_dir():
        raise ValueError("Expected real pack directory and backup root")
    destination = destination.resolve(strict=True)
    journal_path = safe_path(backup, "journal.json")
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
        "destination": str(destination), "originals": {}}
    if journal["destination"] != str(destination):
        raise ValueError("Rollback journal belongs to another installation")
    changes = []
    for name, data in sorted(packs.items()):
        if Path(name).name != name or not name.endswith(".pack"):
            raise ValueError("Expected pack basename")
        target = safe_path(destination, name)
        previous = target.read_bytes() if target.exists() else None
        if previous == data:
            continue
        record = journal["originals"].get(name)
        if record and record.get("sha256") is not None:
            saved = safe_path(backup, record["backup"])
            if digest(saved.read_bytes()) != hash_value(record["sha256"]):
                raise ValueError("Rollback backup hash mismatch: " + name)
        elif record is None and previous is not None and safe_path(backup, name + ".original").exists():
            raise ValueError("Unexpected rollback file: " + name)
        changes.append((name, target, data, previous))
    # Preflight every destination/rollback point before publishing any pack.
    if apply:
        backup.mkdir(parents=True, exist_ok=True)
        for name, target, data, previous in changes:
            now = target.read_bytes() if target.exists() else None
            if now != previous:
                raise ValueError("Pack changed during install: " + name)
            if name not in journal["originals"]:
                record = {"sha256": digest(previous) if previous is not None else None}
                if previous is not None:
                    record["backup"] = name + ".original"
                    atomic_write(safe_path(backup, record["backup"]), previous)
                journal["originals"][name] = record
                atomic_write(journal_path, (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode())
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
            atomic_write(target, data, mode)
    return {"applied": apply, "destination": str(destination), "rollback_journal": str(journal_path),
            "changes": [{"name": n, "before_sha256": digest(p) if p is not None else None,
                         "after_sha256": digest(d)} for n, _, d, p in changes]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--archive", type=Path)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--fsp-version")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.manifest:
        path, manifest, _ = read_manifest(args.manifest)
        packs = {name: safe_path(path.parent, name).read_bytes() for name in manifest["packs"]}
    else:
        if not args.archive_sha256 or not args.fsp_version:
            parser.error("--archive requires --archive-sha256 and --fsp-version")
        packs = archive_packs(args.archive, args.archive_sha256, args.fsp_version)
    print(json.dumps(install(packs, args.destination, args.backup, args.apply), indent=2))


if __name__ == "__main__":
    main()
