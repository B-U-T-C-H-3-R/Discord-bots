import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands
from twitchAPI.twitch import Twitch
import asyncio
from dotenv import load_dotenv
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import sys
import glob
import platform
import socket
import aiohttp

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
ALLOWED_ROLE_IDS = [int(role_id) for role_id in os.getenv('ALLOWED_ROLE_IDS').split(',') if role_id]
ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID'))
GUILD_ID = int(os.getenv('GUILD_ID'))
MAX_LOG_FILES = 7
NOTIFICATION_COOLDOWN = 300  # 5 minutes in seconds
TWITCH_USERNAMES_FILE = "twitch_usernames.json"
MAX_RETRIES = 5
RETRY_DELAY = 30  # seconds
TWITCH_API_TIMEOUT = 10  # seconds
TWITCH_API_RETRIES = 3
MESSAGE_EDIT_COOLDOWN = 300  # 5 minutes between edits for same message 

# Default bot status
DEFAULT_BOT_STATUS = "online"  # online, idle, dnd, invisible
DEFAULT_BOT_ACTIVITY_TYPE = "watching"  # playing, streaming, listening, watching
DEFAULT_BOT_ACTIVITY_NAME = "Twitch streams"

class BotState:
    def __init__(self):
        self.twitch_connected = False
        self.discord_connected = True
        self.connection_retry_count = 0
        self.last_retry_time = None
        self.last_dns_flush = None
        self.backoff_factor = 1
        self.twitch_ip_pool = [
            '151.101.66.167',
            '151.101.194.167',
            '151.101.2.167',
            '151.101.130.167'
        ]
        self.current_twitch_ip = 0
        
    def should_retry(self):
        if self.connection_retry_count >= MAX_RETRIES:
            return False
        if not self.last_retry_time:
            return True
        return (datetime.now() - self.last_retry_time).total_seconds() > min(
            self.backoff_factor * RETRY_DELAY, 
            300  # Max 5 minutes
        )
        
    def increment_backoff(self):
        self.connection_retry_count += 1
        self.backoff_factor *= 2
        self.last_retry_time = datetime.now()
        self.current_twitch_ip = (self.current_twitch_ip + 1) % len(self.twitch_ip_pool)
        
    def reset_backoff(self):
        self.connection_retry_count = 0
        self.backoff_factor = 1
        self.last_retry_time = None

