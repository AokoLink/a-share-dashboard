# tools/verify_sources.py
# -*- coding: utf-8 -*-
"""数据源字段验证:每个接口打印列名 + 前 3 行样本,失败记录原因。"""
import sys, traceback
import pandas as pd
import akshare as ak

RESULTS = []
def probe(name, fn):
    try:
        out = fn()
        if isinstance(out, pd.DataFrame):
            cols = list(out.columns)
            head = out.head(3).to_dict('records')
            RESULTS.append(f"[OK] {name}\n  columns={cols}\n  sample={head}\n")
            return out
        else:
            RESULTS.append(f"[OK] {name}\n  type={type(out)} value={out}\n")
            return out
    except Exception as e:
        RESULTS.append(f"[FAIL] {name}\n  {type(e).__name__}: {e}\n")
        return None

def main():
    probe("stock_zh_a_spot (新浪全市场快照)", lambda: ak.stock_zh_a_spot())
    probe("stock_zh_index_spot_sina (新浪指数实时)", lambda: ak.stock_zh_index_spot_sina())
    probe("stock_zh_index_daily sh000001 (指数日线)", lambda: ak.stock_zh_index_daily(symbol="sh000001"))
    probe("stock_zh_a_daily qfq 600519 (个股日线)", lambda: ak.stock_zh_a_daily(symbol="sh600519", adjust="qfq"))
    probe("stock_intraday_sina sh600519 (分时)", lambda: ak.stock_intraday_sina(symbol="sh600519"))
    probe("stock_board_industry_name_ths (行业板块列表)", lambda: ak.stock_board_industry_name_ths())
    probe("stock_board_concept_name_ths (概念板块列表)", lambda: ak.stock_board_concept_name_ths())
    probe("stock_board_industry_summary_ths (行业摘要)", lambda: ak.stock_board_industry_summary_ths())
    probe("stock_board_concept_summary_ths (概念摘要)", lambda: ak.stock_board_concept_summary_ths())
    probe("stock_board_industry_index_ths 885887 (板块指数日线)", lambda: ak.stock_board_industry_index_ths(symbol="885887", period="daily"))
    probe("stock_board_industry_info_ths 885887 (行业成分股)", lambda: ak.stock_board_industry_info_ths(symbol="885887"))
    probe("stock_board_concept_info_ths (概念成分股)", lambda: ak.stock_board_concept_info_ths(symbol="885559"))
    probe("stock_zh_a_new (新股列表,用于排除上市≤5日)", lambda: ak.stock_zh_a_new())
    # 腾讯盘口字段下标验证(直接打印原始串)
    try:
        import urllib.request
        req = urllib.request.Request("https://qt.gtimg.cn/q=sh600519", headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
        fields = text.split('"')[1].split('~')
        RESULTS.append(f"[OK] qt.gtimg.cn 原始字段数={len(fields)}\n  fields[0:40]={fields[0:40]}\n")
    except Exception as e:
        RESULTS.append(f"[FAIL] qt.gtimg.cn\n  {type(e).__name__}: {e}\n")

    report = "=== A股数据源字段验证报告 ===\n" + "".join(RESULTS)
    with open("tools/source_report.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print(report)

if __name__ == "__main__":
    main()
