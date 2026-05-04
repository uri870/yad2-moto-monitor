#!/usr/bin/env python3
"""
Yad2 A1 Motorcycle Monitor
Polls Yad2 for new A1-license motorcycle listings and sends Telegram notifications
to anyone who has subscribed via the bot.  Each subscriber has independent filters.

Setup:
    pip install -r requirements.txt
    # Fill TELEGRAM_BOT_TOKEN in .env, then:
    python monitor.py

Telegram commands:
    /start               – subscribe to notifications
    /stop                – unsubscribe
    /filter              – show your active filters
    /filter price 15000 30000  – set price range (both bounds optional)
    /filter price 15000        – set only minimum price
    /filter engine 400         – set minimum engine size (cc)
    /filter clear              – remove all filters
    /last [N]            – show N most recent ads matching your filters (default 5)

Optional env vars (in .env):
    CHECK_INTERVAL   – seconds between Yad2 polls (default: 300)
    PAGES_TO_CHECK   – pages to scan each poll after seeding (default: 3)
                       New ads appear first; 0 = always scan all pages.
    SEEN_ADS_FILE    – ad-ID persistence file (default: seen_ads.json)
    SUBSCRIBERS_FILE – subscriber+filter file  (default: subscribers.json)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "300"))
PAGES_TO_CHECK     = int(os.environ.get("PAGES_TO_CHECK", "3"))
SEEN_ADS_FILE      = Path(os.environ.get("SEEN_ADS_FILE", "seen_ads.json"))
SUBSCRIBERS_FILE   = Path(os.environ.get("SUBSCRIBERS_FILE", "subscribers.json"))

# Yad2 API
YAD2_API      = "https://gw.yad2.co.il/feed-search-legacy/vehicles/motorcycles"
YAD2_ITEM_URL = "https://www.yad2.co.il/vehicles/item"
LICENSE_A1    = 3   # Yad2 internal ID for A1 (≤47 HP)

YAD2_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer":         "https://www.yad2.co.il/",
    "Origin":          "https://www.yad2.co.il",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subscriber types
#
# subscribers: dict[int, dict]
#   key   = chat_id
#   value = filter dict with optional keys:
#             price_min (int), price_max (int), engine_min (int)
# ---------------------------------------------------------------------------


def load_seen() -> set[str]:
    if SEEN_ADS_FILE.exists():
        try:
            return set(json.loads(SEEN_ADS_FILE.read_text()))
        except Exception as e:
            log.warning("Could not read seen-ads file: %s", e)
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_ADS_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def load_subscribers() -> dict[int, dict]:
    if SUBSCRIBERS_FILE.exists():
        try:
            raw = json.loads(SUBSCRIBERS_FILE.read_text())
            # Migrate old format (list of ints → dict with empty filters)
            if isinstance(raw, list):
                return {int(cid): {} for cid in raw}
            return {int(k): v for k, v in raw.items()}
        except Exception as e:
            log.warning("Could not read subscribers file: %s", e)
    return {}


def save_subscribers(subs: dict[int, dict]) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2))


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def parse_price(price_str: str) -> int | None:
    """Parse a Yad2 price string like '43,000 ₪' into an int."""
    try:
        digits = "".join(c for c in price_str if c.isdigit())
        return int(digits) if digits else None
    except Exception:
        return None


def matches_filters(item: dict, filters: dict) -> bool:
    """Return True if item satisfies the subscriber's filters."""
    if not filters:
        return True

    price_min  = filters.get("price_min")
    price_max  = filters.get("price_max")
    engine_min = filters.get("engine_min")

    if price_min is not None or price_max is not None:
        price = parse_price(item.get("price", ""))
        if price is None:
            return False   # no price listed — skip if filter is active
        if price_min is not None and price < price_min:
            return False
        if price_max is not None and price > price_max:
            return False

    if engine_min is not None:
        engine = item.get("EngineVal_text")
        try:
            if engine is None or int(engine) < engine_min:
                return False
        except (ValueError, TypeError):
            return False

    return True


def format_filters(filters: dict) -> str:
    if not filters:
        return "אין פילטרים פעילים."
    parts = []
    if "price_min" in filters or "price_max" in filters:
        lo = f"₪{filters['price_min']:,}" if "price_min" in filters else "ללא מינימום"
        hi = f"₪{filters['price_max']:,}" if "price_max" in filters else "ללא מקסימום"
        parts.append(f"💰 מחיר: {lo} – {hi}")
    if "engine_min" in filters:
        parts.append(f"⚙️  מנוע מינימום: {filters['engine_min']} סמ״ק")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

