"""
Forex Group Management Bot
- Gemini AI analysis
- Admin keyword filter with inline buttons
- English + Amharic support
- Railway deployment
"""

import os
import asyncio
import logging
import json
import re
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
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

# ─── Gemini Setup — Key Rotation ──────────────────────────────────────────────
# GEMINI_API_KEY can be one key or multiple comma-separated keys:
# e.g. AIzaSyKEY1,AIzaSyKEY2,AIzaSyKEY3
_raw_keys = os.environ["GEMINI_API_KEY"]
GEMINI_KEYS: list = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_current_key_index = 0

def get_gemini_model() -> genai.GenerativeModel:
    """Return a Gemini model using the current active key."""
    genai.configure(api_key=GEMINI_KEYS[_current_key_index])
    return genai.GenerativeModel("gemini-2.0-flash")

def rotate_key() -> bool:
    """Rotate to the next API key. Returns True if rotated, False if exhausted all keys."""
    global _current_key_index
    next_index = (_current_key_index + 1) % len(GEMINI_KEYS)
    if next_index == 0 and len(GEMINI_KEYS) == 1:
        return False  # only one key, can't rotate
    _current_key_index = next_index
    log.info("🔄 Rotated to Gemini key #%s", _current_key_index + 1)
    return True

# ─── Telethon Client ──────────────────────────────────────────────────────────
client = TelegramClient("bot_session", API_ID, API_HASH)

# ─── In-Memory Stores ─────────────────────────────────────────────────────────
warnings_db: dict = {}
admin_state: dict = {}   # { ADMIN_ID: "awaiting_add" | "awaiting_remove" }

# ─── Pre-loaded Banned Words (English + Amharic) ──────────────────────────────
# Admin can add/remove more via /filter command in bot DM
banned_words: list = [

    # ── ENGLISH: Signal selling / VIP promotion ───────────────────────────
    "dm me for signals",
    "dm for signals",
    "i sell signals",
    "selling signals",
    "join my vip",
    "join our vip",
    "vip signals",
    "paid signals",
    "premium signals",
    "signal provider",
    "signal service",
    "buy signals",
    "my signals",

    # ── ENGLISH: Recruitment / invite spam ────────────────────────────────
    "join my group",
    "join our group",
    "join my channel",
    "join our channel",
    "subscribe to my channel",
    "click the link",
    "link in bio",
    "check my bio",
    "use my referral",
    "referral link",
    "use my link",
    "register with my link",
    "deposit via my link",
    "use my code",
    "promo code",
    "invite link",

    # ── ENGLISH: Scam / guaranteed profit ─────────────────────────────────
    "guaranteed profit",
    "guaranteed return",
    "100% profit",
    "risk free",
    "risk-free",
    "no loss",
    "double your money",
    "i will manage your account",
    "managed account",
    "send me money",
    "send usdt",
    "send btc",
    "invest with me",
    "investment platform",
    "fund your account",
    "withdraw daily",
    "earn daily",
    "earn money online",
    "make money online",
    "passive income",
    "financial freedom",

    # ── ENGLISH: Account selling ───────────────────────────────────────────
    "account for sale",
    "selling account",
    "buying account",
    "broker account for sale",
    "ea for sale",
    "robot for sale",
    "trading bot for sale",

    # ── ENGLISH: Contact solicitation ─────────────────────────────────────
    "whatsapp me",
    "contact me on whatsapp",
    "dm me",
    "message me",
    "inbox me",
    "contact for promo",
    "available for hire",
    "hire me",
    "i offer services",
    "we offer services",

    # ── ENGLISH: Personal insults ─────────────────────────────────────────
    "you idiot",
    "you are stupid",
    "you are dumb",
    "you fool",
    "shut up",
    "go to hell",
    "son of a bitch",
    "motherfucker",
    "you loser",
    "you are a scammer",

    # ── AMHARIC: Signal selling / VIP promotion ───────────────────────────
    "ሲግናል እሸጣለሁ",
    "ሲግናል እልካለሁ",
    "ሲግናል ይግዙ",
    "ሲግናል ይጠቀሙ",
    "ሲግናል ቡድን",
    "ዲኤም አድርጉ",
    "ዲኤም አድርጉኝ",
    "ለሲግናል ዲኤም",
    "ቪአይፒ ቡድን",
    "ቪአይፒ ይቀላቀሉ",
    "ሲግናል ለማግኘት",

    # ── AMHARIC: Recruitment / invite ─────────────────────────────────────
    "ቡድኑን ይቀላቀሉ",
    "ቻናሉን ይቀላቀሉ",
    "ሊንኩን ይጫኑ",
    "ሊንክ ይጠቀሙ",
    "ሪፈራል ሊንክ",
    "ሊንኬን ተጠቀሙ",
    "ቻናሌን ተቀላቀሉ",
    "ቡድኔን ተቀላቀሉ",
    "ሊንኩን ተጫኑ",

    # ── AMHARIC: Scam / guaranteed profit ─────────────────────────────────
    "ትርፍ እናረጋግጣለን",
    "ትርፍ ዋስትና",
    "መቶ ፐርሰንት ትርፍ",
    "ኪሳራ የለም",
    "ገንዘብ ይላኩ",
    "ዩኤስዲቲ ይላኩ",
    "ቢቲሲ ይላኩ",
    "ሂሳብዎን ያስተዳድሩ",
    "ሂሳብ ያስተዳድራለሁ",
    "ኢንቨስት ያድርጉ",
    "ኢንቨስትመንት",
    "ትርፍ ያግኙ",
    "ዕለታዊ ትርፍ",
    "ገንዘብ ያስቀምጡ",
    "ፈጣን ትርፍ",
    "ሀብት ይሁኑ",

    # ── AMHARIC: Account selling ───────────────────────────────────────────
    "አካውንት ይሸጣል",
    "አካውንት እሸጣለሁ",
    "አካውንት ለሽያጭ",
    "ሮቦት ለሽያጭ",
    "ኢኤ ለሽያጭ",

    # ── AMHARIC: Contact solicitation ─────────────────────────────────────
    "ዋትሳፕ ያግኙኝ",
    "ቴሌግራም ያግኙኝ",
    "ያናግሩኝ",
    "መልዕክት ይላኩልኝ",

    # ── AMHARIC: Personal insults ─────────────────────────────────────────
    "ደደብ ነህ",
    "ደደብ ነሽ",
    "ሞኝ ነህ",
    "ሞኝ ነሽ",
    "ዝምበል",
    "ውሻ",
    "አህያ",
    "ጅል ነህ",
    "ጅል ነሽ",
    "ከንቱ",
    "ጊዜ ሌባ",
    "ፋይዳ የለህም",
    "ፋይዳ የለሽም",
]

