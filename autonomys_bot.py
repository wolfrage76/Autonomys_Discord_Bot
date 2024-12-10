import discord
import aiohttp
import asyncio
import os
import logging
import sqlite3
import time

from decimal import Decimal
from dotenv import load_dotenv
from discord.ext import commands, tasks
from query import SubstrateConstantsLibrary  # Adjust the import path if necessary

# Set up logging
logging.basicConfig(level=logging.INFO)

# Initialize settings
testnet = False  # Set to True for testnet or False for mainnet
if testnet:
    nodeUrl = "http://rpc-0.tau1.subspace.network/"  # Update to the correct testnet URL
else:
    nodeUrl = "http://rpc.mainnet.subspace.foundation"

load_dotenv()

# Initialize SQLite database
def initialize_database():
    conn = sqlite3.connect('pledged_history.db')  # Persistent storage on disk
    c = conn.cursor()
    # Create a table for storing timestamp and pledged space
    c.execute('''
        CREATE TABLE IF NOT EXISTS pledged_history (
            timestamp REAL PRIMARY KEY,  -- Timestamp as the primary key
            pledged_space REAL           -- Pledged space in PB
        )
    ''')
    conn.commit()
    conn.close()

initialize_database()  # Ensure database is initialized

# SQLite functions for data management
def add_pledged_data(timestamp, pledged_space):
    conn = sqlite3.connect('pledged_history.db')
    c = conn.cursor()
    try:
        c.execute('INSERT OR REPLACE INTO pledged_history (timestamp, pledged_space) VALUES (?, ?)',
                (timestamp, pledged_space))
        conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Error inserting data: {e}")
    finally:
        conn.close()


def prune_old_data(retention_period_seconds=2592000):  # Default: 30 days
    """
    Delete data points older than the specified retention period.

    Args:
        retention_period_seconds (int): Number of seconds to retain data.
    """
    current_time = time.time()
    cutoff_time = current_time - retention_period_seconds

    conn = sqlite3.connect('pledged_history.db')
    c = conn.cursor()
    try:
        c.execute('DELETE FROM pledged_history WHERE timestamp < ?', (cutoff_time,))
        conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Error pruning old data: {e}")
    finally:
        conn.close()

# Fetch token from environment variable for security
TOKEN = os.getenv('AUTONOMYS_BOT_TOKEN')
if not TOKEN:
    raise ValueError("Discord bot token is not set in environment variables.")

# Discord bot setup
intents = discord.Intents.default()
intents.guilds = True  # Required to fetch guilds
bot = commands.Bot(command_prefix="!", intents=intents)

vers = "Unknown"  # Global variable for version data from utility_run
status_index = 0  # Index to keep track of current status in the rotation
status_options = []  # Store the status options

data_fetch_interval = 40  # How often to query RPC
status_change_interval = 17  # Avoiding rate limiting

constants_lib = SubstrateConstantsLibrary(nodeUrl)  # Initialize SubstrateConstantsLibrary


def format_time_between_rewards(seconds):
    """
    Convert seconds into a human-readable string in the format M d h m.

    Parameters:
    - seconds (int): Time in seconds.

    Returns:
    - str: Formatted time string without leading empty fields.
    """
    # Time units
    seconds_per_minute = 60
    seconds_per_hour = 60 * seconds_per_minute
    seconds_per_day = 24 * seconds_per_hour
    seconds_per_month = 30 * seconds_per_day  # Approximate months as 30 days

    # Calculate time components
    months = seconds // seconds_per_month
    seconds %= seconds_per_month
    days = seconds // seconds_per_day
    seconds %= seconds_per_day
    hours = seconds // seconds_per_hour
    seconds %= seconds_per_hour
    minutes = seconds // seconds_per_minute

    # Build the formatted time string
    time_parts = []
    if months > 0:
        time_parts.append(f"{int(months)}M")
    if days > 0:
        time_parts.append(f"{int(days)}d")
    if hours > 0:
        time_parts.append(f"{int(hours)}h")
    if minutes > 0:
        time_parts.append(f"{int(minutes)}m")

    return " ".join(time_parts)



