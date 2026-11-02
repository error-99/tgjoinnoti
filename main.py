import os
import io
import json
import time
import asyncio
import logging
from html import escape
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

# ======================================================================
# CONFIGURATION
# ======================================================================
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "secret123") 
ADMIN_IDS    = [int(x) for x in os.getenv("ADMIN_IDS", "5087403859").split(",") if x.strip()]
PORT         = int(os.getenv("PORT", "8000"))
PUBLIC_URL   = os.getenv("PUBLIC_URL", "") 
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
    
    # DM Settings
    "dm_text": "স্বাগতম! আমাদের বট ব্যবহার করার জন্য ধন্যবাদ। ❤️",
    "dm_buttons": [],
    
    # Target Group Auto-Post Settings
    "target_msg_text": "নতুন সদস্য {name} যোগদান করেছেন! ❤️\n\nভিডিও দেখতে নিচের লিংকটি ৩টি গ্রুপে শেয়ার করুন।",
    "share_msg": "হিজাবি ভিডিওগ্রুপে যোগ দিন ❤️",
    "share_1_text": "📤 শেয়ার করুন ১",
    "share_1_url": "https://t.me/your_channel",
    "share_2_text": "📤 শেয়ার করুন ২",
    "share_2_url": "https://t.me/your_channel_2"
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

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ======================================================================
# TELEGRAM BOT LOGIC
# ======================================================================
def build_keyboard(state: dict) -> InlineKeyboardMarkup:
    rows = []
    
    if state.get("share_1_url"):
        s_url1 = f"https://t.me/share/url?url={state['share_1_url']}&text={quote(state.get('share_msg', ''))}"
        rows.append([InlineKeyboardButton(text=state.get("share_1_text", "Share 1"), url=s_url1)])
        
    if state.get("share_2_url"):
        s_url2 = f"https://t.me/share/url?url={state['share_2_url']}&text={quote(state.get('share_msg', ''))}"
        rows.append([InlineKeyboardButton(text=state.get("share_2_text", "Share 2"), url=s_url2)])
    
    for b in state.get("custom_buttons", []):
        rows.append([InlineKeyboardButton(text=b["text"], url=b["url"])])
        
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def post_to_targets(bot: Bot, member_name: str):
    state = load_state()
    targets = state.get("targets", [])
    if not targets: return
        
    # Replace the {name} placeholder with the actual HTML-formatted user name
    raw_text = state.get("target_msg_text", DEFAULT_STATE["target_msg_text"])
    msg_text = raw_text.replace("{name}", member_name)
    
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

# --- AUTO SEND & AUTO BACKUP LOOPS ---
async def auto_sender_task():
    while True:
        await asyncio.sleep(30)
        state = load_state()
        now = int(time.time())
        if (now - state["last_sent_time"]) >= state["interval"]:
            if state["targets"]:
                await post_to_targets(bot, state["last_member"])

async def auto_backup_task():
    while True:
        await asyncio.sleep(3600)
        state = load_state()
        backup_data = json.dumps(state, indent=2).encode('utf-8')
        doc = BufferedInputFile(backup_data, filename="bot_state.json")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(
                    chat_id=admin_id, document=doc, 
                    caption="🕒 <b>Hourly Auto-Backup</b>\nForward this back to me to restore your settings!"
                )
            except TelegramAPIError:
                pass

# --- TELEGRAM COMMANDS ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    state = load_state()
    is_adm = await is_admin(message.from_user.id)
    
    dm_text = state.get("dm_text", "স্বাগতম! ❤️")
    dm_rows = []
    for b in state.get("dm_buttons", []):
        dm_rows.append([InlineKeyboardButton(text=b["text"], url=b["url"])])
    markup = InlineKeyboardMarkup(inline_keyboard=dm_rows) if dm_rows else None
    
    await message.answer(dm_text, reply_markup=markup, disable_web_page_preview=True)
    
    if is_adm:
        base_url = PUBLIC_URL.rstrip("/") if PUBLIC_URL else "https://tgjoinnoti-b0gj.onrender.com"
        text = (
            "⚙️ <b>Admin Controls</b>\n"
            f"👉 <a href='{base_url}/admin?secret={ADMIN_SECRET}'>Open Web Dashboard</a>\n\n"
            "<i>Commands: /backup to get config file instantly.</i>"
        )
        await message.answer(text, disable_web_page_preview=True)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id): return
    base_url = PUBLIC_URL.rstrip("/") if PUBLIC_URL else "https://tgjoinnoti-b0gj.onrender.com"
    text = (
        "⚙️ <b>Bot Web Dashboard</b>\n\n"
        f"👉 <a href='{base_url}/admin?secret={ADMIN_SECRET}'>Open Web Admin</a>\n\n"
        "<i>Commands: /backup to get config file instantly.</i>"
    )
    await message.answer(text, disable_web_page_preview=True)

@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if not await is_admin(message.from_user.id): return
    state = load_state()
    backup_data = json.dumps(state, indent=2).encode('utf-8')
    doc = BufferedInputFile(backup_data, filename="bot_state.json")
    await message.answer_document(document=doc, caption="📦 <b>Instant Backup File</b>\nForward this back to restore.")

