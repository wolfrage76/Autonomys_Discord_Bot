import logging
from memory_profiler import profile
from substrateinterface import SubstrateInterface

class SubstrateConstantsLibrary:
    def __init__(self, node_url="wss://rpc.mainnet.subspace.foundation/"):
        self.node_url = node_url
        self.substrate = SubstrateInterface(
            url=node_url,
            #ss58_format=42,  # Adjust if your chain uses a different format
            #type_registry_preset='default',  # Adjust if needed
            auto_reconnect=True
        )

    def __del__(self):
        if self.substrate:
            self.substrate.close()

   # @profile
    def fetch_constants(self):
        """
        Fetch dynamic constants from the node.
        """
        result = []
        try:
            # Refresh runtime metadata to get the latest constant values
            self.substrate.init_runtime()
            
            # Fetch TotalSpacePledged as a constant
            total_space_pledged = self.substrate.get_constant('TransactionFees', 'TotalSpacePledged').value
            result.append({'TotalSpacePledged': total_space_pledged})
            
            # Fetch BlockchainHistorySize as a constant
            blockchain_history_size = self.substrate.get_constant('TransactionFees', 'BlockchainHistorySize').value
            result.append({'BlockchainHistorySize': blockchain_history_size})
        except Exception as e:
            logging.error(f"Error fetching constants: {e}")
        return {'result': result}

    #@profile
    def load_chainhead(self):
        try:
            block_hash = self.substrate.get_chain_head()
            block_number = self.substrate.get_block_number(block_hash)
            return block_number
        except Exception as e:
            logging.error(f"Error fetching block height: {e}")
            return "Unknown"

