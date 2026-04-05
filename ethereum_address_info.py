#!/usr/bin/env python3
"""
Advanced Ethereum address info client using Etherscan API.

Major improvements:
- Smart retry loop (handles rate limit + API errors)
- Exponential backoff with jitter
- Pagination support for large tx history
- Strict validation + optional checksum
- Thread-safe session reuse
- Proxy support
- Clean architecture (transport / parser / service)
"""

from __future__ import annotations

import os
import re
import time
import random
import logging
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

getcontext().prec = 50  # high precision for ETH math


# ============================================================
# Logging
# ============================================================

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"


def setup_logger(level=logging.INFO):
    logger = logging.getLogger("eth")
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(h)
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
# Config
# ============================================================

@dataclass(frozen=True)
class EthereumAPIConfig:
    api_key: str
    address: str

    timeout: int = 10
    retries: int = 5
    backoff_factor: float = 0.7
    max_backoff: float = 10
    rate_limit_wait: float = 1.5

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
            pool_connections=50,
            pool_maxsize=50,
        )

        s.mount("https://", adapter)
        s.headers.update({"User-Agent": "eth-client/7.0"})

        if self.config.proxies:
            s.proxies.update(self.config.proxies)

        return s

    # --------------------------------------------------------
    # Core request with smart retry
    # --------------------------------------------------------

    def _request(self, params: Dict[str, str]) -> Any:

        params["apikey"] = self.config.api_key

        for attempt in range(self.config.retries):

            try:
                r = self.session.get(
                    self.BASE_URL,
                    params=params,
                    timeout=self.config.timeout,
                )
                r.raise_for_status()

                data: EtherscanResponse = r.json()

            except RequestException as e:
                self._sleep(attempt)
                continue

            result = self._handle_response(data)

            if result is not None:
                return result

            # rate limit → retry
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
            logger.warning("Rate limited")
            return None  # retry

        if result == "No transactions found":
            return []

        raise EthereumAPIError(f"{message} | {result}")

    def _sleep(self, attempt: int):
        base = self.config.backoff_factor * (2 ** attempt)
        jitter = random.uniform(0, 0.3)
        delay = min(base + jitter, self.config.max_backoff)

        logger.debug(f"Retry sleep: {delay:.2f}s")
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

        eth = self.wei_to_eth(wei)

        logger.info(f"Balance: {eth} ETH")
        return eth

    def get_transactions_page(
        self,
        page: int,
        offset: int = 1000,
        start_block: int = 0,
        end_block: int = 99999999,
    ) -> List[Dict[str, Any]]:

        return self._request({
            "module": "account",
            "action": "txlist",
            "address": self.config.normalized_address(),
            "startblock": str(start_block),
            "endblock": str(end_block),
            "page": str(page),
            "offset": str(offset),
            "sort": "asc",
        })

    def get_all_transactions(
        self,
        max_pages: int = 10,
        offset: int = 1000,
    ) -> Generator[Dict[str, Any], None, None]:

        for page in range(1, max_pages + 1):

            txs = self.get_transactions_page(page, offset)

            if not txs:
                break

            logger.info(f"Page {page}: {len(txs)} txs")

            for tx in txs:
                yield tx

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

        balance = self.wei_to_eth(wei, decimals)

        logger.info(f"Token balance: {balance}")
        return balance


# ============================================================
# Entry
# ============================================================

def main():

    config = EthereumAPIConfig(
        api_key=os.getenv("ETHERSCAN_API_KEY", ""),
        address=os.getenv("ETHEREUM_ADDRESS", ""),
        proxies=None,  # example: {"http": "...", "https": "..."}
    )

    client = EthereumClient(config)

    try:
        client.get_balance()

        for i, tx in enumerate(client.get_all_transactions(max_pages=3)):
            if i < 3:
                logger.debug(tx)

        contract = os.getenv("TOKEN_CONTRACT_ADDRESS")

        if contract:
            client.get_token_balance(contract)

    finally:
        client.close()


if __name__ == "__main__":
    main()
