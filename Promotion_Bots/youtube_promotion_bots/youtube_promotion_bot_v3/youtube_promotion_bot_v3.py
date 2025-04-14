# Read the Read_me.txt before proceeding with the code
import asyncio
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import discord
from discord.ext import commands, tasks
from discord import app_commands, Status, Activity, ActivityType
import asyncio
import os
import json
from dotenv import load_dotenv
import feedparser  # For parsing RSS feeds
import logging
from logging.handlers import RotatingFileHandler
import sys
import requests  # For making HTTP requests to the YouTube API
from datetime import datetime
from ratelimit import limits, sleep_and_retry
import time

# Load environment variables
load_dotenv()

# Debug: Print environment variables
print(f"DISCORD_TOKEN: {os.getenv('DISCORD_TOKEN')}")
print(f"GUILD_ID: {os.getenv('GUILD_ID')}")
print(f"DISCORD_CHANNEL_ID: {os.getenv('DISCORD_CHANNEL_ID')}")
print(f"COMMANDS_CHANNEL_ID: {os.getenv('COMMANDS_CHANNEL_ID')}")
print(f"LOG_CHANNEL_ID: {os.getenv('LOG_CHANNEL_ID')}")

# Load multiple YouTube API keys from the environment
YOUTUBE_API_KEYS = os.getenv("YOUTUBE_API_KEYS", "").split(",")
if not YOUTUBE_API_KEYS or not all(YOUTUBE_API_KEYS):
    print("Error: No valid YouTube API keys found in the .env file.")
    sys.exit(1)

# Track the current API key index
current_api_key_index = 0

# Global variable to track if daily logs are enabled
daily_logs_enabled = True

# Bot start time for uptime tracking
BOT_START_TIME = datetime.now()

# Validate required environment variables
required_env_vars = ["DISCORD_TOKEN", "GUILD_ID", "DISCORD_CHANNEL_ID", "COMMANDS_CHANNEL_ID", "LOG_CHANNEL_ID"]
for var in required_env_vars:
    if not os.getenv(var):
        print(f"Error: {var} is not set in the .env file.")
        sys.exit(1)

# Discord bot setup
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
COMMANDS_CHANNEL_ID = int(os.getenv("COMMANDS_CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

# Get the directory of the script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# File to store monitored channels
MONITORED_CHANNEL_FILE = os.path.join(SCRIPT_DIR, "monitored_channels.json")

# Configuration file for allowed roles
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

# Log file in a writable directory
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "bot_activity.log")

# Create the logs directory if it doesn't exist
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Load configuration from file
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {}

config = load_config()
allowed_roles = config.get("allowed_roles", {})

# Set up logging with UTF-8 encoding
def setup_logging():
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # File handler with UTF-8 encoding
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(log_formatter)

    # Console handler with UTF-8 encoding
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)

    # Configure the root logger
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler]
    )

# Call the setup_logging function at the start of your script
setup_logging()
log = logging.getLogger(__name__)

# Log actions to a file
def log_action(action: str, user: str = None):
    if user:
        log.info(f"{user} - {action}")
    else:
        log.info(action)

# Load monitored channels from file
def load_monitored_channels():
    if os.path.exists(MONITORED_CHANNEL_FILE):
        with open(MONITORED_CHANNEL_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {}

# Save monitored channels to file
def save_monitored_channels():
    with open(MONITORED_CHANNEL_FILE, "w", encoding="utf-8") as file:
        json.dump(monitored_channels, file, indent=4)

# Store monitored YouTube channels and their last 10 videos/streams
monitored_channels = load_monitored_channels()  # Format: {youtube_channel_id: {"name": "Channel Name", "search_term": "Search Term", "videos": [], "streams": []}}

# Custom bot client
class Client(commands.Bot):
    async def on_ready(self):
        log.info(f'Logged in as {self.user}!')
        log_action(f"Bot logged in as {self.user}")

        try:
            # Sync commands to the specific guild
            guild = discord.Object(id=GUILD_ID)  # GUILD_ID is passed as an integer
            synced = await self.tree.sync(guild=guild)
            log.info(f'Synced {len(synced)} commands to guild {guild.id}')
            log_action(f"Synced {len(synced)} commands to guild {guild.id}")
        except Exception as e:
            log.error(f'Error syncing commands: {e}')
            log_action(f"Error syncing commands: {e}")

        # Set default status showing monitored channels count
        await self.update_monitored_count_status()
        
        # Start the YouTube monitoring task
        check_youtube.start()

        # Start the daily log task
        send_daily_log.start()

    async def update_monitored_count_status(self):
        """Update the bot's status to show monitored channels count"""
        channel_count = len(monitored_channels)
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{channel_count} YouTube channels"
        )
        await self.change_presence(activity=activity)

