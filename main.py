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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")  # আপনার টোকেন দিন বা environment variable ব্যবহার করুন
CHAT_ID = int(os.getenv("CHAT_ID", "-1004398264709"))
PORT = int(os.getenv("PORT", "8080"))
INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tgjoinnoti")

dp = Dispatcher()

# In-memory state only; no database.
started_at = time.time()
previous_message_id = None
member_name_html = "বন্ধু"
group_invite_link = None
bot_username = None
bot_connected = False
polling_running = False
last_sent_at = None
last_error = None

member_join_event = asyncio.Event()
replace_lock = asyncio.Lock()


# ==================================================
# GROUP INVITE LINK
# ==================================================

async def get_group_link(bot: Bot) -> str:
    chat = await bot.get_chat(CHAT_ID)

    if chat.username:
        return f"https://t.me/{chat.username}"

    # Private group: the bot must have permission to
    # create invite links.
    if chat.invite_link:
        return chat.invite_link

    invite = await bot.create_chat_invite_link(
        chat_id=CHAT_ID,
        name="Share group link",
    )
    return invite.invite_link


# ==================================================
# BUTTONS (Fixed)
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

            log.info(
                "Promotion sent; message_id=%s",
                previous_message_id,
            )

        except Exception as exc:
            last_error = str(exc)
            log.exception("Failed to send promotion message")


# ==================================================
# WHEN A USER JOINS (RELIABLE METHOD)
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

    log.info("New group member caught via chat_member: %s", user.full_name)

    # Reset the inactivity timer and send the message immediately.
    member_join_event.set()
    await replace_message(bot)


# ==================================================
# REPEAT AFTER 60 SECONDS OF INACTIVITY
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
            log.info("No new member for 60 seconds; refreshing message")
            await replace_message(bot)


# ==================================================
# WEB HEALTH DASHBOARD
# ==================================================

def health_data():
    return {
        "status": (
            "healthy"
            if bot_connected and polling_running
            else "starting_or_reconnecting"
        ),
        "bot_username": bot_username,
        "chat_id": CHAT_ID,
        "bot_connected": bot_connected,
        "polling_running": polling_running,
        "uptime_seconds": int(time.time() - started_at),
        "interval_seconds": INTERVAL_SECONDS,
        "last_message_id": previous_message_id,
        "last_sent_at": last_sent_at,
        "last_error": last_error,
        "database": "not used",
    }


async def health_json(request):
    data = health_data()
    status_code = (
        200
        if data["bot_connected"] and data["polling_running"]
        else 503
    )
    return web.json_response(data, status=status_code)


async def health_page(request):
    data = health_data()

    status_text = (
        "ONLINE"
        if data["bot_connected"] and data["polling_running"]
        else "STARTING / RECONNECTING"
    )
    status_color = (
        "#16a34a"
        if status_text == "ONLINE"
        else "#d97706"
    )

    def show(value):
        return html.escape(str(value if value is not None else "Not yet"))

    error_html = (
        f'<pre class="error">{show(data["last_error"])}</pre>'
        if data["last_error"]
        else '<p class="ok">No recent send error</p>'
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Tgjoinnoti Health</title>
<style>
* {{ box-sizing: border-box; }}
body {{
    margin: 0; padding: 24px;
    background: #0b1120; color: #e5e7eb;
    font-family: system-ui, sans-serif;
}}
main {{ max-width: 760px; margin: 0 auto; }}
h1 {{ margin-bottom: 4px; }}
.muted {{ color: #94a3b8; }}
.status {{
    background: #111c30; border: 1px solid #263449;
    border-radius: 16px; padding: 20px; margin: 20px 0;
}}
.pill {{
    display: inline-block; padding: 7px 12px;
    border-radius: 999px; color: white;
    background: {status_color}; font-weight: 700;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit,minmax(210px,1fr));
    gap: 12px;
}}
.card {{
    background: #111c30; border: 1px solid #263449;
    border-radius: 12px; padding: 16px;
    overflow-wrap: anywhere;
}}
.label {{ color: #94a3b8; font-size: 13px; }}
.value {{ font-size: 18px; font-weight: 650; margin-top: 8px; }}
a {{ color: #60a5fa; }}
.ok {{ color: #4ade80; }}
.error {{
    white-space: pre-wrap; overflow-wrap: anywhere;
    color: #fca5a5; background: #25151a;
    padding: 12px; border-radius: 8px;
}}
footer {{ margin-top: 24px; color: #94a3b8; font-size: 13px; }}
</style>
</head>
<body>
<main>
    <h1>🤖 Tgjoinnoti</h1>
    <p class="muted">Telegram bot health dashboard</p>

    <section class="status">
        <div class="label">CURRENT STATUS</div>
        <p><span class="pill">{status_text}</span></p>
        <p class="muted">This page refreshes every 10 seconds.</p>
    </section>

    <section class="grid">
        <div class="card">
            <div class="label">Bot username</div>
            <div class="value">@{show(bot_username or "starting")}</div>
        </div>
        <div class="card">
            <div class="label">Target group ID</div>
            <div class="value">{CHAT_ID}</div>
        </div>
        <div class="card">
            <div class="label">Bot API connection</div>
            <div class="value">{'Connected' if bot_connected else 'Not connected'}</div>
        </div>
        <div class="card">
            <div class="label">Telegram polling</div>
            <div class="value">{'Running' if polling_running else 'Stopped / starting'}</div>
        </div>
        <div class="card">
            <div class="label">Uptime</div>
            <div class="value">{data["uptime_seconds"]} seconds</div>
        </div>
        <div class="card">
            <div class="label">Refresh interval</div>
            <div class="value">{INTERVAL_SECONDS} seconds</div>
        </div>
        <div class="card">
            <div class="label">Last sent message ID</div>
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
