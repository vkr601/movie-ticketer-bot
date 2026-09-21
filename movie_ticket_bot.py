import os
import re
import json
import sqlite3
import logging
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ----------------------------
# Configuration
# ----------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "movie_bot.sqlite3")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "600"))
BMS_TIMEOUT = int(os.environ.get("BMS_TIMEOUT_SECONDS", "15"))
BMS_BASE = "https://in.bookmyshow.com/serv/getData"

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("movie-bot")

LOCATION, MOVIE, THEATER, CUSTOM_THEATER, DATE, CUSTOM_DATE, LANGUAGE, FORMAT, TIME, TICKETS, ROW = range(11)

# ----------------------------
# Database
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS alerts (
            chat_id INTEGER PRIMARY KEY,
            config_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_scan TEXT,
            last_error TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerted_shows (
            chat_id INTEGER NOT NULL,
            show_key TEXT NOT NULL,
            alerted_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, show_key)
        )""")


def save_alert(chat_id: int, config: dict):
    with db() as c:
        c.execute(
            "INSERT INTO alerts(chat_id, config_json, enabled) VALUES(?,?,1) "
            "ON CONFLICT(chat_id) DO UPDATE SET config_json=excluded.config_json, enabled=1, last_error=NULL",
            (chat_id, json.dumps(config)),
        )


def load_alert(chat_id: int) -> Optional[dict]:
    with db() as c:
        row = c.execute("SELECT config_json FROM alerts WHERE chat_id=? AND enabled=1", (chat_id,)).fetchone()
    return json.loads(row[0]) if row else None


def disable_alert(chat_id: int):
    with db() as c:
        c.execute("UPDATE alerts SET enabled=0 WHERE chat_id=?", (chat_id,))


def was_alerted(chat_id: int, key: str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM alerted_shows WHERE chat_id=? AND show_key=?", (chat_id, key)).fetchone() is not None


def mark_alerted(chat_id: int, key: str):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO alerted_shows VALUES(?,?,?)", (chat_id, key, datetime.now().isoformat()))

# ----------------------------
# Parsing helpers
# ----------------------------
def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def title_norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm(s))


def parse_date(s: str) -> Optional[str]:
    s = norm(s)
    now = datetime.now()
    if s in {"today", "tdy"}:
        return now.strftime("%Y-%m-%d")
    if s in {"tomorrow", "tmrw"}:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for fmt in ("%d %b", "%d %B", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s.title(), fmt)
            if fmt in ("%d %b", "%d %B"):
                d = d.replace(year=now.year)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_row_seat(s: str):
    clean = s.strip().upper()
    m = re.fullmatch(r"([A-Z]{1,3})\s*[- ]?\s*(\d+)", clean)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"(?:ROW\s*)?([A-Z]{1,3})", clean)
    if m:
        return m.group(1), None
    return "ANY", None


def deep_items(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from deep_items(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from deep_items(v)


def first_value(d, keys):
    wanted = {k.lower() for k in keys}
    for k, v in d.items():
        if str(k).lower() in wanted and v not in (None, ""):
            return v
    return None

# ----------------------------
# BookMyShow provider
# ----------------------------
class BookMyShowProvider:
    """Uses the known BMS internal data endpoints. These are not an official public API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": os.environ.get(
                "BMS_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
            ),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://in.bookmyshow.com/",
        })
        self.regions = {}

    def get(self, cmd: str, **params):
        q = {"cmd": cmd, **params}
        r = self.session.get(BMS_BASE, params=q, timeout=BMS_TIMEOUT)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            # Some BMS responses have a JSON prefix/suffix; try extracting the object/array.
            text = r.text.strip()
            start = min([p for p in (text.find("{"), text.find("[")) if p >= 0], default=-1)
            if start >= 0:
                return json.loads(text[start:])
            raise

    def regions_map(self):
        if self.regions:
            return self.regions
        data = self.get("GETREGIONS")
        out = {}
        for d in deep_items(data):
            code = first_value(d, ["RegionCode", "regionCode", "code"])
            name = first_value(d, ["RegionName", "regionName", "name"])
            if code and name:
                out[norm(name)] = str(code)
        self.regions = out
        return out

    def region_code(self, city: str) -> str:
        regions = self.regions_map()
        n = norm(city)
        if n in regions:
            return regions[n]
        # Common aliases.
        aliases = {"chennai": ["madras"], "bengaluru": ["bangalore"], "mumbai": ["bombay"]}
        for canonical, names in aliases.items():
            if n == canonical or n in names:
                if canonical in regions:
                    return regions[canonical]
        raise ValueError(f"Unsupported/unknown BookMyShow city: {city}")

    def open_movies(self, city: str):
        code = self.region_code(city)
        data = self.get("QUICKBOOK", type="MT")
        # Some versions require the region cookie rather than a query param.
        self.session.cookies.set("Rgn", f"Code={code}|text={city.title()}", domain="in.bookmyshow.com")
        return self.get("QUICKBOOK", type="MT")

    def find_movie_events(self, city: str, movie: str):
        data = self.open_movies(city)
        candidates = []
        target = title_norm(movie)
        for d in deep_items(data):
            name = first_value(d, ["EventName", "eventName", "MovieName", "movieName", "Title", "title"])
            code = first_value(d, ["EventCode", "eventCode", "EventID", "eventCodeId"])
            url = first_value(d, ["EventURL", "eventUrl", "URL", "url"])
            if name and code and target in title_norm(name):
                candidates.append({"name": str(name), "event_code": str(code), "url": url})
        # Exact title first.
        candidates.sort(key=lambda x: (title_norm(x["name"]) != target, len(x["name"])))
        return candidates

    def cinemas(self, city: str):
        self.region_code(city)
        data = self.get("GETPREFERREDCINEMAS")
        out = []
        for d in deep_items(data):
            code = first_value(d, ["VenueCode", "venueCode", "VenueID", "venueId"])
            name = first_value(d, ["VenueName", "venueName", "Name", "name"])
            if code and name:
                out.append({"code": str(code), "name": str(name)})
        # Deduplicate.
        seen = set(); result = []
        for x in out:
            if x["code"] not in seen:
                seen.add(x["code"]); result.append(x)
        return result

    def showtimes(self, city: str, event_code: str, date: str, venue_code: Optional[str] = None):
        d = datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%d")
        venues = self.cinemas(city) if not venue_code else [{"code": venue_code, "name": ""}]
        all_rows = []
        for venue in venues:
            try:
                data = self.get("GETSHOWTIMESBYEVENTANDVENUE", f="json", dc=d, vc=venue["code"], ec=event_code)
            except Exception:
                continue
            for item in deep_items(data):
                sid = first_value(item, ["SessionID", "sessionId", "sessionid", "SessionCode", "sessionCode"])
                stime = first_value(item, ["ShowTime", "showTime", "SessionTime", "sessionTime", "Time", "time"])
                fmt = first_value(item, ["Format", "format", "ScreenFormat", "screenFormat", "Experience", "experience"])
                lang = first_value(item, ["Language", "language", "Lang", "lang"])
                vname = first_value(item, ["VenueName", "venueName", "CinemaName", "cinemaName"]) or venue["name"]
                vcode = first_value(item, ["VenueCode", "venueCode"]) or venue["code"]
                if sid and stime:
                    all_rows.append({
                        "session_id": str(sid), "time": str(stime), "format": str(fmt or ""),
                        "language": str(lang or ""), "venue": str(vname), "venue_code": str(vcode),
                        "raw": item,
                    })
        return self._dedupe_shows(all_rows)

    @staticmethod
    def _dedupe_shows(rows):
        out, seen = [], set()
        for x in rows:
            k = (x["venue_code"], x["session_id"], x["time"])
            if k not in seen:
                seen.add(k); out.append(x)
        return out

    def seat_info(self, venue_code: str, session_id: str):
        return self.get("GETSHOWINFO", vid=venue_code, ssid=session_id)

