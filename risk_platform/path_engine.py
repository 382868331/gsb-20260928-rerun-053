from __future__ import annotations

from dataclasses import dataclass

from risk_platform.holdings import Holding
from risk_platform.paths import PathStep, StressPath
from risk_platform.results import PathResult, StepContribution, StepResult, TradeRecord
from risk_platform.scenarios import MarketScenario, MissingMarketDataError


@dataclass(frozen=True, slots=True)
class PathRunner:
    """Replays ordered stress paths step by step.

    Each step change is decomposed into:

    * market impact - pre-existing holdings moving with the market;
    * trading impact - pre-arranged rebalancing measured at previous prices
      (trades are value neutral there, including the balancing cash leg);
    * interaction impact - traded units re-priced by the new market;
    * cash flow effect - external receipts/withdrawals at current prices.

    The four terms sum exactly to the change in portfolio value, both per
    step and across the whole path.
    """

    pricers: dict[str, object]

    @classmethod
    def default(cls) -> "PathRunner":
        from risk_platform.engine import RevaluationEngine

        return cls(RevaluationEngine.default().pricers)

    def replay(
        self,
        path_id: str | None,
        holdings: list[Holding],
        paths: list[StressPath],
    ) -> tuple[PathResult, ...]:
        if path_id is not None:
            selected = [path for path in paths if path.path_id == path_id]
            if not selected:
                raise ValueError(f"No stress path named {path_id!r}")
        else:
            selected = paths
        return tuple(self._replay_one(holdings, path) for path in selected)

    def _replay_one(self, holdings: list[Holding], path: StressPath) -> PathResult:
        quantities = {holding.id: holding.quantity for holding in holdings}
        holding_by_id = {holding.id: holding for holding in holdings}

        start = path.steps[0]
        start.market.validate_holdings(holdings)
        start_value, start_contribs = self._value(holdings, start.market, quantities)
        step_results: list[StepResult] = [
            StepResult(
                index=0,
                label=start.label,
                scenario_id=start.market.scenario_id,
                scenario_version=start.market.version,
                scenario_fingerprint=start.market.fingerprint,
                portfolio_value=start_value,
                change_vs_previous=0.0,
                market_impact=0.0,
                trading_impact=0.0,
                interaction_impact=0.0,
                cash_flow_effect=0.0,
                executed_trades=(),
                applied_cash_flows=(),
                contributions=start_contribs,
            )
        ]

        status = "complete"
        missing_reason: str | None = None
        for index, step in enumerate(path.steps[1:], start=1):
            previous_market = path.steps[index - 1].market
            prev_quantities = dict(quantities)

            try:
                self._validate_step(holdings, holding_by_id, quantities, step)
            except MissingMarketDataError as error:
                status = "incomplete"
                missing_reason = str(error)
                break

            flow_delta, flows = self._cash_flow_deltas(step)
            trade_delta, executed = self._trade_deltas(
                holding_by_id, quantities, step, previous_market
            )
            for holding_id in set(flow_delta) | set(trade_delta):
                quantities[holding_id] = (
                    quantities.get(holding_id, 0.0)
                    + flow_delta.get(holding_id, 0.0)
                    + trade_delta.get(holding_id, 0.0)
                )

            step_value, contributions = self._value(holdings, step.market, quantities)
            market_impact, trading_impact, interaction_impact, cash_flow_effect = self._decompose(
                holdings=holdings,
                prev_quantities=prev_quantities,
                flow_delta=flow_delta,
                trade_delta=trade_delta,
                prev_market=previous_market,
                market=step.market,
            )
            change = step_value - step_results[-1].portfolio_value
            explained = (
                market_impact + trading_impact + interaction_impact + cash_flow_effect
            )
            if abs(change - explained) > 1e-9:
                raise AssertionError(
                    f"Attribution does not close at step {step.label}: {change - explained}"
                )

            step_results.append(
                StepResult(
                    index=index,
                    label=step.label,
                    scenario_id=step.market.scenario_id,
                    scenario_version=step.market.version,
                    scenario_fingerprint=step.market.fingerprint,
                    portfolio_value=step_value,
                    change_vs_previous=change,
                    market_impact=market_impact,
                    trading_impact=trading_impact,
                    interaction_impact=interaction_impact,
                    cash_flow_effect=cash_flow_effect,
                    executed_trades=executed,
                    applied_cash_flows=flows,
                    contributions=contributions,
                )
            )

        first = step_results[0]
        last = step_results[-1]
        return PathResult(
            path_id=path.path_id,
            path_version=path.version,
            path_fingerprint=path.fingerprint,
            description=path.description,
            strategy=path.strategy,
            status=status,
            completed_steps=len(step_results),
            total_steps=len(path.steps),
            missing_data_reason=missing_reason,
            start_value=first.portfolio_value,
            final_value=last.portfolio_value,
            total_change=last.portfolio_value - first.portfolio_value,
            total_market_impact=sum(step.market_impact for step in step_results),
            total_trading_impact=sum(step.trading_impact for step in step_results),
            total_interaction_impact=sum(step.interaction_impact for step in step_results),
            total_cash_flow_effect=sum(step.cash_flow_effect for step in step_results),
            steps=tuple(step_results),
        )

    def _validate_step(
        self,
        holdings: list[Holding],
        holding_by_id: dict[str, Holding],
        quantities: dict[str, float],
        step: PathStep,
    ) -> None:
        referenced = {flow.holding_id for flow in step.cash_flows}
        referenced.update(trade.holding_id for trade in step.trades)
        referenced_currencies = {
            holding_by_id[holding_id].currency
            for holding_id in referenced
            if holding_by_id[holding_id].asset_type != "cash"
        }
        referenced.update(
            self._cash_holding(holding_by_id, currency).id
            for currency in referenced_currencies
        )
        active = [
            holding
            for holding in holdings
            if quantities.get(holding.id, 0.0) != 0.0 or holding.id in referenced
        ]
        step.market.validate_holdings(active)
        for holding_id in referenced:
            if holding_id not in holding_by_id:
                raise ValueError(
                    f"Step {step.label} references unknown holding {holding_id}"
                )

    @staticmethod
    def _cash_flow_deltas(
        step: PathStep,
    ) -> tuple[dict[str, float], tuple[TradeRecord, ...]]:
        deltas: dict[str, float] = {}
        records: list[TradeRecord] = []
        for flow in step.cash_flows:
            deltas[flow.holding_id] = deltas.get(flow.holding_id, 0.0) + flow.amount
            records.append(TradeRecord(holding_id=flow.holding_id, amount=flow.amount))
        return deltas, tuple(records)

    def _trade_deltas(
        self,
        holding_by_id: dict[str, Holding],
        quantities: dict[str, float],
        step: PathStep,
        previous_market: MarketScenario,
    ) -> tuple[dict[str, float], tuple[TradeRecord, ...]]:
        deltas: dict[str, float] = {}
        records: list[TradeRecord] = []
        cash_units_by_currency: dict[str, float] = {}
        for trade in step.trades:
            holding = holding_by_id[trade.holding_id]
            deltas[holding.id] = deltas.get(holding.id, 0.0) + trade.quantity_delta
            records.append(TradeRecord(holding_id=trade.holding_id, amount=trade.quantity_delta))
            if holding.asset_type != "cash":
                unit_local = self._unit_local_value(holding, previous_market)
                # Sell raises cash, buy consumes it: negative of traded notional.
                cash_units_by_currency[holding.currency] = (
                    cash_units_by_currency.get(holding.currency, 0.0)
                    - trade.quantity_delta * unit_local
                )
        for currency, cash_units in cash_units_by_currency.items():
            cash_holding = self._cash_holding(holding_by_id, currency)
            deltas[cash_holding.id] = deltas.get(cash_holding.id, 0.0) + cash_units
            records.append(TradeRecord(holding_id=cash_holding.id, amount=cash_units))
        return deltas, tuple(records)

    @staticmethod
    def _cash_holding(holding_by_id: dict[str, Holding], currency: str) -> Holding:
        matches = [
            holding
            for holding in holding_by_id.values()
            if holding.asset_type == "cash" and holding.currency == currency
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Rebalancing in {currency} needs exactly one {currency} cash holding"
            )
        return matches[0]

    def _unit_local_value(self, holding: Holding, market: MarketScenario) -> float:
        pricer = self.pricers[holding.asset_type]
        if holding.asset_type == "cash":
            return 1.0
        local_value = pricer.price_in_currency(holding, market)
        return local_value / holding.quantity

    def _unit_base_value(self, holding: Holding, market: MarketScenario) -> float:
        return self._unit_local_value(holding, market) * market.fx_rate(holding.currency)

    def _value(
        self,
        holdings: list[Holding],
        market: MarketScenario,
        quantities: dict[str, float],
    ) -> tuple[float, tuple[StepContribution, ...]]:
        contributions: list[StepContribution] = []
        total = 0.0
        for holding in holdings:
            quantity = quantities.get(holding.id, 0.0)
            unit_local = self._unit_local_value(holding, market)
            fx_rate = market.fx_rate(holding.currency)
            value = quantity * unit_local * fx_rate
            total += value
            contributions.append(
                StepContribution(
                    holding_id=holding.id,
                    currency=holding.currency,
                    quantity=quantity,
                    unit_local_value=unit_local,
                    fx_rate=fx_rate,
                    value=value,
                )
            )
        return total, tuple(contributions)

    def _decompose(
        self,
        holdings: list[Holding],
        prev_quantities: dict[str, float],
        flow_delta: dict[str, float],
        trade_delta: dict[str, float],
        prev_market: MarketScenario,
        market: MarketScenario,
    ) -> tuple[float, float, float, float]:
        market_impact = 0.0
        trading_impact = 0.0
        interaction_impact = 0.0
        cash_flow_effect = 0.0

        all_ids = set(prev_quantities) | set(flow_delta) | set(trade_delta)
        holding_by_id = {holding.id: holding for holding in holdings}
        for holding_id in all_ids:
            holding = holding_by_id[holding_id]
            before = prev_quantities.get(holding_id, 0.0)
            traded = trade_delta.get(holding_id, 0.0)
            flowed = flow_delta.get(holding_id, 0.0)
            prev_price = self._unit_base_value(holding, prev_market)
            new_price = self._unit_base_value(holding, market)

            market_impact += before * (new_price - prev_price)
            trading_impact += traded * prev_price
            interaction_impact += traded * (new_price - prev_price)
            cash_flow_effect += flowed * new_price

        return market_impact, trading_impact, interaction_impact, cash_flow_effect
