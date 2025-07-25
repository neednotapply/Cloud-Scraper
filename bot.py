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

MARKDOWN_PATTERN = re.compile(
    r"`(?P<code>[^`]+)`|\[(?P<text>[^\]]+)\]\((?P<url>[^)]+)\)", re.DOTALL
)


def _markdown_to_html(text: str) -> str:
    """Convert a limited subset of Markdown to HTML for Matrix messages."""

    def repl(match: re.Match) -> str:
        code = match.group("code")
        if code is not None:
            return f"<code>{html.escape(code)}</code>"
        label = match.group("text")
        url = match.group("url")
        return f'<a href="{html.escape(url)}">{html.escape(label)}</a>'

    result: list[str] = []
    last = 0
    for m in MARKDOWN_PATTERN.finditer(text):
        result.append(html.escape(text[last:m.start()]))
        result.append(repl(m))
        last = m.end()
    result.append(html.escape(text[last:]))
    return "".join(result)
from collections import Counter, defaultdict
from urllib.parse import urlparse

import aiohttp
import discord
from playwright.async_api import async_playwright, Browser
from nio import (
    AsyncClient,
    LoginResponse,
    InviteEvent,
    MatrixRoom,
    UploadResponse,
)

# Resolve data files relative to this script's location so the bot can be run
# from any working directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATS_FILE = os.path.join(BASE_DIR, "char_stats.json")
DOMAIN_STATS_FILE = os.path.join(BASE_DIR, "domain_stats.json")
PATTERN_STATS_FILE = os.path.join(BASE_DIR, "pattern_stats.json")

if not os.path.exists(CONFIG_FILE):
    raise RuntimeError(f"Missing {CONFIG_FILE}. See config.example.json")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

TOKEN = config.get("token")
channel_id = config.get("channel_id")
CHANNEL_ID = int(channel_id) if channel_id else 0
DISCORD_ENABLED = bool(TOKEN and CHANNEL_ID)

# Optional Matrix configuration
MATRIX_HOMESERVER = config.get("matrix_homeserver")
MATRIX_USER = config.get("matrix_user")
MATRIX_PASSWORD = config.get("matrix_password")
MATRIX_ROOMS = config.get("matrix_rooms", [])
MATRIX_ENABLED = bool(MATRIX_HOMESERVER and MATRIX_USER and MATRIX_PASSWORD)

# Built-in domain configuration
DOMAINS = {
    "prnt.sc": {"base_url": "https://prnt.sc", "length": 6, "rate_limit": 1.0, "weight": 1.0},
    "tinyurl.com": {"base_url": "https://tinyurl.com", "length": 6, "rate_limit": 1.0, "weight": 1.0},
    "is.gd": {"base_url": "https://is.gd", "length": 6, "rate_limit": 1.0, "weight": 1.0},
    "bit.ly": {"base_url": "https://bit.ly", "length": 7, "rate_limit": 1.0, "weight": 1.0},
    "rb.gy": {"base_url": "https://rb.gy", "length": 6, "rate_limit": 1.0, "weight": 1.0},
    # GoTo Meeting uses an "app.goto.com/meeting" base URL but accepts the
    # traditional "gotomeet.me" domain. Using the full meeting path ensures
    # the link resolves correctly.
    "gotomeet.me": {"base_url": "https://app.goto.com/meeting", "length": 9, "rate_limit": 1.0, "weight": 1.0},
    # Use non-www host so Reddit links we send match the canonical form
    "reddit.com": {
        "base_url": "https://reddit.com/comments",
        "length": 6,
        "rate_limit": 1.0,
        "weight": 1.0,
    },
}



# Weight configuration for each domain used to bias selection.
# These values are adjusted at runtime based on valid/invalid results.
DOMAIN_WEIGHTS = {domain: cfg.get("weight", 1.0) for domain, cfg in DOMAINS.items()}
WEIGHT_INCREASE = 0.1  # Applied when a link is valid
WEIGHT_DECREASE = 0.01  # Applied when a link is invalid
MAX_DOMAIN_WEIGHT = 5.0

# Domains that host text rather than images. For these we simply verify that a
# page exists and send the link without attempting to embed an image.
TEXT_DOMAINS: set[str] = {"gotomeet.me"}

# URL shortener domains. These are considered valid if they redirect to a
# different domain without returning a 404.
SHORTENER_DOMAINS = {
    "tinyurl.com",
    "is.gd",
    "bit.ly",
    "rb.gy",
    "reddit.com",
} & set(DOMAINS.keys())


async def capture_page_screenshot(
    browser: Browser, url: str, headers=None
) -> bytes | None:
    """Return a screenshot of the given page using Playwright.

    The screenshot is limited to the initial viewport to avoid capturing the
    entire scrolled page. A 16:9 viewport is used so the resulting image has a
    consistent aspect ratio.
    """
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    try:
        page = await context.new_page()
        try:
            await page.set_extra_http_headers(headers or {})
            await page.goto(url, timeout=10000, wait_until="domcontentloaded")
            # Capture only the visible portion of the page
            return await page.screenshot(full_page=False)
        finally:
            try:
                await asyncio.shield(page.close())
            except Exception as exc:
                logger.warning("Failed to close screenshot page %s: %s", url, exc)
    except Exception as exc:
        logger.warning("Failed to capture screenshot for %s: %s", url, exc)
    finally:
        await context.close()
    return None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)

