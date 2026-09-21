import os
import requests
from playwright.sync_api import sync_playwright

# Keys retrieved from GitHub Repository Secrets
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Dynamic parameters from GitHub Variables
THEATER_URL = os.environ.get("THEATER_URL", "https://in.bookmyshow.com/cinemas/chennai/pvr-palazzo-the-nexus-vijaya-mall/buytickets/PVPZ/")
MOVIE_NAME = os.environ.get("MOVIE_NAME", "").strip().upper()
SHOW_DATE = os.environ.get("SHOW_DATE", "").strip().upper()    # e.g., "25 SEP"
SHOW_TIME = os.environ.get("SHOW_TIME", "").strip().upper()    # e.g., "07:15 PM"
SEATS_NEEDED = int(os.environ.get("SEATS_NEEDED", "2"))

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")

def check_seats():
    if not MOVIE_NAME:
        print("Error: MOVIE_NAME variable is empty. Please configure it in GitHub Settings > Variables.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            # Navigate to cinema page on BookMyShow
            page.goto(THEATER_URL, wait_until="networkidle", timeout=60000)
            page_content = page.content().upper()
            
            # Step 1: Verify Movie & IMAX showtime availability
            if MOVIE_NAME in page_content and "IMAX" in page_content:
                
                # Check optional date filter
                if SHOW_DATE and SHOW_DATE not in page_content:
                    print(f"Movie found, but date '{SHOW_DATE}' is not open yet.")
                    return
                
                # Step 2: Query DOM for rows and available contiguous seats
                rows = page.query_selector_all(".srt-row, .seat-row, tr.row-container, ._row")
                found_seats = False
                
                for row in rows:
                    row_label = row.query_selector(".row-name, .seat-row-title, ._row-label")
                    row_name = row_label.inner_text().strip() if row_label else "Unknown"
                    available_seats = row.query_selector_all(".seat-available, ._available, .available, ._seat-active")
                    
                    if len(available_seats) >= SEATS_NEEDED:
                        found_seats = True
                        send_telegram(
                            f"🚨 *IMAX SEATS OPEN!* 🚨\n\n"
                            f"🎬 *Movie:* {MOVIE_NAME}\n"
                            f"📅 *Date/Time:* {SHOW_DATE} {SHOW_TIME}\n"
                            f"🎟️ *Seats Found:* {len(available_seats)} available in *Row {row_name}*\n\n"
                            f"[Book Now on BookMyShow]({THEATER_URL})"
                        )
                        break
                
                if not found_seats:
                    # General alert if showtime exists but seat parsing structure is protected
                    send_telegram(
                        f"🚨 *IMAX SHOWTIMES OPEN!* 🚨\n\n"
                        f"Showtimes for *{MOVIE_NAME}* are live at PVR Palazzo (Nexus Vijaya Mall).\n\n"
                        f"[Check Seats on BookMyShow]({THEATER_URL})"
                    )
            else:
                print(f"Showtimes for '{MOVIE_NAME}' not detected yet.")

        except Exception as e:
            print(f"Execution error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    check_seats()
