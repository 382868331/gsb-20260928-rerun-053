# 投资风险计算平台

把持仓、定价模型和风险场景拆成独立模块，对一组压力场景做逐持仓重新估值。

## 模块边界

- `risk_platform/holdings.py`：持仓定义与校验。
- `risk_platform/scenarios.py`：不可变市场场景、场景指纹和缺失数据检查。
- `risk_platform/pricing.py`：现金、股票、零息债券定价模型。
- `risk_platform/engine.py`：在同一场景内同时取价格、收益率和汇率。
- `risk_platform/attribution.py`：利率、汇率、价格同动时的精确变化归因。
- `risk_platform/results.py`：完整结果对象与原子发布仓库。
- `risk_platform/metrics.py`：基于同一份场景结果扩展风险指标，不重新定价。

每次估值记录 `scenario_id`、`version` 和场景内容的 SHA-256 `fingerprint`。任一场景缺价格、收益率或汇率，都会在生成总数前失败，不会偷偷沿用基准值。结果 JSON 先写临时文件，再原子替换；任务失败时不会发布不完整总数。

## 多因素归因

压力场景允许同时改变股票价格、债券收益率和汇率。基准到压力的盈亏不靠差额分摊，而是对三个因素的 2³ 个角点（如「只改价格」「价格+汇率」……）逐一重新估值，再用包含-排斥恒等式分解为：

- 单因素贡献：`factor_equity_price`、`factor_bond_yield`、`factor_fx`；
- 两因素交互：`interaction_equity_yield`、`interaction_equity_fx`、`interaction_yield_fx`；
- 三因素交互：`interaction_all`。

七个项在每个持仓和组合总计上都恒等于 `pnl_vs_base`，引擎在发布前会校验该恒等关系。角点场景只取自已校验通过的基准/压力切片，缺数据时整次运行失败、不发布任何文件。每个结果文件继续绑定当时的 `version` 与 `fingerprint`，同一持仓换用不同市场版本运行时，旧结果始终指向旧输入。

## 运行

```bash
python -m risk_platform.cli \
  --holdings data/holdings.json \
  --scenarios data/scenarios.json \
  --results-dir results \
  --run-id demo
```

旧 run 保存自己的场景版本和指纹。场景文件更新后，仍可用旧 run id 读取并追溯当时的输入，新 run 会获得新的版本和指纹。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包含少量虚构资产的逐项公式对照、持仓贡献求和、组合场景的手算单因素与交互项、归因恒等校验、缺数据失败、旧版本追溯、指标复用和 CLI 发布。
