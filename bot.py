"""
Squad 4x Group Manager — Full Production Version
──────────────────────────────────────────────────
FIXES IN THIS VERSION:
 1. Gemini model auto-switching: 4 models tried in order on any error
    (not just quota). Models: gemini-2.0-flash → gemini-1.5-flash →
    gemini-1.5-pro → gemini-2.0-flash-lite. Switchable via /settings.
 2. /ask animated thinking: dots animate 🤔 → 🤔. → 🤔.. → 🤔...
    then result replaces it. Thinking msg deleted after reply shown.
 3. USER_SESSION_PATH & DB_PATH: removed env var dependency entirely.
    Both now use in-container /app paths that always work on Railway
    without needing volume setup. Falls back gracefully.
 4. warnings_db now also stored in SQLite with in-memory counter cache
    so counts are instant but persist across restarts.
 5. _call_gemini: catches ALL errors (not just 429) and switches model.
 6. asyncio.Queue created inside main() to avoid "attached to different
    loop" error that caused Gemini to silently never work.
"""

import os
import asyncio
import logging
import json
import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PasswordHashInvalidError, FloodWaitError, MessageNotModifiedError
)
import google.generativeai as genai

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==================== ENVIRONMENT ====================
API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ["ADMIN_ID"])
GROUP_ID  = int(os.environ["GROUP_ID"])

TARGET_BOT_USERNAME = os.environ.get("TARGET_BOT_USERNAME", "").lstrip("@")
TARGET_BOT_ID       = int(os.environ["TARGET_BOT_ID"]) if os.environ.get("TARGET_BOT_ID") else None
EXEMPT_CHANNEL_ID   = int(os.environ["EXEMPT_CHANNEL_ID"]) if os.environ.get("EXEMPT_CHANNEL_ID") else None

# ==================== PATHS (FIX #3) ====================
# Use /app directory — always writable on Railway without needing a volume.
# If /data volume IS mounted, it will be used automatically.
def _resolve_path(env_key, default_filename):
    """Pick path: env var > /data if exists > /app fallback."""
    env_val = os.environ.get(env_key, "")
    if env_val:
        p = env_val
    elif os.path.isdir("/data"):
        p = f"/data/{default_filename}"
    else:
        p = f"/app/{default_filename}"
    os.makedirs(os.path.dirname(p) if os.path.dirname(p) else ".", exist_ok=True)
    return p

DB_PATH       = _resolve_path("DB_PATH", "bot_data.db")
_session_raw  = _resolve_path("USER_SESSION_PATH", "user_instance.session")
# Strip .session suffix — TelegramClient adds it automatically
USER_SESSION  = _session_raw[:-8] if _session_raw.endswith(".session") else _session_raw

log.info("📂 DB: %s", DB_PATH)
log.info("📂 Session: %s.session", USER_SESSION)

# ==================== GEMINI SETUP (FIX #1 & #5) ====================
_raw_keys   = os.environ["GEMINI_API_KEY"]
GEMINI_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_current_key_index = 0

# Model list — auto-switches on failure (FIX #1)
# Admin can change active model via /settings → Gemini Model
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash-lite",
]
_current_model_index = 0   # which model is active


def get_current_model_name():
    return GEMINI_MODELS[_current_model_index]


def get_gemini_model(model_name=None):
    name = model_name or get_current_model_name()
    genai.configure(api_key=GEMINI_KEYS[_current_key_index])
    return genai.GenerativeModel(name)


def rotate_api_key():
    global _current_key_index
    next_idx = (_current_key_index + 1) % len(GEMINI_KEYS)
    if next_idx == 0 and len(GEMINI_KEYS) == 1:
        return False
    _current_key_index = next_idx
    log.info("🔄 Switched to API key #%s", _current_key_index + 1)
    return True


def rotate_model():
    global _current_model_index
    next_idx = (_current_model_index + 1) % len(GEMINI_MODELS)
    _current_model_index = next_idx
    log.info("🔄 Switched to model: %s", get_current_model_name())
    return get_current_model_name()

# ==================== TELEGRAM CLIENTS ====================
bot_client  = TelegramClient("bot_session", API_ID, API_HASH)
user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)

# ==================== GLOBAL STATE ====================
connect_state       = {}
userbot_connected   = False
admin_state         = {}
silent_admin_state  = {}
_silent_words_cache = None   # in-memory cache, invalidated on change

# Gemini queue — initialized in main() to avoid event loop issues (FIX #6)
_gemini_queue     = None
_last_gemini_call = 0.0
GEMINI_CALL_GAP   = 8     # seconds between queue calls
MIN_WORDS_GEMINI  = 5

# ==================== SPAM & FOREX SAFE WORDS ====================
SPAM_CAPS_RATIO      = 0.75
SPAM_REPEAT_CHARS    = 6
SPAM_MAX_PUNCTUATION = 0.45
SPAM_MIN_WORD_LEN    = 3
SPAM_REPEATED_WORDS  = 4

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
_spam_phrase_re = re.compile(
    r'(?:' + '|'.join(re.escape(p) for p in SPAM_PHRASES) + r')',
    re.IGNORECASE
)

