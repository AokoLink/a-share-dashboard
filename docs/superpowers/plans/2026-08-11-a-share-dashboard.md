# A股三层分析看板 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `C:\stock` 实现一个本地运行的 A 股三层分析看板(大盘总览 → 板块强弱 → 个股量价),Flask 后端 + ECharts 前端,基于规则打分输出"当前适合走什么方向,什么方向有风险"。

**Architecture:** 四层结构:数据层 `data_source.py`(akshare 封装 + 线程安全 TTL 缓存)、持久化层 `store.py`(SQLite 每日快照)、分析层 `analysis.py`(纯函数打分,输入可注入)、API 层 `app.py`(Flask 路由组合输出契约化 JSON)。数据源只用新浪/腾讯/同花顺,全部避开东方财富。

**Tech Stack:** Python 3.13 + akshare 1.18.84 + Flask 3.x + pandas + SQLite + pytest;前端 ECharts 5.5.1(本地文件)。

## Global Constraints

- 数据源**只用**新浪(Sina)、腾讯(Tencent)、同花顺(THS);**禁止**任何 `xxx_em`(东方财富)与网易接口。
- `requirements.txt` 固定 `akshare==1.18.84`、`flask==3.1.0`、`pandas>=2.0`、`pytest>=8.0`;ECharts 固定 `5.5.1`,由 `install.py` 下载到 `static/echarts.min.js` 并记录版本。
- 安装脚本命名 `install.py`(不用 setup.py);应用启动 `python app.py` → `http://127.0.0.1:8000`。
- 全链路复合键 `type:code`(如 `industry:885887`);个股接受 `600519` 与 `sh600519` 两种写法,响应统一带前缀。
- `change_pct` 全部为**百分比数值**(3.2 = 3.2%)。
- 代码归一化:快照代码 `sh600000` 统一为 6 位数字;join 前过滤停牌(成交量为 0 或价格为 0)。
- 盘中累计 vs EOD 基准:凡"当日累计 / 历史 EOD 均值"类指标(大盘量能、板块成交额放量、板块放量滞涨)**一律仅收盘后(≥15:00)计算**,盘中该子项返回缺失并按权重归一化降级;分子分母同刻的指标(资金活跃度=板块/全市场成交额占比)盘中可用。**唯一例外:个股自定义量比按全日折算,盘中即可比**。
- 打分阈值:情绪分 高≥70/中45-69/低<45;强度分 高≥60/中35-59/低<35;风险分 高≥65/中40-64/低<40。
- 综合分 = `0.4×强度 + 0.35×情绪 + 0.25×(100−风险)`;个股综合分 = `0.4×趋势 + 0.35×量价 + 0.25×信号`;个股风险分 = `max(各风险项得分)`。
- 涨停/跌停近似:主板(60/00)±9.9%、创业板(30)/科创板(688)±19.9%、北交所(8/4)±29.9%;排除上市≤5 交易日新股(尽力而为);ST 不区分。
- 已交易分钟数 = 落在 9:30–11:30 的分钟数 + 落在 13:00–15:00 的分钟数;午休不计;9:30 前=0,≥15:00=240;禁止从 9:30 数自然分钟。已交易分钟 <15 时量比子项缺失。
- SQLite 开 WAL + busy_timeout;板块评分仅用同花顺行业摘要(90 板块,**无成分股聚合**);概念板块不纳入榜单;`data_complete` 恒 True、涨停占比恒 None(情绪子项走 `_weighted` 归一化降级)。
- 板块结论规则优先级 1→6(见规格 §6.2 表格),先命中先出。
- 每个 commit 使用 `git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "..."`(仓库已配置在 C:\stock)。
- 测试命令:`python -m pytest tests/ -v`(Windows 用 Git Bash 执行;conda python 已在 PATH)。

---

## 文件结构

```
C:\stock\
├── app.py               # Flask 入口 + API 路由(Task 8)
├── data_source.py       # 数据层:akshare 封装 + TTLCache + 代码归一化(Task 4)
├── analysis.py          # 分析层:时间口径 + 指标 + 大盘/板块/个股打分(Task 5-7)
├── store.py             # 持久化层:SQLite 每日快照(Task 3)
├── install.py           # 首次安装:装依赖 + 下载 echarts(Task 2)
├── requirements.txt     # pin 版本(Task 2)
├── .gitignore           # 忽略 data/、__pycache__/、static/echarts.min.js(Task 2)
├── conftest.py          # pytest:项目根加入 sys.path(Task 2)
├── README.md            # 使用说明(Task 10 补全)
├── tools\
│   └── verify_sources.py# 数据源字段验证脚本(Task 1)
├── static\
│   ├── echarts.min.js   # install.py 下载(本地图表库)
│   ├── app.js
│   └── style.css
├── templates\
│   └── index.html
├── data\
│   └── market.db        # SQLite,运行时生成(gitignore)
├── docs\superpowers\    # specs + plans(git 跟踪)
└── tests\
    ├── test_store.py
    ├── test_data_source.py
    ├── test_analysis_time.py
    ├── test_analysis_sector.py
    ├── test_analysis_stock.py
    └── test_api.py
```

分层职责(规格 §3):数据层取数+缓存不含业务判断;持久化层归档每日快照;分析层吃数据产出打分与结论;API 层组合输出契约化 JSON。

---

### Task 1: 数据源字段验证(spike,解锁数据层)

**Files:**
- Create: `tools/verify_sources.py`

**Interfaces:**
- Consumes: 已安装的 akshare 1.18.84;用户网络(新浪/腾讯/同花顺可达,东财被屏蔽)。
- Produces: 一份 `tools/source_report.txt`,记录每个数据接口的实际返回列名与样本;Task 4 的字段映射严格按本报告核对,若有出入以本报告为准。

**背景:** 规格 §2 标注的同花顺成分股接口(`stock_board_industry_info_ths`/`stock_board_concept_info_ths`)、§8 的腾讯盘口字段下标是**未验证项**,必须先跑真网络确认。同时确认新浪 `stock_zh_a_spot` 列名、`stock_intraday_sina` 是否有均价列、`stock_zh_a_new()` 是否可用(用于新股排除)。

- [ ] **Step 1: 写验证脚本**

```python
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
```

- [ ] **Step 2: 运行脚本并核对**

Run: `cd /c/stock && PYTHONIOENCODING=utf-8 python tools/verify_sources.py 2>&1 | tail -80`
Expected: 脚本跑通,`tools/source_report.txt` 生成。人工核对:
- `stock_zh_a_spot` 是否含列 `代码/名称/最新价/涨跌幅/成交量/成交额`(若列名不同,记录实际名)。
- `stock_intraday_sina` 是否含 `avg_price` 列(分时均价线依赖)。
- 同花顺摘要是否含 `板块代码/板块名称/涨跌幅/上涨家数/下跌家数/领涨股票/领涨股票-涨跌幅`,是否含成交额列(缺则 Task 4 的 turnover 全 None,分析层自动降级)。
- `stock_board_industry_info_ths` 是否返回成分股列表(含 `代码` 列),概念版同理。
- `stock_zh_a_new()` 是否可用(可用 → 新股排除;不可用 → Task 4 `get_new_stocks` 返回空集,降级说明)。
- 腾讯串 `fields` 中下标 1=名称、3=最新价、4=昨收、5=今开、32=涨跌幅%、33=最高、34=最低、36=成交量(手)、37=成交额(万元)是否成立(不成立则记录实际下标,Task 4 的 `_parse_tencent_quote` 按下标修改)。

- [ ] **Step 3: 提交报告**

```bash
cd /c/stock && git add tools/verify_sources.py tools/source_report.txt && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "chore: 数据源字段验证脚本与报告"
```
Expected: 提交成功。**后续 Task 4 若代码与报告不符,以报告实际字段为准微调映射。**

---

### Task 2: 项目脚手架

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `conftest.py`
- Create: `install.py`

**Interfaces:**
- Produces: `requirements.txt`(供 install.py/README 引用);`conftest.py` 使 `import store/data_source/analysis/app` 在 pytest 下可用;`install.py` 安装依赖 + 下载 echarts(供 Task 9 前端使用)。

- [ ] **Step 1: 写依赖与配置文件**

`requirements.txt`:
```
akshare==1.18.84
flask==3.1.0
pandas>=2.0
pytest>=8.0
```

`.gitignore`:
```
__pycache__/
*.pyc
data/
static/echarts.min.js
.pytest_cache/
```

`conftest.py`:
```python
# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
```

- [ ] **Step 2: 写 install.py(装依赖 + 下载 echarts 5.5.1,含失败回退)**

```python
# -*- coding: utf-8 -*-
"""首次安装:安装依赖 + 下载 ECharts 5.5.1 到 static/echarts.min.js。
用法: python install.py
"""
import os, subprocess, sys, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ECHARTS_VERSION = "5.5.1"
ECHARTS_URLS = [
    f"https://cdn.jsdelivr.net/npm/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
    f"https://unpkg.com/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
    f"https://registry.npmmirror.com/echarts/{ECHARTS_VERSION}/files/dist/echarts.min.js",
]
STATIC_DIR = os.path.join(BASE_DIR, "static")


def install_deps():
    req = os.path.join(BASE_DIR, "requirements.txt")
    print("[1/2] 安装 Python 依赖...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", req], check=True)


def download_echarts():
    print("[2/2] 下载 ECharts %s ..." % ECHARTS_VERSION)
    os.makedirs(STATIC_DIR, exist_ok=True)
    dest = os.path.join(STATIC_DIR, "echarts.min.js")
    if os.path.exists(dest) and os.path.getsize(dest) > 500 * 1024:
        print("  已存在 echarts.min.js,跳过下载")
        return
    last_err = None
    for url in ECHARTS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=30).read()
            if len(data) < 500 * 1024:
                raise RuntimeError("文件过小(%d bytes),疑似错误页面" % len(data))
            with open(dest, "wb") as f:
                f.write(data)
            print("  下载成功: %s" % url)
            return
        except Exception as e:
            last_err = e
            print("  失败: %s (%s)" % (url, e))
    print("!! ECharts 下载失败:%s" % last_err)
    print("!! 请手动下载 https://cdn.jsdelivr.net/npm/echarts@%s/dist/echarts.min.js 放到 static/ 目录" % ECHARTS_VERSION)


def main():
    install_deps()
    download_echarts()
    print("\n完成!运行: python app.py 然后访问 http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 运行 install.py 验证**

Run: `cd /c/stock && python install.py`
Expected: 依赖已满足(akshare 等已装),echarts.min.js 下载到 `static/` 且大小 >500KB。

- [ ] **Step 4: 初始化 app.py 最小骨架(占位,Task 8 填充)**

```python
# -*- coding: utf-8 -*-
"""A股三层分析看板 —— Flask 入口。完整实现在 Task 8。"""
from flask import Flask

def create_app(db_path=None):
    app = Flask(__name__)
    @app.route("/")
    def index():
        return "A股分析看板(实施中)"
    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000)
```

Run: `cd /c/stock && PYTHONIOENCODING=utf-8 python -c "import app; a=app.create_app(); print(a)"`
Expected: 打印 Flask app 对象,无报错。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add requirements.txt .gitignore conftest.py install.py app.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "chore: 项目脚手架(依赖/忽略/安装脚本/app骨架)"
```

---

### Task 3: 持久化层 store.py

**Files:**
- Create: `store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces(供 Task 8 app.py 消费;Task 4 不依赖它):
  - `init_db(db) -> None` 建表(WAL + busy_timeout 每次连接设置)。
  - `upsert_market_daily(db, date, up_count, down_count, flat_count, limit_up, limit_down, total_turnover, index_close, snapshot_time) -> None`
  - `get_market_daily_prev(db, date) -> dict | None` 返回 `< date` 最近一行。
  - `upsert_sector_daily(db, date, type, code, name, change_pct, up_ratio, turnover, rank) -> None`
  - `get_sector_turnover_avg(db, type, code, date, days=5) -> float | None` 近 N 日(不含 date)turnover 均值。
  - `get_sector_prev_change(db, type, code, date) -> float | None` `< date` 最近一行的 change_pct。
  - `get_sector_change_3d(db, type, code, date) -> float | None` 近 3 个已完成交易日(不含 date)累计涨幅(复利),不足 2 行返回 None。
  - `get_consecutive_days(db, type, code, date, top_n=20) -> int` 从 date 起向前连续 rank≤20 的天数。
  - 所有函数第一个参数为 `db`(SQLite 文件路径)。

**口径(规格 §5.1):** 每日快照每交易日一行,盘中多次 upsert 覆盖为最新值。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_store.py
# -*- coding: utf-8 -*-
import os
import pytest
import store


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "t.db")
    store.init_db(path)
    return path


def test_init_creates_tables(db):
    # 无报错即表已建;再次 init 幂等
    store.init_db(db)


def test_market_daily_upsert_idempotent(db):
    store.upsert_market_daily(db, "2026-08-10", 100, 50, 5, 3, 1, 1.0e9, 3400.0, "15:00:00")
    store.upsert_market_daily(db, "2026-08-10", 120, 60, 6, 5, 2, 1.2e9, 3420.0, "15:05:00")  # 覆盖
    rows = store.get_market_daily_prev(db, "2026-08-11")
    assert rows == {"date": "2026-08-10", "up_count": 120, "down_count": 60, "flat_count": 6,
                    "limit_up": 5, "limit_down": 2, "total_turnover": 1.2e9,
                    "index_close": 3420.0, "snapshot_time": "15:05:00"}


def test_market_daily_prev_skips_self(db):
    store.upsert_market_daily(db, "2026-08-10", 1, 0, 0, 0, 0, 100.0, 1.0, "15:00:00")
    assert store.get_market_daily_prev(db, "2026-08-10") is None  # 不含自身
    assert store.get_market_daily_prev(db, "2026-08-11")["date"] == "2026-08-10"


def test_sector_turnover_avg_excludes_today(db):
    for i, d in enumerate(["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-11"]):
        store.upsert_sector_daily(db, d, "industry", "885887", "半导体", 1.0, 0.5, 100 + i, i + 1)
    avg = store.get_sector_turnover_avg(db, "industry", "885887", "2026-08-11", days=5)
    # 排除 2026-08-11,取前 4 行 100..103 均值 = 101.5
    assert avg == pytest.approx(101.5)
    assert store.get_sector_turnover_avg(db, "industry", "999999", "2026-08-11") is None


def test_sector_prev_change_and_3d(db):
    store.upsert_sector_daily(db, "2026-08-06", "industry", "885887", "半导体", 1.0, 0.5, 100.0, 1)
    store.upsert_sector_daily(db, "2026-08-07", "industry", "885887", "半导体", 2.0, 0.6, 110.0, 1)
    store.upsert_sector_daily(db, "2026-08-08", "industry", "885887", "半导体", 3.0, 0.7, 120.0, 1)
    assert store.get_sector_prev_change(db, "industry", "885887", "2026-08-11") == pytest.approx(3.0)
    # 3日累计 = (1.01*1.02*1.03-1) ≈ 6.05%
    assert store.get_sector_change_3d(db, "industry", "885887", "2026-08-11") == pytest.approx(6.05, abs=0.01)
    # 仅 1 行历史 → 无法判断 3 日 → None
    store2 = str(tmp_path / "t2.db")
    store.init_db(store2)
    store.upsert_sector_daily(store2, "2026-08-08", "industry", "885887", "半导体", 1.0, 0.5, 100.0, 1)
    assert store.get_sector_change_3d(store2, "industry", "885887", "2026-08-11") is None


def test_consecutive_days_counts_rank_streak(db):
    rows = [("2026-08-03", 5), ("2026-08-04", 30), ("2026-08-05", 2), ("2026-08-06", 3)]
    for i, (d, r) in enumerate(rows):
        store.upsert_sector_daily(db, d, "industry", "885887", "半导体", 1.0, 0.5, 100.0 + i, r)
    assert store.get_consecutive_days(db, "industry", "885887", "2026-08-06") == 2  # 8-06,8-05 连续,8-04 断开
```

