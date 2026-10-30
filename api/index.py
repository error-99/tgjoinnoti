"""
Single-file Telegram bot — aiogram 3 + FastAPI on Vercel.
Nice inline-button admin panel (no slash commands needed).

Endpoints:
  POST /api/webhook   ← Telegram updates
  GET  /api/cron      ← external cron triggers refresh
  GET  /api/setup     ← one-time webhook registration
"""

import os
import json
import time
import logging
from html import escape
from urllib.parse import quote
from functools import wraps

from fastapi import FastAPI, Request, HTTPException

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import (
    ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER,
)
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
# CONFIG
# ======================================================================
BOT_TOKEN      = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPSTASH_URL    = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN  = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not UPSTASH_URL or not UPSTASH_TOKEN:
    raise RuntimeError("UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required")

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
    "extra_admin_ids": [],
}

def create_bot() -> Bot:
    return Bot(token=BOT_TOKEN,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))

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
    if user_id in ADMIN_IDS:
        return True
    cfg = await get_config()
    return user_id in cfg.get("extra_admin_ids", [])

async def get_target_message_id(chat_id: int):
    v = await redis.get(f"bot:target:{chat_id}:message_id")
    return int(v) if v is not None else None

async def set_target_message_id(chat_id: int, mid: int):
    await redis.set(f"bot:target:{chat_id}:message_id", mid)

async def set_target_last_sent(chat_id: int, ts: int):
    await redis.set(f"bot:target:{chat_id}:last_sent_at", ts)

async def get_cached_share_link():
    v = await redis.get("bot:share_link")
    return v if isinstance(v, str) else (str(v) if v else None)

async def set_cached_share_link(link: str, ttl: int = 3600):
    await redis.set("bot:share_link", link, ex=ttl)

async def clear_cached_share_link():
    await redis.delete("bot:share_link")

# ======================================================================
# ACTIONS
# ======================================================================
def mention_html(user) -> str:
    return f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'

def format_message(cfg: dict, name_html: str | None = None) -> str:
    name = name_html or cfg.get("last_member_name", "বন্ধু")
    return cfg.get("message_template", "").replace("{name}", name)

async def get_share_link(bot: Bot, cfg: dict, force: bool = False) -> str:
    if not force:
        cached = await get_cached_share_link()
        if cached:
            return cached
    chat_id = (cfg.get("share_chat_id")
               or (cfg["trigger_chat_ids"][0] if cfg.get("trigger_chat_ids") else None)
               or (cfg["targets"][0]["chat_id"] if cfg.get("targets") else None))
    if not chat_id:
        raise RuntimeError("No share chat configured")
    chat = await bot.get_chat(chat_id)
    if chat.username:
        link = f"https://t.me/{chat.username}"
    elif chat.invite_link:
        link = chat.invite_link
    else:
        invite = await bot.create_chat_invite_link(chat_id=chat_id, name="Share")
        link = invite.invite_link
    await set_cached_share_link(link)
    return link

def build_keyboard(cfg: dict, share_link: str) -> InlineKeyboardMarkup | None:
    share_url = (f"https://t.me/share/url"
                 f"?url={quote(share_link, safe='')}"
                 f"&text={quote(cfg.get('share_text',''), safe='')}")
    rows = []
    for b in cfg.get("buttons", []):
        text = (b.get("text") or "").strip()
        if not text:
            continue
        url = share_url if b.get("type") == "share" else (b.get("url") or "").strip()
        if not url:
            continue
        rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def refresh_target(bot: Bot, chat_id: int, cfg: dict, name_html: str | None = None) -> bool:
    name_html = name_html or cfg.get("last_member_name", "বন্ধু")
    old = await get_target_message_id(chat_id)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old)
        except TelegramAPIError as e:
            log.debug(f"delete old failed {chat_id}: {e}")
    try:
        share_link = await get_share_link(bot, cfg)
    except Exception as e:
        log.error(f"share link error: {e}")
        return False
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=format_message(cfg, name_html),
            reply_markup=build_keyboard(cfg, share_link),
        )
        await set_target_message_id(chat_id, sent.message_id)
        await set_target_last_sent(chat_id, int(time.time()))
        log.info(f"Sent promotion → {chat_id} (msg={sent.message_id})")
        return True
    except TelegramAPIError as e:
        log.error(f"send failed {chat_id}: {e}")
        return False

