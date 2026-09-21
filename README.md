# Movie Radar Telegram Bot

A multi-alert Telegram movie ticket monitor designed for BookMyShow. It uses Playwright to load the live BookMyShow site rather than the old internal `serv/getData` requests that can return HTTP 403 from cloud hosts.

## Features
- Multiple independent alerts per Telegram user
- Movie, city, theater, language, format, date, time, ticket count and row preferences
- Calendar date picker; no fragile custom-date format required
- `/alerts` dashboard
- `/check` checks all active alerts and returns a result for every alert
- `/status`, `/stop <id>`, `/stopall`, `/help`
- Pause/resume and per-alert check buttons
- SQLite persistence so alerts survive restarts
- Duplicate notification prevention
- Conservative detection: if showtime details cannot be read reliably, it reports that instead of falsely claiming tickets are open
- Render health endpoint

## Render
Use a Web Service connected to GitHub. Recommended settings:

Build command:
`pip install -r requirements.txt && playwright install --with-deps chromium`

Start command:
`python movie_ticket_bot.py`

Health check path:
`/`

Environment variable:
`TELEGRAM_BOT_TOKEN=<BotFather token>`

## Important
BookMyShow's site structure and anti-bot behavior can change. This bot deliberately avoids treating a movie name appearing on a page as proof that booking is open. The Playwright provider should be tested against the current BookMyShow pages before relying on it for time-critical alerts.
