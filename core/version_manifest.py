"""Separate rule, input, execution and presentation identities."""
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {
    "screening": ["core/strategies.py", "core/screening_engine.py", "core/analysis.py"],
    "data_features": ["core/environment.py", "core/daily_data.py", "core/price_collection.py", "core/sector_relations.py",
                      "core/sector_insights.py", "core/sector_service.py", "core/sector_sources.py",
                      "core/sector_index_sources.py", "core/sw_sector_sources.py", "pipeline/collect_sector_indices.py"],
    "execution_evaluation": ["core/strategy_lifecycle.py", "core/account_ledger.py", "core/research_evaluation.py",
                             "core/execution_evidence.py", "core/forward_validation.py",
                             "pipeline/forward_validation.py", "pipeline/account_report.py",
                             "pipeline/track_strategy_signals.py"],
    "application": ["core/strategy_service.py", "core/daily_runner.py", "web/routes/strategies.py",
                    "web/routes/portfolio.py", "static/app.js", "templates/index.html", "static/style.css"],
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime():
    packages = {}
    for name in ("pandas", "numpy", "akshare", "Flask", "requests", "beautifulsoup4", "truststore", "xlrd"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "packages": packages}


def manifest(root=ROOT):
    layers = {}
    for name, paths in LAYERS.items():
        files = {p: sha(Path(root) / p) for p in paths}
        layers[name] = {"files": files, "sha256": digest(files)}
    return {"schema": "version-layers-v1", "layers": layers, "runtime": runtime()}
