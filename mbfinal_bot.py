"""
Music Bot 3.0  (song name + artist + language)
-----------------------------------------------
Run bot  : python music_bot.py
Import   : python music_bot.py import songs.csv
CSV cols : title,artist,language      (language and video_id optional)

Radio flow: /radio -> language buttons -> artist buttons (or type a name) -> plays.
"""
import asyncio
import csv
import json
import math
import os
import random
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# ======================================================================
# DATABASE
# ======================================================================
conn = sqlite3.connect(os.environ.get("DB_PATH", "music_bot.db"), check_same_thread=False)
# upgrade an old database that has no "lang" column (must happen before the index below is created)
_cols = [r[1] for r in conn.execute("PRAGMA table_info(songs)")]
if _cols and "lang" not in _cols:
    conn.execute("ALTER TABLE songs ADD COLUMN lang TEXT DEFAULT ''")
    conn.commit()
conn.executescript(
    """
CREATE TABLE IF NOT EXISTS songs(
    key TEXT PRIMARY KEY, title TEXT, artist TEXT DEFAULT '', video_id TEXT,
    duration INTEGER DEFAULT 0, lang TEXT DEFAULT '', popularity INTEGER DEFAULT 0, plays INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_la ON songs(lang, artist);
CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5(title, artist, content='songs', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS songs_ai AFTER INSERT ON songs BEGIN
    INSERT INTO songs_fts(rowid,title,artist) VALUES (new.rowid,new.title,new.artist); END;
CREATE TRIGGER IF NOT EXISTS songs_ad AFTER DELETE ON songs BEGIN
    INSERT INTO songs_fts(songs_fts,rowid,title,artist) VALUES('delete',old.rowid,old.title,old.artist); END;
CREATE TABLE IF NOT EXISTS groups(chat_id INTEGER PRIMARY KEY, title TEXT, active INTEGER, added_ts REAL);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, first_ts REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT,
                                  chat_id INTEGER, user_id INTEGER, detail TEXT);
"""
)


def db(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.fetchall()


def log(kind, chat_id=None, user_id=None, detail=""):
    db(
        "INSERT INTO events(ts,type,chat_id,user_id,detail) VALUES(?,?,?,?,?)",
        (datetime.now().timestamp(), kind, chat_id, user_id, str(detail)),
    )


# typed name -> language shown in the catalog
LANG_ALIASES = {
    "hindi": "Hindi", "bollywood": "Hindi", "english": "English", "punjabi": "Punjabi",
    "haryanvi": "Haryanvi", "haryanavi": "Haryanvi", "bhojpuri": "Bhojpuri",
    "tamil": "Tamil", "telugu": "Telugu",
}


def norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


def import_csv(path):
    rows, langs = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            title = (r.get("title") or "").strip()
            if not title:
                continue
            artist = (r.get("artist") or "").strip()
            raw = (r.get("language") or "").strip()
            lang = LANG_ALIASES.get(raw.lower(), raw.title())
            vid = (r.get("video_id") or "").strip() or None
            key = f"{norm(title)}|{norm(artist)}"
            rows.append((key, title, artist, vid, lang, 50))
            if lang:
                langs.append((lang, key))
    before = db("SELECT COUNT(*) FROM songs")[0][0]
    conn.executemany(
        "INSERT OR IGNORE INTO songs(key,title,artist,video_id,lang,popularity) VALUES(?,?,?,?,?,?)", rows
    )
    conn.executemany("UPDATE songs SET lang=? WHERE key=? AND lang=''", langs)  # fill language of old songs
    conn.commit()
    after = db("SELECT COUNT(*) FROM songs")[0][0]
    print(f"Imported {after - before} new songs (total {after}).")


if len(sys.argv) > 2 and sys.argv[1] == "import":
    import_csv(sys.argv[2])
    sys.exit(0)

# every start: load songs.csv (already known songs are skipped)
if os.path.exists("songs.csv"):
    import_csv("songs.csv")

# ======================================================================
# TELEGRAM CLIENTS
# ======================================================================
import yt_dlp  # noqa: E402
from pyrogram import Client, filters, idle  # noqa: E402
from pyrogram.enums import ChatMemberStatus  # noqa: E402
from pyrogram.errors import MessageNotModified  # noqa: E402
from pyrogram.types import InlineKeyboardButton as Btn  # noqa: E402
from pyrogram.types import InlineKeyboardMarkup as Markup  # noqa: E402
from pytgcalls import PyTgCalls  # noqa: E402
from pytgcalls import filters as fl  # noqa: E402
from pytgcalls.types import MediaStream  # noqa: E402

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION = os.environ["SESSION_STRING"]
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}
BOT_USERNAME = ""

