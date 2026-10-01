"""Archive the explicit sh000001 reference independently of sector availability."""
import argparse
import json
import uuid
from pathlib import Path

from core import daily_data as data, strategy_lifecycle as life
from core.daily_runner import RunLock
from core.stage1_data import _write_json, load_calendar


def run(root=data.ROOT):
    root = Path(root)
    with RunLock(root):
        now = life.observation_clock()
        calendar = load_calendar(root / "calendar.json")
        if not calendar:
            raise ValueError("benchmark_requires_calendar")
        end = max(d for d in calendar["dates"] if life.observation_clock(d + "T15:00:00+08:00") <= now)
        frame = data.tencent_benchmark(end)
        work = root / "reference_benchmark" / "runs" / str(uuid.uuid4())
        response = data.archive_json(work / "response.json", frame.attrs["raw_response"],
                                     frame.attrs["source"], now.isoformat(), benchmark_symbol="sh000001")
        ref = data.archive_frame(work / "benchmark.csv", frame, frame.attrs["source"], now.isoformat(),
            as_of=str(frame["date"].iloc[-1]), benchmark_symbol="sh000001", dependencies=[response])
        report = {"status": "current" if frame["date"].iloc[-1] == end else "stale",
                  "expected_last_close": end, "as_of": ref["as_of"], "rows": len(frame), "artifact": ref,
                  "basis": "sh000001_reference_not_investable_all_A"}
        _write_json(work / "report.json", report)
        _write_json(root / "reference_benchmark" / "latest.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    args = parser.parse_args()
    print(json.dumps(run(args.root)))


if __name__ == "__main__":
    main()
