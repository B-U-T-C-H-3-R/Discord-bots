#Read the Read_me_youtube_promotion_bot_v1.txt before proceeding with the code 
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import json
from dotenv import load_dotenv
import feedparser  # For parsing RSS feeds
import logging
from logging.handlers import RotatingFileHandler
import sys

# Load environment variables
load_dotenv()

# Debug: Print environment variables
print(f"DISCORD_TOKEN: {os.getenv('DISCORD_TOKEN')}")
print(f"GUILD_ID: {os.getenv('GUILD_ID')}")
print(f"DISCORD_CHANNEL_ID: {os.getenv('DISCORD_CHANNEL_ID')}")
print(f"COMMANDS_CHANNEL_ID: {os.getenv('COMMANDS_CHANNEL_ID')}")

# Validate required environment variables
required_env_vars = ["DISCORD_TOKEN", "GUILD_ID", "DISCORD_CHANNEL_ID", "COMMANDS_CHANNEL_ID"]
for var in required_env_vars:
    if not os.getenv(var):
        print(f"Error: {var} is not set in the .env file.")
        sys.exit(1)

# Discord bot setup
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
COMMANDS_CHANNEL_ID = int(os.getenv("COMMANDS_CHANNEL_ID"))

# File to store monitored channels
MONITORED_CHANNEL_FILE = "monitored_channels.json"

# Set up logging with UTF-8 encoding
def setup_logging():
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # File handler with UTF-8 encoding
    file_handler = RotatingFileHandler("bot.log", maxBytes=5*1024*1024, backupCount=2, encoding="utf-8")
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

# Log actions to a file
def log_action(action: str):
    log.info(action)

# Store monitored YouTube channels and their last video/stream IDs
monitored_channels = load_monitored_channels()  # Format: {youtube_channel_id: {"name": "Channel Name", "last_video_id": "", "last_stream_id": ""}}

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

        # Start the YouTube monitoring task
        check_youtube.start()

# Set up intents and initialize the bot
intents = discord.Intents.default()
intents.message_content = True
client = Client(command_prefix="/", intents=intents)

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
@tasks.loop(minutes=5)  # Check every 5 minutes
async def check_youtube():
    # Run checks in parallel
    tasks = [check_channel(channel_id, data) for channel_id, data in monitored_channels.items()]
    await asyncio.gather(*tasks)

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
                if content_id != data["last_stream_id"]:
                    data["last_stream_id"] = content_id
                    channel = client.get_channel(DISCORD_CHANNEL_ID)
                    if channel:
                        # Create a button for joining the live stream
                        view = JoinLiveStreamButton(url=content_url)
                        await channel.send(
                            f"@everyone\n"
                            f"**{data['name']} is live now!**\n"
                            f"{content_title}\n"
                            f"{content_url}",
                            view=view
                        )
                        log_action(f"Notified about live stream from {data['name']}: {content_title}")
                        return {"type": "live", "channel_name": data["name"], "title": content_title, "url": content_url}
            else:
                # It's a regular video
                if content_id != data["last_video_id"]:
                    data["last_video_id"] = content_id
                    channel = client.get_channel(DISCORD_CHANNEL_ID)
                    if channel:
                        # Create a button for watching the video
                        view = WatchVideoButton(url=content_url)
                        await channel.send(
                            f"@everyone\n"
                            f"**New video uploaded by {data['name']}!**\n"
                            f"{content_title}\n"
                            f"{content_url}",
                            view=view
                        )
                        log_action(f"Notified about new video from {data['name']}: {content_title}")
                        return {"type": "video", "channel_name": data["name"], "title": content_title, "url": content_url}

            # Save the updated last video/stream ID
            save_monitored_channels()
    except Exception as e:
        log.error(f"Error checking channel {channel_id}: {e}")
        log_action(f"Error checking channel {channel_id}: {e}")
    return None