scrape_tasks: list[asyncio.Task] = []
matrix_client: AsyncClient | None = None

# Number of concurrent scraping workers to run. This can be configured in
# config.json using the "scrape_workers" field and overridden at runtime via the
# SCRAPE_WORKERS environment variable.
SCRAPE_WORKERS = int(os.environ.get("SCRAPE_WORKERS", config.get("scrape_workers", 4)))

tested_urls = set()
tested_lock = asyncio.Lock()
save_lock = asyncio.Lock()

async def start_matrix_client() -> None:
    """Login to Matrix and start the sync loop if enabled."""
    global matrix_client
    if not MATRIX_ENABLED:
        return
    matrix_client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USER)
    resp = await matrix_client.login(MATRIX_PASSWORD)
    if isinstance(resp, LoginResponse):
        logger.info("Logged in to Matrix as %s", MATRIX_USER)
    else:
        raise RuntimeError(f"Matrix login failed: {resp}")

    async def on_invite(room: MatrixRoom, event: InviteEvent) -> None:
        try:
            await matrix_client.join(room.room_id)
            logger.info("Joined invited room %s", room.room_id)
        except Exception as exc:
            logger.warning("Failed to join invited room %s: %s", room.room_id, exc)

    matrix_client.add_event_callback(on_invite, InviteEvent)

    for room in MATRIX_ROOMS:
        try:
            await matrix_client.join(room)
        except Exception as exc:
            logger.warning("Failed to join room %s: %s", room, exc)

    async def sync_loop() -> None:
        while True:
            try:
                await matrix_client.sync(timeout=30000)
            except Exception as exc:
                logger.warning("Matrix sync error: %s", exc)
                await asyncio.sleep(5)
            await asyncio.sleep(1)

    asyncio.create_task(sync_loop())

async def send_matrix_message(
    content: str | None = None,
    media_data: bytes | None = None,
    *,
    content_type: str = "image/png",
    filename: str = "image.png",
) -> None:
    if not matrix_client:
        return
    rooms = MATRIX_ROOMS or list(matrix_client.rooms.keys())
    text = content or ""
    html_content = _markdown_to_html(text)
    msg = {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": html_content,
    }
    if media_data:
        try:
            # matrix-nio's upload expects a file-like object or async provider,
            # so wrap the bytes in a BytesIO buffer
            resp, _ = await matrix_client.upload(
                io.BytesIO(media_data),
                content_type=content_type,
                filename=filename,
            )
            if isinstance(resp, UploadResponse):
                msgtype = "m.image"
                if content_type.startswith("video"):
                    msgtype = "m.video"
                elif not content_type.startswith("image"):
                    msgtype = "m.file"
                msg = {
                    "msgtype": msgtype,
                    "body": text or filename,
                    "url": resp.content_uri,
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_content,
                }
            else:
                logger.warning("Matrix upload failed: %s", resp)
        except Exception as exc:
            logger.warning("Matrix upload error: %s", exc)
    for room in rooms:
        try:
            await matrix_client.room_send(room_id=room, message_type="m.room.message", content=msg)
        except Exception as exc:
            logger.error("Failed to send message to Matrix room %s: %s", room, exc)

async def start_scrape_loop() -> None:
    """Ensure scraping workers are running and previous instances are closed."""
    global scrape_tasks
    if scrape_tasks:
        logger.info("Cancelling existing scrape_loop tasks")
        for task in scrape_tasks:
            if not task.done():
                task.cancel()
        for task in scrape_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
    scrape_tasks = []
    logger.info("Starting %d scrape_loop workers", SCRAPE_WORKERS)
    loop = asyncio.get_running_loop()
    for i in range(SCRAPE_WORKERS):
        task = loop.create_task(scrape_loop(worker_id=i))
        scrape_tasks.append(task)


@client.event
async def on_ready():
    global logger
    logger = logging.getLogger(str(client.user))
    logger.info("Logged in as %s", client.user)
    await start_scrape_loop()

@client.event
async def on_resumed():
    logger.info("Gateway resumed")
    await start_scrape_loop()

code_distributions: dict[str, dict[int, list[Counter]]] = defaultdict(lambda: defaultdict(list))
position_category_stats: dict[str, dict[int, list[Counter]]] = defaultdict(lambda: defaultdict(list))
total_category_stats: dict[str, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))

SAVE_STATS_EVERY = 50
SAVE_WEIGHTS_EVERY = 50
scrape_count = 0

ALL_CHARS = string.ascii_letters + string.digits

def _char_category(ch: str) -> str:
    if ch.islower():
        return "lower"
    if ch.isupper():
        return "upper"
    if ch.isdigit():
        return "digit"
    return "other"

