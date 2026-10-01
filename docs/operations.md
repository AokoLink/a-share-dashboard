# 运行、重放与备份维护

## 环境与启动

目前验证环境为 Windows、Python 3.13.13，主要运行库为 pandas 3.0.5、numpy 2.5.2、akshare 1.18.84、Flask 3.1.0、requests 2.33.1。`requirements.txt` 声明直接依赖；`requirements.lock.txt` 保存本机验证环境的 41 项直接与传递依赖，用于同平台重建。没有自动安装、升级或联网下载依赖。

```powershell
python -m pip install -r requirements.lock.txt
python -m pytest -q
python app.py
```

跨平台或 Python 版本变化需另行验证。源代码更新后重启应用与每日任务，不能把磁盘上的新源文件当成旧进程已经加载的代码。

## 日常运行与恢复

收盘后执行 `python -m pipeline.run_daily`，流程为日历、收盘门槛、行情、资格范围、双价格、质量、环境与板块、冻结、逐股跟踪、账户核算。单实例锁防止两个每日任务同时写入。此实现不创建定时任务。

- “无候选”是正常计算结果；“来源失败”“部分覆盖”“等待收盘”有各自状态。
- 同交易日、相同代码、输入清单、账户预算和运行模式，执行 `python -m pipeline.run_daily --resume <运行编号>`。成功且哈希完整的采集可复用，失败步骤重试；冻结重放包的代码和输入也检查完整性。
- 日期或代码／预算改变时开始新运行；不要强行重用旧运行编号。当前步骤 F 已更新代码，因此步骤 B 的旧运行编号不能直接续跑。
- 修改数据源字段后，先查响应和规范化价格档案，不以缺日推断停牌，不手改冻结记录或把旧报告当新数据。
- `_analysis/daily_pipeline/health.json` 保留逐代码、逐口径来源、耗时、错误及最后成功日期；`/api/pipeline-health?failed=1&page=1&limit=30` 分页读取。旧 B 运行没有耗时数据时显示未知。
- 监控页的版本评审摘要由每日后台生成。如果数据库水位变化而摘要尚未更新，接口标为 `stale`，不会在页面请求中重扫全部历史并重新评价。

P0 增加首次历史补齐、跨运行双价格缓存、每日重叠窗口更新、复权变化全量重建、传输重试与失败补采清单。日常命令无需改变；需要重查整段历史时执行 `python -m pipeline.run_daily --refresh-prices`。进度、恢复边界及来源限制见 [P0 实施记录](p0-data-stability.md)。

## 候选与验证查询

主流程为市场环境、板块方向、策略候选、组合风险、验证记录。候选页只读后台结果；账户和验证记录同样不采集整池日线、不新增冻结。旧推荐、可介入龙头、波段等入口保留为单独研究工具，调用习惯与主流程不同。

`GET /api/strategy-monitor?date=2026-09-30&strategy=breakout&version=<版本>&limit=30` 返回本页快照及每个快照的最近观察，最多 100 条；用 `next_cursor` 查询更早快照。版本评审摘要覆盖该版本全部已保存历史，不等于本页样本统计。旧观察仍在数据库，详细快照继续通过 `/api/strategy-snapshot/<编号>` 查询。页面没有摘要时应等待后台更新，不表示数据库没有冻结记录。

## 归档重放

新后台冻结同时保存完整技术价格窗口、执行价格、资格输入、环境、关系快照、状态事件、当时的 core 与 pipeline 源文件，以及分层版本和依赖版本。源文件按原始字节保存，换行和前复权历史后来修订都不影响旧包。一个包可供同日三个策略共同引用。

```powershell
python -m pipeline.replay_signal --snapshot-id <编号> --out _analysis/replay-check.json
```

重放先校验包、输入、代码哈希与运行依赖，再在临时目录的独立 Python 进程加载归档源文件。网络连接禁止，冻结禁止，仅比较资格、池、排序、分数及候选。缺少当时包的旧记录返回 `legacy_missing_input_and_code_bundle`；运行库不匹配、内容篡改、候选差异分别记录。不从当前代码或当前前复权价格补造旧结果。

明确的留档审计包可用 `--bundle <引用JSON路径>` 重放。它不能被算作真实前向样本。恢复目录中的旧绝对引用通过 `--relocation <restore-map.json>` 解析，不改写不可变包和账本内容。

## 备份与恢复

```powershell
python -m pipeline.backup_workspace --db data/market.db --root _analysis/daily_pipeline
python -m pipeline.backup_workspace --restore <备份ZIP路径> --target <尚不存在的新目录>
```

备份持有每日任务锁，通过 SQLite 在线备份读取数据库，包括 WAL 中的已提交内容。保存每日留档、标准独立冻结归档、D 实验、E 手工验收、当前源文件和依赖声明；逐文件保存 SHA256，并登记数据库各表行数。输出目录必须在输入目录外。代码与资料后续变更不会改写已经创建的备份。

恢复拒绝覆盖现有目录，先检查路径、重复条目及所有内容哈希，再核查恢复数据库完整性与行数，并生成外部路径映射。现有生产数据库、快照和状态事件不会替换。恢复到正式运行位置应由维护者在停写后按核对的目录配置操作；本次只做新目录恢复验收。

默认备份不含整个 Git 历史、`_analysis/daily` 的全部旧 pkl，或自定义归档根目录之外的资料。D 的早期报告虽已保留，缺失的历史原始输入和当时代码不能因此变为完整可重放证据。若使用自定义数据目录，应将其纳入明确的备份范围，并验证引用解析。

## P1 数据与前向验收

原生板块清单可独立运行 `python -m pipeline.collect_sector_inventory`；上证参考基准为 `python -m pipeline.collect_benchmark`。假期清单只保存实际观察日关系，不回填前一交易日，也不冻结信号。

原生行业指数独立入口为 `python -m pipeline.collect_sector_indices --workers 6`，同花顺索引在 `sector_indices/latest.json`。申万同源行业成员与指数在板块清单及正常每日特征中采集，独立价格参考索引为 `sector_native_indices/latest.json`。`/api/sector-index-rotation?provider=sw` 或 `provider=ths` 检查报告与输入依赖哈希，只读排名；日历决定新鲜度，缺少日历覆盖标为未知。股票强度要求同源代码和同日关系，不按名称跨来源绑定。

`python -m pipeline.collect_sector_history` 保存申万官方分类历史原 XLS 和日期记录，索引为 `sector_classification_history/latest.json`。这是回顾性资料留档，尚不证明历史时点完整；来源计入／更新日期不替代首次公开时间，不回填历史信号。

`python -m pipeline.forward_validation --register` 登记当前规则、固定预算与交易日历；`python -m pipeline.forward_validation` 查看登记后真实样本。每日账户步骤会更新报告，页面只读 `/api/forward-validation`。代码身份改变后旧方案与报告保留，新方案需明确登记；未登记或隔离期内信号不进入独立样本。

`python -m pipeline.execution_evidence --import-file <证据包>` 校验、留档执行输入；不带导入参数时只审计覆盖。需要原文哈希、明确来源、知悉时间及实际接收时间；缺失或冲突不按已核实处理。当前严格执行输入为零。

2026-10-01 实施与验收见 [P1 记录](p1-evidence-and-validation.md)，机器验收索引为 `_analysis/daily_pipeline/p1_acceptance/latest.json`。当前验证起始日 2026-10-22、有效前向样本为零。没有创建新的定时任务。
