# 预测引擎基线报告

- system_version: `62c60a7`
- module_version: 1.0.0
- generated_at: 2026-08-14T20:21:31.599087
- data_range: 2019-02-25 -> 2026-08-11
- train_window: 2021-08-25 -> 2025-08-08 (n_train=320)
- valid_window: 2025-08-13 -> 2026-08-06 (n_valid=80)
- n_eval=400, step=3

## 校准表(composite 分箱 → P(up))

| 校准器 | 箱上界 | P | degraded | 训练样本 n |
|---|---|---|---|---|
| direction | 29.6162 | 0.4608 | False | 232472 |
| direction | - | 0.4656 | False | 232472 |
| gap | 29.6162 | 0.3273 | False | 232472 |
| gap | 38.4106 | 0.3323 | False | 232472 |
| gap | 41.3654 | 0.3352 | False | 232472 |
| gap | - | 0.3373 | False | 232472 |
| od | 29.6162 | 0.4716 | False | 232472 |
| od | - | 0.4893 | False | 232472 |
| trend3 | 29.6162 | 0.4189 | False | 232472 |
| trend3 | 41.3654 | 0.4611 | False | 232472 |
| trend3 | - | 0.4621 | False | 232472 |

## 指标(valid 段,out-of-sample)

| 校准器 | n | base_rate | hit_rate | ece | brier | n_hold |
|---|---|---|---|---|---|---|
| direction | 85492 | 0.4529 | - | 0.0123 | - | 85492 |
| gap | 85492 | 0.3709 | 0.6291 | 0.0355 | 0.2347 | 0 |
| od | 85492 | 0.4664 | - | 0.0231 | - | 85492 |
| trend3 | 85491 | 0.4677 | 0.5553 | 0.0095 | 0.2476 | 78136 |
| path | 0 | - | acc_path=- | - | - | - |

## 诚实声明

1. 校准概率基于「代理管线」评分(日线 pkl 无 amount,生产板块 composite 不可复现)。
2. 回测模式指标仅在 valid 窗口有效(out-of-sample)。
3. 分钟级路径未做(数据缺口);path 为日线 OHLC 的 gap×od 四分类。
4. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数,ece/brier 不可按 n 直接推置信区间。
5. n_eval 为完整评估日 range,与基线 backtest(跳无热板块日)的 n_eval 可能不同,非同日口径。
