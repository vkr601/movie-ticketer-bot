import os, re, json, sqlite3, logging, asyncio, hashlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, ContextTypes, filters

try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DB_PATH = os.getenv('DB_PATH', 'movie_bot.sqlite3')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL_SECONDS', '600'))
PORT = int(os.getenv('PORT', '10000'))
BMS_CITY_BASE = 'https://in.bookmyshow.com/movies/{city}'

if not BOT_TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN is not set')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('movie-radar')

LOCATION, MOVIE, THEATER, DATE, LANGUAGE, FORMAT, TIME, TICKETS, ROW, CONFIRM = range(10)

TOP_THEATERS = {
    'chennai': [
        'PVR Palazzo (Nexus Vijaya Mall)',
        'PVR Grand Galada',
        'PVR ECR',
        'AGS Cinemas',
        'Luxe Cinemas',
        'Sathyam Cinemas',
        'Escape Cinemas',
        'Rohini Silver Screens',
    ]
}

# ---------------- Database ----------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_scan TEXT,
            last_error TEXT,
            last_result TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS alert_events (
            alert_id INTEGER NOT NULL,
            show_key TEXT NOT NULL,
            alerted_at TEXT NOT NULL,
            PRIMARY KEY(alert_id, show_key)
        )''')

def create_alert(chat_id, cfg):
    now = datetime.now().isoformat(timespec='seconds')
    with db() as c:
        cur = c.execute('INSERT INTO alerts(chat_id,config_json,status,created_at,updated_at) VALUES(?,?,?,?,?)', (chat_id,json.dumps(cfg), 'active', now, now))
        return cur.lastrowid

def get_alert(alert_id, chat_id=None):
    with db() as c:
        if chat_id is None:
            r=c.execute('SELECT * FROM alerts WHERE id=?',(alert_id,)).fetchone()
        else:
            r=c.execute('SELECT * FROM alerts WHERE id=? AND chat_id=?',(alert_id,chat_id)).fetchone()
    return r

def list_alerts(chat_id, include_stopped=False):
    with db() as c:
        if include_stopped:
            return c.execute('SELECT * FROM alerts WHERE chat_id=? ORDER BY id',(chat_id,)).fetchall()
        return c.execute("SELECT * FROM alerts WHERE chat_id=? AND status IN ('active','paused') ORDER BY id",(chat_id,)).fetchall()

def set_alert_status(alert_id, chat_id, status):
    with db() as c:
        c.execute('UPDATE alerts SET status=?, updated_at=? WHERE id=? AND chat_id=?',(status,datetime.now().isoformat(timespec='seconds'),alert_id,chat_id))

def update_scan(alert_id, result=None, error=None):
    with db() as c:
        c.execute('UPDATE alerts SET last_scan=?, last_result=?, last_error=? WHERE id=?',(datetime.now().isoformat(timespec='seconds'), result, error, alert_id))

def event_seen(alert_id, key):
    with db() as c:
        return c.execute('SELECT 1 FROM alert_events WHERE alert_id=? AND show_key=?',(alert_id,key)).fetchone() is not None

def mark_event(alert_id,key):
    with db() as c:
        c.execute('INSERT OR IGNORE INTO alert_events VALUES(?,?,?)',(alert_id,key,datetime.now().isoformat(timespec='seconds')))

# ---------------- Helpers ----------------
def norm(s): return re.sub(r'\s+',' ',str(s or '')).strip().lower()
def compact(s): return re.sub(r'[^a-z0-9]+','',norm(s))

def theater_match(pref, actual):
    if norm(pref) in ('any','any theater',''): return True
    a,b=compact(pref),compact(actual)
    return a in b or b in a

def language_match(pref, actual):
    if norm(pref) in ('any','any language',''): return True
    aliases={'tamil':['tamil','ta'],'telugu':['telugu','te'],'hindi':['hindi','hi'],'english':['english','en'],'malayalam':['malayalam','ml'],'kannada':['kannada','kn']}
    p=norm(pref); a=norm(actual)
    return bool(a) and any(x==a or x in a or a in x for x in aliases.get(p,[p]))

def format_match(pref, actual):
    if norm(pref) in ('any','any format',''): return True
    return norm(pref) in norm(actual) or norm(actual) in norm(pref)

def time_match(pref, value):
    p=norm(pref).upper()
    if p in ('ANY',''): return True
    m=re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?',str(value).lower())
    if not m: return True
    h=int(m.group(1)); minute=int(m.group(2) or 0); ap=m.group(3)
    if ap=='pm' and h!=12:h+=12
    if ap=='am' and h==12:h=0
    mins=h*60+minute
    ranges={'MORNING':(0,720),'MATINEE':(720,960),'EVENING':(960,1200),'NIGHT':(1200,1440)}
    lo,hi=ranges.get(p,(0,1440)); return lo<=mins<hi

def parse_date(text):
    s=norm(text); now=datetime.now()
    if s in ('today','tdy'): return now.date()
    if s in ('tomorrow','tmrw'): return (now+timedelta(days=1)).date()
    fmts=['%d %b %Y','%d %B %Y','%d %b','%d %B','%d-%m-%Y','%d/%m/%Y','%Y-%m-%d']
    for f in fmts:
        try:
            d=datetime.strptime(s.title(),f)
            if '%Y' not in f: d=d.replace(year=now.year)
            return d.date()
        except ValueError: pass
    return None

def fmt_date(iso):
    if not iso:return 'Any date'
    return datetime.strptime(iso,'%Y-%m-%d').strftime('%d %b %Y')

def parse_row(text):
    s=text.strip().upper()
    m=re.fullmatch(r'([A-Z]{1,3})\s*[- ]?\s*(\d+)',s)
    if m:return m.group(1),m.group(1)+m.group(2)
    m=re.fullmatch(r'(?:ROW\s*)?([A-Z]{1,3})',s)
    if m:return m.group(1),None
    return 'ANY',None

def cfg_summary(c):
    return (f"🎬 *{c['movie']}*\n📍 {c['city']}\n🏢 {c.get('theater','ANY')}\n🗣️ {c.get('language','ANY')}\n"
            f"🎥 {c.get('format','ANY')}\n📅 {fmt_date(c.get('date'))}\n⏰ {c.get('time','ANY')}\n🎟️ {c.get('tickets',1)} ticket(s)\n💺 {c.get('row','ANY')}")

# ---------------- Browser provider ----------------
class BMSBrowser:
    def __init__(self): self.pw=None; self.browser=None; self.context=None

    async def start(self):
        if async_playwright is None: raise RuntimeError('Playwright is not installed')
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
        self.context=await self.browser.new_context(locale='en-IN', timezone_id='Asia/Kolkata', user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36')

    async def close(self):
        if self.context: await self.context.close()
        if self.browser: await self.browser.close()
        if self.pw: await self.pw.stop()

    async def scan(self,cfg):
        if not self.context: await self.start()
        city=compact(cfg['city'])
        url=BMS_CITY_BASE.format(city=city)
        page=await self.context.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2500)
            body=(await page.locator('body').inner_text(timeout=10000))
            target=norm(cfg['movie'])
            if target not in norm(body):
                # fallback: search title words individually
                words=[w for w in re.findall(r'[a-z0-9]+',target) if len(w)>2]
                if not words or sum(w in norm(body) for w in words)<max(1,len(words)//2):
                    return {'status':'not_found','reason':'movie_not_found','shows':[],'source_url':url}
            # Attempt to locate movie links and inspect candidate pages.
            links=await page.locator('a').evaluate_all("els => els.map(a => ({text:(a.innerText||'').trim(), href:a.href})).filter(x=>x.href)")
            candidates=[]
            for x in links:
                t=norm(x['text'])
                if target in t or compact(cfg['movie']) in compact(t):
                    if '/movies/' in x['href']:
                        candidates.append(x['href'])
            candidates=list(dict.fromkeys(candidates))[:3]
            if not candidates:
                return {'status':'movie_found_no_detail','reason':'movie_found_but_detail_link_not_found','shows':[],'source_url':url}
            shows=[]
            for href in candidates:
                p=await self.context.new_page()
                try:
                    await p.goto(href,wait_until='domcontentloaded',timeout=30000)
                    await p.wait_for_timeout(2500)
                    text=await p.locator('body').inner_text(timeout=10000)
                    parsed=self.parse_detail_text(text,cfg,href)
                    shows.extend(parsed)
                finally:
                    await p.close()
            # Deduplicate
            out=[]; seen=set()
            for s in shows:
                k=(s['theater'],s['date'],s['time'],s['language'],s['format'])
                if k not in seen: seen.add(k); out.append(s)
            return {'status':'open' if out else 'not_open','reason':'ok' if out else 'no_matching_show','shows':out,'source_url':url}
        finally: await page.close()

    def parse_detail_text(self,text,cfg,url):
        # BMS changes its DOM often. We intentionally refuse to fabricate show data.
        # If structured showtime text is not detectable, return no match rather than a false alert.
        date=cfg.get('date')
        lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines() if x.strip()]
        shows=[]
        theater=cfg.get('theater','ANY')
        langs=['Tamil','Telugu','Hindi','English','Malayalam','Kannada']
        fmts=['IMAX','4DX','3D','2D','DOLBY','PXL','SCREENX','ICE']
        # Conservative heuristic: identify blocks around theater names and time tokens.
        time_re=re.compile(r'\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:AM|PM)\b',re.I)
        for i,line in enumerate(lines):
            if not theater_match(theater,line) and norm(theater)!='any': continue
            window=' | '.join(lines[max(0,i-2):min(len(lines),i+12)])
            times=time_re.findall(window)
            if not times: continue
            lang=next((x for x in langs if x.lower() in window.lower()),'')
            fmt=next((x for x in fmts if x.lower() in window.lower()),'')
            for t in times:
                if language_match(cfg.get('language','ANY'),lang) and format_match(cfg.get('format','ANY'),fmt) and time_match(cfg.get('time','ANY'),t):
                    shows.append({'movie':cfg['movie'],'theater':line,'date':date,'time':t,'language':lang,'format':fmt,'booking_url':url})
        return shows

provider=BMSBrowser()

# ---------------- Telegram UI ----------------
def main_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Alert',callback_data='ADD')],[InlineKeyboardButton('🎬 My Alerts',callback_data='ALERTS'),InlineKeyboardButton('🔍 Check All',callback_data='CHECKALL')],[InlineKeyboardButton('⚙️ Help',callback_data='HELP')]])

async def start(update,context):
    await update.message.reply_text('🎬 *Movie Radar*\n\nTrack multiple movies at once and get notified when a matching show is detected.\n\nUse the buttons below or /alert.',parse_mode='Markdown',reply_markup=main_menu())

async def alert_start(update,context):
    if update.callback_query:
        q=update.callback_query; await q.answer(); await q.message.reply_text('📍 *1/9* City (e.g. Chennai):',parse_mode='Markdown')
    else:
        await update.message.reply_text('📍 *1/9* City (e.g. Chennai):',parse_mode='Markdown')
    context.user_data['cfg']={'theater':'ANY','language':'ANY','format':'ANY','date':None,'time':'ANY','tickets':1,'row':'ANY','exact_seat':None}
    return LOCATION

async def set_location(update,context):
    context.user_data['cfg']['city']=update.message.text.strip(); await update.message.reply_text('🎬 *2/9* Movie name:',parse_mode='Markdown'); return MOVIE
async def set_movie(update,context):
    context.user_data['cfg']['movie']=update.message.text.strip()
    city=norm(context.user_data['cfg']['city']); ts=TOP_THEATERS.get(city,[])
    rows=[[InlineKeyboardButton('ANY Theater',callback_data='TH:ANY')]]+[[InlineKeyboardButton(t,callback_data='TH:'+t)] for t in ts[:8]]+[[InlineKeyboardButton('✍️ Type theater',callback_data='TH:CUSTOM')]]
    await update.message.reply_text('🏢 *3/9* Theater:',reply_markup=InlineKeyboardMarkup(rows),parse_mode='Markdown'); return THEATER
async def set_theater(update,context):
    q=update.callback_query; await q.answer(); v=q.data.split(':',1)[1]
    if v=='CUSTOM': await q.edit_message_text('✍️ Type theater name or partial name:'); return THEATER
    context.user_data['cfg']['theater']=v; return await ask_date(q.message,context)
async def set_custom_theater(update,context):
    context.user_data['cfg']['theater']=update.message.text.strip(); return await ask_date(update.message,context)
async def ask_date(message,context):
    kb=[[InlineKeyboardButton('Today',callback_data='D:TODAY'),InlineKeyboardButton('Tomorrow',callback_data='D:TOMORROW')],[InlineKeyboardButton('📅 Pick a date',callback_data='D:PICK')],[InlineKeyboardButton('Any date',callback_data='D:ANY')]]
    await message.reply_text('📅 *4/9* Show date:',reply_markup=InlineKeyboardMarkup(kb),parse_mode='Markdown'); return DATE

def calendar(year,month):
    import calendar
    rows=[[InlineKeyboardButton(f'{datetime(year,month,1):%B %Y}',callback_data='NOOP')]]
    rows.append([InlineKeyboardButton(x,callback_data='NOOP') for x in ['Mo','Tu','We','Th','Fr','Sa','Su']])
    weeks=[]
    for week in calendar.monthcalendar(year,month):
        row=[]
        for d in week:
            row.append(InlineKeyboardButton(' ' if d==0 else str(d),callback_data='NOOP' if d==0 else f'CAL:{year}-{month:02d}-{d:02d}'))
        weeks.append(row)
    rows.extend(weeks)
    prev=(datetime(year,month,1)-timedelta(days=1)).replace(day=1); nxt=(datetime(year,month,28)+timedelta(days=4)).replace(day=1)
    rows.append([InlineKeyboardButton('‹',callback_data=f'CALNAV:{prev.year}-{prev.month}'),InlineKeyboardButton('Cancel',callback_data='D:CANCEL'),InlineKeyboardButton('›',callback_data=f'CALNAV:{nxt.year}-{nxt.month}')])
    return InlineKeyboardMarkup(rows)
async def set_date(update,context):
    q=update.callback_query; await q.answer(); v=q.data.split(':',1)[1]
    if v=='PICK':
        n=datetime.now(); await q.edit_message_text('📅 Pick a date:',reply_markup=calendar(n.year,n.month)); return DATE
    if v=='ANY': context.user_data['cfg']['date']=None
    elif v=='TODAY': context.user_data['cfg']['date']=datetime.now().date().isoformat()
    elif v=='TOMORROW': context.user_data['cfg']['date']=(datetime.now()+timedelta(days=1)).date().isoformat()
    elif v=='CANCEL': return await ask_date(q.message,context)
    return await ask_language(q.message,context)
async def calendar_action(update,context):
    q=update.callback_query; await q.answer(); v=q.data.split(':',1)[1]
    if q.data.startswith('CALNAV:'):
        y,m=map(int,v.split('-')); await q.edit_message_reply_markup(reply_markup=calendar(y,m)); return DATE
    if q.data.startswith('CAL:'):
        d=datetime.strptime(v,'%Y-%m-%d').date()
        if d<datetime.now().date(): await q.answer('Please choose today or a future date.',show_alert=True); return DATE
        context.user_data['cfg']['date']=v; return await ask_language(q.message,context)
    return DATE
async def ask_language(message,context):
    rows=[[InlineKeyboardButton('Tamil',callback_data='L:Tamil'),InlineKeyboardButton('Telugu',callback_data='L:Telugu')],[InlineKeyboardButton('Hindi',callback_data='L:Hindi'),InlineKeyboardButton('English',callback_data='L:English')],[InlineKeyboardButton('Malayalam',callback_data='L:Malayalam'),InlineKeyboardButton('Kannada',callback_data='L:Kannada')],[InlineKeyboardButton('Any language',callback_data='L:ANY'),InlineKeyboardButton('✍️ Custom',callback_data='L:CUSTOM')]]
    await message.reply_text('🗣️ *5/9* Language:',reply_markup=InlineKeyboardMarkup(rows),parse_mode='Markdown'); return LANGUAGE
async def set_language(update,context):
    q=update.callback_query; await q.answer(); v=q.data.split(':',1)[1]
    if v=='CUSTOM': await q.edit_message_text('✍️ Type language:'); return LANGUAGE
    context.user_data['cfg']['language']=v; return await ask_format(q.message)
async def set_custom_language(update,context): context.user_data['cfg']['language']=update.message.text.strip(); return await ask_format(update.message)
async def ask_format(message):
    kb=[[InlineKeyboardButton('IMAX',callback_data='F:IMAX'),InlineKeyboardButton('3D',callback_data='F:3D')],[InlineKeyboardButton('4DX',callback_data='F:4DX'),InlineKeyboardButton('Any format',callback_data='F:ANY')]]
    await message.reply_text('🎥 *6/9* Format:',reply_markup=InlineKeyboardMarkup(kb),parse_mode='Markdown'); return FORMAT
async def set_format(update,context):
    q=update.callback_query; await q.answer(); context.user_data['cfg']['format']=q.data.split(':',1)[1]; return await ask_time(q.message)
async def ask_time(message):
    kb=[[InlineKeyboardButton('Morning',callback_data='T:MORNING'),InlineKeyboardButton('Matinee',callback_data='T:MATINEE')],[InlineKeyboardButton('Evening',callback_data='T:EVENING'),InlineKeyboardButton('Night',callback_data='T:NIGHT')],[InlineKeyboardButton('Any time',callback_data='T:ANY')]]
    await message.reply_text('⏰ *7/9* Time:',reply_markup=InlineKeyboardMarkup(kb),parse_mode='Markdown'); return TIME
async def set_time(update,context):
    q=update.callback_query; await q.answer(); context.user_data['cfg']['time']=q.data.split(':',1)[1]
    kb=[[InlineKeyboardButton('1',callback_data='K:1'),InlineKeyboardButton('2',callback_data='K:2')],[InlineKeyboardButton('3',callback_data='K:3'),InlineKeyboardButton('4',callback_data='K:4')]]
    await q.edit_message_text('🎟️ *8/9* Tickets:',reply_markup=InlineKeyboardMarkup(kb),parse_mode='Markdown'); return TICKETS
async def set_tickets(update,context):
    q=update.callback_query; await q.answer(); context.user_data['cfg']['tickets']=int(q.data.split(':')[1]);
    kb=[[InlineKeyboardButton('Any row',callback_data='R:ANY'),InlineKeyboardButton('Middle rows',callback_data='R:MIDDLE')],[InlineKeyboardButton('Back/Executive',callback_data='R:BACK')],[InlineKeyboardButton('✍️ Exact row/seat',callback_data='R:CUSTOM')]]
    await q.edit_message_text('💺 *9/9* Seat preference:',reply_markup=InlineKeyboardMarkup(kb),parse_mode='Markdown'); return ROW
async def set_row(update,context):
    q=update.callback_query; await q.answer(); v=q.data.split(':')[1]
    if v=='CUSTOM': await q.edit_message_text('✍️ Type e.g. `M` or `M18`:',parse_mode='Markdown'); return ROW
    context.user_data['cfg']['row']=v; return await confirm(q.message,context)
async def set_row_text(update,context):
    row,seat=parse_row(update.message.text); context.user_data['cfg']['row']=row; context.user_data['cfg']['exact_seat']=seat; return await confirm(update.message,context)
async def confirm(message,context):
    kb=[[InlineKeyboardButton('🚨 Start Monitoring',callback_data='CONFIRM:YES')],[InlineKeyboardButton('✏️ Start over',callback_data='CONFIRM:NO')]]
    await message.reply_text('Review your alert:\n\n'+cfg_summary(context.user_data['cfg']),parse_mode='Markdown',reply_markup=InlineKeyboardMarkup(kb)); return CONFIRM
async def finish_confirm(update,context):
    q=update.callback_query; await q.answer(); v=q.data.split(':')[1]
    if v=='NO': return await alert_start(update,context)
    aid=create_alert(q.message.chat.id,context.user_data['cfg'])
    await q.edit_message_text(f'🟢 *Alert #{aid} is active*\n\n{cfg_summary(context.user_data["cfg"])}\n\nI will check automatically every {CHECK_INTERVAL//60} minutes.',parse_mode='Markdown')
    return ConversationHandler.END

# ---------------- Commands ----------------
def row_for_alert(r):
    c=json.loads(r['config_json']); status={'active':'🟢','paused':'⏸','stopped':'⚫'}.get(r['status'],'⚪')
    return f"*#{r['id']}* {status} {c['movie']} — {c.get('format','ANY')} — {c.get('theater','ANY')} — {fmt_date(c.get('date'))}"
async def alerts_cmd(update,context):
    rows=list_alerts(update.effective_chat.id)
    if not rows: await update.message.reply_text('No alerts yet. Use /alert to add one.'); return
    buttons=[[InlineKeyboardButton(f"#{r['id']} {json.loads(r['config_json'])['movie']}",callback_data=f'VIEW:{r["id"]}')] for r in rows]
    buttons.append([InlineKeyboardButton('➕ Add alert',callback_data='ADD'),InlineKeyboardButton('🔍 Check all',callback_data='CHECKALL')])
    await update.message.reply_text('🎬 *My Movie Radar*\n\n'+'\n'.join(row_for_alert(r) for r in rows),parse_mode='Markdown',reply_markup=InlineKeyboardMarkup(buttons))
async def view_alert(update,context):
    q=update.callback_query; await q.answer(); aid=int(q.data.split(':')[1]); r=get_alert(aid,q.message.chat.id)
    if not r: await q.answer('Alert not found',show_alert=True); return
    c=json.loads(r['config_json']); kb=[[InlineKeyboardButton('🔍 Check now',callback_data=f'CHECK:{aid}')],[InlineKeyboardButton('⏸ Pause' if r['status']=='active' else '▶️ Resume',callback_data=f"TOGGLE:{aid}")],[InlineKeyboardButton('🛑 Stop',callback_data=f'STOP:{aid}')]]
    await q.edit_message_text(f"*Alert #{aid}* — {r['status']}\n\n{cfg_summary(c)}",parse_mode='Markdown',reply_markup=InlineKeyboardMarkup(kb))
async def check_one(chat_id,aid,bot):
    r=get_alert(aid,chat_id)
    if not r:return 'Alert not found.'
    c=json.loads(r['config_json'])
    try:
        result=await provider.scan(c); update_scan(aid,result=result['reason'],error=None)
    except Exception as e:
        update_scan(aid,error=str(e),result='error'); return f"⚠️ *Check failed*\n\n{c['movie']}\n\n`{str(e)[:500]}`"
    if result['status']=='open':
        lines=[f"🚨 *MATCH FOUND — Alert #{aid}*",'',cfg_summary(c),'','*Shows:*']
        for s in result['shows'][:10]: lines.append(f"• {s['time']} — {s['theater']} — {s.get('language') or '?'} — {s.get('format') or '?'}\n  {s['booking_url']}")
        return '\n'.join(lines)
    if result['status']=='movie_found_no_detail': return f"🔍 *Check complete — Alert #{aid}*\n\n{cfg_summary(c)}\n\n🟡 Movie appears to be listed, but I could not reliably read its showtime details. No false alert was sent."
    if result['status']=='not_found': return f"🔍 *Check complete — Alert #{aid}*\n\n{cfg_summary(c)}\n\n❌ Movie was not detected for this city."
    return f"🔍 *Check complete — Alert #{aid}*\n\n{cfg_summary(c)}\n\n⏳ No matching bookable show was detected yet."
async def check_cmd(update,context):
    rows=list_alerts(update.effective_chat.id)
    if not rows: await update.message.reply_text('No alerts. Use /alert first.'); return
    await update.message.reply_text('🔍 Checking your active alerts now…')
    for r in rows:
        if r['status']=='active': await update.message.reply_text(await check_one(update.effective_chat.id,r['id'],context.bot),parse_mode='Markdown',disable_web_page_preview=True)
async def checkall_callback(update,context):
    q=update.callback_query; await q.answer(); await q.message.reply_text('🔍 Checking all active alerts…')
    for r in list_alerts(q.message.chat.id):
        if r['status']=='active': await q.message.reply_text(await check_one(q.message.chat.id,r['id'],context.bot),parse_mode='Markdown',disable_web_page_preview=True)
async def status_cmd(update,context):
    rows=list_alerts(update.effective_chat.id,True)
    if not rows: await update.message.reply_text('No alerts configured.'); return
    await update.message.reply_text('🤖 *Radar status*\n\n'+'\n'.join(row_for_alert(r)+f"\nLast scan: {r['last_scan'] or 'never'}" for r in rows),parse_mode='Markdown')
async def stop_cmd(update,context):
    rows=list_alerts(update.effective_chat.id)
    if not rows: await update.message.reply_text('No active alerts.'); return
    if context.args:
        aid=int(context.args[0]); set_alert_status(aid,update.effective_chat.id,'stopped'); await update.message.reply_text(f'🛑 Alert #{aid} stopped.'); return
    await update.message.reply_text('Use /alerts and choose an alert to stop, or `/stop <id>`.',parse_mode='Markdown')
async def stopall_cmd(update,context):
    with db() as c:c.execute("UPDATE alerts SET status='stopped',updated_at=? WHERE chat_id=? AND status IN ('active','paused')",(datetime.now().isoformat(timespec='seconds'),update.effective_chat.id))
    await update.message.reply_text('🛑 All alerts stopped.')
async def toggle_callback(update,context):
    q=update.callback_query; await q.answer(); aid=int(q.data.split(':')[1]); r=get_alert(aid,q.message.chat.id)
    if not r:return
    new='paused' if r['status']=='active' else 'active'; set_alert_status(aid,q.message.chat.id,new); await view_alert(update,context)
async def stop_callback(update,context):
    q=update.callback_query; await q.answer(); aid=int(q.data.split(':')[1]); set_alert_status(aid,q.message.chat.id,'stopped'); await q.edit_message_text(f'🛑 Alert #{aid} stopped.')
async def check_callback(update,context):
    q=update.callback_query; await q.answer('Checking…'); await q.message.reply_text(await check_one(q.message.chat.id,int(q.data.split(':')[1]),context.bot),parse_mode='Markdown',disable_web_page_preview=True)
async def help_cmd(update,context):
    message = update.effective_message
    if message:
        await message.reply_text(
            '🎬 *Movie Radar commands*\n\n'
            '/alert — add a new alert\n'
            '/alerts — view/manage alerts\n'
            '/check — check all active alerts now\n'
            '/status — show monitoring status\n'
            '/stop <id> — stop one alert\n'
            '/stopall — stop all alerts\n'
            '/cancel — cancel the current alert setup\n'
            '/help — this help',
            parse_mode='Markdown'
        )

# ---------------- Background ----------------
async def monitor_job(context):
    rows=list_alerts_all()
    for r in rows:
        if r['status']!='active': continue
        aid=r['id']; chat=r['chat_id']; c=json.loads(r['config_json'])
        try:
            result=await provider.scan(c); update_scan(aid,result=result['reason'],error=None)
            if result['status']=='open':
                for s in result['shows']:
                    key=hashlib.sha1(json.dumps([s.get('movie'),s.get('theater'),s.get('date'),s.get('time'),s.get('language'),s.get('format')]).encode()).hexdigest()
                    if event_seen(aid,key):continue
                    mark_event(aid,key)
                    await context.bot.send_message(chat_id=chat,text=f"🚨 *BOOKING MATCH — Alert #{aid}*\n\n🎬 {s['movie']}\n🏢 {s['theater']}\n📅 {fmt_date(s['date'])}\n⏰ {s['time']}\n🗣️ {s.get('language') or 'Not reported'}\n🎥 {s.get('format') or 'Not reported'}\n\n🔗 {s['booking_url']}",parse_mode='Markdown',disable_web_page_preview=False)
        except Exception as e: update_scan(aid,error=str(e),result='error')

def list_alerts_all():
    with db() as c:return c.execute("SELECT * FROM alerts WHERE status='active' ORDER BY id").fetchall()

# ---------------- Health / startup ----------------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'Movie Radar Bot is Live')
    def log_message(self,*args):pass

def run_health(): HTTPServer(('0.0.0.0',PORT),Health).serve_forever()

async def post_init(app):
    init_db()
    await provider.start()
    app.job_queue.run_repeating(monitor_job,interval=CHECK_INTERVAL,first=10,name='global-monitor')

async def post_shutdown(app): await provider.close()

def main():
    init_db()
    app=ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    conv=ConversationHandler(
        entry_points=[CommandHandler('alert',alert_start),CallbackQueryHandler(alert_start,pattern='^ADD$')],
        states={
            LOCATION:[MessageHandler(filters.TEXT&~filters.COMMAND,set_location)],
            MOVIE:[MessageHandler(filters.TEXT&~filters.COMMAND,set_movie)],
            THEATER:[CallbackQueryHandler(set_theater,pattern='^TH:'),MessageHandler(filters.TEXT&~filters.COMMAND,set_custom_theater)],
            DATE:[CallbackQueryHandler(set_date,pattern='^D:'),CallbackQueryHandler(calendar_action,pattern='^(CAL:|CALNAV:)')],
            LANGUAGE:[CallbackQueryHandler(set_language,pattern='^L:'),MessageHandler(filters.TEXT&~filters.COMMAND,set_custom_language)],
            FORMAT:[CallbackQueryHandler(set_format,pattern='^F:')],
            TIME:[CallbackQueryHandler(set_time,pattern='^T:')],
            TICKETS:[CallbackQueryHandler(set_tickets,pattern='^K:')],
            ROW:[CallbackQueryHandler(set_row,pattern='^R:'),MessageHandler(filters.TEXT&~filters.COMMAND,set_row_text)],
            CONFIRM:[CallbackQueryHandler(finish_confirm,pattern='^CONFIRM:')],
        },fallbacks=[
            CommandHandler('cancel',lambda u,c: ConversationHandler.END),
            CommandHandler('help',help_cmd)
        ],per_message=False)
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('alerts',alerts_cmd)); app.add_handler(CommandHandler('check',check_cmd)); app.add_handler(CommandHandler('status',status_cmd)); app.add_handler(CommandHandler('stop',stop_cmd)); app.add_handler(CommandHandler('stopall',stopall_cmd)); app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(view_alert,pattern='^VIEW:'))
    app.add_handler(CallbackQueryHandler(check_callback,pattern='^CHECK:'))
    app.add_handler(CallbackQueryHandler(checkall_callback,pattern='^CHECKALL$'))
    app.add_handler(CallbackQueryHandler(toggle_callback,pattern='^TOGGLE:'))
    app.add_handler(CallbackQueryHandler(stop_callback,pattern='^STOP:'))
    app.add_handler(CallbackQueryHandler(lambda u,c: help_cmd(u,c),pattern='^HELP$'))
    import threading; threading.Thread(target=run_health,daemon=True).start()
    log.info('Movie Radar starting')
    app.run_polling()

if __name__=='__main__': main()
