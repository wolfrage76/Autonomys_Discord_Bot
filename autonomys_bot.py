import discord
import aiohttp
import asyncio
import os
import logging
import ssl
import time
import pickle
from decimal import Decimal

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
    nodeUrl = "http://rpc.mainnet.subspace.foundation/"

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

goal = 200  # Launch goal range in PiB
vers = "Unknown"  # Global variable for version data from utility_run
status_index = 0  # Index to keep track of current status in the rotation
status_options = []  # Store the status options

data_fetch_interval = 20  #
status_change_interval = 17  # 17 seconds for status change -- trying to avoid rate limiting
discord_update_interval = 17  # 17  seconds for Discord updates-- trying to avoid rate limiting

update_in_progress = False  # Flag to track if an update is in progress


def track_pledged_space_growth(totPledged, data_file='pledged_history.pkl', display_in_tb=True):
    global pledged_history
    current_time = time.time()

    # Define maxlen based on data retention needs (e.g., 30 days)
    max_data_points = 43200  # 30 days × 1,440 data points/day

    # Load existing data if the file exists
    if os.path.exists(data_file):
        try:
            with open(data_file, 'rb') as f:
                loaded_data = pickle.load(f)
                # Re-initialize the deque with maxlen and extend it with loaded data
                pledged_history = deque(loaded_data, maxlen=max_data_points)
                # logging.info(f"Loaded {len(pledged_history)} data points.")
        except Exception as e:
            logging.error(f"Error loading pledged data: {e}")
            pledged_history = deque(maxlen=max_data_points)
    else:
        pledged_history = deque(maxlen=max_data_points)
        logging.info("Initializing pledged data history.")

    # Append the current pledged amount
    pledged_history.append((current_time, float(totPledged)))
    # logging.info(f"Appended new data point. Total data points: {len(pledged_history)}")

    # Save the updated data back to the file
    try:
        with open(data_file, 'wb') as f:
            pickle.dump(pledged_history, f)
    except Exception as e:
        logging.error(f"Error saving pledged data: {e}")

    # Time periods in seconds
    periods = {
        '1h': 1 * 3600,
        '12h': 12 * 3600,
        '1d': 24 * 3600,
        '3d': 3 * 24 * 3600,
        '7d': 7 * 24 * 3600,
        '30d': 30 * 24 * 3600,
    }

    growth = {}
    if len(pledged_history) == 1:
        # If this is the initial data point, we cannot calculate growth
        for period_name in periods:
            growth[period_name] = "N/A"  # Not enough data
    else:
        for period_name, period_seconds in periods.items():
            # Initialize variables
            past_value = None

            # Calculate the timestamp for the period ago
            period_ago = current_time - period_seconds

            # Find the value from the specified period ago
            for timestamp, value in reversed(pledged_history):
                if timestamp <= period_ago:
                    past_value = value
                    break

            if past_value is not None:
                # Calculate growth in PB
                growth_pb = float(totPledged) - past_value

                if display_in_tb:
                    # Convert growth to TB (1 PB = 1000 TB)
                    growth_value = growth_pb * 1000
                    unit = "TB"
                else:
                    # Use growth in PB
                    growth_value = growth_pb
                    unit = "PB"

                # Round to 3 decimal places
                growth_value = round(growth_value, 3)
                growth[period_name] = growth_value
            else:
                # No data from the specified period, use the earliest data point
                earliest_timestamp, earliest_value = pledged_history[0]
                if earliest_timestamp != current_time:
                    elapsed_time = current_time - earliest_timestamp
                    elapsed_hours = int(elapsed_time / 3600)
                    if elapsed_hours < 24:
                        period_label = f"{elapsed_hours}h"
                    else:
                        period_label = f"{int(elapsed_hours / 24)}d"

                    # Calculate growth from the earliest data point
                    growth_pb = float(totPledged) - earliest_value

                    if display_in_tb:
                        growth_value = growth_pb * 1000
                        unit = "TB"
                    else:
                        growth_value = growth_pb
                        unit = "PB"

                    growth_value = round(growth_value, 3)
                    # Adjust the period name to reflect actual elapsed time
                    growth[period_name] = growth_value
                else:
                    growth[period_name] = "N/A"  # Not enough data

    return growth


async def utility_run():
    global vers, status_options, totPledged
    totPledged = 0
    
    latestver_url = 'http://subspacethingy.ifhya.com/info'
    constants_names = [ "CreditSupply", "TreasuryAccount"]

    # Set the display_in_tb flag here
    display_in_tb = False  # Set to False to display in PB

    while True:
        try:
            
            # Fetch version data
            async with aiohttp.ClientSession() as session:
                async with session.get(latestver_url) as response:
                    data = await response.json()
                    vers = data.get('latestver', 'Unknown')

            # Fetch constants from the node
            constants_response = await constants_lib.pull_constants(constant_names=constants_names)
            # print("\n" + str(constants_response) + "\n")
            
            constants_data = {list(item.keys())[0]: list(item.values())[0] for item in constants_response['result']}

            # Calculate required data
            if constants_data.get("TotalSpacePledged", False):
                totPledged = Decimal(constants_data.get("TotalSpacePledged", 0)) / (10 ** 15)  # In PB
                

            # Call the tracking function
            growth = track_pledged_space_growth(totPledged, display_in_tb=display_in_tb)

            # Log the growth values
            #logging.info(f"Pledged space growth: {growth}")

            totPledgedAmt = f'{totPledged:.3f}'
            pledgedPercent = round(Decimal(totPledgedAmt) * 100 / 200, 2)
            hasChanged = check_pledged_change()
            pledgeText, pledgeEnd = ("🎉 Hit Goal Range!", " 🚀") if totPledged > 200 else ("Total Pledged", "")

            try:
                blockchain_history_size_bytes = Decimal(constants_data.get("BlockchainHistorySize", 0))
                blockchain_history_size_gb = blockchain_history_size_bytes / (10 ** 9)  # GB
                blockHeight = await asyncio.to_thread(constants_lib.load_chainhead)
            except Exception as e:
                blockHeight = 'Unknown'

            # Prepare growth data for display
            past1d = growth.get('1d', 'N/A')
            past3d = growth.get('3d', 'N/A')
            past7d = growth.get('7d', 'N/A')
            past30d = growth.get('30d', 'N/A')

            # Update the unit based on display_in_tb
            unit = "TB" if display_in_tb else "PB"

            status_options = [
                ("Growth " + unit + "/day", f"🌳  1: {past1d:.2f} | 3: {past3d:.2f} | 7: {past7d:.2f}"),
                # ("Growth", f"7d:  | 30d: {past30d:.2f}"),
                ("Latest Release", f'🖥️  {vers}'),
                ("History Size", f"📜 {blockchain_history_size_gb if blockchain_history_size_gb < 1024 else blockchain_history_size_gb / 1024:.3f} {'GB' if blockchain_history_size_gb < 1024 else 'TB'}"),
                ("Block Height", f"🗃️  #{blockHeight}" if blockHeight else "Unavailable"), 
                (pledgeText, f"💾 {totPledgedAmt}PB {pledgeEnd} ({pledgedPercent}%) {hasChanged}"),

            ]
            prevPledged = totPledgedAmt
            if testnet:
                status_options.insert(0, ('👁️ Monitoring', '  Testnet'))

        except Exception as e:
            logging.error(f"Error fetching data: {e}")

        await asyncio.sleep(data_fetch_interval)

def check_pledged_change():
    #print('Trigger utility')
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
