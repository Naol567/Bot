"""
Forex Group Management Bot – Full Edition
- Bot token for normal moderation (Gemini, keywords, links, warnings, bans)
- User account client (login via bot) to delete specific bot's messages
- Admin commands: /filter, /login, /logout, /status
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

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Environment Variables ────────────────────────────────────────────────
API_ID     = int(os.environ["API_ID"])
API_HASH   = os.environ["API_HASH"]
BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])
GROUP_ID   = int(os.environ["GROUP_ID"])
GEMINI_KEYS = [k.strip() for k in os.environ["GEMINI_API_KEY"].split(",") if k.strip()]
TARGET_BOT_ID = int(os.environ.get("TARGET_BOT_ID", 0))  # Bot whose messages will be deleted

# ─── Gemini Setup (key rotation) ─────────────────────────────────────────
_current_key_index = 0

def get_gemini_model():
    genai.configure(api_key=GEMINI_KEYS[_current_key_index])
    return genai.GenerativeModel("gemini-2.0-flash")

def rotate_key():
    global _current_key_index
    next_index = (_current_key_index + 1) % len(GEMINI_KEYS)
    if next_index == 0 and len(GEMINI_KEYS) == 1:
        return False
    _current_key_index = next_index
    log.info("🔄 Rotated to Gemini key #%s", _current_key_index + 1)
    return True

# ─── Two Telegram Clients ────────────────────────────────────────────────
bot_client = TelegramClient("bot_session", API_ID, API_HASH)
user_client = None          # Will be created after login
user_client_ready = False
login_state = {}            # {admin_id: {"step": ..., "temp_client": ..., "phone": ...}}

# ─── In‑Memory Stores ────────────────────────────────────────────────────
warnings_db = {}
admin_state = {}

# ─── Banned Words (full list – English + Amharic + insults) ──────────────
banned_words = [
    # --- English signals / scam / recruitment ---
    "dm me for signals", "dm for signals", "i sell signals", "selling signals",
    "join my vip", "join our vip", "vip signals", "paid signals", "premium signals",
    "signal provider", "signal service", "buy signals",
    "join my group", "join our group", "join my channel", "join our channel",
    "subscribe to my channel", "click the link", "link in bio", "check my bio",
    "use my referral", "referral link", "use my link", "register with my link",
    "deposit via my link", "use my code", "promo code", "invite link",
    "guaranteed profit", "guaranteed return", "100% profit", "risk free", "risk-free",
    "no loss", "double your money", "i will manage your account", "managed account",
    "send me money", "send usdt", "send btc", "invest with me", "investment platform",
    "fund your account", "withdraw daily", "earn daily", "earn money online",
    "make money online", "passive income", "financial freedom",
    "account for sale", "selling account", "buying account", "broker account for sale",
    "ea for sale", "robot for sale", "trading bot for sale",
    "whatsapp me", "contact me on whatsapp", "dm me", "message me", "inbox me",
    "contact for promo", "available for hire", "hire me", "i offer services",
    "we offer services",
    # --- English insults (full) ---
    "you idiot", "you are stupid", "you are dumb", "you fool", "shut up", "go to hell",
    "son of a bitch", "motherfucker", "you loser", "stupid", "idiot", "dumb", "fool",
    "moron", "retard", "bastard", "bitch", "cunt", "dick", "pussy", "asshole",
    "fuck you", "suck my", "eat shit", "kill yourself", "worthless", "piece of shit",
    "dumbass", "dipshit",
    # --- Amharic signals / scam / insults ---
    "ሲግናል እሸጣለሁ", "ሲግናል እልካለሁ", "ሲግናል ይግዙ", "ሲግናል ይጠቀሙ", "ሲግናል ቡድን",
    "ዲኤም አድርጉ", "ዲኤም አድርጉኝ", "ለሲግናል ዲኤም", "ቪአይፒ ቡድን", "ቪአይፒ ይቀላቀሉ",
    "ሲግናል ለማግኘት", "ቡድኑን ይቀላቀሉ", "ቻናሉን ይቀላቀሉ", "ሊንኩን ይጫኑ", "ሊንክ ይጠቀሙ",
    "ሪፈራል ሊንክ", "ሊንኬን ተጠቀሙ", "ቻናሌን ተቀላቀሉ", "ቡድኔን ተቀላቀሉ", "ሊንኩን ተጫኑ",
    "ትርፍ እናረጋግጣለን", "ትርፍ ዋስትና", "መቶ ፐርሰንት ትርፍ", "ኪሳራ የለም", "ገንዘብ ይላኩ",
    "ዩኤስዲቲ ይላኩ", "ቢቲሲ ይላኩ", "ሂሳብዎን ያስተዳድሩ", "ሂሳብ ያስተዳድራለሁ", "ኢንቨስት ያድርጉ",
    "ኢንቨስትመንት", "ትርፍ ያግኙ", "ዕለታዊ ትርፍ", "ገንዘብ ያስቀምጡ", "ፈጣን ትርፍ", "ሀብት ይሁኑ",
    "አካውንት ይሸጣል", "አካውንት እሸጣለሁ", "አካውንት ለሽያጭ", "ሮቦት ለሽያጭ", "ኢኤ ለሽያጭ",
    "ዋትሳፕ ያግኙኝ", "ቴሌግራም ያግኙኝ", "ያናግሩኝ", "መልዕክት ይላኩልኝ",
    "ደደብ ነህ", "ደደብ ነሽ", "ሞኝ ነህ", "ሞኝ ነሽ", "ዝምበል", "ውሻ", "አህያ",
    "ጅል ነህ", "ጅል ነሽ", "ከንቱ", "ጊዜ ሌባ", "ፋይዳ የለህም", "ፋይዳ የለሽም",
    "ርኩስ", "ርኩስ ውሻ", "ዘባኝ", "ደም ጠጪ", "አጭበርባሪ", "ሐሰተኛ", "ክፉ", "ጠላት",
    "ዲያብሎስ", "ሰይጣን", "ቆሻሻ", "እንኳን አትሞት", "ልብህ ይበሰብስ", "ፊትህ ይጥላ",
]

# ─── Link Detection (all URLs) ───────────────────────────────────────────
LINK_PATTERN = re.compile(
    r'https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|bit\.ly/\S+|wa\.me/\S+|\b[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?\b',
    re.IGNORECASE
)
def contains_link(text: str) -> bool:
    return bool(LINK_PATTERN.search(text))

def keyword_is_banned(text: str):
    lower = text.lower()
    for word in banned_words:
        if word.lower() in lower:
            return word
    return None

# ─── Gemini Queue ────────────────────────────────────────────────────────
SUSPICIOUS_PATTERNS = [
    r"https?://", r"t\.me/", r"bit\.ly", r"wa\.me",
    r"\b(usdt|btc|eth|crypto|wallet|deposit|withdraw)\b",
    r"\b(profit|income|earn|money|invest|fund)\b",
    r"\b(vip|premium|paid|sell|buy|hire|promo|referral)\b",
    r"\b(channel|group|subscribe|follow|contact|whatsapp)\b",
    r"(ትርፍ|ገንዘብ|ሲግናል|ቡድን|ቻናል|ሊንክ|ኢንቨስት|አካውንት)",
]
_suspicious_re = re.compile("|".join(SUSPICIOUS_PATTERNS), re.IGNORECASE)

GEMINI_CALL_GAP = 10
MIN_WORDS_GEMINI = 5
_gemini_queue = asyncio.Queue()
_last_gemini_call = 0.0

def should_use_gemini(text: str) -> bool:
    if len(text.strip().split()) < MIN_WORDS_GEMINI:
        return False
    return bool(_suspicious_re.search(text))

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

async def queue_gemini_analysis(text: str) -> dict:
    if not should_use_gemini(text):
        return {"verdict": "ALLOWED", "reason": "Skipped – no suspicious pattern"}
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    await _gemini_queue.put((text, future))
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=300)
    except:
        return {"verdict": "ALLOWED", "reason": "Gemini timeout/error"}

SYSTEM_PROMPT = """You are the AI moderation engine for a professional Forex trading Telegram group.
Members write in BOTH English AND Amharic (አማርኛ). Analyse both languages equally.

