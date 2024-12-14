import discord
import aiohttp
import asyncio
import os
import logging
import aiosqlite
import gc
import time

from dotenv import load_dotenv
from discord.ext import commands, tasks
from substrateinterface import SubstrateInterface

testnet = False # Monitor testnet or False for mainnet. 

# Load environment variables
load_dotenv()
TOKEN = os.getenv('AUTONOMYS_BOT_TOKEN')
if not TOKEN:
    raise ValueError("Discord bot token is not set in environment variables.")

# Global settings

node_url = "http://rpc-0.tau1.subspace.network/" if testnet else "http://rpc.mainnet.subspace.foundation"
data_fetch_interval = 130
status_change_interval = 14

# Global state
class BotState:
    def __init__(self):
        self.status_index = 0
        self.status_options = []
        self.current_nicknames = {}
        self.version = "Unknown"
        self.tot_pledged = 0.0
        self.first_run = True

bot_state = BotState()

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = False # Todo: Add Banner command for Admins
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Database initialization
async def initialize_database():
    async with aiosqlite.connect('pledged_history.db') as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pledged_history (
                timestamp REAL PRIMARY KEY,
                pledged_space REAL
            )
        ''')
        await conn.commit()

async def add_pledged_data(timestamp, pledged_space):
    async with aiosqlite.connect('pledged_history.db') as conn:
        await conn.execute(
            'INSERT OR REPLACE INTO pledged_history (timestamp, pledged_space) VALUES (?, ?)',
            (timestamp, pledged_space)
        )
        await conn.commit()

async def prune_old_data(retention_period_seconds=31536000):  # Default: 1 year
    current_time = time.time()
    cutoff_time = current_time - retention_period_seconds
    async with aiosqlite.connect('pledged_history.db') as conn:
        await conn.execute('DELETE FROM pledged_history WHERE timestamp < ?', (cutoff_time,))
        await conn.commit()

# Fetch constants and block height together
async def fetch_constants_and_height():
    #logging.info('In fetch constants and height')
    try:
        with SubstrateInterface(url=node_url) as substrate:
            constants = {
                "TotalSpacePledged": float(substrate.get_constant("TransactionFees", "TotalSpacePledged").value),
                "CreditSupply": float(substrate.get_constant("TransactionFees", "CreditSupply").value),
                "BlockchainHistorySize": float(substrate.get_constant("TransactionFees", "BlockchainHistorySize").value),
            }
            block_height = substrate.get_block_number(substrate.get_chain_head())
        
        substrate.close() # Seems to help prevent mem leaks caused by pulling substrate constants that change
        gc.collect()  # Trigger garbage collection
        #logging.info('Leaving fetch constants and height')
        return constants, block_height
    
    except Exception as e:
        logging.error(f"Error fetching constants and block height: {e}")
        return {}, None

# Utility functions
def calculate_total_pledged(total_space_pledged):
    try:
        return total_space_pledged / (10 ** 15)  # Convert to PB
    except Exception as e:
        logging.error(f"Error calculating total pledged: {e}")
        return 0.0

async def utility_run():
    global bot_state
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                constants, block_height = await fetch_constants_and_height()
                total_space_pledged = constants.get("TotalSpacePledged")
                total_circulation = constants.get("CreditSupply")
                blockchain_history_size = constants.get("BlockchainHistorySize")

                bot_state.tot_pledged = calculate_total_pledged(total_space_pledged) if total_space_pledged else 0.0
                blockchain_history_size_gb = blockchain_history_size / (10 ** 9) if blockchain_history_size else 0.0

                bot_state.version, acresvers = await fetch_version_data(session)

                bot_state.status_options = await generate_status_options(
                    "Total Pledged", bot_state.tot_pledged, bot_state.version, acresvers,
                    blockchain_history_size_gb, block_height, testnet, total_circulation
                )

                await prune_old_data()
                await asyncio.sleep(data_fetch_interval)
                
            except Exception as e:
                logging.error(f"Error in utility_run: {e}")

            

async def fetch_version_data(session):
    try:
        url = "http://subspacethingy.ifhya.com/info"
        async with session.get(url) as response:
            data = await response.json()
            return data.get('latestver', 'Unknown'), data.get('latest_spaceacres_version', 'Unknown')
    except Exception as e:
        logging.error(f"Error fetching version data: {e}")
        return "Unknown", "Unknown"

async def generate_status_options(pledge_text, tot_pledged, vers, acresvers,
                                blockchain_history_size_gb, block_height, testnet, total_circulation):
    est_rewards = estimate_autonomys_rewards_count(tot_pledged)
    growth = await track_pledged_space_growth(tot_pledged, False)
    chart_growth = f"1: {growth.get('1d', 0):.2f} |3: {growth.get('3d', 0):.2f} |7: {growth.get('7d', 0):.2f}"
    chart_growth2 = f"90: {int(growth.get('90d', 0))} |180: {int(growth.get('180d', 0))} |365: {int(growth.get('365d', 0))}"
    digits = float(10**18)

    status = [
        (pledge_text, f"💾 {tot_pledged:.3f} PB "),
        ("Community Tools", "✨  https://ai3.farm/tools"),
        #(pledge_text, f"💾 {tot_pledged:.3f} PB "),
        ("Est Wins/TB/Day", f"🏆 {est_rewards.get('total_rewards_per_day', '0'):.3f}/day ({est_rewards.get('time_between_rewards', 'N/A')})"),
        ("Total Pledged", f"💾 {tot_pledged:.3f} PB "),
        ("Growth PB/day", f"🌳 {chart_growth}"),
        #(pledge_text, f"💾 {tot_pledged:.3f} PB "),
        ("Latest Release", f"🖥️  {vers}"),
        ("Latest Release", f"🖥️  Space Acres: {acresvers}"),
        ("Total Pledged", f"💾 {tot_pledged:.3f} PB "),
        ("History Size", f"📜 {blockchain_history_size_gb:.3f} GB"),
        #(pledge_text, f"💾 {tot_pledged:.3f} PB "),
        ("Block Height", f"📏  #{block_height}" if block_height else "Unavailable"),
        ("Total Pledged", f"💾 {tot_pledged:.3f} PB "),
        ("In Circulation", f"💰 {int(total_circulation / digits):,}/1B AI3"),
        ("Wolfrage's Tools", f"🚀 github.com/wolfrage76"),
    ]
    if testnet:
        status.insert(0, ('👁️ Monitoring', 'Testnet'))
    return status

async def track_pledged_space_growth(tot_pledged, display_in_tb=True):
    current_time = time.time()
    await add_pledged_data(current_time, tot_pledged)
    
    day = 24 * 3600
    periods = {
        '1d': day,
        '3d': 3 * day,
        '7d': 7 * day,
        '90d': 90 * day,
        '180d': 180 * day,
        '365d': 365 * day,
    }

    growth = {}
    async with aiosqlite.connect('pledged_history.db') as conn:
        for period_name, period_seconds in periods.items():
            cutoff_time = current_time - period_seconds
            async with conn.execute(
                'SELECT MIN(pledged_space), MAX(pledged_space) FROM pledged_history WHERE timestamp >= ?',
                (cutoff_time,)
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0] is not None and row[1] is not None:
                    growth_value = (row[1] - row[0]) * (1000 if display_in_tb else 1)
                    growth[period_name] = round(growth_value, 3)
                else:
                    growth[period_name] = 0.0
    return growth

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
    tib_per_pib = float(1024)  # 1 PiB = 1024 TiB
    seconds_per_day = float(86400)  # Number of seconds in a day
    total_rewards_per_block = float(block_reward_ratio + vote_reward_ratio)  # Total rewards per block

    # Calculate proportion of network space
    total_network_tib = float(network_space_pib) * tib_per_pib
    proportion_of_network = ((float(pledged_space_tib * .99)) / total_network_tib)  

    # Calculate the total number of rewards per day
    total_rewards_per_day = float(daily_blocks) * total_rewards_per_block * proportion_of_network

    # Split rewards into block and vote rewards
    block_rewards_per_day = float(daily_blocks) * float(block_reward_ratio) * proportion_of_network
    vote_rewards_per_day = float(daily_blocks) * float(vote_reward_ratio) * proportion_of_network

    # Calculate the time between rewards in seconds
    time_between_rewards_seconds = seconds_per_day / total_rewards_per_day if total_rewards_per_day > 0 else None

    return {
        "total_rewards_per_day": round(float(total_rewards_per_day), 3),
        "block_rewards_per_day": round(float(block_rewards_per_day), 3),
        "vote_rewards_per_day": round(float(vote_rewards_per_day), 3),
        "time_between_rewards": format_time_between_rewards(float(time_between_rewards_seconds))
        if time_between_rewards_seconds else "0",
    }

@tasks.loop(seconds=status_change_interval)
async def change_status():
    global bot_state
    try:
        if bot_state.first_run:
            bot_state.first_run = False
            await asyncio.sleep(3) # to prevent restarting too quickly and trigger rate limiting
        #    return       
        
        if not bot_state.status_options:
            logging.warning("status_options is empty, skipping status change.")
            return

        bot_state.status_index = (bot_state.status_index + 1) % len(bot_state.status_options)
        nickname, status_message = bot_state.status_options[bot_state.status_index]

        logging.info(f"Updating status: {nickname} - {status_message}")

        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.custom, name='custom', state=status_message))

        for guild in bot.guilds:
            try:
                current_nickname = bot_state.current_nicknames.get(guild.id)
                if current_nickname != nickname:
                    await guild.me.edit(nick=nickname)
                    await asyncio.sleep(.5)
                    bot_state.current_nicknames[guild.id] = nickname
            except discord.Forbidden:
                logging.warning(f"Permission denied to change nickname in {guild.name}")
            except Exception as e:
                logging.error(f"Error updating nickname in {guild.name}: {e}")

    except Exception as e:
        logging.error(f"Error in change_status loop: {e}")

@bot.event
async def on_ready():
    
    global bot_state
    global logging
    
    if bot_state.first_run:
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
            )
        
        logging.info(f"Logged in as {bot.user}")

        bot.loop.create_task(utility_run())
        change_status.start()

        for guild in bot.guilds:
            try:
                current_nickname = bot_state.current_nicknames.get(guild.id)
                if current_nickname != 'Autobots Roll out!':
                    await guild.me.edit(nick='Autobots Roll out!')
                    await asyncio.sleep(.5)
                    bot_state.current_nicknames[guild.id] = 'Autobots Roll out!'
            except discord.Forbidden:
                logging.warning(f"Permission denied to change nickname in {guild.name}")
            except Exception as e:
                logging.error(f"Error updating nickname in {guild.name}: {e}")
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.custom, name='custom', state='Starting Up...'))
        
        
        

asyncio.run(initialize_database())
bot.run(TOKEN)