# Set up intents and initialize the bot
intents = discord.Intents.default()
intents.message_content = True
client = Client(command_prefix="/", intents=intents)

# Function to rotate YouTube API keys
def rotate_api_key():
    global current_api_key_index
    current_api_key_index = (current_api_key_index + 1) % len(YOUTUBE_API_KEYS)
    log.info(f"Rotated to YouTube API key index: {current_api_key_index}")
    return YOUTUBE_API_KEYS[current_api_key_index]

# Rate-limited function to verify if a video is a live stream using YouTube API
@sleep_and_retry
@limits(calls=10, period=1)  # 10 requests per second
def verify_live_stream(video_id: str) -> bool:
    global current_api_key_index
    max_retries = len(YOUTUBE_API_KEYS)  # Maximum retries based on the number of keys

    for _ in range(max_retries):
        try:
            # Get the current API key
            api_key = YOUTUBE_API_KEYS[current_api_key_index]

            # Make a request to the YouTube Data API
            url = f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}&key={api_key}"
            response = requests.get(url)
            data = response.json()

            # Check for quota exhaustion error
            if "error" in data:
                error_message = data["error"].get("message", "")
                if "quotaExceeded" in error_message:
                    log.warning(f"Quota exceeded for API key index {current_api_key_index}. Rotating to the next key.")
                    rotate_api_key()
                    continue  # Retry with the next key

            # Check if the video has live streaming details
            if "items" in data and data["items"]:
                live_details = data["items"][0].get("liveStreamingDetails", {})
                if live_details:
                    # Check if the stream is currently live
                    return True
            return False
        except Exception as e:
            log.error(f"Error verifying live stream with YouTube API: {e}")
            log_action(f"Error verifying live stream with YouTube API: {e}")
            return False

    log.error("All YouTube API keys have exceeded their quota.")
    return False

# Fetch latest video or live stream using RSS feed
def fetch_latest_content_rss(channel_id):
    try:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        feed = feedparser.parse(rss_url)
        if feed.entries:
            latest_entry = feed.entries[0]
            title = latest_entry.title.lower()
            description = latest_entry.description.lower() if hasattr(latest_entry, "description") else ""

            # Method 1: Keyword-based detection
            keyword_check = any(keyword in title or keyword in description for keyword in ["live", "premiere", "stream", "livestream"])

            # Method 2: Check for yt:liveBroadcast tag
            live_broadcast_check = hasattr(latest_entry, "yt_livebroadcast") and latest_entry.yt_livebroadcast == "live"

            # Method 3: Check for media:group and media:live tags
            media_live_check = hasattr(latest_entry, "media_group") and hasattr(latest_entry.media_group, "media_live")

            # Method 4: Check for yt:duration tag (assume live streams are longer than 1 hour)
            duration = int(latest_entry.yt_duration) if hasattr(latest_entry, "yt_duration") else 0
            duration_check = duration > 3600

            # Method 5: Combine all methods
            is_live = keyword_check or live_broadcast_check or media_live_check or duration_check

            # If RSS feed suggests it's a live stream, verify with YouTube API
            if is_live:
                video_id = latest_entry.yt_videoid
                is_live = verify_live_stream(video_id)

            return {
                "id": {"videoId": latest_entry.yt_videoid},
                "snippet": {"title": latest_entry.title},
                "is_live": is_live
            }
        return None
    except Exception as e:
        log.error(f"RSS feed error: {e}")
        log_action(f"RSS feed error: {e}")
        return None

# Button for joining live stream
class JoinLiveStreamButton(discord.ui.View):
    def __init__(self, url: str):
        super().__init__()
        self.add_item(discord.ui.Button(label="Watch Stream", url=url))

