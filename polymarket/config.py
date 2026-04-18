from __future__ import annotations
import base64
import binascii
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Only this exact value (case-insensitive) disables paper mode.
# "1", "yes", "on", "TRUE" all remain in paper mode for safety.
_PAPER_MODE_OFF = "false"


@dataclass
class PolymarketConfig:
    # Endpoints
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"

    # Auth
    private_key: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    funder_address: Optional[str] = None
    chain_id: int = 137
    signature_type: int = 0

    # Mode
    paper_mode: bool = True  # Safety: simulate fills without real orders

    # HTTP
    request_timeout: int = 10
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "PolymarketConfig":
        raw_paper = os.getenv("POLY_PAPER_MODE", "true").strip().lower()
        # SECURITY: only the literal string "false" disables paper mode.
        # Any other value ("1", "yes", "on", "true", unset) keeps it ON.
        paper_mode = raw_paper != _PAPER_MODE_OFF
        if raw_paper not in ("true", "false"):
            logger.warning(
                "POLY_PAPER_MODE=%r is not 'true' or 'false' — defaulting to paper mode ON. "
                "Set POLY_PAPER_MODE=false explicitly to enable live trading.",
                raw_paper,
            )

        private_key = os.getenv("POLY_PRIVATE_KEY")
        api_secret = os.getenv("POLY_API_SECRET")

        # SECURITY: validate private key format at load time so the bot
        # fails fast instead of crashing mid-trade on a bad signature.
        if private_key is not None:
            clean = private_key.strip().lstrip("0x")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", clean):
                raise ValueError(
                    "POLY_PRIVATE_KEY must be a 64-character hex string (no 0x prefix). "
                    "Check your .env file."
                )
            private_key = clean  # store without any 0x prefix

        # SECURITY: validate api_secret is valid base64 now, not at signing time.
        # A corrupt secret would otherwise crash the bot the moment it tries to place an order.
        if api_secret is not None:
            try:
                base64.b64decode(api_secret, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    f"POLY_API_SECRET is not valid base64: {exc}. "
                    "Re-run 'python main.py setup-credentials' to regenerate."
                ) from exc

        return cls(
            private_key=private_key,
            api_key=os.getenv("POLY_API_KEY"),
            api_secret=api_secret,
            api_passphrase=os.getenv("POLY_API_PASSPHRASE"),
            funder_address=os.getenv("POLY_FUNDER_ADDRESS"),
            chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
            signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
            paper_mode=paper_mode,
        )

    @property
    def is_authenticated(self) -> bool:
        return all([self.private_key, self.api_key, self.api_secret, self.api_passphrase])
