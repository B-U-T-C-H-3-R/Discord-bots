import os
import discord
from discord.ext import commands
from discord import app_commands
from twitchAPI.twitch import Twitch
import asyncio
from dotenv import load_dotenv
import json
import logging
from datetime import datetime
import sys

# Load environment variables from .env file
load_dotenv()

# Get sensitive information from .env
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))  
ALLOWED_ROLE_IDS = [int(role_id) for role_id in os.getenv('ALLOWED_ROLE_IDS').split(',')]  
ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID'))  
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID'))  
GUILD_ID = int(os.getenv('GUILD_ID'))

# File to store the list of Twitch usernames
TWITCH_USERNAMES_FILE = "twitch_usernames.json"

# Load Twitch usernames from file (if it exists)
def load_twitch_usernames():
    if os.path.exists(TWITCH_USERNAMES_FILE):
        with open(TWITCH_USERNAMES_FILE, "r") as file:
            return json.load(file)
    return []

# Save Twitch usernames to file
def save_twitch_usernames(usernames):
    with open(TWITCH_USERNAMES_FILE, "w") as file:
        json.dump(usernames, file)

# List of Twitch usernames to monitor (loaded from file)
TWITCH_USERNAMES = load_twitch_usernames()

# Initialize logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_logs.txt"),
        logging.StreamHandler()
    ]
)

# Initialize Discord bot with sharding
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents, shard_count=2)  # Adjust shard_count as needed

# Initialize Twitch API
twitch = None

# Global variable to control log upload
log_upload_enabled = True

async def init_twitch():
    global twitch
    twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)

async def is_user_live(username):
    try:
        async for user in twitch.get_users(logins=[username]):
            user_id = user.id
            async for stream in twitch.get_streams(user_id=[user_id]):
                return True
        return False
    except Exception as e:
        logging.error(f"Error checking if user is live: {e}")
        return False