✅ ALWAYS ALLOW:
- Forex/crypto trading: currency pairs, trade ideas, entries, exits, SL/TP
- Technical analysis: indicators, chart patterns, support/resistance
- Fundamental analysis: NFP, CPI, interest rates, central bank news
- Broker/platform talk: MT4, MT5, TradingView, cTrader
- Risk management, lot size, leverage, drawdown
- Market commentary, economic news in English or Amharic
- Educational content, trading psychology, friendly conversation
- P&L sharing, trade screenshots

❌ PROHIBITED (English or Amharic):
1. Paid signal ads or VIP group recruitment
2. Scams: guaranteed profit, wallet deposit requests, managed accounts
3. Recruiting to other channels/groups, referral links
4. Personal insults or hate speech
5. Completely off-topic spam/advertising

RULES:
- When in doubt → ALWAYS choose ALLOWED
- Missing a scam is better than banning a real trader

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
                log.warning("⚠️ Gemini key #%s quota exceeded (%s/%s)", _current_key_index+1, keys_tried, total_keys)
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
            log.warning("⚠️ Gemini non-quota error: %s", exc)
            return {"verdict": "ALLOWED", "reason": "Gemini error – safe default"}

# ─── Moderation Helpers ──────────────────────────────────────────────────
def get_warning_count(user_id: int) -> int:
    return warnings_db.get(user_id, {}).get("count", 0)

