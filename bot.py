"""
Forex Group Management Bot
Powered by Telethon + Gemini AI
Supports: English + Amharic
Deployed on Railway
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

# GROUP_ID: your group/channel ID (with -100 prefix for supergroups)
# Example: -1001234567890
# Get it by forwarding a message from your group to @userinfobot
GROUP_ID   = int(os.environ["GROUP_ID"])

# ─── Gemini Setup ─────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ─── Telethon Client ──────────────────────────────────────────────────────────
client = TelegramClient("bot_session", API_ID, API_HASH)

# ─── In-Memory Warning Store ──────────────────────────────────────────────────
# { user_id: { "count": int, "username": str, "full_name": str, "last_reason": str } }
warnings_db: dict = {}


def get_warning_count(user_id: int) -> int:
    return warnings_db.get(user_id, {}).get("count", 0)


def record_violation(user_id: int, username: str, full_name: str, reason: str):
    if user_id in warnings_db:
        warnings_db[user_id]["count"] += 1
        warnings_db[user_id]["username"] = username
        warnings_db[user_id]["full_name"] = full_name
        warnings_db[user_id]["last_reason"] = reason
    else:
        warnings_db[user_id] = {
            "count": 1,
            "username": username,
            "full_name": full_name,
            "last_reason": reason,
        }
    log.info("📋 User %s now has %s warning(s)", user_id, warnings_db[user_id]["count"])

# ─── Gemini Analysis ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the AI moderation engine for a professional Forex trading Telegram group.

IMPORTANT: Members write messages in BOTH English AND Amharic (Ethiopian language). 
You MUST analyse and understand messages written in Amharic script (ግዕዝ/አማርኛ) exactly the same as English.
Never ignore or auto-allow a message just because it is written in Amharic.

Your job: read the message and decide ALLOWED or PROHIBITED.

════════════════════════════════════════════
THIS IS A FOREX TRADING GROUP — context matters.
════════════════════════════════════════════

✅ ALWAYS ALLOW:
- Forex/crypto trading: currency pairs (EUR/USD, GBP/JPY, XAU/USD, GOLD, indices)
- Trade ideas, entries, exits, stop loss, take profit
- Technical analysis: support/resistance, indicators (RSI, MACD, EMA, Fibonacci, etc.)
- Fundamental analysis: NFP, CPI, interest rates, central bank news
- Chart sharing, broker screenshots, P&L results
- Broker/platform questions: MT4, MT5, cTrader, TradingView
- Risk management: lot size, leverage, drawdown
- Market commentary in English OR Amharic
- Educational trading content in English OR Amharic
- Friendly conversation between members
- Criticism of brokers or services in genuine discussion

❌ PROHIBITED (in English OR Amharic):
1. SPAM & PROMOTION:
   - "DM me for signals" / "ሲግናል እልካለሁ ዲኤም አድርጉ"
   - Paid signal advertising / VIP group recruitment
   - Referral links, invite links to other channels
   - Selling/buying accounts or software

2. FINANCIAL SCAMS:
   - "Guaranteed profit" / "ትርፍ እናረጋግጣለን"
   - Asking to send money, USDT, crypto to any address
   - Managed account offers to strangers
   - Fake investment platforms

3. PERSONAL ATTACKS:
   - Direct insults in English or Amharic
   - Hate speech, threats, racism

4. COMPLETELY OFF-TOPIC SPAM:
   - Unrelated advertising with zero trading context

════════════════════════════════════════════
RULES:
- Analyse Amharic text with the SAME strictness as English
- When in doubt → ALWAYS choose ALLOWED
- Missed scam > wrongly banning a real trader
════════════════════════════════════════════

Respond ONLY with valid JSON, no markdown:
{
  "verdict": "ALLOWED" or "PROHIBITED",
  "reason": "one clear sentence in English explaining your decision"
}"""


async def analyse_message(text: str) -> dict:
    """
    Sends every message to Gemini for full contextual AI analysis.
    Supports English and Amharic.
    Fails SAFE → ALLOWED on any error.
    """
    try:
        full_prompt = f"{SYSTEM_PROMPT}\n\nMessage to analyse:\n---\n{text[:2000]}\n---"
        response = await asyncio.to_thread(
            gemini_model.generate_content, full_prompt
        )
        raw = response.text.strip()
        raw = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        verdict = str(data.get("verdict", "ALLOWED")).upper()
        reason  = str(data.get("reason", "No reason provided."))
        if verdict not in ("ALLOWED", "PROHIBITED"):
            verdict = "ALLOWED"
        log.info("🔍 Gemini → %s | %s", verdict, reason)
        return {"verdict": verdict, "reason": reason}
    except json.JSONDecodeError:
        log.warning("⚠️ Gemini invalid JSON — defaulting ALLOWED")
        return {"verdict": "ALLOWED", "reason": "Parse error — safe default."}
    except Exception as exc:
        log.warning("⚠️ Gemini error: %s — defaulting ALLOWED", exc)
        return {"verdict": "ALLOWED", "reason": "API error — safe default."}

