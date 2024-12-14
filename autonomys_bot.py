import discord
import aiohttp
import asyncio
import os
import logging
import gc
import time

from sqlalchemy import select, func
from sqlalchemy.sql import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Float, Integer

from dotenv import load_dotenv
from discord.ext import commands, tasks
from substrateinterface import SubstrateInterface
from contextlib import asynccontextmanager

testnet = False # Monitor testnet or False for mainnet. 

global db_lock
db_lock = asyncio.Lock()

# Load environment variables
load_dotenv()
TOKEN = os.getenv('AUTONOMYS_BOT_TOKEN')
if not TOKEN:
    raise ValueError("Discord bot token is not set in environment variables.")

# Load DB info
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set in environment variables.")

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

# SQLAlchemy setup
# DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,  # Limit pool size
    max_overflow=10,  # Allow up to 10 additional connections
)
async_session = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()
# SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Define the database model
class PledgedHistory(Base):
    __tablename__ = "pledged_history"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(Float, nullable=False, unique=True)
    pledged_space = Column(Float, nullable=False)


async def initialize_database():
    async with engine.begin() as conn:
        try:
            await conn.run_sync(Base.metadata.create_all)
        except Exception as e:
            logging.error(f"Error initializing database: {e}")


async def add_pledged_data(timestamp, pledged_space):
    async with db_lock:  # Prevent concurrent writes
        try:
            async with get_async_session() as session:
                new_entry = PledgedHistory(timestamp=timestamp, pledged_space=pledged_space)
                session.add(new_entry)
                await session.commit()
        except Exception as e:
            logging.error(f"Error inserting data: {e}")


async def prune_old_data(retention_period_seconds=2592000):
    current_time = time.time()
    cutoff_time = current_time - retention_period_seconds
    async with db_lock:  # Prevent concurrent modifications
        async with get_async_session() as session:
            try:
                stmt = delete(PledgedHistory).where(PledgedHistory.timestamp < cutoff_time)
                await session.execute(stmt)
            except Exception as e:
                logging.error(f"Error pruning old data: {e}")


@asynccontextmanager
async def get_async_session():
    """Ensure async session works with the current event loop."""
    async with async_session() as session:
        yield session
        
        
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
                # Fetch constants and update bot state
                constants, block_height = await fetch_constants_and_height()
                total_space_pledged = constants.get("TotalSpacePledged")
                total_circulation = constants.get("CreditSupply")
                blockchain_history_size = constants.get("BlockchainHistorySize")

                bot_state.tot_pledged = calculate_total_pledged(total_space_pledged) if total_space_pledged else 0.0
                blockchain_history_size_gb = blockchain_history_size / (10 ** 9) if blockchain_history_size else 0.0

                bot_state.version, acresvers = await fetch_version_data(session)

                # Generate status options asynchronously
                bot_state.status_options = await generate_status_options(
                    "Total Pledged", bot_state.tot_pledged, bot_state.version, acresvers,
                    blockchain_history_size_gb, block_height, testnet, total_circulation
                )

                # Prune old data in the background
                asyncio.create_task(prune_old_data())
            except Exception as e:
                logging.error(f"Error in utility_run: {e}")

            await asyncio.sleep(data_fetch_interval)


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
    growth = await track_pledged_space_growth(bot_state.tot_pledged)
    chart_growth = f"1: {growth.get('1d', 0):.1f} |3: {growth.get('3d', 0):.1f} |7: {growth.get('7d', 0):.1f}"
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


async def track_pledged_space_growth(tot_pledged):
    current_time = time.time()
    await add_pledged_data(current_time, tot_pledged)

    periods = {
        '1d': 24 * 3600,
        '3d': 3 * 24 * 3600,
        '7d': 7 * 24 * 3600,
    }

    growth = {}
    async with db_lock:  # Ensure safe reads
        async with get_async_session() as session:
            for period_name, period_seconds in periods.items():
                cutoff_time = current_time - period_seconds
                try:
                    stmt = select(
                        func.min(PledgedHistory.pledged_space),
                        func.max(PledgedHistory.pledged_space)
                    ).where(PledgedHistory.timestamp >= cutoff_time)
                    result = await session.execute(stmt)
                    min_val, max_val = result.one_or_none()
                    growth[period_name] = round(max_val - min_val, 3) if min_val is not None and max_val is not None else 0.0
                except Exception as e:
                    logging.error(f"Error calculating growth for {period_name}: {e}")
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
        await initialize_database()
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
        
bot.run(TOKEN)