注意:最后一个测试在 `test_sector_prev_change_and_3d` 里用了 `tmp_path` fixture(文件级,pytest 自动注入)。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /c/stock && python -m pytest tests/test_store.py -v`
Expected: FAIL(module not found:`import store` 失败或函数未定义)。

- [ ] **Step 3: 写实现 store.py**

```python
# -*- coding: utf-8 -*-
"""持久化层:SQLite 每日快照。db 参数为数据库文件路径。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_daily (
  date TEXT PRIMARY KEY,
  up_count INTEGER, down_count INTEGER, flat_count INTEGER,
  limit_up INTEGER, limit_down INTEGER,
  total_turnover REAL, index_close REAL, snapshot_time TEXT
);
CREATE TABLE IF NOT EXISTS sector_daily (
  date TEXT, type TEXT, code TEXT, name TEXT,
  change_pct REAL, up_ratio REAL, turnover REAL, rank INTEGER,
  PRIMARY KEY(date, type, code)
);
"""


def init_db(db):
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    conn = _connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _connect(db):
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _row_to_dict(row):
    return dict(row) if row else None


def upsert_market_daily(db, date, up_count, down_count, flat_count, limit_up, limit_down,
                        total_turnover, index_close, snapshot_time):
    conn = _connect(db)
    conn.execute(
        """INSERT INTO market_daily(date, up_count, down_count, flat_count, limit_up, limit_down,
             total_turnover, index_close, snapshot_time)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
             up_count=excluded.up_count, down_count=excluded.down_count,
             flat_count=excluded.flat_count, limit_up=excluded.limit_up,
             limit_down=excluded.limit_down, total_turnover=excluded.total_turnover,
             index_close=excluded.index_close, snapshot_time=excluded.snapshot_time""",
        (date, up_count, down_count, flat_count, limit_up, limit_down,
         total_turnover, index_close, snapshot_time))
    conn.commit()
    conn.close()


def get_market_daily_prev(db, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT * FROM market_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (date,))
    row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def upsert_sector_daily(db, date, type, code, name, change_pct, up_ratio, turnover, rank):
    conn = _connect(db)
    conn.execute(
        """INSERT INTO sector_daily(date, type, code, name, change_pct, up_ratio, turnover, rank)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(date, type, code) DO UPDATE SET
             name=excluded.name, change_pct=excluded.change_pct, up_ratio=excluded.up_ratio,
             turnover=excluded.turnover, rank=excluded.rank""",
        (date, type, code, name, change_pct, up_ratio, turnover, rank))
    conn.commit()
    conn.close()


def get_sector_turnover_avg(db, type, code, date, days=5):
    conn = _connect(db)
    cur = conn.execute(
        """SELECT AVG(turnover) FROM (
             SELECT turnover FROM sector_daily
             WHERE type=? AND code=? AND date < ?
             ORDER BY date DESC LIMIT ?)""",
        (type, code, date, days))
    val = cur.fetchone()[0]
    conn.close()
    return val if val is not None else None


def get_sector_prev_change(db, type, code, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT change_pct FROM sector_daily WHERE type=? AND code=? AND date < ? "
        "ORDER BY date DESC LIMIT 1", (type, code, date))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_sector_change_3d(db, type, code, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT change_pct FROM sector_daily WHERE type=? AND code=? AND date < ? "
        "ORDER BY date DESC LIMIT 3", (type, code, date))
    rows = cur.fetchall()
    conn.close()
    if len(rows) < 2:
        return None
    prod = 1.0
    for (c,) in rows:
        prod *= (1 + c / 100.0)
    return (prod - 1) * 100.0


def get_consecutive_days(db, type, code, date, top_n=20):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT rank FROM sector_daily WHERE type=? AND code=? AND date <= ? "
        "ORDER BY date DESC", (type, code, date))
    days = 0
    for (r,) in cur:
        if r is not None and r <= top_n:
            days += 1
        else:
            break
    conn.close()
    return days
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /c/stock && python -m pytest tests/test_store.py -v`
Expected: PASS(全绿)。若 `test_market_daily_upsert_idempotent` 断言 key 顺序问题,`dict(row)` 按表列顺序返回,断言已按列名,不依赖顺序。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add store.py tests/test_store.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: 持久化层 store.py(每日快照)"
```

---

### Task 4: 数据层 data_source.py(缓存 + 取数)

**Files:**
- Create: `data_source.py`
- Test: `tests/test_data_source.py`

**Interfaces:**
- Consumes: `akshare`(`import akshare as _ak`)、`requests`;Task 1 报告中的实际字段名。
- Produces(供 Task 8 app.py 消费):
  - `class DataSourceError(Exception)`
  - `class TTLCache(max_entries=200, default_ttl=60, clock=None)`:线程安全;`get(key)->(value, fresh)`,`set(key,value,ttl=None)`,`mark_failure(key)`(失败退避 30s×2^n 封顶 10min,保留 stale 值)。
  - `normalize_code(raw) -> str` 6 位数字。
  - `with_prefix(code) -> str` `sh600519`/`sz000001`/`bj830000`。
  - `_parse_tencent_quote(text) -> dict`
  - 以下函数**全部返回 `(data, stale)`**:`get_market_spot() -> (DataFrame[code,name,price,change_pct,volume,amount], bool)`,`get_index_realtime() -> (list[dict], bool)`,`get_index_daily(code) -> (DataFrame, bool)`,`get_sector_summary(type) -> (DataFrame[code,name,change_pct,up_count,down_count,leader,leader_change_pct,turnover], bool)`(仅 industry),`get_sector_index_history(code, type) -> (DataFrame, bool)`(仅 industry),`get_stock_daily(code) -> (DataFrame, bool)`,`get_stock_minute(code) -> (DataFrame[time,price,avg,volume], bool)`,`get_stock_quote(code) -> (dict, bool)`。
  - `get_new_stocks() -> set[str]` 上市≤5 交易日新股 6 位码集合(尽力而为,不可用返回空集)。
  - `last_updated_at: str` 模块级,最近成功拉取时间,供 meta.updated_at。
- TTL(规格 §4):spot 60s、index_realtime 30s、index_daily 10min、sector_summary 60s、sector_index_history 30min、stock_daily 10min、stock_minute 60s、stock_quote 30s。

**口径(规格 §7):** 停牌过滤 = 成交量/价格 >0;`get_market_spot` 输出 code 为 6 位;腾讯成交额单位万元→×10000 转元;同花顺`总成交额`单位亿元→×1e8 转元;板块 code 经 `stock_board_industry_name_ths` 的 name→code 表关联(摘要无板块代码列)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_data_source.py
# -*- coding: utf-8 -*-
import pandas as pd
import pytest
import data_source as ds


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def make_spot():
    return pd.DataFrame({
        "code": ["sh600000", "600519", "sz000001", "sh688981", "300750"],
        "name": ["浦发银行", "贵州茅台", "平安银行", "中芯国际", "宁德时代"],
        "price": [10.0, 1348.9, 12.0, 45.0, 200.0],
        "change_pct": [1.0, 0.3, -2.0, 5.0, 0.0],
        "volume": [100000, 682720, 80000, 0, 90000],
        "amount": [1e8, 9.2e8, 2e8, 0, 5e8],
    })


def test_normalize_and_prefix():
    assert ds.normalize_code("sh600000") == "600000"
    assert ds.normalize_code("600519") == "600519"
    assert ds.normalize_code("SZ000001") == "000001"
    assert ds.with_prefix("600519") == "sh600519"
    assert ds.with_prefix("000001") == "sz000001"
    assert ds.with_prefix("830000") == "bj830000"


def test_ttl_cache_fresh_then_expire():
    clock = FakeClock()
    c = ds.TTLCache(max_entries=5, default_ttl=60, clock=clock)
    c.set("k", "v", 60)
    assert c.get("k") == ("v", True)
    clock.t = 61
    assert c.get("k") == ("v", False)  # 过期但值仍在(stale 可回退)


def test_ttl_cache_evict_oldest():
    clock = FakeClock()
    c = ds.TTLCache(max_entries=2, default_ttl=60, clock=clock)
    c.set("a", 1); clock.t = 1
    c.set("b", 2); clock.t = 2
    c.set("c", 3)  # 淘汰最旧 a
    assert c.get("a") == (None, False)
    assert c.get("c") == (3, True)


def test_ttl_cache_mark_failure_backoff():
    clock = FakeClock()
    c = ds.TTLCache(max_entries=5, default_ttl=60, clock=clock)
    c.set("k", "v")
    c.mark_failure("k")            # 失败1次 → 退避 30s(n 从 0 起)
    clock.t = 30
    assert c.get("k") == ("v", True)     # 恰好 30s 仍新鲜
    clock.t = 31
    assert c.get("k") == ("v", False)    # 超过 30s → stale 回退
    c.mark_failure("k")            # 失败2次 → 退避 60s
    clock.t = 90
    assert c.get("k") == ("v", True)     # 距上次失败 59s < 60s 仍新鲜
    clock.t = 92
    assert c.get("k") == ("v", False)    # 超过 60s → stale


def test_parse_tencent_quote():
    # 40 槽字段:idx0..5 名称/代码/最新价/昨收/今开,idx6..29 填充,
    # idx30 时间,31 涨跌额,32 涨跌幅,33 最高,34 最低,36 成交量(手),37 成交额(万元)
    fields = ["1", "贵州茅台", "600519", "1348.90", "1345.00", "1348.00"]
    fields += ["0"] * 24                                  # idx6..29
    fields += ["20260811103001", "3.90", "0.29", "1352.65", "1338.18"]  # idx30..34
    fields += ["0"]                                       # idx35
    fields += ["682720", "91903.5968"]                    # idx36..37
    fields += ["0"] * 2
    text = 'v_sh600519="' + "~".join(fields) + '";'
    q = ds._parse_tencent_quote(text)
    assert q["name"] == "贵州茅台"
    assert q["price"] == pytest.approx(1348.90)
    assert q["change_pct"] == pytest.approx(0.29)
    assert q["high"] == pytest.approx(1352.65)
    assert q["low"] == pytest.approx(1338.18)
    assert q["volume"] == pytest.approx(682720.0 * 100)      # 手 → 股
    assert q["turnover"] == pytest.approx(91903.5968 * 10000)  # 万元 → 元


def test_market_spot_normalized(monkeypatch):
    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", lambda: make_spot())
    df, stale = ds.get_market_spot()
    assert stale is False
    assert list(df["code"]) == ["600000", "600519", "000001", "688981", "300750"]


