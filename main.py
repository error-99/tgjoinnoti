import asyncio
import html
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.exceptions import TelegramAPIError

# ==================================================
# CONFIGURATION
# ==================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004398264709"))
PORT = int(os.getenv("PORT", "8080"))
INTERVAL_SECONDS = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgjoinnoti")

dp = Dispatcher()

# ==================================================
# STATE VARIABLES (In-Memory)
# ==================================================
state = {
    "started_at": time.time(),
    "previous_message_id": None,
    "member_name_html": "বন্ধু",
    "group_invite_link": None,
    "bot_username": None,
    "bot_connected": False,
    "polling_running": False,
    "last_sent_at": None,
    "last_error": None,
    "bot_instance": None
}

member_join_event = asyncio.Event()
replace_lock = asyncio.Lock()


# ==================================================
# TELEGRAM LOGIC
# ==================================================
async def get_group_link(bot: Bot) -> str:
    chat = await bot.get_chat(CHAT_ID)
    if chat.username:
        return f"https://t.me/{chat.username}"
    if chat.invite_link:
        return chat.invite_link
    invite = await bot.create_chat_invite_link(chat_id=CHAT_ID, name="Share group link")
    return invite.invite_link

def make_keyboard() -> InlineKeyboardMarkup:
    share_text = "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️"
    link = str(state["group_invite_link"]) if state["group_invite_link"] else ""
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 শেয়ার ২/৩ করুন", url=share_url)],
            [InlineKeyboardButton(text="🎥 ভিডিও দেখুন", url=share_url)]
        ]
    )

def make_message() -> str:
    return (
        f"প্রিয় {state['member_name_html']}, আপনাকে "
        "হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি "
        "৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    )

async def replace_message(bot: Bot):
    async with replace_lock:
        if state["previous_message_id"] is not None:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=state["previous_message_id"])
                log.info(f"Deleted previous message {state['previous_message_id']}")
                state["previous_message_id"] = None
            except TelegramAPIError as exc:
                log.warning(f"Could not delete old message: {exc}")

        try:
            sent = await bot.send_message(chat_id=CHAT_ID, text=make_message(), reply_markup=make_keyboard())
            state["previous_message_id"] = sent.message_id
            state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
            state["last_error"] = None
            log.info(f"Promotion sent; message_id={state['previous_message_id']}")
        except Exception as exc:
            state["last_error"] = str(exc)
            log.exception("Failed to send promotion message")

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def new_member_handler(event: ChatMemberUpdated, bot: Bot):
    if event.chat.id != CHAT_ID:
        return

    user = event.new_chat_member.user
    if user.is_bot:
        return

    state["member_name_html"] = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    log.info(f"New group member: {user.full_name}")
    
    member_join_event.set()
    await replace_message(bot)

async def refresh_loop(bot: Bot):
    while True:
        try:
            await asyncio.wait_for(member_join_event.wait(), timeout=INTERVAL_SECONDS)
            member_join_event.clear()
        except asyncio.TimeoutError:
            log.info(f"No new member for {INTERVAL_SECONDS}s; refreshing message")
            await replace_message(bot)


