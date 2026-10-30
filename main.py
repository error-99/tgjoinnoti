# app.py
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
from fastapi.responses import HTMLResponse, RedirectResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
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
# DEFAULTS
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
    if os.getenv("WEB_USER"): cfg["web_user"] = os.getenv("WEB_USER")
    if os.getenv("WEB_PASS"): cfg["web_pass"] = os.getenv("WEB_PASS")
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
    return _read_json(STATE_FILE, {}).get("member_name_html") or ""

def save_last_member(name_html: str) -> None:
    try:
        _atomic_write(STATE_FILE, {"member_name_html": name_html})
    except Exception as e:
        log.warning(f"save state: {e}")

# ============================================================
# BOT ENGINE
# ============================================================
bot_task: Optional[asyncio.Task] = None
bot_ready = threading.Event()
bot_error: Optional[str] = None
member_name_html: str = ""
previous_message_id: dict[int, int] = {}

def make_keyboard(share_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for b in load_buttons().get("buttons", []):
        if not b.get("enabled", True): continue
        url = str(b.get("url", "")).replace("{{SHARE_URL}}", share_url)
        if url:
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
    except Exception:
        return "https://t.me/"

async def replace_message_for(bot: Bot, chat_id: int, name_html: str, cfg: dict) -> None:
    old = previous_message_id.get(chat_id)
    if old is not None:
        try: 
            await bot.delete_message(chat_id=chat_id, message_id=old)
        except Exception: 
            pass
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
    if not name_html: return
    cfg = load_config()
    chat_ids = [int(c["id"]) for c in cfg["chats"] if str(c.get("id", "")).strip()]
    if chat_ids:
        await asyncio.gather(*(replace_message_for(bot, cid, name_html, cfg) for cid in chat_ids), return_exceptions=True)

def _is_join(event: ChatMemberUpdated) -> bool:
    return event.old_chat_member.status in ("left", "kicked") and event.new_chat_member.status == "member"

# IMPORTANT: The parameter MUST be named 'bot' for aiogram to inject it
async def new_member_handler(event: ChatMemberUpdated, bot: Bot) -> None:
    global member_name_html
    cfg = load_config()
    chat_ids = {int(c["id"]) for c in cfg["chats"] if str(c.get("id", "")).strip()}
    
    if event.chat.id in chat_ids and _is_join(event):
        user = event.new_chat_member.user
        member_name_html = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
        save_last_member(member_name_html)
        log.info(f"New member joined: {user.full_name}")
        await replace_message_all(bot, member_name_html)

async def refresh_loop(bot: Bot) -> None:
    while True:
        try:
            cfg = load_config()
            interval = max(30, int(cfg.get("interval_seconds", 240)))
            await asyncio.sleep(interval)
            
            # Only refresh if someone has joined previously
            if member_name_html:
                log.info("Interval reached. Refreshing messages...")
                await replace_message_all(bot, member_name_html)
                
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error(f"refresh_loop: {exc}")
            await asyncio.sleep(5)

async def _run_bot_logic(token: str):
    global bot_ready, bot_error, member_name_html
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.chat_member.register(new_member_handler)
    
    try:
        me = await bot.get_me()
        log.info(f"Bot @{me.username} started successfully.")
        bot_ready.set()
        bot_error = None
        
        # Send on startup if someone already joined before
        if member_name_html:
            await replace_message_all(bot, member_name_html)
            
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=["chat_member"]),
            refresh_loop(bot)
        )
    except Exception as exc:
        bot_error = str(exc)
        bot_ready.clear()
        log.error(f"Bot crashed: {exc}")
    finally:
        await bot.session.close()

def start_bot_thread():
    global bot_task, member_name_html
    if bot_task and not bot_task.done():
        bot_task.cancel()
    
    bot_ready.clear()
    member_name_html = load_last_member()
    token = load_config().get("bot_token", "").strip()
    if not token: return
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_task = loop.create_task(_run_bot_logic(token))
    threading.Thread(target=loop.run_forever, daemon=True).start()

# ============================================================
# WEB UI & AUTH
# ============================================================
SESSIONS = set()
COOKIE = "tgn_session"
app = FastAPI(title="TG Join Noti")

