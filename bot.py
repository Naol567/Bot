"""
Forex Group Management Bot — Dual Client (Bot + Userbot)
─────────────────────────────────────────────────────────
- Bot client: Spam filter + Keyword filter + Gemini AI
- Userbot:   ONLY deletes messages from a specific target bot
- Persistent warnings (SQLite)
"""

import os
import asyncio
import logging
import json
import re
import pathlib
import sqlite3
from datetime import datetime

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

# ─── Target Bot (userbot will delete EVERY message from this bot) ─────────────
TARGET_BOT_USERNAME = os.environ.get("TARGET_BOT_USERNAME")  # without @
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"]) if os.environ.get("TARGET_BOT_ID") else None

# ─── Spam Detection Settings ──────────────────────────────────────────────────
SPAM_CAPS_RATIO = 0.5        # If >50% caps, mark as spam
SPAM_REPEAT_CHARS = 5        # Repeated same char >5 times (e.g., "!!!!!")
SPAM_MAX_PUNCTUATION = 0.3   # If >30% punctuation/emoji, mark as spam
SPAM_MIN_WORD_LEN = 2        # Ignore very short words
SPAM_REPEATED_WORDS = 3      # Same word repeated 3+ times (e.g., "spam spam spam")

# Custom spam phrases (expanded list)
SPAM_PHRASES = [
    # Original
    "experience", "camin",
    # Signal selling / VIP groups
    "dm for signals", "dm me for signals", "signal dm", "vip group", "paid signals",
    "premium signals", "signal service", "buy signals", "sell signals", "signals channel",
    "signals group", "vip signals", "exclusive signals", "signals provider", "signal master",
    "forex signals", "crypto signals",
    # Recruitment / invite to other groups
    "join my group", "join our group", "join my channel", "join our channel",
    "subscribe to my channel", "subscribe to our channel", "link in bio", "check my bio",
    "referral link", "use my link", "invite link", "new group", "new channel",
    "click the link", "click here", "follow me", "follow us",
    # Scams / guaranteed profit
    "guaranteed profit", "guaranteed returns", "100% profit", "risk free", "risk-free",
    "no loss", "double your money", "managed account", "send me money", "send usdt",
    "send btc", "invest with me", "investment platform", "fund your account",
    "withdraw daily", "earn daily", "earn money online", "make money online",
    "passive income", "financial freedom", "wealth academy", "rich quick",
    "get rich", "millionaire",
    # Account selling / EA / robots
    "account for sale", "selling account", "buying account", "ea for sale",
    "robot for sale", "trading bot for sale", "forex robot", "crypto robot",
    "autopilot", "copy trade", "copy trading",
    # Contact solicitation
    "whatsapp me", "contact me on whatsapp", "dm me", "message me", "inbox me",
    "contact for promo", "available for hire", "hire me", "i offer services",
    "we offer services", "telegram me", "pm me",
    # Common obfuscations (spaces, dots, etc.)
    "d m f o r s i g n a l s", "v i p   g r o u p", "f r e e   m o n e y",
    "g u a r a n t e e d", "w i t h d r a w", "d e p o s i t",
    # Amharic additional
    "ሲግናል ሸጭ", "ቪፒ ቡድን", "ነጻ ገንዘብ", "ፈጣን ሀብት", "ሂሳብ ሽያጭ",
]

# Compile regex for spam phrases (case-insensitive)
_spam_phrase_re = re.compile(r'\b(?:' + '|'.join(re.escape(p) for p in SPAM_PHRASES) + r')\b', re.IGNORECASE)

# ─── Gemini Setup — Key Rotation ──────────────────────────────────────────────
_raw_keys   = os.environ["GEMINI_API_KEY"]
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

# ─── Helper: Ensure session directory exists ─────────────────────────────────
def ensure_session_dir(session_path: str) -> str:
    if not session_path:
        session_path = "user_instance.session"
    path = pathlib.Path(session_path)
    parent = path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        log.info("📁 Created session directory: %s", parent)
    return str(path)

