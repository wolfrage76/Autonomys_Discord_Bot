import json
import ssl
import asyncio
from substrateinterface import SubstrateInterface

class SubstrateConstantsLibrary:
    def __init__(self, node_url="wss://rpc.mainnet.subspace.foundation/ws", constants_file="autonomys_query/constants_list.json"):
        self.node_url = node_url
        self.constants_file = constants_file
        self.substrate = SubstrateInterface(url=node_url)
        self.constants_metadata = self._load_constants_metadata()

    def load_chainhead(self):
        """
        Fetch the latest block from the chain and return the block height.
        """
        try:
            block = self.substrate.get_block(self.substrate.get_chain_head())
            block_height = block['header']['number']
            return block_height
        except Exception as e:
            raise Exception(f"Failed to load chain head: {e}")

    async def load_chainhead_with_retry(self, retries=3, delay=5):
        """
        Retry fetching the chain head with a delay if SSL errors occur.
        """
        chain_head = None
        for attempt in range(retries):
            try:
                chain_head = self.substrate.get_chain_head()
                break
            except ssl.SSLEOFError:
                if attempt == retries - 1:
                    raise Exception("Failed to fetch chain head after multiple attempts")
            except Exception as e:
                if attempt == retries - 1:
                    raise Exception(f"Failed to fetch chain head after multiple attempts: {e}")
            await asyncio.sleep(delay)
        if chain_head is None:
            raise Exception("Chain head is None, this should never happen")
        block = self.substrate.get_block(chain_head)
        return block['header']['number']

    def _load_constants_metadata(self):
        """
        Load constants metadata from the JSON file.
        """
        try:
            with open(self.constants_file, 'r') as file:
                return json.load(file)
        except FileNotFoundError:
            raise Exception(f"'{self.constants_file}' vanished into thin air.")
        except json.JSONDecodeError:
            raise Exception(f"Oops, looks like '{self.constants_file}' has some funky JSON.")

    def list_constants(self):
        """
        Show all available constants with their module names and descriptions.
        
        Returns:
            - A list of constants with module names and descriptions.
        """
        return self.constants_metadata

    async def pull_constants(self, constant_names=None):
        """
        Asynchronously fetch the requested constants from the node.
        
        Parameters:
            - constant_names: Specify which constants to fetch. Use 'all' to grab everything.
        
        Returns:
            - A dictionary of constants with their values.
        """
        # Run the blocking code in a background thread to avoid blocking the event loop
        return await asyncio.to_thread(self._fetch_constants, constant_names)

    def _fetch_constants(self, constant_names=None):
        """
        This function performs the actual blocking call to fetch constants.
        """
        result = []
        ignored_constants = []

        # Determine which constants to query
        constants_to_query = (
            self.constants_metadata if constant_names and "all" in constant_names
            else [item for item in self.constants_metadata if item["name"] in constant_names]
        )

        # Query each constant and store in the result
        for item in constants_to_query:
            module, name = item["module"], item["name"]
            try:
                constant_value = self.substrate.get_constant(module, name).value
                result.append({name: constant_value})
            except Exception:
                ignored_constants.append(name)
        try:
            blockchain_history_size = self.substrate.get_constant('TransactionFees', 'BlockchainHistorySize').value
            result.append({'BlockchainHistorySize': blockchain_history_size})
        except Exception:
            pass 

        return {"result": result, "ignored_constants": ignored_constants}
