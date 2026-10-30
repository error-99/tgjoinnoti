import os
import json
import logging
from typing import Any

log = logging.getLogger("bot.core")

# ---------------- env ----------------
BOT_TOKEN        = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")
ADMIN_IDS        = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPSTASH_URL      = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN    = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not UPSTASH_URL or not UPSTASH_TOKEN:
    raise RuntimeError("UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required")

# ---------------- redis ----------------
from upstash_redis.asyncio import Redis
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)


# ---------------- defaults ----------------
DEFAULT_CONFIG: dict[str, Any] = {
    "trigger_chat_ids": [],          # chats where we watch for new members
    "targets": [],                   # [{chat_id, title, type}] where we post
    "message_template": (
        "প্রিয় {name}, আপনাকে হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি ৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    ),
    "buttons": [
        {"text": "📤 শেয়ার করুন", "type": "share", "url": None},
    ],
    "share_text": "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️",
    "share_chat_id": None,           # chat whose invite link is shared (default: first trigger/first target)
    "interval_seconds": 240,
    "last_member_name": "বন্ধু",
    "last_member_joined_at": 0,
    "extra_admin_ids": [],
}


# ---------------- config helpers ----------------
async def get_config() -> dict:
    raw = await redis.get("bot:config")
    if not raw:
        cfg = dict(DEFAULT_CONFIG)
        await save_config(cfg)
        return cfg
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    else:
        data = raw
    # merge defaults for forward-compat
    for k, v in DEFAULT_CONFIG.items():
        data.setdefault(k, v)
    return data


async def save_config(cfg: dict) -> None:
    await redis.set("bot:config", json.dumps(cfg, ensure_ascii=False))


# ---------------- admin helpers ----------------
async def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    cfg = await get_config()
    return user_id in cfg.get("extra_admin_ids", [])


# ---------------- per-target helpers ----------------
async def get_target_message_id(chat_id: int) -> int | None:
    v = await redis.get(f"bot:target:{chat_id}:message_id")
    return int(v) if v is not None else None


async def set_target_message_id(chat_id: int, message_id: int) -> None:
    await redis.set(f"bot:target:{chat_id}:message_id", message_id)


async def get_target_last_sent(chat_id: int) -> int:
    v = await redis.get(f"bot:target:{chat_id}:last_sent_at")
    return int(v) if v is not None else 0


async def set_target_last_sent(chat_id: int, ts: int) -> None:
    await redis.set(f"bot:target:{chat_id}:last_sent_at", ts)


# ---------------- invite-link cache ----------------
async def get_cached_share_link() -> str | None:
    v = await redis.get("bot:share_link")
    if not v:
        return None
    return v if isinstance(v, str) else str(v)


async def set_cached_share_link(link: str, ttl: int = 3600) -> None:
    await redis.set("bot:share_link", link, ex=ttl)


async def clear_cached_share_link() -> None:
    await redis.delete("bot:share_link")


# ---------------- bot factory ----------------
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


def create_bot() -> Bot:
    return Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
