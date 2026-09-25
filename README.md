# Movie Radar Telegram Bot

Telegram movie-show alert bot for BookMyShow, using Playwright.

## Render

Create a **Web Service** and use the included `render.yaml`, or configure:

- Build: `pip install --upgrade pip && pip install -r requirements.txt && python -m playwright install --with-deps chromium`
- Start: `python -u movie_ticket_bot.py`
- Health check: `/`
- Required environment variable: `TELEGRAM_BOT_TOKEN`

The bot starts Telegram polling before Playwright/Chromium is launched. Chromium is started lazily when the first scan is requested.

### Commands

`/start` `/help` `/alert` `/alerts` `/check` `/status` `/stop <id>` `/stopall` `/cancel`

### Important

BookMyShow can change its page structure or block automated browsing. The scanner is deliberately conservative and will not claim a booking match when showtime details cannot be read reliably.
