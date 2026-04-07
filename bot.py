"""
Forex Group Management Bot — Dual Client (Bot + Userbot)
─────────────────────────────────────────────────────────
- Bot client: Spam filter + Keyword filter + Gemini AI
- Userbot:   ONLY deletes messages from a specific target bot
- Persistent warnings (SQLite on /data volume)

FIXES INCLUDED:
- asyncio.gather crash fixed (user_client runs only if connected)
- is_spam false-positives fixed (Forex safe words, higher thresholds)
- SQLite thread-safe with Lock
- Session path handling fixed (no double .session)
- /status command for health check
- Userbot never deletes anything except the target bot
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

# ─── Target Bot (userbot will delete EVERY message from this bot only) ────────
TARGET_BOT_USERNAME = os.environ.get("TARGET_BOT_USERNAME", "").lstrip("@")
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"]) if os.environ.get("TARGET_BOT_ID") else None

# ─── Spam Detection Settings ──────────────────────────────────────────────────
SPAM_CAPS_RATIO      = 0.75
SPAM_REPEAT_CHARS    = 6
SPAM_MAX_PUNCTUATION = 0.45
SPAM_MIN_WORD_LEN    = 3
SPAM_REPEATED_WORDS  = 4

# Forex words that are never spam (prevents false positives)
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

# ─── Gemini Setup — Key Rotation ──────────────────────────────────────────────
_raw_keys = os.environ["GEMINI_API_KEY"]
GEMINI_KEYS: list = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_current_key_index = 0

def get_gemini_model() -> genai.GenerativeModel:
    genai.configure(api_key=GEMINI_KEYS[_current_key_index])
    return genai.GenerativeModel("gemini-2.0-flash")

def rotate_key() -> bool:
    global _current_key_index
    next_index = (_current_key_index + 1) % len(GEMINI_KEYS)
    if next_index == 0 and len(GEMINI_KEYS) == 1:
        return False
    _current_key_index = next_index
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

# ─── SQLite Warnings (thread-safe) ───────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/warnings.db")
_db_lock = threading.Lock()

def _db_conn():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_warnings_db():
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
        conn.commit()
        conn.close()
    log.info("✅ Warnings DB ready at %s", DB_PATH)

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

# ─── Spam Detection ───────────────────────────────────────────────────────────
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

# ─── Gemini Queue ─────────────────────────────────────────────────────────────
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
                rotate_key()
                if keys_tried >= total_keys:
                    retry_wait = 60
                    m = re.search(r'"seconds":\s*(\d+)', err_str)
                    if m:
                        retry_wait = min(int(m.group(1)) + 5, 120)
                    log.warning("⏳ All keys exhausted. Waiting %ss...", retry_wait)
                    await asyncio.sleep(retry_wait)
                    keys_tried = 0
                continue
            log.warning("⚠️ Gemini error: %s", exc)
            return {"verdict": "ALLOWED", "reason": "Gemini error."}

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

async def ban_user(chat_id: int, user_id: int):
    try:
        await bot_client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(until_date=None, view_messages=True)
        ))
        log.info("🔨 Banned user %s", user_id)
    except Exception as exc:
        log.error("Ban failed for %s: %s", user_id, exc)

async def send_warning(event, reason: str, user_id: int, username: str, full_name: str):
    try:
        mention = f"@{username}" if username else f"[{full_name}](tg://user?id={user_id})"
        warning_msg = await bot_client.send_message(
            event.chat_id,
            f"⚠️ **Warning / ማስጠንቀቂያ** — {mention}\n\n"
            f"🇬🇧 This is your **only warning**. Next violation = immediate ban.\n"
            f"🇪🇹 ይህ **የመጨረሻ ማስጠንቀቂያዎ** ነው። ደግመው ከጣሱ ወዲያውኑ ይታገዳሉ።\n\n"
            f"📋 **Reason:** {reason}",
            parse_mode="md"
        )
        async def _delete_later():
            await asyncio.sleep(300)
            await delete_msg(event.chat_id, warning_msg.id)
        asyncio.create_task(_delete_later())
    except Exception as exc:
        log.warning("Warning send failed: %s", exc)

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
        await ban_user(chat_id, user_id)
        await notify_admin(user_id, username, full_name, message_text, violation_reason, "🔨 BANNED")
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

# ─── Filter Keyboard ──────────────────────────────────────────────────────────
def make_filter_keyboard():
    return [
        [Button.inline("➕ Add Word", b"filter_add"),
         Button.inline("➖ Remove Word", b"filter_remove")],
        [Button.inline("📋 Show All Words", b"filter_show")],
    ]

# ═══════════════════════════════════════════════════════════════════════════════
# BOT CLIENT — Admin Commands
# ═══════════════════════════════════════════════════════════════════════════════

@bot_client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    status = "✅ Connected" if userbot_connected else "❌ Not connected — use /connect"
    target = f"@{TARGET_BOT_USERNAME}" if TARGET_BOT_USERNAME else str(TARGET_BOT_ID or "Not set")
    await event.reply(
        "🤖 **Forex Group Bot — Admin Panel**\n\n"
        f"👤 **Userbot:** {status}\n"
        f"🎯 **Target bot:** {target}\n\n"
        "**Commands:**\n"
        "/connect — Login personal account as userbot\n"
        "/filter  — Manage banned keywords\n"
        "/status  — Show bot health\n"
        "/cancel  — Cancel current operation\n\n"
        "**Moderation layers:**\n"
        "1️⃣ Spam heuristics (caps, repeats, phrases)\n"
        "2️⃣ Keyword filter (instant, no API)\n"
        "3️⃣ Gemini AI queue (suspicious only)\n"
        "4️⃣ Userbot deletes target bot messages **only**\n"
        "5️⃣ 1st offence → warning (auto-deleted in 5min)\n"
        "6️⃣ 2nd offence → permanent ban + report\n\n"
        "✅ English & Amharic | 🔄 Multi-key Gemini"
    )

@bot_client.on(events.NewMessage(pattern="/status"))
async def cmd_status(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    queue_size = _gemini_queue.qsize()
    key_count = len(GEMINI_KEYS)
    ub_status = "✅ Connected" if userbot_connected else "❌ Disconnected"
    await event.reply(
        f"📊 **Bot Status**\n\n"
        f"👤 Userbot: {ub_status}\n"
        f"🤖 Gemini keys: {key_count} | Active key: #{_current_key_index + 1}\n"
        f"📥 Gemini queue: {queue_size} message(s) pending\n"
        f"📝 Banned words: {len(banned_words)}\n"
        f"🎯 Target bot: {TARGET_BOT_USERNAME or TARGET_BOT_ID or 'None'}\n"
        f"🕒 Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )

@bot_client.on(events.NewMessage(pattern="/filter"))
async def cmd_filter(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(
        f"🔧 **Keyword Filter Panel**\n\nCurrently **{len(banned_words)}** banned word(s).",
        buttons=make_filter_keyboard()
    )

@bot_client.on(events.NewMessage(pattern="/connect"))
async def cmd_connect(event):
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
                await event.reply(f"✅ Userbot reconnected!\n👤 {me.first_name} (@{me.username or 'no username'})")
                return
        except Exception as exc:
            log.warning("Session reconnect failed: %s", exc)
    connect_state[ADMIN_ID] = {"step": "phone"}
    await event.reply("📱 **Userbot Login**\n\nSend your phone number with country code:\n`+251912345678`")

@bot_client.on(events.NewMessage(pattern="/cancel"))
async def cmd_cancel(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    connect_state.pop(ADMIN_ID, None)
    admin_state.pop(ADMIN_ID, None)
    await event.reply("✅ Cancelled.")

# ─── Callback Handler ─────────────────────────────────────────────────────────
@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
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
                await event.answer("No words in filter yet!", alert=True)
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
    except MessageNotModifiedError:
        pass
    except Exception as exc:
        log.error("Callback error: %s", exc)

# ─── Admin Private Message Handler ───────────────────────────────────────────
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

    # /filter flow
    state = admin_state.get(ADMIN_ID)
    if not state:
        return
    word = text.lower()
    if state == "awaiting_add":
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            await event.reply(f"⚠️ `{word}` already in filter.", buttons=make_filter_keyboard())
        else:
            banned_words.append(word)
            await event.reply(f"✅ **Added:** `{word}`\nTotal: **{len(banned_words)}**", buttons=make_filter_keyboard())
        log.info("🔧 Admin added: '%s'", word)
    elif state == "awaiting_remove":
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            banned_words.remove(word)
            await event.reply(f"✅ **Removed:** `{word}`\nTotal: **{len(banned_words)}**", buttons=make_filter_keyboard())
            log.info("🔧 Admin removed: '%s'", word)
        else:
            await event.reply(f"❌ `{word}` not found.", buttons=make_filter_keyboard())

# ═══════════════════════════════════════════════════════════════════════════════
# BOT CLIENT — Group Message Handler (spam + keyword + Gemini)
# ═══════════════════════════════════════════════════════════════════════════════
@bot_client.on(events.NewMessage(chats=GROUP_ID))
async def handle_group_message(event):
    if event.out:
        return
    sender = await event.get_sender()
    if sender is None:
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
    if not message_text:
        return

    user_id = sender.id
    username = getattr(sender, "username", "") or ""
    full_name = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or username or str(user_id)
    chat_id = event.chat_id
    is_bot = getattr(sender, "bot", False)

    log.info("📨 [%s%s | %s]: %s", "🤖 " if is_bot else "", full_name, user_id, message_text[:80])

    spam_flag, spam_reason = is_spam(message_text)
    if spam_flag:
        await _handle_violation(event, user_id, username, full_name, chat_id,
                                 message_text, f"Spam: {spam_reason}", is_bot)
        return

    matched_word = keyword_is_banned(message_text)
    if matched_word:
        await _handle_violation(event, user_id, username, full_name, chat_id,
                                 message_text, f"Banned word: '{matched_word}'", is_bot)
        return

    result = await queue_gemini_analysis(message_text)
    if result["verdict"] == "PROHIBITED":
        await _handle_violation(event, user_id, username, full_name, chat_id,
                                 message_text, result["reason"], is_bot)
        return

    log.info("✅ Allowed: %s", full_name)

# ═══════════════════════════════════════════════════════════════════════════════
# USERBOT CLIENT — ONLY deletes target bot messages (nothing else)
# ═══════════════════════════════════════════════════════════════════════════════
@user_client.on(events.NewMessage(chats=GROUP_ID))
async def userbot_target_bot_deleter(event):
    if not userbot_connected:
        return
    sender = await event.get_sender()
    if sender is None:
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
    # else: do absolutely nothing

# ─── Entry Point (FIXED: only run user_client if connected) ───────────────────
async def main():
    global userbot_connected

    init_warnings_db()
    await bot_client.start(bot_token=BOT_TOKEN)
    bot_me = await bot_client.get_me()
    log.info("🤖 Bot: @%s (ID: %s)", bot_me.username, bot_me.id)

    # Try to load userbot session
    try:
        os.makedirs(os.path.dirname(USER_SESSION) if os.path.dirname(USER_SESSION) else ".", exist_ok=True)
        await user_client.connect()
        if await user_client.is_user_authorized():
            userbot_connected = True
            ume = await user_client.get_me()
            log.info("✅ Userbot: %s (@%s)", ume.first_name, ume.username or "no username")
        else:
            log.info("⚠️ Userbot not logged in. Send /connect to the bot.")
            await user_client.disconnect()  # avoid dangling connection
    except Exception as exc:
        log.warning("Userbot session load failed: %s", exc)

    # Verify group access
    try:
        entity = await bot_client.get_entity(GROUP_ID)
        log.info("✅ Monitoring: %s (ID: %s)", entity.title, GROUP_ID)
    except Exception as exc:
        log.error("❌ Cannot access group: %s", exc)

    target_desc = f"@{TARGET_BOT_USERNAME}" if TARGET_BOT_USERNAME else str(TARGET_BOT_ID or "None")
    asyncio.create_task(gemini_queue_worker())
    log.info("📡 Gemini: %s key(s) | Gap: %ss", len(GEMINI_KEYS), GEMINI_CALL_GAP)
    log.info("🎯 Target bot (userbot deletes only this): %s", target_desc)
    log.info("👤 Admin: %s", ADMIN_ID)

    # Run only the connected clients
    if userbot_connected:
        await asyncio.gather(
            bot_client.run_until_disconnected(),
            user_client.run_until_disconnected(),
        )
    else:
        await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
