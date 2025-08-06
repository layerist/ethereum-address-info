import os
import logging
import time
from typing import Any, Dict, List, Optional, Union

import requests
from requests import Session
from requests.exceptions import Timeout, HTTPError, RequestException
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Logger setup
def setup_logger(name: str = "EthereumAPI", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

logger = setup_logger(level=logging.DEBUG)


class EthereumAPIError(Exception):
    """Custom exception for Ethereum API errors."""


class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"
    MAX_RETRIES = 3
    BACKOFF_BASE = 2
    MAX_BACKOFF = 10  # seconds

    def __init__(
        self,
        api_key: str,
        address: str,
        timeout: int = 10,
        log_level: int = logging.INFO
    ) -> None:
        if not api_key:
            raise ValueError("Etherscan API key is required.")
        if not address:
            raise ValueError("Ethereum address is required.")

        self.api_key = api_key
        self.address = address
        self.timeout = timeout
        self.session: Session = requests.Session()
        self.session.headers.update({"User-Agent": "EthereumAPIClient/1.0"})
        logger.setLevel(log_level)

    def __enter__(self) -> "EthereumAddressInfo":
        return self

    def __exit__(self, *_: Any) -> None:
        self.session.close()

    def _make_request(self, params: Dict[str, str]) -> Union[str, List[Dict[str, Any]]]:
        params["apikey"] = self.api_key

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "1" and "result" in data:
                    return data["result"]

                if data.get("message") == "NOTOK" and "Max rate limit reached" in str(data.get("result", "")):
                    raise EthereumAPIError("Rate limit reached. Try again later.")

                raise EthereumAPIError(f"API returned error: {data.get('message', 'Unknown')}")

            except (Timeout, HTTPError) as e:
                logger.warning(f"[Attempt {attempt}] Network error: {e}")
                delay = min(self.BACKOFF_BASE ** attempt, self.MAX_BACKOFF)
                time.sleep(delay)
            except RequestException as e:
                raise EthereumAPIError(f"Request failed: {e}")
            except (ValueError, KeyError) as e:
                raise EthereumAPIError(f"Invalid API response format: {e}")

        raise EthereumAPIError("All retry attempts failed.")

    @staticmethod
    def _convert_wei_to_eth(wei: Union[str, int], decimals: int = 18) -> float:
        try:
            return int(wei) / (10 ** decimals)
        except (ValueError, TypeError) as e:
            logger.warning(f"Conversion error (Wei to ETH): {e}")
            return 0.0

    def get_balance(self) -> float:
        params = {
            "module": "account",
            "action": "balance",
            "address": self.address,
            "tag": "latest",
        }
        wei = self._make_request(params)
        eth = self._convert_wei_to_eth(wei)
        logger.info(f"ETH balance: {eth:.6f}")
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
            "address": self.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": sort,
        }
        transactions = self._make_request(params)
        logger.info(f"Retrieved {len(transactions)} transactions.")
        return transactions

    def get_token_balance(self, contract_address: str, decimals: int = 18) -> Optional[float]:
        if not contract_address:
            logger.warning("Contract address not provided.")
            return None

        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.address,
            "tag": "latest",
        }
        wei = self._make_request(params)
        balance = self._convert_wei_to_eth(wei, decimals)
        logger.info(f"Token balance [{contract_address}]: {balance:.6f}")
        return balance


def main() -> None:
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS", "")
    token_decimals = int(os.getenv("TOKEN_DECIMALS", "18"))

    if not api_key or not address:
        logger.error("Missing required environment variables: ETHERSCAN_API_KEY or ETHEREUM_ADDRESS.")
        return

    try:
        with EthereumAddressInfo(api_key, address) as eth:
            eth.get_balance()

            transactions = eth.get_transactions()
            logger.info("First 5 transactions:")
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
