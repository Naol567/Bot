"""
Forex Group Management Bot
Powered by Telethon + New Gemini SDK (google-genai)
Supports: English + Amharic
Status: Fully Adjusted for Railway (In-memory storage)
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
# አዲሱ ላይብረሪ 404 ስህተትን ለመከላከል
from google import genai

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

# ─── Gemini Setup (New SDK) ───────────────────────────────────────────────────
# የድሮው genai.configure አሰራር በአዲሱ client_ai ተተክቷል
client_ai = genai.Client(api_key=GEMINI_KEY)

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
SYSTEM_PROMPT = """You are a professional Telegram moderator for a Forex group.
Language: Amharic and English.

POLICIES:
1. ALLOW: 
   - Friendly chat, greetings (ሰላም, እንዴት ናችሁ), and general conversation.
   - Forex analysis, chart sharing, trading questions.
   - Genuine debates about brokers or strategies.

2. PROHIBITED:
   - INSULTS: Personal attacks, hate speech, or rude language in Amharic/English.
   - SPAM: "DM me for signals", VIP group invites, Referral links.
   - SCAMS: Asking for money or promising guaranteed profits.

3. BIO/NAME CHECK:
   - If user profile is pure advertising (e.g. "I sell accounts"), mark PROHIBITED.

Response Format (Strict JSON):
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "short explanation"}"""

async def analyse_message(text: str) -> dict:
    try:
        # በአዲሱ SDK መሠረት የተስተካከለ የጥሪ ዘዴ
        response = client_ai.models.generate_content(
            model="gemini-1.5-flash",
            contents=f"{SYSTEM_PROMPT}\n\nMessage to analyze: {text[:1500]}"
        )
        
        raw = response.text.strip()
        # JSON Clean-up (Markdown ካለ ለማጥፋት)
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
            
        data = json.loads(raw)
        return {
            "verdict": str(data.get("verdict", "ALLOWED")).upper(),
            "reason": str(data.get("reason", "Policy check failed."))
        }
    except Exception as exc:
        log.warning("⚠️ Gemini Error (404/API): %s - Defaulting to ALLOWED", exc)
        return {"verdict": "ALLOWED", "reason": "System bypass."}

# ─── Moderation Actions ───────────────────────────────────────────────────────
async def delete_message(chat_id: int, message_id: int):
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception as exc:
        log.error("Delete failed: %s", exc)

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
                embed_links=True
            )
        ))
    except Exception as exc:
        log.error("Ban failed: %s", exc)

async def notify_admin(user_id, username, full_name, text, reason):
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
        log.error("Admin report failed: %s", exc)

# ─── Event Handler ────────────────────────────────────────────────────────────
@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_message(event):
    if event.out: return
    sender = await event.get_sender()
    if not sender or getattr(sender, 'bot', False): return
    
    text = event.raw_text or ""
    if not text.strip(): return
    
    # Analyze with Gemini
    result = await analyse_message(text)
    
    if result["verdict"] == "PROHIBITED":
        await delete_message(event.chat_id, event.id)
        user_id = sender.id
        count = get_warning_count(user_id)
        
        full_name = getattr(sender, 'first_name', 'User')
        username = getattr(sender, 'username', '')

        if count == 0:
            record_violation(user_id, username, full_name, result["reason"])
            await event.reply(
                f"⚠️ **ማስጠንቀቂያ / Warning**\n\n"
                f"ምክንያት: {result['reason']}\n\n"
                f"ይህ የመጨረሻ ማስጠንቀቂያዎ ነው። ደግመው ካጠፉ ይታገዳሉ።"
            )
        else:
            await ban_user(event.chat_id, user_id)
            await notify_admin(user_id, username, full_name, text, result["reason"])

# ─── Execution ────────────────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot is live with NEW Gemini SDK (google-genai)!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
