# app.py
"""
Single-file TG Join Noti app.

Run:
    python app.py

Then open:
    http://localhost:8080

Files created next to this script:
    config.json       (bot token, chats, interval, message template)
    buttons.json      (custom inline buttons)
    last_member.json  (last joined member's HTML name)
"""

import asyncio
import html
import json
import logging
import os
import tempfile
import threading
import time
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# PATHS / LOGGING
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BUTTONS_FILE = os.path.join(BASE_DIR, "buttons.json")
STATE_FILE = os.path.join(BASE_DIR, "last_member.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgjoinnoti")


# ============================================================
# JSON STORAGE
# ============================================================
DEFAULT_CONFIG = {
    "bot_token": "",
    "chats": [
        {"id": "", "type": "group"},
        {"id": "", "type": "channel"},
    ],
    "interval_seconds": 240,
    "message_template": (
        "প্রিয় {name}, আপনাকে হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি ৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    ),
}

DEFAULT_BUTTONS = {
    "next_id": 3,
    "buttons": [
        {"id": 1, "text": "📤 শেয়ার ২/৩ করুন", "url": "{{SHARE_URL}}", "enabled": True},
        {"id": 2, "text": "🎥 ভিডিও দেখুন", "url": "{{SHARE_URL}}", "enabled": True},
    ],
}


def _atomic_write(path: str, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"read {path}: {e}")
        return default


def load_config() -> dict:
    return _read_json(CONFIG_FILE, dict(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, cfg)


def load_buttons() -> dict:
    if not os.path.exists(BUTTONS_FILE):
        _atomic_write(BUTTONS_FILE, DEFAULT_BUTTONS)
        return dict(DEFAULT_BUTTONS)
    return _read_json(BUTTONS_FILE, dict(DEFAULT_BUTTONS))


def save_buttons(data: dict) -> None:
    _atomic_write(BUTTONS_FILE, data)


def load_last_member() -> str:
    data = _read_json(STATE_FILE, {})
    return data.get("member_name_html") or "বন্ধু"


def save_last_member(name_html: str) -> None:
    try:
        _atomic_write(STATE_FILE, {"member_name_html": name_html})
    except Exception as e:
        log.warning(f"save state: {e}")


# ============================================================
# BOT STATE
# ============================================================
previous_message_id: dict[int, int] = {}
member_name_html: str = "বন্ধু"
member_join_event: Optional[asyncio.Event] = None
replace_lock: Optional[asyncio.Lock] = None
bot_ready = threading.Event()
bot_error: Optional[str] = None


# ============================================================
# BOT CORE
# ============================================================
def make_keyboard(share_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for b in load_buttons().get("buttons", []):
        if not b.get("enabled", True):
            continue
        url = str(b.get("url", "")).replace("{{SHARE_URL}}", share_url)
        if not url:
            continue
        builder.row(InlineKeyboardButton(text=b.get("text", "button"), url=url))
    if not builder.export():
        builder.row(InlineKeyboardButton(text="📤 শেয়ার ২/৩ করুন", url=share_url))
        builder.row(InlineKeyboardButton(text="🎥 ভিডিও দেখুন", url=share_url))
    return builder.as_markup()


def make_message(template: str, name_html: str) -> str:
    return template.replace("{name}", name_html)


async def get_share_url(bot: Bot, chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id=chat_id)
        target = chat.invite_link or f"https://t.me/c/{str(chat_id).replace('-100', '')}"
        text = quote("হিজাবি ভিডিওগ্রুপে জয়েন করুন")
        return f"https://t.me/share/url?url={quote(target)}&text={text}"
    except Exception as exc:
        log.warning(f"share url {chat_id}: {exc}")
        return "https://t.me/"


async def replace_message_for(bot: Bot, chat_id: int, name_html: str, cfg: dict) -> None:
    async with replace_lock:
        old = previous_message_id.get(chat_id)
        if old is not None:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old)
            except Exception as exc:
                log.warning(f"delete old in {chat_id}: {exc}")
        share_url = await get_share_url(bot, chat_id)
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=make_message(cfg["message_template"], name_html),
                reply_markup=make_keyboard(share_url),
                disable_web_page_preview=True,
            )
            previous_message_id[chat_id] = sent.message_id
            log.info(f"sent promo to {chat_id} -> {sent.message_id}")
        except Exception as exc:
            log.error(f"send promo to {chat_id}: {exc}")


async def replace_message_all(bot: Bot, name_html: str) -> None:
    cfg = load_config()
    chat_ids = [int(c["id"]) for c in cfg["chats"] if str(c.get("id", "")).strip()]
    if not chat_ids:
        return
    await asyncio.gather(
        *(replace_message_for(bot, cid, name_html, cfg) for cid in chat_ids),
        return_exceptions=True,
    )


def _is_join(event: ChatMemberUpdated) -> bool:
    return event.old_chat_member.status in ("left", "kicked") and \
           event.new_chat_member.status == "member"


async def new_member_handler(event: ChatMemberUpdated, bot: Bot) -> None:
    global member_name_html
    cfg = load_config()
    chat_ids = {int(c["id"]) for c in cfg["chats"] if str(c.get("id", "")).strip()}
    if event.chat.id not in chat_ids:
        return
    if not _is_join(event):
        return
    user = event.new_chat_member.user
    member_name_html = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    save_last_member(member_name_html)
    log.info(f"new member in {event.chat.id}: {user.full_name}")
    if member_join_event:
        member_join_event.set()
    await replace_message_all(bot, member_name_html)


async def refresh_loop(bot: Bot) -> None:
    while True:
        try:
            cfg = load_config()
            interval = max(30, int(cfg.get("interval_seconds", 240)))
            try:
                await asyncio.wait_for(member_join_event.wait(), timeout=interval)
                member_join_event.clear()
            except asyncio.TimeoutError:
                pass
            await replace_message_all(bot, member_name_html)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"refresh_loop: {exc}")
            await asyncio.sleep(5)