bot = Client("music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
assistant = Client("assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION)
calls = PyTgCalls(assistant)

# ======================================================================
# SEARCH: catalog first (fast), YouTube fallback (learns new songs)
# ======================================================================
YT_EXTRA = {"cookiefile": "cookies.txt"} if os.path.exists("cookies.txt") else {}

BAD_WORDS = (
    "remix", "slowed", "reverb", "lofi", "lo-fi", "reaction", "cover", "karaoke", "status",
    "shorts", "1 hour", "jukebox", "mashup", "live", "8d", "bass boosted", "nightcore",
    "tutorial", "trailer", "scene",
)


def yt_score(query, e):
    title = (e.get("title") or "").lower()
    chan = (e.get("channel") or e.get("uploader") or "").lower()
    q = query.lower()
    s = sum(3 for w in re.findall(r"\w+", q) if w in title)
    for b in BAD_WORDS:
        if b in title and b not in q:
            s -= 6
    if "official" in title:
        s += 2
    if any(k in chan for k in ("topic", "vevo", "t-series", "saregama", "zee music", "sony music", "tips")):
        s += 3
    d = e.get("duration") or 0
    if d < 60 or d > 600:
        s -= 8
    s += min(math.log10((e.get("view_count") or 1) + 1), 9) * 0.8
    return s


def yt_search(query, n=8):
    opts = {"quiet": True, "extract_flat": True, "skip_download": True, **YT_EXTRA}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    entries = [e for e in info.get("entries", []) if e and e.get("id")]
    entries.sort(key=lambda e: yt_score(query, e), reverse=True)
    return [
        {
            "title": e.get("title", "Unknown"),
            "video_id": e["id"],
            "duration": int(e.get("duration") or 0),
            "views": e.get("view_count") or 0,
        }
        for e in entries
    ]


def learn_yt(e, lang=""):
    """Save a song found on YouTube into the catalog so the next search is instant."""
    key = "yt:" + e["video_id"]
    pop = min(99, int(math.log10(e.get("views", 0) + 1) * 11))
    db(
        "INSERT OR IGNORE INTO songs(key,title,artist,video_id,duration,lang,popularity) VALUES(?,?,?,?,?,?,?)",
        (key, e["title"], "", e["video_id"], e["duration"], lang, pop),
    )
    return {"key": key, "title": e["title"], "artist": "", "video_id": e["video_id"], "duration": e["duration"]}


def row_to_track(r):
    return {"key": r[0], "title": r[1], "artist": r[2] or "", "video_id": r[3], "duration": r[4] or 0}


def label(t):
    return f"{t['title']} - {t['artist']}" if t.get("artist") else t["title"]


def catalog_search(query):
    toks = re.findall(r"\w+", query.lower())[:6]
    if not toks:
        return None
    match = " ".join(f'"{t}"*' for t in toks)
    try:
        rows = db(
            "SELECT s.key,s.title,s.artist,s.video_id,s.duration,s.popularity+s.plays*5 "
            "FROM songs_fts f JOIN songs s ON s.rowid=f.rowid "
            "WHERE songs_fts MATCH ? ORDER BY bm25(songs_fts) LIMIT 30",
            (match,),
        )
    except sqlite3.OperationalError:
        return None
    nq, best, best_score = norm(query), None, -1
    for r in rows:
        sc = r[5]
        nt, nl = norm(r[1]), norm(label(row_to_track(r)))
        if nq in (nt, nl):
            sc += 100
        elif nq in nl or nt in nq:
            sc += 30
        if sc > best_score:
            best, best_score = r, sc
    return row_to_track(best) if best else None


def link_info(url):
    opts = {"quiet": True, "noplaylist": True, "skip_download": True, **YT_EXTRA}
    with yt_dlp.YoutubeDL(opts) as y:
        i = y.extract_info(url, download=False)
    t = {
        "key": "link:" + (i.get("id") or url), "title": i.get("title", "Unknown"), "artist": "",
        "video_id": i.get("id") if "youtu" in url else None, "duration": int(i.get("duration") or 0),
    }
    if not t["video_id"]:
        t["page_url"] = i.get("webpage_url", url)
    return t


def spotify_title(url):
    api = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    with urllib.request.urlopen(api, timeout=10) as r:
        return json.loads(r.read())["title"]


def resolve(query):
    if "open.spotify.com" in query:
        query = spotify_title(query)
    elif re.match(r"https?://", query):
        return link_info(query)
    hit = catalog_search(query)
    if hit:
        return hit
    results = yt_search(query)
    return learn_yt(results[0]) if results else None


def find_video(t, avoid=None):
    """Find (or re-find) a YouTube video for a catalog song, then save it."""
    results = yt_search(f"{t['title']} {t.get('artist', '')} official audio", 6)
    for e in results:
        if e["video_id"] != avoid:
            db("UPDATE songs SET video_id=?, duration=? WHERE key=?", (e["video_id"], e["duration"], t["key"]))
            t["duration"] = t.get("duration") or e["duration"]
            return e["video_id"]
    return None


def stream_url(source):
    opts = {"quiet": True, "format": "bestaudio/best", "noplaylist": True, **YT_EXTRA}
    with yt_dlp.YoutubeDL(opts) as y:
        return y.extract_info(source, download=False)["url"]


# ======================================================================
# PLAYER
# ======================================================================
queues = defaultdict(list)
now = {}
radio_state = {}          # chat_id -> {"lang": "Hindi" or "", "artist": "arijit singh" or ""}
radio_seen = defaultdict(set)


def fmt(sec):
    return f"{sec // 60}:{sec % 60:02d}" if sec else "Live"


async def is_admin(chat_id, user_id):
    if user_id in OWNER_IDS:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


async def ensure_assistant(chat_id):
    try:
        await assistant.get_chat_member(chat_id, "me")
        return
    except Exception:
        pass
    link = await bot.export_chat_invite_link(chat_id)
    await assistant.join_chat(link)


def real_lang(lang):
    return lang if lang and lang != "Any" else ""


async def refill_radio(chat_id):
    """Radio = songs of the chosen language and/or artist, best known first."""
    r = radio_state[chat_id]
    lang, art = real_lang(r.get("lang")), (r.get("artist") or "").lower()
    seen = radio_seen[chat_id]
    conds, args = [], []
    if lang:
        conds.append("lang=?")
        args.append(lang)
    if art:
        conds.append("(lower(artist) LIKE ? OR lower(title) LIKE ?)")
        args += [f"%{art}%", f"%{art}%"]
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db(
        f"SELECT key,title,artist,video_id,duration FROM songs {where} ORDER BY popularity+plays*5 DESC LIMIT 300",
        args,
    )
    head = [row_to_track(x) for x in rows if x[0] not in seen][:40]
    random.shuffle(head)                 # the same radio does not always start with the same songs
    picks = head[:15]
    if len(picks) < 15:
        q = f"{art} songs" if art else (f"best {lang} songs" if lang else "best songs")
        found = await asyncio.to_thread(yt_search, q, 30)
        have = {p["key"] for p in picks}
        for e in found:
            if len(picks) >= 15:
                break
            t = learn_yt(e, lang)
            if t["key"] in seen or t["key"] in have:
                continue
            picks.append(t)
    if not picks:
        seen.clear()
        return
    for p in picks:
        seen.add(p["key"])
        p["requester"] = "Radio"
        queues[chat_id].append(p)


async def prefetch(t):
    try:
        if t and not t.get("video_id") and not t.get("page_url"):
            t["video_id"] = await asyncio.to_thread(find_video, t)
    except Exception:
        pass


async def play_next(chat_id):
    failures = 0
    while failures < 5:
        if not queues[chat_id] and chat_id in radio_state:
            try:
                await refill_radio(chat_id)
            except Exception as e:
                log("error", chat_id, None, f"radio refill: {e}")
        if not queues[chat_id]:
            now.pop(chat_id, None)
            try:
                await calls.leave_call(chat_id)
            except Exception:
                pass
            return
        t = queues[chat_id].pop(0)
        try:
            await ensure_assistant(chat_id)
            if not t.get("video_id") and not t.get("page_url"):
                t["video_id"] = await asyncio.to_thread(find_video, t)
            source = t.get("page_url") or f"https://www.youtube.com/watch?v={t['video_id']}"
            try:
                url = await asyncio.to_thread(stream_url, source)
            except Exception:
                if t.get("page_url"):
                    raise
                t["video_id"] = await asyncio.to_thread(find_video, t, t["video_id"])
                url = await asyncio.to_thread(stream_url, f"https://www.youtube.com/watch?v={t['video_id']}")
            await calls.play(chat_id, MediaStream(url, video_flags=MediaStream.Flags.IGNORE))
            now[chat_id] = t
            db("UPDATE songs SET plays=plays+1 WHERE key=?", (t["key"],))
            log("play", chat_id, None, label(t))
            await bot.send_message(
                chat_id,
                f"**Now Playing**\n{label(t)}\nDuration: {fmt(t['duration'])}\nRequested by: {t.get('requester', 'Unknown')}",
            )
            if queues[chat_id]:
                asyncio.create_task(prefetch(queues[chat_id][0]))
            return
        except Exception as e:
            failures += 1
            log("error", chat_id, None, str(e)[:300])
            try:
                await bot.send_message(chat_id, f"Unable to play **{label(t)}**. Skipping to the next track.")
            except Exception:
                pass
    now.pop(chat_id, None)


@calls.on_update(fl.stream_end)
async def on_stream_end(_, update):
    await play_next(update.chat_id)


# ======================================================================
# COMMANDS
# ======================================================================
WELCOME = (
    "**Music Bot**\n\n"
    "Stream high-quality music in your group voice chat.\n\n"
    "**Everyone**\n"
    "/play <song name or link> - Play a song\n"
    "/queue - Show the queue\n\n"
    "**Admins only**\n"
    "/skip - Skip the current track\n"
    "/pause - Pause playback\n"
    "/resume - Resume playback\n"
    "/stop - Stop and clear the queue\n"
    "/radio - Auto radio: choose language, then artist (opens in private chat)\n"
    "/radio <artist> - Quick artist radio, e.g. `/radio arijit singh`"
)

radio_setup = {}  # user_id -> {"chat_id", "title", "lang", "artist", "artists"}


async def reply_or_edit(msg, text, markup=None, edit=False):
    if edit:
        try:
            await msg.edit(text, reply_markup=markup)
        except MessageNotModified:
            pass
    else:
        await msg.reply(text, reply_markup=markup)


@bot.on_message(filters.command("start"))
async def start(_, m):
    if not m.from_user:
        return
    db("INSERT OR IGNORE INTO users VALUES(?,?)", (m.from_user.id, datetime.now().timestamp()))
    if m.chat.type.name == "PRIVATE" and len(m.command) > 1 and m.command[1].startswith("radio_"):
        try:
            chat_id = int(m.command[1][6:])
            chat = await bot.get_chat(chat_id)
        except Exception:
            return await m.reply("Unable to find that group. Please make sure the bot is added to it.")
        if not await is_admin(chat_id, m.from_user.id):
            return await m.reply("Radio can be controlled by group admins only.")
        radio_setup[m.from_user.id] = {"chat_id": chat_id, "title": chat.title}
        return await next_step(m.from_user.id, m, edit=False)
    await m.reply(WELCOME)


@bot.on_message(filters.command(["play", "p"]) & filters.group)
async def play_cmd(_, m):
    query = " ".join(m.command[1:]).strip()
    if not query and m.reply_to_message:
        query = (m.reply_to_message.text or "").strip()
    if not query:
        return await m.reply("Usage: `/play <song name or link>`")
    status = await m.reply("Searching...")
    try:
        track = await asyncio.to_thread(resolve, query)
    except Exception as e:
        log("error", m.chat.id, None, f"search: {e}")
        return await status.edit("Search failed. Please try again.")
    if not track:
        return await status.edit("No results found.")
    track["requester"] = m.from_user.mention if m.from_user else "Anonymous"
    cid = m.chat.id
    if cid in now:
        pos = 0 if cid in radio_state else len(queues[cid])  # user requests jump ahead of radio
        queues[cid].insert(pos, track)
        await status.edit(f"Added to queue: **{label(track)}**")
    else:
        queues[cid].append(track)
        await status.delete()
        await play_next(cid)


async def admin_only(m):
    if not m.from_user or not await is_admin(m.chat.id, m.from_user.id):
        await m.reply("This command is available to group admins only.")
        return False
    return True


@bot.on_message(filters.command("skip") & filters.group)
async def skip(_, m):
    if await admin_only(m):
        if m.chat.id not in now:
            return await m.reply("Nothing is playing.")
        await play_next(m.chat.id)


@bot.on_message(filters.command("pause") & filters.group)
async def pause(_, m):
    if await admin_only(m):
        try:
            await calls.pause(m.chat.id)
            await m.reply("Playback paused.")
        except Exception:
            await m.reply("Nothing is playing.")


@bot.on_message(filters.command("resume") & filters.group)
async def resume(_, m):
    if await admin_only(m):
        try:
            await calls.resume(m.chat.id)
            await m.reply("Playback resumed.")
        except Exception:
            await m.reply("Nothing is paused.")


async def stop_all(chat_id):
    queues[chat_id].clear()
    radio_state.pop(chat_id, None)
    radio_seen.pop(chat_id, None)
    now.pop(chat_id, None)
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
async def stop(_, m):
    if await admin_only(m):
        await stop_all(m.chat.id)
        await m.reply("Playback stopped and queue cleared.")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, m):
    cur, q = now.get(m.chat.id), queues[m.chat.id]
    if not cur and not q:
        return await m.reply("The queue is empty.")
    text = f"**Now Playing**\n{label(cur)}\n" if cur else ""
    if q:
        text += "\n**Up Next**\n" + "\n".join(f"{i}. {label(t)}" for i, t in enumerate(q[:10], 1))
        if len(q) > 10:
            text += f"\n...and {len(q) - 10} more"
    await m.reply(text)


# ======================================================================
# RADIO  (group -> private setup: language -> artist -> group playback)
# ======================================================================
@bot.on_message(filters.command("radio") & filters.group)
async def radio_group(_, m):
    if not await admin_only(m):
        return
    q = " ".join(m.command[1:]).strip()
    cid = m.chat.id
    if q:  # quick artist radio, no setup
        queues[cid].clear()
        radio_state[cid] = {"lang": "", "artist": q}
        radio_seen[cid].clear()
        await m.reply(f"Radio started: **{q}**. Stop it with /stop.")
        return await play_next(cid)
    link = f"https://t.me/{BOT_USERNAME}?start=radio_{cid}"
    await m.reply(
        "Radio setup opens in a private chat with the bot.",
        reply_markup=Markup([[Btn("Open Radio Setup", url=link)]]),
    )


@bot.on_message(filters.command("radio") & filters.private)
async def radio_private(_, m):
    rows = db("SELECT chat_id,title FROM groups WHERE active=1 ORDER BY added_ts DESC LIMIT 40")
    mine = [(c, t) for c, t in rows if await is_admin(c, m.from_user.id)]
    if not mine:
        return await m.reply("No groups found where you are an admin. Send /radio inside your group instead.")
    buttons = [[Btn(t or str(c), callback_data=f"rg:{c}")] for c, t in mine[:20]]
    await m.reply("**Radio**\nSelect a group.", reply_markup=Markup(buttons))


def langs_in_db():
    """Languages that have at least 5 songs in the catalog, biggest first."""
    rows = db("SELECT lang FROM songs WHERE lang!='' GROUP BY lang HAVING COUNT(*)>=5 ORDER BY COUNT(*) DESC")
    return [r[0] for r in rows]


def artists_for(lang):
    if real_lang(lang):
        rows = db(
            "SELECT artist FROM songs WHERE lang=? AND artist!='' GROUP BY artist "
            "ORDER BY COUNT(*) DESC, SUM(popularity+plays*5) DESC LIMIT 10",
            (lang,),
        )
    else:
        rows = db(
            "SELECT artist FROM songs WHERE artist!='' GROUP BY artist "
            "ORDER BY COUNT(*) DESC, SUM(popularity+plays*5) DESC LIMIT 10"
        )
    return [r[0] for r in rows]


def find_artist(text, lang):
    """Best matching artist name for what the person typed (inside the chosen language if any)."""
    like = f"%{text.lower()}%"
    if real_lang(lang):
        rows = db(
            "SELECT artist,lang FROM songs WHERE lang=? AND artist!='' AND lower(artist) LIKE ? "
            "GROUP BY artist ORDER BY COUNT(*) DESC LIMIT 1",
            (lang, like),
        )
    else:
        rows = db(
            "SELECT artist,lang FROM songs WHERE artist!='' AND lower(artist) LIKE ? "
            "GROUP BY artist ORDER BY COUNT(*) DESC LIMIT 1",
            (like,),
        )
    return (rows[0][0], rows[0][1]) if rows else (None, "")


def lang_markup(langs):
    btns = [Btn(x, callback_data=f"rl:{x}") for x in langs] + [Btn("Any Language", callback_data="rl:Any")]
    return Markup([btns[i:i + 2] for i in range(0, len(btns), 2)])


def artist_markup(artists):
    btns = [Btn(a, callback_data=f"ra:{i}") for i, a in enumerate(artists)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([Btn("All Artists", callback_data="ra:all"), Btn("Change Language", callback_data="rb:")])
    return Markup(rows)


async def next_step(uid, msg, edit):
    s = radio_setup.get(uid)
    if not s:
        return await reply_or_edit(msg, "Session expired. Send /radio in your group to begin again.", None, edit)
    head = f"**Radio for {s['title']}**\n"
    if not s.get("lang"):
        langs = langs_in_db()
        if not langs:
            s["lang"] = "Any"          # catalog has no language info: skip this step
        else:
            text = head + "Choose a language, or type it (for example `hindi`).\nYou can also type an artist name directly."
            return await reply_or_edit(msg, text, lang_markup(langs), edit)
    if "artist" not in s:
        artists = artists_for(s["lang"])
        if not artists:
            s["artist"] = ""
        else:
            s["artists"] = artists
            text = head + f"Language: {s['lang']}\nChoose an artist, or type an artist name."
            return await reply_or_edit(msg, text, artist_markup(artists), edit)
    return await launch(uid, msg, edit)


async def launch(uid, msg, edit):
    s = radio_setup.get(uid)
    chat_id = s["chat_id"]
    if not await is_admin(chat_id, uid):
        return await reply_or_edit(msg, "Radio can be controlled by group admins only.", None, edit)
    lang, artist = s["lang"], s.get("artist", "")
    name = " - ".join(x for x in (artist, real_lang(lang)) if x) or "Top songs"
    await reply_or_edit(
        msg,
        f"Starting **{name}** radio in **{s['title']}**.\nSelecting the top 15 tracks...",
        Markup([[Btn("Stop Radio", callback_data="rs:")]]),
        edit,
    )
    queues[chat_id].clear()
    radio_state[chat_id] = {"lang": lang, "artist": artist}
    radio_seen[chat_id].clear()
    try:
        await bot.send_message(chat_id, f"Radio started: **{name}**. Control: group admins.")
    except Exception:
        pass
    for key in ("lang", "artist", "artists"):
        s.pop(key, None)
    await play_next(chat_id)


@bot.on_callback_query(filters.regex(r"^(rg|rl|ra|rb|rs):"))
async def radio_cb(_, q):
    uid = q.from_user.id
    kind, _, value = q.data.partition(":")
    if kind == "rg":
        chat_id = int(value)
        if not await is_admin(chat_id, uid):
            return await q.answer("Admins only.", show_alert=True)
        chat = await bot.get_chat(chat_id)
        radio_setup[uid] = {"chat_id": chat_id, "title": chat.title}
    s = radio_setup.get(uid)
    if not s:
        return await q.answer("Session expired. Send /radio again.", show_alert=True)
    if kind == "rs":
        if not await is_admin(s["chat_id"], uid):
            return await q.answer("Admins only.", show_alert=True)
        await stop_all(s["chat_id"])
        return await q.message.edit("Radio stopped.")
    if kind == "rl":
        s["lang"] = value
        s.pop("artist", None)
    elif kind == "ra":
        if value == "all":
            s["artist"] = ""
        else:
            try:
                s["artist"] = s["artists"][int(value)]
            except (KeyError, ValueError, IndexError):
                return await q.answer("Please choose again.", show_alert=True)
    elif kind == "rb":
        s.pop("lang", None)
        s.pop("artist", None)
    await q.answer()
    await next_step(uid, q.message, edit=True)


@bot.on_message(filters.private & filters.text & ~filters.command(["start", "radio", "admin"]))
async def radio_text(_, m):
    """Typed input during radio setup: a language, an artist, or both (e.g. `punjabi diljit`)."""
    uid = m.from_user.id
    s = radio_setup.get(uid)
    if not s:
        return await m.reply("Send /radio in your group to set up radio, or use /play inside a group.")
    known = {l.lower(): l for l in langs_in_db()}
    rest, lang = [], None
    for w in re.findall(r"\w+", m.text.lower()):
        l = LANG_ALIASES.get(w) or known.get(w)
        if l:
            lang = l
        else:
            rest.append(w)
    if not lang and not rest:
        return await m.reply("I could not understand that. Try a language like `hindi`, or an artist name.")
    if lang:
        s["lang"] = lang
        s.pop("artist", None)
    if rest:
        text = " ".join(rest)
        name, found_lang = find_artist(text, s.get("lang"))
        if name:
            s["artist"] = name
            if not s.get("lang"):
                s["lang"] = found_lang or "Any"
        else:
            await m.reply(f"No artist matching `{text}` found. Please choose from the buttons or try another name.")
    await next_step(uid, m, edit=False)


# ======================================================================
# GROUP TRACKING
# ======================================================================
@bot.on_chat_member_updated()
async def member_updates(_, u):
    old, new = u.old_chat_member, u.new_chat_member
    ref = new or old
    if not ref:
        return
    user = ref.user
    gone = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    was_in = old is not None and old.status not in gone
    is_in = new is not None and new.status not in gone
    chat = u.chat
    if user.is_self:
        if is_in and not was_in:
            db(
                "INSERT INTO groups VALUES(?,?,1,?) ON CONFLICT(chat_id) DO UPDATE SET active=1,title=excluded.title",
                (chat.id, chat.title, datetime.now().timestamp()),
            )
            log("bot_added", chat.id, u.from_user.id if u.from_user else None)
        elif was_in and not is_in:
            db("UPDATE groups SET active=0 WHERE chat_id=?", (chat.id,))
            log("bot_removed", chat.id)
        return
    if is_in and not was_in:
        log("member_join", chat.id, user.id)
    elif was_in and not is_in:
        log("member_leave", chat.id, user.id)


# ======================================================================
# ADMIN PANEL (owner only, private chat)
# ======================================================================
def panel_markup():
    return Markup(
        [
            [Btn("Today", callback_data="adm:today"), Btn("Last 30 Days", callback_data="adm:month")],
            [Btn("All Time", callback_data="adm:all"), Btn("Error Groups", callback_data="adm:err")],
        ]
    )


def stats_text(label_, since):
    def c(t):
        return db("SELECT COUNT(*) FROM events WHERE type=? AND ts>=?", (t, since))[0][0]

    return (
        f"**Admin Panel - {label_}**\n\n"
        f"Active groups (total): {db('SELECT COUNT(*) FROM groups WHERE active=1')[0][0]}\n"
        f"Bot users (total): {db('SELECT COUNT(*) FROM users')[0][0]}\n"
        f"Catalog songs (total): {db('SELECT COUNT(*) FROM songs')[0][0]}\n\n"
        f"Groups added: {c('bot_added')}\n"
        f"Groups removed: {c('bot_removed')}\n"
        f"Members joined: {c('member_join')}\n"
        f"Members left: {c('member_leave')}\n"
        f"Songs played: {c('play')}\n"
        f"Playback errors: {c('error')}"
    )


def error_text():
    since = (datetime.now() - timedelta(hours=24)).timestamp()
    rows = db(
        "SELECT e.chat_id, COALESCE(g.title,'Unknown'), COUNT(*) FROM events e "
        "LEFT JOIN groups g ON g.chat_id=e.chat_id WHERE e.type='error' AND e.ts>=? "
        "GROUP BY e.chat_id ORDER BY 3 DESC LIMIT 15",
        (since,),
    )
    if not rows:
        return "**Error Groups (last 24h)**\n\nNo errors recorded."
    out = "**Error Groups (last 24h)**\n"
    for cid, title, cnt in rows:
        last = db("SELECT detail FROM events WHERE type='error' AND chat_id=? ORDER BY ts DESC LIMIT 1", (cid,))[0][0]
        out += f"\n{title} (`{cid}`)\nErrors: {cnt}\nLast: {last[:80]}\n"
    return out


@bot.on_message(filters.command("admin") & filters.private)
async def admin_cmd(_, m):
    if m.from_user.id not in OWNER_IDS:
        return await m.reply("Access denied.")
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    await m.reply(stats_text("Today", midnight), reply_markup=panel_markup())


@bot.on_callback_query(filters.regex(r"^adm:"))
async def admin_cb(_, q):
    if q.from_user.id not in OWNER_IDS:
        return await q.answer("Access denied.", show_alert=True)
    k, n = q.data.split(":")[1], datetime.now()
    if k == "today":
        text = stats_text("Today", n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    elif k == "month":
        text = stats_text("Last 30 Days", (n - timedelta(days=30)).timestamp())
    elif k == "all":
        text = stats_text("All Time", 0)
    else:
        text = error_text()
    try:
        await q.message.edit(text, reply_markup=panel_markup())
    except MessageNotModified:
        await q.answer("Already up to date.")


# ======================================================================
# RUN
# ======================================================================
def start_health_server():
    """Render web services need an open port; also lets a pinger keep the service awake."""
    port = os.environ.get("PORT")
    if not port:
        return
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


async def main():
    global BOT_USERNAME
    start_health_server()
    await bot.start()
    await assistant.start()
    await calls.start()
    BOT_USERNAME = (await bot.get_me()).username
    print(f"Bot is running as @{BOT_USERNAME}")
    await idle()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