def test_market_spot_stale_on_failure(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return make_spot()
        raise RuntimeError("network down")

    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", flaky)
    # 可控时钟 + 清空模块级缓存,避免污染其他用例
    clock = FakeClock()
    ds.cache._clock = clock
    ds.cache._data.clear()
    df, stale1 = ds.get_market_spot()
    assert stale1 is False
    # 缓存命中(60s 内),不触发 fetch
    df2, stale2 = ds.get_market_spot()
    assert stale2 is False and calls["n"] == 1
    # 超过 TTL 后重试失败 → stale 回退
    clock.t = 61
    df3, stale3 = ds.get_market_spot()
    assert stale3 is True and df3["code"].iloc[0] == "600000"


def test_sector_summary_mapping(monkeypatch):
    raw = pd.DataFrame({
        "板块": ["半导体", "白酒"],
        "涨跌幅": [3.2, -1.0],
        "上涨家数": [80, 30],
        "下跌家数": [5, 60],
        "领涨股": ["X", "Y"],
        "领涨股-涨跌幅": [10.0, 0.5],
    })
    name_map = pd.DataFrame({"name": ["半导体", "白酒"], "code": ["881121", "881273"]})
    monkeypatch.setattr(ds._ak, "stock_board_industry_summary_ths", lambda: raw)
    monkeypatch.setattr(ds._ak, "stock_board_industry_name_ths", lambda: name_map)
    df, _ = ds.get_sector_summary("industry")
    assert list(df["code"]) == ["881121", "881273"]   # code 来自 name→code 表关联
    assert df["turnover"].isna().all()  # 缺总成交额 → 全 None(分析层自动降级)
    assert df["change_pct"].iloc[0] == pytest.approx(3.2)


def test_stock_minute_normalized(monkeypatch):
    # 分时走 stock_zh_a_minute(period='1'),均价 = 累计成交额/累计成交量
    raw = pd.DataFrame({
        "day": ["2026-08-11 10:00:00", "2026-08-11 10:01:00"],
        "open": [1347.0, 1348.5], "high": [1350.0, 1351.0],
        "low": [1346.0, 1347.0], "close": [1348.0, 1349.0],
        "volume": [12000, 5000], "amount": [1.6e7, 6.7e6],
    })
    monkeypatch.setattr(ds._ak, "stock_zh_a_minute",
                        lambda symbol, period, adjust: raw)
    df, _ = ds.get_stock_minute("sh600519")
    assert list(df["time"]) == ["10:00", "10:01"]
    assert list(df["avg"]) == pytest.approx([1.6e7 / 12000, 2.27e7 / 17000])
```

注意:`test_market_spot_stale_on_failure` 依赖 `ds.cache` 暴露为模块级实例(实现中必须有 `cache = TTLCache(...)` 模块级)。该用例用 FakeClock 接管模块级时钟并 `_data.clear()`,是文件内最后一个依赖模块级 cache 的用例。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /c/stock && python -m pytest tests/test_data_source.py -v`
Expected: FAIL(`import data_source` 失败)。

- [ ] **Step 3: 写实现 data_source.py**

```python
# -*- coding: utf-8 -*-
"""数据层:akshare 封装 + 线程安全 TTL 缓存。所有 get_* 返回 (data, stale)。"""
import threading
import time
from datetime import datetime

import pandas as pd
import akshare as _ak
import requests

CACHE_MAX = 200
SOURCE_TIMEOUT = 15


class DataSourceError(Exception):
    pass


class TTLCache:
    def __init__(self, max_entries=CACHE_MAX, default_ttl=60, clock=None):
        self._max = max_entries
        self._default_ttl = default_ttl
        self._clock = clock or time.time
        self._data = {}  # key -> (ts, ttl, value, failures)
        self._lock = threading.Lock()

    def get(self, key):
        """返回 (value, fresh);fresh=False 表示已过期但值仍在,可作 stale 回退。"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None, False
            ts, ttl, value, _ = item
            if self._clock() - ts <= ttl:
                return value, True
            return value, False

    def set(self, key, value, ttl=None):
        with self._lock:
            if len(self._data) >= self._max and key not in self._data:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (self._clock(), ttl or self._default_ttl, value, 0)

    def mark_failure(self, key):
        """拉取失败:延长 TTL(30s×2^n 封顶 600s),保留 stale 值。"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return
            ts, _, value, n = item
            backoff = min(30 * (2 ** n), 600)
            self._data[key] = (self._clock(), backoff, value, n + 1)


cache = TTLCache()
last_updated_at = ""  # 模块级:最近成功拉取时间


def _key(func_name, *params):
    return "%s:%s" % (func_name, sorted(str(p) for p in params))


def _set_updated():
    global last_updated_at
    last_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cached(key, ttl, fetch):
    """缓存取数:命中返回;过期则重拉;重拉失败 → stale 回退 + 退避;全无 → 抛 DataSourceError。"""
    val, fresh = cache.get(key)
    if fresh:
        return val, False
    try:
        data = fetch()
        cache.set(key, data, ttl)
        _set_updated()
        return data, False
    except Exception as e:
        if val is not None:
            cache.mark_failure(key)
            return val, True
        raise DataSourceError(str(e)) from e


def _fetch_with_retry(fetch):
    """失败重试 1 次(规格 §10)。"""
    try:
        return fetch()
    except Exception:
        return fetch()


# ---------- 代码归一化 ----------

def normalize_code(raw):
    s = str(raw).strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[len(p):]
            break
    if s.isdigit():
        return s.zfill(6)
    return s


def with_prefix(code):
    c = normalize_code(code)
    if c.startswith(("6", "5", "9")):
        return "sh" + c
    if c.startswith(("0", "1", "2", "3")):
        return "sz" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sh" + c


# ---------- 腾讯盘口 ----------

def _parse_tencent_quote(text):
    fields = text.split('"')[1].split("~")
    return {
        "name": fields[1],
        "code": fields[2],
        "price": float(fields[3]),
        "prev_close": float(fields[4]),
        "open": float(fields[5]),
        "change_pct": float(fields[32]),
        "high": float(fields[33]),
        "low": float(fields[34]),
        "volume": float(fields[36]) * 100.0,  # 手 → 股(与日线成交量单位一致)
        "turnover": float(fields[37]) * 10000.0,  # 万元 → 元
    }


def get_stock_quote(code):
    symbol = with_prefix(code)

    def fetch():
        url = "https://qt.gtimg.cn/q=" + symbol
        text = requests.get(url, timeout=SOURCE_TIMEOUT).text
        return _parse_tencent_quote(text)

    return _cached(_key("quote", symbol), 30, lambda: _fetch_with_retry(fetch))


# ---------- 新浪 ----------

def get_market_spot():
    def fetch():
        raw = _ak.stock_zh_a_spot()
        out = pd.DataFrame({
            "code": raw["代码"].map(normalize_code),
            "name": raw.get("名称"),
            "price": raw.get("最新价"),
            "change_pct": raw.get("涨跌幅"),
            "volume": raw.get("成交量"),
            "amount": raw.get("成交额"),
        })
        for col in ("price", "change_pct", "volume", "amount"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out

    return _cached(_key("spot"), 60, lambda: _fetch_with_retry(fetch))


INDEX_NAMES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}


def get_index_realtime():
    def fetch():
        raw = _ak.stock_zh_index_spot_sina()
        rows = []
        for _, r in raw.iterrows():
            code = str(r["代码"]).strip().lower()
            if code in INDEX_NAMES:  # 指数代码自带 sh/sz 前缀;勿用 with_prefix 重拼(000001 会错拼成 sz000001)
                rows.append({"code": code, "name": INDEX_NAMES[code],
                             "price": float(r["最新价"]), "change_pct": float(r["涨跌幅"])})
        return rows

    return _cached(_key("index_spot"), 30, lambda: _fetch_with_retry(fetch))


def get_index_daily(code):
    symbol = with_prefix(code)

    def fetch():
        raw = _ak.stock_zh_index_daily(symbol=symbol)
        out = raw[["date", "open", "high", "low", "close", "volume"]].copy()
        out["date"] = out["date"].astype(str)
        return out

    return _cached(_key("index_daily", symbol), 600, lambda: _fetch_with_retry(fetch))


def get_stock_daily(code):
    symbol = with_prefix(code)

    def fetch():
        raw = _ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
        out = raw[["date", "open", "high", "low", "close", "volume"]].copy()
        out["date"] = out["date"].astype(str)
        return out

    return _cached(_key("stock_daily", symbol), 600, lambda: _fetch_with_retry(fetch))


def get_stock_minute(code):
    symbol = with_prefix(code)

    def fetch():
        raw = _ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        price = pd.to_numeric(raw["close"], errors="coerce")
        volume = pd.to_numeric(raw["volume"], errors="coerce")
        amount = pd.to_numeric(raw["amount"], errors="coerce")
        cum_vol = volume.fillna(0).cumsum()
        cum_amt = amount.fillna(0).cumsum()
        avg = cum_amt / cum_vol.where(cum_vol > 0)
        out = pd.DataFrame({
            "time": raw["day"].astype(str).str.slice(11, 16),
            "price": price,
            "avg": avg,
            "volume": volume,
        })
        return out

    return _cached(_key("stock_minute", symbol), 60, lambda: _fetch_with_retry(fetch))


def get_new_stocks():
    """上市≤5交易日的股票(尽力而为):取最近新股列表;接口不可用 → 空集。"""
    try:
        raw = _ak.stock_zh_a_new()
        codes = raw["代码"].map(normalize_code).tolist() if "代码" in raw.columns else []
        return set(codes)
    except Exception:
        return set()


# ---------- 同花顺 ----------

def _ths_col(df, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([None] * len(df))


def _industry_code_map():
    """name→code 对照表(摘要无板块代码列),静态映射,缓存 1h。"""
    def fetch():
        raw = _ak.stock_board_industry_name_ths()
        return dict(zip(raw["name"], raw["code"].astype(str)))
    return _cached(_key("industry_name_code"), 3600, lambda: _fetch_with_retry(fetch))


def get_sector_summary(type):
    if type != "industry":
        raise DataSourceError("unknown type: %s" % type)

    def fetch():
        raw = _ak.stock_board_industry_summary_ths()
        name_map, _ = _industry_code_map()
        out = pd.DataFrame({
            "code": raw["板块"].map(name_map).map(normalize_code),
            "name": raw["板块"],
            "change_pct": pd.to_numeric(_ths_col(raw, "涨跌幅"), errors="coerce"),
            "up_count": pd.to_numeric(_ths_col(raw, "上涨家数"), errors="coerce"),
            "down_count": pd.to_numeric(_ths_col(raw, "下跌家数"), errors="coerce"),
            "leader": _ths_col(raw, "领涨股"),
            "leader_change_pct": pd.to_numeric(_ths_col(raw, "领涨股-涨跌幅"), errors="coerce"),
            "turnover": pd.to_numeric(_ths_col(raw, "总成交额"), errors="coerce") * 1e8,  # 亿元 → 元
        })
        out = out[out["code"].astype(str).str.isdigit()].reset_index(drop=True)  # 防御:名→码失败的行剔除
        return out.where(pd.notna(out), None)  # NaN → None,避免 NaN 污染 JSON/排序

    return _cached(_key("sector_summary", type), 60, lambda: _fetch_with_retry(fetch))


def get_sector_index_history(code, type):
    if type != "industry":
        raise DataSourceError("unknown type: %s" % type)
    symbol = normalize_code(code)
    end_date = datetime.now().strftime("%Y%m%d")  # 接口默认 end_date 已过期(20240108),必须显式传当天

    def fetch():
        raw = _ak.stock_board_industry_index_ths(symbol=symbol, start_date="20200101", end_date=end_date)
        return pd.DataFrame({
            "date": raw["日期"].astype(str),
            "open": raw["开盘价"], "high": raw["最高价"],
            "low": raw["最低价"], "close": raw["收盘价"],
            "volume": raw["成交量"],
        })

    return _cached(_key("sector_index", type, symbol), 1800, lambda: _fetch_with_retry(fetch))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /c/stock && python -m pytest tests/test_data_source.py -v`
Expected: PASS。若 `test_ttl_cache_mark_failure_backoff` 时序断言不稳,把两次 `mark_failure` 的预期写死(第一次退避 30s,第二次 60s),按实现 `min(30*2^n,600)` 核对后修正断言。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add data_source.py tests/test_data_source.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: 数据层 data_source.py(TTLCache/取数/代码归一化)"
```

---

### Task 5: 分析层 — 时间口径 + 指标 + 大盘

**Files:**
- Create: `analysis.py`(本任务先写时间口径、指标、大盘部分;板块/个股打分在 Task 6-7 追加)
- Test: `tests/test_analysis_time.py`

**Interfaces:**
- Produces(供 Task 6-8 消费):
  - `trading_minutes_elapsed(now: datetime) -> int` 9:30–11:30 + 13:00–15:00 内分钟数,午休不计,9:30 前=0,≥15:00=240。
  - `is_after_close(now) -> bool` `now.hour >= 15`。
  - `is_trading_time(now) -> bool` 周一至五且 9:30≤时钟≤15:00(含午休),用于每日快照写入守卫。
  - `day_adjusted_volume(cum_volume, elapsed_min) -> float | None` `cum*240/elapsed`;<15min → None。
  - `custom_volume_ratio(cum_volume, elapsed_min, avg_5d_volume) -> float | None` 折算估算量/近5日均量。
  - `filter_active(df) -> df` 过滤成交量/价格 ≤0。
  - `limit_threshold(code) -> float` 主板 9.9 / 创业+科创 19.9 / 北交 29.9。
  - `compute_breadth(spot_df, exclude_codes=None) -> dict` `{up,down,flat,limit_up,limit_down,total_turnover}`。
  - `add_ma(df, periods=(5,10,20,60)) -> df`、`add_macd(df) -> df`。
  - `_weighted(items) -> float | None` 子项缺失时按剩余权重归一化(内部工具)。

**口径(规格 §7):** 已交易分钟数禁止数自然分钟(午休计入会错 0.89 系数);涨停/跌停计数分母=活跃样本(停牌过滤:成交量为 0 或价格为 0 剔除)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_analysis_time.py
# -*- coding: utf-8 -*-
from datetime import datetime

import pandas as pd
import pytest

import analysis as an


def dt(h, m, weekday=0):
    d = datetime(2026, 8, 10, h, m)  # 2026-08-10 为周一
    while d.weekday() != weekday:
        d = datetime(2026, 8, 10 + (weekday + 1 - d.weekday()) % 7, h, m)
    return d


def test_trading_minutes_elapsed():
    assert an.trading_minutes_elapsed(dt(9, 20)) == 0            # 开盘前
    assert an.trading_minutes_elapsed(dt(9, 45)) == 15
    assert an.trading_minutes_elapsed(dt(10, 30)) == 60
    assert an.trading_minutes_elapsed(dt(11, 30)) == 120         # 午休起点
    assert an.trading_minutes_elapsed(dt(13, 0)) == 120          # 午休不计入
    assert an.trading_minutes_elapsed(dt(14, 0)) == 180          # 关键:非270
    assert an.trading_minutes_elapsed(dt(15, 0)) == 240          # 收盘


def test_is_after_close_and_trading_time():
    assert an.is_after_close(dt(14, 59)) is False
    assert an.is_after_close(dt(15, 0)) is True
    assert an.is_trading_time(dt(10, 0, weekday=0)) is True      # 周一盘中
    assert an.is_trading_time(dt(12, 0, weekday=0)) is True      # 午休仍算交易日
    assert an.is_trading_time(dt(9, 0, weekday=0)) is False      # 开盘前
    assert an.is_trading_time(dt(10, 0, weekday=5)) is False     # 周六


def test_day_adjusted_volume_and_ratio():
    assert an.day_adjusted_volume(100000, 60) == pytest.approx(400000)   # 100000*240/60
    assert an.day_adjusted_volume(100000, 14) is None                    # <15min 下限
    assert an.day_adjusted_volume(100000, 0) is None
    assert an.custom_volume_ratio(100000, 60, 200000) == pytest.approx(2.0)
    assert an.custom_volume_ratio(100000, 14, 200000) is None            # 开盘尖峰不计算
    assert an.custom_volume_ratio(100000, 60, 0) is None


def test_filter_active_and_limit_threshold():
    df = pd.DataFrame({"code": ["600000", "688981", "830000"],
                       "price": [10.0, 0.0, 5.0], "volume": [100, 200, 0]})
    act = an.filter_active(df)
    assert list(act["code"]) == ["600000"]
    assert an.limit_threshold("600000") == 9.9
    assert an.limit_threshold("688981") == 19.9
    assert an.limit_threshold("300750") == 19.9
    assert an.limit_threshold("830000") == 29.9
    assert an.limit_threshold("400001") == 29.9


def test_compute_breadth():
    df = pd.DataFrame({"code": ["600000", "600519", "000001", "688981", "830000", "300750"],
                       "price": [10, 20, 30, 40, 50, 60],
                       "volume": [1, 1, 1, 1, 1, 0],           # 300750 停牌
                       "change_pct": [10.0, -10.0, 0.0, 5.0, 30.0, 5.0],  # 688981 +5% 未触板(19.9)
                       "amount": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    b = an.compute_breadth(df)
    assert b == {"up": 3, "down": 1, "flat": 1, "limit_up": 2, "limit_down": 1,
                 "total_turnover": pytest.approx(1500.0)}
    # 新股排除:830000 是北交所新股 → 剔除后 limit_up 少一个
    b2 = an.compute_breadth(df, exclude_codes={"830000"})
    assert b2["limit_up"] == 1


def test_ma_and_macd_columns():
    df = pd.DataFrame({"close": [float(i) for i in range(1, 65)]})
    out = an.add_ma(df)
    assert list(out.columns) == ["close", "ma5", "ma10", "ma20", "ma60"]
    assert out["ma20"].iloc[-1] == pytest.approx(54.5)          # (45..64 均值)
    macd = an.add_macd(df)
    assert "dif" in macd.columns and "dea" in macd.columns and "macd" in macd.columns
    assert not macd["dif"].isna().iloc[-1]


def test_weighted_renormalize():
    assert an._weighted([(50.0, 40), (80.0, 20), (None, 25), (100.0, 15)]) == pytest.approx(
        (50 * 40 + 80 * 20 + 100 * 15) / 75)                    # 缺失 25 权重被归一化
    assert an._weighted([(None, 40), (None, 20)]) is None
    assert an._weighted([]) is None
    assert an._weighted([(float("nan"), 40), (80.0, 60)]) == pytest.approx(80.0)  # NaN 视为缺失
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /c/stock && python -m pytest tests/test_analysis_time.py -v`
Expected: FAIL(`import analysis` 失败)。

- [ ] **Step 3: 写实现 analysis.py(本任务部分)**

```python
# -*- coding: utf-8 -*-
"""分析层:时间口径 + 指标 + 打分。纯函数,输入可注入,便于单测。"""
from datetime import datetime

import pandas as pd

# ---- 时间口径(规格 §7 P1/A/B) ----

AM_START = 9 * 60 + 30
AM_END = 11 * 60 + 30
PM_START = 13 * 60
PM_END = 15 * 60


def trading_minutes_elapsed(now: datetime) -> int:
    m = now.hour * 60 + now.minute
    if m < AM_START:
        return 0
    total = 0
    if m <= AM_END:
        total += m - AM_START
    else:
        total += AM_END - AM_START
    if m >= PM_END:
        total += PM_END - PM_START
    elif m >= PM_START:
        total += m - PM_START
    return total


def is_after_close(now: datetime) -> bool:
    return now.hour >= 15


def is_trading_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return AM_START <= m <= PM_END


def day_adjusted_volume(cum_volume: float, elapsed_min: int):
    if elapsed_min <= 0 or elapsed_min < 15:
        return None
    return cum_volume * 240.0 / elapsed_min


def custom_volume_ratio(cum_volume: float, elapsed_min: int, avg_5d_volume):
    if avg_5d_volume is None or avg_5d_volume <= 0:
        return None
    adj = day_adjusted_volume(cum_volume, elapsed_min)
    if adj is None:
        return None
    return adj / avg_5d_volume


# ---- 停牌过滤与涨停近似(规格 §7) ----

def filter_active(df):
    return df[(df["volume"] > 0) & (df["price"] > 0)]


def limit_threshold(code: str) -> float:
    c = str(code)
    if c.startswith(("30", "688")):
        return 19.9
    if c.startswith(("8", "4")):
        return 29.9
    return 9.9


def compute_breadth(spot_df, exclude_codes=None):
    df = filter_active(spot_df)
    if exclude_codes:
        df = df[~df["code"].isin(exclude_codes)]
    if len(df) == 0:
        return {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "total_turnover": 0.0}
    up = int((df["change_pct"] > 0).sum())
    down = int((df["change_pct"] < 0).sum())
    flat = int((df["change_pct"] == 0).sum())
    limit_up = 0
    limit_down = 0
    for r in df.itertuples(index=False):
        th = limit_threshold(r.code)
        if r.change_pct >= th:
            limit_up += 1
        elif r.change_pct <= -th:
            limit_down += 1
    return {"up": up, "down": down, "flat": flat, "limit_up": limit_up,
            "limit_down": limit_down, "total_turnover": float(df["amount"].sum())}


# ---- 指标(规格 §6.3/§7) ----

def add_ma(df, periods=(5, 10, 20, 60)):
    out = df.copy()
    for p in periods:
        out["ma%d" % p] = out["close"].rolling(p).mean()
    return out


def add_macd(df):
    out = df.copy()
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()
    out["macd"] = 2 * (out["dif"] - out["dea"])
    return out


def _is_missing(v):
    """None 或 NaN 都视为缺失。"""
    return v is None or v != v


def _weighted(items):
    """items: [(value|None|NaN, weight)];缺失项移除,剩余按原比例归一化到 100%。"""
    vals = [(v, w) for v, w in items if not _is_missing(v)]
    if not vals:
        return None
    total_w = sum(w for _, w in vals)
    return sum(v * w / total_w for v, w in vals)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /c/stock && python -m pytest tests/test_analysis_time.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add analysis.py tests/test_analysis_time.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: 分析层时间口径/指标/大盘广度"
```

---

### Task 6: 分析层 — 板块打分与结论

**Files:**
- Modify: `analysis.py`(追加板块打分)
- Test: `tests/test_analysis_sector.py`

**Interfaces:**
- Consumes: Task 5 的 `_weighted`、`filter_active`、`limit_threshold`。
- Produces(供 Task 8 消费):
  - `sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change_pct) -> float | None`
  - `sector_strength(index_change, consecutive_days, activity) -> float`
  - `sector_risk(index_change, change_3d, turnover_ratio, prev_change, up_ratio) -> float`
  - `sector_verdict(emotion, strength, risk, consecutive_days, data_complete) -> str`
  - `composite_score(strength, emotion, risk) -> float | None`

**口径(规格 §6.2/§7):**
- 情绪分 = 加权(上涨家数占比 40% + 涨停占比 20% + 成交额放量 25% + 领涨股强度 15%),任一子项缺失走 `_weighted` 归一化;`turnover_ratio` 仅收盘后由调用方传入,盘中传 None。
- 子项得分换算(线性基线):上涨占比→×100;涨停占比→×100(cap 100);成交额放量→`turnover_ratio/1.5×100`(cap 100);领涨股强度→`leader×20`(cap 100);指数涨幅→`index_change×20`(cap 100);连续上榜→`days×25`(cap 100);资金活跃度→`activity/0.03×100`(cap 100,activity=板块成交额/全市场成交额)。
- 强度分 = 加权(指数涨幅 40% + 连续上榜 30% + 资金活跃度 30%),活跃度盘中可用。
- 风险分 = **加性条件**(缺失项计 0,**不归一化**):单日涨幅>5% +40;3日累计>10% +40;放量滞涨 +40(仅收盘后,即 turnover_ratio 与 prev_change 均非 None 时才评估);上涨占比<0.35 严重分化 +40;cap 100。单个信号=40(中档),任意两信号=80≥65(高)。
- 结论规则优先级 1→6(规格 §6.2 表)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_analysis_sector.py
# -*- coding: utf-8 -*-
import pytest
import analysis as an


def test_emotion_score_weights_and_renormalize():
    # 全子项齐备:up 0.9, limit 0.1, turnover_ratio 1.5, leader 5.0
    e = an.sector_emotion(0.9, 0.1, 1.5, 5.0)
    # 0.9*100=90(40) + 0.1*100=10(20) + 1.5/1.5*100=100(25) + 5*20=100(15)
    assert e == pytest.approx((90 * 40 + 10 * 20 + 100 * 25 + 100 * 15) / 100)
    # 盘中:turnover_ratio=None → 25 权重归一化到剩余 75
    e2 = an.sector_emotion(0.9, 0.1, None, 5.0)
    assert e2 == pytest.approx((90 * 40 + 10 * 20 + 100 * 15) / 75)
    # 缺 leader → leader 15 权重归一化到剩余 85(降级口径下涨停占比恒 None,同样由调用方归一化)
    e3 = an.sector_emotion(0.9, 0.1, 1.5, None)
    assert e3 == pytest.approx((90 * 40 + 10 * 20 + 100 * 25) / 85)
    assert an.sector_emotion(None, None, None, None) is None


def test_strength_score():
    s = an.sector_strength(5.0, 4, 0.03)   # 指数5%→100,上榜4天→100,活跃3%→100
    assert s == pytest.approx(100.0)
    s2 = an.sector_strength(1.0, 1, None)  # 活跃度缺失 → 40/30 归一化
    assert s2 == pytest.approx((20 * 40 + 25 * 30) / 70)
    s3 = an.sector_strength(None, 2, 0.03)  # 指数涨幅缺失 → 该子项降级,其余归一化
    assert s3 == pytest.approx((50 * 30 + 100 * 30) / 60)


def test_risk_single_and_double():
    # 单个信号:仅 3日累计>10% → 40(中档)
    assert an.sector_risk(3.0, 12.0, None, None, 0.6) == pytest.approx(40)
    # 两个信号:3日累计>10% + 严重分化 → 80(高)
    assert an.sector_risk(3.0, 12.0, None, None, 0.3) == pytest.approx(80)
    # 单日涨幅>5% 单信号 → 40
    assert an.sector_risk(6.0, None, None, None, 0.6) == pytest.approx(40)


def test_risk_voluptake_only_after_close():
    # 盘中:turnover_ratio=None → 放量滞涨不评估
    r_daytime = an.sector_risk(0.5, None, None, 1.5, 0.6)
    assert r_daytime == pytest.approx(0)
    # 收盘后:放量滞涨(成交额1.8倍均值 + 指数涨0.5<前日1.5 且 <2%)→ +40
    r_close = an.sector_risk(0.5, None, 1.8, 1.5, 0.6)
    assert r_close == pytest.approx(40)
    # 指数涨幅≥2% → 不构成滞涨
    assert an.sector_risk(2.5, None, 1.8, 1.5, 0.6) == pytest.approx(0)
    # 指数涨幅 ≥ 前日 → 不构成滞涨
    assert an.sector_risk(2.0, None, 1.8, 1.0, 0.6) == pytest.approx(0)


def test_verdict_rule1_2_3():
    # P1:强度低 + 风险高 → 回避
    assert an.sector_verdict(20, 20, 80, 1, True) == "风险提示/回避"
    # P2:风险高 + 情绪≥中 → 谨慎追高(全中+高风险,规格 C5')
    assert an.sector_verdict(50, 50, 70, 1, True) == "谨慎追高(过热)"
    # P2:风险高 + 强度≥中
    assert an.sector_verdict(20, 65, 80, 3, True) == "谨慎追高(过热)"
    # P3:情绪高 + 强度高 + 风险非高 → 建议关注
    assert an.sector_verdict(75, 70, 30, 2, True) == "建议关注"


def test_verdict_rule4_5_6():
    # P4:情绪高 + 强度中 + 连续上榜≥2 → 跟踪
    assert an.sector_verdict(75, 55, 20, 2, True) == "跟踪(热点延续)"
    # P5:情绪高 + 强度中/低 + 连续=1 + data_complete → 警惕一日游
    assert an.sector_verdict(75, 55, 20, 1, True) == "警惕一日游"
    # 冷启动 day1:data_complete=False → 不触发一日游 → 观望
    assert an.sector_verdict(75, 55, 20, 1, False) == "观望"
    # P6 其余 → 观望
    assert an.sector_verdict(50, 30, 50, 1, True) == "观望"


def test_composite_arithmetic():
    # 规格 §8 校验:0.4*82 + 0.35*78 + 0.25*(100-55) = 71.35(综合分=0.4强度+0.35情绪+0.25×(100−风险);规格示例 risk=55)
    assert an.composite_score(82, 78, 55) == pytest.approx(71.35)
    # 任一维度缺失 → None
    assert an.composite_score(None, 78, 55) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /c/stock && python -m pytest tests/test_analysis_sector.py -v`
Expected: FAIL(`sector_emotion` 未定义)。

- [ ] **Step 3: 写实现(追加到 analysis.py 末尾)**

```python
# ---- 板块打分(规格 §6.2) ----

def sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change_pct):
    return _weighted([
        (up_ratio * 100 if up_ratio is not None else None, 40),
        (min(100.0, limit_ratio * 100) if limit_ratio is not None else None, 20),
        (min(100.0, turnover_ratio / 1.5 * 100) if turnover_ratio is not None else None, 25),
        (min(100.0, leader_change_pct * 20) if leader_change_pct is not None else None, 15),
    ])


