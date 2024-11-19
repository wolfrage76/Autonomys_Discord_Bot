import discord
import aiohttp
import asyncio
import os
import logging
import ssl
import time

from dotenv import load_dotenv
from discord.ext import commands, tasks
from autonomys_query.query import SubstrateConstantsLibrary
from decimal import Decimal
from collections import deque




# Set up logging
logging.basicConfig(level=logging.INFO)

# Initialize settings
testnet = False  # Set to True for testnet or False for mainnet
if testnet:
    nodeUrl = "wss://rpc-0.taurus.subspace.network/ws"
else:
    nodeUrl = "wss://rpc.mainnet.subspace.foundation/ws"

load_dotenv()

# Initialize a deque to store pledged amounts with timestamps
pledged_history = deque(maxlen=100)  # Adjust maxlen as needed
# Instantiate the SubstrateConstantsLibrary
constants_lib = SubstrateConstantsLibrary(nodeUrl)

# Fetch token from environment variable for security
TOKEN = os.getenv('AUTONOMYS_BOT_TOKEN')
if not TOKEN:
    raise ValueError("Discord bot token is not set in environment variables.")

intents = discord.Intents.default()
intents.guilds = True  # Required to fetch guilds
bot = commands.Bot(command_prefix="!", intents=intents)

goal = 600  # Launch goal in PiB
vers = "Unknown"  # Global variable for version data from utility_run
status_index = 0  # Index to keep track of current status in the rotation
status_options = []  # Store the status options

data_fetch_interval = 20  #
status_change_interval = 17  # 17 seconds for status change -- trying to avoid rate limiting
discord_update_interval = 17  # 17  seconds for Discord updates-- trying to avoid rate limiting

update_in_progress = False  # Flag to track if an update is in progress

async def utility_run():
    global vers, status_options
    latestver_url = 'http://subspacethingy.ifhya.com/info'
    constants_names = ["TotalSpacePledged", "CreditSupply", "TreasuryAccount"]

    while True:
        try:
            # Fetch version data
            async with aiohttp.ClientSession() as session:
                async with session.get(latestver_url) as response:
                    data = await response.json()
                    vers = data.get('latestver', 'Unknown')

            # Fetch constants from the node
            constants_response = await constants_lib.pull_constants(constant_names=constants_names)
            constants_data = {list(item.keys())[0]: list(item.values())[0] for item in constants_response['result']}

            # Calculate required data
#            totPledged = Decimal(constants_data.get("TotalSpacePledged", 0)) / (2 ** 50) # In PiB
            totPledged = Decimal(constants_data.get("TotalSpacePledged", 0)) / (10 ** 15)  #  In PB
            totPledgedAmt = f'{totPledged:.3f}'
            pledgedPercent = round(Decimal(totPledgedAmt) * 100 / 600, 2)
            hasChanged = check_pledged_change()
            pledgeText, pledgeEnd = ("🎉 Hit Goal!", " 🚀") if totPledged > 600 else ("Total Pledged", "") 
            
            try:
                blockchain_history_size_bytes = Decimal(constants_data.get("BlockchainHistorySize", 0))
                # blockchain_history_size_gib = blockchain_history_size_bytes / (1024 ** 3) #  GiB
                blockchain_history_size_gb = blockchain_history_size_bytes / (10 ** 9)  # GB
                blockHeight = await asyncio.to_thread(constants_lib.load_chainhead)
            except Exception as e:
                pass
            
            status_options = [
                ("Latest Release", f'🖥️  {vers}'),
                ("History Size", f"📜 {blockchain_history_size_gb:.3f} GB"), # Change to match GB/GiB above
                ("Block Height", f"🗃️  #{blockHeight}" if blockHeight else "Unavailable"),
                (pledgeText, f"💾 {totPledgedAmt}PB {pledgeEnd} ({pledgedPercent}%) {hasChanged}") , # Change to match PB/PiB above
            ]

            if testnet:
                status_options.insert(0, ('👁️ Monitoring', '  Testnet'))

        except Exception as e:
            logging.error(f"Error fetching data: {e}")

        await asyncio.sleep(data_fetch_interval)

def check_pledged_change():
    print('Trigger utility')
    current_time = time.time()
    
    # Ensure there's at least one entry in the history
    if not pledged_history:
        return '↕️'  # No data yet

    # Get the current pledged amount (rounded to 2 digits)
    current_totPledged = round(pledged_history[-1][1], 2)

    # Calculate the timestamp for one hour ago
    one_hour_ago = current_time - 3600  # 3600 seconds in an hour

    # Initialize variable to store the pledged amount from one hour ago
    one_hour_ago_value = None

    # Iterate over the history to find the pledged amount from one hour ago
    for timestamp, value in reversed(pledged_history):
        if timestamp <= one_hour_ago:
            one_hour_ago_value = value
            break

    # If there's no data from one hour ago
    if one_hour_ago_value is None:
        return '↕️'  # Not enough data to determine change

    one_hour_ago_value = round(one_hour_ago_value, 2)

    # Compare the current pledged amount with the one from an hour ago
    if current_totPledged > one_hour_ago_value:
        return '⬆️'
    elif current_totPledged < one_hour_ago_value:
        return '⬇️'
    else:
        return '↕️'


@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    change_status.start()  # Start the status change task
    update_discord_status.start()  # Start the Discord update task
    bot.loop.create_task(utility_run())  # Start utility_run in the background

@tasks.loop(seconds=status_change_interval)
async def change_status():
    global status_index

    try:
        # Ensure status_options is not empty
        if not status_options:
            logging.warning("status_options is empty, cannot proceed with the loop.")
            return

        # Log the current status index for debugging
        #logging.info(f"Current status index: {status_index}")

        # Cycle through the list based on the current index
        status_index = (status_index + 1) % len(status_options)
        #logging.info(f"Next status index: {status_index}")

    except Exception as e:
        logging.error(f"Error in status rotation loop: {e}")

@tasks.loop(seconds=discord_update_interval)
async def update_discord_status():
    global update_in_progress

    # Skip updating if another update is in progress
    if update_in_progress:
        logging.info("Skipping update as another update is in progress.")
        return

    update_in_progress = True

    try:
        # Ensure status_options is not empty
        if not status_options:
            logging.warning("status_options is empty, cannot proceed with the Discord update.")
            return

        # Get the current status message
        nickname, status_message = status_options[status_index]
        logging.info(f"Updating Discord status: {nickname} - {status_message}")

        # Update bot's presence status
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.custom, name='custom', state=status_message))

        # Update bot's nickname in each server it is in
        for guild in bot.guilds:
            try:
                await guild.me.edit(nick=nickname)
            except discord.Forbidden:
                logging.warning(f"Permission denied: Unable to change nickname in {guild.name}")

    except Exception as e:
        logging.error(f"Error in Discord update loop: {e}")

    finally:
        update_in_progress = False

bot.run(TOKEN)
