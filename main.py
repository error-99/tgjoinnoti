"""
Viral Telegram Bot — aiogram 3 + FastAPI.
Uses a local JSON file for storage (No Redis).
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

# ======================================================================
# CONFIGURATION
# ======================================================================
BOT_TOKEN      = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT           = int(os.getenv("PORT", "8000"))
STATE_FILE     = "bot_state.json"

if not BOT_TOKEN:
    raise RuntimeError("Missing required environment variable: BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

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
# JSON STORAGE HELPERS
# ======================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ensure all default config keys exist
                for k, v in DEFAULT_CONFIG.items():
                    data["config"].setdefault(k, v)
                return data
        except Exception as e:
            log.error(f"Error reading JSON: {e}")
            
    # Default state structure if file doesn't exist
    return {
        "config": dict(DEFAULT_CONFIG),
        "users": [],
        "target_msg_ids": {},
        "dm_msg_ids": {},
        "share_link": None,
        "share_link_expires": 0
    }

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def get_share_link(bot: Bot, state: dict, force: bool = False) -> str:
    now = int(time.time())
    if not force and state.get("share_link") and state.get("share_link_expires", 0) > now:
        return state["share_link"]
    
    cfg = state["config"]
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
        
    state["share_link"] = link
    state["share_link_expires"] = now + 3600
    save_state(state)
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

async def refresh_target(bot: Bot, chat_id: int, state: dict, name_html: str | None = None) -> bool:
    cfg = state["config"]
    name_html = name_html or cfg.get("last_member_name", "বন্ধু")
    old = state["target_msg_ids"].get(str(chat_id))
    
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old)
        except TelegramAPIError:
            pass
            
    share_link = await get_share_link(bot, state)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=cfg.get("message_template", "").replace("{name}", name_html),
            reply_markup=build_keyboard(cfg, share_link),
        )
        state["target_msg_ids"][str(chat_id)] = sent.message_id
        save_state(state)
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
        
    state = load_state()
    uid = message.from_user.id
    
    # Track the user for analytics & broadcasts
    if uid not in state["users"]:
        state["users"].append(uid)
        save_state(state)
    
    cfg = state["config"]
    prev = state["dm_msg_ids"].get(str(uid))
    if prev:
        try:
            await message.bot.delete_message(message.chat.id, prev)
        except Exception:
            pass
            
    share_link = await get_share_link(message.bot, state)
    name_html = f'<a href="tg://user?id={uid}">{escape(message.from_user.full_name)}</a>'
    
    sent = await message.answer(
        cfg.get("message_template", "").replace("{name}", name_html),
        reply_markup=build_keyboard(cfg, share_link),
    )
    state["dm_msg_ids"][str(uid)] = sent.message_id
    save_state(state)

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
async def cmd_admin(message: Message, state_fsm: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state_fsm.clear()
    state = load_state()
    await message.answer("👑 <b>Advanced Admin Dashboard</b>\nSelect a module below to configure your bot:", reply_markup=main_panel(state["config"]))

@dp.callback_query(lambda c: c.data == "menu:home")
async def cb_home(call: CallbackQuery, state_fsm: FSMContext):
    if not await is_admin(call.from_user.id): return
    await state_fsm.clear()
    state = load_state()
    await call.message.edit_text("👑 <b>Advanced Admin Dashboard</b>\nSelect a module below to configure your bot:", reply_markup=main_panel(state["config"]))
    await call.answer()

@dp.callback_query(lambda c: c.data == "none")
async def cb_none(call: CallbackQuery):
    await call.answer()

# ----------------- Analytics & Actions -----------------
@dp.callback_query(lambda c: c.data == "action:stats")
async def cb_stats(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    state = load_state()
    total_users = len(state["users"])
    cfg = state["config"]
    
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
async def cb_broadcast_ask(call: CallbackQuery, state_fsm: FSMContext):
    if not await is_admin(call.from_user.id): return
    state = load_state()
    total_users = len(state["users"])
    
    await state_fsm.set_state(AdminFlow.waiting_broadcast)
    await call.message.edit_text(
        f"📡 <b>Broadcast Message</b>\n\nYou are about to send a message to <b>{total_users} users</b>.\n\nSend the message you want to broadcast now (or tap cancel):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:home")]])
    )
    await call.answer()

@dp.message(AdminFlow.waiting_broadcast)
async def on_broadcast_input(message: Message, state_fsm: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state_fsm.clear()
    
    state = load_state()
    users = state["users"]
    sent = 0
    await message.answer(f"⏳ Broadcasting to {len(users)} users...")
    
    for uid in users:
        try:
            await message.bot.send_message(uid, message.html_text or message.text)
            sent += 1
            if sent % 25 == 0: await dict(time).sleep(1)
        except Exception:
            pass
            
    await message.answer("✅ <b>Broadcast Complete!</b>\n\n" f"Successfully delivered to {sent} users.", reply_markup=main_panel(state["config"]))

# ======================================================================
# EVENT: NEW MEMBER
# ======================================================================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.is_bot:
        return
        
    state = load_state()
    cfg = state["config"]
    if event.chat.id not in cfg["trigger_chat_ids"]:
        return
        
    user = event.new_chat_member.user
    name_html = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
    
    cfg["last_member_name"] = name_html
    cfg["last_member_joined_at"] = int(time.time())
    save_state(state)
    
    log.info(f"New member {user.full_name} in {event.chat.id}")
    for t in cfg.get("targets", []):
        await refresh_target(event.bot, t["chat_id"], state, name_html)

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
