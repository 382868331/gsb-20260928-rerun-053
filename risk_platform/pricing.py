from __future__ import annotations

import math
from abc import ABC, abstractmethod

from risk_platform.holdings import Holding
from risk_platform.scenarios import MarketScenario


class Pricer(ABC):
    asset_type: str

    @abstractmethod
    def price_in_currency(self, holding: Holding, scenario: MarketScenario) -> float:
        raise NotImplementedError


class CashPricer(Pricer):
    asset_type = "cash"

    def price_in_currency(self, holding: Holding, scenario: MarketScenario) -> float:
        return holding.quantity


class EquityPricer(Pricer):
    asset_type = "equity"

    def price_in_currency(self, holding: Holding, scenario: MarketScenario) -> float:
        return holding.quantity * scenario.equity_prices[holding.market_data_key]


class ZeroCouponBondPricer(Pricer):
    asset_type = "zero_coupon_bond"

    def price_in_currency(self, holding: Holding, scenario: MarketScenario) -> float:
        if holding.years_to_maturity is None:
            raise ValueError(f"Holding {holding.id} requires years_to_maturity")
        return holding.quantity * math.exp(
            -scenario.bond_yields[holding.market_data_key] * holding.years_to_maturity
        )
