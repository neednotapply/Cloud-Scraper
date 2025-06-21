import os
import json
import random
import asyncio
import logging
import io
import re
import string
import time
import html
from collections import Counter, defaultdict

import aiohttp
import discord
from playwright.async_api import async_playwright, Browser

CONFIG_FILE = "config.json"
TESTED_FILE = "tested_urls.txt"
VALID_CODES_FILE = "valid_codes.txt"

if not os.path.exists(CONFIG_FILE):
    raise RuntimeError(f"Missing {CONFIG_FILE}. See config.example.json")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

TOKEN = config.get("token")
CHANNEL_ID = int(config.get("channel_id", 0))

# Built-in domain configuration
DOMAINS = {
    "ibb.co": {"base_url": "https://ibb.co", "length": 8, "rate_limit": 1.0},
    "puu.sh": {"base_url": "https://puu.sh", "length": 6, "rate_limit": 1.0},
    "imgur.com": {"base_url": "https://imgur.com", "length": 7, "rate_limit": 1.0},
    "i.imgur.com": {"base_url": "https://i.imgur.com", "length": 7, "rate_limit": 1.0},
    "gyazo.com": {"base_url": "https://gyazo.com", "length": 36, "rate_limit": 1.0},
    "cl.ly": {"base_url": "https://cl.ly", "length": 6, "rate_limit": 1.0},
    "prnt.sc": {"base_url": "https://prnt.sc", "length": 6, "rate_limit": 1.0},
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
scrape_task = None

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (X11; Linux x86_64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)",
    "Mozilla/5.0 (iPad; CPU OS 13_2 like Mac OS X)"
]

def start_scrape_loop() -> None:
    """Ensure the scraping task is running."""
    global scrape_task
    if scrape_task is not None:
        if scrape_task.done():
            exc = scrape_task.exception()
            if exc:
                logger.error("scrape_loop exited with exception: %s", exc)
            else:
                logger.info("scrape_loop completed")
            scrape_task = None
        else:
            logger.info("scrape_loop task already running")
            return
    logger.info("Starting scrape_loop task")
    scrape_task = client.loop.create_task(scrape_loop())


@client.event
async def on_ready():
    global logger
    logger = logging.getLogger(str(client.user))
    logger.info("Logged in as %s", client.user)
    start_scrape_loop()

@client.event
async def on_resumed():
    logger.info("Gateway resumed")
    start_scrape_loop()

tested_urls = set()
if os.path.exists(TESTED_FILE):
    with open(TESTED_FILE, "r", encoding="utf-8") as f:
        tested_urls.update(line.strip() for line in f if line.strip())

code_distributions: dict[str, dict[int, list[Counter]]] = defaultdict(lambda: defaultdict(list))
invalid_distributions: dict[str, dict[int, list[Counter]]] = defaultdict(lambda: defaultdict(list))

SAVE_STATS_EVERY = 50
scrape_count = 0

ALL_CHARS = string.ascii_letters + string.digits

def _update_distribution(domain: str, code: str, valid: bool = True) -> None:
    dist_map = code_distributions if valid else invalid_distributions
    length = len(code)
    while len(dist_map[domain][length]) < length:
        dist_map[domain][length].append(Counter())
    for i, char in enumerate(code):
        dist_map[domain][length][i][char] += 1

def _apply_heuristics(domain: str, charset: str, length: int) -> str:
    if domain == "prnt.sc":
        if length == 6:
            return string.ascii_lowercase
        elif length > 6:
            return string.ascii_letters + string.digits
    return charset

def save_distributions():
    with open("stats_valid.json", "w", encoding="utf-8") as f:
        json.dump({d: {k: [dict(c) for c in v] for k, v in lv.items()} for d, lv in code_distributions.items()}, f, indent=2)
    with open("stats_invalid.json", "w", encoding="utf-8") as f:
        json.dump({d: {k: [dict(c) for c in v] for k, v in lv.items()} for d, lv in invalid_distributions.items()}, f, indent=2)

