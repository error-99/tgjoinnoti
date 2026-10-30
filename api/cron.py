import time
import logging
from fastapi import FastAPI, Request, HTTPException

from bot.core import create_bot, get_config, WEBHOOK_SECRET
from bot.actions import refresh_all_targets, get_share_link

log = logging.getLogger("cron")
logging.basicConfig(level=logging.INFO)

app = FastAPI()


@app.get("/")
@app.get("/api/cron")
async def cron(request: Request, secret: str = ""):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")

    cfg = await get_config()
    interval = cfg.get("interval_seconds", 240)
    last_joined = cfg.get("last_member_joined_at", 0)
    now = int(time.time())

    if now - last_joined < interval:
        return {"status": "skipped", "reason": "recent join",
                "age": now - last_joined}

    bot = create_bot()
    try:
        # refresh invite link (in case it expired)
        try:
            await get_share_link(bot, cfg, force_refresh=True)
        except Exception as e:
            log.warning(f"Share link refresh failed: {e}")

        results = await refresh_all_targets(bot, cfg)
        return {"status": "ok",
                "results": {str(k): v for k, v in results.items()}}
    finally:
        await bot.session.close()
