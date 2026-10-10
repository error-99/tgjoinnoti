import asyncio
import html
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.exceptions import TelegramAPIError

# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004398264709"))
PORT = int(os.getenv("PORT", "8080"))
INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tgjoinnoti")

dp = Dispatcher()

# In-memory state
started_at = time.time()
previous_message_id = None
member_name_html = "বন্ধু"
group_invite_link = None
bot_username = None
bot_connected = False
polling_running = False
last_sent_at = None
last_error = None
bot_instance = None  # Added to access bot globally from web routes

member_join_event = asyncio.Event()
replace_lock = asyncio.Lock()


# ==================================================
# GROUP INVITE LINK
# ==================================================

async def get_group_link(bot: Bot) -> str:
    chat = await bot.get_chat(CHAT_ID)

    if chat.username:
        return f"https://t.me/{chat.username}"

    if chat.invite_link:
        return chat.invite_link

    invite = await bot.create_chat_invite_link(
        chat_id=CHAT_ID,
        name="Share group link",
    )
    return invite.invite_link


# ==================================================
# BUTTONS
# ==================================================

def make_keyboard() -> InlineKeyboardMarkup:
    share_text = "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️"
    link = str(group_invite_link) if group_invite_link else ""

    share_url = (
        "https://t.me/share/url"
        f"?url={quote(link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 শেয়ার ২/৩ করুন",
                    url=share_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎥 ভিডিও দেখুন",
                    url=share_url
                )
            ],
        ]
    )


# ==================================================
# SEND / REPLACE PROMOTION MESSAGE
# ==================================================

