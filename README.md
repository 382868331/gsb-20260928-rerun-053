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
- `risk_platform/limits.py`：组合风险限额（损失、币种敞口、单一资产集中度），
  每个时点逐项独立判定，某项回到范围内不会掩盖其他仍超限的指标。
- `risk_platform/disposal.py`：版本化处置方案（减仓 sell / 换汇 fx_convert，含费率）。
- `risk_platform/limit_engine.py`：沿路径逐时点检查限额，记录每个限额第一次触及的
  位置、观测值与超限来源；处置方案在触发时点上且仅在该时点可见的资料下执行一次，
  与不处置基线并排比较后续风险、费用与现金流。每一步变动精确分解为市场、现金流、
  预定交易、处置与费用五个分量，合计严格等于期初期末差额，同一笔交易不会被算两次。
  资料缺失时保留已确认的越界事实，其余限额标记为 unconfirmed，路径状态为 partial，
  不会宣称整条路径安全。
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

路径文件按步骤给出市场情景、现金流（收到后以同币种现金再投资）和再平衡交易。
结果写入 <run-id>-paths.json，每个 (path_id, version) 下分 hold / rebalance
两种策略给出逐步归因与合计；缺数据的路径以 partial 状态保存已完成步骤。
用 risk_platform.results.compare_path_results 可以把多次运行或同一路径的
新旧版本并排成表核对。

## 风险限额与处置方案

```bash
python -m risk_platform.cli \
  --holdings data/holdings.json \
  --paths data/paths.json \
  --limits data/limits.json \
  --disposals data/disposals.json \
  --results-dir results \
  --run-id demo
```

限额文件支持三类指标：loss（相对路径起点的损失）、currency_exposure（单一币种
敞口绝对值）、concentration（最大单一持仓占比）。处置方案按 (plan_id, version)
版本化，由某个限额首次越界触发；sell 按持仓比例减仓并以同币种现金留存回收款，
fx_convert 在触发时点汇率下换汇，两者均按 fee_rate 计费。结果写入
<run-id>-limits.json：每个 (path_id, version) 下包含 no_action 基线与各处置方案
的逐步归因、总费用、触发证据（触发步骤、场景版本与指纹、观测值、超限来源）以及
未确认限额列表。保存结果后修改后续市场情景或方案版本，用新的 run id 重跑即可，
原触发依据与处置版本仍可通过旧 run id 查询；
risk_platform.results.compare_limit_results 把各方案并排成表核对处置前后总金额
变化与归因。

旧 run 保存自己的场景版本和指纹。场景文件更新后，仍可用旧 run id 读取并追溯当时的输入，新 run 会获得新的版本和指纹。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包含少量虚构资产的逐项公式对照、持仓贡献求和、因素主效应与交互项的手算对照、
缺数据失败、旧版本追溯、指标复用和 CLI 发布。