# ─── Gemini Rate Limiter + Queue ─────────────────────────────────────────────
# Limits Gemini to max 10 requests/minute per key (safe under free tier 15 RPM)
# Messages queue up and are processed one at a time — keyword filter is unaffected

GEMINI_RPM_LIMIT   = 10          # max requests per minute (conservative)
GEMINI_MIN_GAP     = 60 / GEMINI_RPM_LIMIT  # seconds between requests = 6s
SKIP_SHORT_WORDS   = 4           # skip Gemini for messages under this many words

_gemini_queue: asyncio.Queue = asyncio.Queue()
_last_gemini_call: float = 0.0


async def gemini_queue_worker():
    """
    Background worker that processes Gemini requests one at a time
    with a minimum gap between calls to respect RPM limits.
    """
    global _last_gemini_call
    while True:
        text, future = await _gemini_queue.get()
        try:
            # Enforce minimum gap between requests
            now = asyncio.get_event_loop().time()
            gap = GEMINI_MIN_GAP - (now - _last_gemini_call)
            if gap > 0:
                await asyncio.sleep(gap)

            result = await analyse_with_gemini(text)
            _last_gemini_call = asyncio.get_event_loop().time()
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)
        finally:
            _gemini_queue.task_done()


async def queue_gemini_analysis(text: str) -> dict:
    """
    Submit a message to the Gemini queue and wait for result.
    Short messages (under SKIP_SHORT_WORDS words) are skipped entirely.
    """
    word_count = len(text.strip().split())
    if word_count < SKIP_SHORT_WORDS:
        log.info("⏭️ Skipping Gemini for short message (%s words)", word_count)
        return {"verdict": "ALLOWED", "reason": "Short message skipped."}

    loop = asyncio.get_event_loop()
    future = loop.create_future()
    await _gemini_queue.put((text, future))
    log.info("📥 Queued for Gemini (queue size: %s)", _gemini_queue.qsize())
    try:
        return await asyncio.wait_for(future, timeout=180)  # 3 min max wait
    except asyncio.TimeoutError:
        log.warning("⏰ Gemini queue timeout — defaulting ALLOWED")
        return {"verdict": "ALLOWED", "reason": "Gemini timeout — safe default."}
    except Exception as exc:
        log.warning("⚠️ Queue error: %s — defaulting ALLOWED", exc)
        return {"verdict": "ALLOWED", "reason": "Queue error — safe default."}