def make_message() -> str:
    return (
        f"প্রিয় {member_name_html}, আপনাকে "
        "হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি "
        "৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    )


async def replace_message(bot: Bot):
    global previous_message_id, last_sent_at, last_error

    async with replace_lock:
        if previous_message_id is not None:
            try:
                await bot.delete_message(
                    chat_id=CHAT_ID,
                    message_id=previous_message_id,
                )
                log.info("Deleted previous message %s", previous_message_id)
                previous_message_id = None
            except TelegramAPIError as exc:
                log.warning("Could not delete old message: %s", exc)

        try:
            sent = await bot.send_message(
                chat_id=CHAT_ID,
                text=make_message(),
                reply_markup=make_keyboard(),
            )

            previous_message_id = sent.message_id
            last_sent_at = datetime.now(timezone.utc).isoformat()
            last_error = None

            log.info("Promotion sent; message_id=%s", previous_message_id)

        except Exception as exc:
            last_error = str(exc)
            log.exception("Failed to send promotion message")


# ==================================================
# WHEN A USER JOINS
# ==================================================

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def new_member_handler(event: ChatMemberUpdated, bot: Bot):
    global member_name_html

    if event.chat.id != CHAT_ID:
        return

    user = event.new_chat_member.user
    if user.is_bot:
        return

    member_name_html = (
        f'<a href="tg://user?id={user.id}">'
        f'{html.escape(user.full_name)}'
        "</a>"
    )

    log.info("New group member: %s", user.full_name)
    member_join_event.set()
    await replace_message(bot)


# ==================================================
# REPEAT AFTER INACTIVITY
# ==================================================

async def refresh_loop(bot: Bot):
    while True:
        try:
            await asyncio.wait_for(
                member_join_event.wait(),
                timeout=INTERVAL_SECONDS,
            )
            member_join_event.clear()
        except asyncio.TimeoutError:
            log.info(f"No new member for {INTERVAL_SECONDS}s; refreshing message")
            await replace_message(bot)


# ==================================================
# WEB DASHBOARD & CRON ROUTES
# ==================================================

async def handle_cron(request):
    """Triggered by hitting the /cron endpoint"""
    if bot_instance:
        asyncio.create_task(replace_message(bot_instance))
        return web.json_response({
            "status": "success", 
            "message": "Promotion message triggered on Telegram group."
        })
    return web.json_response({"status": "error", "message": "Bot not connected."}, status=503)

def health_data():
    return {
        "bot_username": bot_username,
        "chat_id": CHAT_ID,
        "bot_connected": bot_connected,
        "polling_running": polling_running,
        "uptime_seconds": int(time.time() - started_at),
        "last_message_id": previous_message_id,
        "last_sent_at": last_sent_at,
        "last_error": last_error,
        "last_mentioned": member_name_html.replace("<", "&lt;").replace(">", "&gt;"),
    }

async def health_json(request):
    data = health_data()
    status_code = 200 if (bot_connected and polling_running) else 503
    return web.json_response(data, status=status_code)

async def health_page(request):
    data = health_data()
    
    status_text = "ONLINE" if data["bot_connected"] and data["polling_running"] else "OFFLINE"
    status_color = "#10b981" if status_text == "ONLINE" else "#ef4444"

    def show(value):
        return html.escape(str(value if value is not None else "N/A"))

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Control Panel</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: 'Inter', sans-serif;
    background: linear-gradient(135deg, #0f172a, #1e1b4b);
    color: #f8fafc;
    min-height: 100vh;
    padding: 30px 15px;
}}
.container {{
    max-width: 800px;
    margin: 0 auto;
}}
.header {{
    text-align: center;
    margin-bottom: 30px;
}}
.header h1 {{
    font-size: 2.2rem;
    background: -webkit-linear-gradient(#60a5fa, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.glass-panel {{
    background: rgba(255, 255, 255, 0.05);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 16px;
    padding: 25px;
    margin-bottom: 25px;
    box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
}}
.status-badge {{
    display: inline-block;
    padding: 6px 14px;
    border-radius: 20px;
    background: {status_color};
    color: #fff;
    font-weight: 600;
    font-size: 0.9rem;
    box-shadow: 0 0 10px {status_color}80;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 15px;
    margin-top: 20px;
}}
.stat-card {{
    background: rgba(0, 0, 0, 0.2);
    padding: 15px;
    border-radius: 12px;
    border: 1px solid rgba(255,255,255,0.05);
}}
.stat-card label {{
    font-size: 0.8rem;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
.stat-card .val {{
    font-size: 1.2rem;
    font-weight: 600;
    margin-top: 5px;
    color: #e2e8f0;
}}
.btn {{
    display: block;
    width: 100%;
    padding: 14px;
    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
    color: white;
    border: none;
    border-radius: 10px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.2s, box-shadow 0.2s;
}}
.btn:hover {{
    transform: translateY(-2px);
    box-shadow: 0 5px 15px rgba(139, 92, 246, 0.4);
}}
.error-box {{
    background: rgba(239, 68, 68, 0.1);
    border-left: 4px solid #ef4444;
    padding: 15px;
    border-radius: 8px;
    margin-top: 15px;
    color: #fca5a5;
    font-size: 0.9rem;
    word-break: break-all;
}}
.footer {{
    text-align: center;
    color: #64748b;
    font-size: 0.85rem;
    margin-top: 20px;
}}
</style>
<script>
async function triggerMessage() {{
    const btn = document.getElementById('triggerBtn');
    btn.innerText = 'Sending...';
    try {{
        const res = await fetch('/cron');
        const data = await res.json();
        if(res.ok) {{
            btn.innerText = '✅ Message Sent!';
            setTimeout(() => location.reload(), 1500);
        }} else {{
            btn.innerText = '❌ Failed';
        }}
    }} catch (e) {{
        btn.innerText = '❌ Error';
    }}
    setTimeout(() => {{ btn.innerText = '🚀 Send Message Now (Trigger Cron)'; }}, 3000);
}}
</script>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>TgJoin Notifier Dashboard</h1>
    </div>

    <div class="glass-panel">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>System Status</h2>
            <span class="status-badge">{status_text}</span>
        </div>
        
        <div class="grid">
            <div class="stat-card">
                <label>Bot Username</label>
                <div class="val">@{show(data["bot_username"])}</div>
            </div>
            <div class="stat-card">
                <label>Group ID</label>
                <div class="val">{show(data["chat_id"])}</div>
            </div>
            <div class="stat-card">
                <label>Uptime</label>
                <div class="val">{show(data["uptime_seconds"])}s</div>
            </div>
            <div class="stat-card">
                <label>Last Mentioned User</label>
                <div class="val" style="font-size:1rem;">{data["last_mentioned"]}</div>
            </div>
        </div>

        {f'<div class="error-box"><b>Latest Error:</b> {show(data["last_error"])}</div>' if data["last_error"] else ''}
    </div>

    <div class="glass-panel">
        <h2>Manual Control</h2>
        <p style="color: #94a3b8; margin: 10px 0 20px; font-size: 0.9rem;">
            Click the button below to manually replace the message in the group. You can also automate this by setting up a cron job to make a GET request to <code>{request.scheme}://{request.host}/cron</code>
        </p>
        <button id="triggerBtn" class="btn" onclick="triggerMessage()">🚀 Send Message Now (Trigger Cron)</button>
    </div>

    <div class="footer">
        Powered by aiogram & aiohttp • Auto-refreshes internally every 60s
    </div>
</div>
</body>
</html>"""

    return web.Response(text=page, content_type="text/html")


# ==================================================
# POLLING SUPERVISOR
# ==================================================

async def polling_supervisor(bot: Bot):
    global polling_running, last_error
    while True:
        try:
            polling_running = True
            log.info("Starting Telegram polling")
            await dp.start_polling(bot, handle_signals=False, allowed_updates=["message", "chat_member"])
            log.warning("Polling stopped; restarting shortly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
            log.exception("Polling failed")
        finally:
            polling_running = False
        await asyncio.sleep(5)


# ==================================================
# START EVERYTHING
# ==================================================

async def main():
    global group_invite_link, bot_username, bot_connected, bot_instance

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_instance = bot  # Save instance globally

    runner = None
    polling_task = None
    timer_task = None

    try:
        await bot.delete_webhook(drop_pending_updates=False)

        me = await bot.get_me()
        bot_username = me.username
        bot_connected = True

        group_invite_link = await get_group_link(bot)
        log.info("Connected as @%s", bot_username)

        # Initial message
        await replace_message(bot)

        # Background tasks
        polling_task = asyncio.create_task(polling_supervisor(bot))
        timer_task = asyncio.create_task(refresh_loop(bot))

        # Web App Setup
        app = web.Application()
        app.router.add_get("/", health_page)
        app.router.add_get("/cron", handle_cron)
        app.router.add_post("/cron", handle_cron)
        app.router.add_get("/health", health_json)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info("Dashboard listening on port %s", PORT)

        await asyncio.Event().wait()

    finally:
        bot_connected = False
        for task in (polling_task, timer_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (polling_task, timer_task) if t), return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
D</div>
            <div class="value">{show(previous_message_id)}</div>
        </div>
        <div class="card">
            <div class="label">Last successful send (UTC)</div>
            <div class="value">{show(last_sent_at)}</div>
        </div>
    </section>

    <section class="status">
        <h3>Recent error</h3>
        {error_html}
    </section>

    <p><a href="/health">View JSON health status</a></p>
    <footer>No database is used. The message ID is forgotten after a restart.</footer>
</main>
</body>
</html>"""

    return web.Response(text=page, content_type="text/html")


# ==================================================
# POLLING SUPERVISOR (Fixed)
# ==================================================

async def polling_supervisor(bot: Bot):
    global polling_running, last_error

    while True:
        try:
            polling_running = True
            log.info("Starting Telegram polling")

            # allowed_updates যুক্ত করা হয়েছে যেন বট chat_member ইভেন্ট ধরতে পারে
            await dp.start_polling(
                bot, 
                handle_signals=False, 
                allowed_updates=["message", "chat_member"]
            )

            log.warning("Polling stopped; restarting shortly")

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            last_error = str(exc)
            log.exception("Polling failed")

        finally:
            polling_running = False

        await asyncio.sleep(5)


# ==================================================
# START EVERYTHING
# ==================================================

async def main():
    global group_invite_link, bot_username, bot_connected

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    runner = None
    polling_task = None
    timer_task = None

    try:
        # This app uses long polling, not the old webhook.
        await bot.delete_webhook(drop_pending_updates=False)

        me = await bot.get_me()
        bot_username = me.username
        bot_connected = True

        group_invite_link = await get_group_link(bot)
        log.info("Connected as @%s", bot_username)
        log.info("Group invite link is ready")

        # Initial promotion message.
        await replace_message(bot)

        # Start polling and the repeating timer.
        polling_task = asyncio.create_task(polling_supervisor(bot))
        timer_task = asyncio.create_task(refresh_loop(bot))

        # Serve the website and JSON health endpoint.
        app = web.Application()
        app.router.add_get("/", health_page)
        app.router.add_get("/health", health_json)
        app.router.add_get("/healthz", health_json)

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()

        log.info("Health dashboard listening on port %s", PORT)

        # Keep the app alive.
        await asyncio.Event().wait()

    finally:
        bot_connected = False

        for task in (polling_task, timer_task):
            if task is not None:
                task.cancel()

        await asyncio.gather(
            *(task for task in (polling_task, timer_task) if task),
            return_exceptions=True,
        )

        if runner is not None:
            await runner.cleanup()

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