def record_violation(user_id: int, username: str, full_name: str, reason: str):
    if user_id in warnings_db:
        warnings_db[user_id]["count"] += 1
        warnings_db[user_id]["last_reason"] = reason
    else:
        warnings_db[user_id] = {
            "count": 1, "username": username,
            "full_name": full_name, "last_reason": reason,
        }
    log.info("📋 User %s → %s warning(s)", user_id, warnings_db[user_id]["count"])

async def delete_msg(client, chat_id: int, message_id: int):
    try:
        await client.delete_messages(chat_id, message_id)
        log.info("🗑️ Deleted msg %s", message_id)
    except Exception as e:
        log.warning("Delete failed: %s", e)

async def ban_user(chat_id: int, user_id: int):
    try:
        await bot_client(EditBannedRequest(
            channel=chat_id, participant=user_id,
            banned_rights=ChatBannedRights(until_date=None, view_messages=True)
        ))
        log.info("🔨 Banned user %s", user_id)
    except Exception as e:
        log.error("Ban failed: %s", e)

async def send_warning(event, reason: str, user_id: int, username: str, full_name: str):
    try:
        mention = f"@{username}" if username else f"[{full_name}](tg://user?id={user_id})"
        warning_msg = await event.respond(
            f"⚠️ **Warning / ማስጠንቀቂያ** — {mention}\n\n"
            f"🇬🇧 This is your **only warning**. Next violation = immediate ban.\n"
            f"🇪🇹 ይህ **የመጨረሻ ማስጠንቀቂያዎ** ነው። ደግመው ከጣሱ ወዲያውኑ ይታገዳሉ።\n\n"
            f"📋 **Reason:** {reason}",
            parse_mode="md"
        )
        asyncio.create_task(delete_msg(bot_client, event.chat_id, warning_msg.id))
    except Exception as e:
        log.warning("Warning send failed: %s", e)

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
    except Exception as e:
        log.error("Admin notify failed: %s", e)

# ─── Admin Keyword Filter UI ─────────────────────────────────────────────
def make_filter_keyboard():
    return [
        [Button.inline("➕ Add Word", b"filter_add"), Button.inline("➖ Remove Word", b"filter_remove")],
        [Button.inline("📋 Show All Words", b"filter_show")],
    ]

@bot_client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(
        "🤖 **Forex Group Bot — Admin Panel**\n\n"
        "**Commands:**\n"
        "/filter – Manage banned keywords\n"
        "/login – Log in with your user account (to delete target bot)\n"
        "/logout – Remove user account session\n"
        "/status – Show user client & target bot status\n\n"
        "**How it works:**\n"
        "• Links → instant delete + warning/ban\n"
        "• Keywords → instant delete\n"
        "• Suspicious messages → Gemini analysis\n"
        "• User account (after /login) deletes messages from target bot."
    )

@bot_client.on(events.NewMessage(pattern="/filter"))
async def cmd_filter(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    await event.reply(
        f"🔧 **Keyword Filter Panel**\n\nCurrently **{len(banned_words)}** banned word(s).",
        buttons=make_filter_keyboard()
    )

@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ Admin only.", alert=True)
        return
    data = event.data
    if data == b"filter_add":
        admin_state[ADMIN_ID] = "awaiting_add"
        await event.edit("➕ Send word/phrase to ban:", buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]])
    elif data == b"filter_remove":
        if not banned_words:
            await event.answer("No words in filter!", alert=True)
            return
        admin_state[ADMIN_ID] = "awaiting_remove"
        word_list = "\n".join(f"{i+1}. `{w}`" for i, w in enumerate(banned_words))
        await event.edit(f"➖ Send exact word to remove:\n{word_list}", buttons=[[Button.inline("❌ Cancel", b"filter_cancel")]])
    elif data == b"filter_show":
        if not banned_words:
            await event.answer("Filter list is empty!", alert=True)
            return
        word_list = "\n".join(f"• `{w}`" for w in banned_words)
        await event.edit(f"📋 **Banned Words ({len(banned_words)} total)**\n\n{word_list}", buttons=make_filter_keyboard())
    elif data == b"filter_cancel":
        admin_state.pop(ADMIN_ID, None)
        await event.edit(f"✅ Cancelled.\n\n🔧 **Filter Panel** — {len(banned_words)} word(s)", buttons=make_filter_keyboard())