# ─── Moderation Actions ───────────────────────────────────────────────────────

async def delete_message(chat_id: int, message_id: int):
    try:
        await client.delete_messages(chat_id, message_id)
        log.info("🗑️  Deleted message %s", message_id)
    except Exception as exc:
        log.warning("Could not delete message %s: %s", message_id, exc)


async def ban_user(chat_id: int, user_id: int):
    try:
        await client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(
                until_date=None,
                view_messages=True
            )
        ))
        log.info("🔨 Banned user %s", user_id)
    except Exception as exc:
        log.error("Failed to ban user %s: %s", user_id, exc)


async def send_warning(event, reason: str):
    try:
        await event.reply(
            f"⚠️ **Warning / ማስጠንቀቂያ**\n\n"
            f"🇬🇧 This is your **only warning**. Next violation = immediate ban.\n"
            f"🇪🇹 ይህ **የመጨረሻ ማስጠንቀቂያዎ** ነው። ደግመው ከጣሱ ወዲያውኑ ይታገዳሉ።\n\n"
            f"📋 **Reason:** {reason}"
        )
    except Exception as exc:
        log.warning("Could not send warning: %s", exc)


async def notify_admin(
    user_id: int,
    username: str,
    full_name: str,
    offending_text: str,
    reason: str,
    action: str,
):
    try:
        tag = f"@{username}" if username else f"ID:{user_id}"
        report = (
            f"🚫 **Moderation Report**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Action:** {action}\n"
            f"**User:** {full_name} ({tag})\n"
            f"**User ID:** `{user_id}`\n\n"
            f"**Offending Message:**\n"
            f"```\n{offending_text[:600]}\n```\n\n"
            f"**Gemini Reason:** {reason}\n"
            f"**Time (UTC):** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await client.send_message(ADMIN_ID, report, parse_mode="md")
    except Exception as exc:
        log.error("Failed to notify admin: %s", exc)

# ─── Main Event Handler ───────────────────────────────────────────────────────
# chats=GROUP_ID ensures the bot ONLY listens to your specific group

@client.on(events.NewMessage(chats=GROUP_ID))
async def handle_message(event):
    # Ignore bot's own outgoing messages
    if event.out:
        return

    sender = await event.get_sender()
    if sender is None or getattr(sender, "bot", False):
        return

    message_text = event.raw_text or ""
    if not message_text.strip():
        return  # skip pure media with no caption

    user_id   = sender.id
    username  = getattr(sender, "username", "") or ""
    full_name = " ".join(filter(None, [
        getattr(sender, "first_name", ""),
        getattr(sender, "last_name", ""),
    ])) or username or str(user_id)
    chat_id = event.chat_id

    log.info("📨 New message from %s (%s): %s", full_name, user_id, message_text[:80])

    # ── Every message → Gemini ─────────────────────────────────────────────
    result = await analyse_message(message_text)

    if result["verdict"] != "PROHIBITED":
        return  # clean — bot stays silent

    # ── Violation detected ─────────────────────────────────────────────────
    violation_reason = result["reason"]

    # Always delete the message first
    await delete_message(chat_id, event.id)

    prior_warnings = get_warning_count(user_id)

    if prior_warnings == 0:
        # First offence → warn publicly in group
        record_violation(user_id, username, full_name, violation_reason)
        await send_warning(event, violation_reason)
        log.info("⚠️  Warned %s (%s) — %s", full_name, user_id, violation_reason)
    else:
        # Second offence → ban + private admin report only
        record_violation(user_id, username, full_name, violation_reason)
        await ban_user(chat_id, user_id)
        await notify_admin(
            user_id=user_id,
            username=username,
            full_name=full_name,
            offending_text=message_text,
            reason=violation_reason,
            action="🔨 BANNED",
        )
        log.info("🔨 Banned %s (%s) — %s", full_name, user_id, violation_reason)

# ─── Startup: confirm bot can see the group ───────────────────────────────────

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info("🤖 Bot running as @%s (ID: %s)", me.username, me.id)

    # Verify the group is accessible
    try:
        entity = await client.get_entity(GROUP_ID)
        log.info("✅ Monitoring group: %s (ID: %s)", entity.title, GROUP_ID)
    except Exception as exc:
        log.error("❌ Cannot access GROUP_ID %s: %s", GROUP_ID, exc)
        log.error("Make sure the bot is added as admin in the group!")

    log.info("📡 Gemini analysing every message (English + Amharic)...")
    log.info("👤 Admin reports → ID: %s", ADMIN_ID)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
