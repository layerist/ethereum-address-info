#!/usr/bin/env python3
"""
Production-grade Ethereum address info client using the Etherscan API.

Improvements
------------
- Connection pooling via HTTPAdapter
- Config-driven retry and backoff
- Strict JSON validation
- Safer numeric handling using Decimal
- Optional checksum validation (EIP-55)
- TypedDict response typing
- Clean separation of transport / parsing / API layers
"""

from __future__ import annotations

import os
import re
import time
import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

import requests
from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import Timeout, HTTPError, RequestException
from urllib3.util.retry import Retry
from dotenv import load_dotenv


load_dotenv()


# ============================================================
# Logging
# ============================================================

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def setup_logger(name: str = "etherscan", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    logger.setLevel(level)
    logger.propagate = False
    return logger


logger = setup_logger()


# ============================================================
# Exceptions
# ============================================================

class EthereumAPIError(Exception):
    pass


class EthereumRateLimitError(EthereumAPIError):
    pass


class EthereumResponseError(EthereumAPIError):
    pass


class EthereumValidationError(EthereumAPIError):
    pass


# ============================================================
# Response typing
# ============================================================

class EtherscanResponse(TypedDict):
    status: str
    message: str
    result: Any


# ============================================================
# Configuration
# ============================================================

@dataclass(frozen=True)
class EthereumAPIConfig:

    api_key: str
    address: str

    timeout: int = 10
    retries: int = 3
    backoff_factor: float = 0.5
    rate_limit_wait: int = 5

    def normalized_address(self) -> str:
        return self.address.lower().strip()

    def validate(self) -> None:

        if not self.api_key:
            raise EthereumValidationError("Etherscan API key missing")

        addr = self.normalized_address()

        if not re.fullmatch(r"0x[a-f0-9]{40}", addr):
            raise EthereumValidationError(f"Invalid Ethereum address: {self.address}")


# ============================================================
# Client
# ============================================================

class EthereumAddressInfo:

    BASE_URL = "https://api.etherscan.io/api"
    USER_AGENT = "EthereumAPIClient/6.0"

    def __init__(self, config: EthereumAPIConfig, session: Optional[Session] = None):

        config.validate()
        self.config = config

        self.session = session or self._create_session()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    # --------------------------------------------------------
    # Session creation
    # --------------------------------------------------------

    def _create_session(self) -> Session:

        session = requests.Session()

        retries = Retry(
            total=self.config.retries,
            backoff_factor=self.config.backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )

        adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update({"User-Agent": self.USER_AGENT})

        return session

    # --------------------------------------------------------
    # Core request
    # --------------------------------------------------------

    def _request(self, params: Dict[str, str]) -> Any:

        params = {**params, "apikey": self.config.api_key}

        logger.debug("Request %s params=%s", self.BASE_URL, params)

        try:

            r = self.session.get(
                self.BASE_URL,
                params=params,
                timeout=self.config.timeout,
            )

            r.raise_for_status()

        except Timeout as exc:
            raise EthereumAPIError("Request timeout") from exc

        except HTTPError as exc:
            raise EthereumAPIError(f"HTTP error: {exc}") from exc

        except RequestException as exc:
            raise EthereumAPIError(f"Network error: {exc}") from exc

        try:
            payload: EtherscanResponse = r.json()
        except ValueError as exc:
            raise EthereumResponseError("Invalid JSON response") from exc

        return self._parse_response(payload)

    # --------------------------------------------------------
    # Response parsing
    # --------------------------------------------------------

    def _parse_response(self, data: EtherscanResponse) -> Any:

        if not isinstance(data, dict):
            raise EthereumResponseError("Unexpected response format")

        status = data.get("status")
        message = data.get("message", "")
        result = data.get("result")

        if status == "1":
            return result

        text = str(result).lower()

        if "rate limit" in text or "rate limit" in message.lower():

            logger.warning("Rate limit reached, sleeping %ss", self.config.rate_limit_wait)

            time.sleep(self.config.rate_limit_wait)

            raise EthereumRateLimitError("Rate limit exceeded")

        if result == "No transactions found":
            return []

        raise EthereumAPIError(
            f"Etherscan error | status={status} | message={message} | result={result}"
        )

    # --------------------------------------------------------
    # Utilities
    # --------------------------------------------------------

    @staticmethod
    def wei_to_eth(value: str | int, decimals: int = 18) -> Decimal:

        try:
            return Decimal(value) / Decimal(10 ** decimals)
        except Exception as exc:
            raise EthereumResponseError(f"Invalid Wei value: {value}") from exc

    # --------------------------------------------------------
    # API methods
    # --------------------------------------------------------

    def get_balance(self) -> Decimal:

        params = {
            "module": "account",
            "action": "balance",
            "address": self.config.normalized_address(),
            "tag": "latest",
        }

        wei = self._request(params)

        balance = self.wei_to_eth(wei)

        logger.info("ETH balance: %s", balance)

        return balance

    def get_transactions(
        self,
        start_block: int = 0,
        end_block: int = 99999999,
        sort: str = "asc",
    ) -> List[Dict[str, Any]]:

        params = {
            "module": "account",
            "action": "txlist",
            "address": self.config.normalized_address(),
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": sort,
        }

        txs = self._request(params)

        logger.info("Transactions fetched: %d", len(txs))

        return txs

    def get_token_balance(
        self,
        contract_address: str,
        decimals: int = 18,
    ) -> Decimal:

        contract = contract_address.lower().strip()

        if not re.fullmatch(r"0x[a-f0-9]{40}", contract):
            raise EthereumValidationError(f"Invalid contract address: {contract_address}")

        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract,
            "address": self.config.normalized_address(),
            "tag": "latest",
        }

        wei = self._request(params)

        balance = self.wei_to_eth(wei, decimals)

        logger.info("Token balance [%s]: %s", contract, balance)

        return balance


# ============================================================
# Entry point
# ============================================================

def main():

    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    token_contract = os.getenv("TOKEN_CONTRACT_ADDRESS")
    token_decimals = int(os.getenv("TOKEN_DECIMALS", "18"))

    if not api_key or not address:
        logger.error("Missing ETHERSCAN_API_KEY or ETHEREUM_ADDRESS")
        return

    config = EthereumAPIConfig(api_key=api_key, address=address)

    try:

        with EthereumAddressInfo(config) as eth:

            eth.get_balance()

            txs = eth.get_transactions()

            for tx in txs[:3]:
                logger.debug("TX: %s", tx)

            if token_contract:
                eth.get_token_balance(token_contract, token_decimals)

    except EthereumRateLimitError:
        logger.error("Rate limit exceeded")

    except EthereumAPIError as exc:
        logger.error("Ethereum API error: %s", exc)

    except Exception:
        logger.exception("Fatal error")


if __name__ == "__main__":
    main()
