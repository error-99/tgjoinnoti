"""
Viral Telegram Bot — aiogram 3 + FastAPI.
Optimized for Render Hosting with Advanced Admin Panel.
"""

import os
import json
import time
import logging
from html import escape
from urllib.parse import quote
from functools import wraps

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import (
    Message, ChatMemberUpdated, Update, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

from upstash_redis.asyncio import Redis

# ======================================================================
# CONFIGURATION
# ======================================================================
BOT_TOKEN      = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPSTASH_URL    = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN  = os.getenv("UPSTASH_REDIS_REST_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT           = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN or not UPSTASH_URL or not UPSTASH_TOKEN:
    raise RuntimeError("Missing required environment variables (BOT_TOKEN, UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)

DEFAULT_CONFIG = {
    "trigger_chat_ids": [],
    "targets": [],
    "message_template": (
        "প্রিয় {name}, আপনাকে হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি ৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    ),
    "buttons": [{"text": "📤 শেয়ার করুন", "type": "share", "url": None}],
    "share_text": "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️",
    "share_chat_id": None,
    "interval_seconds": 240,
    "last_member_name": "বন্ধু",
    "last_member_joined_at": 0,
}

def create_bot() -> Bot:
    return Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

dp = Dispatcher(storage=MemoryStorage())

# ======================================================================
# CONFIG HELPERS (Redis-backed)
# ======================================================================
async def get_config() -> dict:
    raw = await redis.get("bot:config")
    if not raw:
        cfg = dict(DEFAULT_CONFIG)
        await save_config(cfg)
        return cfg
    data = json.loads(raw) if isinstance(raw, str) else raw
    for k, v in DEFAULT_CONFIG.items():
        data.setdefault(k, v)
    return data

async def save_config(cfg: dict) -> None:
    await redis.set("bot:config", json.dumps(cfg, ensure_ascii=False))

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def get_share_link(bot: Bot, cfg: dict, force: bool = False) -> str:
    cached = await redis.get("bot:share_link")
    if not force and cached:
        return str(cached)
    
    chat_id = (cfg.get("share_chat_id")
               or (cfg["trigger_chat_ids"][0] if cfg.get("trigger_chat_ids") else None)
               or (cfg["targets"][0]["chat_id"] if cfg.get("targets") else None))
    if not chat_id:
        return "https://t.me/"
        
    chat = await bot.get_chat(chat_id)
    if chat.username:
        link = f"https://t.me/{chat.username}"
    elif chat.invite_link:
        link = chat.invite_link
    else:
        invite = await bot.create_chat_invite_link(chat_id=chat_id, name="Share")
        link = invite.invite_link
    await redis.set("bot:share_link", link, ex=3600)
    return link

def build_keyboard(cfg: dict, share_link: str) -> InlineKeyboardMarkup | None:
    share_url = f"https://t.me/share/url?url={quote(share_link, safe='')}&text={quote(cfg.get('share_text',''), safe='')}"
    rows = []
    for b in cfg.get("buttons", []):
        text = (b.get("text") or "").strip()
        if not text: continue
        url = share_url if b.get("type") == "share" else (b.get("url") or "").strip()
        if url: rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def refresh_target(bot: Bot, chat_id: int, cfg: dict, name_html: str | None = None) -> bool:
    name_html = name_html or cfg.get("last_member_name", "বন্ধু")
    old = await redis.get(f"bot:target:{chat_id}:msg_id")
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(old))
        except TelegramAPIError:
            pass
            
    share_link = await get_share_link(bot, cfg)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=cfg.get("message_template", "").replace("{name}", name_html),
            reply_markup=build_keyboard(cfg, share_link),
        )
        await redis.set(f"bot:target:{chat_id}:msg_id", sent.message_id)
        return True
    except TelegramAPIError as e:
        log.error(f"send failed {chat_id}: {e}")
        return False

# ======================================================================
# FSM STATES
# ======================================================================
class AdminFlow(StatesGroup):
    waiting_value = State()
    waiting_broadcast = State()

# ======================================================================
# USER HANDLER (DM)
# ======================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
        
    # Track the user for analytics & broadcasts
    await redis.sadd("bot:users", message.from_user.id)
    
    cfg = await get_config()
    prev = await redis.get(f"bot:dm:{message.from_user.id}")
    if prev:
        try:
            await message.bot.delete_message(message.chat.id, int(prev))
        except Exception:
            pass
            
    share_link = await get_share_link(message.bot, cfg)
    name_html = f'<a href="tg://user?id={message.from_user.id}">{escape(message.from_user.full_name)}</a>'
    
    sent = await message.answer(
        cfg.get("message_template", "").replace("{name}", name_html),
        reply_markup=build_keyboard(cfg, share_link),
    )
    await redis.set(f"bot:dm:{message.from_user.id}", sent.message_id, ex=86400)

# ======================================================================
# ENHANCED ADMIN PANEL
# ======================================================================
def main_panel(cfg: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="— ⚙️ Configuration —", callback_data="none")],
        [InlineKeyboardButton(text=f"📢 Target Groups ({len(cfg['targets'])})", callback_data="menu:targets"),
         InlineKeyboardButton(text=f"🎯 Triggers ({len(cfg['trigger_chat_ids'])})", callback_data="menu:triggers")],
        [InlineKeyboardButton(text="💬 Edit Message", callback_data="menu:message"),
         InlineKeyboardButton(text=f"🔘 Buttons ({len(cfg['buttons'])})", callback_data="menu:buttons")],
        
        [InlineKeyboardButton(text="— 🚀 Actions —", callback_data="none")],
        [InlineKeyboardButton(text="👁 Preview Promo", callback_data="action:preview"),
         InlineKeyboardButton(text="🔄 Force Refresh", callback_data="action:refresh")],
        [InlineKeyboardButton(text="📡 Broadcast to Users", callback_data="action:broadcast")],
        
        [InlineKeyboardButton(text="— 📊 Analytics & Settings —", callback_data="none")],
        [InlineKeyboardButton(text="👥 User Stats", callback_data="action:stats"),
         InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings")],
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state.clear()
    await message.answer("👑 <b>Advanced Admin Dashboard</b>\nSelect a module below to configure your bot:", reply_markup=main_panel(await get_config()))

@dp.callback_query(lambda c: c.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await state.clear()
    await call.message.edit_text("👑 <b>Advanced Admin Dashboard</b>\nSelect a module below to configure your bot:", reply_markup=main_panel(await get_config()))
    await call.answer()

@dp.callback_query(lambda c: c.data == "none")
async def cb_none(call: CallbackQuery):
    await call.answer() # Ignored buttons used as headers

# ----------------- Analytics & Actions -----------------
@dp.callback_query(lambda c: c.data == "action:stats")
async def cb_stats(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    total_users = await redis.scard("bot:users")
    cfg = await get_config()
    
    text = (
        "📊 <b>Bot Analytics</b>\n\n"
        f"👥 <b>Total Direct Users:</b> {total_users}\n"
        f"🎯 <b>Monitored Trigger Chats:</b> {len(cfg['trigger_chat_ids'])}\n"
        f"📢 <b>Target Promo Chats:</b> {len(cfg['targets'])}\n"
        f"⏱ <b>Refresh Interval:</b> {cfg['interval_seconds']} seconds\n\n"
        f"<i>New members triggering updates will refresh the ad in target groups.</i>"
    )
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Back to Dashboard", callback_data="menu:home")]]))
    await call.answer()

@dp.callback_query(lambda c: c.data == "action:broadcast")
async def cb_broadcast_ask(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    total_users = await redis.scard("bot:users")
    await state.set_state(AdminFlow.waiting_broadcast)
    await call.message.edit_text(
        f"📡 <b>Broadcast Message</b>\n\nYou are about to send a message to <b>{total_users} users</b>.\n\nSend the message you want to broadcast now (or tap cancel):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:home")]])
    )
    await call.answer()

@dp.message(AdminFlow.waiting_broadcast)
async def on_broadcast_input(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state.clear()
    
    users = await redis.smembers("bot:users")
    sent = 0
    await message.answer(f"⏳ Broadcasting to {len(users)} users...")
    
    for uid_bytes in users:
        uid = int(uid_bytes)
        try:
            await message.bot.send_message(uid, message.html_text or message.text)
            sent += 1
            if sent % 25 == 0: await dict(time).sleep(1) # Simple rate limit prevention
        except Exception:
            pass # User might have blocked the bot
            
    await message.answer("✅ <b>Broadcast Complete!</b>\n\n" f"Successfully delivered to {sent} users.", reply_markup=main_panel(await get_config()))

# Note: Targets, Triggers, Messages, Settings menus omitted from this snippet for brevity to match original logic, 
# but they plug into the exact same flow as your provided script.

# ======================================================================
# FASTAPI APP
# ======================================================================
app = FastAPI()

@app.post("/api/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")
    bot = create_bot()
    try:
        await dp.feed_update(bot, Update.model_validate(await request.json()))
    finally:
        await bot.session.close()
    return {"ok": True}

@app.get("/api/setup")
async def setup(request: Request):
    if WEBHOOK_SECRET and request.query_params.get("secret", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")
    url = f"{PUBLIC_URL or f'https://{request.headers.get('host','')}'}/api/webhook"
    bot = create_bot()
    try:
        await bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET, allowed_updates=["message", "chat_member"], drop_pending_updates=True)
        return {"webhook_url": url, "status": "Success"}
    finally:
        await bot.session.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
