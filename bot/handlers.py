import logging
import time
from functools import wraps

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import (
    ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER,
)
from aiogram.types import Message, ChatMemberUpdated

from .core import (
    get_config, save_config, is_admin,
    clear_cached_share_link, redis,
)
from .actions import (
    mention_html, refresh_target, refresh_all_targets,
    get_share_link, build_keyboard, format_message,
)

log = logging.getLogger("bot.handlers")


# ---------------- decorators ----------------
def admin_only(func):
    @wraps(func)
    async def wrapper(message: Message, *a, **kw):
        if not await is_admin(message.from_user.id):
            await message.answer("⛔ You are not an admin.")
            return
        return await func(message, *a, **kw)
    return wrapper


def _args(message: Message) -> tuple[str, str]:
    """Return (command, everything-after-command)."""
    parts = (message.text or "").split(maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


# ================================================================
# USER HANDLERS
# ================================================================
async def cmd_start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    cfg = await get_config()

    # delete previous DM (keep DM clean)
    prev = await redis.get(f"bot:dm:{message.from_user.id}")
    if prev:
        try:
            await message.bot.delete_message(message.chat.id, int(prev))
        except Exception:
            pass

    try:
        share_link = await get_share_link(message.bot, cfg)
        kb = build_keyboard(cfg, share_link)
        text = format_message(cfg, mention_html(message.from_user))
        sent = await message.answer(text, reply_markup=kb)
        await redis.set(f"bot:dm:{message.from_user.id}", sent.message_id, ex=86400)
    except Exception as e:
        log.error(f"/start error: {e}")


# ================================================================
# ADMIN HANDLERS
# ================================================================
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
        "/set_message &lt;text&gt;  (use <code>{name}</code>)\n"
        "/get_message\n"
        "/preview\n\n"
        "<b>Buttons:</b>\n"
        "/buttons\n"
        "/add_button &lt;text&gt; | &lt;url&gt;\n"
        "/add_share_button &lt;text&gt;\n"
        "/remove_button &lt;index&gt;\n\n"
        "<b>Settings:</b>\n"
        "/set_interval &lt;seconds&gt;  (min 30)\n"
        "/set_share_text &lt;text&gt;\n"
        "/set_share_chat &lt;chat_id&gt;\n"
        "/refresh — force refresh all targets\n"
        "/status\n\n"
        "<b>Extra admin:</b> /add_admin &lt;user_id&gt;"
    )


@admin_only
async def cmd_add_admin(message: Message):
    _, body = _args(message)
    try:
        uid = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /add_admin <user_id>")
    cfg = await get_config()
    if uid not in cfg["extra_admin_ids"]:
        cfg["extra_admin_ids"].append(uid)
        await save_config(cfg)
    await message.answer(f"✅ Added extra admin: <code>{uid}</code>")


# ---------- targets ----------
@admin_only
async def cmd_targets(message: Message):
    cfg = await get_config()
    if not cfg["targets"]:
        return await message.answer("No targets yet. Use /add_target <chat_id>")
    lines = ["<b>Targets:</b>"]
    for i, t in enumerate(cfg["targets"], 1):
        lines.append(
            f"{i}. <code>{t['chat_id']}</code> — "
            f"{t.get('title','?')} ({t.get('type','?')})"
        )
    await message.answer("\n".join(lines))


@admin_only
async def cmd_add_target(message: Message):
    _, body = _args(message)
    try:
        chat_id = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /add_target <chat_id>")
    try:
        chat = await message.bot.get_chat(chat_id)
    except Exception as e:
        return await message.answer(f"❌ Cannot access chat: {e}")
    cfg = await get_config()
    if any(t["chat_id"] == chat_id for t in cfg["targets"]):
        return await message.answer("⚠️ Already in targets")
    cfg["targets"].append({
        "chat_id": chat_id,
        "title": chat.title or str(chat_id),
        "type": chat.type,
    })
    await save_config(cfg)
    await message.answer(f"✅ Added target: {chat.title} (<code>{chat_id}</code>)")


@admin_only
async def cmd_remove_target(message: Message):
    _, body = _args(message)
    try:
        chat_id = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /remove_target <chat_id>")
    cfg = await get_config()
    before = len(cfg["targets"])
    cfg["targets"] = [t for t in cfg["targets"] if t["chat_id"] != chat_id]
    if len(cfg["targets"]) == before:
        return await message.answer("⚠️ Not found")
    await save_config(cfg)
    await message.answer(f"✅ Removed target {chat_id}")


# ---------- trigger chats ----------
@admin_only
async def cmd_add_trigger(message: Message):
    _, body = _args(message)
    try:
        chat_id = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /add_trigger <chat_id>")
    cfg = await get_config()
    if chat_id not in cfg["trigger_chat_ids"]:
        cfg["trigger_chat_ids"].append(chat_id)
        await save_config(cfg)
    await message.answer(f"✅ Added trigger chat {chat_id}")


@admin_only
async def cmd_remove_trigger(message: Message):
    _, body = _args(message)
    try:
        chat_id = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /remove_trigger <chat_id>")
    cfg = await get_config()
    if chat_id in cfg["trigger_chat_ids"]:
        cfg["trigger_chat_ids"].remove(chat_id)
        await save_config(cfg)
    await message.answer(f"✅ Removed trigger chat {chat_id}")