def _update_distribution(domain: str, code: str) -> None:
    length = len(code)
    while len(code_distributions[domain][length]) < length:
        code_distributions[domain][length].append(Counter())
        position_category_stats[domain][length].append(Counter())
    for i, char in enumerate(code):
        code_distributions[domain][length][i][char] += 1
        category = _char_category(char)
        position_category_stats[domain][length][i][category] += 1
        total_category_stats[domain][length][category] += 1

def update_domain_weight(domain: str, valid: bool) -> None:
    """Adjust domain weight based on whether a link was valid."""
    if valid:
        DOMAIN_WEIGHTS[domain] = min(
            MAX_DOMAIN_WEIGHT, DOMAIN_WEIGHTS[domain] + WEIGHT_INCREASE
        )
    else:
        DOMAIN_WEIGHTS[domain] = max(1.0, DOMAIN_WEIGHTS[domain] - WEIGHT_DECREASE)


def choose_domain() -> str:
    """Return a domain based on current weights."""
    domains = list(DOMAIN_WEIGHTS.keys())
    weights = [DOMAIN_WEIGHTS[d] for d in domains]
    return random.choices(domains, weights=weights, k=1)[0]

def _apply_heuristics(domain: str, charset: str, length: int) -> str:
    logger.debug(
        "Applying heuristics: domain=%s length=%d initial_charset=%s",
        domain,
        length,
        charset,
    )
    result = charset
    if domain == "prnt.sc":
        if length == 6:
            result = string.ascii_lowercase
        else:
            result = string.ascii_letters + string.digits
    elif domain in {"tinyurl.com", "is.gd", "bit.ly", "rb.gy"}:
        result = string.ascii_letters + string.digits
    elif domain == "reddit.com":
        # Reddit post IDs are base36: digits and lowercase letters in any order
        result = string.digits + string.ascii_lowercase
    elif domain == "gotomeet.me":
        result = string.digits
    logger.debug("Heuristics result for %s: %s", domain, result)
    return result

def save_distributions() -> None:
    data = {
        "valid": {
            d: {str(k): [dict(c) for c in v] for k, v in lv.items()}
            for d, lv in code_distributions.items()
        }
    }
    logger.info("Saving statistics to %s", STATS_FILE)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved statistics to %s", STATS_FILE)

