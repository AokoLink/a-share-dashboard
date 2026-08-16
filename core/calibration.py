# -*- coding: utf-8 -*-
"""通用校准器:composite/risk 分箱 → 单调经验概率。

从 pipeline/predict.py 抽离的纯函数原语,供单日预测引擎与多 horizon 前向分布
引擎(core/forward.py)共享。PAV 单调池化保证 p_up(x) 单调不减;样本不足降级
单箱(degraded),诚实输出整体 base rate。
"""

N_BINS = 10


class Calibrator:
    """x → 单调经验概率。bins = [(upper, p), ...],upper 升序,末箱 upper=inf。"""

    def __init__(self, bins, degraded=False):
        self.bins = bins
        self.degraded = degraded

    def p_up(self, x):
        if x is None:
            return None
        try:
            f = float(x)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        for upper, p in self.bins:
            if f <= upper:
                return p
        return self.bins[-1][1]


def _fit_calibrator(pairs, n_bins, monotone=True):
    """等量分箱 + 可选 PAV 单调池化。n < n_bins 降为单箱(degraded)。"""
    clean = []
    for c, l in pairs:
        try:
            fc, fl = float(c), float(l)
        except (TypeError, ValueError):
            continue
        if fc != fc or fl != fl:
            continue
        clean.append((fc, fl))
    if len(clean) < n_bins:
        if not clean:
            return Calibrator([(float("inf"), 0.5)], degraded=True)
        p = sum(l for _, l in clean) / len(clean)
        return Calibrator([(float("inf"), p)], degraded=True)
    clean.sort(key=lambda x: x[0])
    n = len(clean)
    bins = []
    for b in range(n_bins):
        lo = b * n // n_bins
        hi = (b + 1) * n // n_bins
        chunk = clean[lo:hi]
        p = sum(l for _, l in chunk) / len(chunk)
        if hi < n:
            upper = (chunk[-1][0] + clean[hi][0]) / 2.0
        else:
            upper = chunk[-1][0]
        bins.append([upper, p, len(chunk)])
    if monotone:
        while True:
            merged = False
            out = []
            i = 0
            while i < len(bins):
                if i + 1 < len(bins) and bins[i][1] > bins[i + 1][1]:
                    n_m = bins[i][2] + bins[i + 1][2]
                    p_m = (bins[i][1] * bins[i][2] + bins[i + 1][1] * bins[i + 1][2]) / n_m
                    out.append([bins[i + 1][0], p_m, n_m])
                    i += 2
                    merged = True
                else:
                    out.append(bins[i])
                    i += 1
            bins = out
            if not merged:
                break
    finite = [(u, p) for u, p, _ in bins]
    finite[-1] = (float("inf"), finite[-1][1])
    return Calibrator(finite, degraded=False)


def _bin_index(cal, x):
    for idx, (upper, _p) in enumerate(cal.bins):
        if x <= upper:
            return idx
    return len(cal.bins) - 1
