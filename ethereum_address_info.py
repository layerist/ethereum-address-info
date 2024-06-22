import requests

class EthereumAddressInfo:
    def __init__(self, api_key, address):
        self.api_key = api_key
        self.address = address
        self.base_url = 'https://api.etherscan.io/api'

    def get_balance(self):
        endpoint = f"{self.base_url}?module=account&action=balance&address={self.address}&tag=latest&apikey={self.api_key}"
        response = requests.get(endpoint)
        data = response.json()
        if data['status'] == '1':
            return int(data['result']) / 1e18  # Convert Wei to Ether
        else:
            return f"Error: {data['message']}"

    def get_transactions(self):
        endpoint = f"{self.base_url}?module=account&action=txlist&address={self.address}&startblock=0&endblock=99999999&sort=asc&apikey={self.api_key}"
        response = requests.get(endpoint)
        data = response.json()
        if data['status'] == '1':
            return data['result']
        else:
            return f"Error: {data['message']}"

    def get_token_balance(self, contract_address):
        endpoint = f"{self.base_url}?module=account&action=tokenbalance&contractaddress={contract_address}&address={self.address}&tag=latest&apikey={self.api_key}"
        response = requests.get(endpoint)
        data = response.json()
        if data['status'] == '1':
            return int(data['result']) / 1e18  # Convert to token's unit
        else:
            return f"Error: {data['message']}"

# Example usage
if __name__ == "__main__":
    api_key = 'YourEtherscanAPIKey'
    address = 'YourEthereumAddress'
    
    eth_info = EthereumAddressInfo(api_key, address)
    
    balance = eth_info.get_balance()
    transactions = eth_info.get_transactions()
    
    print(f"Balance: {balance} ETH")
    print(f"Transactions: {transactions}")