# ----------------------------
# Matching / availability
# ----------------------------
def language_matches(preference: str, actual: str) -> bool:
    p, a = norm(preference), norm(actual)
    if p in ("any", "any language", ""):
        return True
    if not a:
        return False
    aliases = {
        "tamil": ["tamil", "ta"], "telugu": ["telugu", "te"],
        "hindi": ["hindi", "hi"], "malayalam": ["malayalam", "ml"],
        "kannada": ["kannada", "kn"], "english": ["english", "en"],
    }
    terms = aliases.get(p, [p])
    return any(t == a or t in a or a in t for t in terms)


def format_matches(preference: str, actual: str) -> bool:
    p, a = norm(preference), norm(actual)
    if p in ("any", "any format", ""):
        return True
    return p in a or a in p


def time_matches(pref: str, value: str) -> bool:
    if pref in ("ANY", "any", ""):
        return True
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.lower())
    if not m:
        return True  # Don't reject a show because the provider uses an unknown time format.
    h = int(m.group(1)); minute = int(m.group(2) or 0); ap = m.group(3)
    if ap == "pm" and h != 12: h += 12
    if ap == "am" and h == 12: h = 0
    mins = h * 60 + minute
    ranges = {"MORNING": (0, 720), "MATINEE": (720, 960), "EVENING": (960, 1200), "NIGHT": (1200, 1440)}
    lo, hi = ranges.get(pref, (0, 1440))
    return lo <= mins < hi