def sector_strength(index_change, consecutive_days, activity):
    return _weighted([
        (min(100.0, index_change * 20) if index_change is not None else None, 40),
        (min(100.0, consecutive_days * 25) if consecutive_days is not None else None, 30),
        (min(100.0, activity / 0.03 * 100) if activity is not None else None, 30),
    ])


def sector_risk(index_change, change_3d, turnover_ratio, prev_change, up_ratio):
    """加性条件,cap 100;缺失项计 0(不归一化)。"""
    score = 0
    if index_change is not None and index_change > 5:
        score += 40
    if change_3d is not None and change_3d > 10:
        score += 40
    if (turnover_ratio is not None and prev_change is not None and index_change is not None
            and turnover_ratio > 1.5 and index_change < 2 and index_change < prev_change):
        score += 40  # 板块放量滞涨(仅收盘后:调用方盘中传 turnover_ratio=None)
    if up_ratio is not None and up_ratio < 0.35:
        score += 40  # 严重分化
    return min(100.0, score)


def sector_verdict(emotion, strength, risk, consecutive_days, data_complete):
    e_hi = emotion is not None and emotion >= 70
    e_mid = emotion is not None and emotion >= 45
    s_hi = strength is not None and strength >= 60
    s_mid = strength is not None and strength >= 35
    r_hi = risk is not None and risk >= 65
    if strength is not None and strength < 35 and r_hi:
        return "风险提示/回避"                                  # P1
    if r_hi and (e_mid or s_mid):
        return "谨慎追高(过热)"                                  # P2
    if e_hi and s_hi and not r_hi:
        return "建议关注"                                        # P3
    if e_hi and s_mid and not s_hi and consecutive_days >= 2 and not r_hi:
        return "跟踪(热点延续)"                                  # P4
    if e_hi and s_mid and consecutive_days == 1 and data_complete and not r_hi:
        return "警惕一日游"                                      # P5
    return "观望"                                                # P6


