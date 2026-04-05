"""
Forex Group Management Bot
Powered by Telethon + OpenAI (GPT-4o mini)
Status: Highly Stable Version (Fixes 404 & ImportError)
"""

import os
import asyncio
import logging
import json
from telethon import TelegramClient, events
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from openai import AsyncOpenAI

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Environment Variables ────────────────────────────────────────────────────
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
BOT_TOKEN      = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ADMIN_ID       = int(os.environ["ADMIN_ID"])
GROUP_ID       = int(os.environ["GROUP_ID"])

# ─── OpenAI Client ────────────────────────────────────────────────────────────
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ─── Telethon Client ──────────────────────────────────────────────────────────
client = TelegramClient("bot_session", API_ID, API_HASH)

# ─── In-Memory Warning Database ───────────────────────────────────────────────
warnings_db = {}

def get_warning_count(user_id):
    return warnings_db.get(user_id, {}).get("count", 0)

def record_violation(user_id, username, full_name, reason):
    if user_id in warnings_db:
        warnings_db[user_id]["count"] += 1
    else:
        warnings_db[user_id] = {"count": 1, "username": username, "full_name": full_name}
    log.info(f"📋 Warning for {user_id}: Total {warnings_db[user_id]['count']}")

# ─── AI Analysis ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional Forex group moderator. 
Analyze messages in English and Amharic.
Rules: Prohibit insults, scam links, 'DM for signals', and off-topic ads.
Allow: Greetings, Forex charts, and polite trading questions.

Response MUST be ONLY a JSON object:
{"verdict": "ALLOWED" or "PROHIBITED", "reason": "Short explanation in Amharic"}"""

async def analyse_message(text: str):
    try:
        response = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Message: {text[:800]}"}
            ],
            response_format={ "type": "json_object" },
            timeout=10.0
        )
        data = json.loads(response.choices[0].message.content)
        return {
            "verdict": str(data.get("verdict", "ALLOWED")).upper(),
            "reason": str(data.get("reason", "የደንብ መጣስ ታይቷል።"))
        }
    except Exception as exc:
        log.error(f"⚠️ OpenAI Error: {exc}")
        return {"verdict": "ALLOWED", "reason": "System bypass."}

# ─── Main Handler ─────────────────────────────────────────────────────────────
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
            log.error(f"Action failed: {e}")

# ─── Start ────────────────────────────────────────────────────────────────────
async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot is live with OpenAI GPT-4o mini!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