async def refresh_all(bot: Bot, cfg: dict, name_html: str | None = None) -> dict:
    out = {}
    for t in cfg.get("targets", []):
        out[t["chat_id"]] = await refresh_target(bot, t["chat_id"], cfg, name_html)
    return out

# ======================================================================
# FSM
# ======================================================================
class AdminFlow(StatesGroup):
    waiting_value = State()

def _args(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""

def admin_only(func):
    @wraps(func)
    async def wrapper(message: Message, *a, **kw):
        if not await is_admin(message.from_user.id):
            return await message.answer("⛔ You are not an admin.")
        return await func(message, *a, **kw)
    return wrapper

# ======================================================================
# USER HANDLER
# ======================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    cfg = await get_config()
    prev = await redis.get(f"bot:dm:{message.from_user.id}")
    if prev:
        try:
            await message.bot.delete_message(message.chat.id, int(prev))
        except Exception:
            pass
    try:
        share_link = await get_share_link(message.bot, cfg)
        sent = await message.answer(
            format_message(cfg, mention_html(message.from_user)),
            reply_markup=build_keyboard(cfg, share_link),
        )
        await redis.set(f"bot:dm:{message.from_user.id}", sent.message_id, ex=86400)
    except Exception as e:
        log.error(f"/start error: {e}")

# ======================================================================
# ADMIN PANEL — MAIN MENU
# ======================================================================
def main_panel(cfg: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📢 Targets ({len(cfg['targets'])})",
                              callback_data="menu:targets")],
        [InlineKeyboardButton(text=f"🎯 Triggers ({len(cfg['trigger_chat_ids'])})",
                              callback_data="menu:triggers")],
        [InlineKeyboardButton(text="💬 Message", callback_data="menu:message")],
        [InlineKeyboardButton(text=f"🔘 Buttons ({len(cfg['buttons'])})",
                              callback_data="menu:buttons")],
        [InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings")],
        [InlineKeyboardButton(text="👁 Preview", callback_data="action:preview"),
         InlineKeyboardButton(text="🔄 Refresh Now", callback_data="action:refresh")],
        [InlineKeyboardButton(text="📊 Status", callback_data="action:status")],
    ])


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return await message.answer("⛔ You are not an admin.")
    await state.clear()
    cfg = await get_config()
    await message.answer(
        "🛠 <b>Admin Panel</b>\nTap a category below:",
        reply_markup=main_panel(cfg),
    )


