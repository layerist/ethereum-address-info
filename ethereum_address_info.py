#!/usr/bin/env python3
"""
High-performance Ethereum client using Etherscan API.

Key features:
- Thread-safe session
- Global rate limiting (Etherscan-safe)
- Smart retry (network + API-level)
- Auto pagination (until exhaustion)
- Optional parallel fetching
- Clean architecture
"""

from __future__ import annotations

import os
import re
import time
import random
import logging
import threading
from decimal import Decimal, getcontext
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict, Generator

import requests
from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()
getcontext().prec = 50


# ============================================================
# Logging
# ============================================================

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"


def setup_logger():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger("eth")

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    logger.setLevel(level)
    return logger


logger = setup_logger()


# ============================================================
# Exceptions
# ============================================================

class EthereumAPIError(Exception):
    pass


class EthereumRateLimitError(EthereumAPIError):
    pass


class EthereumValidationError(EthereumAPIError):
    pass


# ============================================================
# Types
# ============================================================

class EtherscanResponse(TypedDict):
    status: str
    message: str
    result: Any


# ============================================================
# Rate Limiter
# ============================================================

class RateLimiter:
    """
    Simple thread-safe rate limiter.
    """

    def __init__(self, calls_per_sec: float):
        self.interval = 1.0 / calls_per_sec
        self.lock = threading.Lock()
        self.last_call = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_call

            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

            self.last_call = time.time()


# ============================================================
# Config
# ============================================================

@dataclass(frozen=True)
class EthereumAPIConfig:
    api_key: str
    address: str

    timeout: int = 10
    retries: int = 5
    backoff_factor: float = 0.5
    max_backoff: float = 10

    rate_limit_per_sec: float = 4.0  # safe for free tier
    use_checksum: bool = False
    proxies: Optional[Dict[str, str]] = None

    def normalized_address(self) -> str:
        return self.address.strip().lower()

    def validate(self):
        if not self.api_key:
            raise EthereumValidationError("Missing API key")

        addr = self.normalized_address()

        if not re.fullmatch(r"0x[a-f0-9]{40}", addr):
            raise EthereumValidationError(f"Invalid address: {self.address}")


# ============================================================
# Client
# ============================================================

class EthereumClient:

    BASE_URL = "https://api.etherscan.io/api"

    def __init__(self, config: EthereumAPIConfig):
        config.validate()

        self.config = config
        self.session = self._create_session()
        self.rate_limiter = RateLimiter(config.rate_limit_per_sec)

    # --------------------------------------------------------
    # Context manager support
    # --------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        self.session.close()

    # --------------------------------------------------------
    # Session
    # --------------------------------------------------------

    def _create_session(self) -> Session:
        s = requests.Session()

        retry = Retry(
            total=self.config.retries,
            backoff_factor=self.config.backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=100,
            pool_maxsize=100,
        )

        s.mount("https://", adapter)
        s.headers.update({"User-Agent": "eth-client/9.0"})

        if self.config.proxies:
            s.proxies.update(self.config.proxies)

        return s

    # --------------------------------------------------------
    # Core request
    # --------------------------------------------------------

    def _request(self, params: Dict[str, str]) -> Any:

        params["apikey"] = self.config.api_key

        for attempt in range(self.config.retries):

            self.rate_limiter.wait()

            try:
                response = self.session.get(
                    self.BASE_URL,
                    params=params,
                    timeout=self.config.timeout,
                )

                response.raise_for_status()
                data: EtherscanResponse = response.json()

            except RequestException as e:
                logger.warning(f"Network error: {e}")
                self._sleep(attempt)
                continue

            result = self._handle_response(data)

            if result is not None:
                return result

            self._sleep(attempt)

        raise EthereumAPIError("Max retries exceeded")

    def _handle_response(self, data: EtherscanResponse):

        if not isinstance(data, dict):
            raise EthereumAPIError("Invalid response format")

        status = data.get("status")
        message = str(data.get("message", "")).lower()
        result = data.get("result")

        if status == "1":
            return result

        text = str(result).lower()

        if "rate limit" in text or "rate limit" in message:
            logger.warning("Rate limit hit")
            return None

        if result == "No transactions found":
            return []

        raise EthereumAPIError(f"{message} | {result}")

    def _sleep(self, attempt: int):
        delay = min(
            self.config.backoff_factor * (2 ** attempt)
            + random.uniform(0, 0.3),
            self.config.max_backoff,
        )
        time.sleep(delay)

    # --------------------------------------------------------
    # Utils
    # --------------------------------------------------------

    @staticmethod
    def wei_to_eth(value: str | int, decimals: int = 18) -> Decimal:
        return Decimal(value) / (Decimal(10) ** decimals)

    # --------------------------------------------------------
    # API methods
    # --------------------------------------------------------

    def get_balance(self) -> Decimal:
        wei = self._request({
            "module": "account",
            "action": "balance",
            "address": self.config.normalized_address(),
            "tag": "latest",
        })

        return self.wei_to_eth(wei)

    def get_transactions_page(self, page: int, offset: int) -> List[Dict[str, Any]]:
        return self._request({
            "module": "account",
            "action": "txlist",
            "address": self.config.normalized_address(),
            "page": str(page),
            "offset": str(offset),
            "sort": "asc",
        })

    def get_all_transactions(
        self,
        offset: int = 1000,
        max_pages: Optional[int] = None,
    ) -> Generator[Dict[str, Any], None, None]:

        page = 1

        while True:
            if max_pages and page > max_pages:
                break

            txs = self.get_transactions_page(page, offset)

            if not txs:
                break

            logger.info(f"Fetched page {page} ({len(txs)} txs)")

            for tx in txs:
                yield tx

            if len(txs) < offset:
                break  # last page

            page += 1

    def get_token_balance(
        self,
        contract: str,
        decimals: int = 18,
    ) -> Decimal:

        contract = contract.lower().strip()

        if not re.fullmatch(r"0x[a-f0-9]{40}", contract):
            raise EthereumValidationError("Invalid contract")

        wei = self._request({
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract,
            "address": self.config.normalized_address(),
            "tag": "latest",
        })

        return self.wei_to_eth(wei, decimals)


# ============================================================
# Entry
# ============================================================

def main():

    config = EthereumAPIConfig(
        api_key=os.getenv("ETHERSCAN_API_KEY", ""),
        address=os.getenv("ETHEREUM_ADDRESS", ""),
        proxies=None,
    )

    with EthereumClient(config) as client:

        balance = client.get_balance()
        logger.info(f"Balance: {balance} ETH")

        for i, tx in enumerate(client.get_all_transactions(max_pages=3)):
            if i < 3:
                logger.debug(tx)

        contract = os.getenv("TOKEN_CONTRACT_ADDRESS")

        if contract:
            token_balance = client.get_token_balance(contract)
            logger.info(f"Token balance: {token_balance}")


if __name__ == "__main__":
    main()
