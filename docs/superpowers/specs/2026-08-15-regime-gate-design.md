# 市场情绪择时门控(regime gate)设计

## 目标

把已验证的逆向市场择时信号落成产品:在推荐输出里标注**当前市场情绪**,并给出择时建议(恐慌→关注超跌反弹 / 高潮·退潮→建议空仓 / 其余→中性)。**不改选股逻辑**(选股精准已证伪),只加择时标签。

## 背景(已验证结论,来自 regime_extended_probe)

- 恐慌抄底:41 个恐慌日,合法窗口 oo +1.26%/64% win、oc +1.95%/68% win(净 +0.86%/+1.55%)。唯一稳健正信号,且环境标签纯因果(收盘后可算)、T+1 开盘买入可执行。
- 高潮(冲顶):oo 37% win(追高 63% 概率亏);退潮(杀跌延续):38% win。→ **空仓/回避**。
- 牛/熊/震荡/恢复:44–49% win,硬币,无 edge。→ **中性**。
- 选股集中 D>A>TOP1 只减分、composite 无方向区分度(Spearman≈0.02)。→ **不碰选股**。
- 数据缺口真相:不是 pkl 缺数据,是 `build_universe.tail(1200)` 截断;`latest_state` 传 `tail_n=120` 读 pkl(合成指数 M 为复利,ma/r 比较对起始基重定不变,故末日常标签与完整历史一致、但快约 30 倍)。

## 范围

1. `environment.py`:新增 `regime_advice(label)` 纯函数(标签→建议),`latest_state(data_dir, sector_map_path)` 返回当日 regime 标签 + 指标 + 建议。
2. `app.py`:新增 `/api/regime` 端点;在 `/api/recommend` 响应注入 `regime` 字段(best-effort,pkl 不可用/过期则 label=None)。
3. `tests/test_environment.py` + `tests/test_api.py`:纯函数 + 合成小 universe + 端点测试。

## 非目标

- 不重拉数据(pkl 已完整,缺口是截断)。
- 不改选股、不改 composite 打分、不改 sector 逻辑。
- 不细分"好恐慌/坏恐慌"(41 样本细分 = 过拟合)。
- 不自动下单/不改仓位,只给建议标签。

## 关键接口

- `environment.regime_advice(label) -> {"action": "buy_dip"|"avoid"|"neutral"|"unknown", "message": str}`
  - 恐慌 → `buy_dip`;高潮/退潮 → `avoid`;牛/熊/震荡/恢复 → `neutral`;None/未知 → `unknown`。
- `environment.latest_state(data_dir, sector_map_path, tail_n=120, cache_path=None) -> {"as_of": "YYYY-MM-DD", "label": str|None, "metrics": {...}, "advice": {...}}`
  - `as_of` = 日历最后交易日;`label` = `classify` 最后一日结果(历史不足则 None)。
  - `tail_n=120`:仅需最近 60 日(MIN_HISTORY),重定不变保证末日常标签与完整历史一致。
  - `cache_path`:文件缓存(as_of 与 code 数不变则跳过 build_series 重算)。
  - `metrics` 只含展示/诊断字段(r5/r1/up_ratio/limit_up/limit_down/turnover_ratio),不含评分。

## 测试要点

- `regime_advice`:七态 + None 全覆盖映射。
- `latest_state`:用合成小 universe(>=60 根,构造出恐慌/牛等标签)验证 label 正确、as_of 正确;纯因果(标签只依赖 <= as_of)。
- app:`/api/regime` 在 pkl 存在时返回 label;`/api/recommend` 响应含 `regime` 字段(可 None)。