@bot_client.on(events.NewMessage)
async def admin_text_handler(event):
    if event.sender_id != ADMIN_ID or event.is_group:
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
            await event.reply(f"❌ `{word}` not found in filter.", buttons=make_filter_keyboard())

# ─── User Account Login Flow (via bot commands) ──────────────────────────
@bot_client.on(events.NewMessage(pattern="/login"))
async def cmd_login(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    if user_client_ready:
        await event.reply("✅ User client is already logged in. Use /logout first if you want to change account.")
        return
    login_state[ADMIN_ID] = {"step": "phone"}
    await event.reply("📱 **Login to your Telegram user account**\n\nSend your phone number in international format (e.g., `+251912345678`).")

@bot_client.on(events.NewMessage(pattern="/logout"))
async def cmd_logout(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    global user_client, user_client_ready
    if user_client:
        try:
            await user_client.disconnect()
        except:
            pass
        user_client = None
    user_client_ready = False
    if os.path.exists("user_session.session"):
        os.remove("user_session.session")
    await event.reply("🔓 Logged out and session deleted. Use /login to log in again.")

@bot_client.on(events.NewMessage(pattern="/status"))
async def cmd_status(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    status = f"🤖 Bot client: active\n"
    status += f"👤 User client: {'✅ ready' if user_client_ready else '❌ not logged in'}\n"
    status += f"🎯 Target bot ID: {TARGET_BOT_ID if TARGET_BOT_ID else 'not set'}\n"
    if TARGET_BOT_ID and user_client_ready:
        status += f"📌 Will delete messages from bot ID {TARGET_BOT_ID}"
    else:
        status += f"⚠️ Set TARGET_BOT_ID environment variable and log in to enable deletion."
    await event.reply(status)

# Conversation handler for login steps
@bot_client.on(events.NewMessage)
async def login_conversation(event):
    if event.sender_id != ADMIN_ID or event.is_group:
        return
    if ADMIN_ID not in login_state:
        return
    state = login_state[ADMIN_ID]
    text = event.raw_text.strip()
    if state["step"] == "phone":
        state["phone"] = text
        state["step"] = "code"
        await event.reply(f"✅ Phone {text} received. Now send the **OTP code** you received on Telegram (or SMS).")
    elif state["step"] == "code":
        code = text
        state["step"] = "password"
        temp_client = TelegramClient("temp_session", API_ID, API_HASH)
        try:
            await temp_client.start(phone=state["phone"], code_callback=lambda: code)
            state["temp_client"] = temp_client
            state["step"] = "2fa"
            await event.reply("✅ Code accepted. If you have 2‑step verification (password), send it now. Otherwise send `skip`.")
        except Exception as e:
            await event.reply(f"❌ Invalid code. Error: {e}\nPlease restart /login.")
            login_state.pop(ADMIN_ID, None)
    elif state["step"] == "2fa":
        password = text if text.lower() != "skip" else None
        temp_client = state.get("temp_client")
        if not temp_client:
            await event.reply("❌ Session lost. Please /login again.")
            login_state.pop(ADMIN_ID, None)
            return
        try:
            if password:
                await temp_client.sign_in(password=password)
            me = await temp_client.get_me()
            await temp_client.disconnect()
            # Save session permanently
            final_client = TelegramClient("user_session", API_ID, API_HASH)
            await final_client.start(phone=state["phone"])
            await final_client.get_me()
            global user_client, user_client_ready
            user_client = final_client
            user_client_ready = True
            # Set up event handler for this user client (delete target bot messages)
            @user_client.on(events.NewMessage(chats=GROUP_ID))
            async def delete_target_bot(event):
                if TARGET_BOT_ID == 0:
                    return
                sender = await event.get_sender()
                if sender and sender.id == TARGET_BOT_ID:
                    log.info("🗑️ Deleting message from target bot %s", TARGET_BOT_ID)
                    await delete_msg(user_client, event.chat_id, event.id)
                    await bot_client.send_message(ADMIN_ID, f"🧹 Deleted a message from bot `{TARGET_BOT_ID}` at {datetime.utcnow()}")
            await event.reply(f"✅ **Successfully logged in as:** {me.first_name} (ID: {me.id})\n"
                              f"Now monitoring group for messages from bot ID {TARGET_BOT_ID} and deleting them instantly.")
            login_state.pop(ADMIN_ID, None)
        except Exception as e:
            await event.reply(f"❌ 2FA or login failed: {e}\nPlease /login again.")
            login_state.pop(ADMIN_ID, None)

# ─── Main Group Message Handler (Bot Client) ────────────────────────────
@bot_client.on(events.NewMessage(chats=GROUP_ID))
async def handle_group_message(event):
    if event.out:
        return
    sender = await event.get_sender()
    if sender is None or getattr(sender, "bot", False):
        return
    message_text = event.raw_text or ""
    if not message_text.strip():
        return

    user_id = sender.id
    username = getattr(sender, "username", "") or ""
    full_name = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or username or str(user_id)
    chat_id = event.chat_id

    log.info("📨 [%s | %s]: %s", full_name, user_id, message_text[:80])

    # Layer 1: Link detection
    if contains_link(message_text):
        violation_reason = "Message contains a link (URL) — all links are prohibited."
        log.info("🚫 Link detected")
    # Layer 2: Keyword filter
    elif (matched_word := keyword_is_banned(message_text)):
        violation_reason = f"Message contains banned word: '{matched_word}'"
        log.info("🚫 Keyword hit: '%s'", matched_word)
    else:
        result = await queue_gemini_analysis(message_text)
        if result["verdict"] != "PROHIBITED":
            return
        violation_reason = result["reason"]

    # Act on violation
    await delete_msg(bot_client, chat_id, event.id)
    prior = get_warning_count(user_id)

    if prior == 0:
        record_violation(user_id, username, full_name, violation_reason)
        await send_warning(event, violation_reason, user_id, username, full_name)
        log.info("⚠️ Warned %s", full_name)
    else:
        record_violation(user_id, username, full_name, violation_reason)
        await ban_user(chat_id, user_id)
        await notify_admin(user_id, username, full_name, message_text, violation_reason, "🔨 BANNED")
        log.info("🔨 Banned %s", full_name)

# ─── Entry Point ─────────────────────────────────────────────────────────
async def main():
    # Start bot client
    await bot_client.start(bot_token=BOT_TOKEN)
    me_bot = await bot_client.get_me()
    log.info("🤖 Bot client started: @%s", me_bot.username)

    # Try to load existing user client session from file
    global user_client, user_client_ready
    if os.path.exists("user_session.session"):
        try:
            user_client = TelegramClient("user_session", API_ID, API_HASH)
            await user_client.start()
            me = await user_client.get_me()
            user_client_ready = True
            log.info("👤 User client loaded from session: %s (ID: %s)", me.first_name, me.id)
            @user_client.on(events.NewMessage(chats=GROUP_ID))
            async def delete_target_bot(event):
                if TARGET_BOT_ID == 0:
                    return
                sender = await event.get_sender()
                if sender and sender.id == TARGET_BOT_ID:
                    log.info("🗑️ Deleting message from target bot %s", TARGET_BOT_ID)
                    await delete_msg(user_client, event.chat_id, event.id)
                    await bot_client.send_message(ADMIN_ID, f"🧹 Deleted a message from bot `{TARGET_BOT_ID}` at {datetime.utcnow()}")
        except Exception as e:
            log.warning("Could not load existing user session: %s", e)
            user_client = None
            user_client_ready = False

    # Verify group access
    try:
        entity = await bot_client.get_entity(GROUP_ID)
        log.info("✅ Monitoring group: %s (ID: %s)", entity.title, GROUP_ID)
    except Exception as e:
        log.error("❌ Cannot access group %s: %s", GROUP_ID, e)

    asyncio.create_task(gemini_queue_worker())
    log.info("📡 Gemini: ready | %s key(s)", len(GEMINI_KEYS))
    log.info("👤 Admin: %s | /filter, /login, /status", ADMIN_ID)
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