# Initialize logging
def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # File handler
    file_handler = RotatingFileHandler(
        "bot_logs.txt",
        maxBytes=5*1024*1024,  # 5MB
        backupCount=3
    )
    file_handler.setFormatter(formatter)
    
    # Debug file handler
    debug_handler = RotatingFileHandler(
        "bot_debug.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    debug_handler.setFormatter(formatter)
    debug_handler.setLevel(logging.DEBUG)
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(debug_handler)

setup_logging()

# Initialize Discord bot with proper intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
bot.state = BotState()

# Twitch API
twitch = None
last_notification_times = {}
last_stream_info = {}
live_messages = {}
log_upload_enabled = True
last_message_edit_time = {}
message_edit_attempts = {}  # Track message edit attempts for rate limiting

# DNS cache management
original_getaddrinfo = socket.getaddrinfo

def getaddrinfo_with_retry(*args, **kwargs):
    try:
        return original_getaddrinfo(*args, **kwargs)
    except socket.gaierror:
        logging.warning(f"DNS resolution failed for {args[0]}, retrying with direct IP")
        # Try with known IPs as fallback
        if args[0] == 'api.twitch.tv':
            ip = bot.state.twitch_ip_pool[bot.state.current_twitch_ip]
            logging.info(f"Using Twitch IP: {ip}")
            return original_getaddrinfo(ip, args[1], *args[2:], **kwargs)
        elif 'discord.gg' in args[0]:
            return original_getaddrinfo('162.159.135.233', args[1], *args[2:], **kwargs)
        raise

socket.getaddrinfo = getaddrinfo_with_retry

# Load/save Twitch usernames
def load_twitch_usernames():
    if os.path.exists(TWITCH_USERNAMES_FILE):
        try:
            with open(TWITCH_USERNAMES_FILE, "r") as file:
                return json.load(file)
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Failed to load twitch usernames: {e}")
            return []
    return []

def save_twitch_usernames(usernames):
    try:
        with open(TWITCH_USERNAMES_FILE, "w") as file:
            json.dump(usernames, file, indent=2)
    except IOError as e:
        logging.error(f"Failed to save twitch usernames: {e}")

TWITCH_USERNAMES = load_twitch_usernames()

async def make_twitch_request(coro_func, *args, **kwargs):
    """Helper function to make Twitch API requests with retry logic"""
    for attempt in range(TWITCH_API_RETRIES):
        try:
            # For async generators, we need to collect the results
            result = []
            async for item in coro_func(*args, **kwargs):
                result.append(item)
            return result
        except Exception as e:
            if attempt == TWITCH_API_RETRIES - 1:
                raise
            await asyncio.sleep(1 + attempt)  # Exponential backoff
            continue

# Twitch API functions
async def init_twitch():
    global twitch
    try:
        # Initialize Twitch without custom session (newer versions don't support session parameter)
        twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        bot.state.twitch_connected = True
        bot.state.reset_backoff()
        logging.info("Twitch API initialized successfully")
        return True
    except Exception as e:
        logging.error(f"Error initializing Twitch API: {e}")
        bot.state.twitch_connected = False
        return False

async def validate_twitch_user(username):
    try:
        results = await make_twitch_request(twitch.get_users, logins=[username])
        return len(results) > 0
    except Exception as e:
        logging.error(f"Error validating Twitch user {username}: {e}")
        bot.state.twitch_connected = False
        await handle_connection_error()
        return False

async def is_user_live(username):
    try:
        # First get user ID
        users = await make_twitch_request(twitch.get_users, logins=[username])
        if not users:
            return {'is_live': False, 'error': 'User not found'}
        
        user_id = users[0].id
        
        # Check stream status
        streams = await make_twitch_request(twitch.get_streams, user_id=[user_id])
        if not streams:
            return {'is_live': False}
        
        stream_info = streams[0]
        
        # Get game info
        game_name = "Unknown Game"
        if stream_info.game_id:
            games = await make_twitch_request(twitch.get_games, game_ids=[stream_info.game_id])
            if games:
                game_name = games[0].name
        
        return {
            'is_live': True,
            'title': stream_info.title,
            'game': game_name,
            'viewers': stream_info.viewer_count,
            'thumbnail': stream_info.thumbnail_url
        }
    except Exception as e:
        logging.error(f"Error checking if {username} is live: {e}")
        bot.state.twitch_connected = False
        return {'is_live': False, 'error': str(e)}

def create_stream_embed(username, stream_info, is_live=True):
    if is_live:
        # Create a clean, modern embed for live streams
        embed = discord.Embed(
            title=stream_info['title'],
            color=discord.Color(0x9146FF),  # Twitch purple color
            url=f"https://twitch.tv/{username}"
        )
        
        # Add the thumbnail as the main image (not as a thumbnail)
        if stream_info['thumbnail']:
            thumbnail_url = stream_info['thumbnail'].format(width=1280, height=720)
            embed.set_image(url=thumbnail_url)
        
        # Add only game field (remove viewer count)
        embed.add_field(
            name="Game", 
            value=stream_info['game'], 
            inline=True
        )
        
        # Add live status indicator
        embed.add_field(
            name="Status", 
            value="🔴 LIVE", 
            inline=True
        )
        
        # Add the streamer information
        embed.set_author(
            name=f"{username} is live on Twitch!",
            url=f"https://twitch.tv/{username}",
            icon_url="https://cdn3.iconfinder.com/data/icons/social-media-2068/64/_Twitch-512.png"  # Twitch icon
        )
        
        # Add footer with timestamp
        embed.timestamp = datetime.now()
        embed.set_footer(text="Live Now • Twitch")
        
        return embed
    else:
        # Create an embed for offline status
        embed = discord.Embed(
            title="Stream Ended",
            description=f"{username}'s stream has ended",
            color=discord.Color.darker_grey(),
            url=f"https://twitch.tv/{username}"
        )
        
        # Add offline status indicator
        embed.add_field(
            name="Status", 
            value="⚫ OFFLINE", 
            inline=True
        )
        
        embed.set_author(
            name=f"{username} was live on Twitch",
            url=f"https://twitch.tv/{username}",
            icon_url="https://cdn3.iconfinder.com/data/icons/social-media-2068/64/_Twitch-512.png"
        )
        
        embed.set_footer(text="Stream Ended • Twitch")
        return embed

async def safe_message_edit(msg, **kwargs):
    """Safely edit a message with retry logic and rate limit tracking"""
    message_id = msg.id
    current_time = datetime.now().timestamp()
    
    # Check if we've attempted to edit this message recently and failed due to rate limits
    last_attempt = message_edit_attempts.get(message_id, {}).get('last_attempt', 0)
    attempt_count = message_edit_attempts.get(message_id, {}).get('count', 0)
    
    # If we've had multiple recent failures, wait longer before retrying
    if attempt_count > 3 and current_time - last_attempt < 300:  # 5 minutes cooldown after multiple failures
        logging.debug(f"Skipping edit for message {message_id} due to multiple recent failures")
        return False
    
    for attempt in range(3):
        try:
            await msg.edit(**kwargs)
            # Reset attempt counter on success
            message_edit_attempts[message_id] = {'count': 0, 'last_attempt': current_time}
            return True
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = e.retry_after if hasattr(e, 'retry_after') else (attempt + 1) * 5
                logging.warning(f"Rate limited editing message {message_id}, retrying in {retry_after}s (attempt {attempt + 1}/3)")
                
                # Track this attempt
                message_edit_attempts[message_id] = {
                    'count': attempt_count + 1,
                    'last_attempt': current_time
                }
                
                await asyncio.sleep(retry_after)
            elif e.status == 503:  # Service unavailable
                logging.warning(f"Service unavailable, retrying in 10s (attempt {attempt + 1}/3)")
                await asyncio.sleep(10)
            else:
                logging.error(f"Failed to edit message after {attempt + 1} attempts: {e}")
                return False
        except Exception as e:
            logging.error(f"Unexpected error editing message: {e}")
            return False
    return False

async def safe_message_send(channel, *args, **kwargs):
    """Safely send a message with retry logic"""
    for attempt in range(3):
        try:
            msg = await channel.send(*args, **kwargs)
            return msg
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = e.retry_after if hasattr(e, 'retry_after') else (attempt + 1) * 2
                logging.warning(f"Rate limited sending message, retrying in {retry_after}s (attempt {attempt + 1}/3)")
                await asyncio.sleep(retry_after)
            elif e.status == 503:  # Service unavailable
                logging.warning(f"Service unavailable, retrying in 5s (attempt {attempt + 1}/3)")
                await asyncio.sleep(5)
            else:
                logging.error(f"Failed to send message after {attempt + 1} attempts: {e}")
                return None
        except Exception as e:
            logging.error(f"Unexpected error sending message: {e}")
            return None
    return None

# Stream monitoring - Modified to edit notifications for game changes
async def check_live_status():
    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    
    while not bot.is_closed():
        try:
            for username in TWITCH_USERNAMES:
                if not username:
                    continue
                
                current_time = datetime.now().timestamp()
                last_notified = last_notification_times.get(username, 0)
                
                # Skip if in cooldown period
                if current_time - last_notified < NOTIFICATION_COOLDOWN and last_stream_info.get(username, {}).get('is_live', False):
                    continue
                
                stream_info = await is_user_live(username)
                
                if stream_info.get('error'):
                    logging.error(f"Error checking {username}: {stream_info['error']}")
                    continue
                
                if stream_info['is_live']:
                    current_stream_key = f"{username}_{stream_info['title']}_{stream_info['game']}"
                    previous_stream_key = last_stream_info.get(username, {}).get('key', '')
                    
                    # Check if this is a new stream, game change, or title change
                    is_new_stream = not last_stream_info.get(username, {}).get('is_live', False)
                    is_game_change = (not is_new_stream and 
                                    last_stream_info.get(username, {}).get('game') != stream_info['game'])
                    is_title_change = (not is_new_stream and 
                                     last_stream_info.get(username, {}).get('title') != stream_info['title'])
                    
                    # New stream or significant change
                    if is_new_stream or is_game_change or is_title_change:
                        embed = create_stream_embed(username, stream_info, is_live=True)
                        message_content = f"@everyone **{username}** is live on Twitch!"
                        
                        if username in live_messages and not is_new_stream:
                            try:
                                # Edit existing message for game/title changes
                                msg = await channel.fetch_message(live_messages[username])
                                success = await safe_message_edit(msg, content=message_content, embed=embed)
                                if success:
                                    logging.info(f"Updated stream notification for {username} (game/title change)")
                                else:
                                    logging.warning(f"Failed to edit message for {username} after multiple attempts")
                                    continue
                            except discord.NotFound:
                                # Message was deleted, send new one
                                msg = await safe_message_send(channel, message_content, embed=embed)
                                if msg:
                                    live_messages[username] = msg.id
                                else:
                                    logging.warning(f"Failed to send new message for {username}")
                                    continue
                            except Exception as e:
                                logging.error(f"Unexpected error handling message for {username}: {e}")
                                continue
                        else:
                            # Send new message for new streams
                            msg = await safe_message_send(channel, message_content, embed=embed)
                            if msg:
                                live_messages[username] = msg.id
                            else:
                                logging.warning(f"Failed to send initial message for {username}")
                                continue
                        
                        last_notification_times[username] = current_time
                        last_stream_info[username] = {
                            'is_live': True,
                            'key': current_stream_key,
                            'title': stream_info['title'],
                            'game': stream_info['game']
                        }
                        
                        if is_new_stream:
                            logging.info(f"Announced live stream for {username}")
                        elif is_game_change:
                            logging.info(f"Updated game for {username}: {stream_info['game']}")
                        elif is_title_change:
                            logging.info(f"Updated title for {username}")
                else:
                    # Stream is offline - edit the existing message to show offline status
                    if username in live_messages and last_stream_info.get(username, {}).get('is_live', False):
                        try:
                            msg = await channel.fetch_message(live_messages[username])
                            offline_embed = create_stream_embed(username, last_stream_info[username], is_live=False)
                            
                            # Edit the existing message to show offline status
                            success = await safe_message_edit(
                                msg, 
                                content=f"{username} is now offline", 
                                embed=offline_embed
                            )
                            
                            if success:
                                logging.info(f"Updated stream status to offline for {username}")
                            else:
                                logging.warning(f"Failed to update offline status for {username}")
                                
                        except discord.NotFound:
                            # Message was deleted, remove from tracking
                            if username in live_messages:
                                del live_messages[username]
                        except Exception as e:
                            logging.error(f"Failed to update offline status for {username}: {e}")
                    
                    last_stream_info[username] = {'is_live': False}
            
            await asyncio.sleep(30)
        
        except Exception as e:
            logging.error(f"Error in check_live_status: {e}")
            await handle_connection_error()

async def handle_connection_error():
    if not bot.state.should_retry():
        logging.error("Max retries reached. Performing full restart...")
        await restart_bot()
        return
    
    bot.state.increment_backoff()
    wait_time = min(bot.state.backoff_factor * RETRY_DELAY, 300)  # Max 5 minutes
    
    logging.error(f"Connection error detected. Waiting {wait_time} seconds before retry... (Attempt {bot.state.connection_retry_count}/{MAX_RETRIES})")
    bot.state.last_retry_time = datetime.now()
    await asyncio.sleep(wait_time)
    
    try:
        # Try flushing DNS cache
        if platform.system().lower() in ['linux', 'darwin']:  # Linux and MacOS
            os.system('resolvectl flush-caches || sudo dscacheutil -flushcache || sudo killall -HUP mDNSResponder')
        elif platform.system().lower() == 'windows':
            os.system('ipconfig /flushdns')
            
        if not await init_twitch():
            raise Exception("Failed to initialize Twitch API")
        
        if bot.is_closed():
            await bot.start(DISCORD_TOKEN)
        
        bot.state.reset_backoff()
        bot.state.twitch_connected = True
        bot.state.discord_connected = True
    except Exception as e:
        await handle_connection_error()

async def restart_bot():
    logging.info("Restarting bot...")
    python = sys.executable
    os.execl(python, python, *sys.argv)

# Connection health check
@tasks.loop(minutes=5)
async def check_connections():
    try:
        if not bot.state.twitch_connected:
            logging.info("Attempting to reconnect to Twitch API...")
            await init_twitch()
        if not bot.state.discord_connected and bot.is_closed():
            logging.info("Attempting to reconnect to Discord...")
            await bot.start(DISCORD_TOKEN)
    except Exception as e:
        logging.error(f"Error in connection health check: {e}")

# Keep connection alive
@tasks.loop(minutes=1)
async def keep_alive_ping():
    try:
        if bot.is_ws_ratelimited():
            logging.warning("WebSocket is rate limited, reducing activity")
        # Just accessing this property sends a ping
        latency = bot.latency
        if latency > 1.0:  # High latency warning
            logging.warning(f"High Discord latency: {latency:.2f}s")
    except Exception as e:
        logging.warning(f"Keep-alive ping failed: {e}")

# Log management
def clean_up_logs():
    try:
        log_files = glob.glob("bot_logs_*.txt")
        log_files.sort()
        
        if len(log_files) > MAX_LOG_FILES:
            for old_log in log_files[:-MAX_LOG_FILES]:
                os.remove(old_log)
                logging.info(f"Removed old log file: {old_log}")
    except Exception as e:
        logging.error(f"Error cleaning up logs: {e}")

async def upload_logs():
    if not log_upload_enabled:
        return
    
    clean_up_logs()
    current_date = datetime.now().strftime("%Y-%m-%d")
    log_file_name = f"bot_logs_{current_date}.txt"
    
    # Close current file handler
    for handler in logging.root.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            logging.root.removeHandler(handler)
    
    if os.path.exists("bot_logs.txt"):
        os.rename("bot_logs.txt", log_file_name)
        
        # Reinitialize logging
        file_handler = logging.FileHandler("bot_logs.txt")
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logging.root.addHandler(file_handler)
        
        # Upload log
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                with open(log_file_name, "rb") as log_file:
                    await log_channel.send(file=discord.File(log_file, log_file_name))
                logging.info(f"Uploaded log file {log_file_name}")
            except Exception as e:
                logging.error(f"Failed to upload logs: {e}")

@tasks.loop(hours=24)
async def schedule_log_upload():
    await upload_logs()

# Message edit attempts cleanup
@tasks.loop(hours=1)
async def clean_message_attempts():
    """Clean up old entries from message_edit_attempts"""
    current_time = datetime.now().timestamp()
    keys_to_remove = []
    
    for message_id, data in message_edit_attempts.items():
        if current_time - data.get('last_attempt', 0) > 3600:  # 1 hour
            keys_to_remove.append(message_id)
    
    for key in keys_to_remove:
        del message_edit_attempts[key]
    
    logging.debug(f"Cleaned up {len(keys_to_remove)} old message edit attempts")

# Command checks
def has_allowed_role(interaction: discord.Interaction) -> bool:
    return any(role.id in ALLOWED_ROLE_IDS for role in interaction.user.roles)

def is_allowed_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == ALLOWED_CHANNEL_ID

async def safe_response(interaction: discord.Interaction, *args, **kwargs):
    """Safely respond to an interaction, handling cases where it might have expired."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await interaction.response.send_message(*args, **kwargs)
    except discord.NotFound:
        logging.warning(f"Interaction not found for command by {interaction.user}")
    except Exception as e:
        logging.error(f"Error responding to interaction: {e}")

# Unified ConfirmView class - Modified to delete message after use or timeout
class ConfirmView(discord.ui.View):
    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self.value = None
        self.message = None

    async def on_timeout(self):
        # Delete the message when timeout occurs
        if self.message:
            try:
                await self.message.delete()
            except:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.message.interaction.user:
            await interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
            return
        self.value = True
        # Delete the message after confirmation
        try:
            await self.message.delete()
        except:
            pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.message.interaction.user:
            await interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
            return
        self.value = False
        # Delete the message after cancellation
        try:
            await self.message.delete()
        except:
            pass
        self.stop()

# Bot commands 
@bot.tree.command(name="toggle_log_upload", description="Turn log upload on/off", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def toggle_log_upload(interaction: discord.Interaction):
    global log_upload_enabled
    status = "enabled" if not log_upload_enabled else "disabled"
    
    view = ConfirmView(timeout=30)
    await interaction.response.send_message(
        f"Are you sure you want to turn log upload {status}?",
        view=view,
        ephemeral=True
    )
    view.message = await interaction.original_response()
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Toggle cancelled (timed out).", ephemeral=True)
    elif view.value:
        log_upload_enabled = not log_upload_enabled
        await interaction.followup.send(f"Log upload is now {'enabled' if log_upload_enabled else 'disabled'}.", ephemeral=True)
        logging.info(f"{interaction.user} toggled log upload to {'enabled' if log_upload_enabled else 'disabled'}")
    else:
        await interaction.followup.send("Toggle cancelled.", ephemeral=True)

@bot.tree.command(name="add_twitch_user", description="Add a Twitch user to monitor", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(username="The Twitch username to add")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def add_twitch_user(interaction: discord.Interaction, username: str):
    # Validate username format
    if not re.match(r'^[a-zA-Z0-9_]{4,25}$', username):
        await interaction.response.send_message(
            "Invalid Twitch username format (4-25 chars, alphanumeric + underscores)",
            ephemeral=True
        )
        return
    
    if username in TWITCH_USERNAMES:
        await interaction.response.send_message(
            f"{username} is already being monitored: https://twitch.tv/{username}",
            ephemeral=True
        )
        return
    
    # Validate user exists on Twitch
    await interaction.response.defer(ephemeral=True)
    if not await validate_twitch_user(username):
        await interaction.followup.send(
            f"Could not find Twitch user {username}. Please check the spelling.",
            ephemeral=True
        )
        return
    
    view = ConfirmView(timeout=30)
    message = await interaction.followup.send(
        f"Are you sure you want to add {username} to monitoring?\nhttps://twitch.tv/{username}",
        view=view,
        ephemeral=True
    )
    view.message = message
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Add user cancelled (timed out).", ephemeral=True)
    elif view.value:
        TWITCH_USERNAMES.append(username)
        save_twitch_usernames(TWITCH_USERNAMES)
        await interaction.followup.send(
            f"Added {username} to monitoring list: https://twitch.tv/{username}",
            ephemeral=True
        )
        logging.info(f"{interaction.user} added {username} to monitoring")
    else:
        await interaction.followup.send("Add user cancelled.", ephemeral=True)

@bot.tree.command(name="remove_twitch_user", description="Remove a Twitch user from monitoring", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(username="The Twitch username to remove")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def remove_twitch_user(interaction: discord.Interaction, username: str):
    if username not in TWITCH_USERNAMES:
        await interaction.response.send_message(
            f"{username} is not being monitored: https://twitch.tv/{username}",
            ephemeral=True
        )
        return
    
    view = ConfirmView(timeout=30)
    await interaction.response.send_message(
        f"Are you sure you want to remove {username} from monitoring?\nhttps://twitch.tv/{username}",
        view=view,
        ephemeral=True
    )
    view.message = await interaction.original_response()
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Remove user cancelled (timed out).", ephemeral=True)
    elif view.value:
        TWITCH_USERNAMES.remove(username)
        save_twitch_usernames(TWITCH_USERNAMES)
        await interaction.followup.send(
            f"Removed {username} from monitoring list: https://twitch.tv/{username}",
            ephemeral=True
        )
        logging.info(f"{interaction.user} removed {username} from monitoring")
    else:
        await interaction.followup.send("Remove user cancelled.", ephemeral=True)

@bot.tree.command(name="list_twitch_users", description="List monitored Twitch users", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def list_twitch_users(interaction: discord.Interaction):
    if not TWITCH_USERNAMES:
        await interaction.response.send_message("No users are currently being monitored.", ephemeral=True)
        return
    
    # Split users into pages of 10
    users_per_page = 10
    pages = []
    for i in range(0, len(TWITCH_USERNAMES), users_per_page):
        page_users = TWITCH_USERNAMES[i:i + users_per_page]
        pages.append(page_users)
    
    current_page = 0
    total_pages = len(pages)
    
    class PaginationView(discord.ui.View):
        def __init__(self, timeout=60):
            super().__init__(timeout=timeout)
            self.current_page = current_page
            self.total_pages = total_pages
            self.message = None
        
        async def on_timeout(self):
            # Delete the message when timeout occurs
            if self.message:
                try:
                    await self.message.delete()
                except:
                    pass
        
        def create_embed(self):
            embed = discord.Embed(
                title=f"Monitored Twitch Channels (Page {self.current_page + 1}/{self.total_pages})",
                description=f"Currently monitoring {len(TWITCH_USERNAMES)} Twitch channels:",
                color=discord.Color.purple()
            )
            
            for username in pages[self.current_page]:
                embed.add_field(
                    name=username,
                    value=f"[twitch.tv/{username}](https://twitch.tv/{username})",
                    inline=False
                )
            
            return embed
        
        @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary, disabled=True)
        async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != self.message.interaction.user:
                await interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            
            self.current_page -= 1
            
            # Update button states
            self.previous_button.disabled = self.current_page == 0
            self.next_button.disabled = self.current_page == self.total_pages - 1
            
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        
        @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary, disabled=total_pages <= 1)
        async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != self.message.interaction.user:
                await interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            
            self.current_page += 1
            
            # Update button states
            self.previous_button.disabled = self.current_page == 0
            self.next_button.disabled = self.current_page == self.total_pages - 1
            
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        
        @discord.ui.button(label="❌", style=discord.ButtonStyle.danger)
        async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != self.message.interaction.user:
                await interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            
            # Delete the entire message
            try:
                await self.message.delete()
            except:
                pass
            self.stop()
    
    view = PaginationView(timeout=60)
    
    # Send the first page
    embed = view.create_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()
    
    logging.info(f"{interaction.user} listed monitored users (paginated)")

@bot.tree.command(name="set_twitch_bot_status", description="Change the bot's status", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(status="The status to set (online, idle, dnd, invisible)")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def set_twitch_bot_status(interaction: discord.Interaction, status: str):
    status = status.lower()
    valid_statuses = ["online", 'idle', 'dnd', 'invisible']
    if status not in valid_statuses:
        await interaction.response.send_message(f"Invalid status. Valid options are: {', '.join(valid_statuses)}", ephemeral=True)
        return
    
    view = ConfirmView(timeout=30)
    await interaction.response.send_message(
        f"Are you sure you want to change bot status to {status}?", 
        view=view, 
        ephemeral=True
    )
    view.message = await interaction.original_response()
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Status change cancelled (timed out).", ephemeral=True)
    elif view.value:
        await bot.change_presence(status=discord.Status[status])
        await interaction.followup.send(f"Bot status changed to {status}.", ephemeral=True)
        logging.info(f"{interaction.user} changed bot status to {status}")
    else:
        await interaction.followup.send("Status change cancelled.", ephemeral=True)

@bot.tree.command(name="set_twitch_bot_activity", description="Change the bot's activity", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(activity_type="The type of activity (playing, streaming, listening, watching)")
@app_commands.describe(activity_name="The name of the activity")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def set_twitch_bot_activity(interaction: discord.Interaction, activity_type: str, activity_name: str):
    activity_type = activity_type.lower()
    valid_activities = ["playing", "streaming", "listening", "watching"]
    if activity_type not in valid_activities:
        await interaction.response.send_message(f"Invalid activity type. Valid options are: {', '.join(valid_activities)}", ephemeral=True)
        return
    
    view = ConfirmView(timeout=30)
    await interaction.response.send_message(
        f"Are you sure you want to change bot activity to {activity_type} {activity_name}?", 
        view=view, 
        ephemeral=True
    )
    view.message = await interaction.original_response()
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Activity change cancelled (timed out).", ephemeral=True)
    elif view.value:
        if activity_type == "playing":
            activity = discord.Game(name=activity_name)
        elif activity_type == "streaming":
            activity = discord.Streaming(name=activity_name, url="https://twitch.tv/example")
        elif activity_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=activity_name)
        elif activity_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=activity_name)
        
        await bot.change_presence(activity=activity)
        await interaction.followup.send(f"Bot activity changed to {activity_type} {activity_name}.", ephemeral=True)
        logging.info(f"{interaction.user} changed bot activity to {activity_type} {activity_name}")
    else:
        await interaction.followup.send("Activity change cancelled.", ephemeral=True)

@bot.tree.command(name="clear_twitch_bot_activity", description="Clear the bot's current activity", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def clear_twitch_bot_activity(interaction: discord.Interaction):
    view = ConfirmView(timeout=60)
    await interaction.response.send_message(
        "Are you sure you want to clear the bot's activity?", 
        view=view, 
        ephemeral=True
    )
    view.message = await interaction.original_response()
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Clear activity cancelled (timed out).", ephemeral=True)
    elif view.value:
        await bot.change_presence(activity=None)
        await interaction.followup.send("Bot activity cleared.", ephemeral=True)
        logging.info(f"{interaction.user} cleared bot activity")
    else:
        await interaction.followup.send("Clear activity cancelled.", ephemeral=True)

@bot.tree.command(name="check_new_twitch_live_stream", description="Manually check for live streams", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def check_new_twitch_live_stream(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    live_users = []

    # Check all users first
    for username in TWITCH_USERNAMES:
        stream_info = await is_user_live(username)
        if stream_info.get('is_live'):
            live_users.append((username, stream_info))

    if not live_users:
        await interaction.followup.send("No users are currently live.", ephemeral=True)
        return

    # Process each live user individually
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    posted_users = []
    
    for username, stream_info in live_users:
        class UserConfirmView(discord.ui.View):
            def __init__(self, timeout=30):
                super().__init__(timeout=timeout)
                self.value = None
                self.message = None

            async def on_timeout(self):
                # Delete the message when timeout occurs
                if self.message:
                    try:
                        await self.message.delete()
                    except:
                        pass

            @discord.ui.button(label="Post", style=discord.ButtonStyle.green)
            async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = True
                # Delete the message after confirmation
                try:
                    await self.message.delete()
                except:
                    pass
                self.stop()

            @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
            async def skip(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = False
                # Delete the message after skipping
                try:
                    await self.message.delete()
                except:
                    pass
                self.stop()

            @discord.ui.button(label="Cancel All", style=discord.ButtonStyle.red)
            async def cancel_all(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = None
                # Delete the message after cancellation
                try:
                    await self.message.delete()
                except:
                    pass
                self.stop()

        # Create embed for this user
        user_embed = discord.Embed(
            title=f"Live Stream Detected - {username}",
            color=discord.Color(0x9146FF)
        )
        user_embed.add_field(name="Title", value=stream_info['title'], inline=False)
        user_embed.add_field(name="Game", value=stream_info['game'], inline=True)
        user_embed.add_field(name="Status", value="🔴 LIVE", inline=True)
        user_embed.set_author(
            name=f"{username} is live on Twitch!",
            url=f"https://twitch.tv/{username}",
            icon_url="https://cdn3.iconfinder.com/data/icons/social-media-2068/64/_极itch-512.png"
        )
        
        view = UserConfirmView(timeout=30)
        message = await interaction.followup.send(
            f"**{username}** is live!\nPost notification to channel?",
            embed=user_embed,
            view=view,
            ephemeral=True
        )
        view.message = message

        await view.wait()

        if view.value is None:  # Cancel All clicked or timeout
            await interaction.followup.send("Stream check cancelled.", ephemeral=True)
            break
        elif view.value:  # Post clicked
            try:
                main_embed = create_stream_embed(username, stream_info, is_live=True)
                await channel.send(f"@everyone **{username}** is live on Twitch!", embed=main_embed)
                posted_users.append(username)
                logging.info(f"Manually posted live stream for {username}")
            except Exception as e:
                logging.error(f"Failed to post notification for {username}: {e}")
                await interaction.followup.send(f"Failed to post notification for {username}.", ephemeral=True)
        # If Skip was clicked, just continue to next user

    # Send summary
    if posted_users:
        summary = f"Posted notifications for: {', '.join(posted_users)}"
        await interaction.followup.send(summary, ephemeral=True)
    else:
        await interaction.followup.send("No notifications were posted.", ephemeral=True)

@bot.tree.command(name="twitch_bot_help", description="Display all commands and how to use them", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def twitch_bot_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Bot Commands",
        description="Here are all the available commands and how to use them:",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="1. **/add_twitch_user**",
        value="Add a Twitch user to the monitoring list.\n**Usage:** `/add_twitch_user username:<Twitch username>`",
        inline=False
    )
    embed.add_field(
        name="2. **/remove_twitch_user**",
        value="Remove a Twitch user from the monitoring list.\n**Usage:** `/remove_twitch_user username:<Twitch username>`",
        inline=False
    )
    embed.add_field(
        name="3. **/list_twitch_users**",
        value="List all monitored Twitch users.\n**Usage:** `/极tch_users`",
        inline=False
    )
    embed.add_field(
        name="4. **/set_twitch_bot_status**",
        value="Change the bot's status (online, idle, dnd, invisible).\n**Usage:** `/set_twitch_bot_status status:<status>`",
        inline=False
    )
    embed.add_field(
        name="5. **/set_twitch_bot_activity**",
        value="Change the bot's activity (playing, streaming, listening, watching).\极**Usage:** `/set_twitch_bot_activity activity_type:<type> activity_name:<name>`",
        inline=False
    )
    embed.add_field(
        name="6. **/clear_twitch_bot_activity**",
        value="Clear the bot's current activity.\n**Usage:** `/clear_twitch_bot_activity`",
        inline=False
    )
    embed.add_field(
        name="7. **/check_new_twitch_live_stream**",
        value="Manually check for live streams of monitored users.\n**Usage:** `/check_new_twitch_live_stream`",
        inline=False
    )
    embed.add_field(
        name="8. **/twitch_bot_help**",
        value="Display all commands and how to use them.\n**Usage:** `/twitch_bot_help`",
        inline=False
    )
    embed.add_field(
        name="9. **/toggle_log_upload**",
        value="Turn on or off the log upload feature.\n**Usage:** `/toggle_log_upload`",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Bot events
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name}")
    
    # Set default bot status and activity
    try:
        # Set status
        await bot.change_presence(status=discord.Status[DEFAULT_BOT_STATUS])
        
        # Set activity based on type
        if DEFAULT_BOT_ACTIVITY_TYPE == "playing":
            activity = discord.Game(name=DEFAULT_BOT_ACTIVITY_NAME)
        elif DEFAULT_BOT_ACTIVITY_TYPE == "streaming":
            activity = discord.Streaming(name=DEFAULT_BOT_ACTIVITY_NAME, url="https://twitch.tv")
        elif DEFAULT_BOT_ACTIVITY_TYPE == "极ening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=DEFAULT_BOT_ACTIVITY_NAME)
        elif DEFAULT_BOT_ACTIVITY_TYPE == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=DEFAULT_BOT_ACTIVITY_NAME)
        else:
            activity = discord.Activity(type=discord.ActivityType.watching, name=DEFAULT_BOT_ACTIVITY_NAME)
        
        await bot.change_presence(activity=activity)
        logging.info(f"Set default bot status: {DEFAULT_BOT_STATUS}, activity: {DEFAULT_BOT_ACTIVITY_TYPE} {DEFAULT_BOT_ACTIVITY_NAME}")
    except Exception as e:
        logging.error(f"Failed to set default bot status: {e}")
    
    # Initialize Twitch connection
    if not await init_twitch():
        await handle_connection_error()
    
    # Start monitoring tasks if they're not already running
    if not hasattr(bot, 'live_status_task') or bot.l极_status_task.done():
        bot.live_status_task = bot.loop.create_task(check_live_status())
    
    # Start health check task
    if not check_connections.is_running():
        check_connections.start()
    
    # Start keep-alive ping
    if not keep_alive_ping.is_running():
        keep_alive_ping.start()
    
    # Start log upload task if not running
    if not schedule_log_upload.is_running():
        schedule_log_upload.start()
    
    # Start message attempts cleanup task
    if not clean_message_attempts.is_running():
        clean_message_attempts.start()
    
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        logging.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

@bot.event
async def setup_hook():
    # Initial sync of commands
    try:
        guild = discord.Object(id=GUILD_ID)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        logging.error(f"Error in setup_hook: {e}")

@bot.event
async def on_disconnect():
    bot.state.discord_connected = False
    logging.warning("Disconnected from Discord")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not has_allowed_role(interaction):
            await safe_response(interaction, "You don't have permission to use this command.", ephemeral=True)
            logging.warning(f"{interaction.user} tried to use command without permission")
        elif not is_allowed_channel(interaction):
            await safe_response(interaction, "This command can only be used in the designated channel.", ephemeral=True)
            logging.warning(f"{interaction.user} tried to use command in wrong channel")
    else:
        await safe_response(interaction, f"An error occurred: {error}", ephemeral=True)
        logging.error(f"Command error: {error}")

# Windows compatibility
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Start bot
try:
    bot.run(DISCORD_TOKEN)
except Exception as e:
    logging.error(f"Bot crashed: {e}")
    asyncio.run(asyncio.sleep(60))
    python = sys.executable
    os.execl(python, python, *sys.argv)