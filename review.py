# -*- coding: utf-8 -*-
"""自动复盘 + 优化建议(只读)。

消费 evaluate.py build_report 的 JSON payload,产出:
1. 复盘:弱项(显著跑输总体的层/环境)+ 无 edge 维度 + 退化/薄层警示。
2. 优化建议:数据锚定候选(附验证配方),无数据支撑则零建议。

只读:不 import 下划线私有、不改任何模块常量、不应用任何改动、不 fetch。
建议是提案:应用/回测/版本化由 #129 版本管理执行。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import environment as env
import evaluate as ev

MODULE_VERSION = "1.0.0"


def _binom_diff_se(p_l, n_l, p_o, n_o):
    """两二项比例差标准误(镜像 evaluate._binom_diff_se,无 numpy)。"""
    if p_l is None or p_o is None or not n_l or not n_o:
        return None
    sl = (p_l * (1.0 - p_l) / n_l) ** 0.5
    so = (p_o * (1.0 - p_o) / n_o) ** 0.5
    return (sl * sl + so * so) ** 0.5


def _effective_n(dim, m):
    """主指标有效样本量(镜像 evaluate._effective_n)。"""
    metric = ev.PRIMARY[dim]
    if metric == "hit_rate":
        return m["n"] - m.get("n_hold", 0)
    if metric == "sign_agreement":
        return m["sign_n"]
    return m["n"]  # acc_path (path) / adverse_rate (risk)


def _baseline(dim, cell):
    """无信息基线:dir/gap/trend3 用 cell base_rate;path 0.25;return 0.5;risk None。"""
    if dim in ("direction", "gap", "trend3"):
        return cell.get("base_rate")
    if dim == "path":
        return 0.25
    if dim == "return":
        return 0.5
    return None


def _overall_summary(payload):
    """逐维主指标 + 无信息基线 + edge = value - baseline。"""
    rows = []
    for dim in ev.DIMS:
        o = payload["overall"][dim]
        value = o.get(ev.PRIMARY[dim])
        baseline = _baseline(dim, o)
        edge = None
        if value is not None and baseline is not None:
            edge = value - baseline
        rows.append({"dim": dim, "primary": ev.PRIMARY[dim], "n": o.get("n"),
                     "value": value, "baseline": baseline, "edge": edge})
    return rows


def _no_edge_dims(payload):
    """主指标在 1σ 内 ≈ 无信息基线的维度列表;risk 不做无 edge 判定。"""
    out = []
    for dim in ev.DIMS:
        if dim == "risk":
            continue
        o = payload["overall"][dim]
        value = o.get(ev.PRIMARY[dim])
        baseline = _baseline(dim, o)
        n_eff = _effective_n(dim, o)
        if value is None or baseline is None or not n_eff:
            continue
        se = (baseline * (1.0 - baseline) / n_eff) ** 0.5
        if abs(value - baseline) <= se:
            out.append(dim)
    return out


def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


KNOBS = [
    {"name": "verdict_thresholds",
     "locator": {"module": "analysis.py", "func": "stock_verdict", "note": "内联字面量"},
     "current": "67/62/52/42", "controls": "个股五档 verdict 分界",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P2 定稿 unchanged;live 单日分布标定,板块 composite 历史不可复现(R4 缺口);机械重锚 [56,51,41,31] 已被否决"},
    {"name": "sector_bonus_tiers",
     "locator": {"module": "recommend.py", "func": "sector_bonus", "note": "内联字面量"},
     "current": "68/60/50 -> +8/+4/-5", "controls": "板块共振加成档",
     "evidence_status": "backtest-calibrated",
     "validation_recipe": "live §9.4 重锚;-5 边界刻意不对称;板块 composite 历史不可复现(R4 缺口),历史验证不可能"},
    {"name": "HOT_COMPOSITE_THRESHOLD",
     "locator": {"module": "recommend.py", "attr": "HOT_COMPOSITE_THRESHOLD"},
     "current": "68.0", "controls": "热板块谓词门槛",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P0b 定稿维持 68;P3 确认 BONUS_GE75=False(不提到 75)"},
    {"name": "HOT_WEIGHT_MODE",
     "locator": {"module": "recommend.py", "attr": "HOT_WEIGHT_MODE"},
     "current": "signal", "controls": "热权重模式 signal/rel_strength/off",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P0b 控制器裁定 signal(树退化为 any-nominal-b>a);有限可逆,不得自动翻转"},
    {"name": "V3_WEIGHTS",
     "locator": {"module": "analysis.py", "attr": "V3_WEIGHTS"},
     "current": "(0.55, 0.15, 0.10, 0.20)", "controls": "默认股票质量权重(position/vp/trend/signal)",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑 evaluate 对比(未单独标定;P0b 显示板块内选股负 alpha,权重本身未改)"},
    {"name": "HOT_SIGNAL_WEIGHTS",
     "locator": {"module": "analysis.py", "attr": "HOT_SIGNAL_WEIGHTS"},
     "current": "(0.40, 0.15, 0.10, 0.35)", "controls": "signal 模式热权重",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P0b 定稿(弱胜 +0.011pp,~0.08 SE,控制器裁定)"},
    {"name": "HOT_REL_WEIGHTS",
     "locator": {"module": "analysis.py", "attr": "HOT_REL_WEIGHTS"},
     "current": "(0.40, 0.15, 0.10, 0.20)", "controls": "rel_strength 模式热权重",
     "evidence_status": "provisional",
     "validation_recipe": "P0b 未选(rel_strength 多年 delta≈0.000pp);仅休眠代码路径"},
    {"name": "BONUS_GE75",
     "locator": {"module": "recommend.py", "attr": "BONUS_GE75"},
     "current": "False", "controls": "+8 门槛提到 >=75",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P3 定稿 False(>=75 桶 n=0,无数据)"},
    {"name": "BONUS_QUALITY_GATE",
     "locator": {"module": "recommend.py", "attr": "BONUS_QUALITY_GATE"},
     "current": "False", "controls": "quality<50 不给加成",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P3 定稿 False(仅非空桶 n=20 内 3/17,均<20;代理口径错配使探针无效)"},
    {"name": "PRICE_FLOOR",
     "locator": {"module": "recommend.py", "attr": "PRICE_FLOOR"},
     "current": "None", "controls": "低价候选排除(None=关)",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P4 定稿 None(低价<3元反而 +0.400% vs >=10 +0.161%,t=+3.17,否决惩罚假设)"},
    {"name": "OVERHEAT_MIN_DAYS",
     "locator": {"module": "analysis.py", "attr": "OVERHEAT_MIN_DAYS"},
     "current": "4", "controls": "过热连涨天数(徽章)",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P0 定稿 badge,P0_PATH=badge 下 N 生产不消费"},
    {"name": "P0_PATH",
     "locator": {"module": "analysis.py", "attr": "P0_PATH"},
     "current": "badge", "controls": "过热处理 intercept(排除)/badge(仅徽章)",
     "evidence_status": "manual-ruling",
     "validation_recipe": "P0 定稿 badge(过热方向支持但 Welch t>=0.076 不显著)"},
    {"name": "MIN_AMOUNT",
     "locator": {"module": "recommend.py", "attr": "MIN_AMOUNT"},
     "current": "1e8 (1亿 CNY)", "controls": "流动性下限",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "BIG_DROP_PCT",
     "locator": {"module": "recommend.py", "attr": "BIG_DROP_PCT"},
     "current": "-7.0", "controls": "大跌排除线(非对称跌停)",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "QUALIFYING_VERDICTS",
     "locator": {"module": "recommend.py", "attr": "QUALIFYING_VERDICTS"},
     "current": "('建议关注', '跟踪(热点延续)')", "controls": "sector_bonus 资格的 verdict 集",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "risk_exclude",
     "locator": {"module": "recommend.py", "func": "rank_candidates", "note": "内联字面量 risk<70"},
     "current": "70", "controls": "risk>=70 候选排除",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "GAP_WARN_PCT",
     "locator": {"module": "static/app.js", "attr": "GAP_WARN_PCT", "note": "前端常量"},
     "current": "-0.682", "controls": "前端隔夜跳空警示阈值",
     "evidence_status": "backtest-calibrated",
     "validation_recipe": "P1 定稿:机械 max(P20, mean-1σ) = P20 -0.682%(替换暂定 -1.5)"},
    {"name": "N_BINS",
     "locator": {"module": "predict.py", "attr": "N_BINS"},
     "current": "10", "controls": "校准等量分箱数(composite->P(up) / risk ECE)",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "DIRECTION_BAND",
     "locator": {"module": "predict.py", "attr": "DIRECTION_BAND"},
     "current": "0.05", "controls": "ε-band 方向 hold 区(|P-0.5|<=band 观望)",
     "evidence_status": "provisional",
     "validation_recipe": "对比不同 band 下 up/down 调用数 vs 命中率,无提升则维持"},
    {"name": "ADVERSE_THRESHOLD",
     "locator": {"module": "predict.py", "attr": "ADVERSE_THRESHOLD"},
     "current": "-0.03", "controls": "风险 adverse 定义(close1 < -3%)",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "TRAIN_FRAC",
     "locator": {"module": "predict.py", "attr": "TRAIN_FRAC"},
     "current": "0.8", "controls": "训练/valid 时间切分比例",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定)"},
    {"name": "EVAL_DAYS",
     "locator": {"module": "predict.py", "attr": "EVAL_DAYS"},
     "current": "1200", "controls": "每只截尾 1200 根评估窗",
     "evidence_status": "provisional",
     "validation_recipe": "held-out 重跑对比(未标定;与 backtest.py 共享)"},
    {"name": "environment_thresholds",
     "locator": {"module": "environment.py", "attr": "PANIC_R5 等 15 常量"},
     "current": "见 environment.py:26-39", "controls": "七态分类阈值(优先级决策树)",
     "evidence_status": "provisional",
     "validation_recipe": "environment_calibration.md 已锚分布,明确不自动调参"},
    {"name": "MIN_STATE_N",
     "locator": {"module": "environment.py", "attr": "MIN_STATE_N"},
     "current": "20", "controls": "环境态 n<20 标退化",
     "evidence_status": "provisional",
     "validation_recipe": "设计常量"},
    {"name": "layer_predicates",
     "locator": {"module": "evaluate.py", "attr": "TOP_SECTORS 等 6 常量"},
     "current": "TOP_SECTORS=3/AMOUNT_TOP_FRAC=0.20/HIGH_POS_RATIO=0.97/OVERSOLD_RET=-0.15/AMP_THRESHOLD=15.0/MIN_LAYER_N=30",
     "controls": "八层谓词定义",
     "evidence_status": "provisional",
     "validation_recipe": "设计常量;undifferentiated 时触发分层定义重审"},
]


def _layer_weak_strong(payload):
    """读 significant(覆盖 9 个 dim/metric 组合),按 metric == PRIMARY[dim] 过滤。"""
    weak, strong = [], []
    se_map = {(L, dim): se for L, dim, se in payload["se_bounds"]}
    for row in payload["significant"]:
        L, dim, metric, v_l, v_o, _ = row
        if metric != ev.PRIMARY[dim]:
            continue
        cell = payload["layers"][L][dim]
        entry = {"layer": L, "dim": dim, "value": v_l, "overall": v_o,
                 "se": se_map.get((L, dim)), "n": cell.get("n")}
        (weak if v_l < v_o else strong).append(entry)
    return weak, strong


def _env_weak_strong(payload):
    """环境显著性 payload 不含,自算(二项比例差,镜像 evaluate._significance)。"""
    weak, strong = [], []
    for state in env.LABELS:
        for dim in ev.DIMS:
            cell = payload["environments"][state][dim]
            if cell.get("_suppressed"):
                continue
            overall = payload["overall"][dim]
            p_l = cell.get(ev.PRIMARY[dim])
            p_o = overall.get(ev.PRIMARY[dim])
            n_l = _effective_n(dim, cell)
            n_o = _effective_n(dim, overall)
            if p_l is None or p_o is None or n_l < ev.MIN_LAYER_N or n_o <= 0:
                continue
            se = _binom_diff_se(p_l, n_l, p_o, n_o)
            if se is None:
                continue
            if abs(p_l - p_o) > 2.0 * se:
                entry = {"env": state, "dim": dim, "value": p_l, "overall": p_o,
                         "se": 2.0 * se, "n": cell.get("n")}
                (weak if p_l < p_o else strong).append(entry)
    return weak, strong


def _degenerate_thin(payload):
    """退化环境(env_n < MIN_STATE_N)与薄层(layer_n < MIN_LAYER_N)。"""
    degenerate = [s for s in env.LABELS if payload["env_n"].get(s, 0) < env.MIN_STATE_N]
    thin = [L for L in ev.LAYERS if payload["layer_n"].get(L, 0) < ev.MIN_LAYER_N]
    return degenerate, thin


def review(payload):
    """把复盘发现编排为结构化 dict。"""
    weak_layers, strong_layers = _layer_weak_strong(payload)
    weak_envs, strong_envs = _env_weak_strong(payload)
    degenerate, thin = _degenerate_thin(payload)
    return {
        "overall_summary": _overall_summary(payload),
        "weak_layers": weak_layers,
        "strong_layers": strong_layers,
        "weak_environments": weak_envs,
        "strong_environments": strong_envs,
        "no_edge_dims": _no_edge_dims(payload),
        "undifferentiated": bool(payload.get("all_undifferentiated")),
        "degenerate_environments": degenerate,
        "thin_layers": thin,
    }


def suggest(payload, rv):
    """数据锚定优化建议(R1-R5);无命中 → 空表。不产出 R6(薄层/退化已在 review 字段)。"""
    out = []

    # R1 层反信号
    for w in rv["weak_layers"]:
        dim = w["dim"]
        cell = payload["layers"][w["layer"]][dim]
        baseline = _baseline(dim, cell)
        if baseline is None or w["value"] is None:
            continue
        if w["value"] < baseline:  # 反信号(hit_rate < base_rate 或 acc_path < 0.25)
            out.append({
                "id": f"r1-{w['layer']}-{dim}", "kind": "layer_antisignal",
                "knob": None, "current": "分层照常纳入", "proposed": "该层降权或排除",
                "evidence": f"{w['layer']}层 {dim} 维 {ev.PRIMARY[dim]}={w['value']:.3f} "
                            f"< 基线 {baseline:.3f}(n={w['n']}, 2σ 界 {w['se']:.3f})",
                "validation_recipe": "held-out 重跑 evaluate,看该层反信号是否跨期稳定",
                "confidence": "med", "requires_probe": False})

    # R2 环境反信号/无优势(方向维)
    for w in rv["weak_environments"]:
        if w["dim"] != "direction":
            continue
        cell = payload["environments"][w["env"]]["direction"]
        base = cell.get("base_rate")
        if base is None or w["value"] is None or w["value"] >= base:
            continue
        out.append({
            "id": f"r2-{w['env']}-direction", "kind": "environment_antisignal",
            "knob": "DIRECTION_BAND", "current": "0.05",
            "proposed": "该环境更保守(加宽 ε-band 或加警示徽章)",
            "evidence": f"{w['env']}环境 direction hit_rate={w['value']:.3f} "
                        f"< base_rate={base:.3f}(n={w['n']}, 2σ 界 {w['se']:.3f})",
            "validation_recipe": "held-out 重跑 evaluate,看该环境反信号是否跨期稳定",
            "confidence": "med", "requires_probe": False})

    # R3 方向维无区分度
    if "direction" in rv["no_edge_dims"]:
        o = payload["overall"]["direction"]
        out.append({
            "id": "r3-direction-no-edge", "kind": "direction_no_edge",
            "knob": "DIRECTION_BAND", "current": "0.05",
            "proposed": "加宽 DIRECTION_BAND 减少无信息下注(或接受为结论)",
            "evidence": f"direction hit_rate={o.get('hit_rate'):.3f} ≈ "
                        f"base_rate={o.get('base_rate'):.3f}(n={o.get('n')}, 1σ 内)",
            "validation_recipe": "对比不同 DIRECTION_BAND 下 up/down 调用数 vs 命中率,无提升则维持",
            "confidence": "high", "requires_probe": False})

    # R4 风险校准偏离
    risk = payload["overall"]["risk"]
    if risk.get("ece") is not None and risk["ece"] > 0.10:
        out.append({
            "id": "r4-risk-ece", "kind": "risk_miscalibration",
            "knob": "N_BINS", "current": "10",
            "proposed": "重审风险分箱或校准样本",
            "evidence": f"risk ece={risk['ece']:.3f} > 0.10(n={risk.get('n')})",
            "validation_recipe": "需 predict 内部样本复核分箱(requires_probe)",
            "confidence": "low", "requires_probe": True})

    # R5 收益系统性偏估
    ret = payload["overall"]["return"]
    if ret.get("mean_residual") is not None and abs(ret["mean_residual"]) > 0.02:
        out.append({
            "id": "r5-return-bias", "kind": "return_bias",
            "knob": None, "current": "无偏移校准", "proposed": "加偏移校准",
            "evidence": f"return mean_residual={ret['mean_residual']:.3f} "
                        f"(|.| > 0.02, n={ret.get('n')})",
            "validation_recipe": "需 predict 内部样本复核(requires_probe)",
            "confidence": "low", "requires_probe": True})

    return out


def build_report(payload, rv, suggestions, in_path):
    return {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "review",
        "source": {
            "path": in_path,
            "evaluate_system_version": payload.get("system_version"),
            "valid_window": payload.get("valid_window"),
            "n_valid_records": payload.get("n_valid_records"),
        },
        "review": rv,
        "suggestions": suggestions,
        "knobs": KNOBS,
    }


def _fmt(x, nd=4):
    return "-" if x is None else f"{x:.{nd}f}"


def render_markdown(report):
    rv = report["review"]
    L = ["# 自动复盘 + 优化建议(review)", ""]
    L.append(f"- module_version: {report['module_version']}")
    L.append(f"- system_version: `{report['system_version']}`")
    L.append(f"- 源评估报告: {report['source']['path']} "
             f"(engine `{report['source']['evaluate_system_version']}`)")
    L.append(f"- 无分化判定: {'是(触发分层定义重审)' if rv['undifferentiated'] else '否'}")
    L += ["", "## 复盘", "", "### 总体(六维 edge)", "",
          "| 维度 | 主指标 | n | 值 | 基线 | edge |", "|---|---|---|---|---|---|"]
    for row in rv["overall_summary"]:
        L.append(f"| {row['dim']} | {row['primary']} | {row['n']} | "
                 f"{_fmt(row['value'])} | {_fmt(row['baseline'])} | {_fmt(row['edge'])} |")
    def _weak_strong_block(title, items, key):
        if not items:
            return []
        blk = ["", f"### {title}", "",
               f"| {key} | 维度 | 值 | 总体 | 2σ界 | n |", "|---|---|---|---|---|---|"]
        for it in items:
            blk.append(f"| {it[key]} | {it['dim']} | {_fmt(it['value'])} | "
                       f"{_fmt(it['overall'])} | {_fmt(it['se'])} | {it['n']} |")
        return blk
    L += _weak_strong_block("弱层(显著跑输总体)", rv["weak_layers"], "layer")
    L += _weak_strong_block("强层(显著跑赢总体)", rv["strong_layers"], "layer")
    L += _weak_strong_block("弱环境(显著跑输总体)", rv["weak_environments"], "env")
    L += _weak_strong_block("强环境(显著跑赢总体)", rv["strong_environments"], "env")
    L += ["", "### 无 edge 维度", "",
          ("、".join(rv["no_edge_dims"]) if rv["no_edge_dims"] else "(无)")]
    L += ["", "### 退化/薄层", ""]
    L.append(f"- 退化环境(env_n < {env.MIN_STATE_N}):"
             f"{', '.join(rv['degenerate_environments']) or '(无)'}")
    L.append(f"- 薄层(layer_n < {ev.MIN_LAYER_N}):"
             f"{', '.join(rv['thin_layers']) or '(无)'}")
    L += ["", "## 优化建议", ""]
    if report["suggestions"]:
        for s in report["suggestions"]:
            L.append(f"- **[{s['kind']}] {s['proposed']}**")
            L.append(f"  - 旋钮: {s['knob'] or '(无)'};当前: {s['current']}")
            L.append(f"  - 证据: {s['evidence']}")
            L.append(f"  - 验证: {s['validation_recipe']}")
            L.append(f"  - 置信度: {s['confidence']};需额外探针: {s['requires_probe']}")
    else:
        L.append("无规则命中,零建议(数据不足以支撑任何改动)。")
    L += ["", "## 可调旋钮", "",
          "| name | current | controls | evidence_status |", "|---|---|---|---|"]
    for k in report["knobs"]:
        L.append(f"| {k['name']} | {k['current']} | {k['controls']} | {k['evidence_status']} |")
    L += ["", "## 诚实声明", ""]
    L.append("1. 只读:不应用任何改动;建议是提案,应用/版本化由 #129 执行。")
    L.append("2. significant[] 是 72 项原始 ±2σ 未多重校正;weak/strong 为提示性,非确认性。")
    L.append("3. 样本按股票×时间聚集、非 i.i.d.;n 为样本数而非独立观测数。")
    L.append("4. 控制器裁定旋钮(manual-ruling)不可机械推导,触及它们的建议须人工重推。")
    L.append("")
    return "\n".join(L)


def json_load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    parser = argparse.ArgumentParser(description="自动复盘 + 优化建议(只读)")
    parser.add_argument("--in", dest="in_path", default="evaluate_report.json")
    parser.add_argument("--out", default="review_report.json")
    args = parser.parse_args(argv)
    payload = json_load(args.in_path)
    rv = review(payload)
    suggestions = suggest(payload, rv)
    report = build_report(payload, rv, suggestions, args.in_path)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(args.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    print(f"wrote {args.out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
