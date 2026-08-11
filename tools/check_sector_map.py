# -*- coding: utf-8 -*-
"""离线检查板块映射:THS 板块名 → 新浪行业成分股源的覆盖度与 label 有效性。
用法:python tools/check_sector_map.py [--json]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
from data_source import SECTOR_CONS_MAP, SECTOR_CONS_EXPECTED, _keyword_lookup, _sina_industry_names


def main():
    names, _ = _sina_industry_names()          # label → 新浪名
    ths_raw = ak.stock_board_industry_name_ths()
    ths_names = sorted(set(str(x) for x in ths_raw["name"]))
    manual = [n for n in ths_names if n in SECTOR_CONS_MAP]
    kw = [n for n in ths_names if n not in SECTOR_CONS_MAP and _keyword_lookup(n, names) is not None]
    unmapped = [n for n in ths_names if n not in manual and n not in kw]
    stale = [(ths, label) for ths, label in SECTOR_CONS_MAP.items() if label not in names]
    renamed = [(ths, label) for ths, label in SECTOR_CONS_MAP.items()
               if label in names and SECTOR_CONS_EXPECTED.get(label)
               and names[label] != SECTOR_CONS_EXPECTED[label]]
    print("THS 板块总数: %d" % len(ths_names))
    print("手动映射: %d  关键词兜底: %d  未映射: %d" % (len(manual), len(kw), len(unmapped)))
    print("未映射:", " | ".join(unmapped) if unmapped else "(无)")
    print("失效 label:", stale if stale else "(无)")
    print("改名漂移:", renamed if renamed else "(无)")
    if unmapped or stale or renamed:
        sys.exit(1)


if __name__ == "__main__":
    main()
