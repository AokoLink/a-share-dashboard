"""Small read-only report views. Full audit files remain unchanged."""


def run_summary(report):
    fields = ("run_id", "day", "status", "started_at", "finished_at", "generated_at",
              "resumed_at", "effective_at", "source_mode", "check_only", "observe_only",
              "notice", "report")
    view = {key: report[key] for key in fields if key in report}
    view.update(view="summary", read_only=True)
    step_fields = ("status", "started_at", "finished_at", "input_date", "elapsed_seconds",
                   "attempt", "reason", "error")
    view["steps"] = {name: {key: step[key] for key in step_fields if key in step}
                     for name, step in report.get("steps", {}).items()}
    for name, step in report.get("steps", {}).items():
        result = step.get("result", {})
        if isinstance(result, dict):
            if result.get("reason") and not view["steps"][name].get("reason"):
                view["steps"][name]["reason"] = result["reason"]
            errors = result.get("errors", [])
            if isinstance(errors, list) and errors:
                view["steps"][name]["error_count"] = len(errors)
                view["steps"][name]["error_sample"] = [
                    {key: str(item[key])[:500] for key in ("source", "error", "reason") if key in item}
                    for item in errors[:3] if isinstance(item, dict)]
    # Latest reports have coverage; full run manifests keep it in quality results.
    saved = report.get("steps", {}).get("report", {}).get("result", {})
    if not isinstance(saved, dict):
        saved = {}
    quality = report.get("steps", {}).get("quality", {}).get("result", {})
    coverage = report.get("coverage", saved.get("coverage", quality.get("coverage", {})))
    view["coverage"] = {}
    for role, values in coverage.items():
        item = {key: val for key, val in values.items() if isinstance(val, (int, float, bool, type(None)))}
        for kind in ("raw", "technical", "membership"):
            item[kind] = {key: val for key, val in values.get(kind, {}).items()
                          if isinstance(val, (int, float, bool, type(None)))}
        view["coverage"][role] = item
    stability = report.get("stability", saved.get("stability", {}))
    view["stability"] = {key: val for key, val in stability.items()
                         if isinstance(val, (str, int, float, bool, type(None)))}
    if "price_coverage" in stability:
        view["stability"]["price_coverage"] = stability["price_coverage"]
    return view


def health_page(report, page, limit, failed):
    codes = sorted(report.get("codes", {}).items())
    if failed:
        codes = [(code, pair) for code, pair in codes
                 if any(row["status"] != "success" or not row["current"] for row in pair.values())]
    return {**{key: val for key, val in report.items() if key != "codes"},
            "codes": dict(codes[(page - 1) * limit:page * limit]), "total_codes": len(codes),
            "page": page, "limit": limit, "read_only": True}


def candidate_view(report):
    return {key: value for key, value in report.items()
            if key not in {"screening_decision", "input_manifest", "code_ref", "version_manifest"}}
