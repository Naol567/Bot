"""
Squad 4x Group Manager – Fixed for Railway/Render
- Gemini with model rotation & auto-switch
- Loading animation for /ask
- Proper session & DB path handling
- Channel post exemption (your channel messages are safe)
"""

import os
import asyncio
import logging
import json
import re
import pathlib
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone

from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    FloodWaitError, RPCError
)
import google.generativeai as genai

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==================== ENVIRONMENT VALIDATION ====================
def get_env_var(name: str, required: bool = True, default=None):
    value = os.environ.get(name, default)
    if required and value is None:
        log.critical(f"Missing required environment variable: {name}")
        raise ValueError(f"Missing required environment variable: {name}")
    return value

try:
    API_ID = int(get_env_var("API_ID"))
    API_HASH = get_env_var("API_HASH")
    BOT_TOKEN = get_env_var("BOT_TOKEN")
    ADMIN_ID = int(get_env_var("ADMIN_ID"))
    GROUP_ID = int(get_env_var("GROUP_ID"))
except ValueError as e:
    log.critical(f"Invalid environment variable: {e}")
    exit(1)

TARGET_BOT_USERNAME = get_env_var("TARGET_BOT_USERNAME", required=False, default="").lstrip("@")
TARGET_BOT_ID = int(get_env_var("TARGET_BOT_ID", required=False, default="0")) if get_env_var("TARGET_BOT_ID", required=False) else None
EXEMPT_CHANNEL_ID = int(get_env_var("EXEMPT_CHANNEL_ID", required=False, default="0")) if get_env_var("EXEMPT_CHANNEL_ID", required=False) else None

# ==================== SPAM & FOREX SAFE WORDS ====================
SPAM_CAPS_RATIO = 0.75
SPAM_REPEAT_CHARS = 6
SPAM_MAX_PUNCTUATION = 0.45
SPAM_MIN_WORD_LEN = 3
SPAM_REPEATED_WORDS = 4

FOREX_SAFE_WORDS = {
    "buy","sell","long","short","stop","loss","profit","pips","eurusd","gbpusd",
    "xauusd","usdjpy","gbpjpy","audusd","entry","exit","target","sl","tp","rr",
    "lot","leverage","bullish","bearish","breakout","support","resistance","ema",
    "rsi","macd","fib","fibonacci","trend","nfp","cpi","fomc","fed","news","analysis"
}

SPAM_PHRASES = [
    "dm for signals","dm me for signals","signal dm","vip group","paid signals",
    "premium signals","signal service","buy signals","sell signals","signals channel",
    "signals group","vip signals","exclusive signals","signals provider","signal master",
    "join my group","join our group","join my channel","join our channel",
    "subscribe to my channel","link in bio","check my bio","referral link","use my link",
    "invite link","click the link","follow me","follow us","guaranteed profit",
    "guaranteed returns","100% profit","risk free","risk-free","no loss","double your money",
    "managed account","send me money","send usdt","send btc","invest with me",
    "investment platform","fund your account","withdraw daily","earn daily",
    "earn money online","make money online","passive income","financial freedom",
    "get rich","millionaire","account for sale","selling account","buying account",
    "ea for sale","robot for sale","trading bot for sale","whatsapp me",
    "contact me on whatsapp","dm me","message me","inbox me","contact for promo",
    "available for hire","hire me","i offer services","we offer services","pm me",
    "ሲግናል ሸጭ","ቪፒ ቡድን","ነጻ ገንዘብ","ፈጣን ሀብት","ሂሳብ ሽያጭ"
]
_spam_phrase_re = re.compile(r'(?:' + '|'.join(re.escape(p) for p in SPAM_PHRASES) + r')', re.IGNORECASE)

# ==================== GEMINI SETUP ====================
_raw_keys = get_env_var("GEMINI_API_KEY", required=False, default="")
if not _raw_keys:
    log.warning("GEMINI_API_KEY not set – Gemini moderation disabled")
    GEMINI_KEYS = []
else:
    GEMINI_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

# Available models to rotate
GEMINI_MODELS = [
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro"
]
_current_model_idx = 0
_current_key_idx = 0
_gemini_quota_exhausted = False

def get_gemini_config():
    """Returns (api_key, model_name) for current rotation"""
    if not GEMINI_KEYS:
        raise RuntimeError("No Gemini keys configured")
    key = GEMINI_KEYS[_current_key_idx]
    model = GEMINI_MODELS[_current_model_idx % len(GEMINI_MODELS)]
    return key, model

def rotate_gemini():
    """Rotate either key or model when quota exhausted"""
    global _current_key_idx, _current_model_idx, _gemini_quota_exhausted
    # Try next model first
    _current_model_idx += 1
    if _current_model_idx >= len(GEMINI_MODELS):
        # All models exhausted for this key, try next key
        _current_model_idx = 0
        _current_key_idx = (_current_key_idx + 1) % len(GEMINI_KEYS)
        if _current_key_idx == 0:
            _gemini_quota_exhausted = True
            log.warning("All Gemini keys and models exhausted")
            return False
    _gemini_quota_exhausted = False
    log.info(f"🔄 Switched to key #{_current_key_idx+1}, model {GEMINI_MODELS[_current_model_idx]}")
    return True

