import requests

class EthereumAddressInfo:
    def __init__(self, api_key, address):
        self.api_key = api_key
        self.address = address
        self.base_url = 'https://api.etherscan.io/api'
        self.session = requests.Session()

    def _make_request(self, params):
        params['apikey'] = self.api_key
        try:
            response = self.session.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()
            if data['status'] == '1':
                return data['result']
            else:
                raise ValueError(f"Error: {data['message']}")
        except requests.exceptions.RequestException as e:
            raise SystemExit(e)
    
    def get_balance(self):
        params = {
            'module': 'account',
            'action': 'balance',
            'address': self.address,
            'tag': 'latest'
        }
        result = self._make_request(params)
        return int(result) / 1e18  # Convert Wei to Ether

    def get_transactions(self):
        params = {
            'module': 'account',
            'action': 'txlist',
            'address': self.address,
            'startblock': 0,
            'endblock': 99999999,
            'sort': 'asc'
        }
        return self._make_request(params)

    def get_token_balance(self, contract_address):
        params = {
            'module': 'account',
            'action': 'tokenbalance',
            'contractaddress': contract_address,
            'address': self.address,
            'tag': 'latest'
        }
        result = self._make_request(params)
        return int(result) / 1e18  # Convert to token's unit

# Example usage
if __name__ == "__main__":
    api_key = 'YourEtherscanAPIKey'
    address = 'YourEthereumAddress'
    
    eth_info = EthereumAddressInfo(api_key, address)
    
    balance = eth_info.get_balance()
    transactions = eth_info.get_transactions()
    
    print(f"Balance: {balance} ETH")
    print("Transactions:")
    for tx in transactions:
        print(tx)
