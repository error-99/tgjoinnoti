"""
Single-file Telegram bot — aiogram 3 + FastAPI on Vercel.

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
    Message, ChatMemberUpdated, Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand,
)
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

dp = Dispatcher()

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
# HELPERS
# ======================================================================
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
# USER HANDLERS
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
# ADMIN HANDLERS
# ======================================================================
@dp.message(Command("admin"))
@admin_only
async def cmd_admin(message: Message):
    await message.answer(
        "🤖 <b>Admin Panel</b>\n\n"
        "<b>Targets (channels/groups):</b>\n"
        "/targets — list\n"
        "/add_target &lt;chat_id&gt;\n"
        "/remove_target &lt;chat_id&gt;\n\n"
        "<b>Trigger chats (watch for joins):</b>\n"
        "/add_trigger &lt;chat_id&gt;\n"
        "/remove_trigger &lt;chat_id&gt;\n\n"
        "<b>Message:</b>\n"
        "/set_message &lt;text with {name}&gt;\n"
        "/get_message   /preview\n\n"
        "<b>Buttons:</b>\n"
        "/buttons\n"
        "/add_button &lt;text&gt; | &lt;url&gt;\n"
        "/add_share_button &lt;text&gt;\n"
        "/remove_button &lt;index&gt;\n\n"
        "<b>Settings:</b>\n"
        "/set_interval &lt;seconds&gt;\n"
        "/set_share_text &lt;text&gt;\n"
        "/set_share_chat &lt;chat_id&gt;\n"
        "/refresh   /status   /add_admin &lt;user_id&gt;"
    )

@dp.message(Command("add_admin"))
@admin_only
async def cmd_add_admin(message: Message):
    try:
        uid = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /add_admin <user_id>")
    cfg = await get_config()
    if uid not in cfg["extra_admin_ids"]:
        cfg["extra_admin_ids"].append(uid)
        await save_config(cfg)
    await message.answer(f"✅ Added admin <code>{uid}</code>")

@dp.message(Command("targets"))
@admin_only
async def cmd_targets(message: Message):
    cfg = await get_config()
    if not cfg["targets"]:
        return await message.answer("No targets. Use /add_target <chat_id>")
    lines = ["<b>Targets:</b>"]
    for i, t in enumerate(cfg["targets"], 1):
        lines.append(f"{i}. <code>{t['chat_id']}</code> — {t.get('title','?')}")
    await message.answer("\n".join(lines))

@dp.message(Command("add_target"))
@admin_only
async def cmd_add_target(message: Message):
    try:
        chat_id = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /add_target <chat_id>")
    try:
        chat = await message.bot.get_chat(chat_id)
    except Exception as e:
        return await message.answer(f"❌ Cannot access: {e}")
    cfg = await get_config()
    if any(t["chat_id"] == chat_id for t in cfg["targets"]):
        return await message.answer("⚠️ Already added")
    cfg["targets"].append({"chat_id": chat_id, "title": chat.title or str(chat_id), "type": chat.type})
    await save_config(cfg)
    await message.answer(f"✅ Added target: {chat.title} (<code>{chat_id}</code>)")

@dp.message(Command("remove_target"))
@admin_only
async def cmd_remove_target(message: Message):
    try:
        chat_id = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /remove_target <chat_id>")
    cfg = await get_config()
    before = len(cfg["targets"])
    cfg["targets"] = [t for t in cfg["targets"] if t["chat_id"] != chat_id]
    await save_config(cfg)
    await message.answer("✅ Removed" if len(cfg["targets"]) < before else "⚠️ Not found")

@dp.message(Command("add_trigger"))
@admin_only
async def cmd_add_trigger(message: Message):
    try:
        chat_id = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /add_trigger <chat_id>")
    cfg = await get_config()
    if chat_id not in cfg["trigger_chat_ids"]:
        cfg["trigger_chat_ids"].append(chat_id)
        await save_config(cfg)
    await message.answer(f"✅ Trigger chat <code>{chat_id}</code> added")

@dp.message(Command("remove_trigger"))
@admin_only
async def cmd_remove_trigger(message: Message):
    try:
        chat_id = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /remove_trigger <chat_id>")
    cfg = await get_config()
    if chat_id in cfg["trigger_chat_ids"]:
        cfg["trigger_chat_ids"].remove(chat_id)
        await save_config(cfg)
    await message.answer("✅ Removed")

@dp.message(Command("set_message"))
@admin_only
async def cmd_set_message(message: Message):
    body = _args(message)
    if not body.strip():
        return await message.answer("Usage: /set_message <text with {name}>")
    cfg = await get_config()
    cfg["message_template"] = body
    await save_config(cfg)
    await message.answer("✅ Message updated")

@dp.message(Command("get_message"))
@admin_only
async def cmd_get_message(message: Message):
    cfg = await get_config()
    await message.answer(f"<b>Current:</b>\n\n{cfg['message_template']}")

@dp.message(Command("preview"))
@admin_only
async def cmd_preview(message: Message):
    cfg = await get_config()
    try:
        share_link = await get_share_link(message.bot, cfg)
        await message.answer(
            "<b>Preview:</b>\n\n" + format_message(cfg, mention_html(message.from_user)),
            reply_markup=build_keyboard(cfg, share_link),
        )
    except Exception as e:
        await message.answer(f"❌ {e}")

@dp.message(Command("buttons"))
@admin_only
async def cmd_buttons(message: Message):
    cfg = await get_config()
    if not cfg["buttons"]:
        return await message.answer("No buttons.")
    lines = ["<b>Buttons:</b>"]
    for i, b in enumerate(cfg["buttons"], 1):
        if b.get("type") == "share":
            lines.append(f"{i}. [share] {b['text']}")
        else:
            lines.append(f"{i}. [url] {b['text']} → {b.get('url','')}")
    await message.answer("\n".join(lines))

@dp.message(Command("add_button"))
@admin_only
async def cmd_add_button(message: Message):
    body = _args(message)
    if "|" not in body:
        return await message.answer("Usage: /add_button <text> | <url>")
    text, url = (x.strip() for x in body.split("|", 1))
    if not text or not url:
        return await message.answer("❌ Both required")
    cfg = await get_config()
    cfg["buttons"].append({"text": text, "type": "url", "url": url})
    await save_config(cfg)
    await message.answer(f"✅ Added: {text}")

@dp.message(Command("add_share_button"))
@admin_only
async def cmd_add_share_button(message: Message):
    body = _args(message).strip()
    if not body:
        return await message.answer("Usage: /add_share_button <text>")
    cfg = await get_config()
    cfg["buttons"].append({"text": body, "type": "share", "url": None})
    await save_config(cfg)
    await message.answer(f"✅ Added share button: {body}")

@dp.message(Command("remove_button"))
@admin_only
async def cmd_remove_button(message: Message):
    try:
        idx = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /remove_button <index>")
    cfg = await get_config()
    if idx < 1 or idx > len(cfg["buttons"]):
        return await message.answer("❌ Out of range")
    removed = cfg["buttons"].pop(idx - 1)
    await save_config(cfg)
    await message.answer(f"✅ Removed: {removed['text']}")

@dp.message(Command("set_interval"))
@admin_only
async def cmd_set_interval(message: Message):
    try:
        sec = int(_args(message).strip())
        if sec < 30:
            raise ValueError
    except ValueError:
        return await message.answer("Usage: /set_interval <seconds> (min 30)")
    cfg = await get_config()
    cfg["interval_seconds"] = sec
    await save_config(cfg)
    await message.answer(f"✅ Interval = {sec}s")

@dp.message(Command("set_share_text"))
@admin_only
async def cmd_set_share_text(message: Message):
    body = _args(message).strip()
    if not body:
        return await message.answer("Usage: /set_share_text <text>")
    cfg = await get_config()
    cfg["share_text"] = body
    await save_config(cfg)
    await message.answer("✅ Share text updated")

@dp.message(Command("set_share_chat"))
@admin_only
async def cmd_set_share_chat(message: Message):
    try:
        chat_id = int(_args(message).strip())
    except ValueError:
        return await message.answer("Usage: /set_share_chat <chat_id>")
    cfg = await get_config()
    cfg["share_chat_id"] = chat_id
    await save_config(cfg)
    await clear_cached_share_link()
    await message.answer(f"✅ Share source = {chat_id}")

@dp.message(Command("status"))
@admin_only
async def cmd_status(message: Message):
    cfg = await get_config()
    await message.answer(
        "<b>Status</b>\n"
        f"Triggers: {len(cfg['trigger_chat_ids'])}\n"
        f"Targets:  {len(cfg['targets'])}\n"
        f"Buttons:  {len(cfg['buttons'])}\n"
        f"Interval: {cfg['interval_seconds']}s\n"
        f"Last member: {cfg.get('last_member_name','?')}\n"
        f"Share chat: {cfg.get('share_chat_id') or 'auto'}"
    )

@dp.message(Command("refresh"))
@admin_only
async def cmd_refresh(message: Message):
    cfg = await get_config()
    res = await refresh_all(message.bot, cfg)
    ok = sum(1 for v in res.values() if v)
    await message.answer(f"🔄 Refreshed {ok}/{len(res)} targets")

# ======================================================================
# EVENT: new member
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

def _check_secret(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        # fall back to query param for /setup & /cron
        if request.query_params.get("secret", "") != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Bad secret")


@app.post("/api/webhook")
async def webhook(request: Request):
    _check_secret(request)
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
