# app.py
"""
Single-file TG Join Noti app.

Run:
    python app.py

Open:
    http://localhost:8080

Default web login:
    user: admin
    pass: admin     (change via env WEB_USER / WEB_PASS)

Files created next to this script:
    config.json       (bot token, chats, message template, web credentials)
    buttons.json      (custom inline buttons)
    last_member.json  (last joined member's HTML name)
"""

import asyncio
import html
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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
# DEFAULT CONTENT
# ============================================================
DEFAULT_CONFIG = {
    "bot_token": "",
    "chats": [
        {"id": "", "type": "group"},
        {"id": "", "type": "channel"},
    ],
    "message_template": (
        "প্রিয় {name}, আপনাকে হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি ৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    ),
    # web credentials (set env WEB_USER / WEB_PASS to override)
    "web_user": "admin",
    "web_pass": "admin",
}

DEFAULT_BUTTONS = {
    "next_id": 3,
    "buttons": [
        {"id": 1, "text": "📤 শেয়ার ২/৩ করুন", "url": "{{SHARE_URL}}", "enabled": True},
        {"id": 2, "text": "🎥 ভিডিও দেখুন", "url": "{{SHARE_URL}}", "enabled": True},
    ],
}


# ============================================================
# JSON STORAGE
# ============================================================
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
    cfg = _read_json(CONFIG_FILE, dict(DEFAULT_CONFIG))
    # env overrides for web login
    if os.getenv("WEB_USER"):
        cfg["web_user"] = os.getenv("WEB_USER")
    if os.getenv("WEB_PASS"):
        cfg["web_pass"] = os.getenv("WEB_PASS")
    return cfg


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
    return data.get("member_name_html") or ""


def save_last_member(name_html: str) -> None:
    try:
        _atomic_write(STATE_FILE, {"member_name_html": name_html})
    except Exception as e:
        log.warning(f"save state: {e}")


# ============================================================
# BOT STATE
# ============================================================
previous_message_id: dict[int, int] = {}
member_name_html: str = ""
member_join_event: Optional[asyncio.Event] = None
replace_lock: Optional[asyncio.Lock] = None
bot_ready = threading.Event()
bot_error: Optional[str] = None
bot: Optional[Bot] = None


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


async def get_share_url(bot_: Bot, chat_id: int) -> str:
    try:
        chat = await bot_.get_chat(chat_id=chat_id)
        target = chat.invite_link or f"https://t.me/c/{str(chat_id).replace('-100', '')}"
        text = quote("হিজাবি ভিডিওগ্রুপে জয়েন করুন")
        return f"https://t.me/share/url?url={quote(target)}&text={text}"
    except Exception as exc:
        log.warning(f"share url {chat_id}: {exc}")
        return "https://t.me/"


async def replace_message_for(bot_: Bot, chat_id: int, name_html: str, cfg: dict) -> None:
    async with replace_lock:
        old = previous_message_id.get(chat_id)
        if old is not None:
            try:
                await bot_.delete_message(chat_id=chat_id, message_id=old)
            except Exception as exc:
                log.warning(f"delete old in {chat_id}: {exc}")
        share_url = await get_share_url(bot_, chat_id)
        try:
            sent = await bot_.send_message(
                chat_id=chat_id,
                text=make_message(cfg["message_template"], name_html),
                reply_markup=make_keyboard(share_url),
                disable_web_page_preview=True,
            )
            previous_message_id[chat_id] = sent.message_id
            log.info(f"sent promo to {chat_id} -> {sent.message_id}")
        except Exception as exc:
            log.error(f"send promo to {chat_id}: {exc}")


async def replace_message_all(bot_: Bot, name_html: str) -> None:
    if not name_html:
        log.info("skip send: no joined member yet")
        return
    cfg = load_config()
    chat_ids = [int(c["id"]) for c in cfg["chats"] if str(c.get("id", "")).strip()]
    if not chat_ids:
        return
    await asyncio.gather(
        *(replace_message_for(bot_, cid, name_html, cfg) for cid in chat_ids),
        return_exceptions=True,
    )


def _is_join(event: ChatMemberUpdated) -> bool:
    return event.old_chat_member.status in ("left", "kicked") and \
           event.new_chat_member.status == "member"


async def new_member_handler(event: ChatMemberUpdated, bot_: Bot) -> None:
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
    await replace_message_all(bot_, member_name_html)


# NOTE: no periodic re-post loop — messages are only sent when a real join happens
# (or on startup if there's a saved last member).


async def run_bot() -> None:
    global member_name_html, member_join_event, replace_lock, bot_error, bot

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
        # send only if we already know the last joined member
        if member_name_html:
            await replace_message_all(bot, member_name_html)
        await dp.start_polling(bot, allowed_updates=["chat_member"])
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
# WEB AUTH (simple session cookie)
# ============================================================
SESSIONS: set[str] = set()
SESSION_COOKIE = "tgn_session"


def _creds_ok(user: str, pw: str, cfg: dict) -> bool:
    return user == cfg.get("web_user") and pw == cfg.get("web_pass")


def _authed(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token and token in SESSIONS)


# ============================================================
# WEB UI
# ============================================================
app = FastAPI(title="TG Join Noti")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TG Join Noti — Admin</title>
<style>
  :root{{--bg:#f6f7fb;--card:#fff;--ink:#111827;--muted:#6b7280;--brand:#2563eb;--danger:#dc2626;--ok:#15803d}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5}}
  .wrap{{max-width:980px;margin:0 auto;padding:2rem 1rem}}
  h1{{margin:0 0 .25rem}}
  .muted{{color:var(--muted);font-size:.88rem}}
  .topbar{{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}}
  section{{background:var(--card);border-radius:14px;padding:1.25rem;margin:1.25rem 0;box-shadow:0 6px 20px rgba(17,24,39,.06);animation:rise .35s ease both}}
  section:nth-of-type(2){{animation-delay:.06s}}
  section:nth-of-type(3){{animation-delay:.12s}}
  @keyframes rise{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}
  label{{display:block;font-weight:600;margin:.75rem 0 .25rem}}
  input[type=text],input[type=number],input[type=password],textarea,select{{width:100%;padding:.55rem .7rem;border:1px solid #d1d5db;border-radius:8px;font:inherit;background:#fff;transition:border-color .15s,box-shadow .15s}}
  input:focus,textarea:focus,select:focus{{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.15)}}
  textarea{{min-height:120px;resize:vertical}}
  .row{{display:flex;gap:.5rem;align-items:center;margin:.5rem 0}}
  .row>*{{flex:0 0 auto}}
  .row input[type=text]{{flex:1 1 auto}}
  button{{padding:.55rem 1rem;border:0;border-radius:10px;background:var(--brand);color:#fff;cursor:pointer;font:inherit;font-weight:600;transition:transform .08s,filter .15s,box-shadow .15s}}
  button:hover{{filter:brightness(1.05)}}
  button:active{{transform:scale(.97)}}
  button.danger{{background:var(--danger)}}
  button.small{{padding:.32rem .6rem;font-size:.85rem;border-radius:8px}}
  button.ghost{{background:#eef2ff;color:#1e3a8a}}
  .btn-card{{border:1px solid #e5e7eb;border-radius:12px;padding:.85rem;margin:.55rem 0;animation:pop .3s ease both}}
  @keyframes pop{{from{{opacity:0;transform:scale(.97)}}to{{opacity:1;transform:scale(1)}}}}
  .actions{{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.5rem}}
  .pill{{display:inline-block;padding:.2rem .6rem;border-radius:999px;font-size:.75rem;font-weight:700}}
  .pill.ok{{background:#dcfce7;color:#166534}}
  .pill.bad{{background:#fee2e2;color:#991b1b}}
  .toast-wrap{{position:fixed;right:1rem;bottom:1rem;display:flex;flex-direction:column;gap:.5rem;z-index:50}}
  .toast{{background:#111827;color:#fff;padding:.65rem .9rem;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.2);animation:slidein .25s ease}}
  @keyframes slidein{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
  table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}}
  th,td{{padding:.55rem .7rem;border-bottom:1px solid #eef;text-align:left;font-size:.9rem}}
  th{{background:#f1f5f9}}
  code{{background:#f1f5f9;padding:.1rem .35rem;border-radius:6px}}
  a{{color:var(--brand);text-decoration:none}}
  a:hover{{text-decoration:underline}}
</style></head><body>
<div class="wrap">
<div class="topbar">
  <div>
    <h1>TG Join Noti — Admin</h1>
    <div class="muted">Status: {status}</div>
  </div>
  <div class="actions">
    <a href="/health"><button type="button" class="ghost">📊 Health</button></a>
    <form method="post" action="/logout"><button type="submit" class="ghost">Logout</button></form>
  </div>
</div>
{body}
</div>
<div class="toast-wrap" id="toasts"></div>
<script>
  function toast(msg, type){{
    const w = document.getElementById('toasts');
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    w.appendChild(el);
    setTimeout(()=>{{ el.style.opacity = '0'; el.style.transform='translateY(6px)'; setTimeout(()=>el.remove(), 250); }}, 2200);
  }}
  // show a toast when arriving via ?saved=... or ?deleted=...
  const url = new URL(location.href);
  if (url.searchParams.has('saved'))   toast('✅ Saved');
  if (url.searchParams.has('deleted')) toast('🗑 Deleted');
  if (url.searchParams.has('added'))   toast('➕ Added');
  if (url.searchParams.has('moved'))   toast('↕ Reordered');
</script>
</body></html>
"""


def _status_html() -> str:
    if bot_error:
        return f'<span class="pill bad">bot error: {html.escape(bot_error)}</span>'
    if bot_ready.is_set():
        return '<span class="pill ok">bot running</span>'
    return '<span class="pill bad">bot not started</span>'


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, err: Optional[str] = None):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    body = f"""<section style="max-width:420px;margin:6rem auto">
      <h2 style="margin-top:0">Login</h2>
      {'<p style="color:#b91c1c;font-weight:600">Wrong user or password</p>' if err else ''}
      <form method="post" action="/login">
        <label>Username</label>
        <input type="text" name="user" required autofocus>
        <label>Password</label>
        <input type="password" name="pw" required>
        <div class="actions"><button type="submit">Sign in</button></div>
      </form>
    </section>"""
    return PAGE.format(status=_status_html(), body=body)


@app.post("/login")
async def do_login(user: str = Form(...), pw: str = Form(...)):
    cfg = load_config()
    if not _creds_ok(user, pw, cfg):
        return RedirectResponse("/login?err=1", status_code=303)
    token = secrets.token_urlsafe(24)
    SESSIONS.add(token)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    SESSIONS.discard(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)

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
  <h2 style="margin-top:0">Bot &amp; Chats</h2>
  <form method="post" action="/config">
    <label>Bot token</label>
    <input type="text" name="bot_token" value="{html.escape(cfg.get('bot_token',''))}" placeholder="123456:ABC-DEF...">
    <label>Chats (group / channel)</label>
    {chat_rows}
    <label>Message template (use <code>{{name}}</code>)</label>
    <textarea name="message_template">{html.escape(cfg.get('message_template',''))}</textarea>
    <div class="actions"><button type="submit">Save config</button></div>
  </form>
  <form method="post" action="/chats/add" style="margin-top:.5rem">
    <button type="submit" class="ghost small">+ Add chat slot</button>
  </form>
</section>

<section>
  <h2 style="margin-top:0">Buttons</h2>
  <p class="muted">Use <code>{{{{SHARE_URL}}}}</code> in a URL to substitute the live share link.</p>
  {btn_cards}
  <form method="post" action="/buttons/add" class="btn-card">
    <h3 style="margin:.2rem 0 .6rem">Add button</h3>
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
    request: Request,
    bot_token: str = Form(""),
    message_template: str = Form(""),
    chat_id: list[str] = Form([]),
    chat_type: list[str] = Form([]),
):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    cfg["bot_token"] = bot_token.strip()
    cfg["message_template"] = message_template
    cfg["chats"] = [
        {"id": cid.strip(), "type": ctype}
        for cid, ctype in zip(chat_id, chat_type)
        if cid.strip()
    ] or [{"id": "", "type": "group"}]
    save_config(cfg)
    log.info("config saved")
    return RedirectResponse("/?saved=1", status_code=303)


@app.post("/chats/add")
async def add_chat(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    cfg["chats"].append({"id": "", "type": "group"})
    save_config(cfg)
    return RedirectResponse("/?added=1", status_code=303)


@app.post("/buttons/add")
async def add_button(request: Request, text: str = Form(...), url: str = Form(...)):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    bid = data.get("next_id", 1)
    data["next_id"] = bid + 1
    data.setdefault("buttons", []).append(
        {"id": bid, "text": text.strip(), "url": url.strip(), "enabled": True}
    )
    save_buttons(data)
    return RedirectResponse("/?added=1", status_code=303)


@app.post("/buttons/update/{bid}")
async def update_button(
    request: Request,
    bid: int,
    text: str = Form(...),
    url: str = Form(...),
    enabled: Optional[str] = Form(None),
):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    for b in data.get("buttons", []):
        if b["id"] == bid:
            b["text"] = text.strip()
            b["url"] = url.strip()
            b["enabled"] = enabled == "on"
            break
    save_buttons(data)
    return RedirectResponse("/?saved=1", status_code=303)


@app.post("/buttons/delete/{bid}")
async def delete_button(request: Request, bid: int):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    data["buttons"] = [b for b in data.get("buttons", []) if b["id"] != bid]
    save_buttons(data)
    return RedirectResponse("/?deleted=1", status_code=303)


@app.post("/buttons/move/{bid}/{direction}")
async def move_button(request: Request, bid: int, direction: str):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    arr = data.get("buttons", [])
    for i, b in enumerate(arr):
        if b["id"] == bid:
            j = i - 1 if direction == "up" else i + 1
            if 0 <= j < len(arr):
                arr[i], arr[j] = arr[j], arr[i]
            break
    save_buttons(data)
    return RedirectResponse("/?moved=1", status_code=303)


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
async def health(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    results = await asyncio.gather(
        *(check_chat(cfg.get("bot_token", ""), c["id"]) for c in cfg.get("chats", [])),
        return_exceptions=True,
    )
    rows = ""
    for r in results:
        if not isinstance(r, dict):
            continue
        admin_cell = '<span class="pill ok">yes</span>' if r["bot_is_admin"] else '<span class="pill bad">no</span>'
        link_cell = f'<a href="{r["invite_link"]}" target="_blank">open</a>' if r["invite_link"] else "—"
        status_cell = '<span class="pill ok">ok</span>' if r["ok"] else f'<span class="pill bad">{html.escape(r["error"] or "error")}</span>'
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
      <p><a href="/">← back</a></p>
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
    if not os.path.exists(CONFIG_FILE):
        save_config(dict(DEFAULT_CONFIG))
    if not os.path.exists(BUTTONS_FILE):
        save_buttons(dict(DEFAULT_BUTTONS))

    t = threading.Thread(target=bot_thread, daemon=True)
    t.start()
    time.sleep(1.0)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
