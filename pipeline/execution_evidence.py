"""Import sourced execution evidence or audit current coverage; never submit orders."""
import argparse
import json

from core import execution_evidence as evidence, daily_data as data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--import-file")
    args = parser.parse_args()
    if args.import_file:
        print(json.dumps({"import_ref": evidence.import_bundle(args.root, data.read_json(args.import_file))}))
    rows, refs = evidence.load(args.root)
    print(json.dumps({"packages": len(refs), "counts": {k: len(v) for k, v in rows.items()},
                      "missing_policy": "unknown_not_inferred_from_daily_bars"}))


if __name__ == "__main__":
    main()
