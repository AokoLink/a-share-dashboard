"""SQLite online backup plus immutable artifact copies; restore only into a new directory."""
import argparse
import hashlib
import json
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

from core import version_manifest as versions
from core.daily_data import ROOT
from core.stage1_data import _write_json


def inspect_db(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("database_integrity_failed")
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}


def backup(db, root=ROOT, out=None):
    from core.daily_runner import RunLock
    with RunLock(root):
        return _backup(db, root, out)


def _backup(db, root, out):
    db, root = Path(db).resolve(), Path(root).resolve()
    out = Path(out or root.parent / "backups").resolve()
    out.mkdir(parents=True, exist_ok=True)
    archive = out / (str(uuid.uuid4()) + ".zip")
    roots = {"daily": root, "signals": db.parent / "signal_replay",
             "experiments": root.parent / "strategy_experiments", "verification": root.parent / "account_verification"}
    if any(out.is_relative_to(folder.resolve()) for folder in roots.values()):
        raise ValueError("backup_output_must_be_outside_input_roots")
    entries, relocations = {}, {}
    with tempfile.TemporaryDirectory(prefix="db-backup-", dir=out) as tmp:
        copied_db = Path(tmp) / "market.db"
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(copied_db)) as target:
            source.backup(target)
        counts = inspect_db(copied_db)
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as zip_:
            def add(name, path):
                content = path.read_bytes()
                zip_.writestr(name, content)
                entries[name] = hashlib.sha256(content).hexdigest()
            add("database/market.db", copied_db)
            for label, folder in roots.items():
                if not folder.exists():
                    continue
                relocations[str(folder)] = label
                for path in sorted(folder.rglob("*")):
                    if path.is_file() and path.name != ".run.lock":
                        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                            raise ValueError("backup_external_symlink_rejected")
                        add(label + "/" + path.relative_to(folder).as_posix(), path)
            for folder in ("core", "pipeline", "web", "templates", "static"):
                for path in sorted((versions.ROOT / folder).rglob("*")):
                    if path.is_file() and path.suffix in (".py", ".js", ".css", ".html"):
                        add("source/" + path.relative_to(versions.ROOT).as_posix(), path)
            add("source/requirements.txt", versions.ROOT / "requirements.txt")
            if (versions.ROOT / "requirements.lock.txt").exists():
                add("source/requirements.lock.txt", versions.ROOT / "requirements.lock.txt")
            manifest = {"schema": "workspace-backup-v1", "files": entries, "database_counts": counts,
                        "original_roots": relocations, "versions": versions.manifest(),
                        "limits": ["daily_task_locked_during_backup", "legacy_pkl_data_and_git_history_not_included"]}
            zip_.writestr("manifest.json", versions.canonical(manifest))
    ref = {"path": str(archive), "sha256": versions.sha(archive), "files": len(entries), "database_counts": counts}
    _write_json(archive.with_suffix(".json"), ref)
    return ref


def restore(archive, target):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError("restore_target_must_be_new_no_overwrite")
    with zipfile.ZipFile(archive) as zip_:
        names = zip_.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate_backup_paths")
        manifest = json.loads(zip_.read("manifest.json"))
        if manifest.get("schema") != "workspace-backup-v1" or set(names) != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("invalid_backup_manifest")
        for name, expected in manifest["files"].items():
            if not (target / name).resolve().is_relative_to(target) or "\\" in name:
                raise ValueError("invalid_restore_path")
            if hashlib.sha256(zip_.read(name)).hexdigest() != expected:
                raise ValueError("backup_content_hash_failed")
        target.mkdir(parents=True)
        for name in manifest["files"]:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zip_.read(name))
    counts = inspect_db(target / "database" / "market.db")
    if counts != manifest["database_counts"]:
        raise ValueError("restored_record_counts_differ")
    relocation = {original: str(target / relative) for original,relative in manifest["original_roots"].items()}
    _write_json(target / "restore-map.json", relocation)
    result = {"status": "verified", "files": len(manifest["files"]), "database_counts": counts,
              "target": str(target), "relocation": str(target / "restore-map.json")}
    _write_json(target / "restore-check.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT.parents[1] / "data" / "market.db"))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out")
    parser.add_argument("--restore")
    parser.add_argument("--target")
    args = parser.parse_args()
    if args.restore and not args.target:
        parser.error("restore requires an explicit new --target directory")
    result = restore(args.restore, args.target) if args.restore else backup(args.db, args.root, args.out)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