# Button for watching uploaded videos
class WatchVideoButton(discord.ui.View):
    def __init__(self, url: str):
        super().__init__()
        self.add_item(discord.ui.Button(label="Watch Video", url=url))

# Background task to check for new videos and live streams
@tasks.loop(seconds=30)  # Check every 30 seconds for near real-time monitoring
async def check_youtube():
    # Run checks in parallel
    tasks = [check_channel(channel_id, data) for channel_id, data in monitored_channels.items()]
    await asyncio.gather(*tasks)

# Modularized function to handle live streams
async def handle_live_stream(channel_id, data, content_id, content_title, content_url):
    # Check if the stream is new
    if not any(stream["id"] == content_id for stream in data["streams"]):
        # Add the new stream to the list (keep only the last 10)
        data["streams"].insert(0, {"id": content_id, "title": content_title})
        if len(data["streams"]) > 10:
            data["streams"].pop()

        channel = client.get_channel(DISCORD_CHANNEL_ID)
        if channel:
            # Create a button for joining the live stream
            view = JoinLiveStreamButton(url=content_url)
            # Combine all strings into a single message
            message = (
                f"@everyone\n"
                f"**{data['name']} is live now!**\n"
                f"{content_title}\n"
                f"{content_url}"
            )
            await channel.send(message, view=view)
            log_action(f"Notified about live stream from {data['name']}: {content_title}")
            save_monitored_channels()  # Save the updated stream list
            await client.update_monitored_count_status()
            return {"type": "live", "channel_name": data["name"], "title": content_title, "url": content_url}
    return None

# Modularized function to handle uploaded videos
async def handle_uploaded_video(channel_id, data, content_id, content_title, content_url):
    # Check if the video is new
    if not any(video["id"] == content_id for video in data["videos"]):
        # Add the new video to the list (keep only the last 10)
        data["videos"].insert(0, {"id": content_id, "title": content_title})
        if len(data["videos"]) > 10:
            data["videos"].pop()

        channel = client.get_channel(DISCORD_CHANNEL_ID)
        if channel:
            # Create a button for watching the video
            view = WatchVideoButton(url=content_url)
            # Combine all strings into a single message
            message = (
                f"@everyone\n"
                f"**New video uploaded by {data['name']}!**\n"
                f"{content_title}\n"
                f"{content_url}"
            )
            await channel.send(message, view=view)
            log_action(f"Notified about new video from {data['name']}: {content_title}")
            save_monitored_channels()  # Save the updated video list
            await client.update_monitored_count_status()
            return {"type": "video", "channel_name": data["name"], "title": content_title, "url": content_url}
    return None

# Check a single channel for new videos or live streams
async def check_channel(channel_id, data):
    try:
        # Check for new content (video or live stream)
        latest_content = fetch_latest_content_rss(channel_id)

        if latest_content:
            content_id = latest_content["id"]["videoId"]
            content_title = latest_content["snippet"]["title"]
            is_live = latest_content["is_live"]
            content_url = f"https://www.youtube.com/watch?v={content_id}"

            # Check if it's a live stream
            if is_live:
                return await handle_live_stream(channel_id, data, content_id, content_title, content_url)
            else:
                # It's a regular video
                return await handle_uploaded_video(channel_id, data, content_id, content_title, content_url)

    except Exception as e:
        log.error(f"Error checking channel {channel_id}: {e}")
        log_action(f"Error checking channel {channel_id}: {e}")
    return None

# Global error handler for app commands
@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # Send a message if the user doesn't have the required roles
        await interaction.response.send_message(
            "You do not have the required roles to use this command.",
            ephemeral=True
        )
        log_action(f"User {interaction.user.name} attempted to use a command without the required roles.")
    else:
        # Log other errors
        log.error(f"Error in command {interaction.command.name}: {error}")
        log_action(f"Error in command {interaction.command.name}: {error}", user=interaction.user.name)
        await interaction.response.send_message(
            "An error occurred while processing your command.",
            ephemeral=True
        )

