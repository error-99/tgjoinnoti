import os
import json
import time
import asyncio
import logging
from html import escape
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import (
    Message, ChatMemberUpdated, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

# ======================================================================
# CONFIGURATION
# ======================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "YOUR_TELEGRAM_ID_HERE").split(",") if x.strip()]
PORT      = int(os.getenv("PORT", "8000"))
STATE_FILE = "bot_state.json"

if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
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

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# ======================================================================
# JSON STORAGE HELPERS
# ======================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    data["config"].setdefault(k, v)
                return data
        except Exception as e:
            log.error(f"Error reading JSON: {e}")
            
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
    
    if not chat_id: return "https://t.me/"
        
    chat = await bot.get_chat(chat_id)
    if chat.username: link = f"https://t.me/{chat.username}"
    elif chat.invite_link: link = chat.invite_link
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
        try: await bot.delete_message(chat_id=chat_id, message_id=old)
        except TelegramAPIError: pass
            
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
    if message.chat.type != ChatType.PRIVATE: return
        
    state = load_state()
    uid = message.from_user.id
    
    if uid not in state["users"]:
        state["users"].append(uid)
        save_state(state)
    
    cfg = state["config"]
    prev = state["dm_msg_ids"].get(str(uid))
    if prev:
        try: await message.bot.delete_message(message.chat.id, prev)
        except Exception: pass
            
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
    await message.answer("👑 <b>Advanced Admin Dashboard</b>", reply_markup=main_panel(state["config"]))

@dp.callback_query(lambda c: c.data == "menu:home")
async def cb_home(call: CallbackQuery, state_fsm: FSMContext):
    if not await is_admin(call.from_user.id): return
    await state_fsm.clear()
    state = load_state()
    await call.message.edit_text("👑 <b>Advanced Admin Dashboard</b>", reply_markup=main_panel(state["config"]))
    await call.answer()

@dp.callback_query(lambda c: c.data == "none")
async def cb_none(call: CallbackQuery):
    await call.answer()

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
    )
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")]]))
    await call.answer()

# ======================================================================
# EVENT: NEW MEMBER
# ======================================================================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.is_bot: return
    state = load_state()
    cfg = state["config"]
    if event.chat.id not in cfg["trigger_chat_ids"]: return
        
    user = event.new_chat_member.user
    name_html = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
    
    cfg["last_member_name"] = name_html
    cfg["last_member_joined_at"] = int(time.time())
    save_state(state)
    
    log.info(f"New member {user.full_name} in {event.chat.id}")
    for t in cfg.get("targets", []):
        await refresh_target(event.bot, t["chat_id"], state, name_html)


# ======================================================================
# FASTAPI LIFESPAN & WEB SERVER (FOR RENDER HEALTH CHECKS)
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # This runs when the server starts
    log.info("Starting Telegram Bot Polling in the background...")
    await bot.delete_webhook(drop_pending_updates=True) # Ensure webhook is off
    polling_task = asyncio.create_task(dp.start_polling(bot))
    
    yield # Web server runs while yielded
    
    # This runs when the server shuts down
    log.info("Shutting down...")
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
@app.get("/health")
async def health_check():
    # Render will constantly hit this page to make sure the app isn't dead
    return JSONResponse(content={
        "status": "online",
        "bot": "running via long polling",
        "timestamp": int(time.time())
    })

if __name__ == "__main__":
    import uvicorn
    # Start the web server (which automatically starts the bot polling via lifespan)
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
