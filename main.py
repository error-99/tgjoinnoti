import os
import io
import json
import time
import hmac
import asyncio
import logging
from html import escape
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import (
    ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, Message, LinkPreviewOptions,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

# ======================================================================
# CONFIGURATION
# ======================================================================
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "secret123")
ADMIN_IDS    = [int(x) for x in os.getenv("ADMIN_IDS", "5087403859").split(",") if x.strip()]
PORT         = int(os.getenv("PORT", "8000"))
PUBLIC_URL   = os.getenv("PUBLIC_URL", "")
STATE_FILE   = os.getenv("STATE_FILE", "bot_state.json")

if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
if ADMIN_SECRET == "secret123":
    log.warning("ADMIN_SECRET is still the default value. Set a strong ADMIN_SECRET env var!")

# Telegram button colours (Bot API: InlineKeyboardButton.style)
#   primary = blue, success = green, danger = red, "" = default
STYLES = ("primary", "success", "danger")

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

ME = None


async def get_me():
    global ME
    if ME is None:
        ME = await bot.get_me()
    return ME


# ======================================================================
# STATE
# ======================================================================
def default_group() -> dict:
    return {
        "title": "",
        "type": "",
        "username": "",
        "invite_link": "",
        "is_admin": False,
        "can_invite": False,
        "enabled": True,
        "interval": 300,
        "delete_old": True,
        "last_sent_time": 0,
        "last_member": "",
        "last_member_name": "",
        "last_msg_ids": [],
        "last_error": "",
        "msg_text": "নতুন সদস্য {name} যোগদান করেছেন! ❤️\n\nভিডিও দেখতে নিচের লিংকটি ৩টি গ্রুপে শেয়ার করুন।",
        "photo": "",
        "buttons": [
            {"text": "✅ গ্রুপে যোগ দিন", "url": "{shareurl}", "kind": "link",
             "style": "primary", "share_text": "", "same_row": False, "emoji_id": ""},
            {"text": "📤 শেয়ার করুন", "url": "{shareurl}", "kind": "share",
             "style": "success", "share_text": "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️",
             "same_row": False, "emoji_id": ""},
        ],
    }


def default_start() -> dict:
    return {
        "text": "স্বাগতম! আমাদের বট ব্যবহার করার জন্য ধন্যবাদ। ❤️",
        "photo": "",
        "buttons": [],
    }


def sanitize_buttons(raw) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for b in raw[:20]:
        if not isinstance(b, dict):
            continue
        style = b.get("style") or ""
        out.append({
            "text": str(b.get("text", "")).strip()[:64],
            "url": str(b.get("url", "")).strip()[:1000],
            "kind": "share" if b.get("kind") == "share" else "link",
            "style": style if style in STYLES else "",
            "share_text": str(b.get("share_text", ""))[:500],
            "same_row": bool(b.get("same_row")),
            "emoji_id": str(b.get("emoji_id", "")).strip()[:40],
        })
    return out


def migrate(data) -> dict:
    """Accepts new-format or old-format (v1) state and returns the new format."""
    data = data if isinstance(data, dict) else {}

    if "groups" not in data:
        groups = {}
        for cid in data.get("targets", []):
            g = default_group()
            g["msg_text"] = data.get("target_msg_text", g["msg_text"]).replace("{name}", "{name}")
            g["interval"] = int(data.get("interval", 300) or 300)
            btns = []
            for i in (1, 2):
                if data.get(f"share_{i}_url"):
                    btns.append({"text": data.get(f"share_{i}_text", "Share"),
                                 "url": data[f"share_{i}_url"], "kind": "share", "style": "",
                                 "share_text": data.get("share_msg", ""), "same_row": False, "emoji_id": ""})
            for b in data.get("custom_buttons", []):
                btns.append({"text": b.get("text", ""), "url": b.get("url", ""), "kind": "link",
                             "style": "", "share_text": "", "same_row": False, "emoji_id": ""})
            g["buttons"] = btns or g["buttons"]
            groups[str(cid)] = g
        data["groups"] = groups

    if "start" not in data:
        st = default_start()
        if "dm_text" in data:
            st["text"] = data["dm_text"]
            st["buttons"] = [{"text": b.get("text", ""), "url": b.get("url", ""), "kind": "link",
                              "style": "", "share_text": "", "same_row": False, "emoji_id": ""}
                             for b in data.get("dm_buttons", [])]
        data["start"] = st

    for k, v in default_start().items():
        data["start"].setdefault(k, v)
    for g in data["groups"].values():
        for k, v in default_group().items():
            g.setdefault(k, v)

    # keep only the new-format keys
    return {"groups": data["groups"], "start": data["start"]}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return migrate(json.load(f))
        except Exception:
            log.exception("Could not read state file, starting fresh")
    return migrate({})


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


STATE: dict = load_state()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def auth(secret: str):
    if not secret or not hmac.compare_digest(secret.encode(), ADMIN_SECRET.encode()):
        raise HTTPException(403, "Unauthorized")


def base_url() -> str:
    return PUBLIC_URL.rstrip("/") if PUBLIC_URL else "https://tgjoinnoti-b0gj.onrender.com"


