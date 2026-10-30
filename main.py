import os
import json
import time
import asyncio
import logging
from html import escape
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

# ======================================================================
# CONFIGURATION
# ======================================================================
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "secret123") # Password for the web UI
PORT         = int(os.getenv("PORT", "8000"))
STATE_FILE   = "bot_state.json"

if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

DEFAULT_STATE = {
    "triggers": [],       
    "targets": [],        
    "custom_buttons": [], 
    "last_member": "নতুন বন্ধু",
    "last_sent_time": 0,
    "target_msg_ids": {}, 
    "interval": 300,      
}

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# ======================================================================
# JSON STORAGE
# ======================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in DEFAULT_STATE.items():
                    data.setdefault(k, v)
                return data
        except Exception:
            pass
    return dict(DEFAULT_STATE)

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ======================================================================
# TELEGRAM BOT LOGIC (NO ADMIN COMMANDS)
# ======================================================================
def build_keyboard(state: dict) -> InlineKeyboardMarkup:
    rows = []
    
    # --- 1. Fixed Share Buttons ---
    share_text = quote("হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️")
    share_url_1 = f"https://t.me/share/url?url=https://t.me/joinchat/...&text={share_text}"
    share_url_2 = f"https://t.me/share/url?url=https://t.me/joinchat/...&text={share_text}"
    
    rows.append([
        InlineKeyboardButton(text="📤 শেয়ার করুন ১", url=share_url_1),
        InlineKeyboardButton(text="📤 শেয়ার করুন ২", url=share_url_2)
    ])
    
    # --- 2. Custom Buttons from Web UI ---
    for b in state.get("custom_buttons", []):
        rows.append([InlineKeyboardButton(text=b["text"], url=b["url"])])
        
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def post_to_targets(bot: Bot, member_name: str):
    state = load_state()
    targets = state.get("targets", [])
    if not targets: return
        
    msg_text = (
        f"নতুন সদস্য {member_name} যোগদান করেছেন! ❤️\n\n"
        "ভিডিও দেখতে নিচের লিংকটি ৩টি গ্রুপে শেয়ার করুন।"
    )
    keyboard = build_keyboard(state)
    
    for chat_id in targets:
        old_msg_id = state["target_msg_ids"].get(str(chat_id))
        if old_msg_id:
            try: await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except TelegramAPIError: pass
                
        try:
            sent = await bot.send_message(chat_id=chat_id, text=msg_text, reply_markup=keyboard)
            state["target_msg_ids"][str(chat_id)] = sent.message_id
        except TelegramAPIError as e:
            log.error(f"Failed to send to {chat_id}: {e}")
            
    state["last_member"] = member_name
    state["last_sent_time"] = int(time.time())
    save_state(state)

async def auto_sender_task():
    while True:
        await asyncio.sleep(30)
        state = load_state()
        now = int(time.time())
        if (now - state["last_sent_time"]) >= state["interval"]:
            if state["targets"]:
                log.info("Auto-sending interval reached.")
                await post_to_targets(bot, state["last_member"])

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.is_bot: return
    state = load_state()
    if event.chat.id not in state["triggers"]: return
    
    name_html = f'<a href="tg://user?id={event.new_chat_member.user.id}">{escape(event.new_chat_member.user.full_name)}</a>'
    await post_to_targets(event.bot, name_html)

# ======================================================================
# FASTAPI WEB DASHBOARD & API
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.delete_webhook(drop_pending_updates=True) 
    polling_task = asyncio.create_task(dp.start_polling(bot))
    loop_task = asyncio.create_task(auto_sender_task())
    yield 
    polling_task.cancel()
    loop_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health_check():
    return {"status": "online"}

@app.get("/api/state")
async def api_get_state(secret: str = ""):
    if secret != ADMIN_SECRET: raise HTTPException(403, "Unauthorized")
    return load_state()