async def check_live_status():
    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    last_statuses = {username: False for username in TWITCH_USERNAMES if username}

    while not bot.is_closed():
        for username in TWITCH_USERNAMES:
            if not username:
                continue
            # Ensure the username exists in last_statuses
            if username not in last_statuses:
                last_statuses[username] = False
            live = await is_user_live(username)
            if live and not last_statuses[username]:
                await channel.send(f"@everyone {username} is now live on Twitch! https://twitch.tv/{username}")
                last_statuses[username] = True
                logging.info(f"Announced live stream for {username}")
            elif not live:
                last_statuses[username] = False
        await asyncio.sleep(60)  # Check every 60 seconds

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user.name}')
    await init_twitch()
    bot.loop.create_task(check_live_status())

    # Sync commands to the specific guild
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        logging.info(f"Synced {len(synced)} commands to guild {GUILD_ID}.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

# Check if the user has any of the allowed roles
def has_allowed_role(interaction: discord.Interaction) -> bool:
    return any(role.id in ALLOWED_ROLE_IDS for role in interaction.user.roles)

# Check if the command is used in the allowed channel
def is_allowed_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == ALLOWED_CHANNEL_ID

# Function to upload logs to a specific channel
async def upload_logs():
    global log_upload_enabled

    if not log_upload_enabled:
        logging.info("Log upload is currently disabled.")
        return

    # Get the current date for the log file name
    current_date = datetime.now().strftime("%Y-%m-%d")
    log_file_name = f"bot_logs_{current_date}.txt"

    # Close the logging handler to release the file
    for handler in logging.root.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            logging.root.removeHandler(handler)

    # Rename the log file with the current date
    if os.path.exists("bot_logs.txt"):
        os.rename("bot_logs.txt", log_file_name)

        # Reinitialize the logging handler
        file_handler = logging.FileHandler("bot_logs.txt")
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logging.root.addHandler(file_handler)

        # Upload the log file to the specified channel
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            with open(log_file_name, "rb") as log_file:
                await log_channel.send(file=discord.File(log_file, log_file_name))
            logging.info(f"Uploaded log file {log_file_name} to channel {LOG_CHANNEL_ID}.")
        else:
            logging.error(f"Log channel with ID {LOG_CHANNEL_ID} not found.")
    else:
        logging.warning("No log file found to upload.")

# Schedule log upload every 24 hours
async def schedule_log_upload():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await upload_logs()
        await asyncio.sleep(86400)  # 24 hours in seconds

# Use the setup_hook to schedule tasks after the bot has started
@bot.event
async def setup_hook():
    bot.loop.create_task(schedule_log_upload())

# Slash command to toggle log upload
@bot.tree.command(name="toggle_log_upload", description="Turn on or off the log upload feature", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def toggle_log_upload(interaction: discord.Interaction):
    global log_upload_enabled
    log_upload_enabled = not log_upload_enabled
    status = "enabled" if log_upload_enabled else "disabled"
    await interaction.response.send_message(f"Log upload is now {status}.", ephemeral=True)
    logging.info(f"{interaction.user} toggled log upload to {status}.")

# Slash command to add a Twitch user
@bot.tree.command(name="add_twitch_user", description="Add a Twitch user to the monitoring list", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(username="The Twitch username to add")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def adduser(interaction: discord.Interaction, username: str):
    if username in TWITCH_USERNAMES:
        await interaction.response.send_message(f"{username} is already in the list.", ephemeral=True)
        logging.info(f"{interaction.user} tried to add an already monitored user: {username}")
    else:
        TWITCH_USERNAMES.append(username)
        save_twitch_usernames(TWITCH_USERNAMES)  # Save the updated list to file
        await interaction.response.send_message(f"Added {username} to the monitoring list.", ephemeral=True)
        logging.info(f"{interaction.user} added {username} to the monitoring list.")

# Slash command to remove a Twitch user
@bot.tree.command(name="remove_twitch_user", description="Remove a Twitch user from the monitoring list", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(username="The Twitch username to remove")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def removeuser(interaction: discord.Interaction, username: str):
    if username in TWITCH_USERNAMES:
        TWITCH_USERNAMES.remove(username)
        save_twitch_usernames(TWITCH_USERNAMES)  # Save the updated list to file
        await interaction.response.send_message(f"Removed {username} from the monitoring list.", ephemeral=True)
        logging.info(f"{interaction.user} removed {username} from the monitoring list.")
    else:
        await interaction.response.send_message(f"{username} is not in the list.", ephemeral=True)
        logging.info(f"{interaction.user} tried to remove a non-monitored user: {username}")

# Slash command to list monitored Twitch users (in embed)
@bot.tree.command(name="list_twitch_users", description="List all monitored Twitch users", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def listusers(interaction: discord.Interaction):
    if not TWITCH_USERNAMES:
        await interaction.response.send_message("No users are currently being monitored.", ephemeral=True)
        logging.info(f"{interaction.user} listed monitored users: No users being monitored.")
    else:
        # Create an embed to display the list of monitored users
        embed = discord.Embed(
            title="Monitored Twitch Channels",
            description="Here are the Twitch users currently being monitored:",
            color=discord.Color.purple()
        )
        for username in TWITCH_USERNAMES:
            embed.add_field(name="", value=username, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logging.info(f"{interaction.user} listed monitored users: {TWITCH_USERNAMES}")

# Slash command to change bot status
@bot.tree.command(name="set_twitch_bot_status", description="Change the bot's status", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(status="The status to set (online, idle, dnd, invisible)")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def setstatus(interaction: discord.Interaction, status: str):
    status = status.lower()
    valid_statuses = ["online", "idle", "dnd", "invisible"]
    if status not in valid_statuses:
        await interaction.response.send_message(f"Invalid status. Valid options are: {', '.join(valid_statuses)}", ephemeral=True)
        logging.info(f"{interaction.user} tried to set an invalid status: {status}")
    else:
        await bot.change_presence(status=discord.Status[status])
        await interaction.response.send_message(f"Bot status changed to {status}.", ephemeral=True)
        logging.info(f"{interaction.user} changed bot status to {status}.")

# Slash command to change bot activity
@bot.tree.command(name="set_twitch_bot_activity", description="Change the bot's activity", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(activity_type="The type of activity (playing, streaming, listening, watching)")
@app_commands.describe(activity_name="The name of the activity")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def setactivity(interaction: discord.Interaction, activity_type: str, activity_name: str):
    activity_type = activity_type.lower()
    valid_activities = ["playing", "streaming", "listening", "watching"]
    if activity_type not in valid_activities:
        await interaction.response.send_message(f"Invalid activity type. Valid options are: {', '.join(valid_activities)}", ephemeral=True)
        logging.info(f"{interaction.user} tried to set an invalid activity type: {activity_type}")
    else:
        if activity_type == "playing":
            activity = discord.Game(name=activity_name)
        elif activity_type == "streaming":
            activity = discord.Streaming(name=activity_name, url="https://twitch.tv/example")
        elif activity_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=activity_name)
        elif activity_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=activity_name)
        await bot.change_presence(activity=activity)
        await interaction.response.send_message(f"Bot activity changed to {activity_type} {activity_name}.", ephemeral=True)
        logging.info(f"{interaction.user} changed bot activity to {activity_type} {activity_name}.")

# Slash command to clear bot activity
@bot.tree.command(name="clear_twitch_bot_activity", description="Clear the bot's current activity", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def clearactivity(interaction: discord.Interaction):
    await bot.change_presence(activity=None)
    await interaction.response.send_message("Bot activity cleared.", ephemeral=True)
    logging.info(f"{interaction.user} cleared bot activity.")

# Slash command to manually check for live streams
@bot.tree.command(name="check_new_twitch_live_stream", description="Manually check for live streams", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def checklive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Acknowledge the interaction immediately
    live_users = []

    for username in TWITCH_USERNAMES:
        if await is_user_live(username):
            live_users.append(username)

    if live_users:
        # Create a message with the live users
        message = "The following users are now live on Twitch:\n"
        for user in live_users:
            message += f"- {user}: https://twitch.tv/{user}\n"
        
        # Create a view with buttons for confirmation
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)  # Timeout after 30 seconds
                self.value = None

            @discord.ui.button(label="Post", style=discord.ButtonStyle.green)
            async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = True
                self.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
            async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = False
                self.stop()

        # Send the message with buttons
        view = ConfirmView()
        await interaction.followup.send(message, view=view, ephemeral=True)

        # Wait for the user to respond
        await view.wait()

        if view.value is None:
            await interaction.followup.send("Confirmation timed out.", ephemeral=True)
            logging.info(f"{interaction.user} timed out while checking live streams.")
        elif view.value:
            # Post the results to the channel
            channel = bot.get_channel(DISCORD_CHANNEL_ID)
            await channel.send(f"@everyone {message}")
            await interaction.followup.send("Live stream results posted.", ephemeral=True)
            logging.info(f"{interaction.user} posted live stream results: {live_users}")
        else:
            await interaction.followup.send("Live stream results not posted.", ephemeral=True)
            logging.info(f"{interaction.user} canceled posting live stream results.")
    else:
        await interaction.followup.send("No users are currently live.", ephemeral=True)
        logging.info(f"{interaction.user} checked for live streams: No users live.")

# Slash command to display help (in embed)
@bot.tree.command(name="twitch_bot_help", description="Display all commands and how to use them", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def help(interaction: discord.Interaction):
    # Create an embed to display the help message
    embed = discord.Embed(
        title="Bot Commands",
        description="Here are all the available commands and how to use them:",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="1. **/add_Twitch_user**",
        value="Add a Twitch user to the monitoring list.\n**Usage:** `/adduser username:<Twitch username>`",
        inline=False
    )
    embed.add_field(
        name="2. **/remove_twitch_user**",
        value="Remove a Twitch user from the monitoring list.\n**Usage:** `/removeuser username:<Twitch username>`",
        inline=False
    )
    embed.add_field(
        name="3. **/list_twitch_users**",
        value="List all monitored Twitch users.\n**Usage:** `/listusers`",
        inline=False
    )
    embed.add_field(
        name="4. **/setstatus**",
        value="Change the bot's status (online, idle, dnd, invisible).\n**Usage:** `/setstatus status:<status>`",
        inline=False
    )
    embed.add_field(
        name="5. **/setactivity**",
        value="Change the bot's activity (playing, streaming, listening, watching).\n**Usage:** `/setactivity activity_type:<type> activity_name:<name>`",
        inline=False
    )
    embed.add_field(
        name="6. **/clearactivity**",
        value="Clear the bot's current activity.\n**Usage:** `/clearactivity`",
        inline=False
    )
    embed.add_field(
        name="7. **/check_new_twitch_live_stream**",
        value="Manually check for live streams of monitored users.\n**Usage:** `/checklive`",
        inline=False
    )
    embed.add_field(
        name="8. **/help**",
        value="Display all commands and how to use them.\n**Usage:** `/help`",
        inline=False
    )
    embed.add_field(
        name="9. **/toggle_log_upload**",
        value="Turn on or off the log upload feature.\n**Usage:** `/toggle_log_upload`",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logging.info(f"{interaction.user} requested help.")

# Error handler for role and channel checks
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not has_allowed_role(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            logging.warning(f"{interaction.user} tried to use a command without permission.")
        elif not is_allowed_channel(interaction):
            await interaction.response.send_message("This command can only be used in a specific channel.", ephemeral=True)
            logging.warning(f"{interaction.user} tried to use a command in the wrong channel.")
    else:
        await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)
        logging.error(f"An error occurred: {error}")

# Windows event loop policy fix
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

bot.run(DISCORD_TOKEN)