# ======================================================================
# KEYBOARD + MESSAGE SENDING
# ======================================================================
def build_markup(buttons: list, ctx: dict, use_style: bool = True):
    """Builds the inline keyboard. {shareurl} in a button URL = the group's join link."""
    rows = []
    for b in buttons or []:
        text = (b.get("text") or "").strip()
        url = (b.get("url") or "").strip().replace("{shareurl}", ctx.get("shareurl", ""))
        if not text or not url:
            continue
        if not url.startswith(("http://", "https://", "tg://")):
            url = "https://" + url
        if b.get("kind") == "share":
            url = ("https://t.me/share/url?url=" + quote(url, safe="")
                   + "&text=" + quote(b.get("share_text", "") or "", safe=""))
        extra = {}
        if use_style:
            if b.get("style") in STYLES:
                extra["style"] = b["style"]
            if b.get("emoji_id"):
                extra["icon_custom_emoji_id"] = b["emoji_id"]
        btn = InlineKeyboardButton(text=text, url=url, **extra)
        if b.get("same_row") and rows:
            rows[-1].append(btn)
        else:
            rows.append([btn])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_once(chat_id, text, photo, markup, parse_mode) -> list:
    ids = []
    nopreview = LinkPreviewOptions(is_disabled=True)
    try:
        if photo:
            if len(text) <= 1024:
                m = await bot.send_photo(chat_id, photo, caption=text or None,
                                         reply_markup=markup, parse_mode=parse_mode)
                ids.append(m.message_id)
            else:  # caption too long: photo first, then text + buttons
                m1 = await bot.send_photo(chat_id, photo)
                ids.append(m1.message_id)
                m2 = await bot.send_message(chat_id, text, reply_markup=markup,
                                            parse_mode=parse_mode, link_preview_options=nopreview)
                ids.append(m2.message_id)
        else:
            m = await bot.send_message(chat_id, text or "👋", reply_markup=markup,
                                       parse_mode=parse_mode, link_preview_options=nopreview)
            ids.append(m.message_id)
    except Exception:
        for mid in ids:  # don't leave half-sent messages behind
            try:
                await bot.delete_message(chat_id, mid)
            except TelegramAPIError:
                pass
        raise
    return ids


async def send_rich(chat_id, text: str, photo: str, buttons: list, ctx: dict) -> list:
    """Sends text/photo + coloured buttons. Falls back if Telegram rejects the colours or the HTML."""
    attempts = [(True, ParseMode.HTML), (False, ParseMode.HTML), (False, None)]
    last = None
    for use_style, pm in attempts:
        markup = build_markup(buttons, ctx, use_style)
        try:
            return await _send_once(chat_id, text, photo, markup, pm)
        except TelegramBadRequest as e:
            last = e
            log.warning("send to %s failed (style=%s, parse=%s): %s", chat_id, use_style, pm, e)
    raise last


# ======================================================================
# GROUP HELPERS
# ======================================================================
def parse_chat_ref(raw):
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("empty")
    for p in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(p):
            raw = "@" + raw[len(p):].split("/")[0].split("?")[0]
            break
    try:
        return int(raw)
    except ValueError:
        return raw if raw.startswith("@") else "@" + raw


async def check_bot_status(chat_ref):
    """Returns (chat, is_admin, can_invite). Raises TelegramAPIError if the bot can't see the chat."""
    chat = await bot.get_chat(chat_ref)
    me = await get_me()
    member = await bot.get_chat_member(chat.id, me.id)
    status = member.status
    is_adm = status in ("administrator", "creator")
    can_invite = status == "creator" or bool(getattr(member, "can_invite_users", False))
    return chat, is_adm, can_invite


def apply_chat(g: dict, chat, is_adm: bool, can_invite: bool):
    g["title"] = getattr(chat, "title", None) or getattr(chat, "full_name", "") or str(chat.id)
    g["type"] = chat.type
    g["username"] = getattr(chat, "username", None) or ""
    g["is_admin"] = is_adm
    g["can_invite"] = can_invite


async def ensure_invite(g: dict, chat, can_invite: bool, force: bool = False):
    """Finds the group's join link: public @username, else a bot-created invite link."""
    if getattr(chat, "username", None):
        g["invite_link"] = f"https://t.me/{chat.username}"
        return
    if force:
        g["invite_link"] = ""
    if g.get("invite_link"):
        return
    if can_invite:
        try:
            link = await bot.create_chat_invite_link(chat.id, name="Auto-post link")
            g["invite_link"] = link.invite_link
            return
        except TelegramAPIError as e:
            log.warning("create_chat_invite_link failed for %s: %s", chat.id, e)
    g["invite_link"] = getattr(chat, "invite_link", None) or ""


def render_text(g: dict, member_html: str) -> str:
    text = g.get("msg_text", "")
    return (text.replace("{name}", member_html)
                .replace("{shareurl}", escape(g.get("invite_link", "")))
                .replace("{group}", escape(g.get("title", ""))))


async def post_to_group(key: str, member_html: str, member_plain: str = "") -> str:
    """Posts the configured message into one group. Returns "" on success or an error string."""
    g = STATE["groups"].get(key)
    if not g:
        return "Group not found"
    chat_id = int(key)

    if g.get("delete_old", True):
        for mid in g.get("last_msg_ids", []):
            try:
                await bot.delete_message(chat_id, mid)
            except TelegramAPIError:
                pass
        g["last_msg_ids"] = []

    text = render_text(g, member_html or escape(member_plain) or "বন্ধু")
    ctx = {"shareurl": g.get("invite_link", "")}
    try:
        ids = await send_rich(chat_id, text, g.get("photo", ""), g.get("buttons", []), ctx)
    except TelegramAPIError as e:
        g["last_error"] = str(getattr(e, "message", e))[:200]
        save_state()
        log.error("Failed to post to %s: %s", key, e)
        return g["last_error"]

    g["last_msg_ids"] = ids
    g["last_member"] = member_html
    g["last_member_name"] = member_plain
    g["last_sent_time"] = int(time.time())
    g["last_error"] = ""
    save_state()
    return ""


# ======================================================================
# BACKGROUND TASKS
# ======================================================================
async def auto_sender_task():
    """Re-posts in every group when that group's own timer runs out."""
    while True:
        await asyncio.sleep(15)
        try:
            now = int(time.time())
            for key, g in list(STATE["groups"].items()):
                if not g.get("enabled") or not g.get("is_admin"):
                    continue
                last = g.get("last_sent_time", 0)
                if last and now - last >= max(30, int(g.get("interval", 300))):
                    err = await post_to_group(key, g.get("last_member", ""), g.get("last_member_name", ""))
                    if err:  # avoid hammering a broken group; retry next interval
                        g["last_sent_time"] = now
                        save_state()
        except Exception:
            log.exception("auto_sender_task error")