_tg_offset: int = 0


def _tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def tg_send(chat_id: int, text: str) -> bool:
    payload = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(_tg_url("sendMessage"), json=payload, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Telegram sendMessage failed (chat %s): %s", chat_id, e)
        return False


def broadcast(item: dict, subscribers: dict[int, dict]) -> int:
    """Send an ad to every subscriber whose filters match. Returns notified count."""
    msg = build_message(item)
    count = 0
    for chat_id, filters in list(subscribers.items()):
        if matches_filters(item, filters):
            tg_send(chat_id, msg)
            count += 1
            time.sleep(0.3)
    return count


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "פקודות:\n"
    "/filter – הצג פילטרים\n"
    "/filter price 15000 30000 – טווח מחיר\n"
    "/filter price 15000 – מחיר מינימום בלבד\n"
    "/filter engine 400 – מנוע מינימום (סמ״ק)\n"
    "/filter clear – נקה פילטרים\n"
    "/last [מספר] – הצג מודעות אחרונות\n"
    "/stop – ביטול הרשמה"
)


def handle_filter(chat_id: int, text: str, subscribers: dict[int, dict]) -> bool:
    """
    Parse and apply a /filter command.
    Returns True if subscribers dict was changed.
    """
    filters = subscribers.get(chat_id, {}).copy()
    parts   = text.split()

    # /filter  →  show current filters
    if len(parts) == 1:
        tg_send(chat_id, f"🔍 <b>הפילטרים שלך:</b>\n{format_filters(filters)}")
        return False

    sub = parts[1].lower()

    # /filter clear
    if sub == "clear":
        subscribers[chat_id] = {}
        tg_send(chat_id, "✅ כל הפילטרים נוקו.")
        return True

    # /filter price [min] [max]
    if sub == "price":
        if len(parts) < 3:
            tg_send(chat_id, "⚠️ שימוש: /filter price מינימום [מקסימום]")
            return False
        try:
            filters["price_min"] = int(parts[2])
            if len(parts) >= 4:
                filters["price_max"] = int(parts[3])
            else:
                filters.pop("price_max", None)
        except ValueError:
            tg_send(chat_id, "⚠️ ערכי מחיר חייבים להיות מספרים שלמים.")
            return False
        subscribers[chat_id] = filters
        tg_send(chat_id, f"✅ פילטר מחיר עודכן:\n{format_filters(filters)}")
        return True

    # /filter engine [min]
    if sub == "engine":
        if len(parts) < 3:
            tg_send(chat_id, "⚠️ שימוש: /filter engine מינימום_סמק")
            return False
        try:
            filters["engine_min"] = int(parts[2])
        except ValueError:
            tg_send(chat_id, "⚠️ גודל מנוע חייב להיות מספר שלם.")
            return False
        subscribers[chat_id] = filters
        tg_send(chat_id, f"✅ פילטר מנוע עודכן:\n{format_filters(filters)}")
        return True

    tg_send(chat_id, f"⚠️ פקודה לא מוכרת.\n{HELP_TEXT}")
    return False


def handle_last(chat_id: int, text: str, filters: dict) -> None:
    """Handle /last [N] — send the N most recently published A1 ads matching filters."""
    parts = text.split()
    try:
        want = int(parts[1]) if len(parts) > 1 else 5
        want = max(1, min(want, 20))
    except ValueError:
        tg_send(chat_id, "⚠️ שימוש: /last [מספר]  לדוגמה: /last 5")
        return

    tg_send(chat_id, f"⏳ מחפש {want} מודעות אחרונות…")

    # Fetch enough pages to find fresh ads. Promoted ads appear on every page
    # with inconsistent dates, so we collect from several pages, sort by publish
    # date, then take the top N.
    SCAN_PAGES = 8
    all_items: list[dict] = []
    for page in range(1, SCAN_PAGES + 1):
        items, last_page = fetch_page(page)
        all_items.extend(items)
        if page >= last_page:
            break
        time.sleep(1.0)

    # Sort newest first (items without a parseable date go to the end)
    all_items.sort(
        key=lambda it: parse_publish_date(it) or date.min,
        reverse=True,
    )

    collected = [it for it in all_items if matches_filters(it, filters)][:want]

    if not collected:
        tg_send(chat_id, "לא נמצאו מודעות התואמות את הפילטרים שלך.")
        return

    for item in collected:
        tg_send(chat_id, build_message(item))
        time.sleep(0.3)


