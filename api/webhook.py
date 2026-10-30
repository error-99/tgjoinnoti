import logging
from fastapi import FastAPI, Request, HTTPException
from aiogram import Dispatcher
from aiogram.types import Update

from bot.core import WEBHOOK_SECRET, create_bot
from bot.handlers import setup

log = logging.getLogger("webhook")
logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
setup(dp)

app = FastAPI()


@app.post("/")
@app.post("/api/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Bad secret")

    data = await request.json()
    bot = create_bot()
    try:
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        log.exception(f"Error processing update: {e}")
    finally:
        await bot.session.close()

    return {"ok": True}
