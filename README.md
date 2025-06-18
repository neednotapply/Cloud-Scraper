# Filebox Search

This bot attempts to discover public images hosted on [imgBB](https://imgbb.com) by generating random short codes and checking if an image exists.

## Configuration

Copy `config.example.json` to `config.json` and fill in your Discord bot token and the channel ID you want to post found images to. Add one or more target URLs with their associated code length under `urls`. Optional settings such as `rate_limit` and `user_agent` can be supplied per URL.

```
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "channel_id": 123456789012345678,
  "urls": {
    "https://ibb.co": 8
  }
}
```

## Running

Install the requirements and run `bot.py`:

```
pip install -r requirements.txt
python bot.py
```

The bot will log attempts and post any discovered images to the configured Discord channel.

