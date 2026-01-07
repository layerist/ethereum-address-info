#!/usr/bin/env python3
"""
Robust Ethereum address info client using Etherscan API.

Features:
- Typed configuration via dataclass
- Automatic retries with exponential backoff
- Rate-limit detection and handling
- Persistent HTTP session
- Structured logging
"""

from __future__ import annotations

import os
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from requests import Session
from requests.exceptions import Timeout, HTTPError, RequestException
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)
from dotenv import load_dotenv


# ============================================================
# Environment
# ============================================================
load_dotenv()


# ============================================================
# Logging
# ============================================================
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def setup_logger(
    name: str = "etherscan",
    level: int = logging.INFO,
) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


logger = setup_logger(level=logging.DEBUG)


# ============================================================
# Exceptions
# ============================================================
class EthereumAPIError(Exception):
    """Base Etherscan API exception."""


class EthereumRateLimitError(EthereumAPIError):
    """Raised when API rate limit is exceeded."""


class EthereumResponseError(EthereumAPIError):
    """Raised on malformed or unexpected API responses."""


# ============================================================
# Configuration
# ============================================================
@dataclass(frozen=True)
class EthereumAPIConfig:
    api_key: str
    address: str
    timeout: int = 10
    max_retries: int = 3
    backoff_multiplier: float = 2.0
    max_backoff: float = 10.0
    rate_limit_wait: int = 5


# ============================================================
# Client
# ============================================================
class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"
    USER_AGENT = "EthereumAPIClient/4.0"

    def __init__(self, config: EthereumAPIConfig) -> None:
        if not config.api_key:
            raise ValueError("Etherscan API key is required")
        if not config.address:
            raise ValueError("Ethereum address is required")

        self.config = config
        self.session: Session = requests.Session()
        self.session.headers.update({"User-Agent": self.USER_AGENT})

    def __enter__(self) -> "EthereumAddressInfo":
        return self

    def __exit__(self, *_: Any) -> None:
        self.session.close()

    # ------------------------------------------------------------
    # Core request handler
    # ------------------------------------------------------------
    @retry(
        retry=retry_if_exception_type(
            (Timeout, HTTPError, EthereumAPIError)
        ),
        wait=wait_exponential_jitter(
            multiplier=2,
            max=10,
        ),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _request(self, params: Dict[str, str]) -> Any:
        params = {**params, "apikey": self.config.api_key}

        try:
            logger.debug("HTTP GET %s | params=%s", self.BASE_URL, params)
            response = self.session.get(
                self.BASE_URL,
                params=params,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Timeout:
            logger.warning("Request timeout")
            raise
        except HTTPError:
            logger.warning("HTTP status error")
            raise
        except RequestException as exc:
            raise EthereumAPIError(f"Network error: {exc}") from exc
        except ValueError as exc:
            raise EthereumResponseError("Invalid JSON response") from exc

        return self._parse_response(payload)

    # ------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------
    def _parse_response(self, data: Dict[str, Any]) -> Any:
        if not isinstance(data, dict):
            raise EthereumResponseError("Unexpected response format")

        status = data.get("status")
        message = str(data.get("message", "")).lower()
        result = data.get("result")

        if status == "1":
            return result

        if "rate limit" in message or "rate limit" in str(result).lower():
            logger.warning(
                "Rate limit reached, sleeping %ds",
                self.config.rate_limit_wait,
            )
            time.sleep(self.config.rate_limit_wait)
            raise EthereumRateLimitError("Rate limit reached")

        if status == "0" and result == "No transactions found":
            return []

        raise EthereumAPIError(f"Etherscan error: {message or result}")

    # ------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------
    @staticmethod
    def wei_to_eth(
        value: Union[str, int],
        decimals: int = 18,
    ) -> float:
        try:
            return int(value) / 10**decimals
        except (TypeError, ValueError):
            logger.debug("Invalid Wei value: %r", value)
            return 0.0

    # ------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------
    def get_balance(self) -> float:
        params = {
            "module": "account",
            "action": "balance",
            "address": self.config.address,
            "tag": "latest",
        }
        wei = self._request(params)
        balance = self.wei_to_eth(wei)
        logger.info("ETH balance: %.6f", balance)
        return balance

    def get_transactions(
        self,
        start_block: int = 0,
        end_block: int = 99_999_999,
        sort: str = "asc",
    ) -> List[Dict[str, Any]]:
        params = {
            "module": "account",
            "action": "txlist",
            "address": self.config.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": sort,
        }
        txs = self._request(params)
        logger.info("Transactions found: %d", len(txs))
        return txs

    def get_token_balance(
        self,
        contract_address: str,
        decimals: int = 18,
    ) -> Optional[float]:
        if not contract_address:
            logger.warning("Token contract address not provided")
            return None

        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.config.address,
            "tag": "latest",
        }
        wei = self._request(params)
        balance = self.wei_to_eth(wei, decimals)
        logger.info(
            "Token balance [%s]: %.6f",
            contract_address,
            balance,
        )
        return balance


# ============================================================
# Entry point
# ============================================================
def main() -> None:
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS")
    token_decimals = int(os.getenv("TOKEN_DECIMALS", "18"))

    if not api_key or not address:
        logger.error("Missing ETHERSCAN_API_KEY or ETHEREUM_ADDRESS")
        return

    config = EthereumAPIConfig(
        api_key=api_key,
        address=address,
    )

    try:
        with EthereumAddressInfo(config) as eth:
            eth.get_balance()

            txs = eth.get_transactions()
            for tx in txs[:3]:
                logger.debug("TX: %s", tx)

            if contract_address:
                eth.get_token_balance(contract_address, token_decimals)

    except EthereumRateLimitError:
        logger.error("Rate limit exceeded. Try again later.")
    except EthereumAPIError as exc:
        logger.error("Ethereum API error: %s", exc)
    except Exception:
        logger.exception("Unexpected fatal error")


if __name__ == "__main__":
    main()
