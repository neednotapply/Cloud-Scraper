import os
import json
import random
import asyncio
import logging
import io

import aiohttp
import emoji
import discord


CONFIG_FILE = "config.json"

DOMAINS = [
    "adult.army",
    "arabs-for.sale",
    "astronaut.ink",
    "bashscript.lol",
    "crinchy.charity",
    "fakecri.me",
    "fakecrime.bio",
    "fakecrime.lol",
    "fakecrime.pics",
    "fakecrime.tools",
    "fraud.money",
    "grabify.live",
    "grablify.ink",
    "grablify.org",
    "hot-tube.live",
    "ip-finder.wiki",
    "milf.charity",
    "neverlose.wiki",
    "sharex.rocks",
    "shellscript.lol",
    "tailwindcss.lol",
]

TESTED_FILE = "tested_urls.txt"

if not os.path.exists(CONFIG_FILE):
    raise RuntimeError(f"Missing {CONFIG_FILE}. See config.example.json")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

NUM_EMOJIS = int(config.get("num_emojis", 5))
TOKEN = config.get("token")
CHANNEL_ID = int(config.get("channel_id", 0))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fakecrime_bot")


intents = discord.Intents.default()
client = discord.Client(intents=intents)


tested_urls = set()
if os.path.exists(TESTED_FILE):
    with open(TESTED_FILE, "r", encoding="utf-8") as f:
        tested_urls.update(line.strip() for line in f if line.strip())


async def fetch_image(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url) as resp:
            if resp.status == 200 and resp.headers.get("Content-Type", "").startswith("image"):
                return await resp.read()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
    return None



@client.event
async def on_ready():
    """Start the scraping loop once the bot is ready."""
    logger.info("Logged in as %s", client.user)
    client.loop.create_task(scrape_loop())


async def scrape_loop():
    emoji_list = list(emoji.EMOJI_DATA.keys())
    async with aiohttp.ClientSession() as session:
        while True:
            emoji_str = "".join(random.choice(emoji_list) for _ in range(NUM_EMOJIS))
            for domain in DOMAINS:
                url = f"https://i.{domain}/{emoji_str}"
                if url in tested_urls:
                    continue
                tested_urls.add(url)
                with open(TESTED_FILE, "a", encoding="utf-8") as f:
                    f.write(url + "\n")

                image_data = await fetch_image(session, url)
                if image_data is None:
                    continue

                logger.info("Found image %s", url)
                if CHANNEL_ID:
                    channel = client.get_channel(CHANNEL_ID)
                    if channel:
                        file = discord.File(io.BytesIO(image_data), filename="image.png")
                        embed = discord.Embed(url=url)
                        embed.set_image(url="attachment://image.png")
                        await channel.send(url, embed=embed, file=file)
            await asyncio.sleep(1)


if __name__ == "__main__":
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("token and channel_id must be set in config.json")
    client.run(TOKEN)
