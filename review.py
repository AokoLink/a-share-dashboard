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