# Background task to send daily logs
@tasks.loop(hours=24)  # Run every 24 hours
async def send_daily_log():
    global daily_logs_enabled
    if not daily_logs_enabled:
        log_action("Daily logs are currently disabled.")
        return

    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            # Create a timestamp for the log file
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            log_file_name = f"bot_log_{timestamp}.txt"

            # Read the log file content
            with open(LOG_FILE, "r", encoding="utf-8") as file:
                log_content = file.read()

            # Write the log content to a new file with the timestamped name
            with open(log_file_name, "w", encoding="utf-8") as new_file:
                new_file.write(log_content)

            # Send the log file to the specified channel
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                # Send the message with the log file
                message = f"Here is the daily log file for {datetime.now().strftime('%Y-%m-%d')}:"
                with open(log_file_name, "rb") as file:
                    await log_channel.send(message, file=discord.File(file, filename=log_file_name))
                log_action(f"Daily log file sent to channel {LOG_CHANNEL_ID}.")

                # Clear the log file for the next day
                with open(LOG_FILE, "w", encoding="utf-8") as file:
                    file.truncate()

                # Remove the temporary log file
                os.remove(log_file_name)
                break  # Exit the retry loop if successful
            else:
                log.error(f"Log channel with ID {LOG_CHANNEL_ID} not found.")
                break
        except Exception as e:
            log.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                log.error("Max retries reached. Giving up.")

# Function to fetch the channel ID, channel name, and link from a channel name
def fetch_channel_info_from_name(channel_name: str) -> dict:
    global current_api_key_index
    max_retries = len(YOUTUBE_API_KEYS)  # Maximum retries based on the number of keys

    for _ in range(max_retries):
        try:
            # Get the current API key
            api_key = YOUTUBE_API_KEYS[current_api_key_index]

            # Make a request to the YouTube Data API to search for the channel by name
            url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={channel_name}&type=channel&key={api_key}"
            response = requests.get(url)
            data = response.json()

            # Check for quota exhaustion error
            if "error" in data:
                error_message = data["error"].get("message", "")
                if "quotaExceeded" in error_message:
                    log.warning(f"Quota exceeded for API key index {current_api_key_index}. Rotating to the next key.")
                    rotate_api_key()
                    continue  # Retry with the next key

            # Check if the search returned any results
            if "items" in data and data["items"]:
                # Return the channel ID, channel name, and link
                return {
                    "channel_id": data["items"][0]["snippet"]["channelId"],
                    "channel_name": data["items"][0]["snippet"]["title"],
                    "channel_link": f"https://www.youtube.com/channel/{data['items'][0]['snippet']['channelId']}"
                }
            else:
                log.error(f"No channel found for name: {channel_name}")
                return None
        except Exception as e:
            log.error(f"Error fetching channel info from name: {e}")
            return None

    log.error("All YouTube API keys have exceeded their quota.")
    return None

