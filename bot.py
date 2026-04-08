"""
Forex Group Management Bot — Dual Client (Bot + Userbot)
─────────────────────────────────────────────────────────
Version 13: Full featured with settings panel, /ask command,
forward delete with exempt channels, private warnings, temp bans.
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
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError,
    MessageNotModifiedError,
)
import google.generativeai as genai

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Environment Variables ────────────────────────────────────────────────────
API_ID     = int(os.environ["API_ID"])
API_HASH   = os.environ["API_HASH"]
BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])
GROUP_ID   = int(os.environ["GROUP_ID"])

TARGET_BOT_USERNAME = os.environ.get("TARGET_BOT_USERNAME", "").lstrip("@")
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"]) if os.environ.get("TARGET_BOT_ID") else None
EXEMPT_CHANNEL_ID = int(os.environ["EXEMPT_CHANNEL_ID"]) if os.environ.get("EXEMPT_CHANNEL_ID") else None

# ─── Spam Detection Settings ──────────────────────────────────────────────────
SPAM_CAPS_RATIO      = 0.75
SPAM_REPEAT_CHARS    = 6
SPAM_MAX_PUNCTUATION = 0.45
SPAM_MIN_WORD_LEN    = 3
SPAM_REPEATED_WORDS  = 4

FOREX_SAFE_WORDS = {
    "buy", "sell", "long", "short", "stop", "loss", "profit", "pips",
    "eurusd", "gbpusd", "xauusd", "usdjpy", "gbpjpy", "audusd",
    "entry", "exit", "target", "sl", "tp", "rr", "lot", "leverage",
    "bullish", "bearish", "breakout", "support", "resistance",
    "ema", "rsi", "macd", "fib", "fibonacci", "trend",
    "nfp", "cpi", "fomc", "fed", "news", "analysis",
}

SPAM_PHRASES = [
    "dm for signals", "dm me for signals", "signal dm", "vip group",
    "paid signals", "premium signals", "signal service", "buy signals",
    "sell signals", "signals channel", "signals group", "vip signals",
    "exclusive signals", "signals provider", "signal master",
    "join my group", "join our group", "join my channel", "join our channel",
    "subscribe to my channel", "link in bio", "check my bio",
    "referral link", "use my link", "invite link", "click the link",
    "follow me", "follow us",
    "guaranteed profit", "guaranteed returns", "100% profit", "risk free",
    "risk-free", "no loss", "double your money", "managed account",
    "send me money", "send usdt", "send btc", "invest with me",
    "investment platform", "fund your account", "withdraw daily",
    "earn daily", "earn money online", "make money online",
    "passive income", "financial freedom", "get rich", "millionaire",
    "account for sale", "selling account", "buying account", "ea for sale",
    "robot for sale", "trading bot for sale",
    "whatsapp me", "contact me on whatsapp", "dm me", "inbox me",
    "contact for promo", "available for hire", "hire me",
    "i offer services", "we offer services", "pm me",
    "ሲግናል ሸጭ", "ቪፒ ቡድን", "ነጻ ገንዘብ", "ፈጣን ሀብት", "ሂሳብ ሽያጭ",
]
_spam_phrase_re = re.compile(r'(?:' + '|'.join(re.escape(p) for p in SPAM_PHRASES) + r')', re.IGNORECASE)

# ─── Gemini Setup ─────────────────────────────────────────────────────────────
_raw_keys = os.environ["GEMINI_API_KEY"]
GEMINI_KEYS: list = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_current_key_index = 0
_gemini_quota_exhausted = False

def get_gemini_model() -> genai.GenerativeModel:
    genai.configure(api_key=GEMINI_KEYS[_current_key_index])
    return genai.GenerativeModel("gemini-2.0-flash")

def rotate_key() -> bool:
    global _current_key_index, _gemini_quota_exhausted
    next_index = (_current_key_index + 1) % len(GEMINI_KEYS)
    if next_index == 0 and len(GEMINI_KEYS) == 1:
        _gemini_quota_exhausted = True
        return False
    _current_key_index = next_index
    _gemini_quota_exhausted = False
    log.info("🔄 Rotated to Gemini key #%s", _current_key_index + 1)
    return True

# ─── Session Path Helper ──────────────────────────────────────────────────────
def prepare_session_path(raw_path: str) -> str:
    if not raw_path:
        raw_path = "user_instance.session"
    path = pathlib.Path(raw_path)
    if path.suffix == ".session":
        path = path.with_suffix("")
    parent = path.parent
    if parent and str(parent) != "." and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        log.info("📁 Created session directory: %s", parent)
    return str(path)

# ─── Telethon Clients ─────────────────────────────────────────────────────────
bot_client = TelegramClient("bot_session", API_ID, API_HASH)
USER_SESSION = prepare_session_path(os.environ.get("USER_SESSION_PATH", "/data/user_instance.session"))
user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)

# ─── Global State ─────────────────────────────────────────────────────────────
connect_state: dict = {}
userbot_connected: bool = False
admin_state: dict = {}
silent_admin_state: dict = {}

# ─── SQLite Database (Warnings, Silent Words, Settings) ──────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/bot_data.db")
_db_lock = threading.Lock()

def _db_conn():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with _db_lock:
        conn = _db_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS warnings (
            user_id INTEGER PRIMARY KEY,
            count INTEGER DEFAULT 0,
            username TEXT,
            full_name TEXT,
            last_reason TEXT,
            updated_at TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS silent_violations (
            user_id INTEGER PRIMARY KEY,
            count INTEGER DEFAULT 0,
            updated_at TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS silent_words (
            word TEXT PRIMARY KEY
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        default_settings = {
            'private_warning': 'off',
            'warning_duration': '300',
            'temp_ban_duration': '0',
            'delete_all_forwards': 'on',
            'forward_exempt_channels': '',
            'log_to_file': 'off',
        }
        for k, v in default_settings.items():
            conn.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
        conn.close()
    log.info("✅ Database initialized")

def get_setting(key: str) -> str:
    with _db_lock:
        conn = _db_conn()
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None

def set_setting(key: str, value: str):
    with _db_lock:
        conn = _db_conn()
        conn.execute("REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
    log.info(f"Setting {key} = {value}")

# ─── Warning / Ban Helpers ───────────────────────────────────────────────────
def get_warning_count(user_id: int) -> int:
    with _db_lock:
        conn = _db_conn()
        row = conn.execute("SELECT count FROM warnings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
    return row[0] if row else 0

def record_violation(user_id: int, username: str, full_name: str, reason: str):
    with _db_lock:
        conn = _db_conn()
        conn.execute('''
            INSERT INTO warnings (user_id, count, username, full_name, last_reason, updated_at)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                count = count + 1,
                username = excluded.username,
                full_name = excluded.full_name,
                last_reason = excluded.last_reason,
                updated_at = excluded.updated_at
        ''', (user_id, username, full_name, reason, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    log.info("📋 User %s → %s warning(s)", user_id, get_warning_count(user_id))

# ─── Silent Words & Violations ───────────────────────────────────────────────
def get_silent_words() -> list:
    with _db_lock:
        conn = _db_conn()
        rows = conn.execute("SELECT word FROM silent_words").fetchall()
        conn.close()
    return [row[0] for row in rows]

def add_silent_word(word: str):
    with _db_lock:
        conn = _db_conn()
        conn.execute("INSERT OR IGNORE INTO silent_words (word) VALUES (?)", (word.lower(),))
        conn.commit()
        conn.close()
    log.info("🔇 Silent word added: %s", word)

def remove_silent_word(word: str):
    with _db_lock:
        conn = _db_conn()
        conn.execute("DELETE FROM silent_words WHERE word = ?", (word.lower(),))
        conn.commit()
        conn.close()
    log.info("🔇 Silent word removed: %s", word)

def get_silent_violation_count(user_id: int) -> int:
    with _db_lock:
        conn = _db_conn()
        row = conn.execute("SELECT count FROM silent_violations WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
    return row[0] if row else 0

def increment_silent_violation(user_id: int) -> int:
    with _db_lock:
        conn = _db_conn()
        conn.execute('''
            INSERT INTO silent_violations (user_id, count, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                count = count + 1,
                updated_at = excluded.updated_at
        ''', (user_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        new_count = conn.execute("SELECT count FROM silent_violations WHERE user_id=?", (user_id,)).fetchone()[0]
        conn.close()
    return new_count

def reset_silent_violation(user_id: int):
    with _db_lock:
        conn = _db_conn()
        conn.execute("DELETE FROM silent_violations WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

def message_contains_silent_word(text: str) -> str:
    lower = text.lower()
    for word in get_silent_words():
        if word in lower:
            return word
    return None

# ─── Spam Detection ──────────────────────────────────────────────────────────
def is_spam(text: str) -> tuple:
    if not text:
        return (False, "")
    text_lower = text.lower()
    words = text.split()
    word_count = len(words)

    if _spam_phrase_re.search(text_lower):
        return (True, "Contains spam phrase")

    lower_words = {w.lower().strip(".,!?()[]") for w in words}
    if lower_words & FOREX_SAFE_WORDS:
        return (False, "")

    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    if ascii_letters > 15:
        caps = sum(1 for c in text if c.isascii() and c.isupper())
        if caps / ascii_letters > SPAM_CAPS_RATIO:
            return (True, f"Excessive caps ({caps/ascii_letters*100:.0f}%)")

    if re.search(r'(.)\1{' + str(SPAM_REPEAT_CHARS) + r',}', text):
        return (True, "Repeated characters")

    if len(text) > 20:
        punct = sum(1 for c in text if not c.isalnum() and not c.isspace())
        if punct / len(text) > SPAM_MAX_PUNCTUATION:
            return (True, "Too many punctuation/emojis")

    if word_count >= SPAM_REPEATED_WORDS + 1:
        word_counts = Counter(words)
        for w, cnt in word_counts.items():
            if cnt >= SPAM_REPEATED_WORDS and len(w) > SPAM_MIN_WORD_LEN:
                return (True, f"Word '{w}' repeated {cnt} times")

    if word_count >= 3:
        ascii_words = [w for w in words if w.isascii()]
        if len(ascii_words) >= 3:
            garbage = sum(1 for w in ascii_words if len(w) >= 4 and not re.search(r'[aeiouAEIOU]', w))
            if garbage / len(ascii_words) > 0.5:
                return (True, "Random keyboard spam")

    return (False, "")

# ─── Suspicious patterns for Gemini ──────────────────────────────────────────
SUSPICIOUS_PATTERNS = [
    r"https?://", r"t\.me/", r"bit\.ly", r"wa\.me",
    r"\b(usdt|btc|eth|crypto|wallet|deposit|withdraw)\b",
    r"\b(profit|income|earn|money|invest|fund)\b",
    r"\b(vip|premium|paid|sell|buy|hire|promo|referral)\b",
    r"\b(channel|group|subscribe|follow|contact|whatsapp)\b",
    r"(ትርፍ|ገንዘብ|ሲግናል|ቡድን|ቻናል|ሊንክ|ኢንቨስት|አካውንት)",
]
_suspicious_re = re.compile("|".join(SUSPICIOUS_PATTERNS), re.IGNORECASE)

# ─── Gemini Queue ────────────────────────────────────────────────────────────
GEMINI_CALL_GAP = 10
MIN_WORDS_GEMINI = 5
_gemini_queue: asyncio.Queue = asyncio.Queue()
_last_gemini_call: float = 0.0

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
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
        finally:
            _gemini_queue.task_done()

def should_use_gemini(text: str) -> bool:
    if len(text.strip().split()) < MIN_WORDS_GEMINI:
        return False
    return bool(_suspicious_re.search(text))

async def queue_gemini_analysis(text: str) -> dict:
    if not should_use_gemini(text):
        return {"verdict": "ALLOWED", "reason": "Skipped."}
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
        return {"verdict": "ALLOWED", "reason": "Timeout."}
    except asyncio.CancelledError:
        return {"verdict": "ALLOWED", "reason": "Cancelled."}
    except Exception as exc:
        log.warning("⚠️ Queue error: %s", exc)
        return {"verdict": "ALLOWED", "reason": "Error."}

SYSTEM_PROMPT = """You are the AI moderation engine for a professional Forex trading Telegram group.
Members write in BOTH English AND Amharic (አማርኛ). Analyse both equally.
✅ ALWAYS ALLOW:
- Forex/crypto: currency pairs, trade ideas, entries, exits, SL/TP
- Technical analysis: indicators, chart patterns, support/resistance
- Fundamental analysis: NFP, CPI, interest rates, central bank news
- Broker/platform: MT4, MT5, TradingView, cTrader
- Risk management, lot size, leverage, drawdown
- Market commentary, economic news, education, friendly chat
- P&L sharing, trade screenshots
❌ PROHIBITED (English or Amharic):
1. Paid signal ads or VIP group recruitment
2. Scams: guaranteed profit, wallet deposit requests, managed accounts
3. Recruiting to other channels/groups, referral links
4. Personal insults or hate speech
5. Completely off-topic spam/advertising
RULES: When in doubt → ALLOWED. Missing a scam > banning a real trader.
Respond ONLY with valid JSON, no markdown:
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "one sentence in English"}"""

async def _call_gemini(text: str) -> dict:
    global _gemini_quota_exhausted
    if _gemini_quota_exhausted:
        return {"verdict": "ALLOWED", "reason": "Assistant offline."}
    prompt = f"{SYSTEM_PROMPT}\n\nMessage:\n---\n{text[:2000]}\n---"
    keys_tried = 0
    total_keys = len(GEMINI_KEYS)
    while True:
        try:
            model = get_gemini_model()
            response = await asyncio.to_thread(model.generate_content, prompt)
            raw = response.text.strip()
            raw = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)
            verdict = str(data.get("verdict", "ALLOWED")).upper()
            reason = str(data.get("reason", "No reason."))
            if verdict not in ("ALLOWED", "PROHIBITED"):
                verdict = "ALLOWED"
            log.info("🤖 Gemini [key#%s] → %s | %s", _current_key_index + 1, verdict, reason)
            return {"verdict": verdict, "reason": reason}
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "quota" in err_str.lower() or "RESOURCE_EXHAUSTED" in err_str:
                keys_tried += 1
                if not rotate_key():
                    _gemini_quota_exhausted = True
                    log.warning("⛔ All Gemini keys exhausted.")
                    return {"verdict": "ALLOWED", "reason": "Assistant offline."}
                continue
            log.warning("⚠️ Gemini error: %s", exc)
            return {"verdict": "ALLOWED", "reason": "Gemini error."}

# ─── Rest of the code continues in next message...
# ─── Moderation Helpers ───────────────────────────────────────────────────────
async def delete_msg(chat_id: int, message_id: int):
    if userbot_connected:
        try:
            await user_client.delete_messages(chat_id, message_id)
            return
        except Exception:
            pass
    try:
        await bot_client.delete_messages(chat_id, message_id)
    except Exception:
        pass

async def ban_user(chat_id: int, user_id: int, duration_hours: int = 0):
    until_date = None
    if duration_hours > 0:
        until_date = datetime.now(timezone.utc).timestamp() + (duration_hours * 3600)
    try:
        await bot_client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(until_date=until_date, view_messages=True)
        ))
        if duration_hours > 0:
            log.info(f"🔨 Temporarily banned user {user_id} for {duration_hours} hours")
        else:
            log.info("🔨 Permanently banned user %s", user_id)
    except Exception as exc:
        log.error("Ban failed for %s: %s", user_id, exc)

async def send_private_warning(user_id: int, reason: str) -> bool:
    try:
        await bot_client.send_message(
            user_id,
            f"⚠️ **Warning from Squad 4x group**\n\n"
            f"You have violated the group rules: {reason}\n\n"
            f"This is your **only warning**. Next violation will result in a ban.\n\n"
            f"📋 **Reason:** {reason}",
            parse_mode="md"
        )
        return True
    except Exception:
        return False

async def send_warning(event, reason: str, user_id: int, username: str, full_name: str):
    private_mode = get_setting('private_warning') == 'on'
    success = False
    if private_mode:
        success = await send_private_warning(user_id, reason)
    if not private_mode or not success:
        mention = f"@{username}" if username else f"[{full_name}](tg://user?id={user_id})"
        warning_msg = await bot_client.send_message(
            event.chat_id,
            f"⚠️ **Warning / ማስጠንቀቂያ** — {mention}\n\n"
            f"🇬🇧 This is your **only warning**. Next violation = ban.\n"
            f"🇪🇹 ይህ **የመጨረሻ ማስጠንቀቂያዎ** ነው። ደግመው ከጣሱ ይታገዳሉ።\n\n"
            f"📋 **Reason:** {reason}",
            parse_mode="md"
        )
        duration = int(get_setting('warning_duration') or 300)
        async def _delete_later():
            await asyncio.sleep(duration)
            await delete_msg(event.chat_id, warning_msg.id)
        asyncio.create_task(_delete_later())
    log.info("⚠️ Warning sent to %s (private=%s)", user_id, private_mode and success)

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
    except Exception as exc:
        log.error("Admin notify failed: %s", exc)

def keyword_is_banned(text: str):
    lower = text.lower()
    for word in banned_words:
        if word.lower() in lower:
            return word
    return None

async def _handle_violation(event, user_id, username, full_name, chat_id,
                            message_text, violation_reason, is_bot_sender):
    await delete_msg(chat_id, event.id)
    if is_bot_sender:
        log.info("🤖 Bot message deleted (no warning): %s", full_name)
        return
    prior = get_warning_count(user_id)
    if prior == 0:
        record_violation(user_id, username, full_name, violation_reason)
        await send_warning(event, violation_reason, user_id, username, full_name)
        log.info("⚠️ Warned %s (%s)", full_name, user_id)
    else:
        record_violation(user_id, username, full_name, violation_reason)
        temp_ban_hours = int(get_setting('temp_ban_duration') or 0)
        await ban_user(chat_id, user_id, duration_hours=temp_ban_hours)
        await notify_admin(user_id, username, full_name, message_text, violation_reason,
                           f"BANNED ({temp_ban_hours}h)" if temp_ban_hours else "PERMANENT BAN")
        log.info("🔨 Banned %s (%s)", full_name, user_id)

# ─── Banned Words List ────────────────────────────────────────────────────────
banned_words: list = [
    "dm me for signals", "dm for signals", "i sell signals",
    "selling signals", "join my vip", "join our vip",
    "vip signals", "paid signals", "premium signals",
    "signal provider", "signal service", "buy signals",
    "join my group", "join our group", "join my channel",
    "join our channel", "subscribe to my channel",
    "click the link", "link in bio", "check my bio",
    "use my referral", "referral link", "use my link",
    "register with my link", "deposit via my link",
    "use my code", "promo code", "invite link",
    "guaranteed profit", "guaranteed return", "100% profit",
    "risk free", "risk-free", "no loss", "double your money",
    "i will manage your account", "managed account",
    "send me money", "send usdt", "send btc", "invest with me",
    "investment platform", "fund your account", "withdraw daily",
    "earn daily", "earn money online", "make money online",
    "passive income", "financial freedom",
    "account for sale", "selling account", "buying account",
    "broker account for sale", "ea for sale", "robot for sale",
    "whatsapp me", "contact me on whatsapp", "dm me",
    "message me", "inbox me", "contact for promo",
    "available for hire", "hire me", "i offer services", "we offer services",
    "you idiot", "you are stupid", "you are dumb", "you fool",
    "shut up", "go to hell", "son of a bitch", "motherfucker", "you loser",
    "ሲግናል እሸጣለሁ", "ሲግናል እልካለሁ", "ሲግናል ይግዙ", "ሲግናል ይጠቀሙ", "ሲግናል ቡድን",
    "ዲኤም አድርጉ", "ዲኤም አድርጉኝ", "ለሲግናል ዲኤም", "ቪአይፒ ቡድን", "ቪአይፒ ይቀላቀሉ", "ሲግናል ለማግኘት",
    "ቡድኑን ይቀላቀሉ", "ቻናሉን ይቀላቀሉ", "ሊንኩን ይጫኑ", "ሊንክ ይጠቀሙ",
    "ሪፈራል ሊንክ", "ሊንኬን ተጠቀሙ", "ቻናሌን ተቀላቀሉ", "ቡድኔን ተቀላቀሉ", "ሊንኩን ተጫኑ",
    "ትርፍ እናረጋግጣለን", "ትርፍ ዋስትና", "መቶ ፐርሰንት ትርፍ", "ኪሳራ የለም",
    "ገንዘብ ይላኩ", "ዩኤስዲቲ ይላኩ", "ቢቲሲ ይላኩ", "ሂሳብዎን ያስተዳድሩ",
    "ሂሳብ ያስተዳድራለሁ", "ኢንቨስት ያድርጉ", "ኢንቨስትመንት", "ትርፍ ያግኙ",
    "ዕለታዊ ትርፍ", "ገንዘብ ያስቀምጡ", "ፈጣን ትርፍ", "ሀብት ይሁኑ",
    "አካውንት ይሸጣል", "አካውንት እሸጣለሁ", "አካውንት ለሽያጭ", "ሮቦት ለሽያጭ", "ኢኤ ለሽያጭ",
    "ዋትሳፕ ያግኙኝ", "ቴሌግራም ያግኙኝ", "ያናግሩኝ", "መልዕክት ይላኩልኝ",
    "ደደብ ነህ", "ደደብ ነሽ", "ሞኝ ነህ", "ሞኝ ነሽ", "ዝምበል", "ውሻ", "አህያ",
    "ጅል ነህ", "ጅል ነሽ", "ከንቱ", "ጊዜ ሌባ", "ፋይዳ የለህም", "ፋይዳ የለሽም",
]

# ─── Keyboards for Settings ───────────────────────────────────────────────────
def get_main_settings_keyboard():
    return [
        [Button.inline("⚠️ Warning Settings", b"set_warning")],
        [Button.inline("🔨 Ban Settings", b"set_ban")],
        [Button.inline("🔇 Silent Filter", b"set_silent")],
        [Button.inline("📤 Forward Control", b"set_forward")],
        [Button.inline("📝 Logging", b"set_logging")],
        [Button.inline("🤖 AI Assistant", b"set_ai")],
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
    temp_hours = get_setting('temp_ban_duration')
    temp_hours_str = "Permanent" if temp_hours == '0' else f"{temp_hours} hours"
    return [
        [Button.inline(f"Temporary ban: {temp_hours_str}", b"set_temp_ban")],
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

def get_logging_keyboard():
    log_file = get_setting('log_to_file')
    return [
        [Button.inline(f"Log to file: {'✅ ON' if log_file=='on' else '❌ OFF'}", b"toggle_log_file")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

def get_ai_keyboard():
    return [
        [Button.inline("🤖 Ask AI with /ask", b"nothing")],
        [Button.inline("🔙 Back", b"settings_back")]
    ]

async def ask_gemini(question: str) -> str:
    global _gemini_quota_exhausted
    if _gemini_quota_exhausted:
        return "I'm sorry, I am currently unavailable as my assistant service is offline. Please try again later."
    try:
        model = get_gemini_model()
        response = await asyncio.to_thread(model.generate_content, question)
        answer = response.text.strip()
        return f"🤖 *Squad 4x Assistant*:\n\n{answer}"
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "quota" in err_str.lower() or "RESOURCE_EXHAUSTED" in err_str:
            rotate_key()
            if _gemini_quota_exhausted:
                return "I'm sorry, my assistant service is currently offline. Please contact the group admin."
        return "Sorry, I encountered an error while processing your request."

# ═══════════════════════════════════════════════════════════════════════════════
# BOT CLIENT — Admin Commands
# ═══════════════════════════════════════════════════════════════════════════════

@bot_client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    status = "✅ Connected" if userbot_connected else "❌ Not connected — use /connect"
    target = f"@{TARGET_BOT_USERNAME}" if TARGET_BOT_USERNAME else str(TARGET_BOT_ID or "Not set")
    exempt = str(EXEMPT_CHANNEL_ID) if EXEMPT_CHANNEL_ID else "Not set"
    silent_count = len(get_silent_words())
    await event.reply(
        "🤖 **Squad 4x Group Manager**\n\n"
        f"👤 **Userbot:** {status}\n"
        f"🎯 **Target bot:** {target}\n"
        f"🛡️ **Exempt channel:** {exempt}\n"
        f"🔇 **Silent words:** {silent_count}\n\n"
        "**Commands:**\n"
        "/connect — Login personal account as userbot\n"
        "/filter  — Manage banned keywords (with warning)\n"
        "/silentfilter — Manage silent delete words (no warning, 3 strikes ban)\n"
        "/settings — Open settings panel\n"
        "/ask <question> — Ask AI assistant (Squad 4x helper)\n"
        "/status  — Show bot health\n"
        "/cancel  — Cancel current operation\n\n"
        "**Moderation layers:**\n"
        "0️⃣ 🔥 **FORWARDED messages are deleted immediately**\n"
        "1️⃣ Spam heuristics (caps, repeats, phrases)\n"
        "2️⃣ Silent words (immediate delete, no warning)\n"
        "3️⃣ Keyword filter (warning then ban)\n"
        "4️⃣ Gemini AI queue (suspicious only)\n"
        "5️⃣ Userbot deletes target bot messages **only**\n"
        "6️⃣ Exempt channel messages are NEVER moderated\n"
        "7️⃣ 3 silent strikes → automatic ban\n\n"
        "✅ English & Amharic | 🔄 Multi-key Gemini"
    )

@bot_client.on(events.NewMessage(pattern="/status"))
async def cmd_status(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    queue_size = _gemini_queue.qsize()
    key_count = len(GEMINI_KEYS)
    ub_status = "✅ Connected" if userbot_connected else "❌ Disconnected"
    silent_words = get_silent_words()
    await event.reply(
        f"📊 **Bot Status**\n\n"
        f"👤 Userbot: {ub_status}\n"
        f"🤖 Gemini keys: {key_count} | Active key: #{_current_key_index + 1}\n"
        f"📥 Gemini queue: {queue_size} message(s) pending\n"
        f"📝 Banned words: {len(banned_words)}\n"
        f"🔇 Silent words: {len(silent_words)}\n"
        f"🎯 Target bot: {TARGET_BOT_USERNAME or TARGET_BOT_ID or 'None'}\n"
        f"🛡️ Exempt channel: {EXEMPT_CHANNEL_ID or 'None'}\n"
        f"⚙️ Private warning: {get_setting('private_warning')}\n"
        f"⏱️ Temp ban: {get_setting('temp_ban_duration')}h\n"
        f"🕒 Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )

@bot_client.on(events.NewMessage(pattern="/settings"))
async def cmd_settings(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply("⚙️ **Bot Settings**\nChoose a category:", buttons=get_main_settings_keyboard())

@bot_client.on(events.NewMessage(pattern="/ask"))
async def cmd_ask(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    question = event.raw_text.replace("/ask", "").strip()
    if not question:
        await event.reply("Please provide a question. Example: `/ask What is the best Forex strategy?`")
        return
    await event.reply("🤔 Thinking...")
    answer = await ask_gemini(question)
    await event.reply(answer, parse_mode="md")

# ─── Callback Handler for Settings ──────────────────────────────────────────
@bot_client.on(events.CallbackQuery)
async def settings_callback(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ Admin only.", alert=True)
        return
    data = event.data.decode()
    if data == "settings_close":
        await event.edit("Settings closed.")
        return
    elif data == "settings_back":
        await event.edit("⚙️ **Bot Settings**\nChoose a category:", buttons=get_main_settings_keyboard())
        return
    elif data == "set_warning":
        await event.edit("⚠️ **Warning Settings**", buttons=get_warning_keyboard())
    elif data == "set_ban":
        await event.edit("🔨 **Ban Settings**", buttons=get_ban_keyboard())
    elif data == "set_silent":
        await event.edit("🔇 Use /silentfilter to manage silent words.", buttons=[[Button.inline("🔙 Back", b"settings_back")]])
    elif data == "set_forward":
        await event.edit("📤 **Forward Control**", buttons=get_forward_keyboard())
    elif data == "set_logging":
        await event.edit("📝 **Logging Settings**", buttons=get_logging_keyboard())
    elif data == "set_ai":
        await event.edit("🤖 **AI Assistant**\nUse `/ask <question>` to chat with me.\nI am Squad 4x group assistant.", buttons=get_ai_keyboard())
    elif data == "toggle_private_warning":
        current = get_setting('private_warning')
        new = 'off' if current == 'on' else 'on'
        set_setting('private_warning', new)
        await event.edit("⚠️ **Warning Settings**", buttons=get_warning_keyboard())
        await event.answer(f"Private warning turned {new}", alert=True)
    elif data == "set_warning_duration":
        admin_state[ADMIN_ID] = "awaiting_warning_duration"
        await event.edit("Please send the new warning duration in seconds (e.g., 300 for 5 minutes).\nSend /cancel to abort.")
    elif data == "set_temp_ban":
        admin_state[ADMIN_ID] = "awaiting_temp_ban"
        await event.edit("Please send the temporary ban duration in hours (0 = permanent, e.g., 24 for one day).\nSend /cancel to abort.")
    elif data == "toggle_delete_forwards":
        current = get_setting('delete_all_forwards')
        new = 'off' if current == 'on' else 'on'
        set_setting('delete_all_forwards', new)
        await event.edit("📤 **Forward Control**", buttons=get_forward_keyboard())
        await event.answer(f"Delete all forwards turned {new}", alert=True)
    elif data == "set_exempt_channels":
        admin_state[ADMIN_ID] = "awaiting_exempt_channels"
        await event.edit("Please send a comma-separated list of channel IDs to exempt from forward deletion (e.g., -1001234567890,-1009876543210).\nSend /cancel to abort.")
    elif data == "toggle_log_file":
        current = get_setting('log_to_file')
        new = 'off' if current == 'on' else 'on'
        set_setting('log_to_file', new)
        await event.edit("📝 **Logging Settings**", buttons=get_logging_keyboard())
        await event.answer(f"Log to file turned {new}", alert=True)
    elif data == "nothing":
        await event.answer("No action available.", alert=True)

# ─── Admin Private Message Handler ──────────────────────────────────────────
@bot_client.on(events.NewMessage)
async def admin_private_handler(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    if event.text and event.text.startswith("/"):
        return
    text = (event.raw_text or "").strip()
    if not text:
        return

    # /connect flow
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
                await event.reply("📩 **OTP sent!**\n\nSend the code with spaces:\n`1 2 3 4 5`")
            except FloodWaitError as exc:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"⚠️ Flood wait {exc.seconds}s. Try again later.")
            except Exception as exc:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ Failed: {exc}")
            return
        if step == "code":
            code = text.replace(" ", "")
            try:
                await user_client.sign_in(conn["phone"], code, phone_code_hash=conn["phone_code_hash"])
                connect_state.pop(ADMIN_ID, None)
                global userbot_connected
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ **Userbot connected!**\n👤 {me.first_name} (@{me.username or 'no username'})")
                log.info("✅ Userbot logged in: %s", me.first_name)
            except SessionPasswordNeededError:
                conn["step"] = "password"
                await event.reply("🔐 **2FA enabled.** Send your password:")
            except PhoneCodeInvalidError:
                await event.reply("❌ Wrong code. Use /connect to restart.")
            except Exception as exc:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ Login failed: {exc}")
            return
        if step == "password":
            try:
                await user_client.sign_in(password=text)
                connect_state.pop(ADMIN_ID, None)
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ **Userbot connected (2FA)!**\n👤 {me.first_name} (@{me.username or 'no username'})")
                log.info("✅ Userbot 2FA login: %s", me.first_name)
            except PasswordHashInvalidError:
                await event.reply("❌ Wrong 2FA password. Try again:")
            except Exception as exc:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ 2FA failed: {exc}")
            return

    # Settings inputs
    state = admin_state.get(ADMIN_ID)
    if state:
        if state == "awaiting_warning_duration":
            try:
                seconds = int(text)
                if seconds < 10:
                    await event.reply("Duration must be at least 10 seconds.")
                    return
                set_setting('warning_duration', str(seconds))
                await event.reply(f"✅ Warning duration set to {seconds} seconds.", buttons=[[Button.inline("🔙 Back to Settings", b"settings_back")]])
            except ValueError:
                await event.reply("Please send a valid number (seconds).")
            admin_state.pop(ADMIN_ID, None)
        elif state == "awaiting_temp_ban":
            try:
                hours = int(text)
                if hours < 0:
                    await event.reply("Hours cannot be negative. 0 = permanent.")
                    return
                set_setting('temp_ban_duration', str(hours))
                await event.reply(f"✅ Temporary ban set to {hours} hour(s).", buttons=[[Button.inline("🔙 Back to Settings", b"settings_back")]])
            except ValueError:
                await event.reply("Please send a valid number (hours).")
            admin_state.pop(ADMIN_ID, None)
        elif state == "awaiting_exempt_channels":
            set_setting('forward_exempt_channels', text)
            await event.reply(f"✅ Exempt channels set to: {text}", buttons=[[Button.inline("🔙 Back to Settings", b"settings_back")]])
            admin_state.pop(ADMIN_ID, None)
        else:
            # Normal filter or silent filter
            pass
        return

    # Normal filter add/remove (from /filter)
    nstate = admin_state.get(ADMIN_ID)
    if nstate and nstate in ("awaiting_add", "awaiting_remove"):
        word = text.lower()
        if nstate == "awaiting_add":
            admin_state.pop(ADMIN_ID, None)
            if word in banned_words:
                await event.reply(f"⚠️ `{word}` already in filter.", buttons=make_filter_keyboard())
            else:
                banned_words.append(word)
                await event.reply(f"✅ **Added:** `{word}`\nTotal: **{len(banned_words)}**", buttons=make_filter_keyboard())
            log.info("🔧 Admin added: '%s'", word)
        elif nstate == "awaiting_remove":
            admin_state.pop(ADMIN_ID, None)
            if word in banned_words:
                banned_words.remove(word)
                await event.reply(f"✅ **Removed:** `{word}`\nTotal: **{len(banned_words)}**", buttons=make_filter_keyboard())
                log.info("🔧 Admin removed: '%s'", word)
            else:
                await event.reply(f"❌ `{word}` not found.", buttons=make_filter_keyboard())
        return

    # Silent filter add/remove (from /silentfilter)
    sstate = silent_admin_state.get(ADMIN_ID)
    if sstate and sstate in ("awaiting_silent_add", "awaiting_silent_remove"):
        word = text.lower()
        if sstate == "awaiting_silent_add":
            silent_admin_state.pop(ADMIN_ID, None)
            add_silent_word(word)
            silent_words = get_silent_words()
            await event.reply(f"🔇 **Added silent word:** `{word}`\nTotal silent: **{len(silent_words)}**", buttons=make_silent_filter_keyboard())
        elif sstate == "awaiting_silent_remove":
            silent_admin_state.pop(ADMIN_ID, None)
            remove_silent_word(word)
            silent_words = get_silent_words()
            await event.reply(f"🔇 **Removed silent word:** `{word}`\nTotal silent: **{len(silent_words)}**", buttons=make_silent_filter_keyboard())
        return

# ─── Filter Keyboards ────────────────────────────────────────────────────────
def make_filter_keyboard():
    return [
        [Button.inline("➕ Add Word", b"filter_add"),
         Button.inline("➖ Remove Word", b"filter_remove")],
        [Button.inline("📋 Show All Words", b"filter_show")],
    ]

def make_silent_filter_keyboard():
    return [
        [Button.inline("➕ Add Silent Word", b"silent_add"),
         Button.inline("➖ Remove Silent Word", b"silent_remove")],
        [Button.inline("📋 Show Silent Words", b"silent_show")],
    ]

# ─── Callback Handler for Filter ────────────────────────────────────────────
@bot_client.on(events.CallbackQuery)
async def filter_callback_handler(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ Admin only.", alert=True)
        return
    data = event.data
    try:
        if data == b"filter_add":
            admin_state[ADMIN_ID] = "awaiting_add"
            await event.edit(
                "➕ **Add Banned Word**\n\nSend the word or phrase to ban.",
                buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]]
            )
        elif data == b"filter_remove":
            if not banned_words:
                await event.answer("No words in filter!", alert=True)
                return
            admin_state[ADMIN_ID] = "awaiting_remove"
            word_list = "\n".join(f"{i+1}. `{w}`" for i, w in enumerate(banned_words))
            await event.edit(
                f"➖ **Remove Banned Word**\n\nCurrent words:\n{word_list}\n\nSend the exact word:",
                buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]]
            )
        elif data == b"filter_show":
            if not banned_words:
                await event.answer("Filter list is empty!", alert=True)
                return
            word_list = "\n".join(f"• `{w}`" for w in banned_words)
            await event.edit(
                f"📋 **Banned Words ({len(banned_words)} total)**\n\n{word_list}",
                buttons=make_filter_keyboard()
            )
        elif data == b"filter_cancel":
            admin_state.pop(ADMIN_ID, None)
            await event.edit(
                f"✅ Cancelled. — {len(banned_words)} word(s) active",
                buttons=make_filter_keyboard()
            )
        elif data == b"silent_add":
            silent_admin_state[ADMIN_ID] = "awaiting_silent_add"
            await event.edit(
                "🔇 **Add Silent Word**\n\nSend the word or phrase to delete silently (no warning).",
                buttons=[[Button.inline("❌ Cancel", b"silent_cancel")]]
            )
        elif data == b"silent_remove":
            silent_words = get_silent_words()
            if not silent_words:
                await event.answer("No silent words!", alert=True)
                return
            silent_admin_state[ADMIN_ID] = "awaiting_silent_remove"
            word_list = "\n".join(f"{i+1}. `{w}`" for i, w in enumerate(silent_words))
            await event.edit(
                f"🔇 **Remove Silent Word**\n\nCurrent words:\n{word_list}\n\nSend the exact word:",
                buttons=[[Button.inline("❌ Cancel", b"silent_cancel")]]
            )
        elif data == b"silent_show":
            silent_words = get_silent_words()
            if not silent_words:
                await event.answer("No silent words!", alert=True)
                return
            word_list = "\n".join(f"• `{w}`" for w in silent_words)
            await event.edit(
                f"🔇 **Silent Words ({len(silent_words)} total)**\n\n{word_list}",
                buttons=make_silent_filter_keyboard()
            )
        elif data == b"silent_cancel":
            silent_admin_state.pop(ADMIN_ID, None)
            silent_words = get_silent_words()
            await event.edit(
                f"✅ Cancelled. — {len(silent_words)} silent word(s) active",
                buttons=make_silent_filter_keyboard()
            )
    except MessageNotModifiedError:
        pass
    except Exception as exc:
        log.error("Callback error: %s", exc)

# ═══════════════════════════════════════════════════════════════════════════════
# BOT CLIENT — Group Message Handler
# ═══════════════════════════════════════════════════════════════════════════════
@bot_client.on(events.NewMessage(chats=GROUP_ID))
async def handle_group_message(event):
    if event.out:
        return
    sender = await event.get_sender()
    if sender is None:
        return

    # Exempt channel (channel's own posts are never moderated)
    if EXEMPT_CHANNEL_ID and sender.id == EXEMPT_CHANNEL_ID:
        log.info(f"🛡️ Exempt channel message ignored (ID: {sender.id})")
        return

    me_bot = await bot_client.get_me()
    if sender.id == me_bot.id:
        return
    if sender.id == ADMIN_ID:
        return
    if userbot_connected:
        try:
            me_user = await user_client.get_me()
            if sender.id == me_user.id:
                return
        except Exception:
            pass

    message_text = (event.raw_text or "").strip()
    chat_id = event.chat_id

    # Forward deletion based on settings
    delete_all_forwards = get_setting('delete_all_forwards') == 'on'
    if delete_all_forwards and event.message.forward:
        exempt_str = get_setting('forward_exempt_channels') or ""
        exempt_ids = [int(x.strip()) for x in exempt_str.split(",") if x.strip()]
        forward_from_id = None
        if event.message.forward.from_id:
            if hasattr(event.message.forward.from_id, 'channel_id'):
                forward_from_id = event.message.forward.from_id.channel_id
            else:
                forward_from_id = event.message.forward.from_id.user_id
        if forward_from_id and forward_from_id in exempt_ids:
            log.info(f"⏩ Exempt forward from channel {forward_from_id} ignored")
        else:
            await delete_msg(chat_id, event.id)
            log.info("🚫 [FORWARD] Deleted forwarded message from %s", sender.id)
            return

    if not message_text:
        return

    user_id = sender.id
    username = getattr(sender, "username", "") or ""
    full_name = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or username or str(user_id)
    is_bot = getattr(sender, "bot", False)

    log.info("📨 [%s%s | %s]: %s", "🤖 " if is_bot else "", full_name, user_id, message_text[:80])

    # Silent word check
    silent_word = message_contains_silent_word(message_text)
    if silent_word:
        await delete_msg(chat_id, event.id)
        if not is_bot:
            strikes = increment_silent_violation(user_id)
            log.info(f"🔇 Silent delete: '{silent_word}' from {full_name} (strike {strikes}/3)")
            if strikes >= 3:
                temp_ban_hours = int(get_setting('temp_ban_duration') or 0)
                await ban_user(chat_id, user_id, duration_hours=temp_ban_hours)
                await notify_admin(user_id, username, full_name, message_text,
                                   f"3 silent strikes (word: '{silent_word}')",
                                   f"BANNED ({temp_ban_hours}h)" if temp_ban_hours else "PERMANENT BAN")
                reset_silent_violation(user_id)
                log.info(f"🔨 Banned {full_name} due to 3 silent violations")
        else:
            log.info(f"🤖 Bot silent delete (no strike): {full_name}")
        return

    # Spam heuristics
    spam_flag, spam_reason = is_spam(message_text)
    if spam_flag:
        await _handle_violation(event, user_id, username, full_name, chat_id,
                                 message_text, f"Spam: {spam_reason}", is_bot)
        return

    # Normal keyword filter
    matched_word = keyword_is_banned(message_text)
    if matched_word:
        await _handle_violation(event, user_id, username, full_name, chat_id,
                                 message_text, f"Banned word: '{matched_word}'", is_bot)
        return

    # Gemini AI
    result = await queue_gemini_analysis(message_text)
    if result["verdict"] == "PROHIBITED":
        await _handle_violation(event, user_id, username, full_name, chat_id,
                                 message_text, result["reason"], is_bot)
        return

    log.info("✅ Allowed: %s", full_name)

# ═══════════════════════════════════════════════════════════════════════════════
# USERBOT CLIENT — ONLY deletes target bot messages
# ═══════════════════════════════════════════════════════════════════════════════
@user_client.on(events.NewMessage(chats=GROUP_ID))
async def userbot_target_bot_deleter(event):
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

    sender_username = (getattr(sender, "username", "") or "").lower()
    is_target = False
    if TARGET_BOT_USERNAME and sender_username == TARGET_BOT_USERNAME.lower():
        is_target = True
    if TARGET_BOT_ID and sender.id == TARGET_BOT_ID:
        is_target = True

    if is_target:
        try:
            await user_client.delete_messages(event.chat_id, event.id)
            log.info("🎯 [USERBOT] Deleted target bot message from %s (%s)", sender_username or sender.id, sender.id)
        except Exception as exc:
            log.warning("Failed to delete target bot message: %s", exc)

# ─── Entry Point ──────────────────────────────────────────────────────────────
async def main():
    global userbot_connected

    init_db()
    await bot_client.start(bot_token=BOT_TOKEN)
    bot_me = await bot_client.get_me()
    log.info("🤖 Bot: @%s (ID: %s)", bot_me.username, bot_me.id)

    try:
        os.makedirs(os.path.dirname(USER_SESSION) if os.path.dirname(USER_SESSION) else ".", exist_ok=True)
        await user_client.connect()
        if await user_client.is_user_authorized():
            userbot_connected = True
            ume = await user_client.get_me()
            log.info("✅ Userbot: %s (@%s)", ume.first_name, ume.username or "no username")
        else:
            log.info("⚠️ Userbot not logged in. Send /connect to the bot.")
            await user_client.disconnect()
    except Exception as exc:
        log.warning("Userbot session load failed: %s", exc)

    try:
        entity = await bot_client.get_entity(GROUP_ID)
        log.info("✅ Monitoring: %s (ID: %s)", entity.title, GROUP_ID)
    except Exception as exc:
        log.error("❌ Cannot access group: %s", exc)

    target_desc = f"@{TARGET_BOT_USERNAME}" if TARGET_BOT_USERNAME else str(TARGET_BOT_ID or "None")
    exempt_desc = str(EXEMPT_CHANNEL_ID) if EXEMPT_CHANNEL_ID else "None"
    silent_count = len(get_silent_words())
    asyncio.create_task(gemini_queue_worker())
    log.info("📡 Gemini: %s key(s) | Gap: %ss", len(GEMINI_KEYS), GEMINI_CALL_GAP)
    log.info("🎯 Target bot (userbot deletes only this): %s", target_desc)
    log.info("🛡️ Exempt channel (never moderated): %s", exempt_desc)
    log.info("🔇 Silent words loaded: %s", silent_count)
    log.info("🔥 Forward deletion: %s", get_setting('delete_all_forwards'))
    log.info("👤 Admin: %s", ADMIN_ID)

    if userbot_connected:
        await asyncio.gather(
            bot_client.run_until_disconnected(),
            user_client.run_until_disconnected(),
        )
    else:
        await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
