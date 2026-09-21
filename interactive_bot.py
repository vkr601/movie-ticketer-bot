import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Conversation States
LOCATION, MOVIE, FORMAT, THEATER, DATE, TIME, SEATS = range(7)

# User session storage
user_alerts = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome to Movie Ticketer Bot!\nSend /alert to set up a new ticket alert.")

async def alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_alerts[update.effective_chat.id] = {}
    await update.message.reply_text("📍 Step 1/7: Enter your city (e.g., Chennai):")
    return LOCATION

async def set_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_alerts[update.effective_chat.id]['location'] = update.message.text.strip().lower()
    await update.message.reply_text("🎬 Step 2/7: Enter the exact movie name (e.g., Odyssey):")
    return MOVIE

async def set_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_alerts[update.effective_chat.id]['movie'] = update.message.text.strip().upper()
    
    keyboard = [
        [InlineKeyboardButton("IMAX", callback_data="IMAX"), InlineKeyboardButton("PXL", callback_data="PXL")],
        [InlineKeyboardButton("3D", callback_data="3D"), InlineKeyboardButton("ANY Format", callback_data="ANY")]
    ]
    await update.message.reply_text("🎥 Step 3/7: Select format preference:", reply_markup=InlineKeyboardMarkup(keyboard))
    return FORMAT

async def set_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_alerts[query.message.chat.id]['format'] = query.data
    
    keyboard = [
        [InlineKeyboardButton("PVR Palazzo (Nexus Mall)", callback_data="pvr-palazzo-the-nexus-vijaya-mall")],
        [InlineKeyboardButton("ANY Theater in City", callback_data="ANY")]
    ]
    await query.edit_message_text("🏢 Step 4/7: Select theater preference:", reply_markup=InlineKeyboardMarkup(keyboard))
    return THEATER

async def set_theater(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_alerts[query.message.chat.id]['theater'] = query.data
    
    keyboard = [
        [InlineKeyboardButton("Today", callback_data="TODAY"), InlineKeyboardButton("Tomorrow", callback_data="TOMORROW")],
        [InlineKeyboardButton("ANY Date", callback_data="ANY")]
    ]
    await query.edit_message_text("📅 Step 5/7: Select show date:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DATE

async def set_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_alerts[query.message.chat.id]['date'] = query.data
    
    keyboard = [
        [InlineKeyboardButton("Morning", callback_data="MORNING"), InlineKeyboardButton("Evening", callback_data="EVENING")],
        [InlineKeyboardButton("ANY Time", callback_data="ANY")]
    ]
    await query.edit_message_text("⏰ Step 6/7: Select show time window:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TIME

async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_alerts[query.message.chat.id]['time'] = query.data
    
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"), InlineKeyboardButton("2", callback_data="2")],
        [InlineKeyboardButton("3", callback_data="3"), InlineKeyboardButton("4", callback_data="4")]
    ]
    await query.edit_message_text("🎟️ Step 7/7: Select number of contiguous seats required:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SEATS

async def set_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user_alerts[chat_id]['seats'] = query.data
    
    data = user_alerts[chat_id]
    summary = (
        f"✅ *Alert Activated!*\n\n"
        f"📍 City: {data['location'].title()}\n"
        f"🎬 Movie: {data['movie']}\n"
        f"🎥 Format: {data['format']}\n"
        f"🏢 Theater: {data['theater']}\n"
        f"📅 Date: {data['date']}\n"
        f"⏰ Time: {data['time']}\n"
        f"🎟️ Seats: {data['seats']}\n\n"
        f"I will scan continuously and alert you the instant tickets drop!"
    )
    await query.edit_message_text(summary, parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Alert setup canceled.")
    return ConversationHandler.END

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("alert", alert_start)],
        states={
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_location)],
            MOVIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_movie)],
            FORMAT: [CallbackQueryHandler(set_format)],
            THEATER: [CallbackQueryHandler(set_theater)],
            DATE: [CallbackQueryHandler(set_date)],
            TIME: [CallbackQueryHandler(set_time)],
            SEATS: [CallbackQueryHandler(set_seats)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    
    print("Bot is listening for commands...")
    app.run_polling()