# ==================================================
# WEB SERVER & DASHBOARD
# ==================================================
def get_dashboard_html(data, request):
    status_text = "ONLINE" if data["bot_connected"] and data["polling_running"] else "OFFLINE"
    status_color = "#10b981" if status_text == "ONLINE" else "#ef4444"
    
    def show(value):
        return html.escape(str(value if value is not None else "N/A"))

    error_html = f'<div class="error-box"><b>Latest Error:</b> {show(data["last_error"])}</div>' if data["last_error"] else ''

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Control Panel</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Inter', sans-serif; background: linear-gradient(135deg, #0f172a, #1e1b4b); color: #f8fafc; min-height: 100vh; padding: 30px 15px; }}
.container {{ max-width: 800px; margin: 0 auto; }}
.header h1 {{ font-size: 2.2rem; background: -webkit-linear-gradient(#60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; text-align: center; margin-bottom: 30px; }}
.glass-panel {{ background: rgba(255, 255, 255, 0.05); backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 16px; padding: 25px; margin-bottom: 25px; }}
.status-badge {{ padding: 6px 14px; border-radius: 20px; background: {status_color}; color: #fff; font-weight: 600; font-size: 0.9rem; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-top: 20px; }}
.stat-card {{ background: rgba(0, 0, 0, 0.2); padding: 15px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); }}
.stat-card label {{ font-size: 0.8rem; color: #94a3b8; text-transform: uppercase; }}
.stat-card .val {{ font-size: 1.2rem; font-weight: 600; margin-top: 5px; color: #e2e8f0; }}
.btn {{ display: block; width: 100%; padding: 14px; background: linear-gradient(135deg, #3b82f6, #8b5cf6); color: white; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; }}
.error-box {{ background: rgba(239, 68, 68, 0.1); border-left: 4px solid #ef4444; padding: 15px; border-radius: 8px; margin-top: 15px; color: #fca5a5; font-size: 0.9rem; }}
.footer {{ text-align: center; color: #64748b; font-size: 0.85rem; margin-top: 20px; }}
</style>
<script>
async function triggerMessage() {{
    const btn = document.getElementById('triggerBtn');
    btn.innerText = 'Sending...';
    try {{
        const res = await fetch('/cron');
        if(res.ok) {{ btn.innerText = '✅ Message Sent!'; setTimeout(() => location.reload(), 1500); }} 
        else {{ btn.innerText = '❌ Failed'; }}
    }} catch (e) {{ btn.innerText = '❌ Error'; }}
    setTimeout(() => {{ btn.innerText = '🚀 Send Message Now (Trigger Cron)'; }}, 3000);
}}
</script>
</head>
<body>
<div class="container">
    <div class="header"><h1>TgJoin Notifier Dashboard</h1></div>
    <div class="glass-panel">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>System Status</h2><span class="status-badge">{status_text}</span>
        </div>
        <div class="grid">
            <div class="stat-card"><label>Bot Username</label><div class="val">@{show(data["bot_username"])}</div></div>
            <div class="stat-card"><label>Group ID</label><div class="val">{show(data["chat_id"])}</div></div>
            <div class="stat-card"><label>Uptime</label><div class="val">{show(data["uptime_seconds"])}s</div></div>
            <div class="stat-card"><label>Last Mentioned User</label><div class="val" style="font-size:1rem;">{data["last_mentioned"]}</div></div>
        </div>
        {error_html}
    </div>
    <div class="glass-panel">
        <h2>Manual Control</h2>
        <p style="color: #94a3b8; margin: 10px 0 20px; font-size: 0.9rem;">
            Click below to manually send the message. You can automate this via GET request to <code>{request.scheme}://{request.host}/cron</code>
        </p>
        <button id="triggerBtn" class="btn" onclick="triggerMessage()">🚀 Send Message Now (Trigger Cron)</button>
    </div>
    <div class="footer">Powered by aiogram & aiohttp • Auto-refreshes internally every 60s</div>
</div>
</body>
</html>"""

async def handle_cron(request):
    if state["bot_instance"]:
        asyncio.create_task(replace_message(state["bot_instance"]))
        return web.json_response({"status": "success", "message": "Triggered"})
    return web.json_response({"status": "error", "message": "Not connected"}, status=503)

def get_health_data():
    return {
        "bot_username": state["bot_username"],
        "chat_id": CHAT_ID,
        "bot_connected": state["bot_connected"],
        "polling_running": state["polling_running"],
        "uptime_seconds": int(time.time() - state["started_at"]),
        "last_message_id": state["previous_message_id"],
        "last_sent_at": state["last_sent_at"],
        "last_error": state["last_error"],
        "last_mentioned": state["member_name_html"].replace("<", "&lt;").replace(">", "&gt;"),
    }

async def health_json(request):
    data = get_health_data()
    return web.json_response(data, status=200 if (state["bot_connected"] and state["polling_running"]) else 503)

async def health_page(request):
    return web.Response(text=get_dashboard_html(get_health_data(), request), content_type="text/html")


# ==================================================
# APP STARTUP
# ==================================================
async def polling_supervisor(bot: Bot):
    while True:
        try:
            state["polling_running"] = True
            log.info("Starting Telegram polling")
            await dp.start_polling(bot, handle_signals=False, allowed_updates=["message", "chat_member"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["last_error"] = str(exc)
            log.exception("Polling failed")
        finally:
            state["polling_running"] = False
        await asyncio.sleep(5)

async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    state["bot_instance"] = bot
    runner = polling_task = timer_task = None

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        state["bot_username"] = me.username
        state["bot_connected"] = True
        state["group_invite_link"] = await get_group_link(bot)
        
        await replace_message(bot)

        polling_task = asyncio.create_task(polling_supervisor(bot))
        timer_task = asyncio.create_task(refresh_loop(bot))

        app = web.Application()
        app.router.add_get("/", health_page)
        app.router.add_get("/cron", handle_cron)
        app.router.add_post("/cron", handle_cron)
        app.router.add_get("/health", health_json)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info(f"Dashboard listening on port {PORT}")

        await asyncio.Event().wait()

    finally:
        state["bot_connected"] = False
        if polling_task: polling_task.cancel()
        if timer_task: timer_task.cancel()
        await asyncio.gather(polling_task, timer_task, return_exceptions=True)
        if runner: await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
 Message Now (Trigger Cron)'; }}, 3000);
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