async def call_gemini_with_rotation(prompt):
    """Call Gemini with automatic rotation on failure"""
    global _gemini_quota_exhausted
    if not GEMINI_KEYS or _gemini_quota_exhausted:
        return None
    attempts = 0
    max_attempts = len(GEMINI_KEYS) * len(GEMINI_MODELS) + 1
    while attempts < max_attempts:
        try:
            key, model_name = get_gemini_config()
            genai.configure(api_key=key)
            model = genai.GenerativeModel(model_name)
            response = await asyncio.to_thread(model.generate_content, prompt)
            return response.text
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err or "model not found" in err.lower():
                attempts += 1
                if not rotate_gemini():
                    return None
                continue
            log.warning(f"Gemini error (non-quota): {e}")
            return None
    return None

# ==================== SESSION PATH ====================
def prepare_session_path(raw_path, default_name):
    if not raw_path:
        raw_path = default_name
    path = pathlib.Path(raw_path)
    if path.suffix == ".session":
        path = path.with_suffix("")
    parent = path.parent
    if parent and str(parent) != "." and not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
            log.info(f"📁 Created session directory: {parent}")
        except PermissionError:
            # Fallback to /tmp if cannot write
            fallback = pathlib.Path(f"/tmp/{path.name}")
            log.warning(f"Cannot write to {parent}, using {fallback.parent}")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            return str(fallback)
    return str(path)

# ==================== TELEGRAM CLIENTS ====================
BOT_SESSION_PATH = get_env_var("BOT_SESSION_PATH", required=False, default="/data/bot_session")
bot_client = TelegramClient(prepare_session_path(BOT_SESSION_PATH, "bot_session"), API_ID, API_HASH)

USER_SESSION_PATH = get_env_var("USER_SESSION_PATH", required=False, default="/data/user_instance.session")
user_client = TelegramClient(prepare_session_path(USER_SESSION_PATH, "user_instance.session"), API_ID, API_HASH)

# ==================== GLOBAL STATE ====================
connect_state = {}
userbot_connected = False
admin_state = {}
silent_admin_state = {}

# ==================== SQLITE DATABASE ====================
DB_PATH = get_env_var("DB_PATH", required=False, default="/data/bot_data.db")
_db_lock = threading.Lock()