def estimate_autonomys_rewards_count(
    network_space_pib,
    daily_blocks=14400,
    block_reward_ratio=1,
    vote_reward_ratio=9,
    pledged_space_tib=1
):
    """
    Estimate the number of rewards per day, block rewards, vote rewards, and time between rewards for 1 TiB.

    Parameters:
    - network_space_pib (float): Total network space in PiB.
    - daily_blocks (int): Total blocks produced daily.
    - block_reward_ratio (int): Number of block rewards per block (default: 1).
    - vote_reward_ratio (int): Number of vote rewards per block (default: 9).
    - pledged_space_tib (float): Pledged space in TiB (default: 1 TiB).

    Returns:
    - dict: Rewards per day (total, block, vote) and time between rewards in human-readable format.
    """
    # Constants
    tib_per_pib = Decimal(1024)  # 1 PiB = 1024 TiB
    seconds_per_day = Decimal(86400)  # Number of seconds in a day
    total_rewards_per_block = Decimal(block_reward_ratio + vote_reward_ratio)  # Total rewards per block

    # Calculate proportion of network space
    total_network_tib = Decimal(network_space_pib) * tib_per_pib
    proportion_of_network = ((Decimal(pledged_space_tib * Decimal(.99))) / total_network_tib)  

    # Calculate the total number of rewards per day
    total_rewards_per_day = Decimal(daily_blocks) * total_rewards_per_block * proportion_of_network

    # Split rewards into block and vote rewards
    block_rewards_per_day = Decimal(daily_blocks) * Decimal(block_reward_ratio) * proportion_of_network
    vote_rewards_per_day = Decimal(daily_blocks) * Decimal(vote_reward_ratio) * proportion_of_network

    # Calculate the time between rewards in seconds
    time_between_rewards_seconds = seconds_per_day / total_rewards_per_day if total_rewards_per_day > 0 else None

    return {
        "total_rewards_per_day": round(float(total_rewards_per_day), 3),
        "block_rewards_per_day": round(float(block_rewards_per_day), 3),
        "vote_rewards_per_day": round(float(vote_rewards_per_day), 3),
        "time_between_rewards": format_time_between_rewards(Decimal(time_between_rewards_seconds))
        if time_between_rewards_seconds else "0",
    }



# Track pledged space growth
def track_pledged_space_growth(totPledged, display_in_tb=True):
    """
    Track pledged space growth over time and calculate growth for predefined periods.
    """
    current_time = time.time()
    add_pledged_data(current_time, totPledged)

    # Define time periods in seconds
    periods = {
        '1d': 24 * 3600,
        '3d': 3 * 24 * 3600,
        '7d': 7 * 24 * 3600,
        '30d': 30 * 24 * 3600,
    }

    growth = {}
    for period_name, period_seconds in periods.items():
        growth_value = calculate_growth_for_period(period_seconds, display_in_tb)
        growth[period_name] = growth_value

    return growth

def calculate_growth_for_period(period_seconds, display_in_tb=False):
    """
    Calculate pledged space growth over a specific time period using SQL.

    Args:
        period_seconds (int): Number of seconds to look back (e.g., 1 hour = 3600 seconds).
        display_in_tb (bool): Whether to return growth in TB or PB.

    Returns:
        float: The growth value, or "N/A" if not enough data exists.
    """
    current_time = time.time()
    start_time = current_time - period_seconds

    conn = sqlite3.connect('pledged_history.db')
    c = conn.cursor()
    try:
        # Retrieve the earliest and latest pledged_space values in the period
        c.execute('''
            SELECT 
                (SELECT pledged_space FROM pledged_history WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1) AS earliest,
                (SELECT pledged_space FROM pledged_history WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 1) AS latest
        ''', (start_time, start_time))
        
        result = c.fetchone()

        if result and result[0] is not None and result[1] is not None:
            earliest, latest = result
            growth_pb = latest - earliest  # Calculate growth in PB
            if display_in_tb:
                return round(growth_pb * 1000, 3)  # Convert to TB
            else:
                return round(growth_pb, 3)  # Keep in PB
        else:
            return "N/A"  # Not enough data

    except sqlite3.Error as e:
        logging.error(f"Error calculating growth: {e}")
        return "N/A"
    finally:
        conn.close()

async def utility_run():
    global vers, status_options, totPledged
    totPledged = 0

    latestver_url = 'http://subspacethingy.ifhya.com/info'
    display_in_tb = False  # False == Display growth in PB

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Fetch version data
                vers, acresvers = await fetch_version_data(session, latestver_url)   

                total_space_pledged = constants_lib.fetch_constant("TransactionFees", "TotalSpacePledged")
                total_circulation = constants_lib.fetch_constant("TransactionFees", "CreditSupply")
                #logging.info(f"TotalSpacePledged: {total_space_pledged}")

                # Fetch BlockchainHistorySize
                blockchain_history_size = constants_lib.fetch_constant("TransactionFees", "BlockchainHistorySize")
                blockchain_history_size_gb = blockchain_history_size / (10 ** 9)
                #logging.info(f"BlockchainHistorySize: {blockchain_history_size_gb}")


                # Calculate pledged space
                totPledged = calculate_total_pledged(total_space_pledged)

                pledgeText, pledgeEnd = "Total Pledged", ""
               # blockchain_history_size_gb = blockchain_history_size / (10 ** 9)
                
                # Fetch current block height
                block_height = constants_lib.fetch_block_height()
                #logging.info(f"Block Height: {block_height}")

                # Generate status options
                status_options = generate_status_options(
                    pledgeText, pledgeEnd, totPledged, vers,acresvers,
                    blockchain_history_size_gb, block_height, testnet, " TB" if display_in_tb else " PB",
                    total_circulation,
                )

                # Prune old data
                prune_old_data()

            except Exception as e:
                logging.error(f"Error in utility_run: {e}")

            await asyncio.sleep(data_fetch_interval)