# ─── Persistent Warnings (SQLite) ────────────────────────────────────────────
DB_PATH = "warnings.db"

def init_warnings_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS warnings (
        user_id INTEGER PRIMARY KEY,
        count INTEGER DEFAULT 0,
        username TEXT,
        full_name TEXT,
        last_reason TEXT,
        updated_at TEXT
    )''')
    conn.commit()
    conn.close()

def get_warning_count(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM warnings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def record_violation(user_id: int, username: str, full_name: str, reason: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO warnings (user_id, count, username, full_name, last_reason, updated_at)
        VALUES (?, 1, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            count = count + 1,
            username = excluded.username,
            full_name = excluded.full_name,
            last_reason = excluded.last_reason,
            updated_at = excluded.updated_at
    ''', (user_id, username, full_name, reason, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    log.info("📋 User %s → %s warning(s)", user_id, get_warning_count(user_id))

# ─── Spam Detection Function ─────────────────────────────────────────────────
def is_spam(text: str) -> tuple:
    """
    Returns (is_spam, reason) based on various heuristics.
    """
    if not text:
        return (False, "")

    text_lower = text.lower()
    words = text.split()
    word_count = len(words)

    # 1. Spam phrases
    if _spam_phrase_re.search(text_lower):
        return (True, "Contains spam phrase")

    # 2. Excessive caps (only if message has letters)
    letters = sum(1 for c in text if c.isalpha())
    if letters > 0:
        caps = sum(1 for c in text if c.isupper())
        caps_ratio = caps / letters
        if caps_ratio > SPAM_CAPS_RATIO and len(text) > 10:
            return (True, f"Excessive capital letters ({caps_ratio*100:.0f}%)")

    # 3. Repeated characters (e.g., "!!!!!", "loooool")
    if re.search(r'(.)\1{' + str(SPAM_REPEAT_CHARS) + r',}', text):
        return (True, "Repeated characters detected")

    # 4. High punctuation/emoji ratio
    punct_emojis = sum(1 for c in text if not c.isalnum() and not c.isspace())
    if len(text) > 0 and punct_emojis / len(text) > SPAM_MAX_PUNCTUATION:
        return (True, "Too many punctuation marks or emojis")

    # 5. Repeated words (e.g., "spam spam spam")
    if word_count >= SPAM_REPEATED_WORDS:
        from collections import Counter
        word_counts = Counter(words)
        for w, cnt in word_counts.items():
            if cnt >= SPAM_REPEATED_WORDS and len(w) > SPAM_MIN_WORD_LEN:
                return (True, f"Word '{w}' repeated {cnt} times")

    # 6. Garbage / random keyboard smashing (e.g., "asdf asdf asdf")
    if word_count >= 3:
        garbage_count = 0
        for w in words:
            if len(w) >= 4 and not re.search(r'[aeiouAEIOU]', w):
                garbage_count += 1
        if garbage_count / word_count > 0.5:
            return (True, "Random keyboard spam")

    return (False, "")

# ─── Telethon Clients ─────────────────────────────────────────────────────────
bot_client = TelegramClient("bot_session", API_ID, API_HASH)
USER_SESSION_RAW = os.environ.get("USER_SESSION_PATH", "/data/user_instance.session")
USER_SESSION = ensure_session_dir(USER_SESSION_RAW)
user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)

# ─── Userbot Login State ──────────────────────────────────────────────────────
connect_state: dict = {}
userbot_connected: bool = False

# ─── In-Memory Stores (for filter flow) ──────────────────────────────────────
admin_state: dict = {}

