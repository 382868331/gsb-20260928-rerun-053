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

处置方案默认并排重放两种成交假设：`action:<plan_id>` 为即时全额成交，
`constrained:<plan_id>` 使用人工市场情景中每个时段的 `fill_windows`。
每个窗口给出可成交数量、本地价格或换汇报价区间、价格上下沿和结算延迟。
受限方案沿路径展示计划数量、实际成交、尚未成交、已成交但未结算资金、结算入账、
累计费用和价格滑点。限额只使用当时真实可用的持仓和已结算现金；待结算资金计入
风险敞口和组合价值，但不能提前用于继续买入、换汇或其他对冲。

同一时段多笔安排争用窗口容量时，计划必须在执行前指定 `allocation`：
`priority` 按订单顺序，或 `pro_rata` 按当时可成交/可用资金比例。成交价格通过
`fill_price` 在执行前固定为 `worst`、`mid` 或 `best`，不能看到后续损失后改选。
步骤的 `cancellations` 只取消尚未成交安排，不撤销已成交部分；部分成交仅对实际
成交量收一次手续费和汇兑成本。某限额恢复为 `confirmed_within` 不会清除其他指标的
`breached` 状态，也不会改变各指标首次越界步骤和来源。

卖出的费用从成交收入中扣除，换汇只改变现金币种。`action_effect` 仅反映费用、
价格带滑点等真实经济影响；`cash_flow_from_actions` 只统计资产变现形成的现金，
内部换汇不重复计为现金流。成交明细同时给出处置前后总金额、窗口上下沿和成交价格，
差额可与费用、滑点逐项核对。

每个受限方案的步骤台账包含持仓、可用现金、按币种列示的待结算资金、累计成交、
累计结算、累计费用和未成交订单。`risk_platform.results.compare_disposal_policies`
并排比较不处置、即时全额和受限成交，把差异标为成交不足、价格带变化、费用及
结算滞后。受限结果始终保存 `execution_state`；即使完整运行完成也可保存审计状态，
缺市场数据时则保存 partial。后续只可修改尚未执行的市场资料：`--resume-from`
读取旧 run 后会核对已执行步骤的场景 SHA-256，历史成交、待结算金额、现金流和
累计费用不会被改写，校验失败直接报错。

## 结算失败与补救

原成交的待结算资金可在计划结算日入账；被拒后 `resettle` 的余款只在新对手方
真实 `settle` 到账通知到达时转为可用现金。步骤可以声明
`settlement_events`（`settle` 实际到账、`reject` 对手方拒绝，金额可小于待结算
余额以表示分批结算）和 `settlement_actions`（`resettle` 更换对手方重新结算、
`write_off` 撤销余款认亏）。被拒绝的部分进入 disputed 状态：仍计入风险敞口与
组合价值，但在计划结算日到达且没有到账通知时不会自动变成现金，也不能用于继续
买入或换汇。继续等待不需要任何动作，余款保持 disputed。

`resettle` 把 disputed 金额交给新对手方，按 `fee_bps` 收取新费用并按
`settlement_steps` 记录新的计划到账日；该日期只用于核对，不会绕过实际到账通知
自动入账。新费用是实际损失，原交易费用不退回，已到账部分不会重新冻结。
`write_off` 把 disputed 金额确认为实际损失。
步骤的 `settlement_effect` 记录当步结算造成的价值变化（撤销损失与新费用），
与市场、现金流、成交影响一起精确闭合该步变动。

台账按币种分别列示待结算（outstanding）与争议中（disputed）资金，并累计
已核销金额和额外结算费用。`risk_platform.results.compare_settlement_outcomes`
把继续等待、撤销余款、重新结算等分支（不同路径版本或多次运行）并排比较，
分列实际损失（核销 + 额外费用 + 原交易费用）、暂时不可用资金和仍有争议的
金额。当前有效对手方之外的到账或拒收通知记为 `counterparty_mismatch`，不会
入账；当前有效对手方重复到账记为 `duplicate_receipt`（只按欠款入账，不重复计
现金）、已结清后收到的拒绝记为 `late_rejection`、已修正后收到的旧通知记为
`stale_notice`，全部保留在步骤与政策的结算事件日志
（`settlement_events` / `settlement_log`）中。结算日志随 `execution_state` 一起
序列化，重开中途研究后继续处理事件，得到的资金轨迹与不中断处理完全一致。

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