@app.post("/api/state")
async def api_save_state(request: Request, secret: str = ""):
    if secret != ADMIN_SECRET: raise HTTPException(403, "Unauthorized")
    data = await request.json()
    
    state = load_state()
    state["triggers"] = [int(x) for x in data.get("triggers", []) if str(x).strip()]
    state["targets"] = [int(x) for x in data.get("targets", []) if str(x).strip()]
    state["interval"] = int(data.get("interval", 300))
    state["custom_buttons"] = data.get("custom_buttons", [])
    save_state(state)
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
async def admin_ui(secret: str = ""):
    if secret != ADMIN_SECRET:
        return HTMLResponse("<h1>Unauthorized. Check your secret in the URL.</h1>", status_code=403)
        
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bot Web Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    </head>
    <body class="bg-gray-900 text-gray-100 p-4 md:p-8">
        <div id="app" class="max-w-3xl mx-auto">
            <div class="flex justify-between items-center mb-8 border-b border-gray-700 pb-4">
                <h1 class="text-3xl font-bold text-blue-400">🤖 Bot Dashboard</h1>
                <button @click="saveConfig" class="bg-blue-600 hover:bg-blue-500 text-white px-6 py-2 rounded-lg font-semibold transition">
                    {{{{ saving ? 'Saving...' : 'Save Settings' }}}}
                </button>
            </div>

            <!-- Triggers & Targets -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
                <!-- Triggers -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700">
                    <h2 class="text-xl font-semibold mb-2">🎯 Trigger Channels</h2>
                    <p class="text-gray-400 text-sm mb-4">Chat IDs where users join.</p>
                    <div v-for="(t, idx) in config.triggers" :key="'trig'+idx" class="flex gap-2 mb-2">
                        <input v-model="config.triggers[idx]" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text" placeholder="-100123456789">
                        <button @click="config.triggers.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 rounded text-white">X</button>
                    </div>
                    <button @click="config.triggers.push('')" class="text-blue-400 hover:text-blue-300 text-sm mt-2 font-medium">+ Add Trigger ID</button>
                </div>

                <!-- Targets -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700">
                    <h2 class="text-xl font-semibold mb-2">📢 Target Groups</h2>
                    <p class="text-gray-400 text-sm mb-4">Chat IDs where bot sends links.</p>
                    <div v-for="(t, idx) in config.targets" :key="'targ'+idx" class="flex gap-2 mb-2">
                        <input v-model="config.targets[idx]" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text" placeholder="-100123456789">
                        <button @click="config.targets.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 rounded text-white">X</button>
                    </div>
                    <button @click="config.targets.push('')" class="text-blue-400 hover:text-blue-300 text-sm mt-2 font-medium">+ Add Target ID</button>
                </div>
            </div>

            <!-- Custom Buttons -->
            <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 mb-8">
                <h2 class="text-xl font-semibold mb-2">🔗 Custom URL Buttons</h2>
                <p class="text-gray-400 text-sm mb-4">These appear below the 2 fixed share buttons.</p>
                <div v-for="(btn, idx) in config.custom_buttons" :key="'btn'+idx" class="flex flex-col md:flex-row gap-2 mb-3 bg-gray-900/50 p-3 rounded border border-gray-700">
                    <input v-model="btn.text" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-1/3 text-white" type="text" placeholder="Button Name">
                    <input v-model="btn.url" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-2/3 text-white" type="text" placeholder="https://t.me/...">
                    <button @click="config.custom_buttons.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 py-2 md:py-0 rounded text-white">Remove</button>
                </div>
                <button @click="config.custom_buttons.push({{text: '', url: ''}})" class="text-blue-400 hover:text-blue-300 text-sm font-medium">+ Add Button</button>
            </div>

            <!-- Settings -->
            <div class="bg-gray-800 p-6 rounded-xl border border-gray-700">
                <h2 class="text-xl font-semibold mb-4">⚙️ Auto-Send Settings</h2>
                <label class="block text-sm text-gray-400 mb-1">Cooldown Interval (Seconds)</label>
                <input v-model="config.interval" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-1/3 text-white" type="number" min="30">
                <p class="text-gray-500 text-xs mt-2">If no one joins for this amount of time, the bot will automatically delete the old message and resend it to stay at the bottom of the chat.</p>
            </div>
            
            <!-- Toast Notification -->
            <div v-if="toast" class="fixed bottom-4 right-4 bg-green-600 text-white px-6 py-3 rounded shadow-lg transition-opacity duration-300">
                ✅ Settings Saved Successfully!
            </div>
        </div>

        <script>
            const secret = "{secret}";
            const { createApp } = Vue;

            createApp({{
                data() {{
                    return {{
                        config: {{ triggers: [], targets: [], custom_buttons: [], interval: 300 }},
                        saving: false,
                        toast: false
                    }}
                }},
                async mounted() {{
                    const res = await fetch('/api/state?secret=' + secret);
                    if(res.ok) {{
                        const data = await res.json();
                        this.config.triggers = data.triggers || [];
                        this.config.targets = data.targets || [];
                        this.config.custom_buttons = data.custom_buttons || [];
                        this.config.interval = data.interval || 300;
                    }}
                }},
                methods: {{
                    async saveConfig() {{
                        this.saving = true;
                        await fetch('/api/state?secret=' + secret, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify(this.config)
                        }});
                        this.saving = false;
                        this.showToast();
                    }},
                    showToast() {{
                        this.toast = true;
                        setTimeout(() => this.toast = false, 3000);
                    }}
                }}
            }}).mount('#app')
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