def load_code_distributions() -> None:
    if not os.path.exists(VALID_CODES_FILE):
        return
    with open(VALID_CODES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            code = line.strip()
            if code:
                pass

async def fetch_image(session: aiohttp.ClientSession, url: str, headers=None) -> bytes | None:
    try:
        async with session.get(url, headers=headers) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            if status == 200 and content_type.startswith("image"):
                return await resp.read()
            logger.info("Checked %s -> HTTP %s", url, status)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None

# --- Additional helpers for prnt.sc scraping taken from neednotapply/Screenshot_Stealer-Matrix ---
async def prntsc_get_image_url(browser: Browser, url: str) -> str | None:
    """Load a prnt.sc page with Playwright and extract the screenshot URL."""
    context = await browser.new_context()
    try:
        page = await context.new_page()
        try:
            await page.goto(url)
            try:
                await page.wait_for_selector("#screenshot-image", timeout=5000)
                image_url = await page.get_attribute("#screenshot-image", "src")
            except Exception:
                image_url = None
            if image_url:
                if image_url.startswith("//"):
                    image_url = "https:" + image_url
                elif image_url.startswith("/"):
                    image_url = "https://prnt.sc" + image_url
            return image_url
        finally:
            try:
                await page.close()
            except Exception as exc:
                logger.warning("Failed to close prnt.sc page %s: %s", url, exc)
    finally:
        await context.close()

async def prntsc_validate_image_url(session: aiohttp.ClientSession, image_url: str) -> bool:
    """Return True if the given image URL returns HTTP 200."""
    try:
        async with session.head(image_url, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False

async def fetch_prntsc_image(browser: Browser, session: aiohttp.ClientSession, url: str, headers=None) -> bytes | None:
    image_url = await prntsc_get_image_url(browser, url)
    if not image_url:
        return None
    if not await prntsc_validate_image_url(session, image_url):
        return None
    return await fetch_image(session, image_url, headers=headers)

async def _inner_fetch_playwright_image(browser: Browser, url: str, headers=None) -> bytes | None:
    context = await browser.new_context()
    try:
        page = await context.new_page()
        try:
            await page.set_extra_http_headers(headers or {})
            await page.goto(url, timeout=10000, wait_until="domcontentloaded")

            try:
                await page.click('button:has-text("Continue without supporting us")', timeout=3000)
            except Exception:
                pass

            title = await page.title()
            if "Gyazo - Not Found" in title:
                logger.info("Checked %s -> not found (Gyazo title)", url)
                return None
            if title.startswith("That page doesn't exist"):
                logger.info("Checked %s -> not found (imgbb title)", url)
                return None
            if title.startswith("Zight — Not Found"):
                logger.info("Checked %s -> not found (cl.ly title)", url)
                return None
            if await page.locator('p.Toast2-description', has_text="The requested page could not be found").count() > 0:
                logger.info("Checked %s -> not found (Imgur toast popup)", url)
                return None
            content = await page.content()
            if "That puush could not be found." in content:
                logger.info("Checked %s -> not found (Puu.sh body)", url)
                return None

            try:
                image_url = await page.get_attribute('meta[property="og:image"]', 'content', timeout=3000)
            except Exception:
                image_url = None

            # If this is a prnt.sc link, ensure the image is hosted on
            # image.prntscr.com. Links pointing elsewhere (e.g. imgur) usually
            # indicate the screenshot no longer exists.
            if url.startswith("https://prnt.sc") and image_url:
                if not image_url.startswith("https://image.prntscr.com"):
                    logger.info("Checked %s -> not found (prnt.sc external host)", url)
                    image_url = None
        finally:
            try:
                await asyncio.shield(page.close())
            except Exception as exc:
                logger.warning("Failed to close page for %s: %s", url, exc)

        if image_url:
            async with aiohttp.ClientSession() as session:
                return await fetch_image(session, image_url, headers=headers)
        return None
    finally:
        await context.close()

async def fetch_playwright_image(browser: Browser, url: str, headers=None) -> bytes | None:
    try:
        return await asyncio.wait_for(_inner_fetch_playwright_image(browser, url, headers), timeout=15)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> timeout (global hard cap)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None

async def fetch_imgur_image(session: aiohttp.ClientSession, url: str, headers=None) -> bytes | None:
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                logger.info("Checked %s -> HTTP %s", url, resp.status)
                return None

            content_type = resp.headers.get("Content-Type", "")
            if content_type.startswith("image"):
                logger.info("Found image %s (direct)", url)
                return await resp.read()

            text = await resp.text(errors="ignore")
            if "The requested page could not be found" in text:
                logger.info("Checked %s -> not found (Imgur text)", url)
                return None
            m = re.search(r'<meta property="og:image" content="([^"]+)"', text)
            if m:
                image_url = html.unescape(m.group(1))
                if image_url.startswith("//"):
                    image_url = "https:" + image_url
                return await fetch_image(session, image_url, headers=headers)
            logger.info("Checked %s -> not found (missing og:image)", url)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None

SCRAPER_MAP = {
    "ibb.co": lambda browser, session, url, code, headers: fetch_playwright_image(browser, url, headers=headers),
    "puu.sh": lambda browser, session, url, code, headers: fetch_playwright_image(browser, url, headers=headers),
    "imgur.com": lambda browser, session, url, code, headers: fetch_imgur_image(session, url, headers=headers),
    "i.imgur.com": lambda browser, session, url, code, headers: fetch_imgur_image(session, url, headers=headers),
    "gyazo.com": lambda browser, session, url, code, headers: fetch_playwright_image(browser, url, headers=headers),
    "cl.ly": lambda browser, session, url, code, headers: fetch_playwright_image(browser, url, headers=headers),
    "prnt.sc": lambda browser, session, url, code, headers: fetch_prntsc_image(browser, session, url, headers=headers),
}

async def scrape_loop():
    global scrape_count
    logger.info("Starting scrape loop")
    while True:
        try:
            async with aiohttp.ClientSession() as session, async_playwright() as p:
                async with await p.chromium.launch() as browser:
                    last_log = time.time()
                    while True:
                        try:
                            for domain, settings in DOMAINS.items():
                                base_url = settings["base_url"]
                                length = settings.get("length", 6)
                                rate_limit = settings.get("rate_limit", 1.0)
                                charset = _apply_heuristics(domain, ALL_CHARS, length)

                                while True:
                                    ua = settings.get("user_agent")
                                    headers = {"User-Agent": ua or random.choice(USER_AGENTS)}
                                    code = generate_code(domain, length, charset)
                                    url = f"{base_url}/{code}"
                                    if url in tested_urls:
                                        await asyncio.sleep(0)
                                        continue
                                    tested_urls.add(url)
                                    with open(TESTED_FILE, "a", encoding="utf-8") as f:
                                        f.write(url + "\n")

                                    logger.info("Checking %s", url)

                                    fetcher = SCRAPER_MAP.get(domain)
                                    if not fetcher:
                                        break

                                    try:
                                        image_data = await asyncio.wait_for(
                                            fetcher(browser, session, url, code, headers),
                                            timeout=15,
                                        )
                                    except asyncio.TimeoutError:
                                        logger.warning("Checked %s -> timeout (hard cap exceeded)", url)
                                        image_data = None
                                    except Exception as exc:
                                        logger.warning("Checked %s -> error: %s", url, exc)
                                        image_data = None

                                    scrape_count += 1

                                    if time.time() - last_log > 60:
                                        logger.info("Watchdog: still alive, %d URLs tested", scrape_count)
                                        last_log = time.time()

                                    if scrape_count % SAVE_STATS_EVERY == 0:
                                        logger.info("Heartbeat: processed %d URLs", scrape_count)
                                        save_distributions()

                                    if image_data is None:
                                        _update_distribution(domain, code, valid=False)
                                        await asyncio.sleep(rate_limit)
                                        break

                                    logger.info("Found image %s", url)
                                    _update_distribution(domain, code, valid=True)
                                    with open(VALID_CODES_FILE, "a", encoding="utf-8") as f:
                                        f.write(code + "\n")

                                    channel = client.get_channel(CHANNEL_ID)
                                    if not channel:
                                        logger.warning("Could not find Discord channel with ID %s", CHANNEL_ID)
                                    else:
                                        try:
                                            file = discord.File(io.BytesIO(image_data), filename="image.png")
                                            embed = discord.Embed(url=url)
                                            embed.set_image(url="attachment://image.png")
                                            await channel.send(url, embed=embed, file=file)
                                        except Exception as e:
                                            logger.error("Failed to send message to Discord: %s", e)
                                    break

                                await asyncio.sleep(rate_limit)
                        except Exception:
                            logger.exception("Error in scrape_loop iteration")
                            await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.info("scrape_loop cancelled")
            break
        except Exception:
            logger.exception("scrape_loop error - restarting in 5s")
            await asyncio.sleep(5)
    logger.warning("scrape_loop exited")

def generate_code(domain: str, length: int, charset: str) -> str:
    dist = code_distributions.get(domain, {}).get(length)
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

if __name__ == "__main__":
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("token and channel_id must be set in config.json")
    client.run(TOKEN)