def _db_conn():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except PermissionError:
            # Fallback to /tmp
            global DB_PATH
            DB_PATH = "/tmp/bot_data.db"
            log.warning(f"Using fallback DB path: {DB_PATH}")
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with _db_lock:
        conn = _db_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS warnings (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0,
            username TEXT, full_name TEXT, last_reason TEXT, updated_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS silent_violations (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, updated_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS silent_words (word TEXT PRIMARY KEY)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)''')
        defaults = {
            'private_warning': 'off',
            'warning_duration': '300',
            'temp_ban_duration': '0',
            'delete_all_forwards': 'on',
            'forward_exempt_channels': '',
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?,?)", (k, v))
        conn.commit()
        conn.close()
    log.info("✅ Database ready")

def get_setting(key):
    with _db_lock:
        conn = _db_conn()
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None

def set_setting(key, value):
    with _db_lock:
        conn = _db_conn()
        conn.execute("REPLACE INTO bot_settings (key, value) VALUES (?,?)", (key, value))
        conn.commit()
        conn.close()
    log.info(f"Setting {key} = {value}")

def get_warning_count(user_id):
    with _db_lock:
        conn = _db_conn()
        row = conn.execute("SELECT count FROM warnings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return row[0] if row else 0

def record_violation(user_id, username, full_name, reason):
    with _db_lock:
        conn = _db_conn()
        conn.execute('''INSERT INTO warnings (user_id, count, username, full_name, last_reason, updated_at)
                        VALUES (?,1,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        count=count+1, username=excluded.username, full_name=excluded.full_name,
                        last_reason=excluded.last_reason, updated_at=excluded.updated_at''',
                     (user_id, username, full_name, reason, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    log.info("📋 User %s → %s warning(s)", user_id, get_warning_count(user_id))

def get_silent_words():
    with _db_lock:
        conn = _db_conn()
        rows = conn.execute("SELECT word FROM silent_words").fetchall()
        conn.close()
        return [r[0] for r in rows]

def add_silent_word(word):
    with _db_lock:
        conn = _db_conn()
        conn.execute("INSERT OR IGNORE INTO silent_words (word) VALUES (?)", (word.lower(),))
        conn.commit()
        conn.close()
    log.info("🔇 Silent word added: %s", word)

def remove_silent_word(word):
    with _db_lock:
        conn = _db_conn()
        conn.execute("DELETE FROM silent_words WHERE word=?", (word.lower(),))
        conn.commit()
        conn.close()
    log.info("🔇 Silent word removed: %s", word)

def get_silent_violation_count(user_id):
    with _db_lock:
        conn = _db_conn()
        row = conn.execute("SELECT count FROM silent_violations WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return row[0] if row else 0

def increment_silent_violation(user_id):
    with _db_lock:
        conn = _db_conn()
        conn.execute('''INSERT INTO silent_violations (user_id, count, updated_at)
                        VALUES (?,1,?) ON CONFLICT(user_id) DO UPDATE SET count=count+1, updated_at=excluded.updated_at''',
                     (user_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        new = conn.execute("SELECT count FROM silent_violations WHERE user_id=?", (user_id,)).fetchone()[0]
        conn.close()
        return new

def reset_silent_violation(user_id):
    with _db_lock:
        conn = _db_conn()
        conn.execute("DELETE FROM silent_violations WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

def message_contains_silent_word(text):
    lower = text.lower()
    for w in get_silent_words():
        if w in lower:
            return w
    return None

# ==================== SPAM DETECTION ====================
def is_spam(text):
    if not text:
        return False, ""
    text_lower = text.lower()
    words = text.split()
    wc = len(words)
    if _spam_phrase_re.search(text_lower):
        return True, "Spam phrase"
    lower_words = {w.lower().strip(".,!?()[]") for w in words}
    if lower_words & FOREX_SAFE_WORDS:
        return False, ""
    letters = sum(1 for c in text if c.isascii() and c.isalpha())
    if letters > 15:
        caps = sum(1 for c in text if c.isascii() and c.isupper())
        if caps/letters > SPAM_CAPS_RATIO:
            return True, f"Excessive caps ({caps/letters*100:.0f}%)"
    if re.search(r'(.)\1{' + str(SPAM_REPEAT_CHARS) + r',}', text):
        return True, "Repeated chars"
    if len(text) > 20:
        punct = sum(1 for c in text if not c.isalnum() and not c.isspace())
        if punct/len(text) > SPAM_MAX_PUNCTUATION:
            return True, "Too many punctuation/emojis"
    if wc >= SPAM_REPEATED_WORDS+1:
        cnt = Counter(words)
        for w, c in cnt.items():
            if c >= SPAM_REPEATED_WORDS and len(w) > SPAM_MIN_WORD_LEN:
                return True, f"Word '{w}' repeated {c} times"
    if wc >= 3:
        ascii_words = [w for w in words if w.isascii()]
        if len(ascii_words) >= 3:
            garbage = sum(1 for w in ascii_words if len(w)>=4 and not re.search(r'[aeiouAEIOU]', w))
            if garbage/len(ascii_words) > 0.5:
                return True, "Random keyboard spam"
    return False, ""

# ==================== SUSPICIOUS PATTERNS (GEMINI TRIGGER) ====================
SUSPICIOUS_PATTERNS = [
    r"https?://", r"t\.me/", r"bit\.ly", r"wa\.me",
    r"\b(usdt|btc|eth|crypto|wallet|deposit|withdraw)\b",
    r"\b(profit|income|earn|money|invest|fund)\b",
    r"\b(vip|premium|paid|sell|buy|hire|promo|referral)\b",
    r"\b(channel|group|subscribe|follow|contact|whatsapp)\b",
    r"(ትርፍ|ገንዘብ|ሲግናል|ቡድን|ቻናል|ሊንክ|ኢንቨስት|አካውንት)",
]
_suspicious_re = re.compile("|".join(SUSPICIOUS_PATTERNS), re.IGNORECASE)

# ==================== GEMINI QUEUE ====================
GEMINI_CALL_GAP = 10
MIN_WORDS_GEMINI = 5
_gemini_queue = asyncio.Queue()
_last_gemini_call = 0.0

async def gemini_queue_worker():
    global _last_gemini_call
    while True:
        text, future = await _gemini_queue.get()
        try:
            loop = asyncio.get_running_loop()
            now = loop.time()
            gap = GEMINI_CALL_GAP - (now - _last_gemini_call)
            if gap > 0:
                await asyncio.sleep(gap)
            result = await _call_gemini(text)
            _last_gemini_call = loop.time()
            if not future.done():
                future.set_result(result)
        except Exception as e:
            if not future.done():
                future.set_exception(e)
        finally:
            _gemini_queue.task_done()

def should_use_gemini(text):
    if not GEMINI_KEYS:
        return False
    if len(text.strip().split()) < MIN_WORDS_GEMINI:
        return False
    return bool(_suspicious_re.search(text))

async def queue_gemini_analysis(text):
    if not should_use_gemini(text):
        return {"verdict": "ALLOWED", "reason": "Skipped"}
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await _gemini_queue.put((text, future))
    log.info("📥 Queued for Gemini (size: %s)", _gemini_queue.qsize())
    try:
        shielded = asyncio.shield(future)
        return await asyncio.wait_for(shielded, timeout=300)
    except asyncio.TimeoutError:
        if not future.done():
            future.cancel()
        return {"verdict": "ALLOWED", "reason": "Timeout"}
    except asyncio.CancelledError:
        return {"verdict": "ALLOWED", "reason": "Cancelled"}
    except Exception as e:
        log.warning("Queue error: %s", e)
        return {"verdict": "ALLOWED", "reason": "Error"}

SYSTEM_PROMPT = """You are the AI moderation engine for a professional Forex trading Telegram group.
Members write in BOTH English AND Amharic. Analyse both equally.
✅ ALWAYS ALLOW:
- Forex/crypto: currency pairs, trade ideas, entries, exits, SL/TP
- Technical/fundamental analysis, broker/platform discussion, risk management
- Market commentary, economic news, education, friendly chat, P&L sharing
❌ PROHIBITED:
1. Paid signal ads or VIP group recruitment
2. Scams: guaranteed profit, wallet deposit requests, managed accounts
3. Recruiting to other channels/groups, referral links
4. Personal insults or hate speech
5. Completely off-topic spam/advertising
RULES: When in doubt → ALLOWED.
Respond ONLY with valid JSON: {"verdict": "ALLOWED" or "PROHIBITED", "reason": "one sentence"}"""

async def _call_gemini(text):
    if not GEMINI_KEYS:
        return {"verdict": "ALLOWED", "reason": "No keys"}
    prompt = f"{SYSTEM_PROMPT}\n\nMessage:\n---\n{text[:2000]}\n---"
    response = await call_gemini_with_rotation(prompt)
    if response is None:
        return {"verdict": "ALLOWED", "reason": "Gemini offline"}
    try:
        raw = response.strip()
        raw = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        verdict = str(data.get("verdict", "ALLOWED")).upper()
        reason = str(data.get("reason", "No reason"))
        if verdict not in ("ALLOWED","PROHIBITED"):
            verdict = "ALLOWED"
        log.info(f"🤖 Gemini → {verdict} | {reason}")
        return {"verdict": verdict, "reason": reason}
    except Exception as e:
        log.warning(f"Gemini parse error: {e}")
        return {"verdict": "ALLOWED", "reason": "Parse error"}

# ==================== MODERATION HELPERS ====================
async def delete_msg(chat_id, msg_id):
    if userbot_connected:
        try:
            await user_client.delete_messages(chat_id, msg_id)
            return
        except Exception:
            pass
    try:
        await bot_client.delete_messages(chat_id, msg_id)
    except Exception:
        pass

async def ban_user(chat_id, user_id, hours=0):
    until = None
    if hours > 0:
        until = datetime.now(timezone.utc).timestamp() + (hours * 3600)
    try:
        await bot_client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(until_date=until, view_messages=True)
        ))
        log.info(f"Banned user {user_id}" + (f" for {hours}h" if hours else " permanently"))
    except Exception as e:
        log.error(f"Ban failed {user_id}: {e}")

async def send_private_warning(user_id, reason):
    try:
        await bot_client.send_message(user_id,
            f"⚠️ **Warning from Squad 4x group**\n\nYou violated rules: {reason}\n"
            f"This is your **only warning**. Next violation = ban.\n\n📋 Reason: {reason}",
            parse_mode="md")
        return True
    except:
        return False

async def send_warning(event, reason, user_id, username, full_name):
    private = get_setting('private_warning') == 'on'
    if private and await send_private_warning(user_id, reason):
        log.info(f"Private warning to {user_id}")
        return
    mention = f"@{username}" if username else f"[{full_name}](tg://user?id={user_id})"
    msg = await bot_client.send_message(event.chat_id,
        f"⚠️ **Warning** — {mention}\n\nThis is your **only warning**. Next violation = ban.\n\n📋 Reason: {reason}",
        parse_mode="md")
    duration = int(get_setting('warning_duration') or 300)
    async def delete_later():
        await asyncio.sleep(duration)
        await delete_msg(event.chat_id, msg.id)
    asyncio.create_task(delete_later())
    log.info(f"Group warning to {user_id}")

async def notify_admin(user_id, username, full_name, text, reason, action):
    try:
        tag = f"@{username}" if username else f"ID:{user_id}"
        await bot_client.send_message(ADMIN_ID,
            f"🚫 **Moderation Report**\n"
            f"**Action:** {action}\n**User:** {full_name} ({tag})\n**ID:** `{user_id}`\n\n"
            f"**Message:**\n```\n{text[:500]}\n```\n\n**Reason:** {reason}\n"
            f"**Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            parse_mode="md")
    except Exception as e:
        log.error(f"Admin notify failed: {e}")

def keyword_is_banned(text):
    low = text.lower()
    for w in banned_words:
        if w.lower() in low:
            return w
    return None

async def _handle_violation(event, uid, uname, fullname, cid, msg_text, reason, is_bot):
    await delete_msg(cid, event.id)
    if is_bot:
        log.info(f"Bot message deleted (no warn): {fullname}")
        return
    prior = get_warning_count(uid)
    if prior == 0:
        record_violation(uid, uname, fullname, reason)
        await send_warning(event, reason, uid, uname, fullname)
    else:
        record_violation(uid, uname, fullname, reason)
        hours = int(get_setting('temp_ban_duration') or 0)
        await ban_user(cid, uid, hours)
        await notify_admin(uid, uname, fullname, msg_text, reason,
                           f"BANNED ({hours}h)" if hours else "PERMANENT BAN")

# ==================== BANNED WORDS ====================
banned_words = [
    "dm me for signals","dm for signals","i sell signals","selling signals",
    "join my vip","join our vip","vip signals","paid signals","premium signals",
    "signal provider","signal service","buy signals","join my group","join our group",
    "join my channel","join our channel","subscribe to my channel","click the link",
    "link in bio","check my bio","use my referral","referral link","use my link",
    "register with my link","deposit via my link","use my code","promo code","invite link",
    "guaranteed profit","guaranteed return","100% profit","risk free","risk-free",
    "no loss","double your money","i will manage your account","managed account",
    "send me money","send usdt","send btc","invest with me","investment platform",
    "fund your account","withdraw daily","earn daily","earn money online",
    "make money online","passive income","financial freedom","account for sale",
    "selling account","buying account","broker account for sale","ea for sale",
    "robot for sale","whatsapp me","contact me on whatsapp","dm me","message me",
    "inbox me","contact for promo","available for hire","hire me","i offer services",
    "we offer services","you idiot","you are stupid","you are dumb","you fool",
    "shut up","go to hell","son of a bitch","motherfucker","you loser",
    "ሲግናል እሸጣለሁ","ሲግናል እልካለሁ","ሲግናል ይግዙ","ሲግናል ይጠቀሙ","ሲግናል ቡድን",
    "ዲኤም አድርጉ","ዲኤም አድርጉኝ","ለሲግናል ዲኤም","ቪአይፒ ቡድን","ቪአይፒ ይቀላቀሉ","ሲግናል ለማግኘት",
    "ቡድኑን ይቀላቀሉ","ቻናሉን ይቀላቀሉ","ሊንኩን ይጫኑ","ሊንክ ይጠቀሙ","ሪፈራል ሊንክ",
    "ሊንኬን ተጠቀሙ","ቻናሌን ተቀላቀሉ","ቡድኔን ተቀላቀሉ","ሊንኩን ተጫኑ",
    "ትርፍ እናረጋግጣለን","ትርፍ ዋስትና","መቶ ፐርሰንት ትርፍ","ኪሳራ የለም",
    "ገንዘብ ይላኩ","ዩኤስዲቲ ይላኩ","ቢቲሲ ይላኩ","ሂሳብዎን ያስተዳድሩ",
    "ሂሳብ ያስተዳድራለሁ","ኢንቨስት ያድርጉ","ኢንቨስትመንት","ትርፍ ያግኙ",
    "ዕለታዊ ትርፍ","ገንዘብ ያስቀምጡ","ፈጣን ትርፍ","ሀብት ይሁኑ",
    "አካውንት ይሸጣል","አካውንት እሸጣለሁ","አካውንት ለሽያጭ","ሮቦት ለሽያጭ","ኢኤ ለሽያጭ",
    "ዋትሳፕ ያግኙኝ","ቴሌግራም ያግኙኝ","ያናግሩኝ","መልዕክት ይላኩልኝ",
    "ደደብ ነህ","ደደብ ነሽ","ሞኝ ነህ","ሞኝ ነሽ","ዝምበል","ውሻ","አህያ",
    "ጅል ነህ","ጅል ነሽ","ከንቱ","ጊዜ ሌባ","ፋይዳ የለህም","ፋይዳ የለሽም"
]

# ==================== INLINE KEYBOARDS ====================
def make_filter_keyboard():
    return [
        [Button.inline("➕ Add Word", b"filter_add"), Button.inline("➖ Remove Word", b"filter_remove")],
        [Button.inline("📋 Show All Words", b"filter_show")]
    ]
def make_silent_filter_keyboard():
    return [
        [Button.inline("➕ Add Silent Word", b"silent_add"), Button.inline("➖ Remove Silent Word", b"silent_remove")],
        [Button.inline("📋 Show Silent Words", b"silent_show")]
    ]

def get_main_settings_keyboard():
    return [
        [Button.inline("⚠️ Warning Settings", b"set_warning")],
        [Button.inline("🔨 Ban Settings", b"set_ban")],
        [Button.inline("🔇 Silent Filter", b"set_silent")],
        [Button.inline("📤 Forward Control", b"set_forward")],
        [Button.inline("❌ Close", b"settings_close")]
    ]

def get_warning_keyboard():
    private = get_setting('private_warning')
    duration = get_setting('warning_duration')
    return [
        [Button.inline(f"Private warning: {'✅ ON' if private=='on' else '❌ OFF'}", b"toggle_private_warning")],
        [Button.inline(f"Warning duration: {duration}s", b"set_warning_duration")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_ban_keyboard():
    temp = get_setting('temp_ban_duration')
    display = "Permanent" if temp == '0' else f"{temp} hours"
    return [
        [Button.inline(f"Temporary ban: {display}", b"set_temp_ban")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_forward_keyboard():
    delete_all = get_setting('delete_all_forwards')
    exempt = get_setting('forward_exempt_channels')
    exempt_display = exempt if exempt else "None"
    return [
        [Button.inline(f"Delete all forwards: {'✅ ON' if delete_all=='on' else '❌ OFF'}", b"toggle_delete_forwards")],
        [Button.inline(f"Exempt channels: {exempt_display[:20]}", b"set_exempt_channels")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_silent_keyboard():
    return [
        [Button.inline("Manage silent words → /silentfilter", b"nothing")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

# ==================== COMMANDS ====================
@bot_client.on(events.NewMessage(pattern="/start"))
async def start_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    status = "✅ Connected" if userbot_connected else "❌ Not connected — use /connect"
    target = f"@{TARGET_BOT_USERNAME}" if TARGET_BOT_USERNAME else str(TARGET_BOT_ID or "Not set")
    exempt = str(EXEMPT_CHANNEL_ID) if EXEMPT_CHANNEL_ID else "Not set"
    await event.reply(
        f"🤖 **Squad 4x Manager**\n\n👤 Userbot: {status}\n🎯 Target bot: {target}\n🛡️ Exempt channel: {exempt}\n\n"
        "**Commands:**\n/connect — Login userbot\n/filter — Manage banned words\n/silentfilter — Silent words\n"
        "/settings — Open settings\n/ask <question> — Ask AI\n/status — Health\n/cancel — Cancel"
    )

@bot_client.on(events.NewMessage(pattern="/connect"))
async def connect_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    global userbot_connected
    if userbot_connected:
        await event.reply("✅ Userbot already connected.")
        return
    session_file = USER_SESSION_PATH + ".session"
    if os.path.exists(session_file):
        await event.reply("🔄 Session found. Reconnecting...")
        try:
            await user_client.connect()
            if await user_client.is_user_authorized():
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot reconnected as {me.first_name} (@{me.username or 'no username'})")
                return
        except Exception as e:
            log.warning(f"Reconnect failed: {e}")
    connect_state[ADMIN_ID] = {"step": "phone"}
    await event.reply("📱 Send your phone number with country code (e.g., +251912345678)")

@bot_client.on(events.NewMessage(pattern="/cancel"))
async def cancel_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    connect_state.pop(ADMIN_ID, None)
    admin_state.pop(ADMIN_ID, None)
    silent_admin_state.pop(ADMIN_ID, None)
    await event.reply("✅ Cancelled.")

@bot_client.on(events.NewMessage(pattern="/filter"))
async def filter_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(f"🔧 **Keyword Filter**\n{len(banned_words)} words.", buttons=make_filter_keyboard())

@bot_client.on(events.NewMessage(pattern="/silentfilter"))
async def silent_filter_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(f"🔇 **Silent Filter**\n{len(get_silent_words())} words.", buttons=make_silent_filter_keyboard())

@bot_client.on(events.NewMessage(pattern="/settings"))
async def settings_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply("⚙️ **Bot Settings**\nChoose a category:", buttons=get_main_settings_keyboard())

@bot_client.on(events.NewMessage(pattern="/ask"))
async def ask_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    question = event.raw_text.replace("/ask", "").strip()
    if not question:
        await event.reply("Example: `/ask What is a broker?`")
        return
    # Send loading message
    loading_msg = await event.reply("🤔 Thinking...")
    try:
        if not GEMINI_KEYS or _gemini_quota_exhausted:
            await loading_msg.edit("❌ Assistant offline. Contact admin.")
            return
        prompt = f"Answer concisely and professionally:\n{question}"
        response = await call_gemini_with_rotation(prompt)
        if response is None:
            await loading_msg.edit("❌ Gemini service unavailable. Try again later.")
            return
        answer = response.strip()
        # Delete loading message and send answer
        await loading_msg.delete()
        await event.reply(f"🤖 *Squad 4x Assistant:*\n\n{answer}", parse_mode="md")
    except Exception as e:
        log.error(f"Ask error: {e}")
        await loading_msg.edit("❌ An error occurred while processing your request.")

@bot_client.on(events.NewMessage(pattern="/status"))
async def status_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(
        f"📊 **Status**\nUserbot: {'✅' if userbot_connected else '❌'}\n"
        f"Gemini keys: {len(GEMINI_KEYS)} | Queue: {_gemini_queue.qsize()}\n"
        f"Banned words: {len(banned_words)} | Silent: {len(get_silent_words())}\n"
        f"Target bot: {TARGET_BOT_USERNAME or TARGET_BOT_ID}\n"
        f"Exempt channel: {EXEMPT_CHANNEL_ID}\n"
        f"Private warning: {get_setting('private_warning')}\n"
        f"Temp ban: {get_setting('temp_ban_duration')}h"
    )

# ==================== CALLBACK HANDLERS ====================
@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Admin only", alert=True)
        return
    data = event.data

    # Filter callbacks
    if data == b"filter_add":
        admin_state[ADMIN_ID] = "awaiting_add"
        await event.edit("Send word to ban.", buttons=[[Button.inline("Cancel", b"filter_cancel")]])
        return
    if data == b"filter_remove":
        if not banned_words:
            await event.answer("No words!", alert=True)
            return
        admin_state[ADMIN_ID] = "awaiting_remove"
        lst = "\n".join(f"{i+1}. `{w}`" for i,w in enumerate(banned_words))
        await event.edit(f"Current:\n{lst}\n\nSend exact word to remove:", buttons=[[Button.inline("Cancel", b"filter_cancel")]])
        return
    if data == b"filter_show":
        if not banned_words:
            await event.answer("Empty", alert=True)
            return
        await event.edit(f"📋 Banned words ({len(banned_words)}):\n" + "\n".join(f"• `{w}`" for w in banned_words), buttons=make_filter_keyboard())
        return
    if data == b"filter_cancel":
        admin_state.pop(ADMIN_ID, None)
        await event.edit("Cancelled.", buttons=make_filter_keyboard())
        return

    # Silent filter callbacks
    if data == b"silent_add":
        silent_admin_state[ADMIN_ID] = "awaiting_silent_add"
        await event.edit("Send silent word.", buttons=[[Button.inline("Cancel", b"silent_cancel")]])
        return
    if data == b"silent_remove":
        sw = get_silent_words()
        if not sw:
            await event.answer("No silent words!", alert=True)
            return
        silent_admin_state[ADMIN_ID] = "awaiting_silent_remove"
        lst = "\n".join(f"{i+1}. `{w}`" for i,w in enumerate(sw))
        await event.edit(f"Silent words:\n{lst}\n\nSend exact word to remove:", buttons=[[Button.inline("Cancel", b"silent_cancel")]])
        return
    if data == b"silent_show":
        sw = get_silent_words()
        if not sw:
            await event.answer("Empty", alert=True)
            return
        await event.edit(f"🔇 Silent words ({len(sw)}):\n" + "\n".join(f"• `{w}`" for w in sw), buttons=make_silent_filter_keyboard())
        return
    if data == b"silent_cancel":
        silent_admin_state.pop(ADMIN_ID, None)
        await event.edit("Cancelled.", buttons=make_silent_filter_keyboard())
        return

    # Settings callbacks
    try:
        d = data.decode()
    except:
        return

    if d == "settings_close":
        await event.edit("Settings closed.")
        return
    if d == "settings_back":
        await event.edit("⚙️ **Bot Settings**\nChoose a category:", buttons=get_main_settings_keyboard())
        return
    if d == "set_warning":
        await event.edit("⚠️ **Warning Settings**", buttons=get_warning_keyboard())
        return
    if d == "set_ban":
        await event.edit("🔨 **Ban Settings**", buttons=get_ban_keyboard())
        return
    if d == "set_silent":
        await event.edit("🔇 **Silent Filter**\nUse `/silentfilter` to manage words.", buttons=get_silent_keyboard())
        return
    if d == "set_forward":
        await event.edit("📤 **Forward Control**", buttons=get_forward_keyboard())
        return
    if d == "toggle_private_warning":
        cur = get_setting('private_warning')
        new = 'off' if cur == 'on' else 'on'
        set_setting('private_warning', new)
        await event.edit("⚠️ **Warning Settings**", buttons=get_warning_keyboard())
        await event.answer(f"Private warning turned {new}", alert=True)
        return
    if d == "set_warning_duration":
        admin_state[ADMIN_ID] = "awaiting_warning_duration"
        await event.edit("Send duration in seconds (e.g., 300 for 5 min).\nSend /cancel to abort.")
        return
    if d == "set_temp_ban":
        admin_state[ADMIN_ID] = "awaiting_temp_ban"
        await event.edit("Send hours (0 = permanent, e.g., 24 for one day).\nSend /cancel to abort.")
        return
    if d == "toggle_delete_forwards":
        cur = get_setting('delete_all_forwards')
        new = 'off' if cur == 'on' else 'on'
        set_setting('delete_all_forwards', new)
        await event.edit("📤 **Forward Control**", buttons=get_forward_keyboard())
        await event.answer(f"Delete all forwards turned {new}", alert=True)
        return
    if d == "set_exempt_channels":
        admin_state[ADMIN_ID] = "awaiting_exempt_channels"
        await event.edit("Send comma-separated channel IDs to exempt from forward deletion (e.g., -1001234567890,-1009876543210).\nSend /cancel to abort.")
        return
    if d == "nothing":
        await event.answer("No action", alert=True)
        return

# ==================== ADMIN PRIVATE MESSAGE HANDLER ====================
@bot_client.on(events.NewMessage)
async def admin_private_handler(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    if event.text and event.text.startswith("/"):
        return
    text = event.raw_text.strip()
    if not text:
        return

    # Connect flow
    conn = connect_state.get(ADMIN_ID)
    if conn:
        step = conn.get("step")
        if step == "phone":
            conn["phone"] = text
            try:
                await user_client.connect()
                result = await user_client.send_code_request(text)
                conn["phone_code_hash"] = result.phone_code_hash
                conn["step"] = "code"
                await event.reply("📩 Code sent. Send it with spaces (e.g., 1 2 3 4 5)")
            except FloodWaitError as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"Flood wait {e.seconds}s")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"Failed: {e}")
            return
        if step == "code":
            code = text.replace(" ", "")
            try:
                await user_client.sign_in(conn["phone"], code, phone_code_hash=conn["phone_code_hash"])
                connect_state.pop(ADMIN_ID, None)
                global userbot_connected
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot connected as {me.first_name} (@{me.username or 'no username'})")
            except SessionPasswordNeededError:
                conn["step"] = "password"
                await event.reply("🔐 2FA enabled. Send password:")
            except PhoneCodeInvalidError:
                await event.reply("Wrong code. Use /connect to restart.")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"Login failed: {e}")
            return
        if step == "password":
            try:
                await user_client.sign_in(password=text)
                connect_state.pop(ADMIN_ID, None)
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot connected (2FA) as {me.first_name}")
            except Exception as e:
                await event.reply(f"Wrong password: {e}")
            return

    # Settings inputs
    state = admin_state.get(ADMIN_ID)
    if state == "awaiting_warning_duration":
        try:
            sec = int(text)
            if sec < 10:
                await event.reply("Minimum 10 seconds.")
                return
            set_setting('warning_duration', str(sec))
            await event.reply(f"✅ Warning duration set to {sec} seconds.", buttons=[[Button.inline("🔙 Back to Settings", b"settings_back")]])
        except:
            await event.reply("Invalid number.")
        admin_state.pop(ADMIN_ID, None)
        return
    if state == "awaiting_temp_ban":
        try:
            h = int(text)
            if h < 0:
                await event.reply("Hours cannot be negative.")
                return
            set_setting('temp_ban_duration', str(h))
            await event.reply(f"✅ Temporary ban set to {h} hour(s).", buttons=[[Button.inline("🔙 Back to Settings", b"settings_back")]])
        except:
            await event.reply("Invalid number.")
        admin_state.pop(ADMIN_ID, None)
        return
    if state == "awaiting_exempt_channels":
        set_setting('forward_exempt_channels', text)
        await event.reply(f"✅ Exempt channels set to: {text}", buttons=[[Button.inline("🔙 Back to Settings", b"settings_back")]])
        admin_state.pop(ADMIN_ID, None)
        return

    # Filter add/remove
    if state == "awaiting_add":
        word = text.lower()
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            await event.reply(f"⚠️ `{word}` already exists.", buttons=make_filter_keyboard())
        else:
            banned_words.append(word)
            await event.reply(f"✅ Added `{word}`\nTotal: {len(banned_words)}", buttons=make_filter_keyboard())
        return
    if state == "awaiting_remove":
        word = text.lower()
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            banned_words.remove(word)
            await event.reply(f"✅ Removed `{word}`\nTotal: {len(banned_words)}", buttons=make_filter_keyboard())
        else:
            await event.reply(f"❌ `{word}` not found.", buttons=make_filter_keyboard())
        return

    # Silent filter add/remove
    sstate = silent_admin_state.get(ADMIN_ID)
    if sstate == "awaiting_silent_add":
        word = text.lower()
        silent_admin_state.pop(ADMIN_ID, None)
        add_silent_word(word)
        await event.reply(f"🔇 Added silent word: `{word}`\nTotal: {len(get_silent_words())}", buttons=make_silent_filter_keyboard())
        return
    if sstate == "awaiting_silent_remove":
        word = text.lower()
        silent_admin_state.pop(ADMIN_ID, None)
        remove_silent_word(word)
        await event.reply(f"🔇 Removed silent word: `{word}`\nTotal: {len(get_silent_words())}", buttons=make_silent_filter_keyboard())
        return

# ==================== GROUP MESSAGE HANDLER ====================
@bot_client.on(events.NewMessage(chats=GROUP_ID))
async def group_handler(event):
    if event.out:
        return
    sender = await event.get_sender()
    if sender is None:
        return

    # Exempt channel (your channel's own posts are never moderated)
    if EXEMPT_CHANNEL_ID and sender.id == EXEMPT_CHANNEL_ID:
        log.info(f"Exempt channel post ignored (sender {sender.id})")
        return

    me_bot = await bot_client.get_me()
    if sender.id == me_bot.id or sender.id == ADMIN_ID:
        return
    if userbot_connected:
        try:
            me_user = await user_client.get_me()
            if sender.id == me_user.id:
                return
        except:
            pass

    msg_text = event.raw_text or ""
    chat_id = event.chat_id

    # Forward deletion (if enabled, and not from exempt channel)
    if get_setting('delete_all_forwards') == 'on' and event.message.forward:
        # Also check forward_exempt_channels setting
        exempt_str = get_setting('forward_exempt_channels') or ""
        exempt_ids = [int(x.strip()) for x in exempt_str.split(",") if x.strip()]
        forward_from_id = None
        if event.message.forward.from_id:
            if hasattr(event.message.forward.from_id, 'channel_id'):
                forward_from_id = event.message.forward.from_id.channel_id
            else:
                forward_from_id = event.message.forward.from_id.user_id
        if forward_from_id and forward_from_id in exempt_ids:
            log.info(f"Forward from exempt channel {forward_from_id} ignored")
        else:
            await delete_msg(chat_id, event.id)
            log.info(f"Deleted forward from {sender.id}")
            return

    if not msg_text:
        return

    uid = sender.id
    uname = getattr(sender, "username", "") or ""
    fullname = " ".join(filter(None, [getattr(sender, "first_name",""), getattr(sender, "last_name","")])) or uname or str(uid)
    is_bot = getattr(sender, "bot", False)

    log.info(f"📨 [{fullname}]: {msg_text[:80]}")

    # Silent word
    sw = message_contains_silent_word(msg_text)
    if sw:
        await delete_msg(chat_id, event.id)
        if not is_bot:
            strikes = increment_silent_violation(uid)
            log.info(f"Silent '{sw}' from {fullname} strike {strikes}/3")
            if strikes >= 3:
                hours = int(get_setting('temp_ban_duration') or 0)
                await ban_user(chat_id, uid, hours)
                await notify_admin(uid, uname, fullname, msg_text, f"3 silent strikes: '{sw}'",
                                   f"BANNED ({hours}h)" if hours else "PERMANENT BAN")
                reset_silent_violation(uid)
        else:
            log.info(f"Bot silent delete: {fullname}")
        return

    # Spam
    spam_flag, spam_reason = is_spam(msg_text)
    if spam_flag:
        await _handle_violation(event, uid, uname, fullname, chat_id, msg_text, f"Spam: {spam_reason}", is_bot)
        return

    # Keyword
    kw = keyword_is_banned(msg_text)
    if kw:
        await _handle_violation(event, uid, uname, fullname, chat_id, msg_text, f"Banned word: '{kw}'", is_bot)
        return

    # Gemini
    res = await queue_gemini_analysis(msg_text)
    if res["verdict"] == "PROHIBITED":
        await _handle_violation(event, uid, uname, fullname, chat_id, msg_text, res["reason"], is_bot)
        return

    log.info(f"✅ Allowed: {fullname}")

# ==================== USERBOT TARGET DELETER ====================
@user_client.on(events.NewMessage(chats=GROUP_ID))
async def userbot_target_deleter(event):
    if not userbot_connected:
        return
    sender = await event.get_sender()
    if sender is None:
        return
    if EXEMPT_CHANNEL_ID and sender.id == EXEMPT_CHANNEL_ID:
        return
    try:
        me_user = await user_client.get_me()
        if sender.id in (me_user.id, ADMIN_ID):
            return
    except:
        return
    uname = (getattr(sender, "username", "") or "").lower()
    target = False
    if TARGET_BOT_USERNAME and uname == TARGET_BOT_USERNAME.lower():
        target = True
    if TARGET_BOT_ID and sender.id == TARGET_BOT_ID:
        target = True
    if target:
        try:
            await user_client.delete_messages(event.chat_id, event.id)
            log.info(f"🎯 Deleted target bot msg from {uname or sender.id}")
        except Exception as e:
            log.warning(f"Target bot delete failed: {e}")

# ==================== MAIN ====================
async def main():
    global userbot_connected
    init_db()
    await bot_client.start(bot_token=BOT_TOKEN)
    log.info(f"Bot started: {(await bot_client.get_me()).username}")

    try:
        os.makedirs(os.path.dirname(USER_SESSION_PATH) or ".", exist_ok=True)
        await user_client.connect()
        if await user_client.is_user_authorized():
            userbot_connected = True
            me = await user_client.get_me()
            log.info(f"Userbot loaded: {me.first_name}")
        else:
            log.info("Userbot not logged in. Use /connect")
    except Exception as e:
        log.warning(f"Userbot init failed: {e}")

    try:
        ent = await bot_client.get_entity(GROUP_ID)
        log.info(f"Monitoring: {ent.title}")
    except Exception as e:
        log.error(f"Cannot access group: {e}")

    asyncio.create_task(gemini_queue_worker())
    log.info(f"Gemini: {len(GEMINI_KEYS)} keys | Models: {len(GEMINI_MODELS)} | Gap {GEMINI_CALL_GAP}s")
    log.info(f"Exempt channel: {EXEMPT_CHANNEL_ID}")
    if userbot_connected:
        await asyncio.gather(bot_client.run_until_disconnected(), user_client.run_until_disconnected())
    else:
        await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
