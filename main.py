import asyncio
import html
import json
import logging
import os
from urllib.parse import quote

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.exceptions import TelegramAPIError

# ==================================================
# CONFIGURATION
# ==================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

CHAT_ID = int(os.getenv("CHAT_ID", "-1004398264709"))
INTERVAL_SECONDS = 240
STATE_FILE = "last_member.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgjoinnoti")

dp = Dispatcher()

# ==================================================
# STATE VARIABLES
# ==================================================
previous_message_id = None
member_name_html = "বন্ধু"          # default
group_invite_link = None

member_join_event = asyncio.Event()
replace_lock = asyncio.Lock()


# ==================================================
# PERSISTENCE
# ==================================================
def save_last_member(name_html: str):
    """Save the last joined member's HTML name to a JSON file."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"member_name_html": name_html}, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Could not save state: {e}")

def load_last_member() -> str:
    """Load the last joined member's HTML name from the JSON file."""
    if not os.path.exists(STATE_FILE):
        return "বন্ধু"
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("member_name_html", "বন্ধু")
    except Exception as e:
        log.warning(f"Could not load state: {e}")
        return "বন্ধু"


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
    link = str(group_invite_link) if group_invite_link else ""
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 শেয়ার ২/৩ করুন", url=share_url)],
            [InlineKeyboardButton(text="🎥 ভিডিও দেখুন", url=share_url)]  # change this URL if you have a real video link
        ]
    )

def make_message(name_html: str = None) -> str:
    name = name_html or member_name_html
    return (
        f"প্রিয় {name}, আপনাকে "
        "হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি "
        "৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    )

async def replace_message(bot: Bot, name_html: str = None):
    global previous_message_id

    async with replace_lock:
        if previous_message_id is not None:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=previous_message_id)
                log.info(f"Deleted previous message {previous_message_id}")
                previous_message_id = None
            except TelegramAPIError as exc:
                log.warning(f"Could not delete old message: {exc}")

        try:
            sent = await bot.send_message(
                chat_id=CHAT_ID,
                text=make_message(name_html),
                reply_markup=make_keyboard()
            )
            previous_message_id = sent.message_id
            log.info(f"Promotion sent; message_id={previous_message_id}")
        except Exception as exc:
            log.exception(f"Failed to send promotion message: {exc}")

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def new_member_handler(event: ChatMemberUpdated, bot: Bot):
    global member_name_html

    if event.chat.id != CHAT_ID:
        return

    user = event.new_chat_member.user
    if user.is_bot:
        return

    member_name_html = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    save_last_member(member_name_html)
    log.info(f"New group member: {user.full_name}")

    member_join_event.set()
    await replace_message(bot, member_name_html)

async def refresh_loop(bot: Bot):
    while True:
        try:
            await asyncio.wait_for(member_join_event.wait(), timeout=INTERVAL_SECONDS)
            member_join_event.clear()
        except asyncio.TimeoutError:
            log.info(f"No new member for {INTERVAL_SECONDS}s; refreshing message")
            await replace_message(bot)


# ==================================================
# APP STARTUP
# ==================================================
async def main():
    global group_invite_link, member_name_html

    # Load last joined member from file
    member_name_html = load_last_member()
    log.info(f"Loaded last member: {member_name_html}")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        log.info(f"Connected as @{me.username}")
        
        group_invite_link = await get_group_link(bot)
        
        # Send the welcome message on startup using the saved member name
        await replace_message(bot, member_name_html)

        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=["message", "chat_member"]),
            refresh_loop(bot)
        )
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped manually.")
