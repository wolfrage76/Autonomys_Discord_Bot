import logging
import gc
# import sys

# from memory_profiler import profile
from substrateinterface import SubstrateInterface


class SubstrateConstantsLibrary:
    def __init__(self, node_url="http://rpc.mainnet.subspace.foundation/"):
        self.node_url = node_url

    # @profile
    def fetch_constant(self, pallet_name, constant_name):
        try:
            with SubstrateInterface(url=self.node_url) as substrate:
                
                constant = substrate.get_constant(pallet_name, constant_name)
                value = constant.value
                substrate.close()
                
            gc.collect()  # Trigger garbage collection
            return value
        except Exception as e:
            logging.error(f"Error fetching {pallet_name}.{constant_name}: {e}")
            return None

    # @profile
    def fetch_block_height(self):
        """Fetch the current block height from the blockchain."""
        try:
            # Establish a fresh connection using a context manager to ensure proper cleanup
            with SubstrateInterface(url=self.node_url) as substrate:
                # Fetch the block height
                block_height = substrate.get_block_number(substrate.get_chain_head())
                substrate.close()
                gc.collect()
                return block_height
        except Exception as e:
            logging.error(f"Error fetching block height: {e}")
            return None
