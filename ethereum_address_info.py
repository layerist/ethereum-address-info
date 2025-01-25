import os
import requests
import logging
from time import sleep
from typing import Dict, List, Any, Optional
from requests.exceptions import Timeout, HTTPError, RequestException

# Custom logger setup
def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Sets up a logger with a specific name and level."""
    logger = logging.getLogger(name)
    if not logger.hasHandlers():  # Prevent duplicate handlers
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
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
    RETRY_BACKOFF = 2  # Seconds

    def __init__(
        self, 
        api_key: str, 
        address: str, 
        timeout: int = 10, 
        log_level: int = logging.INFO
    ) -> None:
        """
        Initializes the EthereumAddressInfo class.

        Args:
            api_key (str): API key for Etherscan.
            address (str): Ethereum address to query.
            timeout (int): Request timeout in seconds.
            log_level (int): Logging level.
        """
        self.api_key = api_key
        self.address = address
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "EthereumAPIClient/1.0"})
        logger.setLevel(log_level)

    def __enter__(self) -> "EthereumAddressInfo":
        """Enable context management for session handling."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close the session when exiting the context."""
        self.session.close()

    def _make_request(self, params: Dict[str, str]) -> Any:
        """
        Makes a request to the Ethereum API with retries.

        Args:
            params (Dict[str, str]): Parameters for the API request.

        Returns:
            Any: Decoded JSON response from the API.

        Raises:
            EthereumAPIError: If the request fails after retries.
        """
        params["apikey"] = self.api_key
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.debug(f"Attempt {attempt}: Requesting with params {params}")
                response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "1":
                    return data["result"]
                else:
                    error_message = data.get("message", "Unknown error occurred")
                    logger.error(f"API Error (status {response.status_code}): {error_message}")
                    raise EthereumAPIError(f"API Error: {error_message} | Params: {params}")

            except (HTTPError, Timeout) as e:
                logger.warning(f"{e} (Attempt {attempt}/{self.MAX_RETRIES}), retrying...")
                sleep(self.RETRY_BACKOFF ** (attempt - 1))
            except RequestException as e:
                logger.error(f"Request Exception: {e}")
                raise EthereumAPIError(f"Request Exception: {e}")
            except ValueError as e:
                logger.error(f"Failed to decode JSON: {e}")
                raise EthereumAPIError(f"Failed to decode JSON: {e}")

        raise EthereumAPIError("Max retries exceeded.")

    @staticmethod
    def _convert_wei_to_eth(wei: str) -> float:
        """Convert Wei to Ether."""
        return int(wei) / 1e18

    def get_balance(self) -> float:
        """Retrieve the Ether balance of the Ethereum address."""
        params = {
            "module": "account",
            "action": "balance",
            "address": self.address,
            "tag": "latest",
        }
        result = self._make_request(params)
        balance = self._convert_wei_to_eth(result)
        logger.info(f"Balance: {balance:.18f} ETH")
        return balance

    def get_transactions(self, start_block: int = 0, end_block: int = 99999999) -> List[Dict[str, Any]]:
        """Retrieve the list of transactions for the Ethereum address."""
        params = {
            "module": "account",
            "action": "txlist",
            "address": self.address,
            "startblock": str(start_block),
            "endblock": str(end_block),
            "sort": "asc",
        }
        transactions = self._make_request(params)
        logger.info(f"Retrieved {len(transactions)} transactions.")
        return transactions

    def get_token_balance(self, contract_address: str) -> float:
        """Retrieve the token balance for a specific ERC-20 token."""
        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.address,
            "tag": "latest",
        }
        result = self._make_request(params)
        token_balance = self._convert_wei_to_eth(result)
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
            balance = eth_info.get_balance()
            logger.info(f"Ether Balance: {balance:.18f} ETH")

            transactions = eth_info.get_transactions()
            logger.info("Transactions:")
            for tx in transactions[:5]:
                logger.debug(tx)

            contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS", "")
            if contract_address:
                token_balance = eth_info.get_token_balance(contract_address)
                logger.info(f"ERC-20 Token Balance: {token_balance:.18f}")

        except EthereumAPIError as e:
            logger.error(f"Ethereum API Error: {e}")
        except Exception as e:
            logger.error(f"Unexpected Error: {e}")
