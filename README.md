# Cloud Scraper

This bot attempts to discover publicly accessible media by generating random short codes for a variety of hosting services and checking which links are valid. Any discovered content is posted to a Discord channel.

## Configuration

Copy `config.example.json` to `config.json` and fill in your Discord bot token
and the channel ID you want to post found images to.

The bot has a built-in list of supported services so no additional configuration is required.

### Supported services

| Domain | Code length |
| ------ | ----------- |
| prnt.sc | 6 |
| tinyurl.com | 6 |
| is.gd | 6 |
| bit.ly | 7 |
| rb.gy | 6 |
| app.goto.com/meeting | 9 digits |
| reddit.com | 6 (posts parsed for media link) |

Reddit posts are treated like redirects to the linked image or video.

```
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "channel_id": 123456789012345678,
  "scrape_workers": 4
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

Character frequency statistics are saved to `char_stats.json` and used to bias code generation toward more common letters for each domain. Only successful codes contribute to this file. The file is loaded when the bot starts and written back regularly alongside the domain weights so learning persists between runs.
Additional heuristics about letter case and digit placement are recorded in `pattern_stats.json` using only successful codes.

Each domain also maintains a simple weight that influences how often it is selected for testing. Domains start at `1.0` and increase by `0.1` whenever a link is valid. Invalid links decrease the weight by `0.01`, but a domain's weight will never drop below `1.0`. The current weights are stored in `domain_stats.json` so the bot can learn over time which services are more reliable.

If either of these files is missing or contains invalid JSON, the bot will reset
them to default empty structures on startup.

### Concurrency

The scraper can run multiple workers in parallel to speed up scanning. The
number of workers can be configured in `config.json` using the `scrape_workers`
field (defaults to `4`). You can also override this at runtime with the
`SCRAPE_WORKERS` environment variable:

```
SCRAPE_WORKERS=4 python bot.py
```

Each worker launches its own browser instance, so increasing this value will use
more system resources.


