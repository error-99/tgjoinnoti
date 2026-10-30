import logging
import time
from html import escape
from urllib.parse import quote

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError

from .core import (
    get_config, get_target_message_id, set_target_message_id,
    set_target_last_sent, get_cached_share_link, set_cached_share_link,
)

log = logging.getLogger("bot.actions")


# ---------------- helpers ----------------
def mention_html(user) -> str:
    return f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'


def format_message(cfg: dict, name_html: str | None = None) -> str:
    name = name_html or cfg.get("last_member_name", "বন্ধু")
    return cfg.get("message_template", "").replace("{name}", name)


async def get_share_link(bot: Bot, cfg: dict, force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = await get_cached_share_link()
        if cached:
            return cached

    chat_id = (
        cfg.get("share_chat_id")
        or (cfg["trigger_chat_ids"][0] if cfg.get("trigger_chat_ids") else None)
        or (cfg["targets"][0]["chat_id"] if cfg.get("targets") else None)
    )
    if not chat_id:
        raise RuntimeError("No share chat configured")

    chat = await bot.get_chat(chat_id)
    if chat.username:
        link = f"https://t.me/{chat.username}"
    elif chat.invite_link:
        link = chat.invite_link
    else:
        invite = await bot.create_chat_invite_link(chat_id=chat_id, name="Share link")
        link = invite.invite_link

    await set_cached_share_link(link)
    return link


def build_keyboard(cfg: dict, share_link: str) -> InlineKeyboardMarkup | None:
    share_text = cfg.get("share_text", "")
    share_url = (
        f"https://t.me/share/url"
        f"?url={quote(share_link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for btn in cfg.get("buttons", []):
        text = (btn.get("text") or "").strip()
        if not text:
            continue
        if btn.get("type") == "share":
            url = share_url
        else:
            url = (btn.get("url") or "").strip()
        if not url:
            continue
        rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ---------------- core actions ----------------
async def refresh_target(
    bot: Bot,
    chat_id: int,
    cfg: dict | None = None,
    name_html: str | None = None,
) -> bool:
    cfg = cfg or await get_config()
    name_html = name_html or cfg.get("last_member_name", "বন্ধু")

    # delete previous promotion
    old_id = await get_target_message_id(chat_id)
    if old_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_id)
        except TelegramAPIError as e:
            log.debug(f"Could not delete old message in {chat_id}: {e}")

    try:
        share_link = await get_share_link(bot, cfg)
    except Exception as e:
        log.error(f"Could not get share link: {e}")
        return False

    keyboard = build_keyboard(cfg, share_link)
    text = format_message(cfg, name_html)

    try:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        await set_target_message_id(chat_id, sent.message_id)
        await set_target_last_sent(chat_id, int(time.time()))
        log.info(f"Sent promotion → {chat_id} (msg={sent.message_id})")
        return True
    except TelegramAPIError as e:
        log.error(f"Failed to send to {chat_id}: {e}")
        return False


async def refresh_all_targets(
    bot: Bot,
    cfg: dict | None = None,
    name_html: str | None = None,
) -> dict[int, bool]:
    cfg = cfg or await get_config()
    results: dict[int, bool] = {}
    for target in cfg.get("targets", []):
        cid = target["chat_id"]
        results[cid] = await refresh_target(bot, cid, cfg, name_html)
    return results