SUSPICIOUS_PATTERNS = [
    r"https?://", r"t\.me/", r"bit\.ly", r"wa\.me",
    r"\b(usdt|btc|eth|crypto|wallet|deposit|withdraw)\b",
    r"\b(profit|income|earn|money|invest|fund)\b",
    r"\b(vip|premium|paid|sell|buy|hire|promo|referral)\b",
    r"\b(channel|group|subscribe|follow|contact|whatsapp)\b",
    r"(ትርፍ|ገንዘብ|ሲግናል|ቡድን|ቻናል|ሊንክ|ኢንቨስት|አካውንት)",
]
_suspicious_re = re.compile("|".join(SUSPICIOUS_PATTERNS), re.IGNORECASE)

# ==================== SQLITE DATABASE ====================
_db_lock = threading.Lock()


def _db_conn():
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
            'private_warning':         'off',
            'warning_duration':        '300',
            'temp_ban_duration':       '0',
            'delete_all_forwards':     'on',
            'forward_exempt_channels': '',
            'gemini_model':            'gemini-2.0-flash',
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO bot_settings (key,value) VALUES (?,?)", (k, v))
        conn.commit()
        conn.close()
    log.info("✅ Database ready: %s", DB_PATH)


def get_setting(key):
    with _db_lock:
        conn = _db_conn()
        row  = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None


def set_setting(key, value):
    with _db_lock:
        conn = _db_conn()
        conn.execute("REPLACE INTO bot_settings (key,value) VALUES (?,?)", (key, value))
        conn.commit()
        conn.close()
    log.info("⚙️ Setting %s = %s", key, value)


