"""Replay a frozen snapshot from its archived inputs/source without collection or freezing."""
import argparse
import json
from pathlib import Path
from core import store, signal_replay
from core.daily_data import ROOT, read_json
from core.stage1_data import _write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT.parents[1] / "data" / "market.db"))
    parser.add_argument("--snapshot-id", type=int)
    parser.add_argument("--bundle", help="JSON artifact reference; explicit audit bundle need not be a forward signal")
    parser.add_argument("--out", default=str(ROOT / "replay" / "last_check.json"))
    parser.add_argument("--relocation", help="restore-map.json from a verified restore; resolves old refs without altering blobs")
    args = parser.parse_args()
    if bool(args.snapshot_id) == bool(args.bundle):
        parser.error("choose exactly one of --snapshot-id or --bundle")
    if args.bundle:
        ref = read_json(args.bundle)
    else:
        row = store.get_strategy_signal(args.db, args.snapshot_id)
        if row is None:
            parser.error("snapshot not found")
        ref = row["payload"].get("replay_ref")
    result = signal_replay.replay(ref, Path(args.out).parent / "work", read_json(args.relocation) if args.relocation else None)
    _write_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
