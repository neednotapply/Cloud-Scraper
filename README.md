# Filebox Search

This bot attempts to discover public images hosted on [imgBB](https://imgbb.com) by generating random short codes and checking if an image exists.

## Configuration

Copy `config.example.json` to `config.json` and fill in your Discord bot token and the channel ID you want to post found images to.

The bot has a built-in list of supported services so no additional configuration is required.

### Supported services

| Domain | Code length |
| ------ | ----------- |
| ibb.co | 8 |
| puu.sh | 6 |
| imgur.com / i.imgur.com | 7 |
| gyazo.com | 36 |
| cl.ly | 6 |
| prnt.sc | 6 |
| youtu.be | 11 (checked via `https://www.youtube.com/watch?v=CODE`) |
| vgy.me | 5 |
| catbox.moe | 6 |
| tinyurl.com | 6 |
| is.gd | 6 |
| bit.ly | 7 |
| pastebin.com | 8 |
| gist.github.com | 32 |


```
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "channel_id": 123456789012345678
}
```

## Running

Install the requirements and run `bot.py`:

```
pip install -r requirements.txt
playwright install
python bot.py
```

The bot will log attempts and post any discovered images to the configured Discord channel.

Character frequency statistics are saved to `char_stats.json` and used to bias code generation toward more common letters for each domain.

Each domain also maintains a simple weight that influences how often it is selected for testing. Domains start with a weight of `1.0` and the value is adjusted based on whether links are found to be valid or invalid during scraping. The current weights are stored in `domain_stats.json` so the bot can learn over time which services are more reliable.

If either of these files is missing or contains invalid JSON, the bot will reset
them to default empty structures on startup.

### Imgur support

Imgur pages and direct `i.imgur.com` links are handled automatically without any additional configuration. Both the page URL and direct image links such as `https://i.imgur.com/rMluBf1_d.webp` will be processed.

