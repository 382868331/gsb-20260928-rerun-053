# 投资风险计算平台

把持仓、定价模型和风险场景拆成独立模块，对一组压力场景做逐持仓重新估值。

## 模块边界

- `risk_platform/holdings.py`：持仓定义与校验。
- `risk_platform/scenarios.py`：不可变市场场景、场景指纹和缺失数据检查。
- `risk_platform/pricing.py`：现金、股票、零息债券定价模型。
- `risk_platform/engine.py`：在同一场景内同时取价格、收益率和汇率。
- `risk_platform/attribution.py`：把基准到压力场景的盈亏精确分解为各驱动因素
  （价格、利率、汇率）的主效应与交互项。对每个持仓枚举其驱动因素的全部子集，
  用 inclusion-exclusion 求每个非空子集的效应；各效应之和严格等于该持仓盈亏，
  不做任何主观分摊。
- `risk_platform/results.py`：完整结果对象与原子发布仓库。
- `risk_platform/metrics.py`：基于同一份场景结果扩展风险指标，不重新定价。

每次估值记录 `scenario_id`、`version` 和场景内容的 SHA-256 `fingerprint`。任一场景缺价格、收益率或汇率，都会在生成总数前失败，不会偷偷沿用基准值。结果 JSON 先写临时文件，再原子替换；任务失败时不会发布不完整总数。

## 运行

```bash
python -m risk_platform.cli \
  --holdings data/holdings.json \
  --scenarios data/scenarios.json \
  --results-dir results \
  --run-id demo
```

旧 run 保存自己的场景版本和指纹。场景文件更新后，仍可用旧 run id 读取并追溯当时的输入，新 run 会获得新的版本和指纹。

## 多时点压力路径重放

单时点快照之外，`risk_platform/paths.py` 定义按时间顺序排列的压力路径，
`risk_platform/path_engine.py` 逐步重放。路径的每一步都有自己的市场快照，
可以在多个时点改变价格、收益率与汇率，还可以携带：

- `cash_flows`：持仓期间收到的现金流或再投资（按当步价格计入，独立于交易）。
- `trades`：预先给定的再平衡安排，只允许出现在 `rebalance` 路径；
  `hold` 路径必须保持原持仓。非现金交易自动按**上一步价格**生成同币种现金腿，
  因此交易在上一步估值下净值为零。

每一步相对前一步的差额严格分解为四项，并在路径层面加总闭合：

- 市场影响：原有持仓随市场变化的部分。
- 交易影响：再平衡按上一步价格计量的部分（含自动现金腿，净值为零）。
- 市场与交易的交互：交易得来/卖出的仓位被新市场重新定价的部分。
- 现金流影响：外部现金流按当步价格计入的部分。

四项之和同时等于每一步差额和从初始组合到当前组合的总差额，没有残差项。

历史情景与修订后的情景以相同 `path_id` 的不同 `version` 并排重放（如
`data/paths.json` 中 `recession` v1 持有、v2 再平衡），可直接比较终值与归因。
某一步缺少必要市场资料时，引擎停止在该步之前：保留此前各步的有效结果，
路径标记为 `incomplete`（`completed_steps < total_steps`），原子发布部分结果，
但 CLI 以非零退出码结束，绝不输出完整路径已完成的结论。

```bash
python -m risk_platform.cli \
  --holdings data/holdings.json \
  --paths data/paths.json \
  --results-dir results \
  --run-id demo-paths
```

加 `--path-id recession` 只重放该 id 的全部版本，便于历史与修订版并排对比。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包含少量虚构资产的逐项公式对照、持仓贡献求和、因素主效应与交互项的手算对照、
缺数据失败、旧版本追溯、指标复用和 CLI 发布；路径测试覆盖逐步差额闭合、
现金流、再平衡的市场/交易/交互分解、历史与修订版并排、缺数据仅发布部分结果。