def process_updates(subscribers: dict[int, dict]) -> tuple[dict[int, dict], bool]:
    """
    Poll getUpdates; handle /start, /stop, /filter, /last.
    Returns (updated subscribers, changed).
    """
    global _tg_offset
    changed = False
    try:
        r = requests.get(
            _tg_url("getUpdates"),
            params={"offset": _tg_offset, "timeout": 2, "allowed_updates": ["message"]},
            timeout=10,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
    except requests.RequestException as e:
        log.warning("getUpdates failed: %s", e)
        return subscribers, False

    for upd in updates:
        _tg_offset = upd["update_id"] + 1
        msg     = upd.get("message", {})
        text    = (msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if not chat_id:
            continue

        if text.startswith("/start"):
            if chat_id not in subscribers:
                subscribers[chat_id] = {}
                changed = True
                log.info("New subscriber: %s", chat_id)
            tg_send(chat_id,
                "✅ <b>נרשמת לעדכונים!</b>\n"
                "תקבל הודעה על כל מודעת אופנוע A1 חדשה ביד2.\n\n"
                + HELP_TEXT
            )

        elif text.startswith("/stop"):
            if chat_id in subscribers:
                del subscribers[chat_id]
                changed = True
                log.info("Unsubscribed: %s", chat_id)
            tg_send(chat_id, "🔕 הוסרת מרשימת העדכונים. שלח /start כדי להירשם שוב.")

        elif text.startswith("/filter"):
            if chat_id not in subscribers:
                tg_send(chat_id, "שלח /start תחילה כדי להירשם.")
            else:
                if handle_filter(chat_id, text, subscribers):
                    changed = True

        elif text.startswith("/last"):
            filters = subscribers.get(chat_id, {})
            log.info("/last from %s (filters=%s)", chat_id, filters)
            handle_last(chat_id, text, filters)

    return subscribers, changed


# ---------------------------------------------------------------------------
# Yad2 API
# ---------------------------------------------------------------------------


def fetch_page(page: int) -> tuple[list[dict], int]:
    """Return (items, last_page) for one page of A1 motorcycle listings."""
    params = {"page": page, "license": LICENSE_A1, "forceLdLoad": "true"}
    try:
        r = requests.get(YAD2_API, headers=YAD2_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        if not r.text.strip():
            log.error("Empty response from Yad2 (page %d) – IP may be blocked", page)
            return [], 0
        if r.text.strip().startswith("<"):
            log.error("HTML response from Yad2 (page %d) – CAPTCHA or block: %s", page, r.text[:200])
            return [], 0
        body = r.json()
    except requests.RequestException as e:
        log.error("API request failed (page %d): %s", page, e)
        return [], 0
    except ValueError as e:
        log.error("JSON parse failed (page %d): %s | body: %s", page, e, r.text[:200])
        return [], 0

    data      = body.get("data", {})
    feed      = data.get("feed", {})
    items     = feed.get("feed_items", [])
    last_page = data.get("pagination", {}).get("last_page", 1)

    items = [it for it in items if isinstance(it, dict) and it.get("id")]
    return items, last_page


def fetch_pages(max_pages: int = 0) -> list[dict]:
    """
    Fetch A1 motorcycle listings.
    max_pages=0 → all pages (seeding); max_pages=N → first N pages (polling).
    """
    all_items: list[dict] = []
    items, last_page = fetch_page(1)
    all_items.extend(items)
    limit = last_page if max_pages == 0 else min(max_pages, last_page)
    log.info("Page 1/%d – %d items", limit, len(items))

    for page in range(2, limit + 1):
        time.sleep(1.2)
        items, _ = fetch_page(page)
        all_items.extend(items)
        log.info("Page %d/%d – %d items", page, limit, len(items))

    return all_items


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def parse_publish_date(item: dict) -> date | None:
    """
    Extract publish date from the image URL.
    Yad2 encodes it as /Pic/YYYYMM/DD/ in the path, e.g.:
      https://img.yad2.co.il/Pic/202602/12/1_4/o/y2_....jpeg  → 2026-02-12
    """
    img_url = item.get("img_url") or ""
    if not img_url:
        urls = item.get("images_urls") or []
        img_url = urls[0] if urls else ""
    m = re.search(r"/Pic/(\d{4})(\d{2})/(\d{2})/", img_url)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def format_age(published: date) -> str:
    days = (date.today() - published).days
    if days == 0:
        return "פורסם היום"
    if days == 1:
        return "פורסם אתמול"
    return f"פורסם לפני {days} ימים"


def build_message(item: dict) -> str:
    token        = item.get("link_token") or item.get("id", "")
    manufacturer = item.get("manufacturer", "")
    model        = item.get("model", "")
    year         = item.get("year", "")
    price        = item.get("price", "")
    engine       = item.get("EngineVal_text", "")
    hand         = item.get("Hand_text", "")
    area         = item.get("AreaID_text", "") or item.get("city_text", "")
    moto_type    = item.get("MotorcycleTypeID_text", "")
    license_val  = item.get("LicID_text", 'A1 עד 47 כ"ס')
    link         = f"{YAD2_ITEM_URL}/{token}"
    extras       = "  |  ".join(str(x) for x in item.get("row_5", []) if x)
    published    = parse_publish_date(item)

    lines = [
        '🏍️ <b>מודעה חדשה – A1</b>',
        f'<b>{manufacturer} {model}</b>   {year}',
    ]
    if price:
        lines.append(f'💰 {price}')
    if engine:
        lines.append(f'⚙️  {engine} סמ״ק')
    if moto_type:
        lines.append(f'🏷️  {moto_type}')
    if hand:
        lines.append(f'🤝 {hand}')
    if area:
        lines.append(f'📍 {area}')
    lines.append(f'📋 רישיון: {license_val}')
    if published:
        lines.append(f'🗓️  {format_age(published)}')
    if extras:
        lines.append(f'ℹ️  {extras}')
    lines.append(f'\n🔗 <a href="{link}">לצפייה במודעה</a>')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def check_new_ads(
    seen: set[str],
    subscribers: dict[int, dict],
    max_pages: int,
) -> tuple[set[str], int]:
    items = fetch_pages(max_pages=max_pages)
    log.info("Fetched %d A1 listings", len(items))

    new_count = 0
    for item in items:
        ad_id = item.get("link_token") or item.get("id")
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        if not subscribers:
            continue
        notified = broadcast(item, subscribers)
        if notified:
            log.info("Notified %d/%d subs: %s %s [%s]",
                     notified, len(subscribers),
                     item.get("manufacturer"), item.get("model"), ad_id)
            new_count += 1

    return seen, new_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("Set TELEGRAM_BOT_TOKEN in .env before running.")
        sys.exit(1)

    log.info(
        "Yad2 A1 Motorcycle Monitor  |  interval=%ds  |  pages_per_poll=%s",
        CHECK_INTERVAL,
        PAGES_TO_CHECK if PAGES_TO_CHECK > 0 else "all",
    )
    log.info("Send /start to the bot on Telegram to subscribe.")

    seen        = load_seen()
    subscribers = load_subscribers()
    log.info("Loaded %d seen ads, %d subscribers", len(seen), len(subscribers))

    # First run: seed existing ads silently (no notifications)
    if not seen:
        log.info("First run – seeding existing A1 ads (no notifications)")
        items = fetch_pages(max_pages=0)
        seen  = {(it.get("link_token") or it.get("id")) for it in items if it.get("id")}
        save_seen(seen)
        log.info("Seeded %d existing A1 ads", len(seen))

    last_yad2_check = 0.0

    while True:
        subscribers, changed = process_updates(subscribers)
        if changed:
            save_subscribers(subscribers)

        now = time.time()
        if now - last_yad2_check >= CHECK_INTERVAL:
            last_yad2_check = now
            log.info("─── Polling Yad2 (%d subscribers) ───", len(subscribers))
            try:
                seen, new_count = check_new_ads(seen, subscribers, PAGES_TO_CHECK)
                save_seen(seen)
                if new_count:
                    log.info("%d new ad(s) found", new_count)
                else:
                    log.info("No new ads")
            except Exception:
                log.exception("Unexpected error during Yad2 check")

        time.sleep(2)


if __name__ == "__main__":
    main()
