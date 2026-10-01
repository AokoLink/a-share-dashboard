"""Provider-native board identifiers; bounded paging, raw responses and observed dates."""
import json
import math
import re

import pandas as pd
import requests

from core.strategy_lifecycle import observation_clock
from core.sw_sector_sources import SwSectors

VERSION = "native-sector-sources-v3"


class NativeSectors:
    def __init__(self, get=None, clock=None, sw_get=None):
        self.get = get or requests.get
        self.clock = clock or observation_clock
        self.catalog_evidence = {}
        self.provider_errors = {}
        self.sw = SwSectors(get=sw_get or get, clock=self.clock)

    def request(self, url, **params):
        response = self.get(url, params=params, timeout=12)
        response.raise_for_status()
        response.encoding = "gb18030" if "sina.com" in url else "utf-8"
        raw = response.text
        if not raw:
            raise ValueError("empty_sector_response")
        return raw

    @staticmethod
    def decode(raw):
        # Catalogs contain a single assignment, never executable JavaScript.
        prefix = raw.find("{") if raw.lstrip().startswith("var ") else 0
        value, end = json.JSONDecoder().raw_decode(raw[prefix:].lstrip())
        if raw[prefix:].lstrip()[end:].strip() not in ("", ";"):
            raise ValueError("unexpected_sector_response_suffix")
        return value

    def catalog(self):
        rows = []
        for category, param in (("industry", None), ("subindustry", "industry"), ("theme", "class")):
            if category != "theme":
                try:
                    parsed, evidence = self.sw.catalog(category)
                    rows.extend(parsed)
                    self.catalog_evidence[category] = evidence
                    continue
                except Exception as exc:
                    self.provider_errors[category] = {"sw": str(exc)[:300]}
            try:
                if category == "subindustry":
                    raise ValueError("eastmoney_subindustry_catalog_not_provided")
                entries, raw_pages = self.em_pages("m:90 t:" + ("2" if category == "industry" else "3") + " f:!50")
                parsed = []
                for entry in entries:
                    native_id = entry.get("f12", "")
                    if not re.fullmatch(r"BK\d{4}", native_id) or not entry.get("f14"):
                        raise ValueError("invalid_eastmoney_sector_catalog")
                    parsed.append({"code": "em_" + native_id, "native_id": native_id,
                                   "name": entry["f14"], "category": category, "provider": "eastmoney"})
                self.catalog_evidence[category] = {"status": "ok", "observed_at": self.clock().isoformat(),
                    "source": "eastmoney.native_catalog", "raw_pages": raw_pages, "board_count": len(parsed)}
                rows.extend(parsed)
                continue
            except Exception as exc:
                self.provider_errors.setdefault(category, {})["eastmoney"] = str(exc)[:300]
            url = ("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php" if param is None
                   else "https://money.finance.sina.com.cn/q/view/newFLJK.php")
            try:
                raw = self.request(url, **({"param": param} if param else {}))
                mapping = self.decode(raw)
                if not isinstance(mapping, dict) or not mapping:
                    raise ValueError("empty_or_changed_sector_catalog")
                parsed = []
                for native_id, value in mapping.items():
                    fields = value.split(",")
                    if not re.fullmatch(r"[A-Za-z0-9_]+", native_id) or len(fields) < 3 or fields[0] != native_id:
                        raise ValueError("invalid_native_sector_identifier")
                    count = int(fields[2])
                    if count < 0:
                        raise ValueError("invalid_sector_member_count")
                    parsed.append({"code": "sina_" + native_id, "native_id": native_id,
                        "name": fields[1], "category": category, "provider": "sina",
                        "expected_members": count, "taxonomy": "sina_industry_detail" if category == "subindustry"
                            else "sina_legacy_industry" if category == "industry" else "sina_theme",
                        "parent_mapping": "not_asserted"})
                stamp = self.clock().isoformat()
                self.catalog_evidence[category] = {"status": "ok", "observed_at": stamp,
                    "source": url, "raw_response": raw, "board_count": len(parsed),
                    "provider_errors": self.provider_errors[category]}
                rows.extend(parsed)
            except Exception as exc:
                self.catalog_evidence[category] = {"status": "unavailable", "error": str(exc)[:300]}
        if not rows:
            raise ValueError("all_native_sector_catalogs_unavailable")
        frame = pd.DataFrame(rows)
        # Mixed providers must not turn native integer counts into NaN/float metadata.
        frame["expected_members"] = pd.Series([r.get("expected_members") for r in rows], dtype=object)
        frame.attrs["catalog_evidence"] = self.catalog_evidence
        return frame, False

    def em_pages(self, scope):
        rows, pages, total = [], [], None
        for page in range(1, 102):
            raw = self.request("https://17.push2.eastmoney.com/api/qt/clist/get", pn=page, pz=100,
                po=1, np=1, fltt=2, invt=2, fid="f12", fs=scope, fields="f12,f14",
                ut="bd1d9ddb04089700cf9c27f6f7426281")
            item = self.decode(raw).get("data")
            if not isinstance(item, dict) or type(item.get("total")) is not int:
                raise ValueError("invalid_eastmoney_page_schema")
            if total is None:
                total = item["total"]
            if not 0 < total <= 10000 or item["total"] != total:
                raise ValueError("eastmoney_count_changed_or_out_of_bounds")
            diff = item.get("diff")
            if not isinstance(diff, list) or len(diff) != min(100, total - len(rows)):
                raise ValueError("eastmoney_page_incomplete")
            rows.extend(diff)
            pages.append(raw)
            if len(rows) == total:
                if len({r.get("f12") for r in rows}) != total:
                    raise ValueError("eastmoney_duplicate_members")
                return rows, pages
        raise ValueError("eastmoney_paging_limit_exceeded")

    def membership(self, row):
        if row.get("provider") == "sw":
            return self.sw.membership(row)
        native = row["native_id"]
        if row.get("provider") == "eastmoney" and re.fullmatch(r"BK\d{4}", native):
            entries, raw_pages = self.em_pages("b:" + native + " f:!50")
            codes = [r.get("f12", "") for r in entries]
            if any(not re.fullmatch(r"\d{6}", c) for c in codes):
                raise ValueError("invalid_eastmoney_member_code")
            return {"ok": True, "stale": False, "codes": sorted(codes), "complete": True,
                "source": "eastmoney.native_sector", "source_name": row["name"], "native_id": native,
                "observed_at": self.clock().isoformat(), "match_type": "source_native",
                "expected_members": len(codes), "raw_pages": raw_pages}
        if row.get("provider") != "sina" or not re.fullmatch(r"[A-Za-z0-9_]+", native):
            raise ValueError("unsupported_native_sector_provider")
        base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
        count_raw = self.request(base + "getHQNodeStockCount", node=native)
        total = int(json.loads(count_raw))
        if not 0 <= total <= 10000:
            raise ValueError("sector_member_count_out_of_bounds")
        raw_pages, traversals, enumerated = [], [], []
        for asc in ("1", "0"):
            codes, symbols, sizes = [], [], []
            for page in range(1, 127):
                raw = self.request(base + "getHQNodeData", page=str(page), num="80", sort="symbol",
                    asc=asc, node=native, symbol="", _s_r_a="page")
                raw_pages.append(raw)
                entries = self.decode(raw)
                if not isinstance(entries, list) or len(entries) > 80:
                    raise ValueError("invalid_sector_member_page_schema")
                sizes.append(len(entries))
                if not entries:
                    break
                for entry in entries:
                    code, symbol = str(entry.get("code", "")), str(entry.get("symbol", ""))
                    if not re.fullmatch(r"\d{6}", code) or symbol not in {"sh" + code, "sz" + code, "bj" + code}:
                        raise ValueError("invalid_sector_member_symbol")
                    codes.append(code)
                    symbols.append(symbol)
                if len(codes) > 10000 or len(set(codes)) != len(codes):
                    return {"ok": False, "reason": "sector_members_duplicate_or_paging_limit", "native_id": native,
                            "source": "sina.native_sector", "raw_pages": raw_pages}
            else:
                raise ValueError("sector_terminal_page_not_reached")
            if symbols != sorted(symbols, reverse=asc == "0"):
                raise ValueError("sector_requested_sort_not_honored")
            enumerated.append(set(codes))
            traversals.append({"ascending": asc == "1", "page_sizes": sizes, "terminal_page": page})
        final_raw = self.request(base + "getHQNodeStockCount", node=native)
        if enumerated[0] != enumerated[1]:
            return {"ok": False, "reason": "sector_bidirectional_members_changed", "native_id": native,
                    "source": "sina.native_sector", "expected_members": total,
                    "catalog_members": row["expected_members"], "count_responses": [count_raw, final_raw],
                    "raw_pages": raw_pages}
        catalog_count = row["expected_members"]
        if isinstance(catalog_count, bool) or not math.isfinite(float(catalog_count)) or int(catalog_count) != catalog_count:
            raise ValueError("sector_member_count_out_of_bounds")
        counts = [int(catalog_count), total, int(json.loads(final_raw))]
        if any(type(n) is not int or not 0 <= n <= 10000 for n in counts):
            raise ValueError("sector_member_count_out_of_bounds")
        codes = sorted(enumerated[0])
        if not codes and any(counts):
            raise ValueError("empty_sector_members_with_nonzero_reported_count")
        return {"ok": True, "stale": False, "codes": codes, "complete": True,
            "observed_at": self.clock().isoformat(), "source": "sina.native_sector",
            "source_name": row["name"], "native_id": native, "match_type": "source_native",
            "expected_members": len(codes), "reported_counts": counts,
            "count_discrepancy": any(n != len(codes) for n in counts), "traversals": traversals,
            "completion_basis": "bidirectional_terminal_enumeration",
            "scope": "provider_members_including_inactive_not_current_tradeable_universe",
            "count_responses": [count_raw, final_raw], "raw_pages": raw_pages}

    def history(self, row):
        if row.get("provider") == "sw":
            return self.sw.history(row)
        if row.get("provider") == "eastmoney":
            native = row["native_id"]
            if not re.fullmatch(r"BK\d{4}", native):
                raise ValueError("invalid_sector_index_id")
            end = self.clock().date().strftime("%Y%m%d")
            raw = self.request("https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
                secid="90." + native, fields1="f1,f2,f3,f4,f5,f6", fields2="f51,f52,f53,f54,f55,f56,f57",
                klt=101, fqt=0, beg="19900101", end=end, lmt=400)
            item = self.decode(raw).get("data", {})
            if not isinstance(item, dict) or item.get("code") != native or not item.get("klines"):
                raise ValueError("native_sector_index_identity_or_schema_failed")
            frame = pd.DataFrame([v.split(",") for v in item["klines"]],
                                 columns=["date", "open", "close", "high", "low", "volume", "amount"])
            dates = pd.to_datetime(frame["date"], errors="raise")
            if dates.duplicated().any() or not dates.is_monotonic_increasing or dates.max().strftime("%Y%m%d") > end:
                raise ValueError("native_sector_index_dates_invalid")
            frame["close"] = pd.to_numeric(frame["close"], errors="raise")
            if not frame["close"].map(lambda v: math.isfinite(v) and v > 0).all():
                raise ValueError("native_sector_index_prices_invalid")
            frame.attrs["raw_response"] = raw
            frame.attrs["source"] = "eastmoney.native_sector_index"
            return frame, False
        # Sina's spot group average is not a historical native sector index.
        raise ValueError("sina_native_historical_sector_index_unavailable")