# Helper functions
async def fetch_version_data(session, url):
    try:
        async with session.get(url) as response:
            data = await response.json()
            return data.get('latestver', 'Unknown'), data.get('latest_spaceacres_version', 'Unknown') 
    except Exception as e:
        logging.error(f"Error fetching version data: {e}")
        return "Unknown"

def parse_constants_response(response):
    try:
        return {list(item.keys())[0]: list(item.values())[0] for item in response.get('result', [])}
    except Exception as e:
        logging.error(f"Error parsing constants response: {e}")
        return {}

def calculate_total_pledged(constants_data):
    try:
        total_space_pledged = float(constants_data) # constants_data.get("TotalSpacePledged", 0))
        return total_space_pledged / (10 ** 15)  # Convert to PB
    except Exception as e:
        logging.error(f"Error calculating total pledged: {e}")
        return 0

def format_with_commas(number):

    try:
        # If it's a float, format to include commas and maintain decimals
        if isinstance(number, float):
            return f"{number:,.2f}"  # Adjust decimal places if needed
        # If it's an integer, format with commas
        elif isinstance(number, int):
            return f"{number:,}"
        else:
            raise ValueError("Input must be an int or float.")
    except Exception as e:
        raise ValueError(f"Error formatting number: {e}")
    
def generate_status_options(pledgeText, pledgeEnd, totPledged, vers, acresvers,
                            blockchain_history_size_gb, blockHeight, testnet, unit, total_circulation,):
    est_rewards = estimate_autonomys_rewards_count(totPledged,)
    growth = track_pledged_space_growth(totPledged, False)
    chartGrowth = f"1: {growth.get('1d', 0):.2f} |3: {growth.get('3d', 0):.2f} |7: {growth.get('7d', 0):.2f}"
    digits = float(10**18)
    status = [
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ("Community Tools", "🪄  https://ai3.farm/tools"),
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ('Est Wins/TB/Day', f"🏆 {est_rewards.get('total_rewards_per_day','0'):.3f}/day ({est_rewards.get('time_between_rewards', 'N/A')})"),
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ("Growth PB/day", f'🌳 {chartGrowth}'),
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ("Latest Release", f"🖥️  {vers}"),
        ("Latest Release", f"🖥️  Space Acres: {acresvers}"),
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ("History Size", f"📜 {blockchain_history_size_gb:.3f} GB"),
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ("Block Height", f"📏  #{blockHeight}" if blockHeight != "Unknown" else "Unavailable"),
        (pledgeText, f"💾 {totPledged:.3f} PB {pledgeEnd}"),
        ("In Circulation", f"💰 {format_with_commas(int(total_circulation / digits))}/1B AI3"),
        
    ]
    if testnet:
        status.insert(0, ('👁️ Monitoring', 'Testnet'))
    return status

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    change_status.start()  # Start the status change task
    #logging.info("Started change_status task.")
    bot.loop.create_task(utility_run())  # Start utility_run in the background
    #logging.info("Started utility_run task.")

@tasks.loop(seconds=status_change_interval)
async def change_status():
    global status_index

    try:
        if not status_options:
            logging.warning("status_options is empty, skipping status change.")
            return

        # Cycle through the list
        status_index = (status_index + 1) % len(status_options)
        nickname, status_message = status_options[status_index]

        logging.info(f"Attempting to update status: {nickname} - {status_message}")

        # Update bot's presence
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.custom, name='custom', state=status_message))
        
        for guild in bot.guilds:
            try:
                await guild.me.edit(nick=nickname)
                #logging.info(f"Nickname updated in {guild.name} to: {nickname}")
            except discord.Forbidden:
                logging.warning(f"Permission denied: Unable to change nickname in {guild.name}")
            except Exception as e:
                logging.error(f"Error updating nickname in {guild.name}: {e}")

    except Exception as e:
        logging.error(f"Error in change_status loop: {e}")

bot.run(TOKEN)