# Button for confirming or canceling the addition of a YouTube channel
class ConfirmAddChannel(discord.ui.View):
    def __init__(self, channel_info: dict, search_term: str):
        super().__init__()
        self.channel_info = channel_info
        self.search_term = search_term

    @discord.ui.button(label="Add Channel", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            channel_id = self.channel_info["channel_id"]
            channel_name = self.channel_info["channel_name"]

            if channel_id in monitored_channels:
                await interaction.response.send_message("This channel is already being monitored.", ephemeral=True)
                log_action(f"Attempted to add already monitored channel: {channel_name}", user=interaction.user.name)
            else:
                # Store the channel name, search term, and empty lists for videos and streams
                monitored_channels[channel_id] = {
                    "name": channel_name,
                    "search_term": self.search_term,  # Store the search term
                    "videos": [],
                    "streams": []
                }
                save_monitored_channels()
                await client.update_monitored_count_status()
                await interaction.response.send_message(f"Added YouTube channel `{channel_name}` to monitoring list.", ephemeral=True)
                log_action(f"Added YouTube channel: {channel_name} (Search Term: {self.search_term})", user=interaction.user.name)
        except Exception as e:
            log.error(f"Error in confirm button: {e}")
            log_action(f"Error in confirm button: {e}", user=interaction.user.name)
            await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Channel addition canceled.", ephemeral=True)
        log_action(f"Channel addition canceled by {interaction.user.name}")

# Health Check View
class HealthCheckView(discord.ui.View):
    def __init__(self):
        super().__init__()
        
    @discord.ui.button(label="Run Full Health Check", style=discord.ButtonStyle.green)
    async def full_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Create an embed for the health check results
            embed = discord.Embed(
                title="Full Health Check Results",
                color=discord.Color.green()
            )
            
            # Check 1: Bot connection status
            embed.add_field(
                name="Bot Connection",
                value="Connected" if client.is_ready() else "Disconnected",
                inline=False
            )
            
            # Check 2: YouTube API key status
            api_key_status = "Working" if verify_api_keys() else "All keys exhausted"
            embed.add_field(
                name="YouTube API Keys",
                value=api_key_status,
                inline=False
            )
            
            # Check 3: Monitoring task status
            task_status = "Running" if check_youtube.is_running() else "Stopped"
            embed.add_field(
                name="Monitoring Task",
                value=task_status,
                inline=False
            )
            
            # Check 4: Channel access
            channel = client.get_channel(DISCORD_CHANNEL_ID)
            channel_status = "Accessible" if channel else "Unreachable"
            embed.add_field(
                name="Notification Channel",
                value=f"{channel_status} (ID: {DISCORD_CHANNEL_ID})",
                inline=False
            )
            
            # Check 5: Uptime
            uptime = datetime.now() - BOT_START_TIME
            embed.add_field(
                name="Uptime",
                value=str(uptime).split('.')[0],  # Remove microseconds
                inline=False
            )
            
            # Check 6: Monitored channels count
            embed.add_field(
                name="Monitored Channels",
                value=f"{len(monitored_channels)} channels",
                inline=False
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            log_action("Full health check performed", user=interaction.user.name)
            
        except Exception as e:
            log.error(f"Health check error: {e}")
            await interaction.response.send_message(
                "An error occurred during health check.",
                ephemeral=True
            )

def verify_api_keys():
    """Check if any API keys are still functional"""
    test_url = "https://www.googleapis.com/youtube/v3/videos?part=snippet&id=dQw4w9WgXcQ"  # Rick Astley test video
    
    for api_key in YOUTUBE_API_KEYS:
        try:
            response = requests.get(f"{test_url}&key={api_key}", timeout=5)
            if response.status_code == 200:
                return True
        except:
            continue
    return False

# Slash command to add a YouTube channel to monitor
@client.tree.command(name="add_youtube_channel", description="Add a YouTube channel to monitor", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("add_channel", []))
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def add_channel(interaction: discord.Interaction, channel_name: str):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Fetch the channel ID, channel name, and link from the channel name
        channel_info = fetch_channel_info_from_name(channel_name)
        if not channel_info:
            await interaction.response.send_message(f"Could not find a YouTube channel with the name: {channel_name}", ephemeral=True)
            log_action(f"Attempted to add channel with invalid name: {channel_name}", user=interaction.user.name)
            return

        # Create an embed to display the channel information
        embed = discord.Embed(
            title="Channel Found",
            description=f"**Channel Name:** {channel_info['channel_name']}\n**Channel Link:** [Click Here]({channel_info['channel_link']})",
            color=discord.Color.blue()
        )

        # Send the embed with confirmation buttons
        view = ConfirmAddChannel(channel_info, channel_name)  # Pass the search term (channel_name)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        log_action(f"Channel search results displayed for: {channel_name}", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in add_channel command: {e}")
        log_action(f"Error in add_channel command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Slash command to remove a YouTube channel from monitoring
@client.tree.command(name="remove_youtube_channel", description="Remove a YouTube channel from monitoring", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("remove_channel", []))
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def remove_channel(interaction: discord.Interaction, channel_name: str):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Fetch the channel ID and channel name from the channel name
        channel_info = fetch_channel_info_from_name(channel_name)
        if not channel_info:
            await interaction.response.send_message(f"Could not find a YouTube channel with the name: {channel_name}", ephemeral=True)
            log_action(f"Attempted to remove channel with invalid name: {channel_name}", user=interaction.user.name)
            return

        channel_id = channel_info["channel_id"]
        channel_name = channel_info["channel_name"]

        if channel_id in monitored_channels:
            del monitored_channels[channel_id]
            save_monitored_channels()
            await client.update_monitored_count_status()
            await interaction.response.send_message(f"Removed YouTube channel `{channel_name}` from monitoring list.", ephemeral=True)
            log_action(f"Removed YouTube channel: {channel_name}", user=interaction.user.name)
        else:
            await interaction.response.send_message("This channel is not being monitored.", ephemeral=True)
            log_action(f"Attempted to remove non-monitored channel: {channel_name}", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in remove_channel command: {e}")
        log_action(f"Error in remove_channel command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Slash command to list all monitored YouTube channels in an embed
@client.tree.command(name="list_youtube_channels", description="List all monitored YouTube channels", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("list_channels", []))
async def list_channels(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        if monitored_channels:
            # Create an embed to display the monitored channels
            embed = discord.Embed(
                title="Monitored YouTube Channels",
                description="Here are all the YouTube channels currently being monitored:",
                color=discord.Color.blue()
            )

            # Add each monitored channel to the embed
            for channel_id, data in monitored_channels.items():
                embed.add_field(
                    name=data["name"],
                    value=f"Added with: `{data['search_term']}`",  # Only show the search term
                    inline=False
                )

            # Send the embed
            await interaction.response.send_message(embed=embed, ephemeral=True)
            log_action("Listed monitored YouTube channels in an embed.", user=interaction.user.name)
        else:
            # If no channels are being monitored, send a message
            await interaction.response.send_message("No YouTube channels are being monitored.", ephemeral=True)
            log_action("No monitored YouTube channels to list.", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in list_channels command: {e}")
        log_action(f"Error in list_channels command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Slash command to change the bot's status
@client.tree.command(name="set_youtube_bot_status", description="Change the bot's status (online, idle, dnd, invisible)", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("status", []))
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def set_status(interaction: discord.Interaction, status: str):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Map the status string to a discord.Status enum
        status_map = {
            "online": Status.online,
            "idle": Status.idle,
            "dnd": Status.dnd,
            "invisible": Status.invisible
        }

        if status.lower() not in status_map:
            await interaction.response.send_message("Invalid status. Please choose from: online, idle, dnd, invisible.", ephemeral=True)
            return

        # Change the bot's status
        await client.change_presence(status=status_map[status.lower()])
        await interaction.response.send_message(f"Bot status changed to: {status.lower()}", ephemeral=True)
        log_action(f"Bot status changed to {status.lower()}", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in set_status command: {e}")
        log_action(f"Error in set_status command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Slash command to clear the bot's status and presence
@client.tree.command(name="clear_youtube_bot_status", description="Reset the bot's status and presence to default", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("status", []))  # Reuse the same role as set_status
@app_commands.checks.has_permissions(manage_guild=True)
async def clear_status(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Reset to default status (online) and clear activity
        await client.change_presence(status=discord.Status.online, activity=None)
        
        await interaction.response.send_message(
            "Bot status and presence have been reset to default (Online with no activity).",
            ephemeral=True
        )
        log_action("Bot status cleared to default", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in clear_status command: {e}")
        log_action(f"Error in clear_status command: {e}", user=interaction.user.name)
        await interaction.response.send_message(
            "An error occurred while resetting the bot status.",
            ephemeral=True
        )

# Slash command to change the bot's presence
@client.tree.command(name="set_youtube_bot_presence", description="Change the bot's presence (playing, streaming, listening, watching)", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("presence", []))
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def set_presence(interaction: discord.Interaction, activity_type: str, activity_name: str):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Map the activity type string to a discord.ActivityType enum
        activity_map = {
            "playing": ActivityType.playing,
            "streaming": ActivityType.streaming,
            "listening": ActivityType.listening,
            "watching": ActivityType.watching
        }

        if activity_type.lower() not in activity_map:
            await interaction.response.send_message("Invalid activity type. Please choose from: playing, streaming, listening, watching.", ephemeral=True)
            return

        # Change the bot's presence
        activity = Activity(type=activity_map[activity_type.lower()], name=activity_name)
        await client.change_presence(activity=activity)
        await interaction.response.send_message(f"Bot presence changed to: {activity_type.lower()} {activity_name}", ephemeral=True)
        log_action(f"Bot presence changed to {activity_type.lower()} {activity_name}", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in set_presence command: {e}")
        log_action(f"Error in set_presence command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Slash command to toggle daily logs on or off
@client.tree.command(name="toggle_daily_logs", description="Turn on or off the sending of daily logs", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("toggle_logs", []))
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def toggle_daily_logs(interaction: discord.Interaction):
    global daily_logs_enabled

    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Toggle the daily logs state
        daily_logs_enabled = not daily_logs_enabled
        state = "enabled" if daily_logs_enabled else "disabled"
        await interaction.response.send_message(f"Daily logs have been {state}.", ephemeral=True)
        log_action(f"Daily logs toggled to {state}.", user=interaction.user.name)
    except Exception as e:
        log.error(f"Error in toggle_daily_logs command: {e}")
        log_action(f"Error in toggle_daily_logs command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Slash command to check the bot's health status
@client.tree.command(name="youtube_bot_health", description="Check the bot's health status", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("health", []))
async def health_check(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Create an initial response with a button for full check
        embed = discord.Embed(
            title="Bot Health Status",
            description="Basic status information. Click the button below for a full health check.",
            color=discord.Color.blue()
        )
        
        # Add basic status info
        embed.add_field(
            name="Bot Status",
            value="Online" if client.is_ready() else "Offline",
            inline=False
        )
        
        embed.add_field(
            name="Monitoring Active",
            value="Yes" if check_youtube.is_running() else "No",
            inline=False
        )
        
        uptime = datetime.now() - BOT_START_TIME
        embed.add_field(
            name="Uptime",
            value=str(uptime).split('.')[0],
            inline=False
        )
        
        view = HealthCheckView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        log_action("Health check initiated", user=interaction.user.name)
        
    except Exception as e:
        log.error(f"Error in health_check command: {e}")
        await interaction.response.send_message(
            "An error occurred while checking bot health.",
            ephemeral=True
        )

# Slash command to display help information
@client.tree.command(name="youtube_bot_help", description="Display all available commands and their usage", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role(*allowed_roles.get("help", []))
async def help_command(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("This command can only be used in a specific channel.", ephemeral=True)
            return

        # Create an embed to display the commands
        embed = discord.Embed(
            title="Bot Commands",
            description="Here are all the available commands and how to use them:",
            color=discord.Color.red()
        )

        # Add each command to the embed
        embed.add_field(
            name="1. /add_youtube_channel",
            value="Add a YouTube channel to monitor.\n**Usage**: `/add_youtube_channel channel_name:<YouTube channel name>`\nThe bot will display the search results and ask for confirmation.",
            inline=False
        )
        embed.add_field(
            name="2. /remove_youtube_channel",
            value="Remove a YouTube channel from monitoring.\n**Usage**: `/remove_youtube_channel channel_name:<YouTube channel name>`",
            inline=False
        )
        embed.add_field(
            name="3. /list_youtube_channels",
            value="List all monitored YouTube channels.\n**Usage**: `/list_youtube_channels`\nShows the channel name and search term in an embed.",
            inline=False
        )
        embed.add_field(
            name="4. /set_youtube_bot_status",
            value="Set the bot's status.\n**Usage**: `/set_youtube_bot_status status:<online|idle|dnd|invisible>`",
            inline=False
        )
        embed.add_field(
            name="5. /set_youtube_bot_presence",
            value="Set the bot's presence (activity).\n**Usage**: `/set_youtube_bot_presence activity:<playing|streaming|listening|watching> activity_name:<Activity Name>`",
            inline=False
        )
        embed.add_field(
            name="6. /clear_youtube_bot_status",
            value="Reset the bot's status and presence to default.\n**Usage**: `/clear_youtube_bot_status`",
            inline=False
        )
        embed.add_field(
            name="7. /toggle_daily_logs",
            value="Turn on or off the sending of daily logs.\n**Usage**: `/toggle_daily_logs`",
            inline=False
        )
        embed.add_field(
            name="8. /youtube_bot_health",
            value="Check the bot's health status.\n**Usage**: `/youtube_bot_health`",
            inline=False
        )
        embed.add_field(
            name="9. /youtube_bot_help",
            value="Display this help message.\n**Usage**: `/youtube_bot_help`",
            inline=False
        )

        # Send the embed
        await interaction.response.send_message(embed=embed, ephemeral=True)
        log_action(f"Help command used by {interaction.user.name}")
    except Exception as e:
        log.error(f"Error in help_command: {e}")
        log_action(f"Error in help_command: {e}", user=interaction.user.name)
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Run the bot
client.run(DISCORD_TOKEN)