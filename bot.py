import os
import json
import random
import asyncio
import logging
import io
from collections import Counter

import aiohttp
import re
import string
import discord


CONFIG_FILE = "config.json"

TESTED_FILE = "tested_urls.txt"
VALID_CODES_FILE = "valid_codes.txt"

if not os.path.exists(CONFIG_FILE):
    raise RuntimeError(f"Missing {CONFIG_FILE}. See config.example.json")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

URL_MAP = {
    url.rstrip("/"): int(length)
    for url, length in config.get("urls", {"https://ibb.co": 8}).items()
}
TOKEN = config.get("token")
CHANNEL_ID = int(config.get("channel_id", 0))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")


intents = discord.Intents.default()
client = discord.Client(intents=intents)


tested_urls = set()
if os.path.exists(TESTED_FILE):
    with open(TESTED_FILE, "r", encoding="utf-8") as f:
        tested_urls.update(line.strip() for line in f if line.strip())

# Store character frequency for known valid codes
code_distributions: dict[int, list[Counter]] = {}


def _update_distribution(code: str) -> None:
    length = len(code)
    while len(code_distributions.setdefault(length, [])) < length:
        code_distributions[length].append(Counter())
    for i, char in enumerate(code):
        code_distributions[length][i][char] += 1


def load_code_distributions() -> None:
    if not os.path.exists(VALID_CODES_FILE):
        return
    with open(VALID_CODES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            code = line.strip()
            if code:
                _update_distribution(code)


load_code_distributions()


def generate_code(length: int, charset: str) -> str:
    dist = code_distributions.get(length)
    if not dist:
        return "".join(random.choice(charset) for _ in range(length))

    result = []
    for i in range(length):
        if i < len(dist) and dist[i]:
            chars, weights = zip(*dist[i].items())
            result.append(random.choices(chars, weights=weights, k=1)[0])
        else:
            result.append(random.choice(charset))
    return "".join(result)


async def fetch_image(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url) as resp:
            if resp.status == 200 and resp.headers.get("Content-Type", "").startswith("image"):
                return await resp.read()
            logger.info("Image request %s -> HTTP %s", url, resp.status)
    except asyncio.TimeoutError:
        logger.warning("Image request %s timed out", url)
    except Exception as exc:
        logger.warning("Image request %s failed: %s", url, exc)
    return None


async def fetch_ibb_image(
    session: aiohttp.ClientSession, base_url: str, code: str
) -> bytes | None:
    page_url = f"{base_url.rstrip('/')}/{code}"
    try:
        async with session.get(page_url) as resp:
            if resp.status != 200:
                logger.info("Testing %s -> HTTP %s", page_url, resp.status)
                return None
            text = await resp.text()
    except asyncio.TimeoutError:
        logger.warning("Testing %s timed out", page_url)
        return None
    except Exception as exc:
        logger.warning("Testing %s failed: %s", page_url, exc)
        return None

    match = re.search(r'<meta property="og:image" content="([^\"]+)"', text)
    if not match:
        logger.info("Testing %s -> missing og:image", page_url)
        return None

    image_url = match.group(1)
    image_data = await fetch_image(session, image_url)
    if image_data is None:
        return None
    logger.info("Testing %s -> success", page_url)
    return image_data



@client.event
async def on_ready():
    """Start the scraping loop once the bot is ready."""
    global logger
    logger = logging.getLogger(str(client.user))
    logger.info("Logged in as %s", client.user)
    client.loop.create_task(scrape_loop())


async def scrape_loop():
    logger.info("Starting scrape loop")
    charset = string.ascii_letters + string.digits
    async with aiohttp.ClientSession() as session:
        while True:
            for base_url, length in URL_MAP.items():
                code = generate_code(length, charset)
                url = f"{base_url.rstrip('/')}/{code}"
                if url in tested_urls:
                    await asyncio.sleep(0)
                    continue
                tested_urls.add(url)
                with open(TESTED_FILE, "a", encoding="utf-8") as f:
                    f.write(url + "\n")

                logger.info("Testing %s", url)

                image_data = await fetch_ibb_image(session, base_url, code)
                if image_data is None:
                    await asyncio.sleep(0)
                    continue

                logger.info("Found image %s", url)
                _update_distribution(code)
                with open(VALID_CODES_FILE, "a", encoding="utf-8") as f:
                    f.write(code + "\n")
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
