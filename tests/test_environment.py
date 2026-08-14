# -*- coding: utf-8 -*-
"""environment.py 市场环境分类测试。"""
import environment as env


def _row(r5=None, r1=None, up=None, ld=0, lu=0, tr=None, r20=None, r60=0.0,
         ma5=1.0, ma20=1.0, ma60=1.0):
    """构造 classify 输入行;默认 r60=0.0/ma60=1.0 通过历史闸,ma 平坦无趋势。"""
    return {"r1": r1, "up_ratio": up, "limit_up": lu, "limit_down": ld,
            "turnover_ratio": tr, "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "r5": r5, "r20": r20, "r60": r60}


def test_insufficient_history():
    assert env.classify(_row(r60=None)) is None
    assert env.classify(_row(ma60=None)) is None


def test_panic_r5_boundary():
    assert env.classify(_row(r5=-0.08)) == "恐慌"
    assert env.classify(_row(r5=-0.079)) == "震荡"


def test_panic_breadth():
    assert env.classify(_row(r1=-0.04, up=0.15)) == "恐慌"


def test_panic_limit_down():
    assert env.classify(_row(ld=300)) == "恐慌"
    assert env.classify(_row(ld=299)) == "震荡"


def test_climax():
    assert env.classify(_row(r5=0.08, up=0.85)) == "高潮"
    assert env.classify(_row(r5=0.08, lu=100)) == "高潮"
    assert env.classify(_row(r5=0.08, tr=1.8)) == "高潮"
    assert env.classify(_row(r5=0.079, up=0.9)) == "震荡"


def test_bear():
    assert env.classify(_row(r20=-0.08, ma5=0.9, ma20=1.0, ma60=1.1)) == "熊"


def test_bull():
    assert env.classify(_row(r20=0.08, ma5=1.1, ma20=1.0, ma60=0.9)) == "牛"


def test_recovery():
    assert env.classify(_row(r5=0.03, r20=-0.01, up=0.55)) == "恢复"


def test_receding():
    assert env.classify(_row(r20=0.01, r5=-0.03, up=0.45)) == "退潮"


def test_oscillation_fallback():
    assert env.classify(_row(r5=0.0, r20=0.0, up=0.5)) == "震荡"


def test_priority_panic_over_bear():
    assert env.classify(_row(r5=-0.08, r20=-0.08, ma5=0.9, ma20=1.0, ma60=1.1)) == "恐慌"


def test_priority_climax_over_bull():
    assert env.classify(_row(r5=0.08, up=0.85, r20=0.08, ma5=1.1, ma20=1.0, ma60=0.9)) == "高潮"
