import os
import logging
import time
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import requests
from requests import Session
from requests.exceptions import Timeout, HTTPError, RequestException
from dotenv import load_dotenv


# ============================================================
# Environment Setup
# ============================================================
load_dotenv()


# ============================================================
# Logger Configuration
# ============================================================
def setup_logger(name: str = "EthereumAPI", level: int = logging.INFO) -> logging.Logger:
    """Configure a console logger with timestamps and contextual info."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


logger = setup_logger(level=logging.DEBUG)


# ============================================================
# Custom Exceptions
# ============================================================
class EthereumAPIError(Exception):
    """Raised when an Etherscan API request fails."""


# ============================================================
# Configuration Dataclass
# ============================================================
@dataclass
class EthereumAPIConfig:
    """Configuration for Ethereum API client."""
    api_key: str
    address: str
    timeout: int = 10
    max_retries: int = 3
    backoff_base: float = 2.0
    max_backoff: float = 10.0
    rate_limit_wait: int = 5  # seconds


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
        self.session.headers.update({"User-Agent": "EthereumAPIClient/2.0"})

    def __enter__(self) -> "EthereumAddressInfo":
        return self

    def __exit__(self, *_: Any) -> None:
        self.session.close()

    # ------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------
    def _make_request(self, params: Dict[str, str]) -> Any:
        """Perform a GET request with retries and exponential backoff."""
        params["apikey"] = self.config.api_key

        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = self.session.get(
                    self.BASE_URL, params=params, timeout=self.config.timeout
                )
                response.raise_for_status()
                data = response.json()

                # Successful response
                if data.get("status") == "1" and "result" in data:
                    return data["result"]

                # Handle known API errors
                message = data.get("message", "")
                if "NOTOK" in message or data.get("status") == "0":
                    result_text = str(data.get("result", "Unknown"))
                    if "Max rate limit" in result_text:
                        logger.warning(
                            f"Rate limit reached (attempt {attempt}/{self.config.max_retries}). "
                            f"Waiting {self.config.rate_limit_wait}s..."
                        )
                        time.sleep(self.config.rate_limit_wait)
                        continue
                    raise EthereumAPIError(f"API error: {result_text}")

                raise EthereumAPIError(f"Unexpected API response: {data}")

            except (Timeout, HTTPError) as e:
                delay = min(
                    self.config.backoff_base ** attempt + random.uniform(0, 1),
                    self.config.max_backoff,
                )
                logger.warning(
                    f"Request error ({type(e).__name__}) on attempt {attempt}: {e}. Retrying in {delay:.2f}s..."
                )
                time.sleep(delay)
            except (RequestException, ValueError, KeyError) as e:
                raise EthereumAPIError(f"Request failed: {e}") from e

        raise EthereumAPIError("All retry attempts failed.")

    @staticmethod
    def _convert_wei_to_eth(wei: Union[str, int], decimals: int = 18) -> float:
        """Convert Wei to ETH or token units."""
        try:
            return int(wei) / (10 ** decimals)
        except (ValueError, TypeError) as e:
            logger.debug(f"Conversion error (Wei to ETH): {e}")
            return 0.0

    # ------------------------------------------------------------
    # Public API Methods
    # ------------------------------------------------------------
    def get_balance(self) -> float:
        """Fetch ETH balance for the configured address."""
        params = {
            "module": "account",
            "action": "balance",
            "address": self.config.address,
            "tag": "latest",
        }
        wei = self._make_request(params)
        eth = self._convert_wei_to_eth(wei)
        logger.info(f"ETH balance for {self.config.address}: {eth:.6f} ETH")
        return eth

    def get_transactions(
        self, start_block: int = 0, end_block: int = 99999999, sort: str = "asc"
    ) -> List[Dict[str, Any]]:
        """Retrieve all normal transactions for the given address."""
        params = {
            "module": "account",
            "action": "txlist",
            "address": self.config.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": sort,
        }
        transactions = self._make_request(params)
        logger.info(f"Retrieved {len(transactions)} transactions for {self.config.address}.")
        return transactions

    def get_token_balance(
        self, contract_address: str, decimals: int = 18
    ) -> Optional[float]:
        """Fetch ERC-20 token balance for the specified contract."""
        if not contract_address:
            logger.warning("Missing contract address for token balance check.")
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
        logger.info(
            f"Token balance for {self.config.address} [contract: {contract_address}]: {balance:.6f}"
        )
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
        logger.error("Missing ETHERSCAN_API_KEY or ETHEREUM_ADDRESS in .env file.")
        return

    config = EthereumAPIConfig(api_key=api_key, address=address)

    try:
        with EthereumAddressInfo(config) as eth:
            eth.get_balance()
            transactions = eth.get_transactions()

            if transactions:
                logger.info("First 5 transactions (debug output):")
                for tx in transactions[:5]:
                    logger.debug(tx)

            if contract_address:
                eth.get_token_balance(contract_address, token_decimals)

    except EthereumAPIError as e:
        logger.error(f"Ethereum API error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