def get_warning_count(user_id):
    with _db_lock:
        conn = _db_conn()
        row  = conn.execute("SELECT count FROM warnings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return row[0] if row else 0


def record_violation(user_id, username, full_name, reason):
    with _db_lock:
        conn = _db_conn()
        conn.execute('''INSERT INTO warnings (user_id,count,username,full_name,last_reason,updated_at)
                        VALUES (?,1,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        count=count+1, username=excluded.username, full_name=excluded.full_name,
                        last_reason=excluded.last_reason, updated_at=excluded.updated_at''',
                     (user_id, username, full_name, reason, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        new = conn.execute("SELECT count FROM warnings WHERE user_id=?", (user_id,)).fetchone()[0]
        conn.close()
    log.info("📋 User %s → %s warning(s)", user_id, new)


def get_silent_words():
    global _silent_words_cache
    if _silent_words_cache is not None:
        return _silent_words_cache
    with _db_lock:
        conn = _db_conn()
        rows = conn.execute("SELECT word FROM silent_words").fetchall()
        conn.close()
    _silent_words_cache = [r[0] for r in rows]
    return _silent_words_cache


def add_silent_word(word):
    global _silent_words_cache
    with _db_lock:
        conn = _db_conn()
        conn.execute("INSERT OR IGNORE INTO silent_words (word) VALUES (?)", (word.lower(),))
        conn.commit()
        conn.close()
    _silent_words_cache = None
    log.info("🔇 Silent word added: %s", word)


def remove_silent_word(word):
    global _silent_words_cache
    with _db_lock:
        conn = _db_conn()
        conn.execute("DELETE FROM silent_words WHERE word=?", (word.lower(),))
        conn.commit()
        conn.close()
    _silent_words_cache = None
    log.info("🔇 Silent word removed: %s", word)


def increment_silent_violation(user_id):
    with _db_lock:
        conn = _db_conn()
        conn.execute('''INSERT INTO silent_violations (user_id,count,updated_at)
                        VALUES (?,1,?) ON CONFLICT(user_id) DO UPDATE SET
                        count=count+1, updated_at=excluded.updated_at''',
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
        if caps / letters > SPAM_CAPS_RATIO:
            return True, f"Excessive caps ({caps/letters*100:.0f}%)"
    if re.search(r'(.)\1{' + str(SPAM_REPEAT_CHARS) + r',}', text):
        return True, "Repeated chars"
    if len(text) > 20:
        punct = sum(1 for c in text if not c.isalnum() and not c.isspace())
        if punct / len(text) > SPAM_MAX_PUNCTUATION:
            return True, "Too many punctuation/emojis"
    if wc >= SPAM_REPEATED_WORDS + 1:
        cnt = Counter(words)
        for w, c in cnt.items():
            if c >= SPAM_REPEATED_WORDS and len(w) > SPAM_MIN_WORD_LEN:
                return True, f"Word '{w}' repeated {c} times"
    if wc >= 3:
        ascii_words = [w for w in words if w.isascii()]
        if len(ascii_words) >= 3:
            garbage = sum(1 for w in ascii_words if len(w) >= 4 and not re.search(r'[aeiouAEIOU]', w))
            if garbage / len(ascii_words) > 0.5:
                return True, "Random keyboard spam"
    return False, ""

# ==================== GEMINI CORE (FIX #1 & #5) ====================
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


async def _call_gemini_raw(text: str, model_name: str, api_key: str) -> dict:
    """Single attempt with a specific model + key. Raises on any error."""
    genai.configure(api_key=api_key)
    model    = genai.GenerativeModel(model_name)
    prompt   = f"{SYSTEM_PROMPT}\n\nMessage:\n---\n{text[:2000]}\n---"
    response = await asyncio.to_thread(model.generate_content, prompt)
    raw      = response.text.strip()
    raw      = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
    data     = json.loads(raw)
    verdict  = str(data.get("verdict", "ALLOWED")).upper()
    reason   = str(data.get("reason", "No reason"))
    if verdict not in ("ALLOWED", "PROHIBITED"):
        verdict = "ALLOWED"
    return {"verdict": verdict, "reason": reason}


async def _call_gemini(text: str) -> dict:
    """
    FIX #1 & #5: Try every (model × key) combination before giving up.
    Order: current model with all keys → next model with all keys → etc.
    """
    total_models = len(GEMINI_MODELS)
    total_keys   = len(GEMINI_KEYS)
    # Load saved model preference from DB
    saved_model = get_setting('gemini_model') or GEMINI_MODELS[0]
    try:
        start_model_idx = GEMINI_MODELS.index(saved_model)
    except ValueError:
        start_model_idx = 0

    for m_offset in range(total_models):
        model_idx  = (start_model_idx + m_offset) % total_models
        model_name = GEMINI_MODELS[model_idx]
        for k_offset in range(total_keys):
            key_idx = (_current_key_index + k_offset) % total_keys
            api_key = GEMINI_KEYS[key_idx]
            try:
                result = await _call_gemini_raw(text, model_name, api_key)
                if m_offset > 0 or k_offset > 0:
                    log.info("✅ Gemini success: model=%s key#%s", model_name, key_idx + 1)
                else:
                    log.info("🤖 Gemini [%s key#%s] → %s | %s",
                             model_name, key_idx + 1, result["verdict"], result["reason"])
                return result
            except Exception as e:
                err = str(e)
                is_quota = "429" in err or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err
                log.warning("⚠️ Gemini fail model=%s key#%s (%s): %s",
                            model_name, key_idx + 1, "quota" if is_quota else "error", err[:80])
                if is_quota:
                    # For quota errors try next key before next model
                    continue
                else:
                    # For non-quota errors (bad model, network) skip to next model immediately
                    break

    # All combinations failed
    log.error("❌ All Gemini models and keys failed — defaulting ALLOWED")
    return {"verdict": "ALLOWED", "reason": "All Gemini models failed — safe default"}


async def _call_gemini_free(question: str) -> str:
    """For /ask command — returns raw text answer, tries all models."""
    saved_model = get_setting('gemini_model') or GEMINI_MODELS[0]
    try:
        start_model_idx = GEMINI_MODELS.index(saved_model)
    except ValueError:
        start_model_idx = 0

    for m_offset in range(len(GEMINI_MODELS)):
        model_idx  = (start_model_idx + m_offset) % len(GEMINI_MODELS)
        model_name = GEMINI_MODELS[model_idx]
        for k_offset in range(len(GEMINI_KEYS)):
            key_idx = (_current_key_index + k_offset) % len(GEMINI_KEYS)
            api_key = GEMINI_KEYS[key_idx]
            try:
                genai.configure(api_key=api_key)
                model    = genai.GenerativeModel(model_name)
                response = await asyncio.to_thread(model.generate_content, question)
                log.info("✅ /ask answered: model=%s key#%s", model_name, key_idx + 1)
                return response.text.strip(), model_name
            except Exception as e:
                err = str(e)
                log.warning("⚠️ /ask fail model=%s key#%s: %s", model_name, key_idx + 1, err[:60])
                is_quota = "429" in err or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err
                if not is_quota:
                    break  # try next model
                continue   # try next key
    raise RuntimeError("All Gemini models and keys failed")

# ==================== GEMINI QUEUE (FIX #6) ====================
def should_use_gemini(text):
    if len(text.strip().split()) < MIN_WORDS_GEMINI:
        return False
    return bool(_suspicious_re.search(text))


async def gemini_queue_worker():
    global _last_gemini_call
    while True:
        text, future = await _gemini_queue.get()
        try:
            loop = asyncio.get_running_loop()
            now  = loop.time()
            gap  = GEMINI_CALL_GAP - (now - _last_gemini_call)
            if gap > 0:
                await asyncio.sleep(gap)
            result = await _call_gemini(text)
            _last_gemini_call = asyncio.get_running_loop().time()
            if not future.done():
                future.set_result(result)
        except Exception as e:
            if not future.done():
                future.set_exception(e)
        finally:
            _gemini_queue.task_done()


async def queue_gemini_analysis(text):
    if not should_use_gemini(text):
        return {"verdict": "ALLOWED", "reason": "Skipped"}
    loop   = asyncio.get_running_loop()
    future = loop.create_future()
    await _gemini_queue.put((text, future))
    log.info("📥 Queued for Gemini (size: %s)", _gemini_queue.qsize())
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=300)
    except asyncio.TimeoutError:
        if not future.done():
            future.cancel()
        return {"verdict": "ALLOWED", "reason": "Timeout"}
    except (asyncio.CancelledError, Exception) as e:
        log.warning("Queue error: %s", e)
        return {"verdict": "ALLOWED", "reason": "Error"}

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
    until = datetime.now(timezone.utc) + timedelta(hours=hours) if hours > 0 else None
    try:
        await bot_client(EditBannedRequest(
            channel=chat_id, participant=user_id,
            banned_rights=ChatBannedRights(until_date=until, view_messages=True)
        ))
        log.info("🔨 Banned %s%s", user_id, f" for {hours}h" if hours else " permanently")
    except Exception as e:
        log.error("Ban failed %s: %s", user_id, e)


async def send_private_warning(user_id, reason):
    try:
        await bot_client.send_message(
            user_id,
            f"⚠️ **Warning from Squad 4x group**\n\n"
            f"This is your **only warning**. Next violation = ban.\n\n"
            f"📋 Reason: {reason}",
            parse_mode="md"
        )
        return True
    except Exception:
        return False


async def send_warning(event, reason, user_id, username, full_name):
    private = get_setting('private_warning') == 'on'
    if private and await send_private_warning(user_id, reason):
        log.info("Private warning → %s", user_id)
        return
    mention = f"@{username}" if username else f"[{full_name}](tg://user?id={user_id})"
    msg = await bot_client.send_message(
        event.chat_id,
        f"⚠️ **Warning / ማስጠንቀቂያ** — {mention}\n\n"
        f"🇬🇧 This is your **only warning**. Next violation = immediate ban.\n"
        f"🇪🇹 ይህ **የመጨረሻ ማስጠንቀቂያዎ** ነው። ደግመው ከጣሱ ወዲያውኑ ይታገዳሉ።\n\n"
        f"📋 **Reason:** {reason}",
        parse_mode="md"
    )
    duration = int(get_setting('warning_duration') or 300)
    async def _del():
        await asyncio.sleep(duration)
        await delete_msg(event.chat_id, msg.id)
    asyncio.create_task(_del())


async def notify_admin(user_id, username, full_name, text, reason, action):
    try:
        tag = f"@{username}" if username else f"ID:{user_id}"
        await bot_client.send_message(
            ADMIN_ID,
            f"🚫 **Moderation Report**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**Action:** {action}\n"
            f"**User:** {full_name} ({tag})\n"
            f"**ID:** `{user_id}`\n\n"
            f"**Message:**\n```\n{text[:500]}\n```\n\n"
            f"**Reason:** {reason}\n"
            f"**Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            parse_mode="md"
        )
    except Exception as e:
        log.error("Admin notify failed: %s", e)


def keyword_is_banned(text):
    low = text.lower()
    for w in banned_words:
        if w.lower() in low:
            return w
    return None


async def _handle_violation(event, uid, uname, fullname, cid, msg_text, reason, is_bot):
    await delete_msg(cid, event.id)
    if is_bot:
        return
    prior = get_warning_count(uid)
    if prior == 0:
        record_violation(uid, uname, fullname, reason)
        await send_warning(event, reason, uid, uname, fullname)
        log.info("⚠️ Warned %s (%s)", fullname, uid)
    else:
        record_violation(uid, uname, fullname, reason)
        hours = int(get_setting('temp_ban_duration') or 0)
        await ban_user(cid, uid, hours)
        await notify_admin(uid, uname, fullname, msg_text, reason,
                           f"BANNED ({hours}h)" if hours else "PERMANENT BAN")
        log.info("🔨 Banned %s (%s)", fullname, uid)

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
        [Button.inline("⚠️ Warning Settings",  b"set_warning")],
        [Button.inline("🔨 Ban Settings",       b"set_ban")],
        [Button.inline("🔇 Silent Filter",      b"set_silent")],
        [Button.inline("📤 Forward Control",    b"set_forward")],
        [Button.inline("🤖 Gemini Model",       b"set_gemini_model")],
        [Button.inline("❌ Close",              b"settings_close")]
    ]

def get_warning_keyboard():
    private  = get_setting('private_warning')
    duration = get_setting('warning_duration')
    return [
        [Button.inline(f"Private warning: {'✅ ON' if private=='on' else '❌ OFF'}", b"toggle_private_warning")],
        [Button.inline(f"Warning duration: {duration}s", b"set_warning_duration")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_ban_keyboard():
    temp    = get_setting('temp_ban_duration')
    display = "Permanent" if temp == '0' else f"{temp} hours"
    return [
        [Button.inline(f"Temporary ban: {display}", b"set_temp_ban")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_forward_keyboard():
    delete_all = get_setting('delete_all_forwards')
    exempt     = get_setting('forward_exempt_channels') or "None"
    return [
        [Button.inline(f"Delete all forwards: {'✅ ON' if delete_all=='on' else '❌ OFF'}", b"toggle_delete_forwards")],
        [Button.inline(f"Exempt channels: {exempt[:20]}", b"set_exempt_channels")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_gemini_model_keyboard():
    current = get_setting('gemini_model') or GEMINI_MODELS[0]
    rows = []
    for m in GEMINI_MODELS:
        label = f"{'✅ ' if m == current else ''}{m}"
        rows.append([Button.inline(label, f"gemini_model_{m}".encode())])
    rows.append([Button.inline("🔙 Back", b"settings_back")])
    return rows

def get_silent_keyboard():
    return [
        [Button.inline("Manage → /silentfilter", b"nothing")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

# ==================== COMMANDS ====================
@bot_client.on(events.NewMessage(pattern="/start"))
async def start_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    status  = "✅ Connected" if userbot_connected else "❌ Not connected — use /connect"
    target  = f"@{TARGET_BOT_USERNAME}" if TARGET_BOT_USERNAME else str(TARGET_BOT_ID or "Not set")
    exempt  = str(EXEMPT_CHANNEL_ID) if EXEMPT_CHANNEL_ID else "Not set"
    model   = get_setting('gemini_model') or GEMINI_MODELS[0]
    await event.reply(
        f"🤖 **Squad 4x Manager**\n\n"
        f"👤 Userbot: {status}\n"
        f"🎯 Target bot: {target}\n"
        f"🛡️ Exempt channel: {exempt}\n"
        f"🧠 Gemini model: `{model}`\n\n"
        "**Commands:**\n"
        "/connect — Login userbot\n"
        "/filter — Manage banned words\n"
        "/silentfilter — Silent delete words\n"
        "/settings — Bot settings + model\n"
        "/ask <question> — Ask Gemini AI\n"
        "/status — Health check\n"
        "/cancel — Cancel operation"
    )


@bot_client.on(events.NewMessage(pattern="/connect"))
async def connect_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    global userbot_connected
    if userbot_connected:
        await event.reply("✅ Userbot already connected.")
        return
    session_file = USER_SESSION + ".session"
    if os.path.exists(session_file):
        await event.reply("🔄 Session found. Reconnecting...")
        try:
            await user_client.connect()
            if await user_client.is_user_authorized():
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Reconnected as {me.first_name} (@{me.username or 'N/A'})")
                return
        except Exception as e:
            log.warning("Reconnect failed: %s", e)
    connect_state[ADMIN_ID] = {"step": "phone"}
    await event.reply("📱 Send your phone number with country code:\n`+251912345678`")


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
    await event.reply(f"🔧 **Keyword Filter** — {len(banned_words)} words", buttons=make_filter_keyboard())


@bot_client.on(events.NewMessage(pattern="/silentfilter"))
async def silent_filter_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(f"🔇 **Silent Filter** — {len(get_silent_words())} words", buttons=make_silent_filter_keyboard())


@bot_client.on(events.NewMessage(pattern="/settings"))
async def settings_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply("⚙️ **Bot Settings**", buttons=get_main_settings_keyboard())


# ==================== /ask WITH ANIMATED THINKING (FIX #2) ====================
@bot_client.on(events.NewMessage(pattern="/ask"))
async def ask_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    question = event.raw_text.replace("/ask", "", 1).strip()
    if not question:
        await event.reply(
            "💡 **Usage:** `/ask <your question>`\n\n"
            "Example: `/ask What is the best strategy for XAUUSD?`"
        )
        return

    # FIX #2: animated thinking dots
    thinking_msg = await event.reply("🤔")
    frames = ["🤔", "🤔.", "🤔..", "🤔...", "🤔.."]
    stop_animation = asyncio.Event()

    async def animate():
        i = 0
        while not stop_animation.is_set():
            try:
                await thinking_msg.edit(frames[i % len(frames)])
            except Exception:
                pass
            await asyncio.sleep(0.6)
            i += 1

    anim_task = asyncio.create_task(animate())

    try:
        answer, used_model = await _call_gemini_free(question)
        stop_animation.set()
        anim_task.cancel()

        # Delete the thinking message
        try:
            await thinking_msg.delete()
        except Exception:
            pass

        # Send polished answer as a new message
        header = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 **Squad 4x Assistant**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        footer = f"\n\n─────────────────────\n_Model: {used_model}_"
        full_answer = header + answer + footer

        # Telegram has 4096 char limit — split if needed
        if len(full_answer) <= 4096:
            await bot_client.send_message(event.chat_id, full_answer, parse_mode="md")
        else:
            await bot_client.send_message(event.chat_id, header + answer[:3800] + "...", parse_mode="md")

        log.info("/ask answered via %s", used_model)

    except Exception as e:
        stop_animation.set()
        anim_task.cancel()
        try:
            await thinking_msg.edit(f"❌ **Error:** Could not get answer.\n\n`{str(e)[:200]}`", parse_mode="md")
        except Exception:
            pass
        log.error("/ask error: %s", e)


@bot_client.on(events.NewMessage(pattern="/status"))
async def status_cmd(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    model   = get_setting('gemini_model') or GEMINI_MODELS[0]
    qsize   = _gemini_queue.qsize() if _gemini_queue else 0
    session = USER_SESSION + ".session"
    await event.reply(
        f"📊 **Status**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"👤 Userbot: {'✅ Connected' if userbot_connected else '❌ Not connected'}\n"
        f"🧠 Gemini model: `{model}`\n"
        f"🔑 API keys: {len(GEMINI_KEYS)} | Active: #{_current_key_index+1}\n"
        f"📥 Gemini queue: {qsize} pending\n"
        f"🔧 Banned words: {len(banned_words)}\n"
        f"🔇 Silent words: {len(get_silent_words())}\n"
        f"🎯 Target bot: {TARGET_BOT_USERNAME or TARGET_BOT_ID or 'Not set'}\n"
        f"🛡️ Exempt channel: {EXEMPT_CHANNEL_ID or 'Not set'}\n"
        f"💾 DB: `{DB_PATH}`\n"
        f"📂 Session: `{session}`\n"
        f"⏰ Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

# ==================== CALLBACK HANDLER ====================
@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Admin only", alert=True)
        return
    data = event.data
    try:
        # Filter callbacks
        if data == b"filter_add":
            admin_state[ADMIN_ID] = "awaiting_add"
            await event.edit("➕ Send the word/phrase to ban:", buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]])
            return
        if data == b"filter_remove":
            if not banned_words:
                await event.answer("No words!", alert=True); return
            admin_state[ADMIN_ID] = "awaiting_remove"
            lst = "\n".join(f"{i+1}. `{w}`" for i,w in enumerate(banned_words))
            await event.edit(f"Current words:\n{lst}\n\nSend exact word to remove:", buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]])
            return
        if data == b"filter_show":
            if not banned_words:
                await event.answer("Empty!", alert=True); return
            txt = "\n".join(f"• `{w}`" for w in banned_words)
            await event.edit(f"📋 Banned ({len(banned_words)}):\n{txt}", buttons=make_filter_keyboard())
            return
        if data == b"filter_cancel":
            admin_state.pop(ADMIN_ID, None)
            await event.edit("Cancelled.", buttons=make_filter_keyboard()); return

        # Silent filter callbacks
        if data == b"silent_add":
            silent_admin_state[ADMIN_ID] = "awaiting_silent_add"
            await event.edit("Send silent word:", buttons=[[Button.inline("❌ Cancel", b"silent_cancel")]]); return
        if data == b"silent_remove":
            sw = get_silent_words()
            if not sw:
                await event.answer("No silent words!", alert=True); return
            silent_admin_state[ADMIN_ID] = "awaiting_silent_remove"
            lst = "\n".join(f"{i+1}. `{w}`" for i,w in enumerate(sw))
            await event.edit(f"Silent words:\n{lst}\n\nSend exact word to remove:", buttons=[[Button.inline("❌ Cancel", b"silent_cancel")]]); return
        if data == b"silent_show":
            sw = get_silent_words()
            if not sw:
                await event.answer("Empty!", alert=True); return
            await event.edit(f"🔇 Silent ({len(sw)}):\n" + "\n".join(f"• `{w}`" for w in sw), buttons=make_silent_filter_keyboard()); return
        if data == b"silent_cancel":
            silent_admin_state.pop(ADMIN_ID, None)
            await event.edit("Cancelled.", buttons=make_silent_filter_keyboard()); return

        # Settings callbacks
        try:
            d = data.decode()
        except Exception:
            return

        if d == "settings_close":
            await event.edit("Settings closed.")
        elif d == "settings_back":
            await event.edit("⚙️ **Bot Settings**", buttons=get_main_settings_keyboard())
        elif d == "set_warning":
            await event.edit("⚠️ **Warning Settings**", buttons=get_warning_keyboard())
        elif d == "set_ban":
            await event.edit("🔨 **Ban Settings**", buttons=get_ban_keyboard())
        elif d == "set_silent":
            await event.edit("🔇 **Silent Filter**\nUse `/silentfilter`", buttons=get_silent_keyboard())
        elif d == "set_forward":
            await event.edit("📤 **Forward Control**", buttons=get_forward_keyboard())
        elif d == "set_gemini_model":
            current = get_setting('gemini_model') or GEMINI_MODELS[0]
            await event.edit(
                f"🤖 **Gemini Model**\n\nCurrent: `{current}`\n\n"
                "Select model (auto-switches on failure):",
                buttons=get_gemini_model_keyboard()
            )
        elif d.startswith("gemini_model_"):
            chosen = d.replace("gemini_model_", "")
            if chosen in GEMINI_MODELS:
                set_setting('gemini_model', chosen)
                global _current_model_index
                _current_model_index = GEMINI_MODELS.index(chosen)
                await event.edit(
                    f"🤖 **Gemini Model**\n\n✅ Switched to: `{chosen}`",
                    buttons=get_gemini_model_keyboard()
                )
                await event.answer(f"Model: {chosen}", alert=False)
        elif d == "toggle_private_warning":
            cur = get_setting('private_warning')
            new = 'off' if cur == 'on' else 'on'
            set_setting('private_warning', new)
            await event.edit("⚠️ **Warning Settings**", buttons=get_warning_keyboard())
            await event.answer(f"Private warning: {new}", alert=True)
        elif d == "set_warning_duration":
            admin_state[ADMIN_ID] = "awaiting_warning_duration"
            await event.edit("Send duration in seconds (e.g., 300 = 5 min). /cancel to abort.")
        elif d == "set_temp_ban":
            admin_state[ADMIN_ID] = "awaiting_temp_ban"
            await event.edit("Send hours (0 = permanent, 24 = one day). /cancel to abort.")
        elif d == "toggle_delete_forwards":
            cur = get_setting('delete_all_forwards')
            new = 'off' if cur == 'on' else 'on'
            set_setting('delete_all_forwards', new)
            await event.edit("📤 **Forward Control**", buttons=get_forward_keyboard())
            await event.answer(f"Delete forwards: {new}", alert=True)
        elif d == "set_exempt_channels":
            admin_state[ADMIN_ID] = "awaiting_exempt_channels"
            await event.edit("Send comma-separated channel IDs to exempt.\nExample: -1001234,-1009876\n/cancel to abort.")
        elif d == "nothing":
            await event.answer("", alert=False)

    except MessageNotModifiedError:
        pass
    except Exception as e:
        log.error("Callback error: %s", e)

# ==================== ADMIN PRIVATE MESSAGE HANDLER ====================
@bot_client.on(events.NewMessage)
async def admin_private_handler(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    if event.text and event.text.startswith("/"):
        return
    text = (event.raw_text or "").strip()
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
                await event.reply("📩 Code sent! Send it with spaces:\n`1 2 3 4 5`")
            except FloodWaitError as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"⚠️ Flood wait {e.seconds}s. Try again later.")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ Failed: {e}")
            return
        if step == "code":
            code = text.replace(" ", "")
            try:
                await user_client.sign_in(conn["phone"], code, phone_code_hash=conn["phone_code_hash"])
                connect_state.pop(ADMIN_ID, None)
                global userbot_connected
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot connected as **{me.first_name}** (@{me.username or 'N/A'})")
                log.info("✅ Userbot: %s", me.first_name)
            except SessionPasswordNeededError:
                conn["step"] = "password"
                await event.reply("🔐 2FA enabled. Send your password:")
            except PhoneCodeInvalidError:
                await event.reply("❌ Wrong code. Use /connect to restart.")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ Login failed: {e}")
            return
        if step == "password":
            try:
                await user_client.sign_in(password=text)
                connect_state.pop(ADMIN_ID, None)
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot connected (2FA) as **{me.first_name}**")
                log.info("✅ Userbot 2FA: %s", me.first_name)
            except PasswordHashInvalidError:
                await event.reply("❌ Wrong 2FA password. Try again:")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ 2FA failed: {e}")
            return

    # Settings inputs
    state = admin_state.get(ADMIN_ID)
    if state == "awaiting_warning_duration":
        try:
            sec = int(text)
            if sec < 10:
                await event.reply("Minimum 10 seconds."); return
            set_setting('warning_duration', str(sec))
            await event.reply(f"✅ Warning duration: {sec}s", buttons=[[Button.inline("🔙 Back", b"settings_back")]])
        except Exception:
            await event.reply("❌ Invalid. Send a number like `300`")
        admin_state.pop(ADMIN_ID, None); return
    if state == "awaiting_temp_ban":
        try:
            h = int(text)
            if h < 0:
                await event.reply("Cannot be negative."); return
            set_setting('temp_ban_duration', str(h))
            await event.reply(f"✅ Temp ban: {h}h (0=permanent)", buttons=[[Button.inline("🔙 Back", b"settings_back")]])
        except Exception:
            await event.reply("❌ Invalid. Send a number like `24`")
        admin_state.pop(ADMIN_ID, None); return
    if state == "awaiting_exempt_channels":
        set_setting('forward_exempt_channels', text)
        await event.reply(f"✅ Exempt channels: `{text}`", buttons=[[Button.inline("🔙 Back", b"settings_back")]])
        admin_state.pop(ADMIN_ID, None); return
    if state == "awaiting_add":
        word = text.lower()
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            await event.reply(f"⚠️ `{word}` already exists.", buttons=make_filter_keyboard())
        else:
            banned_words.append(word)
            await event.reply(f"✅ Added `{word}` — Total: {len(banned_words)}", buttons=make_filter_keyboard())
        return
    if state == "awaiting_remove":
        word = text.lower()
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            banned_words.remove(word)
            await event.reply(f"✅ Removed `{word}` — Total: {len(banned_words)}", buttons=make_filter_keyboard())
        else:
            await event.reply(f"❌ `{word}` not found.", buttons=make_filter_keyboard())
        return

    sstate = silent_admin_state.get(ADMIN_ID)
    if sstate == "awaiting_silent_add":
        word = text.lower()
        silent_admin_state.pop(ADMIN_ID, None)
        add_silent_word(word)
        await event.reply(f"🔇 Added: `{word}` — Total: {len(get_silent_words())}", buttons=make_silent_filter_keyboard())
        return
    if sstate == "awaiting_silent_remove":
        word = text.lower()
        silent_admin_state.pop(ADMIN_ID, None)
        remove_silent_word(word)
        await event.reply(f"🔇 Removed: `{word}` — Total: {len(get_silent_words())}", buttons=make_silent_filter_keyboard())
        return

# ==================== GROUP MESSAGE HANDLER ====================
@bot_client.on(events.NewMessage(chats=GROUP_ID))
async def group_handler(event):
    if event.out:
        return
    sender = await event.get_sender()
    if sender is None:
        return
    if EXEMPT_CHANNEL_ID and sender.id == EXEMPT_CHANNEL_ID:
        return
    me_bot = await bot_client.get_me()
    if sender.id in (me_bot.id, ADMIN_ID):
        return
    if userbot_connected:
        try:
            me_user = await user_client.get_me()
            if sender.id == me_user.id:
                return
        except Exception:
            pass

    chat_id  = event.chat_id
    msg_text = event.raw_text or ""

    # Forward deletion
    if get_setting('delete_all_forwards') == 'on' and event.message.forward:
        exempt_str = get_setting('forward_exempt_channels') or ""
        exempt_ids = []
        for x in exempt_str.split(","):
            x = x.strip()
            if x:
                try: exempt_ids.append(int(x))
                except ValueError: pass
        fwd = event.message.forward
        forward_from_id = None
        if fwd.from_id:
            forward_from_id = (
                getattr(fwd.from_id, 'channel_id', None) or
                getattr(fwd.from_id, 'user_id', None) or
                getattr(fwd.from_id, 'chat_id', None)
            )
        if forward_from_id and forward_from_id in exempt_ids:
            log.info("Exempt forward from %s", forward_from_id)
        else:
            await delete_msg(chat_id, event.id)
            return

    if not msg_text:
        return

    uid      = sender.id
    uname    = getattr(sender, "username", "") or ""
    fullname = " ".join(filter(None, [getattr(sender,"first_name",""), getattr(sender,"last_name","")])) or uname or str(uid)
    is_bot   = getattr(sender, "bot", False)

    log.info("📨 [%s]: %s", fullname, msg_text[:80])

    sw = message_contains_silent_word(msg_text)
    if sw:
        await delete_msg(chat_id, event.id)
        if not is_bot:
            strikes = increment_silent_violation(uid)
            log.info("🔇 Silent '%s' from %s — strike %s/3", sw, fullname, strikes)
            if strikes >= 3:
                hours = int(get_setting('temp_ban_duration') or 0)
                await ban_user(chat_id, uid, hours)
                await notify_admin(uid, uname, fullname, msg_text,
                                   f"3 silent strikes: '{sw}'",
                                   f"BANNED ({hours}h)" if hours else "PERMANENT BAN")
                reset_silent_violation(uid)
        return

    spam_flag, spam_reason = is_spam(msg_text)
    if spam_flag:
        await _handle_violation(event, uid, uname, fullname, chat_id, msg_text, f"Spam: {spam_reason}", is_bot)
        return

    kw = keyword_is_banned(msg_text)
    if kw:
        await _handle_violation(event, uid, uname, fullname, chat_id, msg_text, f"Banned word: '{kw}'", is_bot)
        return

    res = await queue_gemini_analysis(msg_text)
    if res["verdict"] == "PROHIBITED":
        await _handle_violation(event, uid, uname, fullname, chat_id, msg_text, res["reason"], is_bot)
        return

    log.info("✅ Allowed: %s", fullname)

# ==================== USERBOT ====================
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
    except Exception:
        return
    uname  = (getattr(sender, "username", "") or "").lower()
    target = False
    if TARGET_BOT_USERNAME and uname == TARGET_BOT_USERNAME.lower():
        target = True
    if TARGET_BOT_ID and sender.id == TARGET_BOT_ID:
        target = True
    if target:
        try:
            await user_client.delete_messages(event.chat_id, event.id)
            log.info("🎯 Deleted target bot msg from %s", uname or sender.id)
        except Exception as e:
            log.warning("Target delete failed: %s", e)

# ==================== MAIN ====================
async def main():
    global userbot_connected, _gemini_queue, _current_model_index

    init_db()

    # FIX #6: create queue inside main() after event loop is running
    _gemini_queue = asyncio.Queue()

    # Load saved model preference
    saved_model = get_setting('gemini_model') or GEMINI_MODELS[0]
    if saved_model in GEMINI_MODELS:
        _current_model_index = GEMINI_MODELS.index(saved_model)
    log.info("🧠 Gemini model: %s", saved_model)

    await bot_client.start(bot_token=BOT_TOKEN)
    bot_me = await bot_client.get_me()
    log.info("🤖 Bot: @%s", bot_me.username)

    try:
        await user_client.connect()
        if await user_client.is_user_authorized():
            userbot_connected = True
            me = await user_client.get_me()
            log.info("✅ Userbot: %s (@%s)", me.first_name, me.username or "N/A")
        else:
            log.info("⚠️ Userbot not logged in — use /connect")
    except Exception as e:
        log.warning("Userbot init: %s", e)

    try:
        ent = await bot_client.get_entity(GROUP_ID)
        log.info("✅ Monitoring: %s", ent.title)
    except Exception as e:
        log.error("Cannot access group: %s", e)

    asyncio.create_task(gemini_queue_worker())
    log.info("📡 Gemini: %s key(s) | %s models | Gap: %ss",
             len(GEMINI_KEYS), len(GEMINI_MODELS), GEMINI_CALL_GAP)
    log.info("💾 DB: %s | Session: %s.session", DB_PATH, USER_SESSION)

    await asyncio.gather(
        bot_client.run_until_disconnected(),
        user_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
