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
- `risk_platform/paths.py`：有序多步压力路径（含现金流与再平衡安排）、路径指纹。
- `risk_platform/path_engine.py`：逐步重放路径。每一步把组合变动精确分解为市场影响
  （沿用前一步持仓、新市场）、交易影响（再平衡按前一步市场计价）及两者交互，
  现金流单独列示；四项之和严格等于该步变动，各步之和严格等于初始到最终的总差额。
  支持 hold（保持原持仓）与 rebalance（执行预定再平衡）两种情景并排重放。
  某一步缺少市场资料时保留已完成步骤的有效结果，结果标记为 partial，
  不会发布完整路径已完成的结论。路径按 (path_id, version) 版本化，
  历史情景与修订情景可在同一次运行中并排重放并逐项核对。

每次估值记录 `scenario_id`、`version` 和场景内容的 SHA-256 `fingerprint`。任一场景缺价格、收益率或汇率，都会在生成总数前失败，不会偷偷沿用基准值。结果 JSON 先写临时文件，再原子替换；任务失败时不会发布不完整总数。

## 运行

```bash
python -m risk_platform.cli \
  --holdings data/holdings.json \
  --scenarios data/scenarios.json \
  --results-dir results \
  --run-id demo
```

## 多步压力路径

```bash
python -m risk_platform.cli \
  --holdings data/holdings.json \
  --paths data/paths.json \
  --results-dir results \
  --run-id demo
```

路径文件可在路径级配置 `limits`，同时约束累计损失、币种基础币种敞口和单一
资产/资产类型集中度；也可在 `action_plans` 中为触发点预设减仓或换汇方案。
每个时点评估处置前与处置后快照，分别记录第一次触及各限额的位置、超限来源、
超限幅度、仍超限指标和因缺资料无法确认的指标；某个指标回到范围内会标记为
`confirmed_within`，不会掩盖其他仍为 `breached` 的指标。

处置只使用触发时点场景可见的价格、汇率和可用持仓/现金。卖出的费用从成交
收入中扣除，换汇只改变现金币种；`action_effect` 仅为费用等真实经济影响，
`cash_flow_from_actions` 只统计资产变现形成的现金，内部换汇不重复计为现金流。
成交明细同时给出处置前后总金额，差额可与 `action_effect` 逐项核对。

结果写入 <run-id>-paths.json，每个 (path_id, version) 默认并列 hold、
rebalance 和每个 `action:<plan_id>` 方案；缺数据的路径以 partial 状态保存
已完成步骤和已确认越界事实。用 risk_platform.results.compare_path_results
可以把多次运行或同一路径的新旧版本并排成表核对。限额与方案属于路径定义，
会进入路径 SHA-256 指纹；修改后续市场情景会产生新版本，旧 run 中触发依据、
方案版本和指纹仍可追溯。

旧 run 保存自己的场景版本和指纹。场景文件更新后，仍可用旧 run id 读取并追溯当时的输入，新 run 会获得新的版本和指纹。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包含少量虚构资产的逐项公式对照、持仓贡献求和、因素主效应与交互项的手算对照、
缺数据失败、旧版本追溯、指标复用和 CLI 发布。