# --- NEW MEMBER TRIGGER ---
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.is_bot: return
    state = load_state()
    if event.chat.id not in state["triggers"]: return
    name_html = f'<a href="tg://user?id={event.new_chat_member.user.id}">{escape(event.new_chat_member.user.full_name)}</a>'
    await post_to_targets(event.bot, name_html)

# --- MANUAL RESTORE SYSTEM ---
@dp.message(F.document)
async def restore_backup(message: Message):
    if not await is_admin(message.from_user.id): return
    if not message.document.file_name.endswith('.json'): return
    try:
        file_in_memory = io.BytesIO()
        await bot.download(message.document, destination=file_in_memory)
        file_in_memory.seek(0)
        
        new_state = json.load(file_in_memory)
        for k, v in DEFAULT_STATE.items():
            new_state.setdefault(k, v)
        save_state(new_state)
        await message.answer("✅ <b>Backup restored successfully!</b>")
    except Exception as e:
        await message.answer(f"❌ <b>Error restoring backup:</b>\n{e}")

# ======================================================================
# FASTAPI WEB DASHBOARD & API
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.delete_webhook(drop_pending_updates=True) 
    polling_task = asyncio.create_task(dp.start_polling(bot))
    sender_task = asyncio.create_task(auto_sender_task())
    backup_task = asyncio.create_task(auto_backup_task())
    yield 
    polling_task.cancel()
    sender_task.cancel()
    backup_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return RedirectResponse(url="/health")

