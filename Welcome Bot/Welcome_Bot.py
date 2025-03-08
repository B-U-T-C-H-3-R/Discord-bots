import discord
import logging
from discord.ext import commands, tasks
from discord.ext.commands import Bot
from PIL import Image, ImageDraw, ImageFont
from discord.ui import Button, View
import aiohttp
import io
import os
import asyncio
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables from .env file
load_dotenv()

# Set up logging configuration
logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# File handler to log to a file
file_handler = logging.FileHandler('bot_log.txt')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Define bot intents
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# Constants
SERVER_ID = 1337258005439315988  
WELCOME_CHANNEL_ID = 1337258151497699390
BASE_GIF_PATH = "Welcome_to_512_x_150_px9.gif"  
FONT_PATH = "arial.ttf"  
DEFAULT_AVATAR_PATH = "download (1).png"  
LOG_CHANNEL_ID = 1337258099223957534  
MAX_NAME_WIDTH = 200  # Maximum width for the name text
NAME_AREA_CENTER_X = 322  # Center of the name text area

# Function to send log messages
async def send_log_message(message):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(message)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limit hit
                retry_after = e.retry_after
                logger.warning(f"Rate limit hit. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
                await send_log_message(message)  # Retry
            else:
                logger.error(f"Failed to send log message: {e}")

# Function to send error logs
async def send_error_log(error_message):
    logger.error(f"Error: {error_message}")
    await send_log_message(f"**Error Log**: {error_message}")

# Function to send logs file to the log channel every 24 hours
@tasks.loop(hours=24)
async def send_logs_file():
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            with open("bot_log.txt", "rb") as file:
                await log_channel.send(file=discord.File(file, filename=f"logs_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"))
            # Clear the log file after sending
            open("bot_log.txt", "w").close()
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limit hit
                retry_after = e.retry_after
                logger.warning(f"Rate limit hit. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
                await send_logs_file()  # Retry
            else:
                logger.error(f"Failed to send logs file: {e}")

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    await send_log_message(f'Bot has logged in as {bot.user}')
    send_logs_file.start()

# Function to create welcome buttons
class WelcomeButtons(View):
    def __init__(self):
        super().__init__()
        self.add_buttons()

    def add_buttons(self):
        self.add_item(Button(label="ABOUT US", url="https://discord.com/channels/1249998317954531348/1331596861240512544"))
        self.add_item(Button(label="RULES", url="https://discord.com/channels/1249998317954531348/1249998318524960821"))
        self.add_item(Button(label="HELP", url="https://discord.com/channels/1249998317954531348/1250024694812246108"))
        self.add_item(Button(label="CONTACT US", url="https://discord.com/channels/1249998317954531348/1257949046866317374"))

# Function to make an image circular
def make_circle(image):
    size = image.size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size[0], size[1]), fill=255)
    result = Image.new("RGBA", size, (0, 0, 0, 0))
    result.paste(image, (0, 0), mask)
    return result

# Function to create the personalized welcome GIF
async def create_welcome_gif(member: discord.Member):
    try:
        gif = Image.open(BASE_GIF_PATH)
        frames = []
        font = ImageFont.truetype(FONT_PATH, 19)

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(str(member.display_avatar.url)) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
                        avatar = Image.open(io.BytesIO(avatar_bytes)).resize((78, 78)).convert("RGBA")
                    else:
                        raise Exception(f"Failed to fetch avatar, HTTP {resp.status}")
            except Exception as e:
                await send_error_log(f"Error fetching avatar: {e}")
                avatar = Image.open(DEFAULT_AVATAR_PATH).resize((78, 78)).convert("RGBA")

        avatar = make_circle(avatar)

        for frame in range(gif.n_frames):
            gif.seek(frame)
            frame_image = gif.copy().convert("RGBA")
            draw = ImageDraw.Draw(frame_image)
            
            # Ensure the name does not exceed the maximum width and remains centered
            name = member.name
            while font.getbbox(name)[2] - font.getbbox(name)[0] > MAX_NAME_WIDTH:
                name = name[:-1]  # Truncate the name if it exceeds the width
            bbox = font.getbbox(name)
            name_width = bbox[2] - bbox[0]
            name_height = bbox[3] - bbox[1]
            name_x = NAME_AREA_CENTER_X - (name_width // 2)  # Center align the name
            draw.text((name_x, 65), name, (200, 255, 100), font=font)
            frame_image.paste(avatar, (35, 35), avatar)
            frames.append(frame_image)

        output_path = f"welcome_{member.id}.gif"
        frames[0].save(output_path, save_all=True, append_images=frames[1:], loop=0, duration=gif.info["duration"])
        return output_path
    except Exception as e:
        await send_error_log(f"Error creating welcome GIF: {e}")
        return None

@bot.event
async def on_member_join(member):
    if member.guild.id != SERVER_ID:
        return  
    welcome_channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if not welcome_channel:
        await send_error_log("Welcome channel not found.")
        return
    await send_log_message(f'{member.name} has joined the server!')
    gif_path = await create_welcome_gif(member)
    if gif_path:
        try:
            await welcome_channel.send(
                f"Welcome to the server, {member.mention}!",
                file=discord.File(gif_path),
                view=WelcomeButtons()
            )
            if os.path.exists(gif_path):
                os.remove(gif_path)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limit hit
                retry_after = e.retry_after
                logger.warning(f"Rate limit hit. Retrying after {retry_after} seconds.")
                await asyncio.sleep(retry_after)
                await on_member_join(member)  # Retry
            else:
                await send_error_log(f"Error sending welcome GIF: {e}")

# Function to read bot token from .env file
def get_token():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN not found in .env file!")
    return token

token = get_token()
if token:
    bot.run(token)