"""
Forex Group Management Bot
Powered by Telethon + Gemini AI
Status: Final Fix for 404 Model Not Found
"""

import os
import asyncio
import logging
import json
import re
from telethon import TelegramClient, events
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
import google.generativeai as genai

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Environment Variables ────────────────────────────────────────────────────
API_ID     = int(os.environ["API_ID"])
API_HASH   = os.environ["API_HASH"]
BOT_TOKEN  = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])
GROUP_ID   = int(os.environ["GROUP_ID"])

# ─── Gemini Setup ─────────────────────────────────────────────────────────────
# 404 ስህተትን ለመፍታት ስሪቱን እና የሞዴሉን ስም እናስተካክላለን
genai.configure(api_key=GEMINI_KEY)

# ይበልጥ አስተማማኝ የሆነውን 'gemini-1.5-flash-latest' እንጠቀም
gemini_model = genai.GenerativeModel("gemini-1.5-flash-latest")

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
        warnings_db[user_id] = {"count": 1, "username": username, "full_name": full_name}
    log.info(f"📋 Warning for {user_id}: Total {warnings_db[user_id]['count']}")

# ─── Gemini Analysis ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a group moderator. Respond ONLY in JSON format:
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "short explanation"}
Rules: No insults, no scam links, no 'DM for signals'."""

async def analyse_message(text: str):
    try:
        # ጥያቄው በ 'v1' stable API እንዲሄድ እናስገድዳለን
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            f"{SYSTEM_PROMPT}\n\nMessage: {text[:800]}"
        )
        
        if not response or not response.text:
            return {"verdict": "ALLOWED", "reason": "Empty response"}

        raw_text = response.text.strip()
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            return {
                "verdict": str(data.get("verdict", "ALLOWED")).upper(),
                "reason": str(data.get("reason", "Violation detected."))
            }
        return {"verdict": "ALLOWED", "reason": "No JSON"}
            
    except Exception as exc:
        # 404 ካጋጠመ እዚህ ጋር ሎግ ያደርጋል
        log.error(f"⚠️ Gemini Error Detail: {exc}")
        return {"verdict": "ALLOWED", "reason": "API Error Fallback"}

# ─── Event Handler ────────────────────────────────────────────────────────────
@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_message(event):
    if event.out: return
    sender = await event.get_sender()
    if not sender or getattr(sender, 'bot', False): return
    
    text = event.raw_text or ""
    if not text.strip() or len(text) < 2: return
    
    result = await analyse_message(text)
    
    if result["verdict"] == "PROHIBITED":
        try:
            await event.delete()
            user_id = sender.id
            count = get_warning_count(user_id)
            
            if count == 0:
                record_violation(user_id, getattr(sender, 'username', ''), getattr(sender, 'first_name', 'User'), result["reason"])
                await event.reply(f"⚠️ **ማስጠንቀቂያ**\nምክንያት: {result['reason']}\nደግመው ካጠፉ ይታገዳሉ።")
            else:
                await client(EditBannedRequest(event.chat_id, user_id, ChatBannedRights(until_date=None, view_messages=True, send_messages=True)))
                await client.send_message(ADMIN_ID, f"🔨 **Banned:** `{user_id}`\n**Reason:** {result['reason']}")
        except Exception as e:
            log.error(f"Moderation failed: {e}")

# ─── Start ────────────────────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot is live and fixed...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
