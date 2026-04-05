"""
Forex Group Management Bot
Powered by Telethon + Gemini AI
Supports: English + Amharic
Deployed on Railway (In-memory storage)
"""

import os
import asyncio
import logging
import json
import re
from datetime import datetime

from telethon import TelegramClient, events
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
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])
GROUP_ID   = int(os.environ["GROUP_ID"])

# ─── Gemini Setup ─────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel("gemini-pro")

# ─── Telethon Client ──────────────────────────────────────────────────────────
client = TelegramClient("bot_session", API_ID, API_HASH)

# ─── In-Memory Warning Store ──────────────────────────────────────────────────
warnings_db: dict = {}

def get_warning_count(user_id: int) -> int:
    return warnings_db.get(user_id, {}).get("count", 0)

def record_violation(user_id: int, username: str, full_name: str, reason: str):
    if user_id in warnings_db:
        warnings_db[user_id]["count"] += 1
    else:
        warnings_db[user_id] = {
            "count": 1,
            "username": username,
            "full_name": full_name,
            "last_reason": reason,
        }
    log.info("📋 User %s warning count: %s", user_id, warnings_db[user_id]["count"])

# ─── Gemini Analysis Logic ────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a smart Telegram moderator for a Forex trading group.
Members communicate in Amharic and English.

GOAL: Protect the group from SPAM and INSULTS, but ALLOW normal chatting.

1. ALLOW:
   - Friendly greetings and general chat (e.g., "እንዴት ናችሁ", "How is everyone?", "ሰላም").
   - Normal conversations, jokes, and discussions between members.
   - Forex analysis, trade ideas, charts, and broker questions.
   - Genuine debates about trading.
   - Asking for help or sharing knowledge.

2. PROHIBITED:
   - INSULTS: Any offensive language, hate speech, or personal attacks (English/Amharic).
   - SPAM: "DM me for signals", VIP groups, Selling accounts, or random ad links.
   - SCAMS: Guaranteed profits or asking for money.

3. BIO CHECK:
   - If the user's name or bio is explicitly "I sell signals" or "Contact for investment", mark as PROHIBITED.

Be fair. Don't ban people for just talking or saying hi.
Respond ONLY with valid JSON:
{
  "verdict": "ALLOWED" or "PROHIBITED",
  "reason": "one short sentence explaining why"
}"""

async def analyse_message(text: str) -> dict:
    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            f"{SYSTEM_PROMPT}\n\nMessage: {text[:1500]}"
        )
        
        raw = response.text.strip()
        # Clean Markdown if Gemini returns it
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
            
        data = json.loads(raw)
        return {
            "verdict": str(data.get("verdict", "ALLOWED")).upper(),
            "reason": str(data.get("reason", "Policy violation detected."))
        }
    except Exception as exc:
        log.warning("⚠️ Gemini Analysis error: %s - defaulting to ALLOWED", exc)
        return {"verdict": "ALLOWED", "reason": "System check bypassed."}

# ─── Moderation Actions ───────────────────────────────────────────────────────
async def delete_message(chat_id: int, message_id: int):
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception as exc:
        log.error("Delete error: %s", exc)

async def ban_user(chat_id: int, user_id: int):
    try:
        await client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(
                until_date=None,
                view_messages=True,
                send_messages=True,
                send_media=True,
                send_stickers=True,
                send_gifs=True,
                send_games=True,
                send_inline=True,
                embed_links=True
            )
        ))
    except Exception as exc:
        log.error("Ban error for user %s: %s", user_id, exc)

async def send_warning(event, reason: str):
    try:
        await event.reply(
            f"⚠️ **Warning / ማስጠንቀቂያ**\n\n"
            f"🇬🇧 Only one warning is allowed. Next time is a permanent ban.\n"
            f"🇪🇹 አንድ ጊዜ ብቻ ነው የሚመከሩት። ደግመው ካጠፉ ለዘላለም ይታገዳሉ።\n\n"
            f"📋 **Reason:** {reason}"
        )
    except Exception as exc:
        log.warning("Warning message failed: %s", exc)

async def notify_admin(user_id, username, full_name, offending_text, reason):
    try:
        report = (
            f"🔨 **User Banned**\n"
            f"👤 **Name:** {full_name}\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"🏷️ **Username:** @{username if username else 'N/A'}\n"
            f"📝 **Message:** {offending_text[:500]}\n"
            f"🚫 **Reason:** {reason}"
        )
        await client.send_message(ADMIN_ID, report)
    except Exception as exc:
        log.error("Admin notification failed: %s", exc)

# ─── Event Handler ────────────────────────────────────────────────────────────
@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_message(event):
    if event.out: return
    sender = await event.get_sender()
    if not sender or getattr(sender, 'bot', False): return
    
    text = event.raw_text or ""
    if not text.strip(): return
    
    user_id = sender.id
    
    # Gemini Analysis
    result = await analyse_message(text)
    
    if result["verdict"] == "PROHIBITED":
        await delete_message(event.chat_id, event.id)
        
        count = get_warning_count(user_id)
        full_name = getattr(sender, 'first_name', 'User')
        username = getattr(sender, 'username', '')

        if count == 0:
            record_violation(user_id, username, full_name, result["reason"])
            await send_warning(event, result["reason"])
        else:
            await ban_user(event.chat_id, user_id)
            await notify_admin(user_id, username, full_name, text, result["reason"])

# ─── Execution ────────────────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot is live and monitoring...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
