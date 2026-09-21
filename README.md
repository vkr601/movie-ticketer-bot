# Movie Ticket Alert Bot

Telegram bot that monitors BookMyShow showtimes and alerts only when a matching show is detected.

## What was fixed
- Persistent SQLite configuration instead of in-memory alerts.
- Monitors survive a Render/process restart.
- Movie, city, theater, date, format and time are actually used during matching.
- Duplicate show alerts are suppressed.
- Direct booking URL is included when the provider supplies an event URL.
- `/check`, `/status`, `/stop` and `/alert` commands.
- Dynamic city/theater discovery instead of a Chennai-only hardcoded list.
- Seat/row preference is attempted only when a parsable seat map is returned.
- Network failures are logged and stored rather than crashing the bot.
- Scanning is moved to a worker thread so HTTP calls do not block Telegram.

## Install

```bash
pip install -r requirements.txt
```

Set `TELEGRAM_BOT_TOKEN`, then:

```bash
python movie_ticket_bot.py
```

## Important limitation
BookMyShow does not expose this internal data surface as a stable public API. This project uses known internal endpoints and therefore may need parser/endpoint updates if BookMyShow changes them. The bot intentionally does not claim that a movie is "open" merely because its name appears on a city page: it requires a matching showtime response.

For production reliability, a maintained third-party BookMyShow data provider or a browser-based fallback can be added behind the `BookMyShowProvider` interface.

## Language filtering
- Language is selected after movie + theater + show date.
- The bot first queries the selected movie/theater/date combination and shows the languages actually reported by BookMyShow.
- `ANY Language` is available.
- A custom language can be entered if the provider reports a different spelling.
- During scanning, the language is matched against each show's reported language, so a Tamil show will not trigger a Telugu-only alert.