def theater_matches(pref: str, actual: str) -> bool:
    if norm(pref) in ("any", ""):
        return True
    p = title_norm(pref); a = title_norm(actual)
    return p in a or a in p


def extract_available_seats(data: Any):
    seats = []
    for d in deep_items(data):
        code = first_value(d, ["SeatCode", "seatCode", "SeatName", "seatName", "seatId"])
        status = first_value(d, ["Status", "status", "SeatStatus", "seatStatus", "Availability", "availability"])
        if code and status is not None:
            s = norm(status)
            if any(x in s for x in ("available", "vacant", "free", "bookable")) and not any(x in s for x in ("not available", "unavailable", "sold")):
                seats.append(str(code))
    return list(dict.fromkeys(seats))


def seat_preference_matches(seats: list[str], config: dict) -> bool:
    needed = int(config.get("tickets", 1))
    if not seats:
        # If the provider doesn't expose a parsable seat map, don't claim a seat match.
        return config.get("row", "ANY") in ("ANY", "") and not config.get("exact_seat")
    row = config.get("row", "ANY")
    exact = config.get("exact_seat")
    if exact:
        return exact.upper() in {s.upper() for s in seats}
    if row in ("ANY", ""):
        return len(seats) >= needed
    matching = [s for s in seats if re.match(r"^" + re.escape(row) + r"\s*\d+$", s.upper())]
    return len(matching) >= needed

# ----------------------------
# Scanner
# ----------------------------
provider = BookMyShowProvider()


def scan(config: dict):
    movie_events = provider.find_movie_events(config["city"], config["movie"])
    if not movie_events:
        return {"status": "not_open", "shows": [], "reason": "movie_not_listed"}

    date = config.get("date")
    if not date:
        return {"status": "not_open", "shows": [], "reason": "date_required_for_scan"}

    matching = []
    cinemas = provider.cinemas(config["city"])
    selected_venues = cinemas if norm(config.get("theater", "ANY")) == "any" else [
        v for v in cinemas if theater_matches(config["theater"], v["name"])
    ]

    for event in movie_events:
        for venue in selected_venues:
            shows = provider.showtimes(config["city"], event["event_code"], date, venue["code"])
            for show in shows:
                if not theater_matches(config.get("theater", "ANY"), show["venue"]):
                    continue
                if not language_matches(config.get("language", "ANY"), show.get("language", "")):
                    continue
                if not format_matches(config.get("format", "ANY"), show.get("format", "")):
                    continue
                if not time_matches(config.get("time", "ANY"), show.get("time", "")):
                    continue
                # Seat filtering is only attempted if a seat preference is configured.
                if config.get("row") not in (None, "ANY", "") or config.get("exact_seat"):
                    try:
                        seats = extract_available_seats(provider.seat_info(show["venue_code"], show["session_id"]))
                    except Exception:
                        seats = []
                    if not seat_preference_matches(seats, config):
                        continue
                    show["available_seats"] = seats
                show["movie_name"] = event["name"]
                show["event_code"] = event["event_code"]
                show["date"] = date
                show["booking_url"] = build_booking_url(config["city"], event, date)
                matching.append(show)

    return {"status": "open" if matching else "not_open", "shows": matching, "reason": "ok"}


