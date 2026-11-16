import os
import html
import random
import json
import time
import asyncio
import logging
import urllib.parse
from collections import defaultdict

import yt_dlp
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode, ChatMemberStatus, ChatAction
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import MediaStream, StreamEnded

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vcbot")

# Python 3.12+ mein event loop khud banana padta hai
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ---------------- CONFIG (.env se) ----------------
API_ID = int(os.environ["32118715"])
API_HASH = os.environ["466c3df10f4b3326234558c231d0e020"]
BOT_TOKEN = os.environ["9180281383:AAGsS-4Qbi7Z-FjOD8Gbi6jd9oro1zsEvf8"]
CHANNEL_URL = os.getenv("https://t.me/THESUKOONLYRIC", "").strip()
SUPPORT_URL = os.getenv("https://t.me/MustFixcaz", "").strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()            # YouTube cookies (server ke liye)
ADMIN_ONLY = os.getenv("ADMIN_ONLY", "true").lower() == "true"  # control sirf admins ke liye
MAX_DURATION = int(os.getenv("MAX_DURATION", "14400"))          # seconds (default 4 ghante)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()  # chat AI ke liye (optional)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
AI_COOLDOWN = float(os.getenv("AI_COOLDOWN", "3"))              # ek user ke 2 sawalon ke beech seconds
AUTOCHAT_CHANCE = float(os.getenv("AUTOCHAT_CHANCE", "0.3"))      # 0.3 = lagbhag 3 mein se 1 message pe jawab
AUTOCHAT_COOLDOWN = float(os.getenv("AUTOCHAT_COOLDOWN", "30"))  # ek group mein 2 auto-jawab ke beech seconds
AUTOCHAT_DEFAULT = os.getenv("AUTOCHAT_DEFAULT", "true").lower() == "true"
WELCOME_TEXT = os.getenv("WELCOME_TEXT", "").strip()             # {mention} aur {group} use kar sakte ho
SETTINGS_FILE = "settings.json"
EDIT_DELETE_DEFAULT = os.getenv("EDIT_DELETE_DEFAULT", "true").lower() == "true"
EDIT_DELETE_ADMINS = os.getenv("EDIT_DELETE_ADMINS", "false").lower() == "true"  # true = admins ke edit bhi delete
OWNER_IDS = {
    int(x) for x in os.getenv("9285385174", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

# BOT = commands sunta hai + buttons bhejta hai | ASSISTANT = voice chat mein gaana bajata hai
bot = Client("music_bot", api_id=32118715, api_hash=466c3df10f4b3326234558c231d0e020, bot_token=9180281383:AAGsS-4Qbi7Z-FjOD8Gbi6jd9oro1zsEvf8,
             parse_mode=ParseMode.HTML)
assistant = Client("assistant", api_id=32118715, api_hash=466c3df10f4b3326234558c231d0e020)
call = PyTgCalls(assistant)
ai = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

queues: dict[int, list[dict]] = {}
current: dict[int, dict] = {}
loops: dict[int, bool] = {}
locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
BOT_USERNAME = ""
chat_history: dict[tuple[int, int], list[dict]] = {}
last_ask: dict[int, float] = {}
last_auto: dict[int, float] = {}
vc_notified: dict[tuple[int, int], float] = {}
BOT_ID = 0
ASSISTANT_ID = 0
DEFAULTS = {"autochat": AUTOCHAT_DEFAULT, "welcome": True, "vcnotify": True,
            "editdelete": EDIT_DELETE_DEFAULT}
try:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings: dict = json.load(f)
except Exception:
    settings = {}
SYSTEM_PROMPT = (
    "You are a friendly Telegram bot that plays music in group voice chats and also chats with people. "
    "Reply in the same language the user writes in (Hindi, Hinglish or English). "
    "Keep answers short and clear. Mention music commands like /play only when relevant."
)


# ---------------- Helpers ----------------
def esc(s) -> str:
    return html.escape(str(s))


def fmt_dur(sec, live=False) -> str:
    if live:
        return "🔴 LIVE"
    if not sec:
        return "?"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def link_row():
    row = []
    if CHANNEL_URL:
        row.append(InlineKeyboardButton("📢 Channel", url=https://t.me/THESUKOONLYRIC))
    if SUPPORT_URL:
        row.append(InlineKeyboardButton("💬 Support", url=https://t.me/MustFixcaz))
    return row


def link_buttons():
    row = link_row()
    return InlineKeyboardMarkup([row]) if row else None


def player_buttons():
    rows = [[
        InlineKeyboardButton("⏸", callback_data="ctl_pause"),
        InlineKeyboardButton("▶️", callback_data="ctl_resume"),
        InlineKeyboardButton("⏭", callback_data="ctl_skip"),
        InlineKeyboardButton("⏹", callback_data="ctl_stop"),
    ]]
    if link_row():
        rows.append(link_row())
    return InlineKeyboardMarkup(rows)


def mention(user) -> str:
    return f'<a href="tg://user?id={user.id}">{esc(user.first_name or "User")}</a>'


def get_setting(chat_id: int, key: str) -> bool:
    return settings.get(str(chat_id), {}).get(key, DEFAULTS[key])


def set_setting(chat_id: int, key: str, value: bool):
    settings.setdefault(str(chat_id), {})[key] = value
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except Exception:
        log.exception("settings save error")


async def is_admin(chat_id: int, user_id) -> bool:
    if user_id is None:          # anonymous admin
        return True
    if user_id in OWNER_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def can_control(chat_id: int, user_id) -> bool:
    if not ADMIN_ONLY:
        return True
    return await is_admin(chat_id, user_id)


async def guard(m: Message) -> bool:
    uid = m.from_user.id if m.from_user else None
    if await can_control(m.chat.id, uid):
        return True
    await m.reply_text("🚫 Ye command sirf group admins ke liye hai.")
    return False


# ---------------- YouTube ----------------
def ydl_opts(**extra):
    o = {"format": "bestaudio/best", "noplaylist": True, "quiet": True}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    o.update(extra)
    return o


def search(query: str, by: str, video: bool = False):
    with yt_dlp.YoutubeDL(ydl_opts(default_search="ytsearch1")) as ydl:
        info = ydl.extract_info(query, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else None
    if not info:
        return None
    return {
        "title": info.get("title", "Unknown"),
        "page": info.get("webpage_url") or query,
        "duration": info.get("duration"),
        "live": bool(info.get("is_live")),
        "thumb": info.get("thumbnail"),
        "by": by,
        "video": video,
    }


def resolve(page_url: str, video: bool = False) -> str:
    """Direct link (kuch ghante mein expire hota hai, isliye play se pehle nikalte hain)."""
    extra = {}
    if video:   # audio+video ek hi stream (480p tak)
        extra["format"] = "best[height<=480][vcodec!=none][acodec!=none]/best[height<=480]/best"
    with yt_dlp.YoutubeDL(ydl_opts(**extra)) as ydl:
        info = ydl.extract_info(page_url, download=False)
    return info["url"]


# ---------------- Core player logic ----------------
async def clear_state(chat_id: int):
    queues[chat_id] = []
    current.pop(chat_id, None)
    loops.pop(chat_id, None)
    try:
        await call.leave_call(chat_id)
    except Exception:
        pass


async def play_next(chat_id: int, from_end: bool = False):
    q = queues.setdefault(chat_id, [])
    # Loop on ho aur gaana khatam hua ho to wahi gaana dobara
    if from_end and loops.get(chat_id) and chat_id in current:
        q.insert(0, current[chat_id])
    if not q:
        current.pop(chat_id, None)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        return None
    track = q.pop(0)
    url = await asyncio.to_thread(resolve, track["page"], track.get("video", False))
    if track.get("video"):
        stream = MediaStream(url)
    else:
        stream = MediaStream(url, video_flags=MediaStream.Flags.IGNORE)
    await call.play(chat_id, stream)
    current[chat_id] = track
    return track


async def announce(chat_id: int, track: dict):
    caption = (
        "▶️ <b>Ab baj raha hai</b>\n\n"
        f"🎵 {esc(track['title'])}\n"
        f"⏱ {fmt_dur(track['duration'], track['live'])}\n"
        f"👤 {esc(track['by'])}"
        + ("\n🎬 Video" if track.get("video") else "")
        + ("\n🔁 Loop ON" if loops.get(chat_id) else "")
    )
    try:
        if track.get("thumb"):
            await bot.send_photo(chat_id, track["thumb"], caption=caption,
                                 reply_markup=player_buttons())
            return
    except Exception:
        pass
    await bot.send_message(chat_id, caption, reply_markup=player_buttons())


async def advance(chat_id: int, from_end: bool = False, only_if_idle: bool = False) -> bool:
    """Agla gaana chalata hai. True return karta hai agar koi gaana start hua."""
    track = None
    async with locks[chat_id]:
        if only_if_idle and chat_id in current:
            return False
        fails = 0
        while True:
            try:
                track = await play_next(chat_id, from_end)
                break
            except Exception as e:
                log.exception("play_next error")
                fails += 1
                from_end = False
                if fails >= 3 or not queues.get(chat_id):
                    await clear_state(chat_id)
                    await bot.send_message(
                        chat_id,
                        f"❌ Play nahi ho paya: {esc(str(e)[:150])}\n\n"
                        "Check karo: voice chat START hai aur assistant account group mein hai.",
                    )
                    return False
    if track:
        await announce(chat_id, track)
        return True
    await bot.send_message(chat_id, "✅ Queue khatam. Voice chat chhod diya.",
                           reply_markup=link_buttons())
    return False


@call.on_update(fl.stream_end())
async def on_stream_end(_, update: StreamEnded):
    if update.chat_id not in current:
        return
    try:
        await advance(update.chat_id, from_end=True)
    except Exception:
        log.exception("stream end error")


# ---------------- Commands ----------------
@bot.on_message(filters.command(["start", "help"]))
async def start_cmd(_, m: Message):
    rows = []
    if BOT_USERNAME:
        rows.append([InlineKeyboardButton(
            "➕ Mujhe group mein add karo",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=true")])
    if link_row():
        rows.append(link_row())
    await m.reply_text(
        "🎵 <b>Music Bot</b>\n\n"
        "Group ke voice chat mein live gaana bajata hu.\n\n"
        "<b>Sabke liye:</b>\n"
        "/play &lt;naam ya link&gt; - gaana bajao\n"
        "/queue - queue dekho\n"
        "/vplay &lt;naam&gt; - video ke saath bajao 🎬\n"
        "/search &lt;naam&gt; - list se gaana chuno\n"
        "/lyrics &lt;naam&gt; - lyrics ka link\n"
        "/nowplaying - abhi kya baj raha hai\n"
        "/ping - bot check\n"
        "/ask &lt;sawal&gt; - AI se baat karo (private mein seedha message bhejo)\n"
        "/reset - AI chat memory clear\n\n"
        "<b>Group settings (admins):</b>\n"
        "/autochat on|off - bina tag kiye random chat\n"
        "/welcome on|off - naye members ka welcome\n"
        "/vcnotify on|off - voice chat notifications\n"
        "/editdelete on|off - edit kiya message delete\n\n"
        "<b>Admins ke liye:</b>\n"
        "/skip, /pause, /resume, /stop\n"
        "/loop - current gaana repeat on/off\n"
        "/shuffle - queue mix karo\n"
        "/volume &lt;1-200&gt; - awaaz\n"
        "/remove &lt;number&gt; - queue se hatao\n"
        "/clear - queue khali karo",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


@bot.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    await m.reply_text("🏓 Pong! Bot chalu hai.")


async def do_play(m: Message, query: str, video: bool = False, by: str = ""):
    if not query:
        await m.reply_text("Aise likho: /play kesariya  ya  /vplay kesariya (video)")
        return
    by = by or (m.from_user.first_name if m.from_user else "Admin")
    msg = await m.reply_text("🔎 Dhundh raha hu...")
    try:
        track = await asyncio.to_thread(search, query, by, video)
        if not track:
            await msg.edit_text("Gaana nahi mila.")
            return
        if track["duration"] and track["duration"] > MAX_DURATION:
            await msg.edit_text(
                f"❌ Ye gaana bahut lamba hai (max {fmt_dur(MAX_DURATION)}).")
            return
        chat_id = m.chat.id
        async with locks[chat_id]:
            queues.setdefault(chat_id, []).append(track)
            pos = len(queues[chat_id])
        started = await advance(chat_id, only_if_idle=True)
        if started:
            await msg.delete()
        else:
            await msg.edit_text(
                f"➕ <b>Queue mein add hua (#{pos})</b>\n🎵 {esc(track['title'])}",
                reply_markup=link_buttons(),
            )
    except Exception as e:
        log.exception("play error")
        await msg.edit_text(f"❌ Error: {esc(str(e)[:150])}")


def search_many(query: str):
    with yt_dlp.YoutubeDL(ydl_opts(default_search="ytsearch5", extract_flat=True)) as ydl:
        info = ydl.extract_info(query, download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if e and e.get("id"):
            out.append({"id": e["id"], "title": e.get("title", "?"), "duration": e.get("duration")})
    return out


@bot.on_message(filters.command("play") & filters.group)
async def play_cmd(_, m: Message):
    await do_play(m, " ".join(m.command[1:]).strip(), video=False)


@bot.on_message(filters.command(["vplay", "video"]) & filters.group)
async def vplay_cmd(_, m: Message):
    await do_play(m, " ".join(m.command[1:]).strip(), video=True)


@bot.on_message(filters.command("search") & filters.group)
async def search_cmd(_, m: Message):
    q = " ".join(m.command[1:]).strip()
    if not q:
        await m.reply_text("Aise likho: /search arijit singh")
        return
    msg = await m.reply_text("🔎 Dhundh raha hu...")
    try:
        results = await asyncio.to_thread(search_many, q)
    except Exception as e:
        log.exception("search error")
        await msg.edit_text(f"❌ Error: {esc(str(e)[:150])}")
        return
    if not results:
        await msg.edit_text("Kuch nahi mila.")
        return
    rows = [
        [InlineKeyboardButton(
            f"{i}. {r['title'][:45]} [{fmt_dur(r['duration'])}]",
            callback_data=f"pick_{r['id']}")]
        for i, r in enumerate(results, 1)
    ]
    await msg.edit_text("🔎 <b>Koi gaana chuno:</b>", reply_markup=InlineKeyboardMarkup(rows))


@bot.on_callback_query(filters.regex(r"^pick_[\w-]{11}$"))
async def pick_cb(_, cq: CallbackQuery):
    vid = cq.data.split("_", 1)[1]
    await cq.answer("▶️ Add kar raha hu...")
    await do_play(cq.message, f"https://www.youtube.com/watch?v={vid}", False,
                  by=cq.from_user.first_name)


@bot.on_message(filters.command("lyrics") & filters.group)
async def lyrics_cmd(_, m: Message):
    q = " ".join(m.command[1:]).strip()
    if not q and m.chat.id in current:
        q = current[m.chat.id]["title"]
    if not q:
        await m.reply_text("Aise likho: /lyrics gaane ka naam  (ya gaana bajte waqt sirf /lyrics)")
        return
    genius = "https://genius.com/search?q=" + urllib.parse.quote_plus(q)
    google = "https://www.google.com/search?q=" + urllib.parse.quote_plus(q + " lyrics")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Genius pe lyrics dekho", url=genius)],
        [InlineKeyboardButton("🔎 Google pe dekho", url=google)],
    ])
    await m.reply_text(f"📝 <b>Lyrics:</b> {esc(q)}\n\nNeeche ke button se lyrics dekh lo.",
                       reply_markup=kb)


@bot.on_message(filters.command("volume") & filters.group)
async def volume_cmd(_, m: Message):
    if not await guard(m):
        return
    if len(m.command) < 2 or not m.command[1].isdigit() or not 1 <= int(m.command[1]) <= 200:
        await m.reply_text("Aise likho: /volume 100   (1 se 200 tak)")
        return
    try:
        await call.change_volume_call(m.chat.id, int(m.command[1]))
        await m.reply_text(f"🔊 Volume: {m.command[1]}%")
    except Exception as e:
        await m.reply_text(f"❌ {esc(str(e)[:150])}")


@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, m: Message):
    if not await guard(m):
        return
    if m.chat.id not in current:
        await m.reply_text("Abhi kuch nahi baj raha.")
        return
    await advance(m.chat.id)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, m: Message):
    if not await guard(m):
        return
    try:
        await call.pause(m.chat.id)
        await m.reply_text("⏸ Pause")
    except Exception as e:
        await m.reply_text(f"❌ {esc(str(e)[:150])}")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, m: Message):
    if not await guard(m):
        return
    try:
        await call.resume(m.chat.id)
        await m.reply_text("▶️ Resume")
    except Exception as e:
        await m.reply_text(f"❌ {esc(str(e)[:150])}")


@bot.on_message(filters.command("stop") & filters.group)
async def stop_cmd(_, m: Message):
    if not await guard(m):
        return
    await clear_state(m.chat.id)
    await m.reply_text("⏹ Band kar diya.")


@bot.on_message(filters.command("loop") & filters.group)
async def loop_cmd(_, m: Message):
    if not await guard(m):
        return
    chat_id = m.chat.id
    loops[chat_id] = not loops.get(chat_id, False)
    await m.reply_text("🔁 Loop ON" if loops[chat_id] else "➡️ Loop OFF")


@bot.on_message(filters.command("shuffle") & filters.group)
async def shuffle_cmd(_, m: Message):
    if not await guard(m):
        return
    q = queues.get(m.chat.id, [])
    if len(q) < 2:
        await m.reply_text("Shuffle ke liye queue mein kam se kam 2 gaane chahiye.")
        return
    random.shuffle(q)
    await m.reply_text("🔀 Queue shuffle ho gayi.")


@bot.on_message(filters.command("clear") & filters.group)
async def clear_cmd(_, m: Message):
    if not await guard(m):
        return
    queues[m.chat.id] = []
    await m.reply_text("🧹 Queue khali kar di.")


@bot.on_message(filters.command("remove") & filters.group)
async def remove_cmd(_, m: Message):
    if not await guard(m):
        return
    q = queues.get(m.chat.id, [])
    if len(m.command) < 2 or not m.command[1].isdigit():
        await m.reply_text("Aise likho: /remove 2")
        return
    n = int(m.command[1])
    if not 1 <= n <= len(q):
        await m.reply_text("Ye number queue mein nahi hai.")
        return
    t = q.pop(n - 1)
    await m.reply_text(f"🗑 Hata diya: {esc(t['title'])}")


@bot.on_message(filters.command(["queue", "q"]) & filters.group)
async def queue_cmd(_, m: Message):
    chat_id = m.chat.id
    lines = []
    if chat_id in current:
        t = current[chat_id]
        lines.append(f"▶️ <b>{esc(t['title'])}</b> [{fmt_dur(t['duration'], t['live'])}]")
    for i, t in enumerate(queues.get(chat_id, [])[:15], 1):
        lines.append(f"{i}. {esc(t['title'])} [{fmt_dur(t['duration'], t['live'])}]")
    extra = len(queues.get(chat_id, [])) - 15
    if extra > 0:
        lines.append(f"...aur {extra} gaane")
    await m.reply_text("\n".join(lines) if lines else "Queue khali hai.")


@bot.on_message(filters.command(["nowplaying", "np"]) & filters.group)
async def np_cmd(_, m: Message):
    t = current.get(m.chat.id)
    if not t:
        await m.reply_text("Abhi kuch nahi baj raha.")
        return
    await announce(m.chat.id, t)


# ---------------- AI Chat ----------------
CASUAL_NOTE = (
    " You are casually joining a group conversation on your own, nobody asked you directly. "
    "Reply in one or two short, friendly sentences."
)


async def ask_ai(chat_id: int, user_id: int, text: str, casual: bool = False) -> str:
    hist = chat_history.setdefault((chat_id, user_id), [])
    hist.append({"role": "user", "content": text})
    try:
        res = await ai.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            system=SYSTEM_PROMPT + (CASUAL_NOTE if casual else ""),
            messages=hist,
        )
        reply = "".join(b.text for b in res.content if getattr(b, "type", "") == "text").strip() or "..."
    except Exception:
        hist.pop()
        raise
    hist.append({"role": "assistant", "content": reply})
    while len(hist) > 20:      # last 10 baat-cheet yaad rakho
        del hist[:2]
    return reply


async def handle_ai(m: Message, text: str, casual: bool = False):
    if ai is None:
        if not casual:
            await m.reply_text("💬 Chat AI abhi off hai (API key set nahi hai).")
        return
    if not text:
        await m.reply_text("Kuch likho bhi! Jaise: /ask music kya hota hai?")
        return
    uid = m.from_user.id if m.from_user else 0
    now = time.monotonic()
    if now - last_ask.get(uid, 0) < AI_COOLDOWN:
        if not casual:
            await m.reply_text("⏳ Thoda ruko, phir poocho.")
        return
    last_ask[uid] = now
    try:
        await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        reply = await ask_ai(m.chat.id, uid, text[:2000], casual=casual)
        await m.reply_text(esc(reply[:3500]))
    except Exception:
        log.exception("ai error")
        if not casual:
            await m.reply_text("❌ Abhi jawab nahi de paya, thodi der baad try karo.")


@bot.on_message(filters.command("ask"))
async def ask_cmd(_, m: Message):
    await handle_ai(m, " ".join(m.command[1:]).strip())


@bot.on_message(filters.command("reset"))
async def reset_cmd(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    chat_history.pop((m.chat.id, uid), None)
    await m.reply_text("🧹 AI chat memory clear ho gayi.")


@bot.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
async def private_chat(_, m: Message):
    await handle_ai(m, m.text)


@bot.on_message(filters.group & filters.text & filters.mentioned & ~filters.regex(r"^/"))
async def group_chat(_, m: Message):
    text = m.text.replace(f"@{BOT_USERNAME}", "").strip() if BOT_USERNAME else m.text
    await handle_ai(m, text)


# ---------------- Random chat, Welcome, VC notifications ----------------
@bot.on_message(filters.group & filters.text & ~filters.regex(r"^/"))
async def auto_chat(_, m: Message):
    """Bina tag kiye bhi kabhi-kabhi group ke messages ka jawab deta hai."""
    if ai is None or not m.from_user or m.from_user.is_bot:
        return
    chat_id = m.chat.id
    if not get_setting(chat_id, "autochat") or len(m.text) < 4:
        return
    now = time.monotonic()
    if now - last_auto.get(chat_id, 0) < AUTOCHAT_COOLDOWN:
        return
    if random.random() > AUTOCHAT_CHANCE:
        return
    last_auto[chat_id] = now
    await handle_ai(m, m.text, casual=True)


async def toggle_setting(m: Message, cmd: str, key: str, label: str):
    uid = m.from_user.id if m.from_user else None
    if not await is_admin(m.chat.id, uid):
        await m.reply_text("🚫 Ye sirf group admins ke liye hai.")
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg in ("on", "off"):
        set_setting(m.chat.id, key, arg == "on")
    state = "ON ✅" if get_setting(m.chat.id, key) else "OFF ❌"
    await m.reply_text(f"{label}: <b>{state}</b>\nBadalne ke liye: /{cmd} on ya /{cmd} off")


@bot.on_message(filters.command("autochat") & filters.group)
async def autochat_cmd(_, m: Message):
    await toggle_setting(m, "autochat", "autochat", "🤖 Random chat")


@bot.on_message(filters.command("welcome") & filters.group)
async def welcome_cmd(_, m: Message):
    await toggle_setting(m, "welcome", "welcome", "👋 Welcome message")


@bot.on_message(filters.command("vcnotify") & filters.group)
async def vcnotify_cmd(_, m: Message):
    await toggle_setting(m, "vcnotify", "vcnotify", "🎙 VC notifications")


@bot.on_message(filters.command("editdelete") & filters.group)
async def editdelete_cmd(_, m: Message):
    await toggle_setting(m, "editdelete", "editdelete", "✏️ Edit-delete")


async def _delete_later(msg: Message, delay: int = 10):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


@bot.on_edited_message(filters.group & (filters.text | filters.caption))
async def edit_guard(_, m: Message):
    """Koi message edit kare to edited message delete kar deta hai (bot ko 'Delete messages' admin right chahiye)."""
    if not m.from_user or m.from_user.is_bot or not m.edit_date:
        return
    chat_id = m.chat.id
    if not get_setting(chat_id, "editdelete"):
        return
    if not EDIT_DELETE_ADMINS and await is_admin(chat_id, m.from_user.id):
        return
    try:
        await m.delete()
    except Exception:
        log.exception("edit delete error (bot ko delete permission chahiye)")
        return
    note = await bot.send_message(
        chat_id, f"✏️ {mention(m.from_user)} ne message edit kiya, isliye hata diya gaya.")
    asyncio.create_task(_delete_later(note))


WELCOME_LINES = [
    "👋 Welcome {mention} to <b>{group}</b>! 🎉",
    "🌸 Hey {mention}, <b>{group}</b> mein swagat hai!",
    "🔥 {mention} aa gaye! <b>{group}</b> family mein welcome 🥳",
]


@bot.on_message(filters.group & filters.new_chat_members)
async def welcome_new(_, m: Message):
    if any(u.id == BOT_ID for u in m.new_chat_members):
        await m.reply_text(
            "🎵 <b>Thanks for adding me!</b>\n\n"
            "1. Mujhe group admin banao\n"
            "2. Assistant account ko group mein add karo\n"
            "3. Voice chat start karo aur /play likho\n\n"
            "Saare commands ke liye /help",
            reply_markup=link_buttons(),
        )
        return
    if not get_setting(m.chat.id, "welcome"):
        return
    people = [u for u in m.new_chat_members if not u.is_bot]
    if not people:
        return
    names = ", ".join(mention(u) for u in people)
    group = esc(m.chat.title or "group")
    if WELCOME_TEXT:
        text = WELCOME_TEXT.replace("{mention}", names).replace("{group}", group)
    else:
        text = random.choice(WELCOME_LINES).format(mention=names, group=group)
        text += "\n🎵 Music sunna ho to likho: /play gaane ka naam"
    await m.reply_text(text, reply_markup=link_buttons())


@bot.on_message(filters.group & filters.video_chat_started)
async def vc_started(_, m: Message):
    if not get_setting(m.chat.id, "vcnotify"):
        return
    who = mention(m.from_user) if m.from_user else "Admin"
    await m.reply_text(
        "🎙 <b>Voice chat start ho gayi!</b>\n"
        f"👤 {who} ne start ki.\n\n"
        "Sab join karo 🎧\n🎵 Gaana bajane ke liye: /play gaane ka naam",
        reply_markup=link_buttons(),
    )


@bot.on_message(filters.group & filters.video_chat_ended)
async def vc_ended(_, m: Message):
    await clear_state(m.chat.id)
    if not get_setting(m.chat.id, "vcnotify"):
        return
    dur = getattr(m.video_chat_ended, "duration", None)
    await m.reply_text(
        "🔕 <b>Voice chat khatam ho gayi.</b>"
        + (f"\n⏱ Time: {fmt_dur(dur)}" if dur else "")
    )


@bot.on_message(filters.group & filters.video_chat_members_invited)
async def vc_invited(_, m: Message):
    if not get_setting(m.chat.id, "vcnotify"):
        return
    users = getattr(m.video_chat_members_invited, "users", None) or []
    if not users:
        return
    who = mention(m.from_user) if m.from_user else "Kisi ne"
    names = ", ".join(mention(u) for u in users)
    await m.reply_text(f"📨 {who} ne {names} ko voice chat mein bulaya.")


async def on_vc_join(_, update):
    """Koi member voice chat join kare to group mein batata hai.
    NOTE: ye tabhi kaam karta hai jab assistant khud voice chat mein ho (gaana baj raha ho)."""
    try:
        chat_id = update.chat_id
        if not get_setting(chat_id, "vcnotify"):
            return
        uid = getattr(update.participant, "user_id", None)
        if not uid or uid in (ASSISTANT_ID, BOT_ID):
            return
        now = time.monotonic()
        if now - vc_notified.get((chat_id, uid), 0) < 60:   # duplicate se bachne ke liye
            return
        vc_notified[(chat_id, uid)] = now
        user = await bot.get_users(uid)
        if user.is_bot:
            return
        await bot.send_message(chat_id, f"🎧 {mention(user)} ne voice chat join kiya!")
    except Exception:
        log.exception("vc join notify error")


try:
    from pytgcalls.types import GroupCallParticipant
    call.on_update(fl.call_participant(GroupCallParticipant.Action.JOINED))(on_vc_join)
except Exception as _e:
    log.warning("VC join notification is version mein available nahi: %s", _e)


# ---------------- Player ke buttons ----------------
@bot.on_callback_query(filters.regex(r"^ctl_(pause|resume|skip|stop)$"))
async def ctl_cb(_, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    if not await can_control(chat_id, cq.from_user.id):
        await cq.answer("🚫 Sirf group admins use kar sakte hain.", show_alert=True)
        return
    action = cq.data.split("_", 1)[1]
    try:
        if action == "pause":
            await call.pause(chat_id)
            await cq.answer("⏸ Pause")
        elif action == "resume":
            await call.resume(chat_id)
            await cq.answer("▶️ Resume")
        elif action == "skip":
            if chat_id not in current:
                await cq.answer("Abhi kuch nahi baj raha.", show_alert=True)
                return
            await cq.answer("⏭ Skip")
            await advance(chat_id)
        elif action == "stop":
            await clear_state(chat_id)
            await cq.answer("⏹ Band kar diya")
    except Exception as e:
        await cq.answer(str(e)[:100], show_alert=True)


async def main():
    global BOT_USERNAME, BOT_ID, ASSISTANT_ID
    await bot.start()
    await assistant.start()
    await call.start()
    me = await bot.get_me()
    BOT_USERNAME, BOT_ID = me.username or "", me.id
    ASSISTANT_ID = (await assistant.get_me()).id
    log.info("Music bot ready! @%s | Group mein /play likho.", BOT_USERNAME)
    await idle()
    await bot.stop()
    await assistant.stop()


if __name__ == "__main__":
    loop.run_until_complete(main())
