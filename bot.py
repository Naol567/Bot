"""
Forex Group Management Bot
Powered by Telethon + Gemini AI
Supports: English + Amharic
Status: Stable Version (Fixes ImportError & 404)
"""

import os
import asyncio
import logging
import json
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
# አስተማማኙን ሞዴል ስም እንጠቀማለን
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ─── Telethon Client ──────────────────────────────────────────────────────────
client = TelegramClient("bot_session", API_ID, API_HASH)

# ─── In-Memory Warning Store ──────────────────────────────────────────────────
# Railway ላይ ቮልዩም ስለሌለ ዳታው በሜሞሪ ይያዛል
warnings_db = {}

def get_warning_count(user_id):
    return warnings_db.get(user_id, {}).get("count", 0)

def record_violation(user_id, username, full_name, reason):
    if user_id in warnings_db:
        warnings_db[user_id]["count"] += 1
    else:
        warnings_db[user_id] = {
            "count": 1, 
            "username": username, 
            "full_name": full_name,
            "reason": reason
        }
    log.info("📋 Warning recorded for %s. Total: %s", user_id, warnings_db[user_id]["count"])

# ─── Gemini Analysis ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional Forex group moderator. 
Analyze messages in English and Amharic.

RULES:
1. ALLOW: Greetings (ሰላም, እንዴት ናችሁ), general trading talk, charts, and polite discussion.
2. PROHIBITED: Insults (ስድብ), "DM for signals", VIP links, scams, and spam.

Response MUST be valid JSON:
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "one short sentence"}"""

async def analyse_message(text: str):
    try:
        # በ thread ውስጥ መጥራቱ አንዳንዴ በ async ውስጥ የሚፈጠርን የሞዴል ስህተት ይቀንሳል
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            f"{SYSTEM_PROMPT}\n\nMessage: {text[:1000]}"
        )
        
        raw = response.text.strip()
        # Clean JSON markdown if present
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
            
        data = json.loads(raw)
        return {
            "verdict": str(data.get("verdict", "ALLOWED")).upper(),
            "reason": str(data.get("reason", "Violation detected."))
        }
    except Exception as exc:
        log.warning("⚠️ Gemini API Error: %s. Defaulting to ALLOWED.", exc)
        return {"verdict": "ALLOWED", "reason": "System safe mode."}

# ─── Moderation Actions ───────────────────────────────────────────────────────
async def ban_user(chat_id, user_id):
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
                embed_links=True
            )
        ))
        log.info("🔨 Banned user %s", user_id)
    except Exception as exc:
        log.error("❌ Ban failed for %s: %s", user_id, exc)

async def notify_admin(user_id, full_name, text, reason):
    try:
        report = (
            f"🔨 **User Banned**\n"
            f"👤 **Name:** {full_name}\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"📝 **Message:** {text[:300]}\n"
            f"🚫 **Reason:** {reason}"
        )
        await client.send_message(ADMIN_ID, report)
    except Exception as exc:
        log.error("❌ Admin notify failed: %s", exc)

# ─── Main Handler ────────────────────────────────────────────────────────────
@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_message(event):
    if event.out: return
    sender = await event.get_sender()
    if not sender or getattr(sender, 'bot', False): return
    
    text = event.raw_text or ""
    if not text.strip(): return
    
    result = await analyse_message(text)
    
    if result["verdict"] == "PROHIBITED":
        # Delete offending message
        await event.delete()
        
        user_id = sender.id
        full_name = getattr(sender, 'first_name', 'User')
        username = getattr(sender, 'username', '')
        count = get_warning_count(user_id)
        
        if count == 0:
            record_violation(user_id, username, full_name, result["reason"])
            await event.reply(
                f"⚠️ **ማስጠንቀቂያ / Warning**\n\n"
                f"ምክንያት: {result['reason']}\n\n"
                f"ይህ የመጀመሪያ ማስጠንቀቂያዎ ነው። ደግመው ካጠፉ ይታገዳሉ።"
            )
        else:
            await ban_user(event.chat_id, user_id)
            await notify_admin(user_id, full_name, text, result["reason"])

# ─── Start Bot ────────────────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot is live and monitoring group %s...", GROUP_ID)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