def _authed(req: Request) -> bool:
    return req.cookies.get(COOKIE) in SESSIONS

CSS = """
<style>
  :root { --bg:#f8fafc; --card:#fff; --ink:#0f172a; --muted:#64748b; --brand:#2563eb; --danger:#ef4444; --ok:#22c55e; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:system-ui,sans-serif; line-height:1.5; }
  .wrap { max-width:960px; margin:0 auto; padding:2rem 1rem; }
  h1 { margin:0 0 .25rem; font-size:1.5rem; }
  .topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:1.5rem; }
  .badge { padding:.2rem .6rem; border-radius:99px; font-size:.75rem; font-weight:700; }
  .badge.ok { background:#dcfce7; color:#166534; }
  .badge.bad { background:#fee2e2; color:#991b1b; }
  section { background:var(--card); border-radius:12px; padding:1.5rem; margin-bottom:1.5rem; box-shadow:0 4px 6px -1px rgb(0 0 0 / 0.05); animation:rise .4s ease-out; }
  @keyframes rise { from { opacity:0; transform:translateY(15px); } to { opacity:1; transform:translateY(0); } }
  label { display:block; font-weight:600; margin:1rem 0 .4rem; font-size:.9rem; }
  input[type=text], input[type=number], input[type=password], textarea, select { width:100%; padding:.65rem; border:1px solid #cbd5e1; border-radius:8px; font:inherit; transition:.2s; }
  input:focus, textarea:focus, select:focus { outline:none; border-color:var(--brand); box-shadow:0 0 0 3px rgba(37,99,235,.15); }
  textarea { min-height:130px; resize:vertical; }
  .chat-row { display:flex; gap:.5rem; margin-bottom:.5rem; }
  .chat-row input { flex:1; }
  .chat-row select { width:120px; flex-shrink:0; }
  button { padding:.6rem 1.2rem; border:0; border-radius:8px; background:var(--brand); color:#fff; cursor:pointer; font:inherit; font-weight:600; transition:.15s; }
  button:hover { filter:brightness(1.1); }
  button:active { transform:scale(.96); }
  button.danger { background:var(--danger); }
  button.ghost { background:#e2e8f0; color:#334155; }
  button.small { padding:.35rem .75rem; font-size:.85rem; }
  .btn-card { border:1px solid #e2e8f0; border-radius:10px; padding:1rem; margin-bottom:1rem; animation:pop .3s ease-out; }
  @keyframes pop { from { opacity:0; transform:scale(.98); } to { opacity:1; transform:scale(1); } }
  .actions { display:flex; gap:.5rem; flex-wrap:wrap; margin-top:1rem; }
  table { width:100%; border-collapse:collapse; background:var(--card); border-radius:10px; overflow:hidden; }
  th, td { padding:.75rem 1rem; border-bottom:1px solid #e2e8f0; text-align:left; font-size:.9rem; }
  th { background:#f1f5f9; font-weight:600; }
  .toast-wrap { position:fixed; bottom:20px; right:20px; display:flex; flex-direction:column; gap:10px; z-index:100; }
  .toast { background:#0f172a; color:#fff; padding:.75rem 1rem; border-radius:8px; font-size:.9rem; box-shadow:0 10px 15px -3px rgba(0,0,0,.1); animation:slideIn .3s ease-out; }
  @keyframes slideIn { from { opacity:0; transform:translateX(20px); } to { opacity:1; transform:translateX(0); } }
</style>
"""

JS = """
<script>
  function toast(msg) {
    const w = document.getElementById('toasts');
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    w.appendChild(el);
    setTimeout(()=>{ el.style.opacity='0'; el.style.transition='.3s'; setTimeout(()=>el.remove(), 300); }, 2500);
  }
  const params = new URLSearchParams(location.search);
  if (params.has('saved')) toast('✅ Saved config');
  if (params.has('deleted')) toast('🗑 Deleted');
  if (params.has('added')) toast('➕ Added');
</script>
"""

