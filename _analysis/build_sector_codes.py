# -*- coding: utf-8 -*-
"""把 spike 生成的 GBK code2sector.json 转成提交的 UTF-8 sector_codes.json(生产依赖)。"""
import json
import os

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "code2sector.json")
DST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sector_codes.json")

d = json.load(open(SRC, encoding="gbk"))
out = {k: sorted(set(v)) for k, v in d.items()}
with open(DST, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, sort_keys=True, indent=0)
print("wrote", DST, "codes:", len(out))
