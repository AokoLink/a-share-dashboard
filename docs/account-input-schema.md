# 账户执行与公司行为输入

步骤 E 的账本是资金与股数模拟，不连接券商。只有有来源和时点的证据可以标为核实；没有文件意味着未知。不能从缺失日线推断停牌，不能从涨跌幅阈值推断开盘可成交。

## 未复权日线

后台与独立账户命令共用 `_analysis/daily_pipeline/execution/raw_daily/<六位代码>.csv` 及同名 `.json` 元数据。必须具备 `date/open/close/amount`，元数据 `adjust=none`，SHA256 一致；日期不得重复或逆序。账户入口不回退到旧复权不明的 pkl。缺失价格会留下订单或估值异常。

开盘执行仅使用该日开盘、已冻结信号，以及上一交易日的流动性和板块归属；不使用当日最终成交额、收盘价或收盘关系来决定开盘下单。收盘才使用当日收盘估值和观察关系。

## 开盘成交状态

可补充 `_analysis/daily_pipeline/execution/trade_status.json`：

```json
{
  "rows": [{
    "date": "2026-09-02",
    "code": "600001",
    "verified": true,
    "source": "请替换为可核查来源及记录引用",
    "known_at": "2026-09-02T09:30:00+08:00",
    "halted": false,
    "buy_open_allowed": true,
    "sell_open_allowed": true,
    "max_buy_shares": 1000,
    "max_sell_shares": 1000
  }]
}
```

这是结构示例，不是真实证券的成交证明。`known_at` 是执行信息在该时点可用的时间，开盘检查不接受之后才可知的证据。可选数量上限必须为非负整数，按同代码、同方向、同日所有订单共享；不能每个策略各用一遍。只知道停牌或开盘不可成交时，可以明确置相应字段，其余未知字段不要填成 `true`。

输入没有盘口排队和真实券商成交回报，因此即使字段经过核实也仍是模拟。

## 公司行为与覆盖

可补充 `_analysis/daily_pipeline/execution/corporate_actions.json`：

```json
{
  "events": [{
    "id": "独立公告或核账事件编号",
    "code": "600001",
    "type": "cash_and_split",
    "ex_date": "2026-09-03",
    "pay_date": "2026-09-07",
    "share_factor": 2,
    "net_cash_per_share": 1,
    "verified": true,
    "source": "请替换为实际公告及核账引用",
    "known_at": "2026-09-01T08:00:00+08:00"
  }],
  "coverage": [{
    "date": "2026-09-03",
    "code": "600001",
    "complete": true,
    "verified": true,
    "source": "请替换为该证券该日公司行为完整性核查引用",
    "known_at": "2026-09-03T08:00:00+08:00"
  }]
}
```

示例仅用于解释模型：除权日以此前持仓股数计算应收净分红，再应用股数乘数。除权日前卖出的股数没有权利，当日开盘新买入的股数没有这次权利。应收款计入资产，付款后才成为可下单现金；非交易日付款在下一交易日模拟开盘入现金，这一时点假设需要独立核实。已有持仓退出后应收款仍保留。

`net_cash_per_share` 必须是已核对的每原股实际净权利金额，当前模型不自动计算依赖持有期的税务差异。`share_factor` 默认 1，处理后股数必须为整数。分股后的成本总额不变。暂不支持配股出资、复杂换股、零碎股折现、退市结算等事件；已知但无法处理的公司行为会持续阻断估值与虚构退出，原持仓保留。

没有 `events` 不证明没有公司行为。覆盖需逐日逐证券核查。没有完整覆盖时，`verified_inputs` 模式阻止新增成交；`research_proxy` 可做明确标记的代理核算，但不能作为核实的账户收益。

## 板块与预算证据

账户共用 C 批次带日期的关系查询，不回填当前映射。开盘使用前一交易日关系，收盘暴露使用当日关系。只核实到一条关系不证明行业／题材分类完整。

独立输入包可在 `relations[日期][代码]` 中提供 `status=ok`、相同 `as_of`、`relations=[{id,category}]` 和 `categories_complete=["industry","theme"]`，后者必须有完整分类核查依据。P1 查询仅在各来源全目录及成员枚举通过、且证券具有行业覆盖依据时提供该保证。本轮 330 个分类枚举通过，适用于研究范围 2,446 只证券；22 只仍未知，缺失日期和未知执行资料仍会阻塞严格预算。来源成员不替代可交易资格，父子映射未断言。历史分类文件尚不能充当有知悉时间的历史指数成员。研究代理模式报告未知覆盖，不把未知行业或题材记为已证实的零暴露。

## 独立研究输入包

`python -m pipeline.account_report --inputs <JSON路径> --config <预算路径> --as-of <截止日>` 接收：

```text
snapshots: [{id,signal_date,strategy,version,frozen_at,payload:{horizon_sessions,candidates,pool}}]
calendar: [YYYY-MM-DD, ...]                    # 唯一、有序
bars: {code:{date:{open,close,amount}}}
bases: {code:verified_unadjusted_raw 或明确未知状态}
trade_status: {date:{code:{执行状态证据}}}
corporate_actions: [公司行为事件]
corporate_coverage: {date:{code:{覆盖证据}}}
relations: {date:{code:{带日期关系}}}
benchmark: {date:上证综指收盘参考值}
sector_returns: {date:{行业id:当日收益小数}}
status_events: [{strategy,version,status,changed_at}]
execution_rules: {date:{code:{数量与费用规则}}}
```

输入包属于明确的研究重放。它不写数据库冻结记录，不覆盖网页真实前向账户的 `accounts/latest.json`。各输入的完整快照、哈希、账户预算、代码哈希及报告另存；用原输入包和原预算可重放。原报告不因后续数据修订而覆盖。

## P1 来源导入与数量费用规则

实际每日账户从 `execution/evidence/imports/` 读取带哈希的不可变导入包。原 `trade_status.json`、`corporate_actions.json` 的可变验证标志仅用于研究。`python -m pipeline.execution_evidence --import-file <文件>` 接收：

```text
trade_status: [{date,code,halted,buy_open_allowed,sell_open_allowed,可选容量,来源证据}]
corporate_coverage: [{date,code,complete,来源证据}]
corporate_actions: [{id,code,action_type,ex_date,pay_date,net_cash_per_share,share_factor,来源证据}]
execution_rules: [{date,code,buy_min,buy_step,sell_min,sell_step,odd_lot_sell_all,
                  commission_rate,minimum_commission,sell_tax_rate,transfer_rate,来源证据}]
来源证据: verified=true, source=明确来源, known_at=带时区知悉时间,
          evidence_refs=[{path:本地原文路径,sha256:原文字节哈希}]
```

`action_type` 支持 `cash_dividend`、`share_distribution`、`combined_distribution`；复杂事件不能标成已处理。数量为正整数，零股全部退出须明确布尔值；费率为非负小数，最低佣金为非负金额。`buy_min=200,buy_step=1` 表示最低 200 股、其后逐股递增，不能以全账户统一 100 股替代逐证券规则。

原文复制至内容寻址目录并保存实际 `received_at`。开盘时 `known_at` 与实际接收时间都不能晚于开盘；补入历史资料不会反证此前实际已知。重复语义保留最早接收时间；冲突输入拒绝发布，旧包保留。附件哈希不证明真实性，导入校验不替代来源核实。

缺规则、成交状态、公司行为覆盖或分类预算时，严格模式继续阻止建仓；研究代理模式报告未知。费用分项见 `fee_components` 和成交事件；已核实的净分红不再按卖出税率重复扣减。