def composite_score(strength, emotion, risk):
    if strength is None or emotion is None or risk is None:
        return None
    return round(0.4 * strength + 0.35 * emotion + 0.25 * (100 - risk), 2)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /c/stock && python -m pytest tests/test_analysis_sector.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add analysis.py tests/test_analysis_sector.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: 板块情绪/强度/风险打分与结论规则"
```

---

### Task 7: 分析层 — 个股量价打分

**Files:**
- Modify: `analysis.py`(追加个股打分)
- Test: `tests/test_analysis_stock.py`

**Interfaces:**
- Consumes: Task 5 的 `trading_minutes_elapsed`、`custom_volume_ratio`、`add_ma`、`add_macd`。
- Produces(供 Task 8 消费):
  - `compute_trend_score(daily_df) -> float`
  - `compute_volume_price_score(daily_df, quote, now) -> float`
  - `compute_signal_score(daily_df, quote) -> float`
  - `compute_stock_risk(daily_df, quote, now) -> float`
  - `stock_composite(trend, vp, signal) -> float`
  - `stock_verdict(composite, risk) -> str`
  - `score_stock(daily_df, quote, now) -> dict {trend, volume_price, signal, risk, composite, verdict}`
- `quote` dict 字段:`name, code, price, prev_close, open, high, low, volume, turnover, change_pct`(Task 4 产出)。

**口径(规格 §6.3/§7):** 趋势分=基础40+多头排列30+站上MA20 20+站上MA60 10;量价分=基础40+放量上攻30+放量突破平台30+缩量健康回踩30(cap 100);信号分=基础50+MACD金叉40−死叉40+突破前20日平台30(夹 0-100);风险项 max:乖离>15% +70、放量跌破MA20 +70、放量滞涨 +50、高位长上影 +30;`avg_5d = daily_df['volume'].iloc[-6:-1].mean()`(前5日,不含最后一根)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_analysis_stock.py
# -*- coding: utf-8 -*-
from datetime import datetime

import pandas as pd
import pytest

import analysis as an


def quote(**kw):
    q = {"name": "X", "code": "sh600000", "price": 10.0, "prev_close": 9.5,
         "open": 9.6, "high": 10.2, "low": 9.5, "volume": 1_000_000,
         "turnover": 5e7, "change_pct": 5.0}
    q.update(kw)
    return q


def make_daily(closes, volumes=None):
    """构造日线 DataFrame,date 递增。"""
    n = len(closes)
    vols = volumes or [100000] * n
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    opens = [c for c in closes]
    return pd.DataFrame({
        "date": [f"2026-01-{i+1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})


def dt_now(h=10, m=30):
    return datetime(2026, 8, 11, h, m)  # 周二盘中


def test_trend_score_bull_alignment():
    # 持续上升 → 多头排列 + 站上 MA20/60 → 高分
    closes = [float(10 + i * 0.3) for i in range(65)]
    df = make_daily(closes)
    assert an.compute_trend_score(df) == pytest.approx(100.0)
    # 持续下跌 → 破位 → 低分(基础40)
    df2 = make_daily([float(100 - i * 0.5) for i in range(65)])
    assert an.compute_trend_score(df2) == pytest.approx(40.0)
    # 数据不足 → 基础分
    assert an.compute_trend_score(make_daily([1.0, 2.0])) == pytest.approx(50.0)


def test_volume_price_score_volume_surge():
    # 量比2.0 + 涨幅5% → 放量上攻 +30
    df = make_daily([float(10 + i) for i in range(30)])
    df["volume"] = [100000] * 29 + [200000]   # 今日显著放量(但 avg5 用 -6:-1,今日计入后 5 日均量被抬高)
    # 注:avg5 取 [-6:-1] 不含最后一根 → 5日均量=100000
    q = quote(volume=400000, change_pct=5.0)  # 10:30 → 折算 400000*240/60=1600000 → 量比16
    s = an.compute_volume_price_score(df, q, dt_now())
    assert s == pytest.approx(70.0)  # 40+30(放量上攻);未突破平台


def test_volume_price_shrink_healthy_pullback():
    # 缩量(量比<0.7)+ 跌幅≤3% + 站上 MA20 → 缩量健康回踩
    closes = [float(10 + i) for i in range(30)]
    df = make_daily(closes)
    df["volume"] = [100000] * 29 + [100000]
    q = quote(volume=10000, change_pct=-1.0, price=30.0)  # 10:30 折算量比≈0.4
    s = an.compute_volume_price_score(df, q, dt_now())
    # 站上 MA20(close=30 > ma20≈24.5) + 缩量 + 跌幅≤3 → +30
    assert s == pytest.approx(70.0)


def test_signal_score_macd_golden_cross_and_breakout():
    # 构造 V 型:前段下跌后急升 → 金叉
    closes = [float(50 - i) for i in range(30)] + [float(20 + i * 2) for i in range(30)]
    df = make_daily(closes)
    q = quote(price=closes[-1] * 1.1)  # 突破前20日平台
    s = an.compute_signal_score(df, q)
    assert s > 50.0  # 金叉 +40,突破 +30 → 120 封顶 100


def test_stock_risk_max_semantics():
    # 乖离率>15% → 70
    closes = [10.0] * 30
    closes[-1] = 30.0   # MA20≈11 → 乖离≈173%
    df = make_daily(closes)
    q = quote(price=30.0, change_pct=10.0)
    assert an.compute_stock_risk(df, q, dt_now()) == pytest.approx(70)
    # 放量跌破 MA20 → 70(风险分=max,两风险项取最大值 70)
    closes2 = [10.0] * 30
    df2 = make_daily(closes2)
    q2 = quote(price=5.0, change_pct=-5.0, volume=500000)  # 10:30 量比20>1.5,破MA20
    assert an.compute_stock_risk(df2, q2, dt_now()) == pytest.approx(70)
    # 无风险项 → 0(温和上涨、缩量、价格≈MA20、无长上影)
    df3 = make_daily([float(10 + i) for i in range(30)])
    q3 = quote(price=19.5, change_pct=1.0, volume=20000,
               high=19.6, low=19.4, open=19.5)   # 量比≈0.8<1.5,乖离≈0,无上影
    assert an.compute_stock_risk(df3, q3, dt_now()) == pytest.approx(0)


def test_stock_composite_and_verdict():
    assert an.stock_composite(70, 65, 60) == pytest.approx(65.75)  # 规格 §8 校验
    # 综合分表:≥70 无重大→关注;≥70 有重大→规避
    assert an.stock_verdict(80, 30) == "关注"
    assert an.stock_verdict(80, 70) == "规避"
    # 55-69 无重大→持有/跟踪;有重大→回调风险
    assert an.stock_verdict(60, 20) == "持有/跟踪"
    assert an.stock_verdict(60, 70) == "回调风险"
    # <55 无重大→观望;有重大→规避
    assert an.stock_verdict(50, 30) == "观望"
    assert an.stock_verdict(50, 70) == "规避"


def test_score_stock_wrapper():
    closes = [float(10 + i * 0.2) for i in range(65)]
    df = make_daily(closes)
    q = quote(price=closes[-1], change_pct=2.0, volume=300000)
    out = an.score_stock(df, q, dt_now(15, 0))  # 收盘后
    assert set(out) == {"trend", "volume_price", "signal", "risk", "composite", "verdict"}
    assert 0 <= out["risk"] <= 100
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /c/stock && python -m pytest tests/test_analysis_stock.py -v`
Expected: FAIL(`compute_trend_score` 未定义)。

- [ ] **Step 3: 写实现(追加到 analysis.py 末尾)**

```python
# ---- 个股量价打分(规格 §6.3/§7) ----

def compute_trend_score(daily_df):
    if len(daily_df) < 5:
        return 50.0
    df = add_ma(daily_df)
    last = df.iloc[-1]
    score = 40.0
    m5, m10, m20, m60 = last["ma5"], last["ma10"], last["ma20"], last["ma60"]
    if not pd.isna(m5) and not pd.isna(m10) and not pd.isna(m20) and m5 > m10 > m20:
        score += 30
    if not pd.isna(m20) and last["close"] > m20:
        score += 20
    if not pd.isna(m60) and last["close"] > m60:
        score += 10
    return score


def _avg5_volume(daily_df):
    if len(daily_df) < 6:
        return None
    v = daily_df["volume"].iloc[-6:-1].mean()
    return None if pd.isna(v) or v <= 0 else v


def compute_volume_price_score(daily_df, quote, now):
    if len(daily_df) < 5:
        return 50.0
    elapsed = trading_minutes_elapsed(now)
    vr = custom_volume_ratio(quote.get("volume", 0), elapsed, _avg5_volume(daily_df))
    price = quote.get("price", daily_df["close"].iloc[-1])
    change = quote.get("change_pct", 0.0)
    score = 40.0
    if vr is not None:
        if vr > 1.5 and change > 0:
            score += 30  # 放量上攻
        df = add_ma(daily_df, (20,))
        ma20 = df["ma20"].iloc[-1]
        if vr > 1.5 and len(daily_df) > 21 and price > daily_df["high"].iloc[-22:-1].max():
            score += 30  # 放量突破平台
        elif (vr < 0.7 and change >= -3 and not pd.isna(ma20) and price > ma20):
            score += 30  # 缩量健康回踩
    return min(100.0, score)


def compute_signal_score(daily_df, quote):
    if len(daily_df) < 26:
        return 50.0
    df = add_macd(daily_df)
    last, prev = df.iloc[-1], df.iloc[-2]
    score = 50.0
    if not pd.isna(last["dif"]) and not pd.isna(last["dea"]):
        if prev["dif"] <= prev["dea"] and last["dif"] > last["dea"]:
            score += 40   # MACD 金叉
        elif prev["dif"] >= prev["dea"] and last["dif"] < last["dea"]:
            score -= 40   # MACD 死叉
    if len(df) > 21:
        prev_high = df["high"].iloc[-21:-1].max()  # 前20日(不含当日)
        price = quote.get("price", last["close"])
        if price > prev_high:
            score += 30   # 突破近期平台高点
    return min(100.0, max(0.0, score))


def compute_stock_risk(daily_df, quote, now):
    items = []
    df = add_ma(daily_df, (20,))
    ma20 = df["ma20"].iloc[-1]
    price = quote.get("price", df["close"].iloc[-1])
    change = quote.get("change_pct", 0.0)
    # 乖离率偏离 MA20>15%
    if not pd.isna(ma20) and ma20 > 0:
        bias = (price - ma20) / ma20 * 100
        if bias > 15:
            items.append(70)
    # 放量跌破 MA20
    vr = custom_volume_ratio(quote.get("volume", 0), trading_minutes_elapsed(now), _avg5_volume(daily_df))
    if vr is not None and vr > 1.5 and not pd.isna(ma20) and price < ma20 and change < 0:
        items.append(70)
    # 放量滞涨:量比>1.5 且 当日涨幅<前日涨幅 且 当日涨幅<2%
    if vr is not None and vr > 1.5 and len(daily_df) >= 3:
        prev_change = (daily_df["close"].iloc[-2] / daily_df["close"].iloc[-3] - 1) * 100
        if change < prev_change and change < 2:
            items.append(50)
    # 高位长上影:上影线>实体2倍 且 振幅>5%
    high = quote.get("high"); low = quote.get("low"); op = quote.get("open")
    if high is not None and low is not None and op is not None and price > 0:
        body = max(op, price) - min(op, price)
        upper = high - max(op, price)
        amplitude = (high - low) / max(op, price) * 100 if max(op, price) > 0 else 0
        if body > 0 and upper > 2 * body and amplitude > 5:
            items.append(30)
    return max(items) if items else 0


def stock_composite(trend, vp, signal):
    return round(0.4 * trend + 0.35 * vp + 0.25 * signal, 2)


def stock_verdict(composite, risk):
    major = risk >= 70
    if composite >= 70:
        return "规避" if major else "关注"
    if composite >= 55:
        return "回调风险" if major else "持有/跟踪"
    return "规避" if major else "观望"


def score_stock(daily_df, quote, now):
    trend = compute_trend_score(daily_df)
    vp = compute_volume_price_score(daily_df, quote, now)
    signal = compute_signal_score(daily_df, quote)
    risk = compute_stock_risk(daily_df, quote, now)
    composite = stock_composite(trend, vp, signal)
    return {"trend": trend, "volume_price": vp, "signal": signal,
            "risk": risk, "composite": composite, "verdict": stock_verdict(composite, risk)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /c/stock && python -m pytest tests/test_analysis_stock.py -v`