def render_page(status: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TG Join Noti — Admin</title>
{CSS}
</head><body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>TG Join Noti</h1>
      <div style="font-size:.85rem;color:var(--muted)">Status: {status}</div>
    </div>
    <div class="actions">
      <a href="/health"><button type="button" class="ghost">📊 Health</button></a>
      <form method="post" action="/logout" style="margin:0"><button type="submit" class="ghost">Logout</button></form>
    </div>
  </div>
  {body}
</div>
<div class="toast-wrap" id="toasts"></div>
{JS}
</body></html>"""

def _status_html():
    if bot_error: return f'<span class="badge bad">Error: {html.escape(bot_error)}</span>'
    if bot_ready.is_set(): return '<span class="badge ok">Running</span>'
    return '<span class="badge bad">Not Started (Missing Token)</span>'

@app.get("/login", response_class=HTMLResponse)
async def login_page(err: Optional[str] = None):
    body = f"""<section style="max-width:400px;margin:5rem auto;">
      <h2>Login</h2>
      {'<p style="color:red;font-weight:bold;">Wrong password</p>' if err else ''}
      <form method="post" action="/login">
        <label>Username</label>
        <input type="text" name="user" required>
        <label>Password</label>
        <input type="password" name="pw" required>
        <div class="actions"><button type="submit">Login</button></div>
      </form>
    </section>"""
    return render_page(_status_html(), body)

@app.post("/login")
async def do_login(user: str = Form(...), pw: str = Form(...)):
    if user == load_config().get("web_user") and pw == load_config().get("web_pass"):
        token = secrets.token_urlsafe(24)
        SESSIONS.add(token)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True)
        return resp
    return RedirectResponse("/login?err=1", status_code=303)

@app.post("/logout")
async def logout(req: Request):
    SESSIONS.discard(req.cookies.get(COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp

@app.get("/", response_class=HTMLResponse)
async def index(req: Request):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    
    chat_rows = "".join(
        f"""<div class="chat-row">
          <input type="text" name="chat_id" value="{html.escape(str(c.get('id','')))}" placeholder="-100...">
          <select name="chat_type">
            <option value="group" {'selected' if c.get('type')=='group' else ''}>group</option>
            <option value="channel" {'selected' if c.get('type')=='channel' else ''}>channel</option>
          </select>
        </div>""" for c in cfg.get("chats", [])
    )

    btn_cards = "".join(
        f"""<form class="btn-card" method="post" action="/buttons/update/{b['id']}">
          <div style="display:flex;gap:.5rem;">
            <input type="text" name="text" value="{html.escape(b['text'])}" style="flex:1;">
            <input type="text" name="url" value="{html.escape(b['url'])}" style="flex:2;">
            <label style="margin:0;display:flex;align-items:center;"><input type="checkbox" name="enabled" {'checked' if b.get('enabled',True) else ''}> On</label>
          </div>
          <div class="actions">
            <button type="submit" class="small">Save</button>
            <button type="submit" formaction="/buttons/delete/{b['id']}" class="danger small" onclick="return confirm('Delete?')">Delete</button>
          </div>
        </form>""" for b in load_buttons().get("buttons", [])
    )

    body = f"""
    <section>
      <h2>Bot & Chats</h2>
      <form method="post" action="/config">
        <label>Bot Token</label>
        <input type="text" name="bot_token" value="{html.escape(cfg.get('bot_token',''))}" placeholder="123456:ABC...">
        
        <label>Chats (Group / Channel IDs)</label>
        {chat_rows}
        <button type="submit" formaction="/chats/add" class="ghost small" style="margin-top:.5rem;">+ Add another chat</button>
        
        <label>Refresh Interval (Seconds)</label>
        <input type="number" name="interval_seconds" value="{cfg.get('interval_seconds',240)}" min="30">
        <div style="font-size:.8rem;color:var(--muted)">Reposts the message so it stays at the bottom. (Only runs after someone joins).</div>

        <label>Message Template</label>
        <textarea name="message_template">{html.escape(cfg.get('message_template',''))}</textarea>
        
        <div class="actions"><button type="submit">Save Config & Restart Bot</button></div>
      </form>
    </section>

    <section>
      <h2>Buttons</h2>
      <p style="color:var(--muted);font-size:.9rem;">Use <code>{{SHARE_URL}}</code> in a link to substitute the group invite link.</p>
      {btn_cards}
      <form method="post" action="/buttons/add" class="btn-card">
        <div style="display:flex;gap:.5rem;">
          <input type="text" name="text" placeholder="Button Text" required style="flex:1;">
          <input type="text" name="url" placeholder="https://... or {{SHARE_URL}}" required style="flex:2;">
        </div>
        <div class="actions"><button type="submit" class="small">Add Button</button></div>
      </form>
    </section>"""
    return render_page(_status_html(), body)

@app.post("/config")
async def post_config(req: Request, bot_token: str = Form(""), interval_seconds: int = Form(240), message_template: str = Form(""), chat_id: list[str] = Form([]), chat_type: list[str] = Form([])):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    old_token = cfg.get("bot_token", "")
    
    cfg["bot_token"] = bot_token.strip()
    cfg["interval_seconds"] = max(30, interval_seconds)
    cfg["message_template"] = message_template
    cfg["chats"] = [{"id": cid.strip(), "type": ctype} for cid, ctype in zip(chat_id, chat_type) if cid.strip()] or [{"id": "", "type": "group"}]
    save_config(cfg)
    
    if old_token != cfg["bot_token"]:
        start_bot_thread()
        
    return RedirectResponse("/?saved=1", status_code=303)

@app.post("/chats/add")
async def add_chat(req: Request):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    cfg["chats"].append({"id": "", "type": "group"})
    save_config(cfg)
    return RedirectResponse("/?added=1", status_code=303)

@app.post("/buttons/add")
async def add_button(req: Request, text: str = Form(...), url: str = Form(...)):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    data["buttons"].append({"id": data.get("next_id", 1), "text": text.strip(), "url": url.strip(), "enabled": True})
    data["next_id"] = data.get("next_id", 1) + 1
    save_buttons(data)
    return RedirectResponse("/?added=1", status_code=303)

@app.post("/buttons/update/{bid}")
async def update_button(req: Request, bid: int, text: str = Form(...), url: str = Form(...), enabled: Optional[str] = Form(None)):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    for b in data["buttons"]:
        if b["id"] == bid:
            b["text"], b["url"], b["enabled"] = text.strip(), url.strip(), enabled == "on"
            break
    save_buttons(data)
    return RedirectResponse("/?saved=1", status_code=303)

@app.post("/buttons/delete/{bid}")
async def delete_button(req: Request, bid: int):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    data = load_buttons()
    data["buttons"] = [b for b in data["buttons"] if b["id"] != bid]
    save_buttons(data)
    return RedirectResponse("/?deleted=1", status_code=303)

@app.get("/health", response_class=HTMLResponse)
async def health(req: Request):
    if not _authed(req): return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    rows = ""
    if not cfg.get("bot_token"): 
        rows = '<tr><td colspan="5">No token</td></tr>'
    else:
        b = Bot(token=cfg["bot_token"])
        for c in cfg.get("chats", []):
            cid = c.get("id")
            if not cid: continue
            try:
                chat = await b.get_chat(chat_id=int(cid))
                me = await b.get_me()
                m = await b.get_chat_member(chat_id=int(cid), user_id=me.id)
                adm = '<span class="badge ok">Yes</span>' if m.status in ("administrator", "creator") else '<span class="badge bad">No</span>'
                rows += f"<tr><td>{chat.title}</td><td><code>{cid}</code></td><td>{chat.type}</td><td>{getattr(chat,'member_count','—')}</td><td>{adm}</td></tr>"
            except Exception as e:
                rows += f"<tr><td>Error</td><td><code>{cid}</code></td><td colspan='3' style='color:red'>{html.escape(str(e))}</td></tr>"
        await b.session.close()
        
    body = f"""<section>
      <p><a href="/">← Back to Dashboard</a></p>
      <table><tr><th>Title</th><th>ID</th><th>Type</th><th>Members</th><th>Bot is Admin?</th></tr>{rows}</table>
    </section>"""
    return render_page(_status_html(), body)

# ============================================================
# STARTUP
# ============================================================
if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE): save_config(dict(DEFAULT_CONFIG))
    if not os.path.exists(BUTTONS_FILE): save_buttons(dict(DEFAULT_BUTTONS))

    start_bot_thread()
    time.sleep(1)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
