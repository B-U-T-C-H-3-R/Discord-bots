## Requirements
- Python 3.8+
- Discord bot token
- `.env` file for environment variables
- Required Python libraries: `discord.py`, `feedparser`, `dotenv`, `asyncio`, `json`, `logging`

## Installation & Setup
### 1. Clone the repository
```sh
git clone https://github.com/B-U-T-C-H-3-R/Discord-bots/youtube-promotion-bot.git
cd youtube-promotion-bot
```

### 2. Install dependencies
```sh
pip install -r requirements.txt
```

### 3. Create a `.env` file
Create a `.env` file in the project directory and add the following environment variables:
```
DISCORD_TOKEN=your_discord_bot_token
GUILD_ID=your_discord_server_id
DISCORD_CHANNEL_ID=your_notification_channel_id
COMMANDS_CHANNEL_ID=your_commands_channel_id
```

### 4. Run the bot
```sh
python youtube_promotion_bot_v1.py
```

## Commands
### **1. Admin Commands**
| Command | Description |
|---------|-------------|
| `/add_channel channel_id channel_name` | Adds a YouTube channel to monitoring |
| `/remove_channel channel_id` | Removes a YouTube channel from monitoring |
| `/list_channels` | Lists all monitored YouTube channels |
| `/check_for_new_live_stream` | Manually checks for new live streams |
| `/check_for_new_videos` | Manually checks for new uploaded videos |

**Note:** These commands require Admin or Moderator roles and `Manage Guild` permission.

## How It Works
1. The bot periodically checks YouTube RSS feeds for updates.
2. If a new video or live stream is detected, it posts a message in the designated Discord channel.
3. Interactive buttons allow users to watch videos or join live streams directly.
4. Admins can manage monitored channels using the provided slash commands.
5. All actions are logged in `bot.log` for debugging.

## Positive Aspects
- **Automation**: Saves time by automatically notifying users about new content.
- **User Engagement**: Encourages community interaction by making it easy to access videos and streams.
- **Customizable**: Admins can easily manage the monitored channels.
- **Logging & Debugging**: Provides detailed logs for troubleshooting.
- **Secure**: Restricts commands to authorized roles.

## Negative Aspects
- **Limited Detection**: Relies on RSS feeds, which may not always be updated instantly.
- **Potential API Limitations**: Heavy usage may lead to rate-limiting issues.
- **Requires Proper Permissions**: Bot setup must be carefully configured to work correctly.
- **Not Fully Real-Time**: The check interval (e.g., every 5 minutes) means updates are not instant.
- **Manual Maintenance**: Requires periodic updates and monitoring by admins.
- **False Positives for Live Streams**: The bot may mistakenly detect a scheduled or pre-recorded premiere as a live stream, leading to inaccurate notifications.

## Troubleshooting
- Ensure the bot has the correct permissions in Discord.
- Check the `.env` file for correct values.
- Verify that required libraries are installed.
- Check `bot.log` for error messages.

## License
This project is licensed under the MIT License.

## Contact
For issues or support, open a GitHub issue or contact the B-U-T-C-H-3-R.