# ─── Pre-loaded Banned Words (English + Amharic) ──────────────────────────────
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
    "you idiot", "you are stupid", "you are dumb", "you fool", "shut up", "go to hell",
    "son of a bitch", "motherfucker", "you loser",
    # Amharic
    "ሲግናል እሸጣለሁ", "ሲግናል እልካለሁ", "ሲግናል ይግዙ", "ሲግናል ይጠቀሙ", "ሲግናል ቡድን",
    "ዲኤም አድርጉ", "ዲኤም አድርጉኝ", "ለሲግናል ዲኤም", "ቪአይፒ ቡድን", "ቪአይፒ ይቀላቀሉ",
    "ሲግናል ለማግኘት", "ቡድኑን ይቀላቀሉ", "ቻናሉን ይቀላቀሉ", "ሊንኩን ይጫኑ", "ሊንክ ይጠቀሙ",
    "ሪፈራል ሊንክ", "ሊንኬን ተጠቀሙ", "ቻናሌን ተቀላቀሉ", "ቡድኔን ተቀላቀሉ", "ሊንኩን ተጫኑ",
    "ትርፍ እናረጋግጣለን", "ትርፍ ዋስትና", "መቶ ፐርሰንት ትርፍ", "ኪሳራ የለም", "ገንዘብ ይላኩ",
    "ዩኤስዲቲ ይላኩ", "ቢቲሲ ይላኩ", "ሂሳብዎን ያስተዳድሩ", "ሂሳብ ያስተዳድራለሁ", "ኢንቨስት ያድርጉ",
    "ኢንቨስትመንት", "ትርፍ ያግኙ", "ዕለታዊ ትርፍ", "ገንዘብ ያስቀምጡ", "ፈጣን ትርፍ", "ሀብት ይሁኑ",
    "አካውንት ይሸጣል", "አካውንት እሸጣለሁ", "አካውንት ለሽያጭ", "ሮቦት ለሽያጭ", "ኢኤ ለሽያጭ",
    "ዋትሳፕ ያግኙኝ", "ቴሌግራም ያግኙኝ", "ያናግሩኝ", "መልዕክት ይላኩልኝ",
    "ደደብ ነህ", "ደደብ ነሽ", "ሞኝ ነህ", "ሞኝ ነሽ", "ዝምበል", "ውሻ", "አህያ", "ጅል ነህ", "ጅል ነሽ",
    "ከንቱ", "ጊዜ ሌባ", "ፋይዳ የለህም", "ፋይዳ የለሽም"
]

# ─── Suspicious patterns that trigger Gemini ──────────────────────────────────
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
GEMINI_CALL_GAP  = 10
MIN_WORDS_GEMINI = 5

_gemini_queue: asyncio.Queue = asyncio.Queue()
_last_gemini_call: float = 0.0

async def gemini_queue_worker():
    global _last_gemini_call
    while True:
        text, future = await _gemini_queue.get()
        try:
            now = asyncio.get_event_loop().time()
            gap = GEMINI_CALL_GAP - (now - _last_gemini_call)
            if gap > 0:
                await asyncio.sleep(gap)
            result = await _call_gemini(text)
            _last_gemini_call = asyncio.get_event_loop().time()
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
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    await _gemini_queue.put((text, future))
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=300)
    except asyncio.TimeoutError:
        return {"verdict": "ALLOWED", "reason": "Timeout."}
    except Exception:
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
            reason  = str(data.get("reason", "No reason."))
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
                    await asyncio.sleep(retry_wait)
                    keys_tried = 0
                continue
            return {"verdict": "ALLOWED", "reason": "Gemini error."}

# ─── Helper Functions ─────────────────────────────────────────────────────────
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
    except Exception as e:
        log.error(f"Ban failed: {e}")

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
        async def delete_warning_later():
            await asyncio.sleep(300)
            await delete_msg(event.chat_id, warning_msg.id)
        asyncio.create_task(delete_warning_later())
    except Exception:
        pass

