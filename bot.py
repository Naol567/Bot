"""
Forex Group Management Bot
Powered by Telethon + Gemini AI + In-Memory warnings
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
    log.info("📋 warnings_db updated: user %s → count %s", user_id, warnings_db[user_id]["count"])

# ─── Gemini Analysis ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the AI moderation engine for a professional Forex trading Telegram group.

Your job is to read every message sent in the group and decide: ALLOWED or PROHIBITED.

════════════════════════════════════════════
THIS IS A FOREX TRADING GROUP — understand the context before judging.
════════════════════════════════════════════

✅ ALWAYS ALLOW — these are the heart of this group:
- Any discussion about Forex, currency pairs (EUR/USD, GBP/JPY, XAU/USD, indices, commodities)
- Trade ideas, entries, exits, stop loss, take profit, trailing stops
- Technical analysis: support/resistance, trend lines, chart patterns, candlesticks, indicators (RSI, MACD, EMA, Fibonacci, Bollinger Bands, etc.)
- Fundamental analysis: interest rates, CPI, NFP, central bank decisions, geopolitical events
- Sharing TradingView charts, broker screenshots, P&L screenshots
- Questions about brokers, trading platforms (MT4, MT5, cTrader, TradingView)
- Risk management: lot size, position sizing, leverage discussion, drawdown
- Market commentary: "Dollar is strong", "Gold hit resistance", "EURUSD looks bearish"
- Educational messages about trading strategy, psychology, journaling, discipline
- Casual conversation between members in a friendly tone
- Sharing trade results (profits or losses) in a natural way
- Asking for second opinions on trade setups
- Economic calendar events and their expected market impact
- Even criticism of brokers or trading services in a genuine discussion context

❌ PROHIBITED — remove only these:
1. SPAM & PROMOTION:
   - Advertising paid services: "DM me for signals", "I sell signals", "join my VIP"
   - Posting invite links to other groups/channels to recruit members
   - Referral links: "use my code", "register through my link", "deposit via my link"
   - Selling or buying Telegram accounts, broker accounts, or trading software for money
   - Copy-pasted promotional text advertising unrelated products/services

2. FINANCIAL SCAMS:
   - "Guaranteed profit", "risk-free investment", "I will manage your account for % profit"
   - Asking members to send money, USDT, crypto to any wallet address
   - Fake broker/investment platform links designed to defraud
   - Unsolicited offers to manage someone's trading funds

3. PERSONAL ATTACKS:
   - Direct insults targeting a specific member (e.g., "you are stupid/idiot/loser")
   - Hate speech, racism, threats, or harassment

4. PURE OFF-TOPIC SPAM:
   - Messages completely unrelated to trading, finance, or the group with no context

════════════════════════════════════════════
JUDGMENT GUIDELINES:
- When in doubt → ALWAYS choose ALLOWED. A missed scam is better than banning a real trader.
- "Paid signals" mentioned in discussion or criticism → ALLOWED
- Strong opinions, arguments, debates about trading → ALLOWED
- A long message that mixes trading content with one promotional line → judge by DOMINANT intent
════════════════════════════════════════════

Respond ONLY with valid JSON. No markdown. No explanation outside the JSON:
{
  "verdict": "ALLOWED" or "PROHIBITED",
  "reason": "one clear sentence explaining your decision"
}"""


async def analyse_message(text: str) -> dict:
    """
    Sends message to Gemini for full contextual analysis.
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
        # EditBannedRequest is the correct Telethon method for banning
        await client(EditBannedRequest(
            channel=chat_id,
            participant=user_id,
            banned_rights=ChatBannedRights(
                until_date=None,   # permanent
                view_messages=True
            )
        ))
        log.info("🔨 Banned user %s", user_id)
    except Exception as exc:
        log.error("Failed to ban user %s: %s", user_id, exc)


async def send_warning(event, reason: str):
    try:
        await event.reply(
            f"⚠️ **Warning**\n\n"
            f"This is your **only warning**. "
            f"The next violation will result in an **immediate ban**.\n\n"
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
    """Sends private report to admin only — nothing posted in the group."""
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

@client.on(events.NewMessage)
async def handle_message(event):
    # Only watch group/channel messages
    if not event.is_group and not event.is_channel:
        return
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

    # ── Every message → Gemini ─────────────────────────────────────────────
    result = await analyse_message(message_text)

    if result["verdict"] != "PROHIBITED":
        return  # clean — bot stays silent

    # ── Violation ──────────────────────────────────────────────────────────
    violation_reason = result["reason"]

    # Always delete first
    await delete_message(chat_id, event.id)

    prior_warnings = get_warning_count(user_id)

    if prior_warnings == 0:
        # First offence → public warning in group
        record_violation(user_id, username, full_name, violation_reason)
        await send_warning(event, violation_reason)
        log.info("⚠️  Warned %s (%s) — %s", full_name, user_id, violation_reason)
    else:
        # Second offence → ban + silent admin report, no group message
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

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info("🤖 Bot running as @%s (ID: %s)", me.username, me.id)
    log.info("📡 Gemini analysing every message...")
    log.info("👤 Admin reports → ID: %s", ADMIN_ID)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
