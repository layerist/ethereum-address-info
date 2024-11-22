import requests
import logging
from typing import Dict, List, Optional, Any
from requests.exceptions import Timeout, HTTPError, RequestException

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class EthereumAPIError(Exception):
    """Custom exception for Ethereum API errors."""
    pass

class EthereumAddressInfo:
    BASE_URL = "https://api.etherscan.io/api"

    def __init__(self, api_key: str, address: str, timeout: int = 10, retries: int = 3, log_level: int = logging.INFO) -> None:
        self.api_key = api_key
        self.address = address
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()  # Session reuse for better performance
        logger.setLevel(log_level)

    def __enter__(self) -> "EthereumAddressInfo":
        """Enter context for session management."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context and close session."""
        self.session.close()

    def _make_request(self, params: Dict[str, str]) -> Any:
        """Internal method to make API requests with retry mechanism."""
        params["apikey"] = self.api_key
        for attempt in range(self.retries):
            try:
                logger.debug(f"Attempt {attempt + 1}: Request parameters - {params}")
                response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "1":
                    return data["result"]
                else:
                    error_message = data.get("message", "Unknown error occurred")
                    logger.error(f"API Error: {error_message}")
                    raise EthereumAPIError(f"API Error: {error_message} | Params: {params}")

            except (HTTPError, Timeout) as e:
                logger.warning(f"{e} (Attempt {attempt + 1}/{self.retries}), retrying...")
            except RequestException as e:
                logger.error(f"Request Exception: {e}")
                raise EthereumAPIError(f"Request Exception: {e}")
            except ValueError as e:
                logger.error(f"JSON Decoding failed: {e}")
                raise EthereumAPIError(f"JSON Decoding failed: {e}")
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                raise EthereumAPIError(f"Unexpected error: {e}")
        
        raise EthereumAPIError("Max retries exceeded.")

    def get_balance(self) -> float:
        """Retrieve the balance of the Ethereum address in Ether."""
        params = {
            "module": "account",
            "action": "balance",
            "address": self.address,
            "tag": "latest",
        }
        result = self._make_request(params)
        balance = int(result) / 1e18  # Convert Wei to Ether
        logger.info(f"Balance retrieved: {balance:.18f} ETH")
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
        token_balance = int(result) / 1e18  # Convert to token's unit
        logger.info(f"Token balance retrieved: {token_balance:.18f}")
        return token_balance

if __name__ == "__main__":
    # Replace these placeholders with your actual credentials and addresses
    api_key = "YourEtherscanAPIKey"
    address = "YourEthereumAddress"

    with EthereumAddressInfo(api_key, address, log_level=logging.DEBUG) as eth_info:
        try:
            # Retrieve Ether balance
            balance = eth_info.get_balance()
            logger.info(f"Balance: {balance:.18f} ETH")
            
            # Retrieve transaction history
            transactions = eth_info.get_transactions()
            logger.info("Transactions:")
            for tx in transactions[:5]:  # Log only the first 5 transactions for brevity
                logger.debug(tx)
            
            # Retrieve ERC-20 token balance (example contract address)
            contract_address = "YourTokenContractAddress"
            token_balance = eth_info.get_token_balance(contract_address)
            logger.info(f"Token Balance: {token_balance:.18f}")

        except EthereumAPIError as e:
            logger.error(f"Ethereum API error occurred: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