# ---------- message ----------
@admin_only
async def cmd_set_message(message: Message):
    _, body = _args(message)
    if not body.strip():
        return await message.answer("Usage: /set_message <text with {name}>")
    cfg = await get_config()
    cfg["message_template"] = body
    await save_config(cfg)
    await message.answer("✅ Message template updated.")


@admin_only
async def cmd_get_message(message: Message):
    cfg = await get_config()
    await message.answer(f"<b>Current message:</b>\n\n{cfg['message_template']}")


@admin_only
async def cmd_preview(message: Message):
    cfg = await get_config()
    try:
        share_link = await get_share_link(message.bot, cfg)
        kb = build_keyboard(cfg, share_link)
        text = format_message(cfg, mention_html(message.from_user))
        await message.answer("<b>Preview:</b>\n\n" + text, reply_markup=kb)
    except Exception as e:
        await message.answer(f"❌ {e}")


# ---------- buttons ----------
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


@admin_only
async def cmd_add_button(message: Message):
    _, body = _args(message)
    if "|" not in body:
        return await message.answer("Usage: /add_button <text> | <url>")
    text, url = (x.strip() for x in body.split("|", 1))
    if not text or not url:
        return await message.answer("❌ Both text and url required")
    cfg = await get_config()
    cfg["buttons"].append({"text": text, "type": "url", "url": url})
    await save_config(cfg)
    await message.answer(f"✅ Added URL button: {text}")


@admin_only
async def cmd_add_share_button(message: Message):
    _, body = _args(message)
    if not body.strip():
        return await message.answer("Usage: /add_share_button <text>")
    cfg = await get_config()
    cfg["buttons"].append({"text": body.strip(), "type": "share", "url": None})
    await save_config(cfg)
    await message.answer(f"✅ Added share button: {body.strip()}")


@admin_only
async def cmd_remove_button(message: Message):
    _, body = _args(message)
    try:
        idx = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /remove_button <index>")
    cfg = await get_config()
    if idx < 1 or idx > len(cfg["buttons"]):
        return await message.answer("❌ Index out of range")
    removed = cfg["buttons"].pop(idx - 1)
    await save_config(cfg)
    await message.answer(f"✅ Removed button: {removed['text']}")


# ---------- settings ----------
@admin_only
async def cmd_set_interval(message: Message):
    _, body = _args(message)
    try:
        sec = int(body.strip())
        if sec < 30:
            raise ValueError
    except ValueError:
        return await message.answer("Usage: /set_interval <seconds> (min 30)")
    cfg = await get_config()
    cfg["interval_seconds"] = sec
    await save_config(cfg)
    await message.answer(f"✅ Interval = {sec}s")


@admin_only
async def cmd_set_share_text(message: Message):
    _, body = _args(message)
    if not body.strip():
        return await message.answer("Usage: /set_share_text <text>")
    cfg = await get_config()
    cfg["share_text"] = body.strip()
    await save_config(cfg)
    await message.answer("✅ Share text updated.")


@admin_only
async def cmd_set_share_chat(message: Message):
    _, body = _args(message)
    try:
        chat_id = int(body.strip())
    except ValueError:
        return await message.answer("Usage: /set_share_chat <chat_id>")
    cfg = await get_config()
    cfg["share_chat_id"] = chat_id
    await save_config(cfg)
    await clear_cached_share_link()
    await message.answer(f"✅ Share source set to {chat_id}")


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


@admin_only
async def cmd_refresh(message: Message):
    cfg = await get_config()
    results = await refresh_all_targets(message.bot, cfg)
    ok = sum(1 for v in results.values() if v)
    await message.answer(f"🔄 Refreshed {ok}/{len(results)} targets.")


# ================================================================
# EVENT HANDLERS
# ================================================================
async def new_member(event: ChatMemberUpdated):
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

    log.info(f"New member {user.full_name} in {event.chat.id} — refreshing targets")
    await refresh_all_targets(event.bot, cfg, name_html)


# ================================================================
# SETUP
# ================================================================
def setup(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())

    # admin
    dp.message.register(cmd_admin,          Command("admin"))
    dp.message.register(cmd_add_admin,      Command("add_admin"))
    dp.message.register(cmd_targets,        Command("targets"))
    dp.message.register(cmd_add_target,     Command("add_target"))
    dp.message.register(cmd_remove_target,  Command("remove_target"))
    dp.message.register(cmd_add_trigger,    Command("add_trigger"))
    dp.message.register(cmd_remove_trigger, Command("remove_trigger"))
    dp.message.register(cmd_set_message,    Command("set_message"))
    dp.message.register(cmd_get_message,    Command("get_message"))
    dp.message.register(cmd_preview,        Command("preview"))
    dp.message.register(cmd_buttons,        Command("buttons"))
    dp.message.register(cmd_add_button,     Command("add_button"))
    dp.message.register(cmd_add_share_button, Command("add_share_button"))
    dp.message.register(cmd_remove_button,  Command("remove_button"))
    dp.message.register(cmd_set_interval,   Command("set_interval"))
    dp.message.register(cmd_set_share_text, Command("set_share_text"))
    dp.message.register(cmd_set_share_chat, Command("set_share_chat"))
    dp.message.register(cmd_status,         Command("status"))
    dp.message.register(cmd_refresh,        Command("refresh"))

    # events
    dp.chat_member.register(
        new_member,
        ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER),
    )
