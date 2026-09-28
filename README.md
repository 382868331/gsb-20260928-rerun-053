# 投资风险计算平台

这是一个仅使用 Python 标准库的场景化组合重估值示例。持仓、定价模型和市场场景彼此独立；每次估值必须传入同一个不可变 `ScenarioSnapshot`，价格和汇率都从该版本读取。

## 分层

- `data/holdings.json`：少量虚构持仓。
- `data/scenarios/*.json`：基准、压力、更新版本和缺数据场景。
- `risk_platform/models.py`：持仓、场景、估值和盈亏贡献的数据结构。
- `risk_platform/pricing.py`：按资产类型注册定价模型。
- `risk_platform/engine.py`：逐持仓估值、压力盈亏和场景哈希。
- `risk_platform/storage.py`：先写临时文件再原子替换，失败时不发布总数。
- `risk_platform/app.py`：批量运行基准和压力场景。

## 运行

    python -m risk_platform.app --data-dir data --output-dir output
    python -m unittest discover -s tests -v

估值结果保存在 `output/results/valuations/`，压力盈亏保存在 `output/results/stress/`。未来扩展 VaR、希腊字母或敞口指标时，直接读取已发布的估值 JSON 或对应的 `ValuationResult`，不要重新混用场景数据。

## 数据完整性规则

- 同一项持仓的价格与 FX 必须来自同一个场景版本。
- 场景缺价格或缺汇率会直接失败，不会沿用基准值。
- 只有所有持仓估值完成后才计算总数并发布。
- 每个结果记录 `scenario_id`、`scenario_version`、`as_of` 和输入场景 SHA-256；场景更新后，旧结果仍能按版本和哈希追溯。
- `StressResult.contributions` 可逐项拆回持仓贡献，贡献之和等于总盈亏。
