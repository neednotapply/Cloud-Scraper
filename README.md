# FakeCrime Imagehost Grabber

This project contains a small Discord bot that attempts to discover images hosted on several domains related to `fakecrime.bio`. The bot generates random combinations of five emoji and checks each domain for a matching URL in the form `https://i.<domain>/<emoji string>`.

When a valid image URL is found, the bot sends the image to a configured Discord channel. URLs that have been checked are logged to `tested_urls.txt` to avoid duplicate requests.

## Usage

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `config.example.json` to `config.json` and edit it with your Discord bot token,
   the channel ID where images should be sent, and the number of emoji to generate.
3. Run the bot:
   ```bash
   python bot.py
   ```
