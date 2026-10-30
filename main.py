import os
import json
import time
import asyncio
import logging
from html import escape
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
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
        "নতুন সদস্য {name} যোগদান করেছেন! ❤️\n\n"
        "ভিডিও দেখতে লিংকটি ৩টি গ্রুপে শেয়ার করুন।"
    ),
    "buttons": [], # Custom URL buttons
    "interval_seconds": 60,
    
    # Auto-Share System settings
    "share_source_chat_id": None, 
    "share_button_text": "📤 শেয়ার করুন",
    "share_prompt_text": "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️",
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
        "target_msg_ids": {},
        "target_last_sent": {},
        "share_link": None,
        "share_link_expires": 0
    }

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ======================================================================
# LINK & KEYBOARD GENERATION
# ======================================================================
async def get_share_link(bot: Bot, state: dict, force: bool = False) -> str:
    now = int(time.time())
    # Return cached link if valid
    if not force and state.get("share_link") and state.get("share_link_expires", 0) > now:
        return state["share_link"]
    
    cfg = state["config"]
    chat_id = cfg.get("share_source_chat_id")
    
    if not chat_id: 
        return "" # No share source configured
        
    try:
        chat = await bot.get_chat(chat_id)
        if chat.username: 
            link = f"https://t.me/{chat.username}"
        elif chat.invite_link: 
            link = chat.invite_link
        else:
            # Bot MUST be admin in that channel/group to do this
            invite = await bot.create_chat_invite_link(chat_id=chat_id, name="AutoShare")
            link = invite.invite_link
            
        state["share_link"] = link
        state["share_link_expires"] = now + 3600 # Cache for 1 hour
        save_state(state)
        return link
    except Exception as e:
        log.error(f"Failed to fetch invite link for {chat_id}: {e}")
        return ""

def build_keyboard(cfg: dict, share_link: str) -> InlineKeyboardMarkup | None:
    rows = []
    
    # 1. Add Fixed Auto-Share Button (if configured)
    if share_link:
        share_url = f"https://t.me/share/url?url={quote(share_link, safe='')}&text={quote(cfg.get('share_prompt_text',''), safe='')}"
        rows.append([InlineKeyboardButton(text=cfg.get("share_button_text", "📤 Share"), url=share_url)])
        
    # 2. Add Custom URL Buttons
    for b in cfg.get("buttons", []):
        text = (b.get("text") or "").strip()
        url = (b.get("url") or "").strip()
        if text and url: 
            rows.append([InlineKeyboardButton(text=text, url=url)])
            
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def refresh_target(bot: Bot, chat_id: int, state: dict, name_html: str = "বন্ধু", force: bool = False) -> bool:
    cfg = state["config"]
    now = int(time.time())
    last_sent = state["target_last_sent"].get(str(chat_id), 0)
    
    # Throttle sending
    if not force and (now - last_sent) < cfg["interval_seconds"]:
        return False

    old_msg = state["target_msg_ids"].get(str(chat_id))
    if old_msg:
        try: await bot.delete_message(chat_id=chat_id, message_id=old_msg)
        except TelegramAPIError: pass
            
    share_link = await get_share_link(bot, state)
    
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=cfg.get("message_template", "").replace("{name}", name_html),
            reply_markup=build_keyboard(cfg, share_link),
        )
        state["target_msg_ids"][str(chat_id)] = sent.message_id
        state["target_last_sent"][str(chat_id)] = now
        save_state(state)
        return True
    except TelegramAPIError as e:
        log.error(f"send failed {chat_id}: {e}")
        return False

# ======================================================================
# ADMIN FSM
# ======================================================================
class AdminFlow(StatesGroup):
    waiting_input = State()

# ======================================================================
# ADMIN PANEL
# ======================================================================
def main_panel(cfg: dict) -> InlineKeyboardMarkup:
    share_source = cfg.get("share_source_chat_id") or "Not Set"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="— 🎯 Bots Core Settings —", callback_data="none")],
        [InlineKeyboardButton(text=f"📢 Targets ({len(cfg['targets'])})", callback_data="menu:targets"),
         InlineKeyboardButton(text=f"🎯 Triggers ({len(cfg['trigger_chat_ids'])})", callback_data="menu:triggers")],
        [InlineKeyboardButton(text="💬 Edit Message", callback_data="menu:message"),
         InlineKeyboardButton(text=f"⏱ Cooldown: {cfg['interval_seconds']}s", callback_data="ask:interval")],
         
        [InlineKeyboardButton(text="— 📤 Auto-Share Button Settings —", callback_data="none")],
        [InlineKeyboardButton(text=f"🔗 Set Auto-Share Chat (Now: {share_source})", callback_data="ask:share_source")],
        [InlineKeyboardButton(text="✏️ Edit Share Text", callback_data="ask:share_button_text")],
        
        [InlineKeyboardButton(text="— ⚙️ Other Buttons & Actions —", callback_data="none")],
        [InlineKeyboardButton(text=f"➕ Add Custom URL Buttons ({len(cfg['buttons'])})", callback_data="menu:buttons")],
        [InlineKeyboardButton(text="👁 Preview Message", callback_data="action:preview"),
         InlineKeyboardButton(text="🔄 Force Send Now", callback_data="action:refresh")],
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state.clear()
    bot_state = load_state()
    await message.answer("👑 <b>Admin Dashboard</b>", reply_markup=main_panel(bot_state["config"]))

