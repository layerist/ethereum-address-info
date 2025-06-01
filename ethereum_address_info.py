import os
import logging
from time import sleep
from typing import Dict, List, Any, Optional, Union

import requests
from requests.exceptions import Timeout, HTTPError, RequestException
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def setup_logger(name: str = "EthereumAPI", level: int = logging.INFO) -> logging.Logger:
    """Set up and return a logger instance."""
    logger = logging.getLogger(name)
    if not logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

logger = setup_logger(level=logging.DEBUG)


class EthereumAPIError(Exception):
    """Custom exception for Ethereum API-related errors."""


class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"
    MAX_RETRIES = 3
    RETRY_BACKOFF = 2  # seconds

    def __init__(
        self,
        api_key: str,
        address: str,
        timeout: int = 10,
        log_level: int = logging.INFO
    ) -> None:
        """
        Initialize the EthereumAddressInfo client.
        
        :param api_key: Etherscan API key.
        :param address: Ethereum wallet address.
        :param timeout: Request timeout in seconds.
        :param log_level: Logging level.
        """
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
        """
        Make a request to the Etherscan API with retry logic.
        
        :param params: Dictionary of request parameters.
        :return: API result as string or list of dictionaries.
        :raises EthereumAPIError: If all retries fail or response is invalid.
        """
        params["apikey"] = self.api_key

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "1":
                    return data["result"]

                message = data.get("message", "Unknown error")
                raise EthereumAPIError(f"Etherscan API returned failure status: {message}")

            except (HTTPError, Timeout) as e:
                logger.warning(f"Attempt {attempt}/{self.MAX_RETRIES} failed: {e}")
                sleep(min(self.RETRY_BACKOFF ** attempt, 10))
            except RequestException as e:
                raise EthereumAPIError(f"Request exception: {e}")
            except (ValueError, KeyError) as e:
                raise EthereumAPIError(f"Invalid API response format: {e}")

        raise EthereumAPIError("Exceeded maximum retry attempts.")

    @staticmethod
    def _convert_wei_to_eth(wei: Union[str, int], decimals: int = 18) -> float:
        """Convert Wei to Ether with specified decimal precision."""
        try:
            return int(wei) / 10 ** decimals
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to convert Wei to ETH: {e}")
            return 0.0

    def get_balance(self) -> float:
        """Retrieve the ETH balance of the address."""
        params = {
            "module": "account",
            "action": "balance",
            "address": self.address,
            "tag": "latest"
        }
        balance_wei = self._make_request(params)
        balance_eth = self._convert_wei_to_eth(balance_wei)
        logger.info(f"ETH Balance for {self.address}: {balance_eth:.18f}")
        return balance_eth

    def get_transactions(self, start_block: int = 0, end_block: int = 99999999) -> List[Dict[str, Any]]:
        """Retrieve normal transactions for the address."""
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

    def get_token_balance(self, contract_address: str, decimals: int = 18) -> Optional[float]:
        """Retrieve the ERC-20 token balance."""
        if not contract_address:
            logger.warning("No contract address provided for token balance check.")
            return None

        params = {
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": self.address,
            "tag": "latest"
        }

        balance_wei = self._make_request(params)
        balance = self._convert_wei_to_eth(balance_wei, decimals)
        logger.info(f"Token Balance for {contract_address}: {balance:.6f}")
        return balance


def main() -> None:
    """Main function to run the Ethereum address info checks."""
    api_key = os.getenv("ETHERSCAN_API_KEY")
    address = os.getenv("ETHEREUM_ADDRESS")
    contract_address = os.getenv("TOKEN_CONTRACT_ADDRESS", "")
    token_decimals = int(os.getenv("TOKEN_DECIMALS", "18"))

    if not api_key or not address:
        logger.error("Missing required environment variables: ETHERSCAN_API_KEY or ETHEREUM_ADDRESS.")
        return

    try:
        with EthereumAddressInfo(api_key, address) as eth_info:
            eth_info.get_balance()

            transactions = eth_info.get_transactions()
            logger.info("First 5 transactions:")
            for tx in transactions[:5]:
                logger.debug(tx)

            if contract_address:
                eth_info.get_token_balance(contract_address, token_decimals)

    except EthereumAPIError as e:
        logger.error(f"Ethereum API error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
