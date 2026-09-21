
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from rapidfuzz import process
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# States
LOCATION, MOVIE, FORMAT, THEATER_CHOICE, CUSTOM_THEATER, DATE_CHOICE, CUSTOM_DATE, TIME, SEATS, ROW_PREF = range(10)

user_alerts = {}

TOP_THEATERS = {
    "chennai": [
        "PVR Palazzo (Nexus Vijaya Mall)",
        "PVR Grand Galada",
        "SPI Escape (Express Avenue)",
        "Santham / Sathyam Cinemas",
        "AGS Cinemas (T. Nagar)"
    ]
}

# 1. Health-Check & Keep-Alive for Render
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Movie Ticketer Bot is Live")

def run_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def keep_alive_ping():
    """Self-pings every 10 mins to prevent Render Web Service from sleeping."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        while True:
            time.sleep(600)
            try:
                requests.get(f"{render_url}/", timeout=5)
            except Exception:
                pass

# 2. Parsing Helpers
def parse_seat_and_row_input(input_str):
    clean = input_str.strip().upper()
    seat_match = re.match(r"^([A-Z])\s*[-]?\s*(\d+)$", clean)
    if seat_match:
        return seat_match.group(1), int(seat_match.group(2))
    
    row_match = re.match(r"^(?:ROW\s*)?([A-Z])$", clean)
    if row_match:
        return row_match.group(1), None
    return clean, None

def resolve_theater_name(city, raw_input):
    theaters = TOP_THEATERS.get(city.lower(), TOP_THEATERS["chennai"])
    match, score, _ = process.extractOne(raw_input, theaters)
    if score >= 60:
        return match
    return raw_input.title()

def parse_date_input(raw_date):
    clean = raw_date.strip().lower()
    today = datetime.now()
    if clean in ["today", "tdy"]:
        return today.strftime("%d %b").upper()
    elif clean in ["tomorrow", "tmrw"]:
        return (today + timedelta(days=1)).strftime("%d %b").upper()
    elif "weekend" in clean:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        return f"{saturday.strftime('%d %b')} (Weekend)".upper()
    return raw_date.upper()

# 3. Core Commands & Prompts
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 *Welcome to Movie Ticketer Bot!*\n\n"
        "Here are the available commands:\n\n"
        "🚨 */alert* — Set up continuous 24/7 ticket tracking.\n"
        "   _The bot will monitor showtimes every 10–12 minutes and alert you immediately when tickets open._\n\n"
        "🔍 */check* — Perform an immediate, one-time ticket availability scan.\n"
        "   _Use this if you want to inspect a movie's status right now without activating background alerts._\n\n"
        "🛑 */stop* — Cancel active background tracking.\n\n"
        "Tap /alert to set up your first monitor!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def quick_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in user_alerts and user_alerts[chat_id]:
        await update.message.reply_text("🔍 Running an instant check on your configured alert...")
        perform_ticket_check(chat_id, user_alerts[chat_id], context)
    else:
        await update.message.reply_text("❌ No active configuration found. Please run /alert first to configure your target movie and theater.")

async def stop_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    if jobs:
        for job in jobs:
            job.schedule_removal()
        await update.message.reply_text("🛑 Active 10-minute ticket alert stopped.")
    else:
        await update.message.reply_text("ℹ️ No active background alert running.")

# 4. Interactive Conversation Flow
async def alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_alerts[update.effective_chat.id] = {}
    await update.message.reply_text("📍 *Step 1/8:* Enter your city (e.g., Chennai):", parse_mode="Markdown")
    return LOCATION

async def set_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_alerts[update.effective_chat.id]['location'] = update.message.text.strip().lower()
    await update.message.reply_text("🎬 *Step 2/8:* Enter the movie name (e.g., Odyssey):", parse_mode="Markdown")
    return MOVIE

async def set_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_alerts[update.effective_chat.id]['movie'] = update.message.text.strip().upper()
    keyboard = [
        [InlineKeyboardButton("IMAX", callback_data="IMAX"), InlineKeyboardButton("PXL", callback_data="PXL")],
        [InlineKeyboardButton("3D", callback_data="3D"), InlineKeyboardButton("ANY Format", callback_data="ANY")]
    ]
    await update.message.reply_text("🎥 *Step 3/8:* Select format preference:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return FORMAT

async def set_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user_alerts[chat_id]['format'] = query.data
    city = user_alerts[chat_id].get('location', 'chennai')
    
    keyboard = []
    for t in TOP_THEATERS.get(city, TOP_THEATERS["chennai"]):
        keyboard.append([InlineKeyboardButton(t, callback_data=t)])
    
    keyboard.append([InlineKeyboardButton("✍️ Type Custom / Partial Theater Name", callback_data="CUSTOM")])
    keyboard.append([InlineKeyboardButton("ANY Theater", callback_data="ANY")])
    
    await query.edit_message_text("🏢 *Step 4/8:* Select or type a theater name:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return THEATER_CHOICE

async def handle_theater_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    
    if query.data == "CUSTOM":
        await query.edit_message_text("✏️ Type the theater name (typos/partial names allowed):")
        return CUSTOM_THEATER
    else:
        user_alerts[chat_id]['theater'] = query.data
        return await ask_date(query.message, edit=True)

async def set_custom_theater(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    city = user_alerts[chat_id].get('location', 'chennai')
    matched_theater = resolve_theater_name(city, update.message.text)
    user_alerts[chat_id]['theater'] = matched_theater
    await update.message.reply_text(f"🎯 Matched Theater: *{matched_theater}*", parse_mode="Markdown")
    return await ask_date(update.message, edit=False)

async def ask_date(message_obj, edit=False):
    keyboard = [
        [InlineKeyboardButton("Today", callback_data="TODAY"), InlineKeyboardButton("Tomorrow", callback_data="TOMORROW")],
        [InlineKeyboardButton("✍️ Type Specific Date / Weekend", callback_data="CUSTOM")],
        [InlineKeyboardButton("ANY Date", callback_data="ANY")]
    ]
    text = "📅 *Step 5/8:* Select show date:"
    if edit:
        await message_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return DATE_CHOICE

async def handle_date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    
    if query.data == "CUSTOM":
        await query.edit_message_text("✏️ Type your target date (e.g., '28 Sep', 'This Weekend', 'Month End'):")
        return CUSTOM_DATE
    else:
        user_alerts[chat_id]['date'] = parse_date_input(query.data)
        return await ask_time(query.message, edit=True)

async def set_custom_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parsed_date = parse_date_input(update.message.text)
    user_alerts[chat_id]['date'] = parsed_date
    return await ask_time(update.message, edit=False)

async def ask_time(message_obj, edit=False):
    keyboard = [
        [InlineKeyboardButton("Morning (< 12 PM)", callback_data="MORNING"), InlineKeyboardButton("Matinee (12-4 PM)", callback_data="MATINEE")],
        [InlineKeyboardButton("Evening (4-8 PM)", callback_data="EVENING"), InlineKeyboardButton("Night (> 8 PM)", callback_data="NIGHT")],
        [InlineKeyboardButton("ANY Time", callback_data="ANY")]
    ]
    text = "⏰ *Step 6/8:* Select show time window:"
    if edit:
        await message_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return TIME

async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_alerts[query.message.chat.id]['time'] = query.data
    
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"), InlineKeyboardButton("2", callback_data="2")],
        [InlineKeyboardButton("3", callback_data="3"), InlineKeyboardButton("4", callback_data="4")]
    ]
    await query.edit_message_text("🎟️ *Step 7/8:* Select ticket count:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SEATS

async def set_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_alerts[query.message.chat.id]['seats'] = query.data
    
    keyboard = [
        [InlineKeyboardButton("Back / Executive", callback_data="BACK"), InlineKeyboardButton("Middle Rows", callback_data="MIDDLE")],
        [InlineKeyboardButton("ANY Row", callback_data="ANY")]
    ]
    await query.edit_message_text(
        "💺 *Step 8/8:* Select row tier OR reply with exact seat/row (e.g., 'M18' or 'Row M'):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ROW_PREF

async def set_row_pref_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user_alerts[chat_id]['row'], user_alerts[chat_id]['exact_seat'] = parse_seat_and_row_input(query.data)
    return await finalize_alert(query.message, chat_id, edit=True, context=context)

async def set_row_pref_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    row, seat = parse_seat_and_row_input(update.message.text)
    user_alerts[chat_id]['row'] = row
    user_alerts[chat_id]['exact_seat'] = seat
    return await finalize_alert(update.message, chat_id, edit=False, context=context)

# 5. Background Repeating Worker
async def check_ticket_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    perform_ticket_check(job.chat_id, job.data, context)

async def finalize_alert(message_obj, chat_id, edit=False, context=None):
    data = user_alerts[chat_id]
    seat_detail = f"Row {data['row']}"
    if data.get('exact_seat'):
        seat_detail += f" (Seat {data['exact_seat']})"
    
    summary = (
        f"✅ *Alert Activated! Immediate scan starting...*\n\n"
        f"📍 *City:* {data['location'].title()}\n"
        f"🎬 *Movie:* {data['movie']}\n"
        f"🎥 *Format:* {data['format']}\n"
        f"🏢 *Theater:* {data['theater']}\n"
        f"📅 *Date:* {data['date']}\n"
        f"⏰ *Time:* {data['time']}\n"
        f"🎟️ *Tickets:* {data['seats']}\n"
        f"💺 *Target Seat/Row:* {seat_detail}\n\n"
        f"🔄 *Schedule:* Automatically scanning every *10–12 minutes* 24/7.\n"
        f"💡 Use /check anytime for an instant scan, or /stop to cancel."
    )
    if edit:
        await message_obj.edit_message_text(summary, parse_mode="Markdown")
    else:
        await message_obj.reply_text(summary, parse_mode="Markdown")
        
    # 1. Perform instant scan right away
    perform_ticket_check(chat_id, data, context)
    
    # 2. Schedule recurring scan every 600 seconds (10 minutes)
    if context and context.job_queue:
        # Clear existing job if any
        existing_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
        for j in existing_jobs:
            j.schedule_removal()
            
        context.job_queue.run_repeating(
            callback=check_ticket_job,
            interval=600,  # 10 minutes (600 seconds)
            first=600,
            chat_id=chat_id,
            data=data,
            name=str(chat_id)
        )
    return ConversationHandler.END

def perform_ticket_check(chat_id, alert_data, context):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        search_url = f"https://in.bookmyshow.com/explore/movies-{alert_data['location']}"
        resp = requests.get(search_url, headers=headers, timeout=8)
        
        movie_found = alert_data['movie'].lower() in resp.text.lower()
        
        if movie_found:
            msg = (
                f"🚨 *TICKETS / SHOWTIMES ARE OPEN!* 🚨\n\n"
                f"Showtimes detected for *{alert_data['movie']}* in {alert_data['location'].title()}!\n"
                f"Theater: {alert_data['theater']} | Format: {alert_data['format']}\n"
                f"Target Seat/Row: Row {alert_data['row']}\n\n"
                f"Open BookMyShow immediately to book!"
            )
            context.application.create_task(
                context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            )
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Checked {alert_data['movie']} for {chat_id}: Not open yet.")
    except Exception as e:
        print(f"Check error: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Alert setup canceled.")
    return ConversationHandler.END

if __name__ == "__main__":
    threading.Thread(target=run_health_check, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("alert", alert_start)],
        states={
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_location)],
            MOVIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_movie)],
            FORMAT: [CallbackQueryHandler(set_format, per_message=False)],
            THEATER_CHOICE: [CallbackQueryHandler(handle_theater_choice, per_message=False)],
            CUSTOM_THEATER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_theater)],
            DATE_CHOICE: [CallbackQueryHandler(handle_date_choice, per_message=False)],
            CUSTOM_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_custom_date)],
            TIME: [CallbackQueryHandler(set_time, per_message=False)],
            SEATS: [CallbackQueryHandler(set_seats, per_message=False)],
            ROW_PREF: [
                CallbackQueryHandler(set_row_pref_button, per_message=False),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_row_pref_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", quick_check_command))
    app.add_handler(CommandHandler("stop", stop_alert))
    app.add_handler(conv_handler)
    
    print("Bot is listening for commands...")
    app.run_polling()
