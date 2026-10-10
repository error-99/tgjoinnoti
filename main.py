import asyncio
import html
import logging
import os
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramAPIError

# ================= CONFIGURATION =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.getenv("CHAT_ID", "-1004384102181"))
INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

dp = Dispatcher()

# In-memory state only; no database.
previous_message_id = None
last_member_html = "বন্ধু"
group_invite_link = None

refresh_event = asyncio.Event()
replace_lock = asyncio.Lock()


# ================= GROUP LINK =================

async def get_group_link(bot: Bot) -> str:
    """
    Automatically obtain the group's public link or create
    an invite link for a private group.
    """
    chat = await bot.get_chat(CHAT_ID)

    if chat.username:
        return f"https://t.me/{chat.username}"

    # For private groups, the bot must be an administrator
    # with permission to invite users.
    invite = await bot.create_chat_invite_link(
        chat_id=CHAT_ID,
        name="Group sharing link",
    )

    return invite.invite_link


# ================= MESSAGE BUTTONS =================

def make_keyboard() -> InlineKeyboardMarkup:
    share_text = "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️"

    # Both buttons open Telegram's share chooser.
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(group_invite_link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔘 শেয়ার ২/৩ করুন",
                    url=share_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔘 ভিডিও দেখুন 🎥",
                    url=share_url,
                )
            ],
        ]
    )


# ================= PROMOTION MESSAGE =================

def make_message() -> str:
    return (
        f"প্রিয় {last_member_html}, আপনাকে "
        "হিজাবি ভিডিওগ্রুপে জয়েন হওয়ার জন্য ধন্যবাদ। ❤️\n\n"
        "হিজাবি ভিডিওগ্রুপে ভিডিও দেখতে লিংকটি "
        "৩টি গ্রুপে শেয়ার করুন। "
        "৩টি গ্রুপে শেয়ার করলে ভিডিওটি আনলক হবে।"
    )


async def replace_promo_message(bot: Bot):
    """
    Delete the last promotion message, if known,
    then send the replacement.
    """
    global previous_message_id

    async with replace_lock:
        if previous_message_id is not None:
            try:
                await bot.delete_message(
                    chat_id=CHAT_ID,
                    message_id=previous_message_id,
                )
                logging.info(
                    "Deleted previous message: %s",
                    previous_message_id,
                )
            except TelegramAPIError as error:
                logging.warning(
                    "Could not delete previous message: %s",
                    error,
                )

        try:
            sent = await bot.send_message(
                chat_id=CHAT_ID,
                text=make_message(),
                reply_markup=make_keyboard(),
            )

            previous_message_id = sent.message_id

            logging.info(
                "Promotion message sent: %s",
                previous_message_id,
            )

        except TelegramAPIError:
            logging.exception("Could not send promotion message")
            return

        # Restart the inactivity timer after each replacement.
        refresh_event.set()


# ================= NEW MEMBER HANDLER =================

@dp.message(F.new_chat_members)
async def welcome_new_member(message: Message, bot: Bot):
    global last_member_html

    if message.chat.id != CHAT_ID:
        return

    members = [
        member
        for member in message.new_chat_members
        if not member.is_bot
    ]

    if not members:
        return

    # Use the latest human member's name.
    member = members[-1]

    last_member_html = (
        f'<a href="tg://user?id={member.id}">'
        f'{html.escape(member.full_name)}'
        "</a>"
    )

    logging.info("New member joined: %s", member.full_name)

    # Immediately replace the current promotion message.
    await replace_promo_message(bot)


# ================= ONE-MINUTE REFRESH =================

async def refresh_loop(bot: Bot):
    while True:
        try:
            # Wait until someone joins, or until 60 seconds pass.
            await asyncio.wait_for(
                refresh_event.wait(),
                timeout=INTERVAL_SECONDS,
            )
            refresh_event.clear()

        except asyncio.TimeoutError:
            logging.info("No new member; refreshing promotion message")
            await replace_promo_message(bot)


# ================= STARTUP =================

async def main():
    global group_invite_link

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    try:
        # This bot uses polling. It must not simultaneously use
        # the Telegram Serverless webhook with the same token.
        await bot.delete_webhook(drop_pending_updates=False)

        group_invite_link = await get_group_link(bot)

        logging.info("Group link obtained successfully")

        # Send the first message immediately.
        await replace_promo_message(bot)

        # Start the timer in the background.
        timer_task = asyncio.create_task(refresh_loop(bot))

        try:
            await dp.start_polling(bot)
        finally:
            timer_task.cancel()
            await asyncio.gather(
                timer_task,
                return_exceptions=True,
            )

    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
