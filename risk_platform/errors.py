class RiskPlatformError(Exception):
    """Base class for deterministic valuation failures."""


class MissingMarketDataError(RiskPlatformError):
    pass


class IncompleteValuationError(RiskPlatformError):
    pass