@app.get("/health")
async def health_check():
    return JSONResponse(content={"status": "online"})

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
    state["dm_text"] = data.get("dm_text", DEFAULT_STATE["dm_text"])
    state["dm_buttons"] = data.get("dm_buttons", [])
    
    state["target_msg_text"] = data.get("target_msg_text", DEFAULT_STATE["target_msg_text"])
    state["share_msg"] = data.get("share_msg", "")
    state["share_1_text"] = data.get("share_1_text", "")
    state["share_1_url"] = data.get("share_1_url", "")
    state["share_2_text"] = data.get("share_2_text", "")
    state["share_2_url"] = data.get("share_2_url", "")
    
    save_state(state)
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
async def admin_ui(secret: str = ""):
    if secret != ADMIN_SECRET:
        return HTMLResponse("<h1>Unauthorized. Check your secret in the URL.</h1>", status_code=403)
        
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Web Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
</head>
<body class="bg-gray-900 text-gray-100 p-4 md:p-8">
    <div id="app" class="max-w-4xl mx-auto">
        <div class="flex flex-col md:flex-row justify-between items-start md:items-center mb-8 border-b border-gray-700 pb-4 gap-4">
            <h1 class="text-3xl font-bold text-blue-400">🤖 Bot Dashboard</h1>
            <button @click="saveConfig" class="bg-blue-600 hover:bg-blue-500 text-white px-8 py-3 rounded-lg font-bold shadow-lg transition w-full md:w-auto">
                {{{{ saving ? 'Saving...' : 'SAVE ALL SETTINGS' }}}}
            </button>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            <div class="bg-gray-800 p-6 rounded-xl border border-gray-700">
                <h2 class="text-xl font-semibold mb-2">🎯 Trigger Channels</h2>
                <p class="text-gray-400 text-sm mb-4">Chat IDs where users join.</p>
                <div v-for="(t, idx) in config.triggers" :key="'trig'+idx" class="flex gap-2 mb-2">
                    <input v-model="config.triggers[idx]" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text" placeholder="-100123456789">
                    <button @click="config.triggers.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 rounded text-white">X</button>
                </div>
                <button @click="config.triggers.push('')" class="text-blue-400 hover:text-blue-300 text-sm mt-2 font-medium">+ Add Trigger ID</button>
            </div>

            <div class="bg-gray-800 p-6 rounded-xl border border-gray-700">
                <h2 class="text-xl font-semibold mb-2">📢 Target Groups</h2>
                <p class="text-gray-400 text-sm mb-4">Chat IDs where bot posts the links.</p>
                <div v-for="(t, idx) in config.targets" :key="'targ'+idx" class="flex gap-2 mb-2">
                    <input v-model="config.targets[idx]" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text" placeholder="-100123456789">
                    <button @click="config.targets.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 rounded text-white">X</button>
                </div>
                <button @click="config.targets.push('')" class="text-blue-400 hover:text-blue-300 text-sm mt-2 font-medium">+ Add Target ID</button>
            </div>
        </div>

        <!-- NEW: Editable Target Message -->
        <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 mb-8 border-l-4 border-l-green-500">
            <h2 class="text-xl font-bold text-white mb-2">📝 Auto-Post Message</h2>
            <p class="text-gray-400 text-sm mb-4">This is the text the bot posts to the Target Groups. Type <code>{{name}}</code> exactly like that where you want the user's name to show up.</p>
            <textarea v-model="config.target_msg_text" rows="4" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white"></textarea>
        </div>

        <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 mb-8 border-l-4 border-l-blue-500">
            <h2 class="text-xl font-bold text-white mb-2">📤 Main Share Buttons</h2>
            <p class="text-gray-400 text-sm mb-6">These generate the Telegram forward link automatically. They will appear stacked on top of each other.</p>
            
            <div class="mb-4">
                <label class="block text-sm text-gray-300 mb-1">Text user sees when sharing (e.g., "Join here!")</label>
                <input v-model="config.share_msg" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text">
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div class="bg-gray-900/50 p-4 rounded border border-gray-700">
                    <h3 class="font-semibold text-gray-300 mb-3">Share Button 1</h3>
                    <label class="block text-xs text-gray-500 mb-1">Button Text</label>
                    <input v-model="config.share_1_text" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white mb-3" type="text">
                    <label class="block text-xs text-gray-500 mb-1">Link to Share (e.g. https://t.me/your_channel)</label>
                    <input v-model="config.share_1_url" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text">
                </div>

                <div class="bg-gray-900/50 p-4 rounded border border-gray-700">
                    <h3 class="font-semibold text-gray-300 mb-3">Share Button 2</h3>
                    <label class="block text-xs text-gray-500 mb-1">Button Text</label>
                    <input v-model="config.share_2_text" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white mb-3" type="text">
                    <label class="block text-xs text-gray-500 mb-1">Link to Share (e.g. https://t.me/your_channel)</label>
                    <input v-model="config.share_2_url" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white" type="text">
                </div>
            </div>
        </div>

        <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 mb-8">
            <h2 class="text-xl font-semibold mb-2">🔗 Custom URL Buttons</h2>
            <p class="text-gray-400 text-sm mb-4">These appear below the 2 share buttons.</p>
            <div v-for="(btn, idx) in config.custom_buttons" :key="'btn'+idx" class="flex flex-col md:flex-row gap-2 mb-3 bg-gray-900/50 p-3 rounded border border-gray-700">
                <input v-model="btn.text" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-1/3 text-white" type="text" placeholder="Button Name">
                <input v-model="btn.url" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-2/3 text-white" type="text" placeholder="https://t.me/...">
                <button @click="config.custom_buttons.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 py-2 md:py-0 rounded text-white">Remove</button>
            </div>
            <button @click="config.custom_buttons.push({{text: '', url: ''}})" class="text-blue-400 hover:text-blue-300 text-sm font-medium">+ Add Extra Button</button>
        </div>

        <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 mb-8">
            <h2 class="text-xl font-semibold mb-2">👤 Personal Message (/start)</h2>
            <textarea v-model="config.dm_text" rows="3" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-white mb-4"></textarea>
            <div v-for="(btn, idx) in config.dm_buttons" :key="'dm_btn'+idx" class="flex flex-col md:flex-row gap-2 mb-3 bg-gray-900/50 p-3 rounded border border-gray-700">
                <input v-model="btn.text" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-1/3 text-white" type="text" placeholder="Button Name">
                <input v-model="btn.url" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-2/3 text-white" type="text" placeholder="https://t.me/...">
                <button @click="config.dm_buttons.splice(idx, 1)" class="bg-red-600/80 hover:bg-red-500 px-3 py-2 md:py-0 rounded text-white">Remove</button>
            </div>
            <button @click="config.dm_buttons.push({{text: '', url: ''}})" class="text-blue-400 hover:text-blue-300 text-sm font-medium">+ Add DM Button</button>
        </div>

        <div class="bg-gray-800 p-6 rounded-xl border border-gray-700">
            <h2 class="text-xl font-semibold mb-4">⚙️ System Settings</h2>
            <label class="block text-sm text-gray-400 mb-1">Cooldown Interval (Seconds)</label>
            <input v-model="config.interval" class="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full md:w-1/3 text-white" type="number" min="30">
        </div>
        
        <div v-if="toast" class="fixed bottom-4 right-4 bg-green-600 text-white px-6 py-3 rounded shadow-lg transition-opacity duration-300 z-50">
            ✅ Settings Saved Successfully!
        </div>
    </div>

    <script>
        const secret = "{secret}";
        const {{ createApp }} = Vue;

        createApp({{
            data() {{
                return {{
                    config: {{ triggers: [], targets: [], custom_buttons: [], interval: 300, dm_text: "", dm_buttons: [], target_msg_text: "", share_msg: "", share_1_text: "", share_1_url: "", share_2_text: "", share_2_url: "" }},
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
                    this.config.dm_text = data.dm_text || "স্বাগতম! ❤️";
                    this.config.dm_buttons = data.dm_buttons || [];
                    
                    this.config.target_msg_text = data.target_msg_text || "নতুন সদস্য {{name}} যোগদান করেছেন! ❤️\\n\\nভিডিও দেখতে নিচের লিংকটি ৩টি গ্রুপে শেয়ার করুন।";
                    this.config.share_msg = data.share_msg || "";
                    this.config.share_1_text = data.share_1_text || "";
                    this.config.share_1_url = data.share_1_url || "";
                    this.config.share_2_text = data.share_2_text || "";
                    this.config.share_2_url = data.share_2_url || "";
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
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