@dp.callback_query(lambda c: c.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    await state.clear()
    cfg = await get_config()
    await call.message.edit_text(
        "🛠 <b>Admin Panel</b>\nTap a category below:",
        reply_markup=main_panel(cfg),
    )
    await call.answer()

# ======================================================================
# ADMIN PANEL — TARGETS
# ======================================================================
@dp.callback_query(lambda c: c.data == "menu:targets")
async def cb_targets(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    rows = []
    for t in cfg["targets"]:
        rows.append([InlineKeyboardButton(
            text=f"❌ {t.get('title','?')} ({t['chat_id']})",
            callback_data=f"del_target:{t['chat_id']}")])
    rows.append([InlineKeyboardButton(text="➕ Add Target", callback_data="ask:add_target")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")])
    await call.message.edit_text(
        "<b>📢 Targets</b>\nChannels/groups the promotion is posted to.\n\n"
        "Tap a target to remove it, or add a new one:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("del_target:"))
async def cb_del_target(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    chat_id = int(call.data.split(":", 1)[1])
    cfg = await get_config()
    cfg["targets"] = [t for t in cfg["targets"] if t["chat_id"] != chat_id]
    await save_config(cfg)
    await call.answer("✅ Removed")
    await cb_targets(call)

# ======================================================================
# ADMIN PANEL — TRIGGERS
# ======================================================================
@dp.callback_query(lambda c: c.data == "menu:triggers")
async def cb_triggers(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    rows = []
    for cid in cfg["trigger_chat_ids"]:
        rows.append([InlineKeyboardButton(
            text=f"❌ {cid}", callback_data=f"del_trigger:{cid}")])
    rows.append([InlineKeyboardButton(text="➕ Add Trigger Chat", callback_data="ask:add_trigger")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")])
    await call.message.edit_text(
        "<b>🎯 Trigger Chats</b>\nChats where the bot watches for new members.\n\n"
        "Tap one to remove, or add a new one:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("del_trigger:"))
async def cb_del_trigger(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    chat_id = int(call.data.split(":", 1)[1])
    cfg = await get_config()
    if chat_id in cfg["trigger_chat_ids"]:
        cfg["trigger_chat_ids"].remove(chat_id)
        await save_config(cfg)
    await call.answer("✅ Removed")
    await cb_triggers(call)

# ======================================================================
# ADMIN PANEL — MESSAGE
# ======================================================================
@dp.callback_query(lambda c: c.data == "menu:message")
async def cb_message(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    text = cfg["message_template"]
    preview = text.replace("{name}", "👤 User")[:200]
    suffix = "…" if len(text) > 200 else ""
    await call.message.edit_text(
        f"<b>💬 Message Template</b>\n\n{preview}{suffix}"
        "\n\nUse <code>{name}</code> for the new member's mention.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Edit Message", callback_data="ask:edit_message")],
            [InlineKeyboardButton(text="👁 Preview", callback_data="action:preview")],
            [InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")],
        ]),
    )
    await call.answer()

# ======================================================================
# ADMIN PANEL — BUTTONS
# ======================================================================
@dp.callback_query(lambda c: c.data == "menu:buttons")
async def cb_buttons(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    rows = []
    for i, b in enumerate(cfg["buttons"], 1):
        tag = "📤" if b.get("type") == "share" else "🔗"
        rows.append([InlineKeyboardButton(
            text=f"❌ {tag} {b['text']}"[:60],
            callback_data=f"del_button:{i}")])
    rows.append([
        InlineKeyboardButton(text="➕ Share Button", callback_data="ask:add_share_button"),
        InlineKeyboardButton(text="➕ URL Button",   callback_data="ask:add_url_button"),
    ])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")])
    await call.message.edit_text(
        "<b>🔘 Buttons</b>\nTap to remove, or add a new one:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("del_button:"))
async def cb_del_button(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    idx = int(call.data.split(":", 1)[1])
    cfg = await get_config()
    if 1 <= idx <= len(cfg["buttons"]):
        cfg["buttons"].pop(idx - 1)
        await save_config(cfg)
    await call.answer("✅ Removed")
    await cb_buttons(call)

# ======================================================================
# ADMIN PANEL — SETTINGS
# ======================================================================
@dp.callback_query(lambda c: c.data == "menu:settings")
async def cb_settings(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    await call.message.edit_text(
        "<b>⚙️ Settings</b>\n\n"
        f"⏱ Interval: <b>{cfg['interval_seconds']}s</b>\n"
        f"📤 Share text: <i>{escape(cfg.get('share_text','')[:80])}</i>\n"
        f"🔗 Share source chat: <code>{cfg.get('share_chat_id') or 'auto'}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏱ Change Interval", callback_data="ask:interval")],
            [InlineKeyboardButton(text="✏️ Change Share Text", callback_data="ask:share_text")],
            [InlineKeyboardButton(text="🔗 Set Share Source Chat", callback_data="ask:share_chat")],
            [InlineKeyboardButton(text="⬅ Back", callback_data="menu:home")],
        ]),
    )
    await call.answer()

# ======================================================================
# ADMIN PANEL — ACTIONS
# ======================================================================
@dp.callback_query(lambda c: c.data == "action:preview")
async def cb_preview(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    try:
        share_link = await get_share_link(call.bot, cfg)
        await call.message.answer(
            "<b>👁 Preview:</b>\n\n" + format_message(cfg, mention_html(call.from_user)),
            reply_markup=build_keyboard(cfg, share_link),
        )
    except Exception as e:
        await call.message.answer(f"❌ {e}")
    await call.answer()


@dp.callback_query(lambda c: c.data == "action:refresh")
async def cb_refresh(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    await call.answer("🔄 Refreshing…")
    res = await refresh_all(call.bot, cfg)
    ok = sum(1 for v in res.values() if v)
    await call.message.answer(f"🔄 Refreshed {ok}/{len(res)} targets")


@dp.callback_query(lambda c: c.data == "action:status")
async def cb_status(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    cfg = await get_config()
    await call.message.answer(
        "<b>📊 Status</b>\n"
        f"🎯 Triggers: {len(cfg['trigger_chat_ids'])}\n"
        f"📢 Targets:  {len(cfg['targets'])}\n"
        f"🔘 Buttons:  {len(cfg['buttons'])}\n"
        f"⏱ Interval: {cfg['interval_seconds']}s\n"
        f"👤 Last member: {cfg.get('last_member_name','?')}\n"
        f"🔗 Share chat: {cfg.get('share_chat_id') or 'auto'}"
    )
    await call.answer()

# ======================================================================
# ADMIN PANEL — ASK PROMPTS
# ======================================================================
ASK_PROMPTS = {
    "add_target":       ("📢 Send the target chat ID\n(e.g. <code>-1001234567890</code>)", "add_target"),
    "add_trigger":      ("🎯 Send the trigger chat ID\n(e.g. <code>-1001234567890</code>)", "add_trigger"),
    "edit_message":     ("💬 Send the new message.\nUse <code>{name}</code> for the mention.", "edit_message"),
    "add_share_button": ("📤 Send the button text (e.g. <b>📤 শেয়ার করুন</b>)", "add_share_button"),
    "add_url_button":   ("🔗 Send: <b>text | url</b>\nExample:\n<code>🎥 Watch | https://youtube.com/x</code>", "add_url_button"),
    "interval":         ("⏱ Send interval in seconds (min 30)", "interval"),
    "share_text":       ("✏️ Send the new share text", "share_text"),
    "share_chat":       ("🔗 Send chat ID whose invite link is shared", "share_chat"),
}


@dp.callback_query(lambda c: c.data.startswith("ask:"))
async def cb_ask(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer("Not admin", show_alert=True)
    key = call.data.split(":", 1)[1]
    prompt, action = ASK_PROMPTS[key]
    await state.set_state(AdminFlow.waiting_value)
    await state.update_data(action=action)
    await call.message.answer(
        prompt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:home")]
        ]),
    )
    await call.answer()

# ======================================================================
# ADMIN PANEL — INPUT HANDLER
# ======================================================================
@dp.message(AdminFlow.waiting_value)
async def on_admin_input(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action = data.get("action")
    text = (message.text or "").strip()
    await state.clear()

    cfg = await get_config()

    try:
        if action == "add_target":
            chat_id = int(text)
            chat = await message.bot.get_chat(chat_id)
            if any(t["chat_id"] == chat_id for t in cfg["targets"]):
                return await message.answer("⚠️ Already added")
            cfg["targets"].append({
                "chat_id": chat_id,
                "title": chat.title or str(chat_id),
                "type": chat.type,
            })
            await save_config(cfg)
            await message.answer(f"✅ Target added: <b>{escape(chat.title or str(chat_id))}</b>")

        elif action == "add_trigger":
            chat_id = int(text)
            if chat_id in cfg["trigger_chat_ids"]:
                return await message.answer("⚠️ Already added")
            cfg["trigger_chat_ids"].append(chat_id)
            await save_config(cfg)
            await message.answer(f"✅ Trigger added: <code>{chat_id}</code>")

        elif action == "edit_message":
            if not text:
                return await message.answer("❌ Empty message")
            cfg["message_template"] = text
            await save_config(cfg)
            await message.answer("✅ Message updated")

        elif action == "add_share_button":
            if not text:
                return await message.answer("❌ Empty")
            cfg["buttons"].append({"text": text, "type": "share", "url": None})
            await save_config(cfg)
            await message.answer(f"✅ Share button added: {escape(text)}")

        elif action == "add_url_button":
            if "|" not in text:
                return await message.answer("❌ Format: <code>text | url</code>")
            label, url = (x.strip() for x in text.split("|", 1))
            if not label or not url:
                return await message.answer("❌ Both required")
            cfg["buttons"].append({"text": label, "type": "url", "url": url})
            await save_config(cfg)
            await message.answer(f"✅ URL button added: {escape(label)}")

        elif action == "interval":
            sec = int(text)
            if sec < 30:
                return await message.answer("❌ Min 30s")
            cfg["interval_seconds"] = sec
            await save_config(cfg)
            await message.answer(f"✅ Interval = {sec}s")

        elif action == "share_text":
            if not text:
                return await message.answer("❌ Empty")
            cfg["share_text"] = text
            await save_config(cfg)
            await message.answer("✅ Share text updated")

        elif action == "share_chat":
            chat_id = int(text)
            cfg["share_chat_id"] = chat_id
            await save_config(cfg)
            await clear_cached_share_link()
            await message.answer(f"✅ Share source = <code>{chat_id}</code>")

    except ValueError:
        await message.answer("❌ Invalid number. Open /admin again.")
    except Exception as e:
        await message.answer(f"❌ {e}")

    cfg = await get_config()
    await message.answer("🛠 <b>Admin Panel</b>", reply_markup=main_panel(cfg))

# ======================================================================
# EVENT: NEW MEMBER
# ======================================================================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.is_bot:
        return
    cfg = await get_config()
    if event.chat.id not in cfg["trigger_chat_ids"]:
        return
    user = event.new_chat_member.user
    name_html = mention_html(user)
    cfg["last_member_name"] = name_html
    cfg["last_member_joined_at"] = int(time.time())
    await save_config(cfg)
    log.info(f"New member {user.full_name} in {event.chat.id}")
    await refresh_all(event.bot, cfg, name_html)

# ======================================================================
# FASTAPI APP
# ======================================================================
app = FastAPI()


@app.post("/api/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")
    data = await request.json()
    bot = create_bot()
    try:
        await dp.feed_update(bot, Update.model_validate(data))
    except Exception as e:
        log.exception(f"update error: {e}")
    finally:
        await bot.session.close()
    return {"ok": True}


@app.get("/api/cron")
async def cron(request: Request):
    if WEBHOOK_SECRET and request.query_params.get("secret", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")
    cfg = await get_config()
    interval = cfg.get("interval_seconds", 240)
    last_joined = cfg.get("last_member_joined_at", 0)
    now = int(time.time())
    if now - last_joined < interval:
        return {"status": "skipped", "age": now - last_joined}
    bot = create_bot()
    try:
        try:
            await get_share_link(bot, cfg, force=True)
        except Exception as e:
            log.warning(f"share refresh: {e}")
        res = await refresh_all(bot, cfg)
        return {"status": "ok", "results": {str(k): v for k, v in res.items()}}
    finally:
        await bot.session.close()


@app.get("/api/setup")
async def setup(request: Request):
    if WEBHOOK_SECRET and request.query_params.get("secret", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")
    host = request.headers.get("host", "")
    url = f"https://{host}/api/webhook"
    bot = create_bot()
    try:
        await bot.set_webhook(
            url=url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "chat_member"],
            drop_pending_updates=True,
        )
        await bot.set_my_commands([
            BotCommand(command="start", description="🎬 Get the video link"),
            BotCommand(command="admin", description="🛠 Admin panel"),
        ])
        info = await bot.get_webhook_info()
        return {"webhook_url": url, "info": str(info)}
    finally:
        await bot.session.close()
