"""
Forex Group Management Bot
Powered by Telethon + Gemini AI
Supports: English + Amharic
Status: Final Stable Version (Improved JSON Parsing)
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
# አስተማማኙን የሞዴል ጥሪ ዘዴ እንጠቀም
gemini_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash"
)

# ─── Telethon Client ──────────────────────────────────────────────────────────
client = TelegramClient("bot_session", API_ID, API_HASH)

# ─── In-Memory Warning Store ──────────────────────────────────────────────────
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
SYSTEM_PROMPT = """You are a smart moderator. Analyze the message for a Forex group.
- ALLOW: Greetings (ሰላም, እንዴት ናችሁ), Forex discussion, charts, help requests.
- PROHIBITED: Insults (English/Amharic), DM for signals, Spam links, Scams.

Response MUST be ONLY a JSON object:
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "short explanation"}"""

async def analyse_message(text: str):
    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            f"{SYSTEM_PROMPT}\n\nMessage: {text[:1000]}"
        )
        
        raw_text = response.text.strip()
        
        # JSON ለማግኘት ይበልጥ አስተማማኝ የሆነ Regex ዘዴ
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            json_str = match.group(0)
            data = json.loads(json_str)
            return {
                "verdict": str(data.get("verdict", "ALLOWED")).upper(),
                "reason": str(data.get("reason", "Violation detected."))
            }
        else:
            log.warning("⚠️ No JSON found in Gemini response: %s", raw_text)
            return {"verdict": "ALLOWED", "reason": "Parsing failed."}
            
    except Exception as exc:
        log.warning("⚠️ Gemini API Error: %s. Defaulting to ALLOWED.", exc)
        return {"verdict": "ALLOWED", "reason": "Safe mode bypass."}

# ─── Event Handler ────────────────────────────────────────────────────────────
@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_message(event):
    if event.out: return
    sender = await event.get_sender()
    if not sender or getattr(sender, 'bot', False): return
    
    text = event.raw_text or ""
    if not text.strip(): return
    
    result = await analyse_message(text)
    
    if result["verdict"] == "PROHIBITED":
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
            try:
                await client(EditBannedRequest(
                    channel=event.chat_id,
                    participant=user_id,
                    banned_rights=ChatBannedRights(until_date=None, view_messages=True, send_messages=True)
                ))
                await client.send_message(ADMIN_ID, f"🔨 **Banned:** {full_name} (`{user_id}`)\n**Reason:** {result['reason']}")
            except Exception as e:
                log.error("Ban error: %s", e)

# ─── Start Bot ────────────────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot is live and monitoring group %s...", GROUP_ID)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