async def notify_admin(user_id, username, full_name, text, reason, action):
    try:
        tag = f"@{username}" if username else f"ID:{user_id}"
        msg = (
            f"🚫 **Moderation Report**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"**Action:** {action}\n"
            f"**User:** {full_name} ({tag})\n"
            f"**ID:** `{user_id}`\n\n"
            f"**Message:**\n```\n{text[:500]}\n```\n\n"
            f"**Reason:** {reason}\n"
            f"**Time:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        await bot_client.send_message(ADMIN_ID, msg, parse_mode="md")
    except Exception:
        pass

def keyword_is_banned(text: str):
    lower = text.lower()
    for word in banned_words:
        if word.lower() in lower:
            return word
    return None

def make_filter_keyboard():
    return [
        [Button.inline("➕ Add Word", b"filter_add"),
         Button.inline("➖ Remove Word", b"filter_remove")],
        [Button.inline("📋 Show All Words", b"filter_show")],
    ]

# ═══════════════════════════════════════════════════════════════════════════════
# BOT CLIENT HANDLERS (Admin commands)
# ═══════════════════════════════════════════════════════════════════════════════

@bot_client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    status = "✅ Connected" if userbot_connected else "❌ Not connected (use /connect)"
    await event.reply(
        "🤖 **Forex Group Bot — Admin Panel**\n\n"
        f"👤 **Userbot status:** {status}\n\n"
        "**Commands:**\n"
        "/connect — Login your personal account as userbot\n"
        "/filter  — Manage banned keywords\n\n"
        "**How it works:**\n"
        "1️⃣ Spam filter (caps, repeats, phrases)\n"
        "2️⃣ Keyword filter — instant, no API\n"
        "3️⃣ Suspicious messages → Gemini AI queue\n"
        "4️⃣ Userbot deletes ONLY the target bot's messages\n"
        "5️⃣ 1st offence → warning (deleted after 5min)\n"
        "6️⃣ 2nd offence → permanent ban + private report\n\n"
        "✅ English & Amharic | 🔄 Multi-key Gemini rotation"
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
    session_file = USER_SESSION + ".session" if not USER_SESSION.endswith(".session") else USER_SESSION
    if os.path.exists(session_file):
        await event.reply("🔄 Session file found. Trying to reconnect...")
        try:
            await user_client.connect()
            if await user_client.is_user_authorized():
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot reconnected as {me.first_name} (@{me.username or 'no username'})")
                return
        except Exception:
            pass
    connect_state[ADMIN_ID] = {"step": "phone"}
    await event.reply("📱 Send your phone number with country code (e.g., +251912345678)")

@bot_client.on(events.NewMessage(pattern="/cancel"))
async def cmd_cancel(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    connect_state.pop(ADMIN_ID, None)
    admin_state.pop(ADMIN_ID, None)
    await event.reply("✅ Cancelled.")

@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ Admin only.", alert=True)
        return
    data = event.data
    try:
        if data == b"filter_add":
            admin_state[ADMIN_ID] = "awaiting_add"
            await event.edit("➕ Send the word/phrase to ban.", buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]])
        elif data == b"filter_remove":
            if not banned_words:
                await event.answer("No words in filter!", alert=True)
                return
            admin_state[ADMIN_ID] = "awaiting_remove"
            word_list = "\n".join(f"{i+1}. `{w}`" for i, w in enumerate(banned_words))
            await event.edit(f"➖ Current words:\n{word_list}\n\nSend the exact word to remove:", buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]])
        elif data == b"filter_show":
            if not banned_words:
                await event.answer("Filter list is empty!", alert=True)
                return
            word_list = "\n".join(f"• `{w}`" for w in banned_words)
            await event.edit(f"📋 Banned Words ({len(banned_words)} total)\n\n{word_list}", buttons=make_filter_keyboard())
        elif data == b"filter_cancel":
            admin_state.pop(ADMIN_ID, None)
            await event.edit(f"✅ Cancelled. — {len(banned_words)} word(s) active", buttons=make_filter_keyboard())
    except MessageNotModifiedError:
        # Ignore: the message content is already the same
        log.debug("Edit skipped: message content unchanged")
    except Exception as e:
        log.error(f"Error in callback_handler: {e}")
        await event.answer("An error occurred.", alert=True)

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
            phone = text
            conn["phone"] = phone
            try:
                await user_client.connect()
                result = await user_client.send_code_request(phone)
                conn["phone_code_hash"] = result.phone_code_hash
                conn["step"] = "code"
                await event.reply("📩 Code sent. Send it with spaces (e.g., 1 2 3 4 5)")
            except FloodWaitError as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"⚠️ Flood wait {e.seconds}s")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ {e}")
            return
        if step == "code":
            code = text.replace(" ", "")
            phone = conn["phone"]
            phone_code_hash = conn["phone_code_hash"]
            try:
                await user_client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                connect_state.pop(ADMIN_ID, None)
                global userbot_connected
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot connected as {me.first_name} (@{me.username or 'no username'})")
            except SessionPasswordNeededError:
                conn["step"] = "password"
                await event.reply("🔐 2FA enabled. Send your password:")
            except PhoneCodeInvalidError:
                await event.reply("❌ Wrong code. Use /connect to restart.")
            except Exception as e:
                connect_state.pop(ADMIN_ID, None)
                await event.reply(f"❌ {e}")
            return
        if step == "password":
            password = text
            try:
                await user_client.sign_in(password=password)
                connect_state.pop(ADMIN_ID, None)
                userbot_connected = True
                me = await user_client.get_me()
                await event.reply(f"✅ Userbot connected (2FA) as {me.first_name}")
            except Exception as e:
                await event.reply(f"❌ Wrong password: {e}")
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
            await event.reply(f"✅ Added `{word}`\nTotal: {len(banned_words)}", buttons=make_filter_keyboard())
    elif state == "awaiting_remove":
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            banned_words.remove(word)
            await event.reply(f"✅ Removed `{word}`\nTotal: {len(banned_words)}", buttons=make_filter_keyboard())
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
    # Skip: bot itself, human admin
    if sender.id in (me_bot.id, ADMIN_ID):
        return
    # Skip userbot if connected
    if userbot_connected:
        me_user = await user_client.get_me()
        if sender.id == me_user.id:
            return

    message_text = event.raw_text or ""
    if not message_text.strip():
        return

    user_id = sender.id
    username = getattr(sender, "username", "") or ""
    full_name = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or username or str(user_id)
    chat_id = event.chat_id
    is_bot = getattr(sender, "bot", False)

    # ── Layer 0: Spam Detection (runs first) ────────────────────────────────
    spam_flag, spam_reason = is_spam(message_text)
    if spam_flag:
        violation_reason = f"Spam detected: {spam_reason}"
        log.info(f"🚫 Spam detected from {full_name}: {spam_reason}")
        await delete_msg(chat_id, event.id)
        if not is_bot:
            prior = get_warning_count(user_id)
            if prior == 0:
                record_violation(user_id, username, full_name, violation_reason)
                await send_warning(event, violation_reason, user_id, username, full_name)
            else:
                record_violation(user_id, username, full_name, violation_reason)
                await ban_user(chat_id, user_id)
                await notify_admin(user_id, username, full_name, message_text, violation_reason, "BANNED")
        else:
            log.info(f"🤖 Bot spam deleted (no warning): {full_name}")
        return

    # ── Layer 1: Keyword filter ────────────────────────────────────────────
    matched_word = keyword_is_banned(message_text)
    if matched_word:
        violation_reason = f"Banned word: '{matched_word}'"
        log.info(f"🚫 Keyword hit from {full_name}: {matched_word}")
        await delete_msg(chat_id, event.id)
        if not is_bot:
            prior = get_warning_count(user_id)
            if prior == 0:
                record_violation(user_id, username, full_name, violation_reason)
                await send_warning(event, violation_reason, user_id, username, full_name)
            else:
                record_violation(user_id, username, full_name, violation_reason)
                await ban_user(chat_id, user_id)
                await notify_admin(user_id, username, full_name, message_text, violation_reason, "BANNED")
        else:
            log.info(f"🤖 Bot keyword deleted (no warning): {full_name}")
        return

    # ── Layer 2: Gemini AI (only if suspicious) ────────────────────────────
    result = await queue_gemini_analysis(message_text)
    if result["verdict"] == "PROHIBITED":
        violation_reason = result["reason"]
        log.info(f"🤖 Gemini prohibited from {full_name}: {violation_reason}")
        await delete_msg(chat_id, event.id)
        if not is_bot:
            prior = get_warning_count(user_id)
            if prior == 0:
                record_violation(user_id, username, full_name, violation_reason)
                await send_warning(event, violation_reason, user_id, username, full_name)
            else:
                record_violation(user_id, username, full_name, violation_reason)
                await ban_user(chat_id, user_id)
                await notify_admin(user_id, username, full_name, message_text, violation_reason, "BANNED")
        else:
            log.info(f"🤖 Bot Gemini deleted (no warning): {full_name}")
        return

    # If we reach here, message is allowed
    log.info(f"✅ Allowed: {full_name}")

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

    # Never delete messages from the userbot itself or the human admin
    me_user = await user_client.get_me()
    if sender.id in (me_user.id, ADMIN_ID):
        return

    # Check if this is the target bot (by username or ID)
    is_target = False
    username = getattr(sender, "username", "") or ""
    if TARGET_BOT_USERNAME and username.lower() == TARGET_BOT_USERNAME.lower():
        is_target = True
    if TARGET_BOT_ID and sender.id == TARGET_BOT_ID:
        is_target = True

    if is_target:
        try:
            await user_client.delete_messages(event.chat_id, event.id)
            log.info(f"🎯 [USERBOT] Deleted target bot message from {username or sender.id}")
        except Exception as e:
            log.warning(f"Failed to delete target bot message: {e}")
    # else: do absolutely nothing (no link deletion, no mention deletion, no other bot deletion)

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    global userbot_connected
    init_warnings_db()
    await bot_client.start(bot_token=BOT_TOKEN)
    log.info(f"🤖 Bot started: {(await bot_client.get_me()).username}")
    # Try to load userbot session
    try:
        os.makedirs(os.path.dirname(USER_SESSION) if os.path.dirname(USER_SESSION) else ".", exist_ok=True)
        await user_client.connect()
        if await user_client.is_user_authorized():
            userbot_connected = True
            me = await user_client.get_me()
            log.info(f"✅ Userbot loaded: {me.first_name} (@{me.username or 'no username'})")
        else:
            log.info("⚠️ Userbot not logged in. Send /connect to your bot.")
    except Exception as e:
        log.warning(f"Userbot connect failed: {e}")
    # Verify group access
    try:
        entity = await bot_client.get_entity(GROUP_ID)
        log.info(f"✅ Monitoring: {entity.title} (ID: {GROUP_ID})")
    except Exception as e:
        log.error(f"Cannot access group: {e}")
    asyncio.create_task(gemini_queue_worker())
    log.info(f"📡 Gemini: {len(GEMINI_KEYS)} key(s) | Gap: {GEMINI_CALL_GAP}s")
    target_desc = TARGET_BOT_USERNAME or TARGET_BOT_ID or "None"
    log.info(f"🎯 Userbot will ONLY delete messages from target bot: {target_desc}")
    log.info(f"👤 Admin: {ADMIN_ID} | /connect to login userbot | /filter for keywords")
    if userbot_connected:
        await asyncio.gather(
            bot_client.run_until_disconnected(),
            user_client.run_until_disconnected(),
        )
    else:
        await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
