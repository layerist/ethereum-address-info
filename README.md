# Ethereum Address Info

This Python script retrieves various information about a given Ethereum address using the Etherscan API, including the balance and transaction history.

## Features

- Get the balance of an Ethereum address.
- Get the transaction history of an Ethereum address.
- Get the balance of a specific ERC-20 token for an Ethereum address.

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/yourusername/ethereum-address-info.git
    cd ethereum-address-info
    ```

2. Install the required packages:
    ```bash
    pip install requests
    ```

## Usage

1. Replace `YourEtherscanAPIKey` and `YourEthereumAddress` in `ethereum_address_info.py` with your Etherscan API key and the Ethereum address you want to query.

2. Run the script:
    ```bash
    python ethereum_address_info.py
    ```

## Example

```python
# Example usage
if __name__ == "__main__":
    api_key = 'YourEtherscanAPIKey'
    address = 'YourEthereumAddress'
    
    eth_info = EthereumAddressInfo(api_key, address)
    
    balance = eth_info.get_balance()
    transactions = eth_info.get_transactions()
    
    print(f"Balance: {balance} ETH")
    print(f"Transactions: {transactions}")
```

## API Endpoints Used

- `account balance`: Get Ether balance for a single address.
- `account txlist`: Get a list of transactions for a single address.
- `account tokenbalance`: Get ERC-20 token balance for a single address by contract address.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
```

### Notes:
1. Replace `'YourEtherscanAPIKey'` and `'YourEthereumAddress'` with actual values.
2. You may need to sign up for an API key on [Etherscan](https://etherscan.io/apis) if you don't have one.

This script covers the essentials for interacting with the Etherscan API and retrieving relevant information about an Ethereum address. The provided README file is a basic documentation for your GitHub repository.