@dp.callback_query(lambda c: c.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await state.clear()
    bot_state = load_state()
    await call.message.edit_text("👑 <b>Admin Dashboard</b>", reply_markup=main_panel(bot_state["config"]))
    await call.answer()

@dp.callback_query(lambda c: c.data == "none")
async def cb_none(call: CallbackQuery):
    await call.answer()

# --- TARGETS ---
@dp.callback_query(lambda c: c.data == "menu:targets")
async def cb_targets(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    cfg = load_state()["config"]
    rows = [[InlineKeyboardButton(text=f"❌ {t['title']} ({t['chat_id']})", callback_data=f"del_target:{t['chat_id']}")] for t in cfg["targets"]]
    rows.append([InlineKeyboardButton(text="➕ Add Target Group/Channel", callback_data="ask:add_target")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")])
    await call.message.edit_text("<b>📢 Target Groups/Channels</b>\nWhere the bot posts the link:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(lambda c: c.data.startswith("del_target:"))
async def cb_del_target(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    chat_id = int(call.data.split(":")[1])
    bot_state = load_state()
    bot_state["config"]["targets"] = [t for t in bot_state["config"]["targets"] if t["chat_id"] != chat_id]
    save_state(bot_state)
    await cb_targets(call)

# --- TRIGGERS ---
@dp.callback_query(lambda c: c.data == "menu:triggers")
async def cb_triggers(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    cfg = load_state()["config"]
    rows = [[InlineKeyboardButton(text=f"❌ {cid}", callback_data=f"del_trigger:{cid}")] for cid in cfg["trigger_chat_ids"]]
    rows.append([InlineKeyboardButton(text="➕ Add Trigger Channel", callback_data="ask:add_trigger")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")])
    await call.message.edit_text("<b>🎯 Trigger Channels</b>\nWhere the bot watches for new members:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(lambda c: c.data.startswith("del_trigger:"))
async def cb_del_trigger(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    chat_id = int(call.data.split(":")[1])
    bot_state = load_state()
    if chat_id in bot_state["config"]["trigger_chat_ids"]:
        bot_state["config"]["trigger_chat_ids"].remove(chat_id)
        save_state(bot_state)
    await cb_triggers(call)

# --- CUSTOM BUTTONS ---
@dp.callback_query(lambda c: c.data == "menu:buttons")
async def cb_buttons(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    cfg = load_state()["config"]
    rows = [[InlineKeyboardButton(text=f"❌ 🔗 {b['text']}", callback_data=f"del_button:{i}")] for i, b in enumerate(cfg["buttons"])]
    rows.append([InlineKeyboardButton(text="➕ Add Custom Link Button", callback_data="ask:add_button")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")])
    await call.message.edit_text("<b>🔗 Custom Link Buttons</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(lambda c: c.data.startswith("del_button:"))
async def cb_del_button(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    idx = int(call.data.split(":")[1])
    bot_state = load_state()
    if 0 <= idx < len(bot_state["config"]["buttons"]):
        bot_state["config"]["buttons"].pop(idx)
        save_state(bot_state)
    await cb_buttons(call)

# --- ACTIONS ---
@dp.callback_query(lambda c: c.data == "action:preview")
async def cb_preview(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    bot_state = load_state()
    cfg = bot_state["config"]
    share_link = await get_share_link(call.bot, bot_state)
    try:
        await call.message.answer(
            "<b>👁 Preview:</b>\n\n" + cfg["message_template"].replace("{name}", f'<a href="tg://user?id={call.from_user.id}">{escape(call.from_user.full_name)}</a>'),
            reply_markup=build_keyboard(cfg, share_link)
        )
    except Exception as e:
        await call.message.answer(f"❌ Error: {e}")
    await call.answer()

@dp.callback_query(lambda c: c.data == "action:refresh")
async def cb_refresh(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    bot_state = load_state()
    count = 0
    await call.answer("Sending to targets...")
    for t in bot_state["config"]["targets"]:
        success = await refresh_target(call.bot, t["chat_id"], bot_state, name_html="নতুন বন্ধু", force=True)
        if success: count += 1
    await call.message.answer(f"✅ Forced refresh sent to {count} targets.")

# --- INPUT HANDLERS (ASK) ---
ASK_PROMPTS = {
    "add_target": ("📢 Send the Target Chat ID (e.g. -1001234567890)", "add_target"),
    "add_trigger": ("🎯 Send the Trigger Channel ID (e.g. -1001234567890)", "add_trigger"),
    "edit_message": ("💬 Send the new message.\nUse <code>{name}</code> for the member's mention.", "edit_message"),
    "add_button": ("🔗 Send button text and link separated by a pipe `|`\nExample: `Watch Video | https://t.me/mychannel`", "add_button"),
    "interval": ("⏱ Send the cooldown interval in seconds (e.g., 60 for 1 minute)", "interval"),
    "share_source": ("🔗 Send the Chat ID for the Auto-Share link (e.g. -1001234567890).\n\n<i>Note: The bot MUST be an admin in this chat to generate invite links!</i>", "share_source"),
    "share_button_text": ("✏️ Send the text for the Auto-Share Button (e.g., '📤 শেয়ার করুন')", "share_button_text"),
}

@dp.callback_query(lambda c: c.data.startswith("ask:"))
async def cb_ask(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    key = call.data.split(":")[1]
    prompt, action = ASK_PROMPTS[key]
    await state.set_state(AdminFlow.waiting_input)
    await state.update_data(action=action)
    await call.message.edit_text(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:home")]]))

@dp.message(AdminFlow.waiting_input)
async def on_admin_input(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    data = await state.get_data()
    action = data.get("action")
    text = (message.html_text or message.text or "").strip()
    await state.clear()
    
    bot_state = load_state()
    cfg = bot_state["config"]
    
    try:
        if action == "add_target":
            chat = await message.bot.get_chat(int(text))
            if not any(t["chat_id"] == chat.id for t in cfg["targets"]):
                cfg["targets"].append({"chat_id": chat.id, "title": chat.title or str(chat.id)})
                await message.answer(f"✅ Target added: {chat.title}")
                
        elif action == "add_trigger":
            cid = int(text)
            if cid not in cfg["trigger_chat_ids"]:
                cfg["trigger_chat_ids"].append(cid)
                await message.answer(f"✅ Trigger channel added: {cid}")
                
        elif action == "edit_message":
            cfg["message_template"] = text
            await message.answer("✅ Message updated")
            
        elif action == "add_button":
            label, url = (x.strip() for x in text.split("|", 1))
            cfg["buttons"].append({"text": label, "type": "url", "url": url})
            await message.answer(f"✅ Custom Link added: {label}")
            
        elif action == "interval":
            cfg["interval_seconds"] = int(text)
            await message.answer(f"✅ Cooldown interval set to {text} seconds")
            
        elif action == "share_source":
            cid = int(text)
            # Test if bot can read the chat
            await message.bot.get_chat(cid)
            cfg["share_source_chat_id"] = cid
            bot_state["share_link"] = None # Clear cached link
            bot_state["share_link_expires"] = 0
            await message.answer(f"✅ Auto-Share source updated to {cid}")
            
        elif action == "share_button_text":
            cfg["share_button_text"] = text
            await message.answer(f"✅ Share button text updated to: {text}")
            
        save_state(bot_state)
    except Exception as e:
        await message.answer(f"❌ Error saving input: {e}")
        
    await message.answer("👑 <b>Admin Dashboard</b>", reply_markup=main_panel(cfg))

# ======================================================================
# EVENT: NEW MEMBER IN TRIGGER CHAT
# ======================================================================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.is_bot: return
    
    bot_state = load_state()
    cfg = bot_state["config"]
    
    # Only act if it happens in a channel/group we are monitoring
    if event.chat.id not in cfg["trigger_chat_ids"]: return
        
    user = event.new_chat_member.user
    name_html = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
    
    log.info(f"Triggered by new member {user.full_name} in {event.chat.id}")
    
    # Attempt to post to all targets (respecting the cooldown interval)
    for t in cfg.get("targets", []):
        await refresh_target(event.bot, t["chat_id"], bot_state, name_html, force=False)


# ======================================================================
# FASTAPI LIFESPAN & WEB SERVER (FOR RENDER HEALTH CHECKS)
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Telegram Bot Polling in the background...")
    await bot.delete_webhook(drop_pending_updates=True) 
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield 
    log.info("Shutting down...")
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
@app.get("/health")
async def health_check():
    return JSONResponse(content={"status": "online", "bot": "running", "time": int(time.time())})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
