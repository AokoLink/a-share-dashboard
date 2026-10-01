"""Content-addressed inputs and source files for offline, isolated signal replay."""
import json
import base64
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from core import version_manifest as versions

ROOT = Path(__file__).resolve().parents[1]


def encode(value):
    if isinstance(value, (pd.Timestamp,)):
        return {"__timestamp__": value.isoformat()}
    if isinstance(value, np.generic):
        return encode(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return {"__float__": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, dict):
        return {str(k): encode(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"__timestamp__"}:
            return pd.Timestamp(value["__timestamp__"])
        if set(value) == {"__float__"}:
            return float(value["__float__"])
        return {k: decode(v) for k,v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def frames(values):
    return {c: None if f is None else encode({"columns": list(f.columns), "data": f.to_numpy().tolist(),
             "dtypes": {str(k): str(v) for k,v in f.dtypes.items()}}) for c,f in values.items()}


def restore_frames(values):
    out = {}
    for code, value in values.items():
        if value is None:
            out[code] = None
            continue
        value = decode(value)
        frame = pd.DataFrame(value["data"], columns=value["columns"])
        for column, dtype in value["dtypes"].items():
            frame[column] = frame[column].astype(dtype)
        out[code] = frame
    return out


def immutable(path, value):
    content = versions.canonical(value).encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("immutable_replay_archive_collision")
    else:
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    return {"path": str(path.resolve()), "sha256": versions.sha(path)}


def capture(root, *, pool, technical, execution, regime, now, sector_snapshot,
            decision, groups, status_events, source="frozen_signal", memberships=None):
    root = Path(root).resolve()
    # Archive the complete local core package, not only hashes of current source paths.
    code = {str(p.relative_to(ROOT)).replace("\\", "/"): base64.b64encode(p.read_bytes()).decode("ascii")
            for p in sorted((ROOT / "core").glob("*.py"))}
    code.update({str(p.relative_to(ROOT)).replace("\\", "/"): base64.b64encode(p.read_bytes()).decode("ascii")
                 for p in sorted((ROOT / "pipeline").glob("*.py"))})
    for paths in versions.LAYERS.values():
        for name in paths:
            code[name] = base64.b64encode((ROOT / name).read_bytes()).decode("ascii")
    code_ref = immutable(root / "code" / (versions.digest(code) + ".json"), code)
    inputs = encode({"pool": pool, "technical": frames(technical), "execution": frames(execution),
                     "regime": regime, "now": now.isoformat(), "sector_snapshot": sector_snapshot,
                     "status_events": status_events, "memberships": memberships or {}})
    inputs_ref = immutable(root / "inputs" / (versions.digest(inputs) + ".json"), inputs)
    bundle = {"schema": "signal-replay-v1", "source": source, "code": code_ref, "inputs": inputs_ref,
              "versions": versions.manifest(), "expected": encode({"decision": decision, "groups": groups})}
    ref = immutable(root / "bundles" / (versions.digest(bundle) + ".json"), bundle)
    return {**ref, "dependencies": [code_ref, inputs_ref]}


BOOTSTRAP = '''
import json, sys, socket
from pathlib import Path
def no_network(*args, **kwargs):
    raise RuntimeError("network_disabled_during_signal_replay")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
sys.path.insert(0, sys.argv[1])
from core import signal_replay, store, strategy_service, strategy_lifecycle
value = signal_replay.decode(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
db = Path(sys.argv[1]) / "isolated.sqlite"
store.init_db(str(db))
for event in value["status_events"]:
    store._connect(str(db)).execute("INSERT INTO strategy_status_event(strategy,version,status,reason,changed_at) VALUES(?,?,?,?,?)",
        (event["strategy"],event["version"],event["status"],event.get("reason","replay"),event["changed_at"]))
store._connect(str(db)).commit()
result = strategy_service.generate(str(db),value["pool"],signal_replay.restore_frames(value["technical"]),
    signal_replay.restore_frames(value["execution"]),value["memberships"],value["regime"],
    strategy_lifecycle.observation_clock(value["now"]),allow_freeze=False,sector_snapshot=value["sector_snapshot"])
Path(sys.argv[3]).write_text(json.dumps(signal_replay.encode({"decision": result["screening_decision"],"groups":result["groups"]}),
    ensure_ascii=False,sort_keys=True,allow_nan=False),encoding="utf-8")
'''


def verified(ref, relocation=None):
    path = Path(ref["path"])
    for original, target in sorted((relocation or {}).items(), key=lambda pair: -len(pair[0])):
        if path.resolve().is_relative_to(Path(original).resolve()):
            path = Path(target) / path.resolve().relative_to(Path(original).resolve())
            break
    if versions.sha(path) != ref["sha256"]:
        raise ValueError("replay_artifact_hash_mismatch")
    return json.loads(path.read_text(encoding="utf-8"))


def replay(ref, work_root, relocation=None):
    if not ref:
        return {"status": "legacy_missing_input_and_code_bundle", "identical": False}
    bundle = verified(ref, relocation)
    if bundle.get("schema") != "signal-replay-v1":
        raise ValueError("unsupported_replay_bundle")
    code, inputs = verified(bundle["code"], relocation), verified(bundle["inputs"], relocation)
    if versions.runtime() != bundle["versions"]["runtime"]:
        return {"status": "runtime_dependency_mismatch", "identical": False,
                "required": bundle["versions"]["runtime"], "actual": versions.runtime()}
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="replay-", dir=work_root) as work:
        work = Path(work)
        for name, content in code.items():
            target = (work / name).resolve()
            if (not target.is_relative_to(work.resolve()) or target.suffix not in (".py", ".html", ".js", ".css")
                or not name.startswith(("core/", "pipeline/", "web/", "templates/", "static/"))):
                raise ValueError("invalid_archived_source_path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(content, validate=True))
        (work / "inputs.json").write_text(versions.canonical(inputs), encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", "-c", BOOTSTRAP, str(work.resolve()),
            str((work / "inputs.json").resolve()), str((work / "output.json").resolve())],
            cwd=work, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode:
            return {"status": "archived_replay_failed", "identical": False, "error": result.stderr[-2000:]}
        actual = json.loads((work / "output.json").read_text(encoding="utf-8"))
    equal = versions.digest(actual) == versions.digest(bundle["expected"])
    return {"status": "identical" if equal else "decision_or_candidate_difference", "identical": equal,
            "expected_sha256": versions.digest(bundle["expected"]), "actual_sha256": versions.digest(actual),
            "network": "disabled", "freeze": "disabled", "source": bundle["source"]}
