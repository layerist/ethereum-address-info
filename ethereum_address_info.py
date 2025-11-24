import os
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from requests import Session
from requests.exceptions import Timeout, HTTPError, RequestException
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type
from dotenv import load_dotenv


# ============================================================
# Environment Setup
# ============================================================
load_dotenv()


# ============================================================
# Logger Configuration
# ============================================================
def setup_logger(name: str = "EthereumAPI", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


logger = setup_logger(level=logging.DEBUG)


# ============================================================
# Custom Exceptions
# ============================================================
class EthereumAPIError(Exception):
    """General API exception."""


class EthereumRateLimitError(EthereumAPIError):
    """Rate limit reached."""


# ============================================================
# Configuration Dataclass
# ============================================================
@dataclass
class EthereumAPIConfig:
    api_key: str
    address: str
    timeout: int = 10
    max_retries: int = 3
    backoff_base: float = 2.0
    max_backoff: float = 10.0
    rate_limit_wait: int = 5


# ============================================================
# Ethereum API Client
# ============================================================
class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"

    def __init__(self, config: EthereumAPIConfig) -> None:
        if not config.api_key:
            raise ValueError("Etherscan API key is required.")
        if not config.address:
            raise ValueError("Ethereum address is required.")

        self.config = config
        self.session: Session = requests.Session()
        self.session.headers.update({"User-Agent": "EthereumAPIClient/3.0"})

    def __enter__(self) -> "EthereumAddressInfo":
        return self

    def __exit__(self, *_: Any) -> None:
        self.session.close()

    # ------------------------------------------------------------
    # Core Request Handler With Retry
    # ------------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((Timeout, HTTPError, EthereumAPIError)),
        wait=wait_exponential_jitter(multiplier=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True
    )
    def _make_request(self, params: Dict[str, str]) -> Any:
        params["apikey"] = self.config.api_key

        try:
            logger.debug(f"Requesting: {params}")
            response = self.session.get(self.BASE_URL, params=params, timeout=self.config.timeout)
            response.raise_for_status()
            data = response.json()
        except Timeout:
            logger.warning("Timeout occurred. Retrying...")
            raise
        except HTTPError as e:
            logger.warning(f"HTTP error: {e}. Retrying...")
            raise
        except RequestException as e:
            raise EthereumAPIError(f"Network error: {e}") from e

        return self._handle_api_response(data)

    # ------------------------------------------------------------
    # Specific Etherscan API checks
    # ------------------------------------------------------------
    def _handle_api_response(self, data: Dict[str, Any]) -> Any:
        status = data.get("status")
        message = data.get("message", "")
        result = data.get("result")

        # Etherscan success
        if status == "1":
            return result

        # Rate limit handling
        if "Max rate limit" in str(result) or message == "NOTOK" and result == "Max rate limit reached":
            logger.warning(f"Rate limit reached. Waiting {self.config.rate_limit_wait}s...")
            time.sleep(self.config.rate_limit_wait)
            raise EthereumRateLimitError("Rate limit reached")

        # Empty success
        if status == "0" and result == "No transactions found":
            return []

        raise EthereumAPIError(f"Etherscan API error: {message or result}")

    # ------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------
    @staticmethod
    def _convert_wei_to_eth(wei: Union[str, int], decimals: int = 18) -> float:
        try:
            return int(wei) / (10 ** decimals)
        except (ValueError, TypeError):
            logger.debug(f"Invalid Wei value: {wei}")
            return 0.0

    # ------------------------------------------------------------
    # API Endpoints
    # ------------------------------------------------------------
    def get_balance(self) -> float:
        params = {
            "module": "account",
            "action": "balance",
            "address": self.config.address,
            "tag": "latest",
        }
        wei = self._make_request(params)
        eth = self._convert_wei_to_eth(wei)
        logger.info(f"Balance: {eth:.6f} ETH")
        return eth

    def get_transactions(
        self,
        start_block: int = 0,
        end_block: int = 99999999,
        sort: str = "asc"
    ) -> List[Dict[str, Any]]:
        params = {
            "module": "account",
            "action": "txlist",
            "address": self.config.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": sort,
        }
        txs = self._make_request(params)
        logger.info(f"Transactions: {len(txs)} found")
        return txs

    def get_token_balance(self, contract_address: str, decimals: int = 18) -> Optional[float]:
        if not contract_address:
            logger.warning("No contract address provided for token balance.")
            return None

        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.config.address,
            "tag": "latest",
        }
        wei = self._make_request(params)
        balance = self._convert_wei_to_eth(wei, decimals)
        logger.info(f"Token balance [{contract_address}]: {balance:.6f}")
        return balance


# ============================================================
# Main Runner
# ============================================================
def main() -> None:
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS")
    token_decimals = int(os.getenv("TOKEN_DECIMALS", "18"))

    if not api_key or not address:
        logger.error("ETHERSCAN_API_KEY or ETHEREUM_ADDRESS missing.")
        return

    config = EthereumAPIConfig(api_key=api_key, address=address)

    try:
        with EthereumAddressInfo(config) as eth:
            eth.get_balance()
            txs = eth.get_transactions()

            if txs:
                logger.debug("Sample transactions (first 3):")
                for tx in txs[:3]:
                    logger.debug(tx)

            if contract_address:
                eth.get_token_balance(contract_address, token_decimals)

    except EthereumRateLimitError:
        logger.error("Too many API requests. Try again later.")
    except EthereumAPIError as e:
        logger.error(f"API error: {e}")
    except Exception as e:
        logger.exception(f"Unexpected fatal error: {e}")


if __name__ == "__main__":
    main()
