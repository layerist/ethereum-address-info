import os
import logging
from time import sleep
from typing import Dict, List, Any, Optional

import requests
from requests.exceptions import Timeout, HTTPError, RequestException
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Logger setup
def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure and return a logger."""
    logger = logging.getLogger(name)
    if not logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

logger = setup_logger(__name__)

class EthereumAPIError(Exception):
    """Raised when an Ethereum API call fails after retries."""
    pass

class EthereumAddressInfo:
    BASE_URL: str = "https://api.etherscan.io/api"
    MAX_RETRIES: int = 3
    RETRY_BACKOFF: int = 2  # Base backoff (seconds)

    def __init__(self, api_key: str, address: str, timeout: int = 10, log_level: int = logging.INFO) -> None:
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
        """Makes a request to the Etherscan API with retry logic."""
        params["apikey"] = self.api_key

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if data.get("status") == "1":
                    return data["result"]
                raise EthereumAPIError(f"Etherscan API error: {data.get('message', 'Unknown error')}")
            except (HTTPError, Timeout) as e:
                logger.warning(f"{e} (Attempt {attempt}/{self.MAX_RETRIES}), retrying...")
                sleep(min(self.RETRY_BACKOFF ** attempt, 10))
            except RequestException as e:
                raise EthereumAPIError(f"Request failed: {e}")
            except (ValueError, KeyError) as e:
                raise EthereumAPIError(f"Invalid response format: {e}")

        raise EthereumAPIError("Exceeded maximum number of retries.")

    @staticmethod
    def _convert_wei_to_eth(wei: str) -> float:
        """Converts Wei to ETH."""
        try:
            return int(wei) / 1e18
        except (ValueError, TypeError):
            logger.warning("Failed to convert Wei to ETH.")
            return 0.0

    def get_balance(self) -> float:
        """Retrieve Ether balance."""
        params = {
            "module": "account",
            "action": "balance",
            "address": self.address,
            "tag": "latest"
        }
        balance_wei = self._make_request(params)
        balance_eth = self._convert_wei_to_eth(balance_wei)
        logger.info(f"ETH Balance: {balance_eth:.18f}")
        return balance_eth

    def get_transactions(self, start_block: int = 0, end_block: int = 99999999) -> List[Dict[str, Any]]:
        """Retrieve normal transactions."""
        params = {
            "module": "account",
            "action": "txlist",
            "address": self.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": "asc"
        }
        transactions = self._make_request(params)
        logger.info(f"Fetched {len(transactions)} transactions.")
        return transactions

    def get_token_balance(self, contract_address: str) -> Optional[float]:
        """Retrieve ERC-20 token balance by contract address."""
        if not contract_address:
            logger.warning("No contract address provided.")
            return None
        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.address,
            "tag": "latest"
        }
        balance_wei = self._make_request(params)
        balance_eth = self._convert_wei_to_eth(balance_wei)
        logger.info(f"Token Balance: {balance_eth:.18f}")
        return balance_eth

def main():
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS", "")

    if not api_key or not address:
        logger.error("Missing required environment variables: ETHERSCAN_API_KEY or ETHEREUM_ADDRESS.")
        return

    try:
        with EthereumAddressInfo(api_key, address, log_level=logging.DEBUG) as eth:
            eth.get_balance()
            transactions = eth.get_transactions()
            logger.info("First 5 transactions:")
            for tx in transactions[:5]:
                logger.debug(tx)

            if contract_address:
                eth.get_token_balance(contract_address)

    except EthereumAPIError as e:
        logger.error(f"Ethereum API Error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)

if __name__ == "__main__":
    main()