def build_booking_url(city: str, event: dict, date: str) -> str:
    # EventURL is not always returned by the internal endpoint. If absent, provide the movie search page.
    if event.get("url"):
        slug = str(event["url"]).strip("/")
        return f"https://in.bookmyshow.com/{slug}-{norm(city).replace(' ', '-')}/movie-{norm(city).replace(' ', '-')}-{event['event_code']}-MT/{datetime.strptime(date, '%Y-%m-%d').strftime('%Y%m%d')}"
    return f"https://in.bookmyshow.com/explore/movies-{norm(city).replace(' ', '-')}"

# ----------------------------
# Telegram UI
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Movie Ticket Alert Bot*\n\n"
        "/alert — create/update a monitor\n"
        "/check — scan now\n"
        "/status — show current monitor\n"
        "/stop — stop monitoring",
        parse_mode="Markdown",
    )

async def alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cfg"] = {"theater": "ANY", "language": "ANY", "format": "ANY", "date": None, "time": "ANY", "tickets": 1, "row": "ANY", "exact_seat": None}
    await update.message.reply_text("📍 *1/8* Enter city (e.g. Chennai):", parse_mode="Markdown")
    return LOCATION

async def set_location(update, context):
    context.user_data["cfg"]["city"] = update.message.text.strip()
    await update.message.reply_text("🎬 *2/8* Enter movie name:", parse_mode="Markdown")
    return MOVIE