Expected: PASS。若量价分个别断言因折算口径偏差,先手工验算(10:30→已交易 60 分钟,volume=400000→折算 1600000,avg5=100000→量比 16>1.5 且涨幅>0 → 70)。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add analysis.py tests/test_analysis_stock.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: 个股趋势/量价/信号打分与风险合成"
```

---

### Task 8: API 层 app.py(契约路由 + 错误处理)

**Files:**
- Create: `app.py`(覆盖 Task 2 骨架)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `data_source as ds`、`analysis as an`、`store`;模块级导入方式(`import data_source as ds`),测试用 monkeypatch 替换模块属性。
- Produces: `create_app(db_path=None) -> Flask app`;`python app.py` 起 127.0.0.1:8000。
- 路由(规格 §8):`GET /`、`GET /api/market`、`GET /api/sectors?type=&top=&search=`、`GET /api/sector?code=type:code`、`GET /api/stock?code=600519|sh600519`。
- 错误契约:400 `BAD_PARAM`(参数非法);500 `SOURCE_FAIL`(完全失败无缓存);数据源失败但有缓存 → 200 `meta.stale=true`。

**口径(规格 §8/§11):** `/api/sectors` 返回 top60(按综合分降序,None 排后)+ search(`total`=匹配数);`/api/sector` 校验 `type:code`;`/api/stock` 两种 code 写法;每日快照仅在 `is_trading_time` 时 upsert;`volume_vs_yesterday` 与板块放量口径仅收盘后计算。**板块评分降级口径(无成分股聚合):** 上涨家数占比=摘要上涨/(上涨+下跌),涨停占比恒 None(情绪子项归一化),领涨强度=摘要领涨股-涨跌幅,`data_complete` 恒 True。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api.py
# -*- coding: utf-8 -*-
import datetime
import os
import threading

import pandas as pd
import pytest

import analysis as an
import app as app_mod
import data_source as ds
import store


def make_spot():
    return pd.DataFrame({
        "code": ["600000", "600519", "000001", "688981"],
        "name": ["浦发银行", "贵州茅台", "平安银行", "中芯国际"],
        "price": [10.0, 1348.9, 12.0, 45.0],
        "change_pct": [1.0, 0.3, -2.0, 5.0],
        "volume": [100000, 682720, 80000, 90000],
        "amount": [1e8, 9.2e8, 2e8, 5e8],
    })


def make_summary():
    return pd.DataFrame({
        "code": ["885887", "885559"],
        "name": ["半导体", "白酒"],
        "change_pct": [3.2, -1.0],
        "up_count": [80.0, 30.0],
        "down_count": [5.0, 60.0],
        "leader": ["X", "Y"],
        "leader_change_pct": [10.0, 0.5],
        "turnover": [None, None],
    })


def make_quote():
    return {"name": "贵州茅台", "code": "sh600519", "price": 1348.9, "prev_close": 1345.0,
            "open": 1348.0, "high": 1352.65, "low": 1338.18, "volume": 682720,
            "turnover": 919035968, "change_pct": 0.3}


def make_daily():
    n = 65
    closes = [float(1000 + i) for i in range(n)]
    return pd.DataFrame({
        "date": [f"2026-05-{i%28+1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [682720] * n})


def make_minute():
    return pd.DataFrame({"time": ["10:00", "10:01"], "price": [1348.0, 1349.0],
                         "avg": [1342.0, 1343.0], "volume": [12000, 5000]})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "get_index_realtime", lambda: (
        [{"code": "sh000001", "name": "上证指数", "price": 3456.78, "change_pct": 0.45}], False))
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), False))
    monkeypatch.setattr(ds, "get_sector_index_history", lambda c, t: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (make_minute(), False))
    monkeypatch.setattr(ds, "get_stock_quote", lambda c: (make_quote(), False))
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(an, "is_after_close", lambda now: True)   # 测试按收盘后口径
    monkeypatch.setattr(an, "is_trading_time", lambda now: True)
    app.config["TESTING"] = True
    return app.test_client()


def test_market_endpoint(client):
    r = client.get("/api/market")
    body = r.get_json()
    assert body["ok"] is True and body["meta"]["stale"] is False
    d = body["data"]
    assert d["indices"][0]["code"] == "sh000001"
    assert d["breadth"]["up"] == 3  # 600000(1.0) 600519(0.3) 688981(5.0) 上涨;000001 -2.0 下跌
    assert d["breadth"]["down"] == 1
    assert d["breadth"]["limit_up"] == 0  # 无一达到阈值
    assert d["volume_vs_yesterday"]["pct"] is None  # 无昨日数据


def test_sectors_search_and_composite(client):
    r = client.get("/api/sectors?type=industry")
    body = r.get_json()
    assert body["ok"] is True
    s = body["data"]["sectors"]
    # 半导体:emotion=? strength=? 仅断言结构 + 综合分算术一致性
    semi = next(x for x in s if x["code"] == "industry:885887")
    assert semi["consecutive_days"] == 1  # 当日已 upsert(rank 1≤20),从今天起算连续 1 天
    assert semi["data_complete"] is True
    assert semi["composite_score"] is None or 0 <= semi["composite_score"] <= 100
    r2 = client.get("/api/sectors?type=industry&search=半导")
    assert r2.get_json()["data"]["total"] == 1
    assert r2.get_json()["data"]["sectors"][0]["name"] == "半导体"


def test_sector_detail(client):
    r = client.get("/api/sector?code=industry:885887")
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    assert d["name"] == "半导体"
    assert d["index_history"][0]["date"].startswith("2026-")
    assert d["scores"]["composite"] is None or 0 <= d["scores"]["composite"] <= 100


def test_sector_bad_param(client):
    r = client.get("/api/sector?code=foo:123")
    body = r.get_json()
    assert r.status_code == 400 and body["error"]["code"] == "BAD_PARAM"
    r2 = client.get("/api/sector?code=industry:abc")
    assert r2.status_code == 400
    r3 = client.get("/api/sectors?type=xxx")
    assert r3.status_code == 400


def test_stock_two_code_forms(client):
    r1 = client.get("/api/stock?code=600519")
    r2 = client.get("/api/stock?code=sh600519")
    d1, d2 = r1.get_json()["data"], r2.get_json()["data"]
    assert d1["code"] == "sh600519" and d2["code"] == "sh600519"
    assert d1["name"] == "贵州茅台"
    assert d1["quote"]["change_pct"] == pytest.approx(0.3)
    assert d1["kline"][0]["date"].startswith("2026-")
    assert d1["intraday"][0]["time"] == "10:00"
    # 综合分算术:0.4*趋势+0.35*量价+0.25*信号(这里 daily 是持续上涨 → 高分)
    assert 0 <= d1["scores"]["composite"] <= 100


def test_stock_bad_param(client):
    assert client.get("/api/stock?code=abc").status_code == 400
    assert client.get("/api/stock?code=600519extra").status_code == 400


def test_source_fail_returns_500(monkeypatch, tmp_path):
    db = str(tmp_path / "fail.db")
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_market_spot",
                        lambda: (_ for _ in ()).throw(ds.DataSourceError("boom")))
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/api/market")
    assert r.status_code == 500
    assert r.get_json()["error"]["code"] == "SOURCE_FAIL"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /c/stock && python -m pytest tests/test_api.py -v`
Expected: FAIL(`create_app` 无这些路由)。

- [ ] **Step 3: 写实现 app.py**

```python
# -*- coding: utf-8 -*-
"""A股三层分析看板 —— Flask 入口 + API 路由。"""
import os
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import analysis as an
import data_source as ds
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "market.db")
SECTOR_TYPES = ("industry",)


def ok(data, stale=False):
    return jsonify({"ok": True,
                    "meta": {"stale": stale, "updated_at": ds.last_updated_at},
                    "data": data})


def err(code, message, http):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), http


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _num(v):
    """None/NaN/非数值 → None,其余 → float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sort_key(v):
    """数值排序键:None/非数值排最后。"""
    n = _num(v)
    return float("-inf") if n is None else n


def _parse_sector_code(raw):
    if not raw or ":" not in raw:
        return None
    t, c = raw.split(":", 1)
    if t not in SECTOR_TYPES or not (c.isdigit() and len(c) == 6):
        return None
    return t, c


def _parse_stock_code(raw):
    if not raw:
        return None
    c = ds.normalize_code(raw)
    if not (c.isdigit() and len(c) == 6):
        return None
    return c


# ---------- API ----------

def register_routes(app):
    db_path = app.config["DB"]

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/market")
    def api_market():
        try:
            spot, spot_stale = ds.get_market_spot()
            indices, idx_stale = ds.get_index_realtime()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        today = _today()
        exclude = ds.get_new_stocks()
        breadth = an.compute_breadth(spot, exclude)
        volume_pct = None
        if an.is_after_close(now):
            prev = store.get_market_daily_prev(db_path, today)
            if prev and prev["total_turnover"]:
                volume_pct = round(
                    (breadth["total_turnover"] - prev["total_turnover"]) / prev["total_turnover"] * 100, 2)
        if an.is_trading_time(now):
            index_close = indices[0]["price"] if indices else None
            store.upsert_market_daily(
                db_path, today, breadth["up"], breadth["down"], breadth["flat"],
                breadth["limit_up"], breadth["limit_down"], breadth["total_turnover"],
                index_close, now.strftime("%Y-%m-%d %H:%M:%S"))
        return ok({"indices": indices, "breadth": breadth,
                   "volume_vs_yesterday": {"pct": volume_pct}}, stale=spot_stale or idx_stale)

    @app.route("/api/sectors")
    def api_sectors():
        type_key = request.args.get("type", "industry")
        if type_key not in SECTOR_TYPES:
            return err("BAD_PARAM", "type 必须为 industry", 400)
        try:
            top = int(request.args.get("top", "60"))
        except ValueError:
            return err("BAD_PARAM", "top 必须为整数", 400)
        top = max(1, min(200, top))
        search = (request.args.get("search") or "").strip()
        try:
            summary, stale = ds.get_sector_summary(type_key)
            spot, spot_stale = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        today = _today()
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0

        # 计算当日 rank 并持久化(仅交易日);change_pct 可能为 None → _sort_key 排序
        if an.is_trading_time(now):
            ranked = summary.iloc[summary["change_pct"].map(_sort_key).sort_values(ascending=False).index]
            for i, r in ranked.iterrows():
                store.upsert_sector_daily(
                    db_path, today, type_key, r["code"], r["name"], _num(r["change_pct"]),
                    None, _num(r["turnover"]), i + 1)

        rows = []
        for _, r in summary.iterrows():
            code = str(r["code"])
            chg = _num(r["change_pct"])
            turn = _num(r["turnover"])
            # 降级口径(无成分股聚合):上涨家数占比取摘要上涨/(上涨+下跌);涨停占比无成分股数据恒 None
            # (情绪子项走 _weighted 归一化);领涨强度取摘要领涨股-涨跌幅
            up_count = _num(r["up_count"])
            down_count = _num(r["down_count"])
            up_ratio = None
            if up_count is not None and down_count is not None and (up_count + down_count) > 0:
                up_ratio = up_count / (up_count + down_count)
            limit_ratio = None
            leader_change = _num(r["leader_change_pct"])
            data_complete = True
            turnover_ratio = None
            if an.is_after_close(now) and turn is not None:
                avg5 = store.get_sector_turnover_avg(db_path, type_key, code, today, 5)
                if avg5 and avg5 > 0:
                    turnover_ratio = turn / float(avg5)
            prev_change = store.get_sector_prev_change(db_path, type_key, code, today)
            change_3d = store.get_sector_change_3d(db_path, type_key, code, today)
            activity = (turn / market_turnover) if (turn is not None and market_turnover) else None
            consecutive = store.get_consecutive_days(db_path, type_key, code, today, 20)

            emotion = an.sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change)
            strength = an.sector_strength(chg, consecutive, activity)
            risk = an.sector_risk(chg, change_3d, turnover_ratio, prev_change, up_ratio)
            composite = an.composite_score(strength, emotion, risk)
            verdict = an.sector_verdict(emotion, strength, risk, consecutive, data_complete)
            rows.append({
                "code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                "index_change_pct": chg,
                "emotion_score": emotion, "strength_score": strength, "risk_score": risk,
                "composite_score": composite, "verdict": verdict,
                "consecutive_days": consecutive, "data_complete": data_complete,
            })

        if search:
            kw = search.lower()
            matched = [x for x in rows if kw in str(x["name"]).lower()]
            total = len(matched)
            rows = matched
        else:
            total = len(rows)
            rows = sorted(rows, key=lambda x: (x["composite_score"] is None, -(x["composite_score"] or 0)))
        return ok({"type": type_key, "total": total, "sectors": rows[:top]}, stale=stale or spot_stale)

    @app.route("/api/sector")
    def api_sector():
        parsed = _parse_sector_code(request.args.get("code"))
        if parsed is None:
            return err("BAD_PARAM", "code 必须形如 industry:885887", 400)
        type_key, code = parsed
        try:
            summary, _ = ds.get_sector_summary(type_key)
            hist, _ = ds.get_sector_index_history(code, type_key)
            spot, _ = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        row = summary[summary["code"] == code]
        if row.empty:
            return err("BAD_PARAM", "未找到该板块", 400)
        r = row.iloc[0]
        chg = _num(r["change_pct"])
        turn = _num(r["turnover"])
        up_count = _num(r["up_count"])
        down_count = _num(r["down_count"])
        up_ratio = None
        if up_count is not None and down_count is not None and (up_count + down_count) > 0:
            up_ratio = up_count / (up_count + down_count)
        limit_ratio = None
        leader_change = _num(r["leader_change_pct"])
        now = datetime.now()
        today = _today()
        turnover_ratio = None
        if an.is_after_close(now) and turn is not None:
            avg5 = store.get_sector_turnover_avg(app.config["DB"], type_key, code, today, 5)
            if avg5 and avg5 > 0:
                turnover_ratio = turn / float(avg5)
        prev_change = store.get_sector_prev_change(app.config["DB"], type_key, code, today)
        change_3d = store.get_sector_change_3d(app.config["DB"], type_key, code, today)
        consecutive = store.get_consecutive_days(app.config["DB"], type_key, code, today, 20)
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0
        activity = (turn / market_turnover) if (turn is not None and market_turnover) else None
        emotion = an.sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change)
        strength = an.sector_strength(chg, consecutive, activity)
        risk = an.sector_risk(chg, change_3d, turnover_ratio, prev_change, up_ratio)
        composite = an.composite_score(strength, emotion, risk)
        verdict = an.sector_verdict(emotion, strength, risk, consecutive, True)
        hist_rows = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                      "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                     for x in hist.to_dict("records")]
        return ok({"code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                   "scores": {"emotion": emotion, "strength": strength, "risk": risk,
                              "composite": composite},
                   "verdict": verdict, "index_history": hist_rows})

    @app.route("/api/stock")
    def api_stock():
        raw = request.args.get("code")
        code6 = _parse_stock_code(raw)
        if code6 is None:
            return err("BAD_PARAM", "code 必须为 6 位股票代码", 400)
        try:
            quote, _ = ds.get_stock_quote(code6)
            daily, _ = ds.get_stock_daily(code6)
            minute, _ = ds.get_stock_minute(code6)
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        scores = an.score_stock(daily, quote, now)
        kline = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                  "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                 for x in daily.tail(250).to_dict("records")]
        intraday = [{"time": str(x["time"]), "price": float(x["price"]),
                     "avg": float(x["avg"]), "volume": float(x["volume"])}
                    for x in minute.to_dict("records")]
        return ok({"code": ds.with_prefix(code6), "name": quote["name"], "quote": quote,
                   "scores": scores, "verdict": scores["verdict"], "kline": kline,
                   "intraday": intraday})


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB"] = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB"])), exist_ok=True)
    store.init_db(app.config["DB"])
    register_routes(app)
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000)
```