# ─── Warning Helpers ──────────────────────────────────────────────────────────

def get_warning_count(user_id: int) -> int:
    return warnings_db.get(user_id, {}).get("count", 0)


def record_violation(user_id: int, username: str, full_name: str, reason: str):
    if user_id in warnings_db:
        warnings_db[user_id]["count"] += 1
        warnings_db[user_id]["last_reason"] = reason
    else:
        warnings_db[user_id] = {
            "count": 1,
            "username": username,
            "full_name": full_name,
            "last_reason": reason,
        }
    log.info("📋 User %s → %s warning(s)", user_id, warnings_db[user_id]["count"])

# ─── Keyword Filter ───────────────────────────────────────────────────────────

def keyword_is_banned(text: str):
    lower = text.lower()
    for word in banned_words:
        if word.lower() in lower:
            return word
    return None

# ─── Gemini Analysis ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the AI moderation engine for a professional Forex trading Telegram group.

Members write in BOTH English AND Amharic (አማርኛ). Analyse both languages equally.

✅ ALWAYS ALLOW:
- Forex, crypto, currency pairs (EUR/USD, XAU/USD, GBP/JPY, indices, commodities)
- Trade ideas, entries/exits, stop loss, take profit, signals discussion
- Technical analysis: indicators, chart patterns, support/resistance, candlesticks
- Fundamental analysis: NFP, CPI, interest rates, central bank news
- Broker/platform talk: MT4, MT5, TradingView, cTrader
- Risk management, lot size, leverage, drawdown
- Market commentary, economic news in English or Amharic
- Educational content, trading psychology
- Friendly member conversation
- P&L sharing, trade screenshots

❌ PROHIBITED (English or Amharic):
1. Paid signal ads: "DM for signals", "join my VIP", "ሲግናል እሸጣለሁ"
2. Scams: "guaranteed profit", "ትርፍ እናረጋግጣለን", wallet addresses for deposits
3. Recruiting to other channels or groups
4. Referral or affiliate links
5. Personal insults or hate speech
6. Completely off-topic spam

RULES:
- When in doubt → ALLOWED
- Missing a scam is better than banning a real trader