async def set_movie(update, context):
    context.user_data["cfg"]["movie"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("ANY Theater", callback_data="ANY")], [InlineKeyboardButton("✍️ Type theater", callback_data="CUSTOM")]]
    await update.message.reply_text("🏢 *3/9* Theater:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return THEATER

async def set_format(update, context):
    q = update.callback_query; await q.answer(); context.user_data["cfg"]["format"] = q.data
    return await ask_time(q.message)

async def set_theater(update, context):
    q = update.callback_query; await q.answer()
    if q.data == "CUSTOM":
        await q.edit_message_text("✍️ Type the theater name or partial name:")
        return CUSTOM_THEATER
    context.user_data["cfg"]["theater"] = q.data
    return await ask_date(q.message, context)

async def set_custom_theater(update, context):
    context.user_data["cfg"]["theater"] = update.message.text.strip()
    return await ask_date(update.message, context)

async def ask_date(message, context):
    kb = [[InlineKeyboardButton("Today", callback_data="TODAY"), InlineKeyboardButton("Tomorrow", callback_data="TOMORROW")], [InlineKeyboardButton("✍️ Specific date", callback_data="CUSTOM")]]
    await message.reply_text("📅 *4/9* Show date:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return DATE

async def set_date(update, context):
    q = update.callback_query; await q.answer()
    if q.data == "CUSTOM":
        await q.edit_message_text("✍️ Enter date (e.g. 2 Oct 2026):")
        return CUSTOM_DATE
    context.user_data["cfg"]["date"] = (datetime.now() if q.data == "TODAY" else datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return await ask_language(q.message, context)

async def set_custom_date(update, context):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Couldn't understand that date. Try `2 Oct 2026`.", parse_mode="Markdown")
        return CUSTOM_DATE
    context.user_data["cfg"]["date"] = d
    return await ask_language(update.message, context)

async def ask_language(message, context):
    cfg = context.user_data["cfg"]
    langs = []
    try:
        events = await asyncio.to_thread(provider.find_movie_events, cfg["city"], cfg["movie"])
        cinemas = await asyncio.to_thread(provider.cinemas, cfg["city"])
        if norm(cfg.get("theater", "ANY")) == "any":
            venues = cinemas
        else:
            venues = [v for v in cinemas if theater_matches(cfg["theater"], v["name"])]
        found = set()
        for event in events[:3]:
            for venue in venues[:20]:
                shows = await asyncio.to_thread(provider.showtimes, cfg["city"], event["event_code"], cfg["date"], venue["code"])
                for show in shows:
                    lang = str(show.get("language") or "").strip()
                    if lang:
                        found.add(lang)
        langs = sorted(found, key=norm)
    except Exception as e:
        log.warning("Could not discover languages during setup: %s", e)
    if not langs:
        langs = ["Tamil", "Telugu", "Hindi", "Malayalam", "Kannada", "English"]
    buttons = []
    for lang in langs[:8]:
        buttons.append(InlineKeyboardButton(lang, callback_data=f"LANG:{lang}"))
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("ANY Language", callback_data="LANG:ANY")])
    rows.append([InlineKeyboardButton("✍️ Type language", callback_data="LANG:CUSTOM")])
    await message.reply_text("🗣️ *5/9* Language — choose the language you want to watch:", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return LANGUAGE

async def set_language(update, context):
    q = update.callback_query; await q.answer()
    value = q.data.split(":", 1)[1]
    if value == "CUSTOM":
        await q.edit_message_text("✍️ Type the movie language (e.g. Tamil, Telugu, Hindi, English):")
        return LANGUAGE
    context.user_data["cfg"]["language"] = value
    return await ask_format(q.message)

async def set_custom_language(update, context):
    context.user_data["cfg"]["language"] = update.message.text.strip()
    return await ask_format(update.message)

async def ask_format(message):
    kb = [[InlineKeyboardButton("IMAX", callback_data="IMAX"), InlineKeyboardButton("3D", callback_data="3D")], [InlineKeyboardButton("ANY", callback_data="ANY")]]
    await message.reply_text("🎥 *6/9* Format:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return FORMAT

async def ask_time(message):
    kb = [[InlineKeyboardButton("Morning", callback_data="MORNING"), InlineKeyboardButton("Matinee", callback_data="MATINEE")], [InlineKeyboardButton("Evening", callback_data="EVENING"), InlineKeyboardButton("Night", callback_data="NIGHT")], [InlineKeyboardButton("ANY", callback_data="ANY")]]
    await message.reply_text("⏰ *7/9* Time window:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return TIME

async def set_time(update, context):
    q = update.callback_query; await q.answer(); context.user_data["cfg"]["time"] = q.data
    kb = [[InlineKeyboardButton(str(x), callback_data=str(x)) for x in (1, 2)], [InlineKeyboardButton(str(x), callback_data=str(x)) for x in (3, 4)]]
    await q.edit_message_text("🎟️ *8/9* Tickets needed:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return TICKETS

async def set_tickets(update, context):
    q = update.callback_query; await q.answer(); context.user_data["cfg"]["tickets"] = int(q.data)
    kb = [[InlineKeyboardButton("ANY Row", callback_data="ANY")], [InlineKeyboardButton("Back/Executive", callback_data="BACK"), InlineKeyboardButton("Middle Rows", callback_data="MIDDLE")]]
    await q.edit_message_text("💺 *9/9* Row preference, or type `M18` / `Row M` after selecting ANY:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ROW

async def finish_row(update, context):
    q = update.callback_query; await q.answer()
    context.user_data["cfg"]["row"] = q.data
    context.user_data["cfg"]["exact_seat"] = None
    return await activate(q.message, context)

async def finish_row_text(update, context):
    row, seat = parse_row_seat(update.message.text)
    context.user_data["cfg"]["row"] = row
    context.user_data["cfg"]["exact_seat"] = f"{row}{seat}" if seat is not None else None
    return await activate(update.message, context)

async def activate(message, context):
    chat_id = message.chat.id
    cfg = context.user_data["cfg"]
    save_alert(chat_id, cfg)
    for j in context.application.job_queue.get_jobs_by_name(str(chat_id)):
        j.schedule_removal()
    context.application.job_queue.run_repeating(scan_job, interval=CHECK_INTERVAL, first=1, chat_id=chat_id, name=str(chat_id))
    await message.reply_text(format_config(cfg) + f"\n\n🔄 Monitoring every {CHECK_INTERVAL // 60} min. An alert is sent only for a matching show that is actually returned by the showtime source.")
    return ConversationHandler.END


def format_config(c):
    return ("✅ *Alert activated*\n\n"
            f"🎬 {c['movie']}\n📍 {c['city']}\n🏢 {c.get('theater','ANY')}\n🗣️ {c.get('language','ANY')}\n🎥 {c.get('format','ANY')}\n"
            f"📅 {c.get('date','ANY')}\n⏰ {c.get('time','ANY')}\n🎟️ {c.get('tickets',1)}\n💺 {c.get('row','ANY')}")

async def scan_job(context):
    chat_id = context.job.chat_id
    cfg = load_alert(chat_id)
    if not cfg:
        return
    try:
        result = await asyncio.to_thread(scan, cfg)
        if result["status"] != "open":
            log.info("No matching show for %s: %s", chat_id, result["reason"])
            return
        for show in result["shows"]:
            key = f"{show['event_code']}|{show['venue_code']}|{show['session_id']}|{show['date']}"
            if was_alerted(chat_id, key):
                continue
            mark_alerted(chat_id, key)
            seats = show.get("available_seats", [])
            seat_text = f"\n💺 Seats: {', '.join(seats[:12])}" if seats else ""
            msg = ("🚨 *MATCHING SHOW IS OPEN* 🚨\n\n"
                   f"🎬 *{show['movie_name']}*\n"
                   f"🏢 {show['venue']}\n"
                   f"📅 {show['date']}\n"
                   f"⏰ {show['time']}\n"
                   f"🗣️ {show.get('language') or 'Language not reported'}\n🎥 {show.get('format') or 'Format not reported'}"
                   f"{seat_text}\n\n"
                   f"🔗 {show['booking_url']}")
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", disable_web_page_preview=False)
    except Exception as e:
        log.exception("Scan failed")
        with db() as c:
            c.execute("UPDATE alerts SET last_error=?, last_scan=? WHERE chat_id=?", (str(e)[:500], datetime.now().isoformat(), chat_id))

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_alert(update.effective_chat.id)
    if not cfg:
        await update.message.reply_text("No active monitor. Use /alert first.")
        return
    await update.message.reply_text("🔍 Checking now…")
    await scan_job(type("J", (), {"chat_id": update.effective_chat.id})())

async def status(update, context):
    cfg = load_alert(update.effective_chat.id)
    await update.message.reply_text(format_config(cfg) if cfg else "ℹ️ No active monitor.", parse_mode="Markdown")

async def stop(update, context):
    chat_id = update.effective_chat.id
    for j in context.job_queue.get_jobs_by_name(str(chat_id)):
        j.schedule_removal()
    disable_alert(chat_id)
    await update.message.reply_text("🛑 Monitoring stopped.")

async def cancel(update, context):
    await update.message.reply_text("Setup canceled.")
    return ConversationHandler.END

# ----------------------------
# Main
# ----------------------------

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("alert", alert_start)],
        states={
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_location)],
            MOVIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_movie)],
            LANGUAGE: [CallbackQueryHandler(set_language), MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_language)],
            FORMAT: [CallbackQueryHandler(set_format)],
            THEATER: [CallbackQueryHandler(set_theater)],
            CUSTOM_THEATER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_theater)],
            DATE: [CallbackQueryHandler(set_date)],
            CUSTOM_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_date)],
            TIME: [CallbackQueryHandler(set_time)],
            TICKETS: [CallbackQueryHandler(set_tickets)],
            ROW: [CallbackQueryHandler(finish_row), MessageHandler(filters.TEXT & ~filters.COMMAND, finish_row_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(conv)

    # Restore monitors after a process restart.
    with db() as c:
        rows = c.execute("SELECT chat_id FROM alerts WHERE enabled=1").fetchall()
    for r in rows:
        app.job_queue.run_repeating(scan_job, interval=CHECK_INTERVAL, first=5, chat_id=r[0], name=str(r[0]))

    log.info("Movie Ticket Alert Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
