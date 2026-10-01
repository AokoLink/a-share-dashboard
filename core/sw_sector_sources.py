"""Official SW index catalogs, observed members and matching native histories."""
import re
import ssl

import numpy as np
import pandas as pd
import requests
import truststore
from requests.adapters import HTTPAdapter

from core.strategy_lifecycle import observation_clock

BASE = "https://www.swsresearch.com/institute-sw/api/index_publish/"
VERSION = "official-sw-native-v1"


class SystemTrustAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return super().init_poolmanager(*args, **kwargs)


def verified_get(url, **kwargs):
    """Keep TLS verification using the OS trust store; never disable it."""
    with requests.Session() as session:
        session.mount("https://", SystemTrustAdapter())
        return session.get(url, **kwargs)


class SwSectors:
    def __init__(self, get=None, clock=None):
        self.get = get or verified_get
        self.clock = clock or observation_clock

    def request(self, endpoint, **params):
        response = self.get(BASE + endpoint, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        response.raise_for_status()
        response.encoding = "utf-8"
        raw = response.text
        if not raw or len(raw.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("sw_response_empty_or_too_large")
        item = response.json()
        if not isinstance(item, dict) or str(item.get("code")) != "200" or "data" not in item:
            raise ValueError("sw_response_schema_changed")
        return item["data"], raw

    def pages(self, endpoint, **params):
        rows, raw_pages, total = [], [], None
        for page in range(1, 52):
            item, raw = self.request(endpoint, page=page, page_size=200, **params)
            raw_pages.append(raw)
            if not isinstance(item, dict) or type(item.get("count")) is not int:
                raise ValueError("sw_page_schema_changed")
            if total is None:
                total = item["count"]
            if not 0 < total <= 10000 or item["count"] != total:
                raise ValueError("sw_count_changed_or_out_of_bounds")
            entries = item.get("results")
            if not isinstance(entries, list) or len(entries) != min(200, total - len(rows)):
                raise ValueError("sw_member_page_incomplete")
            rows.extend(entries)
            if len(rows) == total:
                if item.get("next") is not None:
                    raise ValueError("sw_unexpected_next_page")
                return rows, raw_pages
            if not item.get("next"):
                raise ValueError("sw_early_terminal_page")
        raise ValueError("sw_paging_limit_exceeded")

    def catalog(self, category):
        level = "一级行业" if category == "industry" else "二级行业"
        entries, raw = self.pages("current/", indextype=level)
        rows = []
        for entry in entries:
            code = entry.get("swindexcode", "")
            name = entry.get("swindexname")
            if not re.fullmatch(r"801\d{3}", code) or not isinstance(name, str) or not name:
                raise ValueError("sw_catalog_identity_invalid")
            rows.append({"code": "sw_" + code, "native_id": code, "name": name, "category": category,
                         "provider": "sw", "taxonomy": "sw_native_index", "parent_mapping": "not_asserted",
                         "taxonomy_level": 1 if category == "industry" else 2})
        if len({r["code"] for r in rows}) != len(rows):
            raise ValueError("sw_catalog_duplicate_ids")
        return rows, {"status": "ok", "source": BASE + "current/", "raw_pages": raw,
                      "observed_at": self.clock().isoformat(), "board_count": len(rows)}

    def membership(self, row):
        code = row["native_id"]
        if not re.fullmatch(r"801\d{3}", code):
            raise ValueError("sw_invalid_index_id")
        before, raw1 = self.pages("details/component_stocks/", swindexcode=code)
        after, raw2 = self.pages("details/component_stocks/", swindexcode=code)
        def codes(entries):
            out = []
            for entry in entries:
                value = entry.get("stockcode", "")
                beginning = entry.get("beginningdate")
                if not re.fullmatch(r"\d{6}", value) or not beginning:
                    raise ValueError("sw_member_identity_invalid")
                if observation_clock(beginning) > self.clock():
                    raise ValueError("sw_member_future_effective_date")
                out.append(value)
            if len(set(out)) != len(out):
                raise ValueError("sw_duplicate_member_codes")
            return set(out)
        members = codes(before)
        if members != codes(after):
            raise ValueError("sw_members_changed_between_enumerations")
        return {"ok": True, "stale": False, "codes": sorted(members), "complete": True,
            "source": "sw.official_native_index_members", "source_name": row["name"], "native_id": code,
            "observed_at": self.clock().isoformat(), "match_type": "source_native", "expected_members": len(members),
            "completion_basis": "two_full_count_checked_enumerations", "raw_pages": raw1 + raw2,
            "scope": "observed_native_index_constituents_not_historical_classification",
            "member_records": before}

    def history(self, row):
        code = row["native_id"]
        if not re.fullmatch(r"801\d{3}", code):
            raise ValueError("sw_invalid_index_id")
        entries, raw = self.request("trend/", swindexcode=code, period="DAY")
        if not isinstance(entries, list) or not entries or len(entries) > 20000:
            raise ValueError("sw_index_history_schema_changed")
        if any(e.get("swindexcode") != code for e in entries):
            raise ValueError("sw_index_identity_mismatch")
        frame = pd.DataFrame(entries).rename(columns={"bargaindate": "date", "openindex": "open",
            "maxindex": "high", "minindex": "low", "closeindex": "close",
            "bargainamount": "volume_native", "bargainsum": "amount_native"})
        dates = pd.to_datetime(frame["date"], errors="raise")
        if dates.duplicated().any() or not dates.is_monotonic_increasing:
            raise ValueError("sw_index_duplicate_or_reverse_dates")
        frame["date"] = dates.dt.strftime("%Y-%m-%d")
        day = self.clock().date().isoformat()
        if frame["date"].iloc[-1] > day:
            raise ValueError("sw_index_future_dates")
        frame = frame[frame["date"] >= (pd.Timestamp(day) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")]
        columns = ["date", "open", "high", "low", "close", "volume_native", "amount_native"]
        if frame.empty or not set(columns).issubset(frame):
            raise ValueError("sw_index_window_empty_or_schema_changed")
        frame = frame[columns].copy()
        for column in columns[1:]:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        prices = frame[["open", "high", "low", "close"]]
        if (not np.isfinite(frame.iloc[:, 1:].to_numpy()).all() or (prices <= 0).any().any()
                or (frame["high"] < prices.max(axis=1)).any() or (frame["low"] > prices.min(axis=1)).any()
                or (frame[["volume_native", "amount_native"]] < 0).any().any()):
            raise ValueError("sw_index_invalid_prices")
        frame.attrs.update(raw_response=raw, source="sw.official_native_index",
                           quantity_basis="provider_native_not_stock_execution_units")
        return frame.reset_index(drop=True), False