注意:summary 经 `.where(pd.notna(out), None)` 后 `change_pct/turnover/up_count/down_count/leader_change_pct` 可能为 None,循环内一律用 `_num()` 转换,排序用 `_sort_key`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /c/stock && python -m pytest tests/test_api.py -v`
Expected: PASS。`semi["data_complete"]` 恒为 True(降级口径:无成分股聚合,不再依赖内存映射)。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add app.py tests/test_api.py && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: API 层 app.py(契约路由/错误处理)"
```

---

### Task 9: 前端 index.html + app.js + style.css

**Files:**
- Create: `templates/index.html`
- Create: `static/app.js`
- Create: `static/style.css`

**Interfaces:**
- Consumes: `create_app` 的 `GET /api/*`(Task 8);`static/echarts.min.js`(Task 2 已下载);`GET /` 渲染 index.html。
- Produces: 三层看板 UI,满足规格 §11 布局与交互。

**口径(规格 §11):** 顶栏(标题/更新时间/手动刷新/自动刷新开关);大盘总览条;板块榜单(主区)+右侧详情区;自选股条;交互 1-7;无障碍配色(emoji+文字,颜色仅辅助);图表(板块K线蜡烛+MA、个股K线蜡烛+MA+成交量副图、分时价格+均价+量)。

- [ ] **Step 1: 写 index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股三层分析看板</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/echarts.min.js"></script>
</head>
<body>
<header id="topbar">
  <div class="brand">📈 A股三层分析看板</div>
  <div class="top-actions">
    <span id="updated" class="muted">—</span>
    <button id="btn-refresh" title="手动刷新">🔄 刷新</button>
    <label class="switch"><input type="checkbox" id="auto-refresh"><span>自动刷新(60s)</span></label>
    <span id="stale-flag" class="hidden">⚠️ 数据可能过期</span>
  </div>
</header>

<section id="market-bar" class="panel">
  <div id="indices" class="indices"></div>
  <div id="breadth" class="breadth"></div>
</section>

<main id="layout">
  <section id="left" class="panel">
    <div class="tabs">
      <button class="tab active" data-type="industry">行业板块</button>
    </div>
    <div class="sector-tools">
      <input id="sector-search" placeholder="板块搜索,如 半导">
      <button id="btn-sector-search">搜索</button>
    </div>
    <table id="sector-table">
      <thead><tr><th>板块</th><th>涨幅%</th><th>情绪</th><th>强度</th><th>风险</th><th>综合</th><th>结论</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>

  <section id="right" class="panel">
    <div class="stock-tools">
      <input id="stock-search" placeholder="股票代码,如 600519 或 sh600519">
      <button id="btn-stock">查询</button>
    </div>
    <div id="detail">
      <div id="detail-title" class="muted">点击左侧板块行,或输入股票代码查看个股</div>
      <div id="chart-sector" class="chart hidden"></div>
      <div id="sector-scores" class="score-cards hidden"></div>
      <div id="chart-stock" class="chart hidden"></div>
      <div id="chart-intraday" class="chart hidden"></div>
      <div id="stock-scores" class="score-cards hidden"></div>
    </div>
  </section>
</main>

<footer id="watchlist">
  <span class="wl-label">⭐ 自选:</span>
  <span id="wl-items"></span>
  <button id="btn-wl-add" class="hidden">加入自选</button>
</footer>

<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 写 style.css**

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg:#f5f6f8; --panel:#fff; --line:#e3e6ea; --text:#222; --muted:#8a919c; --up:#e53935; --down:#1e9e4a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#17191d; --panel:#202329; --line:#33363d; --text:#e6e8ec; --muted:#9aa1ac; --up:#ff6b62; --down:#4cce78; }
}
body { font: 14px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text); padding: 12px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
#topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.brand { font-size: 18px; font-weight: 700; }
.top-actions { display: flex; gap: 12px; align-items: center; }
.muted { color: var(--muted); }
.hidden { display: none !important; }
.up { color: var(--up); } .down { color: var(--down); }
#indices, #breadth { display: flex; gap: 20px; flex-wrap: wrap; }
#layout { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) { #layout { grid-template-columns: 1fr; } }
.tabs { display: flex; gap: 8px; margin-bottom: 8px; }
.tab { padding: 6px 12px; border: 1px solid var(--line); background: var(--panel); border-radius: 6px; cursor: pointer; }
.tab.active { background: var(--text); color: var(--bg); }
.sector-tools, .stock-tools { display: flex; gap: 8px; margin-bottom: 8px; }
input { padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--text); }
button { padding: 6px 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--text); cursor: pointer; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; }
tr.sector-row { cursor: pointer; }
tr.sector-row:hover { background: var(--bg); }
.chart { width: 100%; height: 320px; margin-bottom: 12px; }
.score-cards { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.card { border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; min-width: 96px; }
.card .label { color: var(--muted); font-size: 12px; }
.card .value { font-size: 20px; font-weight: 700; }
.verdict { font-weight: 700; font-size: 15px; }
#watchlist { display: flex; gap: 10px; align-items: center; }
.wl-item { cursor: pointer; padding: 2px 8px; border: 1px solid var(--line); border-radius: 20px; }
#comp-list table td { font-size: 13px; }
```

- [ ] **Step 3: 写 app.js(核心逻辑,交互 1-7)**

```javascript
const state = {
  type: "industry",
  market: null,
  sectors: [],
  current: null, // {kind:'sector'|'stock', code}
  autoTimer: null,
};

const $ = (s) => document.querySelector(s);

function api(path) {
  return fetch(path).then((r) => r.json()).then((b) => {
    if (!b.ok) throw new Error((b.error && b.error.message) || "请求失败");
    return b;
  });
}

function fmtPct(v) { return v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(2) + "%"; }

function renderIndices(indices) {
  $("#indices").innerHTML = indices.map((i) =>
    `<span>${i.name} <b class="${i.change_pct >= 0 ? "up" : "down"}">${fmtPct(i.change_pct)}</b>` +
    ` <span class="muted">${i.price.toFixed(2)}</span></span>`).join("");
}

function renderBreadth(b, vol) {
  const s = (v) => v ?? "—";
  $("#breadth").innerHTML =
    `<span>上涨 <b class="up">${b.up}</b> / 下跌 <b class="down">${b.down}</b> / 平 ${b.flat}</span>` +
    `<span>涨停 ${b.limit_up} / 跌停 ${b.limit_down}</span>` +
    `<span>总成交额 ${(b.total_turnover / 1e8).toFixed(0)}亿</span>` +
    `<span>量能较昨日 ${vol && vol.pct !== null && vol.pct !== undefined ? fmtPct(vol.pct) : "收盘后对比"}</span>`;
}

async function loadMarket() {
  const b = await api("/api/market");
  state.market = b.data;
  renderIndices(b.data.indices);
  renderBreadth(b.data.breadth, b.data.volume_vs_yesterday);
  $("#updated").textContent = "更新于 " + (b.meta.updated_at || "—");
  $("#stale-flag").classList.toggle("hidden", !b.meta.stale);
}

async function loadSectors() {
  const b = await api(`/api/sectors?type=${state.type}&top=60`);
  state.sectors = b.data.sectors;
  const tbody = $("#sector-table tbody");
  tbody.innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${s.code}">` +
    `<td>${s.name}</td><td class="${s.index_change_pct >= 0 ? "up" : "down"}">${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${s.verdict}</td></tr>`).join("");
  tbody.querySelectorAll("tr.sector-row").forEach((tr) =>
    tr.addEventListener("click", () => openSector(tr.dataset.code)));
}

function scoreCell(v) { return v === null ? "…" : v.toFixed(0); }

async function openSector(code) {
  state.current = { kind: "sector", code };
  const b = await api("/api/sector?code=" + encodeURIComponent(code));
  $("#detail-title").classList.add("hidden");
  $("#chart-sector").classList.remove("hidden");
  $("#sector-scores").classList.remove("hidden");
  $("#chart-stock").classList.add("hidden");
  $("#chart-intraday").classList.add("hidden");
  $("#stock-scores").classList.add("hidden");
  renderSectorCharts(b.data);
  renderSectorScores(b.data);
}

function renderSectorScores(d) {
  const sc = d.scores;
  $("#sector-scores").innerHTML =
    `<div class="card"><div class="label">板块</div><div class="value">${d.name}</div></div>` +
    `<div class="card"><div class="label">综合分</div><div class="value">${sc.composite === null ? "…" : sc.composite.toFixed(2)}</div></div>` +
    `<div class="card"><div class="label">情绪</div><div class="value">${sc.emotion === null ? "…" : sc.emotion.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">强度</div><div class="value">${sc.strength.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">风险</div><div class="value">${sc.risk.toFixed(0)}</div></div>` +
    `<div class="card"><div class="verdict">${d.verdict}</div></div>`;
}

async function openStock(code) {
  state.current = { kind: "stock", code };
  const b = await api("/api/stock?code=" + encodeURIComponent(code));
  $("#detail-title").classList.add("hidden");
  $("#chart-sector").classList.add("hidden");
  $("#sector-scores").classList.add("hidden");
  $("#chart-stock").classList.remove("hidden");
  $("#chart-intraday").classList.remove("hidden");
  $("#stock-scores").classList.remove("hidden");
  renderStockCharts(b.data);
  renderStockScores(b.data);
  $("#btn-wl-add").classList.remove("hidden");
}

function renderStockScores(d) {
  const sc = d.scores;
  $("#stock-scores").innerHTML =
    `<div class="card"><div class="label">${d.name}</div><div class="value">${d.quote.price}</div><div class="muted">${fmtPct(d.quote.change_pct)}</div></div>` +
    `<div class="card"><div class="label">综合分</div><div class="value">${sc.composite.toFixed(2)}</div></div>` +
    `<div class="card"><div class="label">趋势</div><div class="value">${sc.trend.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">量价</div><div class="value">${sc.volume_price.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">信号</div><div class="value">${sc.signal.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">风险</div><div class="value">${sc.risk.toFixed(0)}</div></div>` +
    `<div class="card"><div class="verdict">${d.verdict}</div></div>`;
}

