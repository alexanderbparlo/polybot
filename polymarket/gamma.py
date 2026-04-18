from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, Optional

from .config import PolymarketConfig
from .http import PolymarketHTTP
from .models import Market, MarketToken

logger = logging.getLogger(__name__)


class GammaAPI:
    """Gamma API client — market discovery and metadata (public, no auth)."""

    def __init__(self, config: PolymarketConfig, http: PolymarketHTTP):
        self.config = config
        self.http = http

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        min_volume: float = 0.0,
        active_only: bool = True,
        exclude_neg_risk: bool = True,
    ) -> list[Market]:
        url = f"{self.config.gamma_host}/markets"
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": "true" if active_only else "false",
        }
        data = await self.http.get(url, params=params)

        markets: list[Market] = []
        raw_list = data if isinstance(data, list) else data.get("markets", [])
        for raw in raw_list:
            try:
                market = self._parse_market(raw)
            except Exception as exc:
                logger.debug("Failed to parse market %s: %s", raw.get("conditionId", "?"), exc)
                continue

            if exclude_neg_risk and market.neg_risk:
                continue
            if market.volume_24h < min_volume:
                continue
            markets.append(market)

        logger.info("Fetched %d qualifying markets from Gamma API", len(markets))
        return markets

    async def get_market(self, condition_id: str) -> Optional[Market]:
        url = f"{self.config.gamma_host}/markets/{condition_id}"
        try:
            raw = await self.http.get(url)
            return self._parse_market(raw)
        except Exception as exc:
            logger.error("Failed to fetch market %s: %s", condition_id, exc)
            return None

    def _parse_market(self, raw: dict) -> Market:
        # Parse tokens (YES / NO)
        tokens: list[MarketToken] = []
        clob_token_ids: list[str] = raw.get("clobTokenIds", [])
        outcomes: list[str] = raw.get("outcomes", ["Yes", "No"])
        outcome_prices_str: list[str] = raw.get("outcomePrices", [])

        for i, token_id in enumerate(clob_token_ids):
            outcome = outcomes[i] if i < len(outcomes) else f"token_{i}"
            try:
                price = float(outcome_prices_str[i]) if i < len(outcome_prices_str) else 0.5
            except (ValueError, TypeError):
                price = 0.5
            tokens.append(MarketToken(token_id=token_id, outcome=outcome, price=price))

        # Parse end date
        end_date: Optional[datetime] = None
        raw_end = raw.get("endDate") or raw.get("endDateIso")
        if raw_end:
            try:
                end_date = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Volume — Gamma returns strings sometimes
        try:
            volume_24h = float(raw.get("volume24hr", 0) or 0)
        except (ValueError, TypeError):
            volume_24h = 0.0

        try:
            liquidity = float(raw.get("liquidity", 0) or 0)
        except (ValueError, TypeError):
            liquidity = 0.0

        return Market(
            condition_id=raw["conditionId"],
            question=raw.get("question", ""),
            end_date=end_date,
            tokens=tokens,
            volume_24h=volume_24h,
            liquidity=liquidity,
            active=raw.get("active", True),
            tick_size=float(raw.get("minimumTickSize", 0.01) or 0.01),
            neg_risk=bool(raw.get("negRisk", False)),
        )
