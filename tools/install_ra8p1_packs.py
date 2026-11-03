#!/usr/bin/env python3
"""Install preserved 5.9 packs or a built candidate into an explicit pack directory.

Dry-run is the default. --apply is deliberately required for host changes.
A journal records pre-install bytes (or absence) for manual rollback; later runs
never replace the original rollback point. No IDE binaries/plugins are modified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from build_ra8p1_packs import ARCHIVE_SHA256, digest, read_zip


def install(root: Path, destination: Path, candidate: Path | None, apply: bool) -> dict:
    original = (root / "reference/vendor/FSP_Packs_v5.9.0.zip").read_bytes()
    if digest(original) != ARCHIVE_SHA256:
        raise ValueError("Original archive checksum mismatch")
    archive = read_zip(original)
    packs = {Path(n).name: d for n, d in archive.items()
             if n.startswith("internal/projectgen/ra/packs/") and n.endswith(".pack")
             and "5.9.0" in Path(n).name}
    if candidate is not None:
        manifest = json.loads((candidate / "manifest.json").read_text())
        if manifest["source_archive_sha256"] != ARCHIVE_SHA256:
            raise ValueError("Candidate is not based on the preserved archive")
        # Shared BSP sources also live in older MCU packs; permit only original
        # 5.9 pack names plus the two new target packs, never installed 6.x packs.
        allowed = set(packs) | {"Renesas.RA_mcu_ra8p1.5.9.0.pack",
                                "Renesas.RA_board_ra8p1_ek.5.9.0.pack"}
        packs = {}
        for name, record in manifest["packs"].items():
            if name not in allowed:
                raise ValueError(f"Unexpected candidate pack: {name}")
            data = (candidate / name).read_bytes()
            if digest(data) != record["sha256"]:
                raise ValueError(f"Candidate checksum mismatch: {name}")
            packs[name] = data
    destination = destination.resolve(strict=True)
    backup = root / "validation/pack-install-backup" / digest(str(destination).encode())[:12]
    journal_path = backup / "journal.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
        "destination": str(destination), "originals": {}}
    if journal["destination"] != str(destination):
        raise ValueError("This rollback journal belongs to another installation")
    changes = []
    for name, data in sorted(packs.items()):
        target = destination / name
        if target.is_symlink():
            raise ValueError(f"Refusing symlinked pack: {target}")
        previous = target.read_bytes() if target.exists() else None
        if previous == data:
            continue
        changes.append({"name": name, "before_sha256": digest(previous) if previous is not None else None,
                        "after_sha256": digest(data)})
        if apply:
            backup.mkdir(parents=True, exist_ok=True)
            if name not in journal["originals"]:
                record = {"sha256": digest(previous) if previous is not None else None}
                if previous is not None:
                    saved = backup / (name + ".original")
                    if saved.exists():
                        raise ValueError(f"Unexpected existing rollback file: {saved}")
                    saved.write_bytes(previous)
                    record["backup"] = saved.name
                journal["originals"][name] = record
                journal_path.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n")
            # Publish each complete file atomically; the rollback journal exists first.
            with tempfile.NamedTemporaryFile(dir=destination, prefix=".ra8p1-", delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            os.replace(temporary, target)
    return {"applied": apply, "destination": str(destination), "changes": changes,
            "rollback_journal": str(journal_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.root.resolve(), args.destination, args.candidate, args.apply), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
