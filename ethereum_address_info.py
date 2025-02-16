import os
import requests
import logging
from time import sleep
from typing import Dict, List, Any
from requests.exceptions import Timeout, HTTPError, RequestException

# Logger setup
def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Sets up and returns a logger."""
    logger = logging.getLogger(name)
    if not logger.hasHandlers():
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

logger = setup_logger(__name__)

class EthereumAPIError(Exception):
    """Custom exception for Ethereum API errors."""
    pass

class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"
    MAX_RETRIES = 3
    RETRY_BACKOFF = 2  # Exponential backoff base time in seconds

    def __init__(self, api_key: str, address: str, timeout: int = 10, log_level: int = logging.INFO) -> None:
        """
        Initializes the EthereumAddressInfo class.
        """
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

    def _make_request(self, params: Dict[str, str]) -> Any:
        """
        Makes a request to the Ethereum API with retries.
        """
        params["apikey"] = self.api_key
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if data.get("status") == "1":
                    return data["result"]
                raise EthereumAPIError(f"API Error: {data.get('message', 'Unknown error')}")
            except (HTTPError, Timeout) as e:
                logger.warning(f"{e} (Attempt {attempt}/{self.MAX_RETRIES}), retrying...")
                sleep(self.RETRY_BACKOFF ** (attempt - 1))
            except RequestException as e:
                raise EthereumAPIError(f"Request failed: {e}")
            except ValueError as e:
                raise EthereumAPIError(f"Invalid JSON response: {e}")
        raise EthereumAPIError("Max retries exceeded.")

    @staticmethod
    def _convert_wei_to_eth(wei: str) -> float:
        return int(wei) / 1e18

    def get_balance(self) -> float:
        """Retrieve the Ether balance."""
        params = {"module": "account", "action": "balance", "address": self.address, "tag": "latest"}
        balance = self._convert_wei_to_eth(self._make_request(params))
        logger.info(f"Balance: {balance:.18f} ETH")
        return balance

    def get_transactions(self, start_block: int = 0, end_block: int = 99999999) -> List[Dict[str, Any]]:
        """Retrieve transaction history."""
        params = {"module": "account", "action": "txlist", "address": self.address, "startblock": str(start_block), "endblock": str(end_block), "sort": "asc"}
        transactions = self._make_request(params)
        logger.info(f"Retrieved {len(transactions)} transactions.")
        return transactions

    def get_token_balance(self, contract_address: str) -> float:
        """Retrieve ERC-20 token balance."""
        params = {"module": "account", "action": "tokenbalance", "contractaddress": contract_address, "address": self.address, "tag": "latest"}
        token_balance = self._convert_wei_to_eth(self._make_request(params))
        logger.info(f"Token Balance: {token_balance:.18f}")
        return token_balance

if __name__ == "__main__":
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")

    if not api_key or not address:
        logger.error("Missing required environment variables: ETHERSCAN_API_KEY or ETHEREUM_ADDRESS.")
        exit(1)

    with EthereumAddressInfo(api_key, address, log_level=logging.DEBUG) as eth_info:
        try:
            logger.info(f"Ether Balance: {eth_info.get_balance():.18f} ETH")
            transactions = eth_info.get_transactions()
            logger.info(f"Displaying first 5 transactions:")
            for tx in transactions[:5]:
                logger.debug(tx)
            contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS", "")
            if contract_address:
                logger.info(f"ERC-20 Token Balance: {eth_info.get_token_balance(contract_address):.18f}")
        except EthereumAPIError as e:
            logger.error(f"Ethereum API Error: {e}")
        except Exception as e:
            logger.error(f"Unexpected Error: {e}")