Respond ONLY with valid JSON, no markdown:
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "one sentence in English"}"""


async def analyse_with_gemini(text: str) -> dict:
    """
    Sends message to Gemini with key rotation + retry on 429 quota errors.
    Tries every available key before giving up.
    Always fails SAFE -> ALLOWED.
    """
    prompt = f"{SYSTEM_PROMPT}\n\nMessage:\n---\n{text[:2000]}\n---"
    keys_tried = 0
    total_keys = len(GEMINI_KEYS)

    while keys_tried < total_keys:
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

            # 429 quota exceeded — rotate key and retry
            if "429" in err_str or "quota" in err_str.lower() or "RESOURCE_EXHAUSTED" in err_str:
                log.warning("⚠️ Gemini key #%s quota exceeded. Rotating...", _current_key_index + 1)
                keys_tried += 1
                rotated = rotate_key()

                if not rotated or keys_tried >= total_keys:
                    # All keys exhausted — wait for retry_delay if mentioned in error
                    retry_wait = 60  # default
                    match = re.search(r"retry.*?(\d+)", err_str, re.IGNORECASE)
                    if match:
                        retry_wait = min(int(match.group(1)), 120)  # cap at 2 min
                    log.warning("⏳ All %s Gemini key(s) exhausted. Waiting %ss then retrying...", total_keys, retry_wait)
                    await asyncio.sleep(retry_wait)
                    keys_tried = 0  # reset and try all keys once more after wait
                continue

            # Other error (network, parse, etc.) — log and fail safe
            log.warning("⚠️ Gemini error: %s", exc)
            return {"verdict": "ALLOWED", "reason": "Gemini error — safe default."}

    log.warning("⚠️ All Gemini keys failed — defaulting ALLOWED")
    return {"verdict": "ALLOWED", "reason": "All Gemini keys exhausted — keyword filter still active."}

# ─── Moderation Actions ───────────────────────────────────────────────────────

async def delete_msg(chat_id: int, message_id: int):
    try:
        await client.delete_messages(chat_id, message_id)
        log.info("🗑️ Deleted msg %s", message_id)
    except Exception as exc:
        log.warning("Delete failed: %s", exc)


async def ban_user(chat_id: int, user_id: int):
    try:
        await client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(until_date=None, view_messages=True)
        ))
        log.info("🔨 Banned user %s", user_id)
    except Exception as exc:
        log.error("Ban failed for %s: %s", user_id, exc)


async def send_warning(event, reason: str, user_id: int, username: str, full_name: str):
    try:
        # Mention user by @username or inline mention if no username
        if username:
            mention = f"@{username}"
        else:
            mention = f"[{full_name}](tg://user?id={user_id})"

        warning_msg = await event.respond(
            f"⚠️ **Warning / ማስጠንቀቂያ** — {mention}\n\n"
            f"🇬🇧 This is your **only warning**. Next violation = immediate ban.\n"
            f"🇪🇹 ይህ **የመጨረሻ ማስጠንቀቂያዎ** ነው። ደግመው ከጣሱ ወዲያውኑ ይታገዳሉ።\n\n"
            f"📋 **Reason:** {reason}",
            parse_mode="md"
        )
        log.info("⚠️ Warning sent to %s, auto-delete in 5min", user_id)

        async def delete_warning_later():
            await asyncio.sleep(300)
            await delete_msg(event.chat_id, warning_msg.id)
            log.info("🗑️ Warning auto-deleted (msg_id=%s)", warning_msg.id)

        asyncio.create_task(delete_warning_later())

    except Exception as exc:
        log.warning("Warning send failed: %s", exc)


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
        await client.send_message(ADMIN_ID, msg, parse_mode="md")
    except Exception as exc:
        log.error("Admin notify failed: %s", exc)

# ─── Filter Keyboard ──────────────────────────────────────────────────────────

def make_filter_keyboard():
    return [
        [Button.inline("➕ Add Word", b"filter_add"),
         Button.inline("➖ Remove Word", b"filter_remove")],
        [Button.inline("📋 Show All Words", b"filter_show")],
    ]

# ─── Admin Commands (private DM only) ────────────────────────────────────────

@client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    # Only respond to admin in private
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(
        "🤖 **Forex Group Bot — Admin Panel**\n\n"
        "**Commands:**\n"
        "/filter — Manage banned keywords\n\n"
        "**How it works:**\n"
        "1️⃣ Every message checked against your keyword list\n"
        "2️⃣ If no keyword match → Gemini AI analyses it\n"
        "3️⃣ Violation → message deleted\n"
        "4️⃣ 1st offence → public warning in group\n"
        "5️⃣ 2nd offence → ban + private report to you\n\n"
        "✅ Supports English & Amharic"
    )


@client.on(events.NewMessage(pattern="/filter"))
async def cmd_filter(event):
    # Only respond to admin in private
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    count = len(banned_words)
    await event.reply(
        f"🔧 **Keyword Filter Panel**\n\n"
        f"Currently **{count}** banned word(s).\n"
        f"Bot deletes any message containing these words instantly.\n\n"
        f"Choose an action:",
        buttons=make_filter_keyboard()
    )

# ─── Callback Query Handler (NO from_users — check manually inside) ───────────

@client.on(events.CallbackQuery)
async def callback_handler(event):
    # Only admin can use buttons
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ Admin only.", alert=True)
        return

    data = event.data

    if data == b"filter_add":
        admin_state[ADMIN_ID] = "awaiting_add"
        await event.edit(
            "➕ **Add Banned Word**\n\n"
            "Send me the word or phrase you want to ban.\n"
            "Example: `dm me` or `guaranteed profit` or `ሲግናል`",
            buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]]
        )

    elif data == b"filter_remove":
        if not banned_words:
            await event.answer("No words in filter yet!", alert=True)
            return
        admin_state[ADMIN_ID] = "awaiting_remove"
        word_list = "\n".join(f"{i+1}. `{w}`" for i, w in enumerate(banned_words))
        await event.edit(
            f"➖ **Remove Banned Word**\n\n"
            f"Current banned words:\n{word_list}\n\n"
            f"Send me the exact word to remove:",
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
        count = len(banned_words)
        await event.edit(
            f"✅ Cancelled.\n\n"
            f"🔧 **Keyword Filter Panel**\n"
            f"Currently **{count}** banned word(s).",
            buttons=make_filter_keyboard()
        )

# ─── Admin Text Handler (for add/remove input) ────────────────────────────────

@client.on(events.NewMessage)
async def admin_text_handler(event):
    # Only in private DM with admin, not a command, and admin is in a state
    if event.sender_id != ADMIN_ID:
        return
    if event.is_group:
        return
    if event.text and event.text.startswith("/"):
        return

    state = admin_state.get(ADMIN_ID)
    if not state:
        return

    word = (event.raw_text or "").strip().lower()
    if not word:
        return

    if state == "awaiting_add":
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            await event.reply(
                f"⚠️ `{word}` is already in the filter list.",
                buttons=make_filter_keyboard()
            )
        else:
            banned_words.append(word)
            await event.reply(
                f"✅ **Added:** `{word}`\n"
                f"Total banned words: **{len(banned_words)}**",
                buttons=make_filter_keyboard()
            )
        log.info("🔧 Admin added banned word: '%s'", word)

    elif state == "awaiting_remove":
        admin_state.pop(ADMIN_ID, None)
        if word in banned_words:
            banned_words.remove(word)
            await event.reply(
                f"✅ **Removed:** `{word}`\n"
                f"Total banned words: **{len(banned_words)}**",
                buttons=make_filter_keyboard()
            )
            log.info("🔧 Admin removed banned word: '%s'", word)
        else:
            word_list = ", ".join(f"`{w}`" for w in banned_words) or "none"
            await event.reply(
                f"❌ `{word}` not found in filter.\n\n"
                f"Current words: {word_list}",
                buttons=make_filter_keyboard()
            )

# ─── Main Group Message Handler ───────────────────────────────────────────────

@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_group_message(event):
    if event.out:
        return
    sender = await event.get_sender()
    if sender is None or getattr(sender, "bot", False):
        return
    message_text = event.raw_text or ""
    if not message_text.strip():
        return

    user_id   = sender.id
    username  = getattr(sender, "username", "") or ""
    full_name = " ".join(filter(None, [
        getattr(sender, "first_name", ""),
        getattr(sender, "last_name", ""),
    ])) or username or str(user_id)
    chat_id = event.chat_id

    log.info("📨 [%s | %s]: %s", full_name, user_id, message_text[:80])

    # ── Layer 1: keyword filter (instant) ─────────────────────────────────
    matched_word = keyword_is_banned(message_text)
    if matched_word:
        violation_reason = f"Message contains banned word: '{matched_word}'"
        log.info("🚫 Keyword hit: '%s'", matched_word)
    else:
        # ── Layer 2: Gemini AI ─────────────────────────────────────────────
        result = await queue_gemini_analysis(message_text)
        if result["verdict"] != "PROHIBITED":
            return  # clean — stay silent
        violation_reason = result["reason"]

    # ── Act on violation ──────────────────────────────────────────────────
    await delete_msg(chat_id, event.id)
    prior = get_warning_count(user_id)

    if prior == 0:
        record_violation(user_id, username, full_name, violation_reason)
        await send_warning(event, violation_reason, user_id, username, full_name)
        log.info("⚠️ Warned %s (%s)", full_name, user_id)
    else:
        record_violation(user_id, username, full_name, violation_reason)
        await ban_user(chat_id, user_id)
        await notify_admin(
            user_id, username, full_name,
            message_text, violation_reason, "🔨 BANNED"
        )
        log.info("🔨 Banned %s (%s)", full_name, user_id)

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info("🤖 Bot: @%s (ID: %s)", me.username, me.id)

    try:
        entity = await client.get_entity(GROUP_ID)
        log.info("✅ Monitoring: %s (ID: %s)", entity.title, GROUP_ID)
    except Exception as exc:
        log.error("❌ Cannot access group %s: %s", GROUP_ID, exc)

    # Start Gemini queue worker
    asyncio.create_task(gemini_queue_worker())
    log.info("📡 Gemini: gemini-2.0-flash | RPM limit: %s req/min | Queue: active", GEMINI_RPM_LIMIT)
    log.info("⏭️ Short messages under %s words skip Gemini", SKIP_SHORT_WORDS)
    log.info("👤 Admin: %s", ADMIN_ID)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