# Slash command to manually check for new live streams
@client.tree.command(name="check_for_new_live_stream", description="Manually check for new live streams", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role("Admin", "Moderator")  # Restrict to specific roles
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def check_now_live(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Trigger the check for live streams manually
        await interaction.response.send_message("Checking for new live streams...", ephemeral=True)

        # Run checks in parallel
        tasks = [check_channel(channel_id, data) for channel_id, data in monitored_channels.items()]
        results = await asyncio.gather(*tasks)

        # Filter out None results and only keep live streams
        new_live_streams = [result for result in results if result is not None and result["type"] == "live"]

        if new_live_streams:
            # Build a summary of new live streams
            summary = "**New live streams found:**\n"
            for live_stream in new_live_streams:
                summary += f"- **{live_stream['channel_name']}**: {live_stream['title']}\n**Link:** {live_stream['url']}\n"
            await interaction.followup.send(summary, ephemeral=True)
        else:
            await interaction.followup.send("No new live streams found.", ephemeral=True)

        log_action(f"Manual live stream check triggered by {interaction.user}")
    except Exception as e:
        log.error(f"Error in check_now_live command: {e}")
        log_action(f"Error in check_now_live command: {e}")
        await interaction.followup.send("An error occurred while processing your request.", ephemeral=True)

# Slash command to manually check for new uploaded videos
@client.tree.command(name="check_for_new_videos", description="Manually check for new uploaded videos", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role("Admin", "Moderator")  # Restrict to specific roles
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def check_now_videos(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        # Trigger the check for uploaded videos manually
        await interaction.response.send_message("Checking for new uploaded videos...", ephemeral=True)

        # Run checks in parallel
        tasks = [check_channel(channel_id, data) for channel_id, data in monitored_channels.items()]
        results = await asyncio.gather(*tasks)

        # Filter out None results and only keep uploaded videos
        new_videos = [result for result in results if result is not None and result["type"] == "video"]

        if new_videos:
            # Build a summary of new uploaded videos
            summary = "**New uploaded videos found:**\n"
            for video in new_videos:
                summary += f"- **{video['channel_name']}**: {video['title']}\n**Link:** {video['url']}\n"
            await interaction.followup.send(summary, ephemeral=True)
        else:
            await interaction.followup.send("No new uploaded videos found.", ephemeral=True)

        log_action(f"Manual video check triggered by {interaction.user}")
    except Exception as e:
        log.error(f"Error in check_now_videos command: {e}")
        log_action(f"Error in check_now_videos command: {e}")
        await interaction.followup.send("An error occurred while processing your request.", ephemeral=True)

# Slash commands to manage monitored channels
@client.tree.command(name="add_channel", description="Add a YouTube channel to monitor", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role("Admin", "Moderator")  # Restrict to specific roles
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def add_channel(interaction: discord.Interaction, channel_id: str, channel_name: str):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        if channel_id in monitored_channels:
            await interaction.response.send_message("This channel is already being monitored.", ephemeral=True)
            log_action(f"Attempted to add already monitored channel: {channel_name} (ID: {channel_id})")
        else:
            monitored_channels[channel_id] = {"name": channel_name, "last_video_id": "", "last_stream_id": ""}
            save_monitored_channels()
            await interaction.response.send_message(f"Added YouTube channel `{channel_name}` (ID: `{channel_id}`) to monitoring list.", ephemeral=True)
            log_action(f"Added YouTube channel: {channel_name} (ID: {channel_id})")
    except Exception as e:
        log.error(f"Error in add_channel command: {e}")
        log_action(f"Error in add_channel command: {e}")
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

@client.tree.command(name="remove_channel", description="Remove a YouTube channel from monitoring", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_any_role("Admin", "Moderator")  # Restrict to specific roles
@app_commands.checks.has_permissions(manage_guild=True)  # Restrict to users with manage guild permissions
async def remove_channel(interaction: discord.Interaction, channel_id: str):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        if channel_id in monitored_channels:
            channel_name = monitored_channels[channel_id]["name"]
            del monitored_channels[channel_id]
            save_monitored_channels()
            await interaction.response.send_message(f"Removed YouTube channel `{channel_name}` (ID: `{channel_id}`) from monitoring list.", ephemeral=True)
            log_action(f"Removed YouTube channel: {channel_name} (ID: {channel_id})")
        else:
            await interaction.response.send_message("This channel is not being monitored.", ephemeral=True)
            log_action(f"Attempted to remove non-monitored channel: {channel_id}")
    except Exception as e:
        log.error(f"Error in remove_channel command: {e}")
        log_action(f"Error in remove_channel command: {e}")
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

@client.tree.command(name="list_channels", description="List all monitored YouTube channels", guild=discord.Object(id=GUILD_ID))
async def list_channels(interaction: discord.Interaction):
    try:
        if interaction.channel_id != COMMANDS_CHANNEL_ID:
            await interaction.response.send_message("Commands can only be used in the designated commands channel.", ephemeral=True)
            return

        if monitored_channels:
            channels_list = "\n".join([f"{data['name']} (ID: `{channel_id}`)" for channel_id, data in monitored_channels.items()])
            await interaction.response.send_message(f"Monitored YouTube channels:\n{channels_list}", ephemeral=True)
            log_action("Listed monitored YouTube channels.")
        else:
            await interaction.response.send_message("No YouTube channels are being monitored.", ephemeral=True)
            log_action("No monitored YouTube channels to list.")
    except Exception as e:
        log.error(f"Error in list_channels command: {e}")
        log_action(f"Error in list_channels command: {e}")
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

# Run the bot
client.run(DISCORD_TOKEN)