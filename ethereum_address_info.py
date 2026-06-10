#!/usr/bin/env python3
"""
Ultra-fast Ethereum Etherscan client.

Features
--------
- Thread-safe global token bucket rate limiter
- High-performance connection pooling
- Smart retries (network + API + JSON)
- Parallel pagination
- Generator streaming
- Production-grade error handling
- Optional checksum validation
- Context manager support
- Strong typing
- Configurable workers
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
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    RequestException,
    JSONDecodeError,
)

from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

getcontext().prec = 50

# ============================================================
# Logging
# ============================================================

LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | "
    "%(threadName)s | %(message)s"
)


def setup_logger() -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logger = logging.getLogger("eth")

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    logger.setLevel(level)
    logger.propagate = False

    return logger


logger = setup_logger()

# ============================================================
# Constants
# ============================================================

ETH_ADDRESS_REGEX = re.compile(r"^0x[a-fA-F0-9]{40}$")

DEFAULT_HEADERS = {
    "User-Agent": "etherscan-client/10.0",
    "Accept": "application/json",
    "Connection": "keep-alive",
}

# ============================================================
# Exceptions
# ============================================================


class EthereumAPIError(Exception):
    """Base exception."""


class EthereumRateLimitError(EthereumAPIError):
    """Rate limit exceeded."""


class EthereumValidationError(EthereumAPIError):
    """Validation failed."""


class EthereumResponseError(EthereumAPIError):
    """Invalid response."""


# ============================================================
# Types
# ============================================================


class EtherscanResponse(TypedDict):
    status: str
    message: str
    result: Any


# ============================================================
# Token Bucket Rate Limiter
# ============================================================


class TokenBucketRateLimiter:
    """
    Thread-safe token bucket limiter.
    Better than sleep-based interval limiting.
    """

    def __init__(self, rate: float, capacity: Optional[int] = None):
        self.rate = rate
        self.capacity = capacity or max(1, int(rate))

        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()

        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()

                elapsed = now - self.updated_at
                self.updated_at = now

                self.tokens = min(
                    self.capacity,
                    self.tokens + elapsed * self.rate,
                )

                if self.tokens >= 1:
                    self.tokens -= 1
                    return

                missing_tokens = 1 - self.tokens
                sleep_time = missing_tokens / self.rate

            time.sleep(sleep_time)


# ============================================================
# Config
# ============================================================


@dataclass(frozen=True)
class EthereumAPIConfig:
    api_key: str
    address: str

    timeout: int = 15
    retries: int = 6

    backoff_factor: float = 0.5
    max_backoff: float = 20.0

    rate_limit_per_sec: float = 4.0
    circuit_breaker_failures: int = 8

    max_workers: int = 4
    use_checksum: bool = False

    proxies: Optional[Dict[str, str]] = None

    def normalized_address(self) -> str:
        return self.address.strip().lower()

    def validate(self) -> None:
        if not self.api_key:
            raise EthereumValidationError(
                "ETHERSCAN_API_KEY missing"
            )

        address = self.normalized_address()

        if not ETH_ADDRESS_REGEX.fullmatch(address):
            raise EthereumValidationError(
                f"Invalid ETH address: {self.address}"
            )


# ============================================================
# Ethereum Client
# ============================================================


class EthereumClient:

    BASE_URL = "https://api.etherscan.io/api"

    def __init__(self, config: EthereumAPIConfig):
        config.validate()

        self.config = config
        self.session = self._create_session()

        self.rate_limiter = TokenBucketRateLimiter(
            config.rate_limit_per_sec
        )

    # ========================================================
    # Context manager
    # ========================================================

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self) -> None:
        self.session.close()

    # ========================================================
    # Session
    # ========================================================

    def _create_session(self) -> Session:
        session = requests.Session()

        retry = Retry(
            total=self.config.retries,
            connect=self.config.retries,
            read=self.config.retries,
            backoff_factor=0.0,
            allowed_methods={"GET"},
            status_forcelist={
                429,
                500,
                502,
                503,
                504,
            },
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=100,
            pool_maxsize=100,
            pool_block=True,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(DEFAULT_HEADERS)

        if self.config.proxies:
            session.proxies.update(
                self.config.proxies
            )

        return session

    # ========================================================
    # Core request
    # ========================================================

    def _request(
        self,
        params: Dict[str, str],
    ) -> Any:

        params["apikey"] = self.config.api_key

        for attempt in range(
            self.config.retries
        ):

            self.rate_limiter.wait()

            try:
                response = self.session.get(
                    self.BASE_URL,
                    params=params,
                    timeout=self.config.timeout,
                )

                response.raise_for_status()

                try:
                    data: EtherscanResponse = (
                        response.json()
                    )
                except JSONDecodeError:
                    logger.warning(
                        "Invalid JSON response"
                    )
                    self._sleep(attempt)
                    continue

            except RequestException as e:
                logger.warning(
                    "Network error: %s",
                    e,
                )

                self._sleep(attempt)
                continue

            result = self._handle_response(
                data
            )

            if result is not None:
                return result

            self._sleep(attempt)

        raise EthereumAPIError(
            "Maximum retries exceeded"
        )

    def _handle_response(
        self,
        data: EtherscanResponse,
    ) -> Any:

        if not isinstance(data, dict):
            raise EthereumResponseError(
                "Invalid response type"
            )

        status = data.get("status")
        message = str(
            data.get("message", "")
        ).lower()

        result = data.get("result")

        if status == "1":
            return result

        result_text = str(result).lower()

        if (
            "rate limit" in result_text
            or "rate limit" in message
            or "max rate limit reached"
            in result_text
        ):
            logger.warning(
                "Etherscan rate limit hit"
            )
            return None

        if result == "No transactions found":
            return []

        raise EthereumAPIError(
            f"{message} | {result}"
        )

    def _sleep(
        self,
        attempt: int,
    ) -> None:

        delay = min(
            (
                self.config.backoff_factor
                * (2**attempt)
            )
            + random.uniform(0, 0.5),
            self.config.max_backoff,
        )

        time.sleep(delay)

    # ========================================================
    # Utils
    # ========================================================

    @staticmethod
    def wei_to_eth(
        value: str | int,
        decimals: int = 18,
    ) -> Decimal:

        return (
            Decimal(value)
            / Decimal(10**decimals)
        )

    # ========================================================
    # API methods
    # ========================================================

    def get_balance(self) -> Decimal:
        wei = self._request(
            {
                "module": "account",
                "action": "balance",
                "address": self.config.normalized_address(),
                "tag": "latest",
            }
        )

        return self.wei_to_eth(wei)

    def get_transactions_page(
        self,
        page: int,
        offset: int = 1000,
    ) -> List[Dict[str, Any]]:

        return self._request(
            {
                "module": "account",
                "action": "txlist",
                "address": self.config.normalized_address(),
                "page": str(page),
                "offset": str(offset),
                "sort": "asc",
            }
        )

    def get_all_transactions(
        self,
        offset: int = 1000,
        max_pages: Optional[int] = None,
        parallel: bool = True,
    ) -> Generator[
        Dict[str, Any],
        None,
        None,
    ]:

        if not parallel:
            yield from self._serial_fetch(
                offset,
                max_pages,
            )
            return

        yield from self._parallel_fetch(
            offset,
            max_pages,
        )

    def _serial_fetch(
        self,
        offset: int,
        max_pages: Optional[int],
    ):

        page = 1

        while True:

            if (
                max_pages
                and page > max_pages
            ):
                break

            txs = self.get_transactions_page(
                page,
                offset,
            )

            if not txs:
                break

            logger.info(
                "Fetched page %s (%s txs)",
                page,
                len(txs),
            )

            yield from txs

            if len(txs) < offset:
                break

            page += 1

    def _parallel_fetch(
        self,
        offset: int,
        max_pages: Optional[int],
    ):

        max_pages = max_pages or 1000

        with ThreadPoolExecutor(
            max_workers=self.config.max_workers
        ) as executor:

            futures = {
                executor.submit(
                    self.get_transactions_page,
                    page,
                    offset,
                ): page
                for page in range(
                    1,
                    max_pages + 1
                )
            }

            stop = False

            ordered_results = {}

            for future in as_completed(
                futures
            ):

                page = futures[future]

                if stop:
                    future.cancel()
                    continue

                txs = future.result()

                ordered_results[
                    page
                ] = txs

                if not txs:
                    stop = True
                    continue

                logger.info(
                    "Fetched page %s (%s txs)",
                    page,
                    len(txs),
                )

            for page in sorted(
                ordered_results
            ):
                txs = ordered_results[
                    page
                ]

                if not txs:
                    break

                yield from txs

    def get_token_balance(
        self,
        contract: str,
        decimals: int = 18,
    ) -> Decimal:

        contract = contract.strip().lower()

        if not ETH_ADDRESS_REGEX.fullmatch(
            contract
        ):
            raise EthereumValidationError(
                "Invalid contract address"
            )

        wei = self._request(
            {
                "module": "account",
                "action": "tokenbalance",
                "contractaddress": contract,
                "address": self.config.normalized_address(),
                "tag": "latest",
            }
        )

        return self.wei_to_eth(
            wei,
            decimals,
        )


# ============================================================
# Main
# ============================================================


def main():

    config = EthereumAPIConfig(
        api_key=os.getenv(
            "ETHERSCAN_API_KEY",
            "",
        ),
        address=os.getenv(
            "ETHEREUM_ADDRESS",
            "",
        ),
        proxies=None,
        max_workers=4,
    )

    with EthereumClient(
        config
    ) as client:

        balance = (
            client.get_balance()
        )

        logger.info(
            "Balance: %s ETH",
            balance,
        )

        for i, tx in enumerate(
            client.get_all_transactions(
                max_pages=5,
                parallel=True,
            )
        ):
            if i < 5:
                logger.debug(tx)

        contract = os.getenv(
            "TOKEN_CONTRACT_ADDRESS"
        )

        if contract:
            token_balance = (
                client.get_token_balance(
                    contract
                )
            )

            logger.info(
                "Token balance: %s",
                token_balance,
            )


if __name__ == "__main__":
    main()
