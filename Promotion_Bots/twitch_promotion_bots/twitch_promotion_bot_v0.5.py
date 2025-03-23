import os
import discord
from discord.ext import commands
from twitchAPI.twitch import Twitch
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get sensitive information from .env
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
TWITCH_USERNAME = os.getenv('TWITCH_USERNAME')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))  # Convert to integer

# Debugging: Print loaded values
print(f"Discord Token: {DISCORD_TOKEN}")
print(f"Twitch Client ID: {TWITCH_CLIENT_ID}")
print(f"Twitch Client Secret: {TWITCH_CLIENT_SECRET}")
print(f"Twitch Username: {TWITCH_USERNAME}")
print(f"Discord Channel ID: {DISCORD_CHANNEL_ID}")

# Initialize Discord bot
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize Twitch API
twitch = None

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
        print(f"Error checking if user is live: {e}")
        return False

async def check_live_status():
    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    last_status = False

    while not bot.is_closed():
        live = await is_user_live(TWITCH_USERNAME)
        if live and not last_status:
            await channel.send(f"@everyone {TWITCH_USERNAME} is now live on Twitch! https://twitch.tv/{TWITCH_USERNAME}")
            last_status = True
        elif not live:
            last_status = False
        await asyncio.sleep(60)  # Check every 60 seconds

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    await init_twitch()
    bot.loop.create_task(check_live_status())

bot.run(DISCORD_TOKEN)