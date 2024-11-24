import discord
import aiohttp
import asyncio
import os
import logging
import sqlite3
import time
from dotenv import load_dotenv
from discord.ext import commands, tasks
from query import SubstrateConstantsLibrary  # Adjust the import path if necessary

# Set up logging
logging.basicConfig(level=logging.INFO)
# profiling=False

# Initialize settings
testnet = False  # Set to True for testnet or False for mainnet
if testnet:
    nodeUrl = "wss://rpc-0.taurus.subspace.network/ws"
else:
    nodeUrl = "wss://rpc.mainnet.subspace.foundation/"

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

def get_pledged_data(period_seconds):
    current_time = time.time()
    start_time = current_time - period_seconds
    conn = sqlite3.connect('pledged_history.db')
    c = conn.cursor()
    try:
        c.execute('SELECT timestamp, pledged_space FROM pledged_history WHERE timestamp >= ? ORDER BY timestamp ASC',
                (start_time,))
        data = c.fetchall()
        return data
    except sqlite3.Error as e:
        logging.error(f"Error retrieving data: {e}")
        return []
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

def calculate_growth_for_period(period_seconds, display_in_tb):
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
    display_in_tb = False  # Display growth in PB

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Fetch version data
                vers = await fetch_version_data(session, latestver_url)

                # Fetch constants from the node
                constants_response = await asyncio.to_thread(constants_lib.fetch_constants)
                constants_data = parse_constants_response(constants_response)

                # Calculate pledged space
                totPledged = calculate_total_pledged(constants_data)

                pledgeText, pledgeEnd = "Total Pledged", ""
                blockchain_history_size_gb, blockHeight = await fetch_blockchain_details(constants_data)

                # Generate status options
                status_options = generate_status_options(
                    pledgeText, pledgeEnd, totPledged, vers,
                    blockchain_history_size_gb, blockHeight, testnet, "TB" if display_in_tb else "PB"
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
            return data.get('latestver', 'Unknown')
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
        total_space_pledged = float(constants_data.get("TotalSpacePledged", 0))
        return total_space_pledged / (10 ** 15)  # Convert to PB
    except Exception as e:
        logging.error(f"Error calculating total pledged: {e}")
        return 0

async def fetch_blockchain_details(constants_data):
    try:
        blockchain_history_size_bytes = float(constants_data.get("BlockchainHistorySize", 0))
        blockchain_history_size_gb = blockchain_history_size_bytes / (10 ** 9)
        blockHeight = await asyncio.to_thread(constants_lib.load_chainhead)
        return blockchain_history_size_gb, blockHeight
    except Exception as e:
        logging.error(f"Error fetching blockchain details: {e}")
        return 0, "Unknown"

def generate_status_options(pledgeText, pledgeEnd, totPledged, vers,
                            blockchain_history_size_gb, blockHeight, testnet, unit):
    growth = track_pledged_space_growth(totPledged, False)
    chartGrowth = f"1: {growth.get('1d', 0):.2f} |3: {growth.get('3d', 0):.2f} |7: {growth.get('7d', 0):.2f}"
    status = [
        (pledgeText, f"💾 {totPledged:.3f}PB {pledgeEnd}"),
        ("Growth PB/day", f'🌳 {chartGrowth}'),
        (pledgeText, f"💾 {totPledged:.3f}PB {pledgeEnd}"),
        ("Latest Release", f"🖥️  {vers}"),
        (pledgeText, f"💾 {totPledged:.3f}PB {pledgeEnd}"),
        ("History Size", f"📜 {blockchain_history_size_gb:.3f} GB"),
        (pledgeText, f"💾 {totPledged:.3f}PB {pledgeEnd}"),
        ("Block Height", f"🗃️  #{blockHeight}" if blockHeight != "Unknown" else "Unavailable"),
    ]
    if testnet:
        status.insert(0, ('👁️ Monitoring', 'Testnet'))
    return status

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    change_status.start()  # Start the status change task
    logging.info("Started change_status task.")
    bot.loop.create_task(utility_run())  # Start utility_run in the background
    logging.info("Started utility_run task.")

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
