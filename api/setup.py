from fastapi import FastAPI, Request, HTTPException
from aiogram.types import BotCommand

from bot.core import create_bot, WEBHOOK_SECRET

app = FastAPI()


@app.get("/")
@app.get("/api/setup")
async def setup(request: Request, secret: str = ""):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")

    host = request.headers.get("host", "")
    webhook_url = f"https://{host}/api/webhook"

    bot = create_bot()
    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "chat_member"],
            drop_pending_updates=True,
        )
        await bot.set_my_commands([
            BotCommand(command="start", description="🎬 Get the video link"),
            BotCommand(command="admin", description="🛠 Admin panel"),
        ])
        info = await bot.get_webhook_info()
        return {"webhook_url": webhook_url, "info": str(info)}
    finally:
        await bot.session.close()
