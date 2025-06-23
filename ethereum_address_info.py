import os
import logging
from time import sleep
from typing import Dict, List, Any, Optional, Union

import requests
from requests.exceptions import Timeout, HTTPError, RequestException
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Logger setup
def setup_logger(name: str = "EthereumAPI", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

logger = setup_logger(level=logging.DEBUG)

class EthereumAPIError(Exception):
    """Custom exception for Ethereum API-related errors."""
    pass

class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"
    MAX_RETRIES = 3
    BACKOFF_FACTOR = 2  # Exponential backoff base in seconds
    MAX_BACKOFF = 10    # Maximum backoff duration

    def __init__(self, api_key: str, address: str, timeout: int = 10, log_level: int = logging.INFO) -> None:
        if not api_key or not address:
            raise ValueError("Both 'api_key' and 'address' must be provided.")

        self.api_key = api_key
        self.address = address
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "EthereumAPIClient/1.0"})
        logger.setLevel(log_level)

    def __enter__(self) -> "EthereumAddressInfo":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
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

                error_msg = data.get("message", "Unknown error")
                raise EthereumAPIError(f"Etherscan API returned error: {error_msg}")

            except (Timeout, HTTPError) as e:
                logger.warning(f"[Attempt {attempt}] Temporary issue: {e}")
                sleep(min(self.BACKOFF_FACTOR ** attempt, self.MAX_BACKOFF))
            except RequestException as e:
                raise EthereumAPIError(f"Request failed: {e}")
            except (ValueError, KeyError) as e:
                raise EthereumAPIError(f"Malformed response: {e}")

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
            "tag": "latest"
        }
        wei_balance = self._make_request(params)
        eth_balance = self._convert_wei_to_eth(wei_balance)
        logger.info(f"ETH Balance: {eth_balance:.18f}")
        return eth_balance

    def get_transactions(self, start_block: int = 0, end_block: int = 99999999) -> List[Dict[str, Any]]:
        params = {
            "module": "account",
            "action": "txlist",
            "address": self.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": "asc"
        }
        transactions = self._make_request(params)
        logger.info(f"Retrieved {len(transactions)} transactions.")
        return transactions

    def get_token_balance(self, contract_address: str, decimals: int = 18) -> Optional[float]:
        if not contract_address:
            logger.warning("Missing contract address for token balance.")
            return None

        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.address,
            "tag": "latest"
        }
        token_balance_wei = self._make_request(params)
        token_balance = self._convert_wei_to_eth(token_balance_wei, decimals)
        logger.info(f"Token Balance [{contract_address}]: {token_balance:.6f}")
        return token_balance


def main() -> None:
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS", "")
    token_decimals = int(os.getenv("TOKEN_DECIMALS", "18"))

    if not api_key or not address:
        logger.error("Missing required environment variables: ETHERSCAN_API_KEY or ETHEREUM_ADDRESS")
        return

    try:
        with EthereumAddressInfo(api_key, address) as eth:
            eth.get_balance()

            txs = eth.get_transactions()
            logger.info("Preview of first 5 transactions:")
            for tx in txs[:5]:
                logger.debug(tx)

            if contract_address:
                eth.get_token_balance(contract_address, token_decimals)

    except EthereumAPIError as e:
        logger.error(f"Ethereum API error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