async def auto_backup_task():
    while True:
        await asyncio.sleep(3600)
        try:
            data = json.dumps(STATE, ensure_ascii=False, indent=2).encode("utf-8")
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_document(
                        chat_id=admin_id,
                        document=BufferedInputFile(data, filename="bot_state.json"),
                        caption="🕒 <b>Hourly Auto-Backup</b>\nSend this file back to me to restore your settings!",
                    )
                except TelegramAPIError:
                    pass
        except Exception:
            log.exception("auto_backup_task error")


# ======================================================================
# TELEGRAM HANDLERS
# ======================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    st = STATE["start"]
    name = escape(message.from_user.first_name or "")
    text = st.get("text", "").replace("{name}", name)
    try:
        await send_rich(message.chat.id, text, st.get("photo", ""), st.get("buttons", []), {})
    except TelegramAPIError as e:
        log.error("/start send failed: %s", e)
        await message.answer(text or "👋")

    if is_admin(message.from_user.id):
        await message.answer(
            "⚙️ <b>Admin Controls</b>\n"
            f"👉 <a href='{base_url()}/admin?secret={quote(ADMIN_SECRET)}'>Open Web Dashboard</a>\n\n"
            "<i>/backup sends the config file. Send a .json backup file to restore.</i>",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "⚙️ <b>Bot Web Dashboard</b>\n\n"
        f"👉 <a href='{base_url()}/admin?secret={quote(ADMIN_SECRET)}'>Open Web Admin</a>",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if not is_admin(message.from_user.id):
        return
    data = json.dumps(STATE, ensure_ascii=False, indent=2).encode("utf-8")
    await message.answer_document(
        BufferedInputFile(data, filename="bot_state.json"),
        caption="📦 <b>Instant Backup File</b>\nSend it back here to restore.",
    )


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    user = event.new_chat_member.user
    if user.is_bot:
        return
    key = str(event.chat.id)
    g = STATE["groups"].get(key)
    if not g or not g.get("enabled"):
        return
    name_html = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
    await post_to_group(key, name_html, user.full_name)


@dp.my_chat_member()
async def on_my_status(event: ChatMemberUpdated):
    """Keeps the 'is admin' flag fresh when the bot is promoted/demoted."""
    key = str(event.chat.id)
    g = STATE["groups"].get(key)
    if not g:
        return
    new = event.new_chat_member
    g["is_admin"] = new.status in ("administrator", "creator")
    g["can_invite"] = new.status == "creator" or bool(getattr(new, "can_invite_users", False))
    if g["is_admin"]:
        await ensure_invite(g, event.chat, g["can_invite"])
    save_state()


@dp.message(F.document)
async def restore_backup(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not (message.document.file_name or "").endswith(".json"):
        return
    try:
        buf = io.BytesIO()
        await bot.download(message.document, destination=buf)
        buf.seek(0)
        new_state = migrate(json.load(buf))
        STATE.clear()
        STATE.update(new_state)
        save_state()
        await message.answer("✅ <b>Backup restored successfully!</b>")
    except Exception as e:
        await message.answer(f"❌ <b>Error restoring backup:</b>\n{escape(str(e))}")


# ======================================================================
# FASTAPI: LIFESPAN + API
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_me()
    await bot.delete_webhook(drop_pending_updates=True)
    tasks = [
        asyncio.create_task(dp.start_polling(
            bot, allowed_updates=["message", "chat_member", "my_chat_member"])),
        asyncio.create_task(auto_sender_task()),
        asyncio.create_task(auto_backup_task()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


def get_group_or_404(key: str) -> dict:
    g = STATE["groups"].get(key)
    if not g:
        raise HTTPException(404, "Group not found")
    return g


@app.get("/")
async def root():
    return RedirectResponse(url="/health")


@app.get("/health")
async def health_check():
    return JSONResponse(content={"status": "online"})


@app.get("/api/state")
async def api_state(secret: str = ""):
    auth(secret)
    me = await get_me()
    return {
        "bot": {"username": me.username, "name": me.full_name},
        "groups": STATE["groups"],
        "start": STATE["start"],
        "now": int(time.time()),
    }


@app.post("/api/groups")
async def api_add_group(request: Request, secret: str = ""):
    """Add a group/channel by ID and auto-check whether the bot is admin there."""
    auth(secret)
    body = await request.json()
    try:
        ref = parse_chat_ref(body.get("chat_id", ""))
    except ValueError:
        raise HTTPException(400, "Enter a group or channel ID")
    try:
        chat, is_adm, can_invite = await check_bot_status(ref)
    except TelegramAPIError as e:
        raise HTTPException(400, f"Telegram says: {getattr(e, 'message', e)}. "
                                 "Add the bot to that group/channel first, then try again.")
    key = str(chat.id)
    g = STATE["groups"].get(key) or default_group()
    apply_chat(g, chat, is_adm, can_invite)
    if is_adm:
        await ensure_invite(g, chat, can_invite)
    STATE["groups"][key] = g
    save_state()
    return {"ok": True, "key": key, "is_admin": is_adm, "group": g}


@app.post("/api/groups/{key}/recheck")
async def api_recheck(key: str, secret: str = "", relink: int = 0):
    auth(secret)
    g = get_group_or_404(key)
    try:
        chat, is_adm, can_invite = await check_bot_status(int(key))
    except TelegramAPIError as e:
        raise HTTPException(400, f"Telegram says: {getattr(e, 'message', e)}")
    apply_chat(g, chat, is_adm, can_invite)
    if is_adm:
        await ensure_invite(g, chat, can_invite, force=bool(relink))
    save_state()
    return g


@app.put("/api/groups/{key}")
async def api_update_group(key: str, request: Request, secret: str = ""):
    auth(secret)
    g = get_group_or_404(key)
    d = await request.json()
    try:
        interval = max(30, int(d.get("interval", g["interval"])))
    except (TypeError, ValueError):
        interval = g["interval"]
    g["enabled"] = bool(d.get("enabled", True))
    g["delete_old"] = bool(d.get("delete_old", True))
    g["interval"] = interval
    g["msg_text"] = str(d.get("msg_text", ""))[:4000]
    g["photo"] = str(d.get("photo", "")).strip()
    g["invite_link"] = str(d.get("invite_link", "")).strip()
    g["buttons"] = sanitize_buttons(d.get("buttons", []))
    save_state()
    return g


@app.delete("/api/groups/{key}")
async def api_delete_group(key: str, secret: str = ""):
    auth(secret)
    get_group_or_404(key)
    del STATE["groups"][key]
    save_state()
    return {"ok": True}


@app.post("/api/groups/{key}/send")
async def api_send_now(key: str, secret: str = ""):
    auth(secret)
    g = get_group_or_404(key)
    if not g.get("is_admin"):
        raise HTTPException(400, "Bot is not admin in this group")
    err = await post_to_group(key, g.get("last_member", ""), g.get("last_member_name", ""))
    if err:
        raise HTTPException(400, err)
    return g


@app.put("/api/start")
async def api_save_start(request: Request, secret: str = ""):
    auth(secret)
    d = await request.json()
    STATE["start"] = {
        "text": str(d.get("text", ""))[:4000],
        "photo": str(d.get("photo", "")).strip(),
        "buttons": sanitize_buttons(d.get("buttons", [])),
    }
    save_state()
    return STATE["start"]


@app.post("/api/upload")
async def api_upload(request: Request, secret: str = ""):
    """Upload an image. It is sent once to the first admin to get a permanent Telegram file_id."""
    auth(secret)
    data = await request.body()
    if not data or len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Choose an image under 10 MB")
    if not ADMIN_IDS:
        raise HTTPException(400, "Set ADMIN_IDS first")
    try:
        msg = await bot.send_photo(ADMIN_IDS[0], BufferedInputFile(data, filename="upload.jpg"),
                                   disable_notification=True)
    except TelegramAPIError as e:
        raise HTTPException(400, f"Upload failed: {getattr(e, 'message', e)}. "
                                 "Open the bot and press /start with the admin account first.")
    file_id = msg.photo[-1].file_id
    try:
        await bot.delete_message(ADMIN_IDS[0], msg.message_id)
    except TelegramAPIError:
        pass
    return {"file_id": file_id}


@app.get("/api/photo")
async def api_photo(file_id: str, secret: str = ""):
    auth(secret)
    try:
        f = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(f.file_path, destination=buf)
    except TelegramAPIError:
        raise HTTPException(404, "Image not found")
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/backup")
async def api_backup(secret: str = ""):
    auth(secret)
    data = json.dumps(STATE, ensure_ascii=False, indent=2)
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="bot_state.json"'})


# ======================================================================
# WEB DASHBOARD
# ======================================================================
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<title>Join Notifier</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js"></script>
<style>
:root{--bg:#0e1319;--panel:#151c25;--panel2:#1c2530;--line:#273343;--text:#e8edf3;--muted:#8b98a8;--accent:#4aa3ff;--ok:#34c778;--bad:#ff6b6b;--warn:#f5b84b}
html,body{background:var(--bg)}
body{font-family:"Hind Siliguri",system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text);-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.card{display:block;transition:border-color .15s,transform .15s}
.card:hover{border-color:var(--accent)}
.inp{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:9px 12px;color:var(--text)}
.inp:focus{border-color:var(--accent);outline:none}
textarea.inp{resize:vertical;line-height:1.5}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border-radius:10px;padding:9px 16px;font-weight:600;border:1px solid var(--line);background:var(--panel2);color:var(--text);cursor:pointer;transition:border-color .15s,opacity .15s}
.btn:hover{border-color:var(--accent)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#06121f}
.btn-danger{color:var(--bad)}
.btn-danger:hover{border-color:var(--bad)}
.btn-sm{padding:5px 10px;font-size:13px;border-radius:8px}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600}
.badge.ok{background:rgba(52,199,120,.14);color:var(--ok)}
.badge.bad{background:rgba(255,107,107,.14);color:var(--bad)}
.badge.off{background:rgba(139,152,168,.16);color:var(--muted)}
.muted{color:var(--muted)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.chip{border:1px dashed var(--line);border-radius:8px;padding:3px 9px;font-size:13px;cursor:pointer;background:transparent;color:var(--text)}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.swatch{width:26px;height:26px;border-radius:8px;border:2px solid transparent;cursor:pointer;padding:0}
.swatch.on{border-color:#fff;box-shadow:0 0 0 2px var(--bg)}
.btnrow{border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--bg)}
.bubble{background:#223040;border-radius:14px;padding:6px;overflow:hidden}
.bubble img{width:100%;border-radius:10px;display:block;margin-bottom:6px}
.bubble .bt{padding:4px 8px 8px;font-size:14.5px;line-height:1.45;word-break:break-word}
.mention{color:#6ab3f3}
.kb{display:flex;flex-direction:column;gap:4px;margin-top:4px}
.kbrow{display:flex;gap:4px}
.kbbtn{flex:1;text-align:center;padding:8px 10px;border-radius:9px;color:#fff;font-size:14px;font-weight:500;word-break:break-word}
.toast{position:fixed;bottom:18px;right:18px;z-index:50;padding:11px 18px;border-radius:10px;font-weight:600;color:#06121f;background:var(--ok)}
.toast.bad{background:var(--bad)}
.savebar{position:sticky;bottom:0;margin-top:20px;padding:12px 0;background:linear-gradient(transparent,var(--bg) 35%);display:flex;justify-content:flex-end}
</style>
</head>
<body>
<div id="app"></div>

<script>
const SECRET = __SECRET_JSON__;
const { createApp, ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;

async function api(path, opts = {}) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch('/api' + path + sep + 'secret=' + encodeURIComponent(SECRET), opts);
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((data && data.detail) || ('Error ' + res.status));
  return data;
}
const J = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

const STYLES = [
  { id: '', label: 'Default (grey)', color: '#3a4a5e' },
  { id: 'primary', label: 'Blue', color: '#2563eb' },
  { id: 'success', label: 'Green', color: '#16a34a' },
  { id: 'danger', label: 'Red', color: '#dc2626' },
];
const styleColor = s => (STYLES.find(x => x.id === (s || '')) || STYLES[0]).color;
const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const photoSrc = p => !p ? '' : (/^https?:/.test(p) ? p : '/api/photo?file_id=' + encodeURIComponent(p) + '&secret=' + encodeURIComponent(SECRET));

function renderPreview(text, ctx) {
  let h = esc(text);
  h = h.replace(/&lt;(\/?)(b|i|u|s|em|strong|code|pre)&gt;/g, '<$1$2>');
  h = h.replace(/&lt;a href=&quot;(.*?)&quot;&gt;/g, '<a href="$1" onclick="return false">').replace(/&lt;\/a&gt;/g, '</a>');
  h = h.split('{name}').join('<span class="mention">' + esc(ctx.name) + '</span>');
  h = h.split('{shareurl}').join(esc(ctx.link));
  h = h.split('{group}').join(esc(ctx.group));
  return h.replace(/\n/g, '<br>');
}

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return ((h ? h + 'h ' : '') + (m ? m + 'm ' : '') + ((!h && x) ? x + 's' : '')).trim() || '0s';
}

/* ---------- Shared editor: text + image + coloured buttons + live preview ---------- */
const MsgEditor = {
  props: { model: Object, placeholders: Array, ctx: Object, groupMode: Boolean },
  setup(props) {
    const ta = ref(null), uploading = ref(false), err = ref('');
    const insert = tag => {
      const el = ta.value, m = props.model;
      if (!el) { m.text += tag; return; }
      const s = el.selectionStart == null ? m.text.length : el.selectionStart;
      const e = el.selectionEnd == null ? s : el.selectionEnd;
      m.text = m.text.slice(0, s) + tag + m.text.slice(e);
      nextTick(() => { el.focus(); el.setSelectionRange(s + tag.length, s + tag.length); });
    };
    const upload = async ev => {
      const f = ev.target.files[0];
      if (!f) return;
      uploading.value = true; err.value = '';
      try {
        const d = await api('/upload', { method: 'POST', headers: { 'Content-Type': f.type || 'application/octet-stream' }, body: f });
        props.model.photo = d.file_id;
      } catch (e) { err.value = e.message; }
      uploading.value = false; ev.target.value = '';
    };
    const addBtn = (preset = {}) => props.model.buttons.push(Object.assign(
      { text: '', url: '', kind: 'link', style: '', share_text: '', same_row: false, emoji_id: '' }, preset));
    const move = (i, d) => { const a = props.model.buttons, j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; };
    const rows = computed(() => {
      const out = [];
      for (const b of props.model.buttons) {
        if (!b.text) continue;
        if (b.same_row && out.length) out[out.length - 1].push(b); else out.push([b]);
      }
      return out;
    });
    const html = computed(() => renderPreview(props.model.text, props.ctx));
    const src = computed(() => photoSrc(props.model.photo));
    return { ta, uploading, err, insert, upload, addBtn, move, rows, html, src, STYLES, styleColor };
  },
  template: `
  <div class="grid lg:grid-cols-5 gap-6 items-start">
    <div class="lg:col-span-3 space-y-5">
      <section class="panel">
        <h3 class="font-semibold mb-1">Message text</h3>
        <p class="muted text-sm mb-3">Click a tag to insert it where your cursor is. Basic HTML works: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, &lt;a href="…"&gt;link&lt;/a&gt;.</p>
        <div class="flex flex-wrap gap-2 mb-3">
          <button v-for="p in placeholders" :key="p.tag" class="chip" @click="insert(p.tag)" :title="p.hint">{{ p.tag }}</button>
        </div>
        <textarea ref="ta" v-model="model.text" rows="6" class="inp"></textarea>
        <ul class="muted text-sm mt-3 space-y-1">
          <li v-for="p in placeholders" :key="'h'+p.tag"><span class="mono">{{ p.tag }}</span> – {{ p.hint }}</li>
        </ul>
      </section>

      <section class="panel">
        <h3 class="font-semibold mb-3">Image (optional)</h3>
        <div class="flex flex-wrap items-center gap-3 mb-3">
          <label class="btn btn-sm">
            {{ uploading ? 'Uploading…' : 'Upload image' }}
            <input type="file" accept="image/*" class="hidden" @change="upload" :disabled="uploading">
          </label>
          <button v-if="model.photo" class="btn btn-sm btn-danger" @click="model.photo=''">Remove image</button>
          <span v-if="err" class="text-sm" style="color:var(--bad)">{{ err }}</span>
        </div>
        <input class="inp" v-model="model.photo" placeholder="…or paste an image URL (https://…)">
        <p v-if="model.photo" class="muted text-sm mt-2">With an image, the text becomes the caption (max 1024 characters, longer text is sent as a second message).</p>
      </section>

      <section class="panel">
        <div class="flex flex-wrap items-center justify-between gap-2 mb-1">
          <h3 class="font-semibold">Buttons</h3>
          <div class="flex flex-wrap gap-2">
            <button v-if="groupMode" class="btn btn-sm" @click="addBtn({text:'✅ Join group',url:'{shareurl}',style:'primary'})">+ Join-link button</button>
            <button v-if="groupMode" class="btn btn-sm" @click="addBtn({text:'📤 Share',url:'{shareurl}',kind:'share',style:'success',share_text:'Join this group ❤️'})">+ Share button</button>
            <button class="btn btn-sm" @click="addBtn()">+ Custom button</button>
          </div>
        </div>
        <p class="muted text-sm mb-4">
          <template v-if="groupMode">Use <span class="mono">{shareurl}</span> as the URL to insert this group's join link automatically. </template>
          "Share link" opens Telegram's forward screen with the URL. Colours follow Telegram's button styles.
        </p>
        <p v-if="!model.buttons.length" class="muted text-sm">No buttons yet.</p>
        <div class="space-y-3">
          <div v-for="(b,i) in model.buttons" :key="i" class="btnrow space-y-3">
            <div class="flex flex-col sm:flex-row gap-2">
              <input class="inp sm:w-2/5" v-model="b.text" placeholder="Button text">
              <input class="inp" v-model="b.url" :placeholder="groupMode ? 'https://…  or  {shareurl}' : 'https://t.me/…'">
            </div>
            <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
              <div class="flex items-center gap-2" role="radiogroup" aria-label="Button colour">
                <button v-for="s in STYLES" :key="s.id" class="swatch" :class="{on:(b.style||'')===s.id}"
                  :style="{background:s.color}" :title="s.label" :aria-label="s.label" @click="b.style=s.id"></button>
              </div>
              <select class="inp" style="width:auto" v-model="b.kind">
                <option value="link">Open link</option>
                <option value="share">Share link</option>
              </select>
              <label class="flex items-center gap-2 text-sm"><input type="checkbox" v-model="b.same_row"> Same row as button above</label>
              <div class="flex gap-1 ml-auto">
                <button class="btn btn-sm" @click="move(i,-1)" aria-label="Move up">↑</button>
                <button class="btn btn-sm" @click="move(i,1)" aria-label="Move down">↓</button>
                <button class="btn btn-sm btn-danger" @click="model.buttons.splice(i,1)">Remove</button>
              </div>
            </div>
            <input v-if="b.kind==='share'" class="inp" v-model="b.share_text" placeholder="Text sent along with the shared link">
            <details class="text-sm muted">
              <summary class="cursor-pointer">Custom emoji icon (advanced)</summary>
              <input class="inp mt-2" v-model="b.emoji_id" placeholder="Custom emoji ID (Telegram may limit this to some bots)">
            </details>
          </div>
        </div>
      </section>
    </div>

    <div class="lg:col-span-2 lg:sticky lg:top-4">
      <div class="panel">
        <h3 class="font-semibold mb-3">Preview</h3>
        <div class="bubble">
          <img v-if="src" :src="src" alt="">
          <div class="bt" v-html="html"></div>
          <div class="kb">
            <div v-for="(row,ri) in rows" :key="ri" class="kbrow">
              <div v-for="(b,bi) in row" :key="bi" class="kbbtn" :style="{background:styleColor(b.style)}">{{ b.text }}</div>
            </div>
          </div>
        </div>
        <p class="muted text-sm mt-3">Names and links shown here are examples.</p>
      </div>
    </div>
  </div>`,
};

/* ---------- Root app ---------- */
createApp({
  components: { MsgEditor },
  setup() {
    const state = reactive({ loaded: false, error: '', bot: {}, groups: {}, start: null });
    const route = ref(location.hash || '#/');
    const now = ref(Math.floor(Date.now() / 1000));
    let offset = 0, t1 = null, t2 = null;

    const toastMsg = ref(''), toastBad = ref('');
    const toast = (m, bad = false) => { toastMsg.value = m; toastBad.value = bad; setTimeout(() => toastMsg.value = '', 3000); };

    const page = computed(() => route.value.startsWith('#/g/') ? 'group' : route.value === '#/start' ? 'start' : 'home');
    const key = computed(() => page.value === 'group' ? decodeURIComponent(route.value.slice(4)) : '');
    const cur = computed(() => state.groups[key.value] || null);
    const groupList = computed(() => Object.entries(state.groups).map(([k, g]) => Object.assign({ key: k }, g)));
    const stats = computed(() => ({
      total: groupList.value.length,
      ready: groupList.value.filter(g => g.is_admin).length,
      active: groupList.value.filter(g => g.is_admin && g.enabled).length,
    }));

    async function load() {
      try {
        const d = await api('/state');
        state.bot = d.bot; state.groups = d.groups; state.start = d.start; state.error = '';
        offset = d.now - Math.floor(Date.now() / 1000);
        if (!state.loaded) { state.loaded = true; syncForms(); }
      } catch (e) { state.error = e.message; }
    }

    /* forms */
    const gform = ref(null), sform = ref(null);
    const clone = o => JSON.parse(JSON.stringify(o));
    function toForm(g) {
      const s = g.interval || 300; let unit = 'sec', v = s;
      if (s % 3600 === 0) { unit = 'hour'; v = s / 3600; } else if (s % 60 === 0) { unit = 'min'; v = s / 60; }
      return { enabled: g.enabled, delete_old: g.delete_old, invite_link: g.invite_link || '', text: g.msg_text,
               photo: g.photo || '', buttons: clone(g.buttons || []), iv: v, unit };
    }
    function syncForms() {
      gform.value = (cur.value && cur.value.is_admin) ? toForm(cur.value) : null;
      if (state.start && !sform.value) sform.value = { text: state.start.text, photo: state.start.photo || '', buttons: clone(state.start.buttons || []) };
    }
    watch([key, () => cur.value && cur.value.is_admin], syncForms);

    /* home */
    const newId = ref(''), adding = ref(false), addMsg = ref(''), addBad = ref(false);
    async function addGroup() {
      adding.value = true; addMsg.value = '';
      try {
        const d = await api('/groups', J('POST', { chat_id: newId.value }));
        state.groups[d.key] = d.group; newId.value = '';
        if (d.is_admin) { toast('Bot is admin. Tools unlocked.'); location.hash = '#/g/' + d.key; }
        else { addBad.value = true; addMsg.value = '"' + d.group.title + '" was added, but the bot is not an admin there yet. Make it admin, then open the group and press Recheck.'; }
      } catch (e) { addBad.value = true; addMsg.value = e.message; }
      adding.value = false;
    }

    /* group page */
    const busy = ref(false);
    async function recheck(relink = false) {
      busy.value = true;
      try {
        const g = await api('/groups/' + encodeURIComponent(key.value) + '/recheck?relink=' + (relink ? 1 : 0), { method: 'POST' });
        state.groups[key.value] = g;
        if (gform.value && relink) gform.value.invite_link = g.invite_link;
        toast(g.is_admin ? 'Bot is admin' : 'Bot is still not an admin', !g.is_admin);
      } catch (e) { toast(e.message, true); }
      busy.value = false;
    }
    async function saveGroup() {
      const f = gform.value, mult = { sec: 1, min: 60, hour: 3600 }[f.unit];
      const interval = Math.max(30, Math.round(Number(f.iv) * mult) || 300);
      busy.value = true;
      try {
        const g = await api('/groups/' + encodeURIComponent(key.value), J('PUT', {
          enabled: f.enabled, delete_old: f.delete_old, invite_link: f.invite_link,
          msg_text: f.text, photo: f.photo, interval, buttons: f.buttons }));
        state.groups[key.value] = g; toast('Saved');
        if (interval !== Number(f.iv) * mult) { gform.value = toForm(g); }
      } catch (e) { toast(e.message, true); }
      busy.value = false;
    }
    async function sendNow() {
      busy.value = true;
      try { state.groups[key.value] = await api('/groups/' + encodeURIComponent(key.value) + '/send', { method: 'POST' }); toast('Posted in the group'); }
      catch (e) { toast(e.message, true); await load(); }
      busy.value = false;
    }
    async function removeGroup() {
      if (!confirm('Remove "' + cur.value.title + '" from the bot? Its settings will be deleted.')) return;
      try { await api('/groups/' + encodeURIComponent(key.value), { method: 'DELETE' }); delete state.groups[key.value]; location.hash = '#/'; toast('Group removed'); }
      catch (e) { toast(e.message, true); }
    }

    /* start page */
    async function saveStart() {
      busy.value = true;
      try { state.start = await api('/start', J('PUT', sform.value)); toast('Start message saved'); }
      catch (e) { toast(e.message, true); }
      busy.value = false;
    }

    /* helpers */
    function nextIn(g) {
      if (!g.is_admin) return 'Locked until the bot is admin';
      if (!g.enabled) return 'Paused';
      if (!g.last_sent_time) return 'Waiting for the first new member';
      const r = g.last_sent_time + g.interval - (now.value + offset);
      return r <= 0 ? 'Reposting now…' : 'Next repost in ' + fmtDur(r);
    }
    const gctx = computed(() => ({ name: 'Rahim', group: cur.value ? cur.value.title : '',
      link: (gform.value && gform.value.invite_link) || (cur.value && cur.value.invite_link) || 'https://t.me/+example' }));
    const sctx = { name: 'Rahim', group: '', link: '' };
    const groupPH = [
      { tag: '{name}', hint: 'the new member (clickable name)' },
      { tag: '{shareurl}', hint: "this group's join link, filled in automatically" },
      { tag: '{group}', hint: "the group's title" },
    ];
    const startPH = [{ tag: '{name}', hint: "the person's first name" }];
    const backupUrl = '/api/backup?secret=' + encodeURIComponent(SECRET);

    onMounted(() => {
      window.addEventListener('hashchange', () => { route.value = location.hash || '#/'; });
      load();
      t1 = setInterval(() => now.value = Math.floor(Date.now() / 1000), 1000);
      t2 = setInterval(() => { if (page.value === 'home') load(); }, 15000);
    });
    onBeforeUnmount(() => { clearInterval(t1); clearInterval(t2); });

    return { state, page, key, cur, groupList, stats, gform, sform, newId, adding, addMsg, addBad, addGroup,
             busy, recheck, saveGroup, sendNow, removeGroup, saveStart, nextIn, gctx, sctx, groupPH, startPH,
             toastMsg, toastBad, backupUrl, fmtDur };
  },
  template: `
  <div class="max-w-6xl mx-auto px-4 py-6">
    <header class="flex flex-wrap items-center justify-between gap-3 mb-6">
      <div>
        <a href="#/" class="text-xl font-bold">Join Notifier</a>
        <div class="muted text-sm" v-if="state.bot.username">@{{ state.bot.username }}</div>
      </div>
      <nav class="flex flex-wrap gap-2">
        <a href="#/" class="btn" :class="{'btn-primary': page!=='start'}">Groups</a>
        <a href="#/start" class="btn" :class="{'btn-primary': page==='start'}">Start message</a>
        <a :href="backupUrl" class="btn">Download backup</a>
      </nav>
    </header>

    <div v-if="state.error" class="panel mb-4" style="border-color:var(--bad)">Could not load: {{ state.error }}</div>
    <div v-else-if="!state.loaded" class="muted">Loading…</div>

    <!-- HOME -->
    <div v-else-if="page==='home'">
      <div class="panel mb-6">
        <h2 class="font-semibold mb-1">Add a group or channel</h2>
        <p class="muted text-sm mb-3">Paste its ID (like -1001234567890) or @username. The bot checks whether it is an admin there and unlocks the tools if it is.</p>
        <div class="flex flex-col sm:flex-row gap-2">
          <input class="inp" v-model="newId" @keyup.enter="addGroup" placeholder="-1001234567890">
          <button class="btn btn-primary whitespace-nowrap" :disabled="adding || !newId.trim()" @click="addGroup">{{ adding ? 'Checking…' : 'Check and add' }}</button>
        </div>
        <p v-if="addMsg" class="text-sm mt-3" :style="{color: addBad ? 'var(--bad)' : 'var(--ok)'}">{{ addMsg }}</p>
      </div>

      <div class="grid grid-cols-3 gap-3 mb-6">
        <div class="panel"><div class="text-2xl font-bold">{{ stats.total }}</div><div class="muted text-sm">Groups</div></div>
        <div class="panel"><div class="text-2xl font-bold">{{ stats.ready }}</div><div class="muted text-sm">Bot is admin</div></div>
        <div class="panel"><div class="text-2xl font-bold">{{ stats.active }}</div><div class="muted text-sm">Auto-posting</div></div>
      </div>

      <div v-if="!groupList.length" class="panel text-center muted">No groups yet. Add the bot to a group, make it admin, then paste the group ID above.</div>
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <a v-for="g in groupList" :key="g.key" :href="'#/g/'+g.key" class="panel card">
          <div class="flex items-start justify-between gap-2 mb-1">
            <div class="font-semibold break-words">{{ g.title || g.key }}</div>
            <span class="badge shrink-0" :class="g.is_admin ? (g.enabled ? 'ok' : 'off') : 'bad'">{{ g.is_admin ? (g.enabled ? 'Active' : 'Paused') : 'Not admin' }}</span>
          </div>
          <div class="mono muted mb-3">{{ g.key }}</div>
          <div class="text-sm">Repost every {{ fmtDur(g.interval) }}</div>
          <div class="text-sm muted">{{ nextIn(g) }}</div>
          <div class="text-sm muted" v-if="g.last_member_name">Last member: {{ g.last_member_name }}</div>
          <div class="text-sm mt-2" v-if="g.last_error" style="color:var(--bad)">{{ g.last_error }}</div>
        </a>
      </div>
    </div>

    <!-- GROUP -->
    <div v-else-if="page==='group'">
      <a href="#/" class="muted text-sm">← All groups</a>
      <div v-if="!cur" class="panel mt-4">This group is not in the list.</div>
      <template v-else>
        <div class="flex flex-wrap items-center justify-between gap-3 my-4">
          <div>
            <h1 class="text-2xl font-bold break-words">{{ cur.title }}</h1>
            <div class="flex items-center gap-3 mt-1">
              <span class="badge" :class="cur.is_admin ? 'ok' : 'bad'">{{ cur.is_admin ? 'Bot is admin' : 'Not admin' }}</span>
              <span class="mono muted">{{ key }}</span>
            </div>
          </div>
          <div class="flex flex-wrap gap-2">
            <button class="btn" :disabled="busy" @click="recheck(false)">Recheck admin</button>
            <button class="btn" :disabled="busy || !cur.is_admin" @click="sendNow">Post now</button>
            <button class="btn btn-danger" @click="removeGroup">Remove</button>
          </div>
        </div>

        <div v-if="!cur.is_admin" class="panel" style="border-color:var(--warn)">
          <h3 class="font-semibold mb-1">Tools are locked</h3>
          <p class="muted">Make the bot an admin in this group (with permission to post messages, delete messages and invite users), then press <b>Recheck admin</b>. The tools unlock automatically.</p>
        </div>

        <template v-else-if="gform">
          <div class="panel mb-6">
            <h3 class="font-semibold mb-3">Timer and link</h3>
            <div class="grid md:grid-cols-2 gap-5">
              <div>
                <label class="block text-sm muted mb-1">Repost this message every</label>
                <div class="flex gap-2">
                  <input class="inp" type="number" min="1" v-model="gform.iv">
                  <select class="inp" style="width:auto" v-model="gform.unit">
                    <option value="sec">seconds</option><option value="min">minutes</option><option value="hour">hours</option>
                  </select>
                </div>
                <p class="muted text-sm mt-2">Minimum 30 seconds. The timer starts after the first new member (or after you press Post now).</p>
                <div class="flex flex-col gap-2 mt-3">
                  <label class="flex items-center gap-2"><input type="checkbox" v-model="gform.enabled"> Auto-posting is on for this group</label>
                  <label class="flex items-center gap-2"><input type="checkbox" v-model="gform.delete_old"> Delete the previous post before sending a new one</label>
                </div>
                <p class="text-sm mt-3 muted">{{ nextIn(cur) }}</p>
                <p class="text-sm mt-1" v-if="cur.last_error" style="color:var(--bad)">Last error: {{ cur.last_error }}</p>
              </div>
              <div>
                <label class="block text-sm muted mb-1">Join link used for <span class="mono">{shareurl}</span></label>
                <input class="inp" v-model="gform.invite_link" placeholder="Filled in automatically">
                <div class="flex items-center gap-2 mt-2">
                  <button class="btn btn-sm" :disabled="busy" @click="recheck(true)">Get a new link</button>
                  <span class="muted text-sm" v-if="!cur.username && !cur.can_invite">The bot needs the "Invite users" right to create links itself. You can also paste one.</span>
                </div>
              </div>
            </div>
          </div>

          <msg-editor :model="gform" :placeholders="groupPH" :ctx="gctx" :group-mode="true"></msg-editor>

          <div class="savebar"><button class="btn btn-primary" :disabled="busy" @click="saveGroup">{{ busy ? 'Saving…' : 'Save this group' }}</button></div>
        </template>
      </template>
    </div>

    <!-- START -->
    <div v-else-if="page==='start'">
      <h1 class="text-2xl font-bold mb-1">Start message</h1>
      <p class="muted mb-5">What people see when they press Start in a private chat with the bot.</p>
      <msg-editor v-if="sform" :model="sform" :placeholders="startPH" :ctx="sctx" :group-mode="false"></msg-editor>
      <div class="savebar"><button class="btn btn-primary" :disabled="busy" @click="saveStart">{{ busy ? 'Saving…' : 'Save start message' }}</button></div>
    </div>

    <div v-if="toastMsg" class="toast" :class="{bad: toastBad}" role="status">{{ toastMsg }}</div>
  </div>`,
}).mount('#app');
</script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_ui(secret: str = ""):
    if not secret or not hmac.compare_digest(secret.encode(), ADMIN_SECRET.encode()):
        return HTMLResponse("<h1>Unauthorized. Check your secret in the URL.</h1>", status_code=403)
    secret_json = json.dumps(secret).replace("</", "<\\/")
    return HTMLResponse(ADMIN_HTML.replace("__SECRET_JSON__", secret_json))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