async def run_bot() -> None:
    global member_name_html, member_join_event, replace_lock, bot_error

    member_join_event = asyncio.Event()
    replace_lock = asyncio.Lock()
    member_name_html = load_last_member()

    cfg = load_config()
    token = cfg.get("bot_token", "").strip()
    if not token:
        bot_error = "No bot token in config.json — set it in the web UI."
        log.error(bot_error)
        return

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.chat_member.register(new_member_handler)

    try:
        me = await bot.get_me()
        log.info(f"bot @{me.username} ready")
        bot_ready.set()
        await replace_message_all(bot, member_name_html)
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=["chat_member"]),
            refresh_loop(bot),
        )
    except Exception as exc:
        bot_error = str(exc)
        log.exception("bot crashed")
    finally:
        await bot.session.close()


def bot_thread() -> None:
    try:
        asyncio.run(run_bot())
    except Exception as exc:
        log.exception(f"bot_thread: {exc}")


# ============================================================
# WEB UI
# ============================================================
app = FastAPI(title="TG Join Noti")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>TG Join Noti — Admin</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;background:#f6f7fb;color:#222}}
  h1{{margin-bottom:.25rem}}
  section{{background:#fff;border-radius:10px;padding:1.25rem;margin:1.25rem 0;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
  label{{display:block;font-weight:600;margin:.75rem 0 .25rem}}
  input[type=text],input[type=number],textarea,select{{width:100%;padding:.55rem;border:1px solid #ccd;border-radius:6px;font:inherit;box-sizing:border-box}}
  textarea{{min-height:120px}}
  .row{{display:flex;gap:.5rem;align-items:center;margin:.5rem 0}}
  .row>*{{flex:0 0 auto}}
  .row input[type=text]{{flex:1 1 auto}}
  button{{padding:.5rem .9rem;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer;font:inherit}}
  button.danger{{background:#dc2626}}
  button.small{{padding:.3rem .55rem;font-size:.85rem}}
  button.ghost{{background:#e5e7eb;color:#111}}
  .btn-card{{border:1px solid #e5e7eb;border-radius:8px;padding:.75rem;margin:.5rem 0}}
  .muted{{color:#666;font-size:.85rem}}
  .ok{{color:#15803d;font-weight:600}}
  .bad{{color:#b91c1c;font-weight:600}}
  .actions{{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.5rem}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}}
  th,td{{padding:.55rem .7rem;border-bottom:1px solid #eef;text-align:left;font-size:.9rem}}
  th{{background:#f1f5f9}}
  code{{background:#f1f5f9;padding:.1rem .3rem;border-radius:4px}}
</style></head><body>
<h1>TG Join Noti — Admin</h1>
<p class="muted">Status: {status}</p>
{body}
</body></html>
"""


def _status_html() -> str:
    if bot_error:
        return f'<span class="bad">bot error: {html.escape(bot_error)}</span>'
    if bot_ready.is_set():
        return '<span class="ok">bot running</span>'
    return '<span class="bad">bot not started</span>'


@app.get("/", response_class=HTMLResponse)
async def index():
    cfg = load_config()
    btns = load_buttons().get("buttons", [])

    chat_rows = "".join(
        f"""<div class="row">
            <input type="text" name="chat_id" value="{html.escape(str(c.get('id','')))}" placeholder="-100...">
            <select name="chat_type">
              <option value="group" {'selected' if c.get('type')=='group' else ''}>group</option>
              <option value="channel" {'selected' if c.get('type')=='channel' else ''}>channel</option>
            </select>
        </div>"""
        for c in cfg.get("chats", [])
    )

    btn_cards = "".join(
        f"""<form class="btn-card" method="post" action="/buttons/update/{b['id']}">
            <div class="row">
              <input type="text" name="text" value="{html.escape(b['text'])}">
              <input type="text" name="url" value="{html.escape(b['url'])}">
              <label style="margin:0;display:flex;align-items:center;gap:.25rem;">
                <input type="checkbox" name="enabled" {'checked' if b.get('enabled',True) else ''}> on
              </label>
            </div>
            <div class="actions">
              <button type="submit" class="small">Save</button>
              <button type="submit" formaction="/buttons/move/{b['id']}/up" class="ghost small">↑</button>
              <button type="submit" formaction="/buttons/move/{b['id']}/down" class="ghost small">↓</button>
              <button type="submit" formaction="/buttons/delete/{b['id']}" class="danger small"
                      onclick="return confirm('Delete this button?')">Delete</button>
            </div>
        </form>"""
        for b in btns
    )

    body = f"""
<section>
  <h2>Bot &amp; Chats</h2>
  <form method="post" action="/config">
    <label>Bot token</label>
    <input type="text" name="bot_token" value="{html.escape(cfg.get('bot_token',''))}" placeholder="123456:ABC-DEF...">
    <label>Chats (group / channel)</label>
    {chat_rows}
    <label>Refresh interval (seconds)</label>
    <input type="number" name="interval_seconds" min="30" value="{cfg.get('interval_seconds',240)}">
    <label>Message template (use <code>{{name}}</code>)</label>
    <textarea name="message_template">{html.escape(cfg.get('message_template',''))}</textarea>
    <div class="actions"><button type="submit">Save config</button></div>
  </form>
  <form method="post" action="/chats/add" style="margin-top:.5rem">
    <button type="submit" class="ghost small">+ Add chat slot</button>
  </form>
  <div class="actions">
    <a href="/health"><button type="button" class="ghost">📊 Check health</button></a>
  </div>
</section>

<section>
  <h2>Buttons</h2>
  <p class="muted">Use <code>{{{{SHARE_URL}}}}</code> in a URL to substitute the live share link.</p>
  {btn_cards}
  <form method="post" action="/buttons/add" class="btn-card">
    <h3 style="margin-top:0">Add button</h3>
    <div class="row">
      <input type="text" name="text" placeholder="Button text" required>
      <input type="text" name="url" placeholder="https://... or {{{{SHARE_URL}}}}" required>
    </div>
    <div class="actions"><button type="submit" class="small">+ Add</button></div>
  </form>
</section>
"""
    return PAGE.format(status=_status_html(), body=body)


@app.post("/config")
async def post_config(
    bot_token: str = Form(""),
    interval_seconds: int = Form(240),
    message_template: str = Form(""),
    chat_id: list[str] = Form([]),
    chat_type: list[str] = Form([]),
):
    cfg = load_config()
    cfg["bot_token"] = bot_token.strip()
    cfg["interval_seconds"] = max(30, int(interval_seconds))
    cfg["message_template"] = message_template
    cfg["chats"] = [
        {"id": cid.strip(), "type": ctype}
        for cid, ctype in zip(chat_id, chat_type)
        if cid.strip()
    ] or [{"id": "", "type": "group"}]
    save_config(cfg)
    log.info("config saved")
    return RedirectResponse("/", status_code=303)


@app.post("/chats/add")
async def add_chat():
    cfg = load_config()
    cfg["chats"].append({"id": "", "type": "group"})
    save_config(cfg)
    return RedirectResponse("/", status_code=303)


@app.post("/buttons/add")
async def add_button(text: str = Form(...), url: str = Form(...)):
    data = load_buttons()
    bid = data.get("next_id", 1)
    data["next_id"] = bid + 1
    data.setdefault("buttons", []).append(
        {"id": bid, "text": text.strip(), "url": url.strip(), "enabled": True}
    )
    save_buttons(data)
    return RedirectResponse("/", status_code=303)


@app.post("/buttons/update/{bid}")
async def update_button(
    bid: int,
    text: str = Form(...),
    url: str = Form(...),
    enabled: Optional[str] = Form(None),
):
    data = load_buttons()
    for b in data.get("buttons", []):
        if b["id"] == bid:
            b["text"] = text.strip()
            b["url"] = url.strip()
            b["enabled"] = enabled == "on"
            break
    save_buttons(data)
    return RedirectResponse("/", status_code=303)


@app.post("/buttons/delete/{bid}")
async def delete_button(bid: int):
    data = load_buttons()
    data["buttons"] = [b for b in data.get("buttons", []) if b["id"] != bid]
    save_buttons(data)
    return RedirectResponse("/", status_code=303)


@app.post("/buttons/move/{bid}/{direction}")
async def move_button(bid: int, direction: str):
    data = load_buttons()
    arr = data.get("buttons", [])
    for i, b in enumerate(arr):
        if b["id"] == bid:
            j = i - 1 if direction == "up" else i + 1
            if 0 <= j < len(arr):
                arr[i], arr[j] = arr[j], arr[i]
            break
    save_buttons(data)
    return RedirectResponse("/", status_code=303)


async def check_chat(token: str, chat_id: str) -> dict:
    r = {"chat_id": chat_id, "ok": False, "title": None, "type": None,
         "member_count": None, "bot_is_admin": False, "invite_link": None,
         "error": None}
    if not token or not chat_id:
        r["error"] = "missing token or chat id"
        return r
    b = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        chat = await b.get_chat(chat_id=int(chat_id))
        r.update(ok=True, title=chat.title, type=chat.type,
                 member_count=getattr(chat, "member_count", None),
                 invite_link=getattr(chat, "invite_link", None))
        me = await b.get_me()
        try:
            m = await b.get_chat_member(chat_id=int(chat_id), user_id=me.id)
            r["bot_is_admin"] = m.status in ("administrator", "creator")
        except Exception as exc:
            r["error"] = f"get_chat_member: {exc}"
        if not r["invite_link"]:
            try:
                link = await b.create_chat_invite_link(chat_id=int(chat_id))
                r["invite_link"] = link.invite_link
            except Exception:
                pass
    except Exception as exc:
        r["error"] = str(exc)
    finally:
        await b.session.close()
    return r


@app.get("/health", response_class=HTMLResponse)
async def health():
    cfg = load_config()
    results = await asyncio.gather(
        *(check_chat(cfg.get("bot_token", ""), c["id"]) for c in cfg.get("chats", [])),
        return_exceptions=True,
    )
    rows = ""
    for r in results:
        if not isinstance(r, dict):
            continue
        admin_cell = '<span class="ok">yes</span>' if r["bot_is_admin"] else '<span class="bad">no</span>'
        link_cell = f'<a href="{r["invite_link"]}" target="_blank">open</a>' if r["invite_link"] else "—"
        status_cell = '<span class="ok">ok</span>' if r["ok"] else f'<span class="bad">{html.escape(r["error"] or "error")}</span>'
        rows += f"""<tr>
          <td><code>{html.escape(r["chat_id"])}</code></td>
          <td>{html.escape(r["title"] or "—")}</td>
          <td>{html.escape(r["type"] or "—")}</td>
          <td>{r["member_count"] or "—"}</td>
          <td>{admin_cell}</td>
          <td>{link_cell}</td>
          <td>{status_cell}</td>
        </tr>"""
    body = f"""<section>
      <p><a href="/">← back to admin</a></p>
      <table>
        <tr><th>Chat ID</th><th>Title</th><th>Type</th><th>Members</th>
            <th>Bot admin</th><th>Invite link</th><th>Status</th></tr>
        {rows or '<tr><td colspan="7" class="muted">No chats configured.</td></tr>'}
      </table>
    </section>"""
    return PAGE.format(status=_status_html(), body=body)


# ============================================================
# ENTRYPOINT
# ============================================================
if __name__ == "__main__":
    # seed config/buttons so the web UI has something to show
    if not os.path.exists(CONFIG_FILE):
        save_config(dict(DEFAULT_CONFIG))
    if not os.path.exists(BUTTONS_FILE):
        save_buttons(dict(DEFAULT_BUTTONS))

    # start bot in a background thread
    t = threading.Thread(target=bot_thread, daemon=True)
    t.start()

    # wait briefly so first load can show accurate status
    time.sleep(1.0)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
