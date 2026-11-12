"""补算历史研究候选，绝不补记为当日实时信号。"""
import argparse
import json
from core.candidate_recovery import run
from core.daily_data import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--resume")
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("workers must be between 1 and 16")
    result = run(args.root, args.date, workers=args.workers, resume=args.resume)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
