import requests
import logging
from typing import Dict, List

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EthereumAddressInfo:
    def __init__(self, api_key: str, address: str) -> None:
        self.api_key = api_key
        self.address = address
        self.base_url = 'https://api.etherscan.io/api'
        self.session = requests.Session()

    def _make_request(self, params: Dict[str, str]) -> dict:
        params['apikey'] = self.api_key
        try:
            response = self.session.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()
            if data['status'] == '1':
                return data['result']
            else:
                error_message = f"Error: {data['message']}"
                logger.error(error_message)
                raise ValueError(error_message)
        except requests.exceptions.RequestException as e:
            logger.exception(f"Request failed: {e}")
            raise SystemExit(e)

    def get_balance(self) -> float:
        params = {
            'module': 'account',
            'action': 'balance',
            'address': self.address,
            'tag': 'latest'
        }
        result = self._make_request(params)
        balance = int(result) / 1e18  # Convert Wei to Ether
        logger.info(f"Retrieved balance: {balance} ETH")
        return balance

    def get_transactions(self) -> List[Dict]:
        params = {
            'module': 'account',
            'action': 'txlist',
            'address': self.address,
            'startblock': '0',
            'endblock': '99999999',
            'sort': 'asc'
        }
        transactions = self._make_request(params)
        logger.info(f"Retrieved {len(transactions)} transactions")
        return transactions

    def get_token_balance(self, contract_address: str) -> float:
        params = {
            'module': 'account',
            'action': 'tokenbalance',
            'contractaddress': contract_address,
            'address': self.address,
            'tag': 'latest'
        }
        result = self._make_request(params)
        token_balance = int(result) / 1e18  # Convert to token's unit
        logger.info(f"Retrieved token balance: {token_balance}")
        return token_balance

# Example usage
if __name__ == "__main__":
    api_key = 'YourEtherscanAPIKey'
    address = 'YourEthereumAddress'
    
    eth_info = EthereumAddressInfo(api_key, address)
    
    try:
        balance = eth_info.get_balance()
        logger.info(f"Balance: {balance} ETH")
        
        transactions = eth_info.get_transactions()
        logger.info("Transactions:")
        for tx in transactions:
            logger.info(tx)
        
        # Example for token balance (use your specific contract address)
        contract_address = 'YourTokenContractAddress'
        token_balance = eth_info.get_token_balance(contract_address)
        logger.info(f"Token Balance: {token_balance}")

    except Exception as e:
        logger.exception(f"An error occurred: {e}")