// ---- ECharts 图表 ----
function klineOption(d) {
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["K线", "MA5", "MA10", "MA20"] },
    grid: [{ left: 50, right: 20, top: 30, height: "55%" }, { left: 50, right: 20, top: "72%", height: "20%" }],
    xAxis: [{ type: "category", data: d.kline.map((k) => k.date), boundaryGap: true },
            { type: "category", gridIndex: 1, data: d.kline.map((k) => k.date) }],
    yAxis: [{ scale: true }, { gridIndex: 1, scale: true }],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
    series: [
      { name: "K线", type: "candlestick", data: d.kline.map((k) => [k.open, k.close, k.low, k.high]) },
      { name: "MA5", type: "line", data: ma(d.kline, 5), smooth: true, showSymbol: false },
      { name: "MA10", type: "line", data: ma(d.kline, 10), smooth: true, showSymbol: false },
      { name: "MA20", type: "line", data: ma(d.kline, 20), smooth: true, showSymbol: false },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: d.kline.map((k) => k.volume) },
    ],
  };
}
function ma(kline, n) {
  return kline.map((_, i) => {
    if (i < n - 1) return null;
    let s = 0; for (let j = i - n + 1; j <= i; j++) s += kline[j].close;
    return +(s / n).toFixed(2);
  });
}
function intradayOption(d) {
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["价格", "均价"] },
    grid: [{ left: 50, right: 20, top: 30, height: "55%" }, { left: 50, right: 20, top: "72%", height: "20%" }],
    xAxis: [{ type: "category", data: d.intraday.map((x) => x.time) },
            { type: "category", gridIndex: 1, data: d.intraday.map((x) => x.time) }],
    yAxis: [{ scale: true }, { gridIndex: 1, scale: true }],
    series: [
      { name: "价格", type: "line", data: d.intraday.map((x) => x.price), showSymbol: false },
      { name: "均价", type: "line", data: d.intraday.map((x) => x.avg), showSymbol: false, lineStyle: { type: "dashed" } },
      { name: "分时量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: d.intraday.map((x) => x.volume) },
    ],
  };
}
function makeChart(el, option) {
  const old = echarts.getInstanceByDom(el);
  if (old) old.dispose();   // 重渲染前销毁旧实例,避免“已有实例”报错
  const chart = echarts.init(el);
  chart.setOption(option);
  return chart;
}
function renderSectorCharts(d) {
  const el = $("#chart-sector");
  el.innerHTML = "";   // 板块名由 score-cards 的“板块”卡展示
  if (d.index_history.length < 2) { el.innerHTML = "<p class='muted'>历史数据不足</p>"; return; }
  makeChart(el, klineOption({ kline: d.index_history }));
}
function renderStockCharts(d) {
  const el1 = $("#chart-stock");
  el1.innerHTML = "<b>" + d.name + "</b> 日K线";   // 先写 DOM 再初始化图表
  const el2 = $("#chart-intraday");
  el2.innerHTML = "<b>分时图</b> 价格/均价/成交量";
  if (d.kline.length >= 2) makeChart(el1, klineOption(d));
  if (d.intraday.length >= 2) makeChart(el2, intradayOption(d));
}

// ---- 自选股 ----
function getWatchlist() {
  try { return JSON.parse(localStorage.getItem("stock_wl") || "[]"); } catch (e) { return []; }
}
function saveWatchlist(w) { localStorage.setItem("stock_wl", JSON.stringify(w)); }
function renderWatchlist() {
  const w = getWatchlist();
  $("#wl-items").innerHTML = w.map((x) =>
    `<span class="wl-item" data-code="${x.code}">⭐ ${x.name}(${x.code})</span>`).join("");
  document.querySelectorAll(".wl-item").forEach((el) =>
    el.addEventListener("click", () => openStock(el.dataset.code)));
}
function addWatchlist(name, code) {
  const w = getWatchlist();
  if (!w.some((x) => x.code === code)) w.push({ name, code });
  saveWatchlist(w);
  renderWatchlist();
}
$("#btn-wl-add").addEventListener("click", async () => {
  if (state.current && state.current.kind === "stock") {
    const b = await api("/api/stock?code=" + state.current.code);
    addWatchlist(b.data.name, b.data.code);
  }
});

// ---- 刷新 ----
async function refreshAll() {
  try { await loadMarket(); } catch (e) { $("#stale-flag").classList.remove("hidden"); }
  try { await loadSectors(); } catch (e) { /* 沿用旧列表 */ }
  if (state.current) {
    try {
      if (state.current.kind === "sector") await openSector(state.current.code);
      else await openStock(state.current.code);
    } catch (e) { /* 保留当前 */ }
  }
}
$("#btn-refresh").addEventListener("click", refreshAll);
$("#auto-refresh").addEventListener("change", (e) => {
  if (e.target.checked) {
    state.autoTimer = setInterval(refreshAll, 60000);
  } else if (state.autoTimer) {
    clearInterval(state.autoTimer);
    state.autoTimer = null;
  }
});

// ---- 交互绑定 ----
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    state.type = t.dataset.type;
    loadSectors();
  }));
$("#btn-sector-search").addEventListener("click", async () => {
  const kw = $("#sector-search").value.trim();
  const b = await api(`/api/sectors?type=${state.type}&top=60&search=${encodeURIComponent(kw)}`);
  $("#sector-table tbody").innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${s.code}"><td>${s.name}</td><td>${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${s.verdict}</td></tr>`).join("");
  document.querySelectorAll("#sector-table tr.sector-row").forEach((tr) =>
    tr.addEventListener("click", () => openSector(tr.dataset.code)));
});
$("#btn-stock").addEventListener("click", () => {
  const code = $("#stock-search").value.trim();
  if (code) openStock(code);
});
$("#stock-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { const c = $("#stock-search").value.trim(); if (c) openStock(c); }
});

// ---- 启动 ----
renderWatchlist();
refreshAll();
```

- [ ] **Step 4: 手动验收(规格 §12 前端手动验收项)**

Run: `cd /c/stock && python app.py`(后台起服务)→ 浏览器打开 `http://127.0.0.1:8000`
Expected:
1. 顶栏 + 大盘总览条(三大指数/涨跌平/涨停跌停/总成交额)显示。
2. 板块榜单:行业板块、按综合分排序、行内情绪/强度/风险/结论。
3. 点板块行 → 右侧板块 K 线 + 三围分 + 综合分 + 结论。
4. 搜股票代码 → 右侧个股日K+MA+成交量、分时图、量价评分卡。
5. 自选:加入自选 → localStorage 持久化,点击快速打开。
6. 手动刷新、自动刷新开关。
7. 数据源故障时右上角"⚠️ 数据可能过期"(可临时停网验证或信任单测覆盖)。
> 验收时若 THS 板块成交额缺失导致情绪/强度显示 "…"(归一化降级),属预期行为。

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add templates/index.html static/app.js static/style.css && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "feat: 前端三层看板(index.html/app.js/style.css)"
```

---

### Task 10: 端到端验收 + README 补全

**Files:**
- Create: `README.md`
- Modify: 无(验收为主)

**Interfaces:**
- Consumes: 全部 Task 1-9 产物。
- Produces: `README.md`(安装/启动/使用说明);一份端到端验收记录。

- [ ] **Step 1: 跑全量单测**

Run: `cd /c/stock && python -m pytest tests/ -v`
Expected: 全部 PASS(store/data_source/analysis_time/analysis_sector/analysis_stock/api 六套)。

- [ ] **Step 2: 全流程冒烟(真网络)**

Run(顺序执行):
```
cd /c/stock
python -m pytest tests/test_data_source.py::test_market_spot_normalized -q   # 确认 mock 正常
python tools/verify_sources.py 2>&1 | tail -30                                # 真网络确认字段未变
python app.py &                                                                # 后台起服务
sleep 25
curl -s "http://127.0.0.1:8000/api/market" | head -c 400
curl -s "http://127.0.0.1:8000/api/sectors?type=industry&top=5" | head -c 400
curl -s "http://127.0.0.1:8000/api/stock?code=600519" | head -c 400
curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8000/api/sector?code=bad:1"
curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8000/"                  # 首页 200
```
Expected: `/api/market` 返回 `ok:true` 且 breadth 有数;`/api/sectors` 返回 top5;`/api/stock` 返回 kline/intraday;`bad:1` → 400;首页 200。**首次冒烟冷启动可能 >30s(新浪全市场快照 ~10s + THS),耐心等**,若 `SOURCE_FAIL` 复测一次。

- [ ] **Step 3: 性能抽检(规格 §13)**

Run: 连续两次 `curl -s -o /dev/null -w "%{time_total}s\n" "http://127.0.0.1:8000/api/sectors?type=industry&top=60"`(第二次为缓存命中)
Expected: 冷启动(首次)<30s(受快照主导);缓存命中后 <3s。全量板块打分(90 个行业板块)<2s。

- [ ] **Step 4: 写 README.md**

```markdown
# A股三层分析看板

本地运行的 A 股分析工具:大盘总览 → 板块强弱(含板块级情绪)→ 个股量价体检,规则打分直接给出"适合走什么方向、什么方向有风险"。

## 安装

```bash
cd C:\stock
python install.py     # 装依赖 + 下载 ECharts 5.5.1
```

## 启动

```bash
python app.py         # 打开 http://127.0.0.1:8000
```

## 功能

- **大盘总览**:三大指数、涨/跌/平家数、涨停/跌停、总成交额、量能较昨日(收盘后有效)。
- **板块强弱**:行业板块榜(情绪/强度/风险/综合分与结论:建议关注/跟踪/观望/谨慎追高/回避/一日游)。
- **个股量价**:日K+MA+成交量、分时(价格+均价+量)、趋势/量价/信号/风险分与结论。
- **盘中可用**:个股量比按全日折算,盘中即可比;板块"成交额放量/放量滞涨"仅收盘后计算,盘中自动降级。
- **刷新**:手动刷新 + 可选 60s 自动刷新;数据源故障时有过期提示并沿用旧数据。

## 数据源

新浪(Sina)/腾讯(Tencent)/同花顺(THS);东方财富、网易接口在部分网络被屏蔽,本项目不使用。

## 技术说明

- 数据层 `data_source.py`:TTL 缓存(200 条上限,失败退避 30s×2^n 封顶 10min)。
- 持久化 `store.py`:每日快照(盘中覆盖)、板块成交额近5日均值。
- 打分 `analysis.py`:口径见 `docs/superpowers/specs/2026-08-11-a-share-dashboard-design.md`(Rev.5)。
- 测试:`python -m pytest tests/ -v`(mock 数据源,离线可跑)。
```

- [ ] **Step 5: 提交**

```bash
cd /c/stock && git add README.md && git -c user.name="stock-tool" -c user.email="stock-tool@local" commit -m "docs: README 使用说明与端到端验收"
```

---

## 自审清单(对照规格 Rev.5)

| 规格要求 | 落地任务 |
|---|---|
| §2 数据源约束(避开东财/网易) | Task 1 验证、Task 4 实现只用新浪/腾讯/THS |
| §2 风险项:同花顺成分股字段验证 | Task 1(结论:无成分股接口 → 板块评分降级为摘要口径) |
| §4 数据层 9 个函数 + TTL | Task 4(含返回 `(data, stale)` 契约) |
| §4 代码表示 `type:code` / 个股双写法 | Task 4 `normalize_code/with_prefix`、Task 8 `_parse_sector_code/_parse_stock_code`、Task 9 搜索框 |
| §5.1 每日快照 upsert/幂等/盘中覆盖 | Task 3、Task 8(仅 `is_trading_time` 写库) |
| §5.1 盘中累计 vs EOD 原则(收盘后对比) | Task 8 `volume_vs_yesterday` 与 `turnover_ratio` 仅 `is_after_close`;测试 Task 6 `test_risk_voluptake_only_after_close` |
| §6.1 大盘总览无情绪结论 | Task 8 `/api/market` 只给 breadth |
| §6.2 板块三围分 + 阈值 + 结论优先级 1-6 + 综合分 | Task 6(含 C5' 谨慎追高、M2 单/双风险信号) |
| §6.3 个股四维分 + 合成 + 风险≥70 重大 | Task 7 |
| §7 自定义量比全日折算 A/B(午休/15min 下限) | Task 5 `trading_minutes_elapsed/day_adjusted_volume`,测试 `test_trading_minutes_elapsed`(14:00=180) |
| §7 涨停近似/分母=活跃成分股/新股排除 | Task 5 `limit_threshold/compute_breadth`,Task 4 `get_new_stocks`;测试 `test_compute_breadth` |
| §8 API 契约 + 示例算术 71.35/65.75 | Task 6 `test_composite_arithmetic`、Task 7 `test_stock_composite_and_verdict`、Task 8 test_api |
| §8 400/500 三态 + search total 语义 | Task 8 `test_sector_bad_param/test_source_fail_returns_500/test_sectors_search_and_composite` |
| §9 缓存(200 上限/锁/失败退避) | Task 4 `TTLCache` |
| §10 错误处理(15s 超时/重试 1 次/stale 回退) | Task 4 `_fetch_with_retry/_cached`、Task 8 错误契约 |
| §11 前端布局与交互 1-7 | Task 9(含自选 localStorage、自动刷新) |
| §12 测试矩阵(量比折算/持久化/API/性能) | Task 3-8 测试 + Task 10 验收 |
| §13 验收性能(冷启动<30s/缓存<3s/板块打分<5s) | Task 10 Step 3 |
| §14 L1/L2(install.py 命名、版本 pin) | Task 2 |