def save_pattern_stats() -> None:
    data = {}
    for domain, lengths in position_category_stats.items():
        domain_data = {}
        for length, positions in lengths.items():
            domain_data[str(length)] = {
                "positions": [dict(c) for c in positions],
                "totals": dict(total_category_stats[domain][length]),
            }
        data[domain] = domain_data
    logger.info("Saving pattern statistics to %s", PATTERN_STATS_FILE)
    with open(PATTERN_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved pattern statistics to %s", PATTERN_STATS_FILE)

def save_domain_stats() -> None:
    """Persist current domain weights to DOMAIN_STATS_FILE."""
    logger.info("Saving domain weights to %s", DOMAIN_STATS_FILE)
    with open(DOMAIN_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(DOMAIN_WEIGHTS, f, indent=2)
    logger.info("Saved domain weights to %s", DOMAIN_STATS_FILE)

def load_distributions() -> None:
    logger.info("Loading statistics from %s", STATS_FILE)
    if not os.path.exists(STATS_FILE):
        logger.info("Statistics file %s does not exist, creating defaults", STATS_FILE)
        save_distributions()
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.warning("Statistics file %s is invalid, resetting", STATS_FILE)
        save_distributions()
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

    # Reset existing distributions before loading to avoid exponential growth
    code_distributions.clear()
    position_category_stats.clear()
    total_category_stats.clear()

    for domain, lengths in data.get("valid", {}).items():
        for length_str, counters in lengths.items():
            length = int(length_str)
            code_distributions[domain][length] = [Counter(c) for c in counters]

def load_pattern_stats() -> None:
    logger.info("Loading pattern statistics from %s", PATTERN_STATS_FILE)
    if not os.path.exists(PATTERN_STATS_FILE):
        logger.info(
            "Pattern stats file %s does not exist, creating defaults",
            PATTERN_STATS_FILE,
        )
        save_pattern_stats()
    try:
        with open(PATTERN_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.warning("Pattern stats file %s is invalid, resetting", PATTERN_STATS_FILE)
        save_pattern_stats()
        with open(PATTERN_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

    position_category_stats.clear()
    total_category_stats.clear()

    for domain, lengths in data.items():
        for length_str, info in lengths.items():
            length = int(length_str)
            pos_list = [Counter(c) for c in info.get("positions", [])]
            position_category_stats[domain][length] = pos_list
            total_category_stats[domain][length] = Counter(info.get("totals", {}))

def load_domain_stats() -> None:
    """Load domain weights from DOMAIN_STATS_FILE if it exists."""
    if not os.path.exists(DOMAIN_STATS_FILE):
        logger.info(
            "Domain weights file %s does not exist, creating defaults",
            DOMAIN_STATS_FILE,
        )
        save_domain_stats()
        return
    logger.info("Loading domain weights from %s", DOMAIN_STATS_FILE)
    try:
        with open(DOMAIN_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.warning("Domain weights file %s is invalid, resetting", DOMAIN_STATS_FILE)
        save_domain_stats()
        return
    for domain, weight in data.items():
        if domain in DOMAIN_WEIGHTS:
            w = max(1.0, float(weight))
            DOMAIN_WEIGHTS[domain] = min(MAX_DOMAIN_WEIGHT, w)

async def fetch_image(session: aiohttp.ClientSession, url: str, headers=None) -> bytes | None:
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
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

async def fetch_media(session: aiohttp.ClientSession, url: str, headers=None) -> tuple[bytes, str] | None:
    """Fetch any media content and return bytes with its content type."""
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            status = resp.status
            if status == 200:
                data = await resp.read()
                return data, resp.headers.get("Content-Type", "application/octet-stream")
            logger.info("Checked %s -> HTTP %s", url, status)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None

def _guess_extension(url: str, content_type: str) -> str:
    ext = ""
    if "/" in content_type:
        import mimetypes

        ext = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ""
    if not ext:
        path = urlparse(url).path
        if "." in path:
            ext = os.path.splitext(path)[1]
    return ext or ""

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
                logger.info("Checked %s -> not found (prnt.sc selector)", url)
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
            valid = response.status == 200
            if not valid:
                logger.debug(
                    "prnt.sc image HEAD check failed %s -> HTTP %s",
                    image_url,
                    response.status,
                )
            return valid
    except Exception as exc:
        logger.debug(
            "prnt.sc image HEAD request error for %s: %s",
            image_url,
            exc,
        )
        return False

async def fetch_prntsc_image(browser: Browser, session: aiohttp.ClientSession, url: str, headers=None) -> bytes | None:
    image_url = await prntsc_get_image_url(browser, url)
    if not image_url:
        logger.info("Checked %s -> not found (missing screenshot)", url)
        return None

    if not await prntsc_validate_image_url(session, image_url):
        logger.info("Checked %s -> not found (image unavailable)", url)
        return None

    return await fetch_image(session, image_url, headers=headers)

async def _inner_fetch_playwright_image(browser: Browser, url: str, headers=None) -> bytes | None:
    """Capture a screenshot of ``url`` with Playwright."""
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
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

            screenshot = await page.screenshot(full_page=False)
            return screenshot
        finally:
            try:
                await asyncio.shield(page.close())
            except Exception as exc:
                logger.warning("Failed to close page for %s: %s", url, exc)
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


async def check_youtube_video(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> bool:

    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text(errors="ignore")
                if (
                    "promo-title style-scope ytd-background-promo-renderer" in text
                    or "This video isn't available anymore" in text
                    or "This video isn&#39;t available anymore" in text
                    or "Video unavailable" in text
                ):
                    logger.info("Checked %s -> not found (unavailable)", url)
                    return False
                return True
            logger.info("Checked %s -> HTTP %s", url, resp.status)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return False


async def check_text_page(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> str | None:
    try:
        async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as resp:
            final_url = str(resp.url)
            if urlparse(final_url).hostname == "www.reddit.com":
                final_url = final_url.replace("https://www.reddit.com", "https://reddit.com", 1)
            final_host = urlparse(final_url).hostname or ""
            text = await resp.text(errors="ignore")
            if resp.status == 200:
                text_lower = text.lower()
                if (
                    "this page is no longer available" in text_lower
                    or "this is not the web page you are looking for" in text_lower
                    or "this subreddit was banned" in text_lower
                    or "this community has been banned" in text_lower
                    or "this subreddit has been banned" in text_lower
                    or "this community is private" in text_lower
                    or "you must be 18+ to view this community" in text_lower
                ):
                    logger.info("Checked %s -> not found (banned or unavailable)", url)
                    return None

                if "post title: [deleted by user]" in text_lower or "[deleted by user]" in text_lower:
                    logger.info("Checked %s -> not found (deleted by user)", url)
                    return None

                # Additional check for deleted users indicated by a faceplate tracker element
                if (
                    "faceplate-tracker" in text_lower
                    and "post_credit_bar" in text_lower
                    and "user_profile" in text_lower
                    and ">[deleted]</div>" in text_lower
                ):
                    logger.info("Checked %s -> not found (deleted user)", url)
                    return None

                if final_host.endswith("reddit.com"):
                    json_url = final_url + ".json?raw_json=1"
                    try:
                        async with session.get(json_url, timeout=10) as jresp:
                            if jresp.status == 200:
                                data = await jresp.json()
                                post = data[0]["data"]["children"][0]["data"]
                                title = (post.get("title") or "").strip().lower()
                                author = (post.get("author") or "").strip().lower()
                                if title in {"[deleted by user]", "[deleted]", "[removed]"}:
                                    logger.info(
                                        "Checked %s -> not found (deleted post)",
                                        url,
                                    )
                                    return None
                                if author == "[deleted]":
                                    logger.info(
                                        "Checked %s -> not found (deleted post)",
                                        url,
                                    )
                                    return None
                                if post.get("is_self"):
                                    logger.info(
                                        "Checked %s -> not found (self post)", url
                                    )
                                    return None

                                crossposts = post.get("crosspost_parent_list")
                                if crossposts:
                                    parent = crossposts[0]
                                    if parent.get("is_self"):
                                        logger.info(
                                            "Checked %s -> not found (self crosspost)",
                                            url,
                                        )
                                        return None
                                    post = parent

                                if not (
                                    post.get("post_hint") in {"image", "hosted:video", "rich:video"}
                                    or post.get("is_video")
                                ):
                                    logger.info("Checked %s -> not found (no media)", url)
                                    return None
                    except Exception as exc:
                        logger.debug(
                            "Failed to check Reddit post type %s: %s", url, exc
                        )
                return final_url
            logger.info("Checked %s -> HTTP %s", url, resp.status)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None


async def check_discord_invite(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> str | None:
    """Validate a Discord invite by waiting for the page to fully resolve."""
    context = await browser.new_context()
    try:
        page = await context.new_page()
        try:
            await page.set_extra_http_headers(headers or {})
            await page.goto(url, timeout=10000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            # Allow additional time for any client-side redirects
            await page.wait_for_timeout(2000)

            content = await page.content()
            if (
                "Discord App Launched" in content
                or "What should everyone call you?" in content
                or await page.locator(
                    'input[placeholder="What should everyone call you?"][maxlength="999"]'
                ).count()
                > 0
            ):
                return str(page.url)

            if "This invite may be expired" in content:
                logger.info("Checked %s -> not found (invite expired)", url)
            else:
                logger.info("Checked %s -> not found (invalid invite)", url)
        finally:
            try:
                await asyncio.shield(page.close())
            except Exception as exc:
                logger.warning("Failed to close Discord page %s: %s", url, exc)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    finally:
        await context.close()
    return None


async def check_google_meet(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> str | None:
    """Validate a Google Meet code by ensuring it doesn't redirect to an unsupported page."""
    try:
        async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as resp:
            final_url = str(resp.url)
            if "unsupported?meetingCode" in final_url:
                logger.info("Checked %s -> not found (unsupported)", url)
                return None
            if resp.status == 200:
                return final_url
            logger.info("Checked %s -> HTTP %s", url, resp.status)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None


async def check_gotomeet(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> str | None:
    """Validate a GoToMeeting invite by waiting for the page to fully resolve."""
    context = await browser.new_context()
    try:
        page = await context.new_page()
        try:
            await page.set_extra_http_headers(headers or {})
            await page.goto(url, timeout=10000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            # Allow additional time for any client-side redirects
            await page.wait_for_timeout(2000)

            if await page.locator('text="Couldn\'t find that meeting"').count() > 0:
                logger.info("Checked %s -> not found (invalid meeting)", url)
                return None

            return str(page.url)
        finally:
            try:
                await asyncio.shield(page.close())
            except Exception as exc:
                logger.warning("Failed to close GoToMeeting page %s: %s", url, exc)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    finally:
        await context.close()
    return None



async def fetch_shortener_screenshot(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> tuple[str, bytes | None] | None:
    """Follow the shortener and return the final URL with any embed data."""
    try:
        async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as resp:
            if resp.status == 404:
                logger.info("Checked %s -> HTTP 404", url)
                return None
            initial_host = urlparse(url).hostname
            final_url = str(resp.url)
            final_host = urlparse(final_url).hostname
            if final_host and initial_host and final_host != initial_host:
                if initial_host == "rb.gy" and final_url.startswith("https://free-url-shortener.rb.gy/"):
                    logger.info("Checked %s -> not found (homepage redirect)", url)
                    return None

                if final_host in {"youtu.be", "www.youtube.com", "youtube.com"}:
                    return final_url, None

                if final_host == "prnt.sc":
                    image = await fetch_prntsc_image(browser, session, final_url, headers=headers)
                    if image:
                        return final_url, image
                    return None

                content_type = resp.headers.get("Content-Type", "").lower()
                if content_type.startswith("image/") or content_type.startswith("video/"):
                    return final_url, None

                text = ""
                try:
                    text = await resp.text(errors="ignore")
                except Exception:
                    pass
                lower = text.lower()
                if (
                    "cloudflare" in lower
                    and (
                        "you have been blocked" in lower
                        or "attention required" in lower
                        or "access denied" in lower
                        or "checking if the site connection is secure" in lower
                        or "verifying you are human" in lower
                    )
                ):
                    logger.info("Checked %s -> not found (blocked page)", url)
                    return None
                screenshot = await capture_page_screenshot(browser, final_url, headers=headers)
                return final_url, screenshot
            logger.info("Checked %s -> HTTP %s", url, resp.status)
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None


async def fetch_reddit_redirect(
    browser: Browser,
    session: aiohttp.ClientSession,
    url: str,
    code: str,
    headers=None,
) -> tuple[str, bytes | None] | None:
    """Return the linked media URL from a Reddit post.

    This will attempt to extract a direct link to any hosted video so that the
    resulting Discord message can embed the video itself instead of requiring a
    user to navigate to Reddit. If no direct media can be determined, a normal
    redirect URL is returned instead.
    """
    try:
        async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as resp:
            if resp.status != 200:
                logger.info("Checked %s -> HTTP %s", url, resp.status)
                return None

            final_url = str(resp.url)
            if urlparse(final_url).hostname == "www.reddit.com":
                final_url = final_url.replace("https://www.reddit.com", "https://reddit.com", 1)

            text = await resp.text(errors="ignore")
            text_lower = text.lower()
            if (
                "this page is no longer available" in text_lower
                or "this is not the web page you are looking for" in text_lower
                or "this subreddit was banned" in text_lower
                or "this community has been banned" in text_lower
                or "this subreddit has been banned" in text_lower
                or "this community is private" in text_lower
                or "you must be 18+ to view this community" in text_lower
            ):
                logger.info("Checked %s -> not found (banned or unavailable)", url)
                return None

            if "post title: [deleted by user]" in text_lower or "[deleted by user]" in text_lower:
                logger.info("Checked %s -> not found (deleted by user)", url)
                return None

            if (
                "faceplate-tracker" in text_lower
                and "post_credit_bar" in text_lower
                and "user_profile" in text_lower
                and ">[deleted]</div>" in text_lower
            ):
                logger.info("Checked %s -> not found (deleted user)", url)
                return None

            json_url = final_url + ".json?raw_json=1"
            try:
                async with session.get(json_url, timeout=10) as jresp:
                    if jresp.status != 200:
                        logger.info("Checked %s -> HTTP %s (json)", url, jresp.status)
                        return None
                    data = await jresp.json()
            except Exception as exc:
                logger.debug("Failed to fetch Reddit JSON %s: %s", url, exc)
                return None

            post = data[0]["data"]["children"][0]["data"]
            title = (post.get("title") or "").strip().lower()
            author = (post.get("author") or "").strip().lower()
            if title in {"[deleted by user]", "[deleted]", "[removed]"}:
                logger.info("Checked %s -> not found (deleted post)", url)
                return None
            if author == "[deleted]":
                logger.info("Checked %s -> not found (deleted post)", url)
                return None
            if post.get("is_self"):
                logger.info("Checked %s -> not found (self post)", url)
                return None

            crossposts = post.get("crosspost_parent_list")
            if crossposts:
                parent = crossposts[0]
                if parent.get("is_self"):
                    logger.info("Checked %s -> not found (self crosspost)", url)
                    return None
                post = parent

            if not (
                post.get("post_hint") in {"image", "hosted:video", "rich:video"}
                or post.get("is_video")
                or post.get("is_gallery")
            ):
                logger.info("Checked %s -> not found (no media)", url)
                return None

            reddit_video = (
                (post.get("secure_media") or {}).get("reddit_video")
                or (post.get("media") or {}).get("reddit_video")
                or (post.get("preview") or {}).get("reddit_video_preview")
            )

            if reddit_video:
                video_url = (
                    reddit_video.get("fallback_url")
                    or reddit_video.get("scrubber_media_url")
                    or reddit_video.get("dash_url")
                )
                if video_url:
                    final_video = video_url
                    try:
                        async with session.get(
                            video_url,
                            allow_redirects=True,
                            timeout=10,
                            headers={"Range": "bytes=0-0"},
                        ) as vresp:
                            if vresp.status < 400:
                                final_video = str(vresp.url)
                    except Exception as exc:
                        logger.debug(
                            "Failed to resolve Reddit video %s: %s", video_url, exc
                        )
                    return final_video, None

            if post.get("is_gallery"):
                items = (post.get("gallery_data") or {}).get("items") or []
                if items:
                    first_id = items[0].get("media_id")
                    meta = (post.get("media_metadata") or {}).get(first_id) or {}
                    img_url = (meta.get("s") or {}).get("u")
                    if not img_url and meta.get("p"):
                        img_url = meta["p"][-1].get("u")
                    if img_url:
                        return html.unescape(img_url), None

            redirect = post.get("url_overridden_by_dest") or post.get("url")
            if redirect:
                if redirect.startswith("/"):
                    redirect = "https://reddit.com" + redirect
                return redirect, None
    except asyncio.TimeoutError:
        logger.warning("Checked %s -> not found (timeout)", url)
    except Exception as exc:
        logger.warning("Checked %s -> error: %s", url, exc)
    return None

SCRAPER_MAP = {
    "prnt.sc": lambda browser, session, url, code, headers: fetch_prntsc_image(browser, session, url, headers=headers),
    "tinyurl.com": fetch_shortener_screenshot,
    "is.gd": fetch_shortener_screenshot,
    "bit.ly": fetch_shortener_screenshot,
    "rb.gy": fetch_shortener_screenshot,
    "gotomeet.me": check_gotomeet,
    "reddit.com": fetch_reddit_redirect,
}

async def scrape_loop(worker_id: int = 0):
    global scrape_count
    logger.info("Worker %d: Starting scrape loop", worker_id)
    while True:
        browser = None
        p = None
        try:
            async with aiohttp.ClientSession() as session:
                p = await async_playwright().start()
                try:
                    browser = await p.chromium.launch()
                    logger.info("Browser launched")
                except Exception as exc:
                    logger.exception("Failed to launch browser: %s", exc)
                    await p.stop()
                    p = None
                    raise

                last_log = time.time()
                while True:
                    try:
                        domain = choose_domain()
                        settings = DOMAINS[domain]
                        base_url = settings["base_url"]
                        length_setting = settings.get("length", 6)
                        if isinstance(length_setting, (list, tuple)):
                            length = random.choice(length_setting)
                        else:
                            length = length_setting
                        rate_limit = settings.get("rate_limit", 1.0)
                        logger.debug(
                            "Domain selected: %s length=%d rate_limit=%s",
                            domain,
                            length,
                            rate_limit,
                        )
                        charset = _apply_heuristics(domain, ALL_CHARS, length)

                        headers = None
                        code = generate_code(domain, length, charset)
                        logger.debug("Generated code for %s: %s", domain, code)
                        url = f"{base_url}/{code}"
                        async with tested_lock:
                            if url in tested_urls:
                                await asyncio.sleep(0)
                                continue
                            tested_urls.add(url)

                        logger.info("Checking %s", url)

                        fetcher = SCRAPER_MAP.get(domain)
                        if not fetcher:
                            await asyncio.sleep(rate_limit)
                            continue

                        try:
                            result = await asyncio.wait_for(
                                fetcher(browser, session, url, code, headers),
                                timeout=15,
                            )
                        except asyncio.TimeoutError:
                            logger.warning("Checked %s -> timeout (hard cap exceeded)", url)
                            result = None
                        except Exception as exc:
                            logger.warning("Checked %s -> error: %s", url, exc)
                            result = None
                        else:
                            logger.debug(
                                "Fetcher completed for %s -> %s",
                                url,
                                "success" if result else "not found",
                            )

                        scrape_count += 1

                        if time.time() - last_log > 60:
                            logger.info("Watchdog: still alive, %d URLs tested", scrape_count)
                            last_log = time.time()

                        if scrape_count % SAVE_WEIGHTS_EVERY == 0:
                            logger.info(
                                "Heartbeat: processed %d URLs", scrape_count
                            )
                            async with save_lock:
                                save_distributions()
                                save_pattern_stats()
                                load_distributions()
                                load_pattern_stats()
                                save_domain_stats()
                                load_domain_stats()
                        elif scrape_count % SAVE_STATS_EVERY == 0:
                            logger.info(
                                "Heartbeat: processed %d URLs", scrape_count
                            )
                            async with save_lock:
                                save_distributions()
                                save_pattern_stats()
                                load_distributions()
                                load_pattern_stats()

                        if domain in TEXT_DOMAINS:
                            if not result:
                                update_domain_weight(domain, False)
                                await asyncio.sleep(rate_limit)
                                continue
                            final_url = result
                            logger.info("Found page %s", final_url)
                            _update_distribution(domain, code)
                            update_domain_weight(domain, True)
                            channel = client.get_channel(CHANNEL_ID) if DISCORD_ENABLED else None
                            if channel:
                                try:
                                    await asyncio.wait_for(
                                        channel.send(final_url),
                                        timeout=10,
                                    )
                                except Exception as e:
                                    logger.error("Failed to send message to Discord: %s", e)
                            elif DISCORD_ENABLED:
                                logger.warning("Could not find Discord channel with ID %s", CHANNEL_ID)
                            await send_matrix_message(final_url)
                        elif domain in SHORTENER_DOMAINS:
                            if not result:
                                update_domain_weight(domain, False)
                                await asyncio.sleep(rate_limit)
                                continue
                            final_url, screenshot_data = result
                            logger.info("Found redirect %s -> %s", url, final_url)
                            _update_distribution(domain, code)
                            update_domain_weight(domain, True)
                            channel = client.get_channel(CHANNEL_ID) if DISCORD_ENABLED else None
                            if channel:
                                try:
                                    if screenshot_data:
                                        file = discord.File(io.BytesIO(screenshot_data), filename="screenshot.png")
                                        embed = discord.Embed()
                                        embed.set_image(url="attachment://screenshot.png")
                                        final_host = urlparse(final_url).hostname or final_url
                                        if final_host.startswith("www."):
                                            final_host = final_host[4:]
                                        embed.url = final_url
                                        link = f"[{final_host}]({final_url})"
                                        content = f"`{url}` -> {link}"
                                        await asyncio.wait_for(
                                            channel.send(content, embed=embed, file=file),
                                            timeout=10,
                                        )
                                    else:
                                        media = await fetch_media(session, final_url)
                                        if media:
                                            data, ctype = media
                                            ext = _guess_extension(final_url, ctype) or ".bin"
                                            filename = f"file{ext}"
                                            file = discord.File(io.BytesIO(data), filename=filename)
                                            link_host = urlparse(final_url).hostname or final_url
                                            if link_host.startswith("www."):
                                                link_host = link_host[4:]
                                            link = f"[{link_host}]({final_url})"
                                            content = f"`{url}` -> {link}"
                                            if ctype.startswith("image"):
                                                embed = discord.Embed(url=final_url)
                                                embed.set_image(url=f"attachment://{filename}")
                                                await asyncio.wait_for(
                                                    channel.send(content, embed=embed, file=file),
                                                    timeout=10,
                                                )
                                            else:
                                                await asyncio.wait_for(
                                                    channel.send(content, file=file),
                                                    timeout=10,
                                                )
                                        else:
                                            display_host = urlparse(final_url).hostname or final_url
                                            if display_host.startswith("www."):
                                                display_host = display_host[4:]
                                            link = f"[{display_host}]({final_url})"
                                            content = f"`{url}` -> {link}"
                                            await asyncio.wait_for(
                                                channel.send(content),
                                                timeout=10,
                                            )
                                except Exception as e:
                                    logger.error("Failed to send message to Discord: %s", e)
                            elif DISCORD_ENABLED:
                                logger.warning("Could not find Discord channel with ID %s", CHANNEL_ID)
                            link_host = urlparse(final_url).hostname or final_url
                            if link_host.startswith("www."):
                                link_host = link_host[4:]
                            link = f"[{link_host}]({final_url})"
                            content = f"`{url}` -> {link}"
                            if screenshot_data:
                                await send_matrix_message(
                                    content,
                                    screenshot_data,
                                    content_type="image/png",
                                    filename="screenshot.png",
                                )
                            else:
                                media = await fetch_media(session, final_url)
                                if media:
                                    data, ctype = media
                                    ext = _guess_extension(final_url, ctype) or ".bin"
                                    await send_matrix_message(
                                        content,
                                        data,
                                        content_type=ctype,
                                        filename=f"file{ext}",
                                    )
                                else:
                                    await send_matrix_message(content)
                        else:
                            image_data = result
                            if image_data is None:
                                update_domain_weight(domain, False)
                                await asyncio.sleep(rate_limit)
                                continue

                            logger.info("Found image %s", url)
                            _update_distribution(domain, code)
                            update_domain_weight(domain, True)

                            channel = client.get_channel(CHANNEL_ID) if DISCORD_ENABLED else None
                            if channel:
                                try:
                                    file = discord.File(io.BytesIO(image_data), filename="image.png")
                                    embed = discord.Embed(url=url)
                                    embed.set_image(url="attachment://image.png")
                                    await asyncio.wait_for(
                                        channel.send(url, embed=embed, file=file),
                                        timeout=10,
                                    )
                                except Exception as e:
                                    logger.error("Failed to send message to Discord: %s", e)
                            elif DISCORD_ENABLED:
                                logger.warning("Could not find Discord channel with ID %s", CHANNEL_ID)
                            await send_matrix_message(
                                url,
                                image_data,
                                content_type="image/png",
                                filename="image.png",
                            )

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
        finally:
            if browser:
                try:
                    await asyncio.wait_for(browser.close(), timeout=10)
                    logger.info("Browser closed")
                except Exception as exc:
                    logger.warning("Failed to close browser: %s", exc)
                browser = None
            if p:
                try:
                    await asyncio.wait_for(p.stop(), timeout=10)
                    logger.info("Playwright stopped")
                except Exception as exc:
                    logger.warning("Failed to stop Playwright: %s", exc)
                p = None
    logger.warning("scrape_loop exited")

def generate_code(domain: str, length: int, charset: str) -> str:
    """Generate a code biased by collected statistics but still random."""
    dist = code_distributions.get(domain, {}).get(length)
    pattern = position_category_stats.get(domain, {}).get(length)
    result = []
    for i in range(length):
        weight_map = {ch: 1 for ch in charset}
        if dist and i < len(dist):
            for ch, w in dist[i].items():
                weight_map[ch] = weight_map.get(ch, 1) + w
        if pattern and i < len(pattern):
            counter = pattern[i]
            total = sum(counter.values())
            if total:
                for ch in weight_map:
                    cat = _char_category(ch)
                    weight_map[ch] *= 1 + counter.get(cat, 0) / total
        chars, weights = zip(*weight_map.items())
        result.append(random.choices(chars, weights=weights, k=1)[0])
    return "".join(result)

async def main() -> None:
    load_distributions()
    load_pattern_stats()
    load_domain_stats()
    if MATRIX_ENABLED:
        await start_matrix_client()
    if DISCORD_ENABLED:
        await client.start(TOKEN)
    else:
        await start_scrape_loop()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
