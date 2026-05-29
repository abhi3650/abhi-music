#!/usr/bin/env python3
"""
🎵 MusicVault — Telegram Music Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ DB_CHANNEL is the source of truth — works across servers
✅ On startup: scans channel & rebuilds local index automatically
✅ Thumbnails extracted from audio & served over HTTP
✅ Spotify-grade web player UI
✅ Self-installs packages (skips if already present)
✅ Real audio streaming with Range support (seek works)
✅ Upload, Browse, Search, Random, Favs, Playlists, Stats
✅ Auto command registration + /restart hot-redeploy
"""

# ══════════════════════════════════════════════════════════
# BLOCK 1 — SELF-INSTALL
# ══════════════════════════════════════════════════════════
import sys, subprocess, os
from importlib.metadata import version as _V, PackageNotFoundError as _NF

REQUIRED = {
    "python-telegram-bot==20.7": ("python-telegram-bot", "20.7"),
    "aiohttp":    ("aiohttp",    None),
    "python-dotenv": ("python-dotenv", None),
    "mutagen":    ("mutagen",    None),
    "Pillow":     ("Pillow",     None),   # thumbnail extraction
}

def _ok(d, p):
    try:    return not p or _V(d) == p
    except _NF: return False

def _bootstrap():
    miss = [s for s,(d,v) in REQUIRED.items() if not _ok(d,v)]
    if not miss: print("📦 All packages present — skipping.\n"); return
    print("📦 Installing missing packages…")
    for s in miss:
        print(f"  ⬇️  {s}")
        try:
            subprocess.check_call([sys.executable,"-m","pip","install",s,"--quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  ✅ {s}")
        except subprocess.CalledProcessError:
            print(f"  ❌ {s}  →  pip install {s}"); sys.exit(1)
    print("✅ Ready!\n")

_bootstrap()

# ══════════════════════════════════════════════════════════
# BLOCK 2 — IMPORTS
# ══════════════════════════════════════════════════════════
import json, logging, asyncio, time, datetime, platform
import threading, http.server, socketserver, urllib.parse
import mimetypes, random, io, base64
from pathlib import Path
from dotenv  import load_dotenv

from telegram import (
    Update, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

try:
    from mutagen.mp3  import MP3
    from mutagen.id3  import ID3, APIC
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4  import MP4
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

# ══════════════════════════════════════════════════════════
# BLOCK 3 — CONFIG
# ══════════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN",  "")
DB_CHANNEL = os.getenv("DB_CHANNEL", "")    # REQUIRED  e.g. -1001234567890
ADMIN_IDS  = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()]
HTTP_PORT  = int(os.getenv("HTTP_PORT", "8080"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

BOT_DIR      = Path(__file__).parent.resolve()
DB_FILE      = BOT_DIR / "musicvault.json"
CACHE_DIR    = BOT_DIR / "cache"
THUMB_DIR    = BOT_DIR / "thumbs"
RESTART_FLAG = BOT_DIR / ".restart_chat"

CACHE_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Conversation states
UPL_FILE, UPL_TITLE, UPL_ARTIST, UPL_GENRE = range(4)
SEARCH_Q = 4

APP: Application = None   # type: ignore
BOT_LOOP: asyncio.AbstractEventLoop | None = None  # set in main(), used by HTTP thread

# ══════════════════════════════════════════════════════════
# BLOCK 4 — DATABASE
# ══════════════════════════════════════════════════════════
# Architecture:
#
#   DB_CHANNEL (Telegram channel) is the ONLY source of truth.
#   Two types of messages live there:
#
#   1. AUDIO messages  — one per track, caption = JSON metadata
#      { tid, title, artist, album, genre, duration,
#        uploaded_by, uploaded_at, thumb_file_id }
#
#   2. INDEX message   — ONE pinned text message, caption starts with
#      "MUSICVAULT_INDEX\n" followed by JSON:
#      { tracks: { tid: {message_id, plays, ...all metadata} },
#        playlists: {...}, favourites: {...} }
#
#   On startup  → fetch pinned INDEX message → populate local DB.
#   On every change (upload/delete/play/fav/playlist) → push updated
#                   index back to channel (edit the pinned message).
#
#   local musicvault.json = write-through cache only (faster reads).
#   New server = zero local files needed, just BOT_TOKEN + DB_CHANNEL.
#
#   Index message size limit: Telegram captions max 4096 chars.
#   We use message TEXT (not caption) → 4096 chars for text messages.
#   For large libraries (>4096 chars) we split into chunks and store
#   a pointer message that lists all chunk message IDs.
# ══════════════════════════════════════════════════════════

INDEX_MARKER  = "MUSICVAULT_INDEX_V2"
POINTER_MARKER = "MUSICVAULT_POINTER"
_INDEX_MSG_ID: int | None = None   # cached message_id of the pinned index

def load_db() -> dict:
    """Load from local cache (fast path). Falls back to empty."""
    if DB_FILE.exists():
        try: return json.loads(DB_FILE.read_text("utf-8"))
        except: pass
    return {"tracks":{}, "playlists":{}, "favourites":{}, "_index_msg_id": None}

def save_db(db: dict):
    """Write-through to local cache."""
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2), "utf-8")
    if DB_FILE.exists(): DB_FILE.unlink()
    tmp.rename(DB_FILE)

def _db_to_index_json(db: dict) -> str:
    """Serialise the DB to the string stored in the channel index message."""
    payload = {
        "tracks":     db.get("tracks",{}),
        "playlists":  db.get("playlists",{}),
        "favourites": db.get("favourites",{}),
    }
    return INDEX_MARKER + "\n" + json.dumps(payload, separators=(",",":"))

def _index_json_to_db(text: str) -> dict | None:
    """Parse the channel index message back into a DB dict."""
    try:
        if not text.startswith(INDEX_MARKER): return None
        payload = json.loads(text[len(INDEX_MARKER)+1:])
        return {
            "tracks":     payload.get("tracks",{}),
            "playlists":  payload.get("playlists",{}),
            "favourites": payload.get("favourites",{}),
        }
    except Exception as e:
        logger.warning(f"_index_json_to_db: {e}")
        return None

async def push_index(bot, db: dict):
    """
    Write the full DB into the channel index message.
    If no index message exists yet, create one and pin it.
    Handles Telegram's 4096-char text limit by chunking.
    """
    global _INDEX_MSG_ID
    if not DB_CHANNEL: return

    text = _db_to_index_json(db)
    cid  = int(DB_CHANNEL)

    # Split into ≤4000-char chunks
    CHUNK = 4000
    chunks = [text[i:i+CHUNK] for i in range(0, len(text), CHUNK)]

    try:
        if len(chunks) == 1:
            # Simple case: everything fits in one message
            if _INDEX_MSG_ID:
                try:
                    await bot.edit_message_text(
                        chat_id=cid, message_id=_INDEX_MSG_ID, text=chunks[0])
                    save_db(db); return
                except TelegramError as e:
                    if "message is not modified" in str(e).lower():
                        save_db(db); return
                    logger.warning(f"push_index edit failed: {e} — recreating")
                    _INDEX_MSG_ID = None

            # Create fresh index message
            msg = await bot.send_message(cid, chunks[0], disable_notification=True)
            _INDEX_MSG_ID = msg.message_id
            db["_index_msg_id"] = _INDEX_MSG_ID
            try:
                await bot.pin_chat_message(cid, msg.message_id, disable_notification=True)
            except: pass

        else:
            # Large library: send N chunk messages, then a pointer message
            # Delete old chunks first if we have a pointer
            if _INDEX_MSG_ID:
                try:
                    old = await bot.forward_message(cid, cid, _INDEX_MSG_ID)
                    await bot.delete_message(cid, old.message_id)
                except: pass

            chunk_ids = []
            for c in chunks:
                m = await bot.send_message(cid, c, disable_notification=True)
                chunk_ids.append(m.message_id)

            pointer = POINTER_MARKER + "\n" + json.dumps(chunk_ids)
            if _INDEX_MSG_ID:
                try:
                    await bot.edit_message_text(cid, _INDEX_MSG_ID, pointer)
                except:
                    msg = await bot.send_message(cid, pointer, disable_notification=True)
                    _INDEX_MSG_ID = msg.message_id
                    try: await bot.pin_chat_message(cid, _INDEX_MSG_ID, disable_notification=True)
                    except: pass
            else:
                msg = await bot.send_message(cid, pointer, disable_notification=True)
                _INDEX_MSG_ID = msg.message_id
                try: await bot.pin_chat_message(cid, _INDEX_MSG_ID, disable_notification=True)
                except: pass

        db["_index_msg_id"] = _INDEX_MSG_ID
        save_db(db)

    except Exception as e:
        logger.error(f"push_index: {e}")
        save_db(db)   # at least save locally

async def pull_index(bot) -> dict | None:
    """
    Fetch the DB from the channel's pinned index message.
    This is the bootstrap called on every startup — works on a
    brand-new server with zero local files.
    Returns the DB dict or None if channel has no index yet.
    """
    global _INDEX_MSG_ID
    if not DB_CHANNEL: return None
    cid = int(DB_CHANNEL)

    # Strategy 1: use locally cached _index_msg_id
    local = load_db()
    cached_id = local.get("_index_msg_id")
    if cached_id:
        result = await _fetch_index_by_id(bot, cid, cached_id)
        if result:
            _INDEX_MSG_ID = cached_id
            return result

    # Strategy 2: read the pinned message
    try:
        chat = await bot.get_chat(cid)
        pinned = chat.pinned_message
        if pinned:
            result = _parse_index_message(pinned)
            if result is not None:
                _INDEX_MSG_ID = pinned.message_id
                return result
    except Exception as e:
        logger.warning(f"pull_index get_chat: {e}")

    # No index found — fresh install
    logger.info("pull_index: no existing index found (fresh install)")
    return None

async def _fetch_index_by_id(bot, cid: int, msg_id: int) -> dict | None:
    """Fetch a specific message from the channel by forwarding it to itself."""
    try:
        fwd = await bot.forward_message(cid, cid, msg_id)
        text = fwd.text or fwd.caption or ""
        await bot.delete_message(cid, fwd.message_id)
        return _parse_index_text(text)
    except Exception as e:
        logger.warning(f"_fetch_index_by_id {msg_id}: {e}")
        return None

def _parse_index_message(msg) -> dict | None:
    text = msg.text or msg.caption or ""
    return _parse_index_text(text)

def _parse_index_text(text: str) -> dict | None:
    if text.startswith(INDEX_MARKER):
        return _index_json_to_db(text)
    if text.startswith(POINTER_MARKER):
        # Multi-chunk — pull_chunked_index handles this async; signal caller to resync
        logger.info("Pointer index detected — run /resync to reassemble chunks")
        return None
    return None

async def pull_chunked_index(bot, pointer_msg_id: int) -> dict | None:
    """Reassemble a multi-chunk index from the channel."""
    cid = int(DB_CHANNEL)
    try:
        fwd = await bot.forward_message(cid, cid, pointer_msg_id)
        text = fwd.text or ""
        await bot.delete_message(cid, fwd.message_id)
        if not text.startswith(POINTER_MARKER): return None
        chunk_ids = json.loads(text[len(POINTER_MARKER)+1:])
        full = ""
        for cid2 in chunk_ids:
            fwd2 = await bot.forward_message(cid, cid, cid2)
            full += (fwd2.text or "")
            await bot.delete_message(cid, fwd2.message_id)
        return _index_json_to_db(full)
    except Exception as e:
        logger.error(f"pull_chunked_index: {e}")
        return None

async def post_audio_to_channel(bot, track: dict, file_id: str) -> int | None:
    """Post the audio file to DB_CHANNEL. Returns message_id."""
    if not DB_CHANNEL: return None
    caption = json.dumps({
        "tid":          track.get("tid",""),
        "title":        track.get("title",""),
        "artist":       track.get("artist",""),
        "album":        track.get("album",""),
        "genre":        track.get("genre",""),
        "duration":     track.get("duration",0),
        "uploaded_by":  track.get("uploaded_by",0),
        "uploaded_at":  track.get("uploaded_at",0),
        "thumb_file_id":track.get("thumb_file_id",""),
    })
    try:
        msg = await bot.send_audio(
            chat_id=int(DB_CHANNEL), audio=file_id,
            caption=caption, title=track.get("title",""),
            performer=track.get("artist",""),
        )
        return msg.message_id
    except TelegramError as e:
        logger.error(f"post_audio_to_channel: {e}"); return None

# ══════════════════════════════════════════════════════════
# BLOCK 5 — AUDIO METADATA + THUMBNAIL EXTRACTION
# ══════════════════════════════════════════════════════════

def extract_meta(path: str) -> dict:
    m = {"title":"","artist":"","album":"","genre":"","duration":0,"thumb_bytes":None}
    if not MUTAGEN: return m
    try:
        pl = path.lower()
        if pl.endswith(".mp3"):
            a = MP3(path); m["duration"] = int(a.info.length)
            try:
                tags = ID3(path)
                m["title"]  = str(tags.get("TIT2",""))
                m["artist"] = str(tags.get("TPE1",""))
                m["album"]  = str(tags.get("TALB",""))
                m["genre"]  = str(tags.get("TCON",""))
                for tag in tags.values():
                    if isinstance(tag, APIC):
                        m["thumb_bytes"] = tag.data; break
            except: pass
        elif pl.endswith(".flac"):
            a = FLAC(path); m["duration"] = int(a.info.length)
            m["title"]  = (a.get("title",  [""])[0])
            m["artist"] = (a.get("artist", [""])[0])
            m["album"]  = (a.get("album",  [""])[0])
            m["genre"]  = (a.get("genre",  [""])[0])
            if a.pictures: m["thumb_bytes"] = a.pictures[0].data
        elif pl.endswith((".m4a",".mp4",".aac")):
            a = MP4(path); m["duration"] = int(a.info.length)
            m["title"]  = (a.get("\xa9nam",[""])[0])
            m["artist"] = (a.get("\xa9ART",[""])[0])
            m["album"]  = (a.get("\xa9alb",[""])[0])
            m["genre"]  = (a.get("\xa9gen",[""])[0])
            if "covr" in a: m["thumb_bytes"] = bytes(a["covr"][0])
    except Exception as e:
        logger.warning(f"mutagen: {e}")
    return m

def save_thumb(tid: str, raw: bytes) -> bool:
    """Resize & save thumbnail as JPEG. Returns True on success."""
    if not raw or not PIL_OK: return False
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((300, 300), Image.LANCZOS)
        img.save(THUMB_DIR / f"{tid}.jpg", "JPEG", quality=85)
        return True
    except Exception as e:
        logger.warning(f"save_thumb: {e}"); return False

def get_thumb_b64(tid: str) -> str:
    """Return base64-encoded JPEG thumbnail or empty string."""
    p = THUMB_DIR / f"{tid}.jpg"
    if p.exists():
        return base64.b64encode(p.read_bytes()).decode()
    return ""

async def download_tg_thumb(bot, thumb_file_id: str, tid: str) -> bool:
    """Download thumbnail from Telegram and save locally."""
    if not thumb_file_id: return False
    try:
        dest = THUMB_DIR / f"{tid}.jpg"
        if dest.exists(): return True
        f = await bot.get_file(thumb_file_id)
        await f.download_to_drive(str(dest))
        return True
    except Exception as e:
        logger.warning(f"download_tg_thumb: {e}"); return False

# ══════════════════════════════════════════════════════════
# BLOCK 6 — HELPERS
# ══════════════════════════════════════════════════════════

def is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS

def fmt_dur(s: int) -> str:
    if not s: return "0:00"
    m, sec = divmod(int(s), 60)
    h, m   = divmod(m, 60)
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"

def track_card(t: dict, tid: str, pos: int = 0) -> str:
    pre = f"{pos}. " if pos else ""
    return (f"{pre}🎵 *{t.get('title') or 'Unknown'}*\n"
            f"   👤 {t.get('artist') or '—'}   💿 {t.get('album') or '—'}\n"
            f"   🎸 {t.get('genre') or '—'}   ⏱ {fmt_dur(t.get('duration',0))}\n"
            f"   ▶️ {t.get('plays',0)} plays  |  `{tid}`")

def paginate(items, page, size=5):
    pages = max(1,(len(items)+size-1)//size)
    page  = max(0,min(page,pages-1))
    return items[page*size:(page+1)*size], page, pages

def nav_row(prefix, page, pages):
    row = []
    if page > 0:       row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{page-1}"))
    row.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages-1: row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{page+1}"))
    return row

def player_url() -> str:
    return (PUBLIC_URL or f"http://localhost:{HTTP_PORT}") + "/"

# ══════════════════════════════════════════════════════════
# BLOCK 7 — SPOTIFY-GRADE WEB PLAYER HTML
# ══════════════════════════════════════════════════════════

def build_html(tracks_json: str) -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>MusicVault</title>
<style>
/* ── Reset & variables ── */
:root{
  --green:#1DB954;--green2:#1ed760;
  --bg:#0d0d0d;--bg2:#121212;--bg3:#181818;--bg4:#242424;
  --border:#2a2a2a;--muted:#6a6a6a;--subtle:#b3b3b3;
  --white:#fff;--radius:8px;--np-h:92px;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--white);
  font-family:-apple-system,BlinkMacSystemFont,'Circular','Helvetica Neue',
  Helvetica,Arial,sans-serif;font-size:14px}

/* ── Layout ── */
#app{display:grid;grid-template-rows:auto auto auto 1fr auto;height:100vh}

/* ── Top bar ── */
.topbar{background:linear-gradient(180deg,#2a2a2a 0%,var(--bg2) 100%);
  padding:16px 16px 10px;display:flex;align-items:center;gap:12px}
.logo{font-size:22px;font-weight:900;letter-spacing:-1px;color:var(--green);
  white-space:nowrap;display:flex;align-items:center;gap:6px}
.logo svg{flex-shrink:0}
.search-box{flex:1;display:flex;align-items:center;background:var(--bg4);
  border-radius:24px;padding:8px 14px;gap:8px;min-width:0;
  border:1.5px solid transparent;transition:.2s}
.search-box:focus-within{border-color:var(--white)}
.search-box input{flex:1;background:none;border:none;outline:none;
  color:var(--white);font-size:13px;min-width:0}
.search-box input::placeholder{color:var(--muted)}
.search-box .x{color:var(--muted);cursor:pointer;font-size:13px;display:none;
  padding:0 2px;line-height:1}

/* ── Tabs ── */
.tabs{display:flex;background:var(--bg2);padding:0 8px;
  border-bottom:1px solid var(--border);gap:4px;overflow-x:auto;
  scrollbar-width:none;flex-shrink:0}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:11px 14px;font-size:12px;font-weight:700;color:var(--muted);
  cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;
  transition:.2s;letter-spacing:.3px}
.tab.on{color:var(--white);border-color:var(--green)}

/* ── Genre chips ── */
.chips{display:none;gap:8px;overflow-x:auto;padding:10px 12px;
  background:var(--bg2);border-bottom:1px solid var(--border);
  scrollbar-width:none;flex-shrink:0}
.chips::-webkit-scrollbar{display:none}
.chip{background:var(--bg4);border-radius:20px;padding:5px 14px;
  font-size:11px;font-weight:700;white-space:nowrap;cursor:pointer;
  border:1px solid var(--border);transition:.15s;color:var(--subtle)}
.chip.on{background:var(--white);color:#000;border-color:var(--white)}

/* ── Track list ── */
.list{overflow-y:auto;padding:8px 0;background:var(--bg2)}
.list::-webkit-scrollbar{width:4px}
.list::-webkit-scrollbar-track{background:transparent}
.list::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:2px}

/* ── Track row ── */
.row{display:flex;align-items:center;gap:12px;padding:8px 16px;
  cursor:pointer;transition:background .12s;user-select:none}
.row:hover{background:var(--bg4)}
.row.now{background:rgba(29,185,84,.08)}
.row:active{background:var(--bg3)}
.row-num{width:18px;text-align:center;font-size:11px;color:var(--muted);flex-shrink:0}
.row-num.eq{color:var(--green);animation:pulse 1.2s ease infinite alternate}
@keyframes pulse{from{opacity:.5}to{opacity:1}}
.row-art{position:relative;width:46px;height:46px;border-radius:var(--radius);
  background:var(--bg3);flex-shrink:0;overflow:hidden}
.row-art img{width:100%;height:100%;object-fit:cover;border-radius:var(--radius)}
.row-art .no-art{width:100%;height:100%;display:flex;align-items:center;
  justify-content:center;font-size:22px}
.row-info{flex:1;min-width:0}
.row-title{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;margin-bottom:3px}
.row-title.now{color:var(--green)}
.row-sub{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.row-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.row-dur{font-size:11px;color:var(--muted)}
.hrt{background:none;border:none;cursor:pointer;font-size:16px;
  padding:4px;line-height:1;opacity:.7;transition:.15s}
.hrt:hover{opacity:1;transform:scale(1.15)}

.empty{display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:220px;gap:10px;color:var(--muted)}
.empty-ico{font-size:52px;opacity:.4}
.empty p{font-size:13px}

/* ── Now Playing ── */
.np{background:linear-gradient(180deg,#1a1a1a 0%,var(--bg3) 100%);
  border-top:1px solid var(--border);padding:10px 16px 12px;flex-shrink:0;
  display:none}
.np.show{display:block}
.np-main{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.np-cover{width:48px;height:48px;border-radius:var(--radius);
  background:var(--bg4);flex-shrink:0;overflow:hidden;position:relative}
.np-cover img{width:100%;height:100%;object-fit:cover}
.np-cover .no-art{width:100%;height:100%;display:flex;align-items:center;
  justify-content:center;font-size:22px}
.np-cover .spin{
  animation:spin 8s linear infinite;
  animation-play-state:paused}
.np-cover.playing .spin{animation-play-state:running}
@keyframes spin{to{transform:rotate(360deg)}}
.np-txt{flex:1;min-width:0}
.np-title{font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;margin-bottom:2px}
.np-artist{font-size:11px;color:var(--muted)}
.np-hrt{background:none;border:none;cursor:pointer;font-size:20px;padding:4px}

/* ── Progress ── */
.prog-row{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.prog-time{font-size:10px;color:var(--muted);min-width:30px}
.prog-time.r{text-align:right}
.prog-bar{flex:1;height:4px;background:var(--bg4);border-radius:2px;
  cursor:pointer;position:relative;overflow:hidden}
.prog-fill{height:100%;background:var(--green);border-radius:2px;
  pointer-events:none;transition:width .25s linear}
.prog-bar:hover .prog-fill{background:var(--green2)}

/* ── Controls ── */
.ctrl{display:flex;align-items:center;justify-content:space-between;padding:0 4px}
.ctrl-btn{background:none;border:none;cursor:pointer;color:var(--subtle);
  font-size:18px;padding:6px;transition:.15s;line-height:1}
.ctrl-btn:hover{color:var(--white);transform:scale(1.08)}
.ctrl-btn.active{color:var(--green)}
.ctrl-btn.play{font-size:36px;color:var(--white);padding:0}
.ctrl-btn.play:hover{color:var(--green2);transform:scale(1.05)}
.vol-row{display:flex;align-items:center;gap:8px;margin-top:6px}
.vol-icon{font-size:13px;color:var(--muted)}
input[type=range]{flex:1;accent-color:var(--green);height:3px}
</style>
</head>
<body>
<div id="app">

<!-- Top bar -->
<div class="topbar">
  <div class="logo">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="var(--green)">
      <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
    </svg>
    MusicVault
  </div>
  <div class="search-box">
    <svg width="14" height="14" fill="var(--muted)" viewBox="0 0 24 24">
      <path d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z" stroke="var(--muted)" stroke-width="2" fill="none" stroke-linecap="round"/>
    </svg>
    <input id="q" placeholder="Search songs, artists…" oninput="onSearch()"/>
    <span class="x" id="qx" onclick="clearSearch()">✕</span>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab on"  onclick="setTab('all')">All songs</div>
  <div class="tab"     onclick="setTab('genre')">Genres</div>
  <div class="tab"     onclick="setTab('favs')">❤ Liked</div>
  <div class="tab"     onclick="setTab('recent')">Recently played</div>
</div>

<!-- Genre chips -->
<div class="chips" id="chips"></div>

<!-- Track list -->
<div class="list" id="list"></div>

<!-- Now playing -->
<div class="np" id="np">
  <div class="np-main">
    <div class="np-cover" id="npCover">
      <div class="no-art spin" id="npArtEl">🎵</div>
    </div>
    <div class="np-txt">
      <div class="np-title" id="npTitle">—</div>
      <div class="np-artist" id="npArtist">—</div>
    </div>
    <button class="np-hrt" id="npHrt" onclick="hrtNP()">🤍</button>
  </div>
  <div class="prog-row">
    <span class="prog-time" id="tCur">0:00</span>
    <div class="prog-bar" id="pbar" onclick="seek(event)">
      <div class="prog-fill" id="pfill" style="width:0%"></div>
    </div>
    <span class="prog-time r" id="tTot">0:00</span>
  </div>
  <div class="ctrl">
    <button class="ctrl-btn" id="shufBtn" onclick="togShuf()" title="Shuffle">⇄</button>
    <button class="ctrl-btn" onclick="prevTrack()" title="Previous">⏮</button>
    <button class="ctrl-btn play" id="playBtn" onclick="togPlay()">▶</button>
    <button class="ctrl-btn" onclick="nextTrack()" title="Next">⏭</button>
    <button class="ctrl-btn" id="repBtn" onclick="togRep()" title="Repeat">↺</button>
  </div>
  <div class="vol-row">
    <span class="vol-icon">🔈</span>
    <input type="range" id="vol" min="0" max="1" step="0.02" value="1" oninput="setVol()"/>
    <span class="vol-icon">🔊</span>
  </div>
</div>

</div><!-- #app -->
<audio id="aud" preload="auto"></audio>

<script>
/* ── Data injected by server ── */
const DATA = __TRACKS_JSON__;
const IDs  = Object.keys(DATA);

/* ── State ── */
let tab      = 'all';
let genre    = null;
let qStr     = '';
let queue    = [...IDs];
let qi       = 0;
let shuffle  = false;
let repeat   = false;
let recently = JSON.parse(localStorage.getItem('mv_recent') || '[]');
let favs     = JSON.parse(localStorage.getItem('mv_favs')   || '[]');

const aud = document.getElementById('aud');
aud.volume = parseFloat(localStorage.getItem('mv_vol') || '1');
document.getElementById('vol').value = aud.volume;

/* ── Utils ── */
const fmt = s => {
  if(!s && s!==0) return '0:00';
  s = Math.floor(s);
  const m = Math.floor(s/60), sec = s % 60;
  return `${m}:${sec.toString().padStart(2,'0')}`;
};
const isFav   = id => favs.includes(id);
const saveFavs = () => localStorage.setItem('mv_favs', JSON.stringify(favs));
const saveRecent = () => localStorage.setItem('mv_recent', JSON.stringify(recently.slice(0,50)));

/* ── Search ── */
function onSearch(){
  qStr = document.getElementById('q').value.toLowerCase();
  document.getElementById('qx').style.display = qStr ? 'block' : 'none';
  render();
}
function clearSearch(){
  document.getElementById('q').value = '';
  document.getElementById('qx').style.display = 'none';
  qStr = ''; render();
}

/* ── Tab ── */
function setTab(t){
  tab = t; genre = null;
  const names = ['all','genre','favs','recent'];
  document.querySelectorAll('.tab').forEach((el,i) => el.classList.toggle('on', names[i]===t));
  const chips = document.getElementById('chips');
  chips.style.display = t==='genre' ? 'flex' : 'none';
  if(t==='genre') buildChips();
  render();
}

/* ── Genres ── */
function buildChips(){
  const gs = [...new Set(IDs.map(id=>(DATA[id].genre||'').trim().toLowerCase()).filter(Boolean))].sort();
  document.getElementById('chips').innerHTML =
    `<div class="chip ${!genre?'on':''}" onclick="setGenre(null)">All genres</div>` +
    gs.map(g=>`<div class="chip ${genre===g?'on':''}" onclick="setGenre('${g}')">${g.charAt(0).toUpperCase()+g.slice(1)}</div>`).join('');
}
function setGenre(g){ genre=g; buildChips(); render(); }

/* ── Filter ── */
function getIds(){
  let ids = [...IDs];
  if(tab==='favs')   ids = ids.filter(id=>isFav(id));
  if(tab==='recent') ids = recently.filter(id=>DATA[id]).slice(0,30);
  if(tab==='genre' && genre) ids = ids.filter(id=>(DATA[id].genre||'').toLowerCase()===genre);
  if(qStr) ids = ids.filter(id=>{
    const t = DATA[id];
    return (t.title+t.artist+t.album+t.genre).toLowerCase().includes(qStr);
  });
  return ids;
}

/* ── Thumbnail ── */
function thumbEl(id, cls=''){
  const t = DATA[id];
  if(t && t.thumb_b64){
    return `<img src="data:image/jpeg;base64,${t.thumb_b64}" alt="" loading="lazy"/>`;
  }
  if(t && t.thumb_url){
    return `<img src="${t.thumb_url}" alt="" loading="lazy"/>`;
  }
  return `<div class="no-art">🎵</div>`;
}

/* ── Render ── */
function render(){
  const ids = getIds();
  const el  = document.getElementById('list');
  if(!ids.length){
    el.innerHTML=`<div class="empty">
      <div class="empty-ico">🎵</div>
      <p>${tab==='favs'?'No liked songs yet':'No tracks found'}</p>
    </div>`; return;
  }
  const curId = queue[qi];
  el.innerHTML = ids.map((id,i)=>{
    const t   = DATA[id];
    const now = id===curId && !aud.paused;
    return `<div class="row${now?' now':''}" onclick="playId('${id}')">
      <div class="row-num${now?' eq':''}">${now ? '♫' : i+1}</div>
      <div class="row-art">${thumbEl(id)}</div>
      <div class="row-info">
        <div class="row-title${now?' now':''}">${esc(t.title||'Unknown')}</div>
        <div class="row-sub">${esc(t.artist||'Unknown artist')} · ${esc(t.album||'')}</div>
      </div>
      <div class="row-right">
        <span class="row-dur">${fmt(t.duration)}</span>
        <button class="hrt" onclick="event.stopPropagation();hrt('${id}')">${isFav(id)?'❤':'🤍'}</button>
      </div>
    </div>`;
  }).join('');
}

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

/* ── Playback ── */
function playId(id){
  const ids = getIds();
  const idx = ids.indexOf(id);
  queue = ids.length ? ids : [...IDs];
  qi    = idx>=0 ? idx : 0;
  load(queue[qi]);
}

function load(id){
  const t = DATA[id];
  if(!t) return;
  aud.src = t.stream_url;
  aud.load();
  aud.play().catch(()=>{});
  // Update recently played
  recently = [id, ...recently.filter(x=>x!==id)];
  saveRecent();
  updateNP(id);
  render();
}

function updateNP(id){
  const t = DATA[id];
  const np = document.getElementById('np');
  np.classList.add('show');

  document.getElementById('npTitle').textContent  = t.title  || 'Unknown';
  document.getElementById('npArtist').textContent = t.artist || '—';
  document.getElementById('npHrt').textContent    = isFav(id) ? '❤' : '🤍';

  // Cover art
  const cover = document.getElementById('npCover');
  if(t.thumb_b64){
    cover.innerHTML = `<img class="spin" src="data:image/jpeg;base64,${t.thumb_b64}" alt=""/>`;
  } else if(t.thumb_url){
    cover.innerHTML = `<img class="spin" src="${t.thumb_url}" alt=""/>`;
  } else {
    cover.innerHTML = `<div class="no-art spin" id="npArtEl">🎵</div>`;
  }
}

function togPlay(){
  if(aud.paused) aud.play().catch(()=>{});
  else           aud.pause();
}

function nextTrack(){
  if(shuffle) qi = Math.floor(Math.random()*queue.length);
  else        qi = (qi+1) % queue.length;
  load(queue[qi]);
}
function prevTrack(){
  if(aud.currentTime > 3){ aud.currentTime=0; return; }
  qi = (qi-1+queue.length)%queue.length;
  load(queue[qi]);
}
function togShuf(){
  shuffle=!shuffle;
  document.getElementById('shufBtn').classList.toggle('active',shuffle);
}
function togRep(){
  repeat=!repeat;
  document.getElementById('repBtn').classList.toggle('active',repeat);
}
function setVol(){
  aud.volume = parseFloat(document.getElementById('vol').value);
  localStorage.setItem('mv_vol', aud.volume);
}
function seek(e){
  if(!aud.duration) return;
  aud.currentTime = (e.offsetX / document.getElementById('pbar').clientWidth) * aud.duration;
}

/* ── Favs ── */
function hrt(id){
  if(isFav(id)) favs=favs.filter(x=>x!==id);
  else          favs.push(id);
  saveFavs(); render();
  const id2 = queue[qi];
  if(id===id2) document.getElementById('npHrt').textContent=isFav(id)?'❤':'🤍';
}
function hrtNP(){
  const id=queue[qi]; if(!id) return;
  hrt(id);
}

/* ── Audio events ── */
aud.addEventListener('timeupdate',()=>{
  if(!aud.duration) return;
  const pct=(aud.currentTime/aud.duration)*100;
  document.getElementById('pfill').style.width=pct+'%';
  document.getElementById('tCur').textContent=fmt(aud.currentTime);
  document.getElementById('tTot').textContent=fmt(aud.duration);
});
aud.addEventListener('ended',()=>{ if(repeat) aud.play(); else nextTrack(); });
aud.addEventListener('play', ()=>{
  document.getElementById('playBtn').textContent='⏸';
  document.getElementById('npCover').classList.add('playing');
  render();
});
aud.addEventListener('pause',()=>{
  document.getElementById('playBtn').textContent='▶';
  document.getElementById('npCover').classList.remove('playing');
  render();
});

/* ── Init ── */
render();
if(IDs.length){ queue=[...IDs]; }
</script>
</body>
</html>""".replace("__TRACKS_JSON__", tracks_json)

# ══════════════════════════════════════════════════════════
# BLOCK 8 — THREADING HTTP SERVER
# Routes: /  → player page
#         /stream/<tid>  → audio (with Range)
#         /thumb/<tid>   → JPEG thumbnail
# ══════════════════════════════════════════════════════════

class _THTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/":              self._home();            return
        if p.startswith("/stream/"): self._stream(p[8:]); return
        if p.startswith("/thumb/"):  self._thumb(p[7:]);  return
        self.send_error(404)

    def _home(self):
        db = load_db()
        tracks = {}
        for tid, t in db.get("tracks",{}).items():
            tracks[tid] = {
                **t,
                "stream_url": f"/stream/{tid}",
                "thumb_url":  f"/thumb/{tid}",
                # Inline base64 thumbnail for fast load (≤300×300 JPEG ≈ 10-30 KB)
                "thumb_b64":  get_thumb_b64(tid),
            }
        html = build_html(json.dumps(tracks)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _thumb(self, tid: str):
        # Strip query string
        tid = tid.split("?")[0]
        p   = THUMB_DIR / f"{tid}.jpg"
        if not p.exists():
            self.send_error(404); return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, tid: str):
        tid   = tid.split("?")[0]
        db    = load_db()
        track = db.get("tracks",{}).get(tid)
        if not track:
            self.send_error(404, "Track not found"); return

        cache = CACHE_DIR / f"{tid}.audio"
        if not cache.exists():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                tf = loop.run_until_complete(APP.bot.get_file(track["file_id"]))
                loop.run_until_complete(tf.download_to_drive(str(cache)))
                loop.close()
            except Exception as e:
                logger.exception(e)
                self.send_error(500, str(e)); return

        size   = cache.stat().st_size
        mime   = mimetypes.guess_type(str(cache))[0] or "audio/mpeg"
        rng    = self.headers.get("Range","")
        start, end, status = 0, size-1, 200

        if rng.startswith("bytes="):
            try:
                r0,r1  = rng[6:].split("-",1)
                start  = int(r0) if r0 else 0
                end    = int(r1) if r1 else size-1
                status = 206
            except: pass

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type",   mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range",  f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges",  "bytes")
        self.end_headers()

        with cache.open("rb") as f:
            f.seek(start); rem = length
            while rem > 0:
                chunk = f.read(min(256*1024, rem))
                if not chunk: break
                self.wfile.write(chunk); rem -= len(chunk)

        # Increment plays in local cache + schedule async channel push
        try:
            db["tracks"][tid]["plays"] = db["tracks"][tid].get("plays", 0) + 1
            save_db(db)
            # Schedule push_index on the bot's asyncio loop from this HTTP thread
            if APP and BOT_LOOP and BOT_LOOP.is_running():
                asyncio.run_coroutine_threadsafe(
                    push_index(APP.bot, load_db()), BOT_LOOP
                )
        except Exception:
            pass

    def log_message(self,*_): pass

def start_http():
    srv = _THTTP(("0.0.0.0", HTTP_PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"🌐 HTTP → http://0.0.0.0:{HTTP_PORT}")

# ══════════════════════════════════════════════════════════
# BLOCK 9 — BOT COMMANDS
# ══════════════════════════════════════════════════════════

BOT_COMMANDS = [
    BotCommand("start",    "🏠 Home"),
    BotCommand("upload",   "⬆️  Upload a track"),
    BotCommand("browse",   "🗂  Browse library"),
    BotCommand("search",   "🔍 Search"),
    BotCommand("random",   "🎲 Random track"),
    BotCommand("favs",     "❤️  Liked tracks"),
    BotCommand("playlist", "📋 Playlists"),
    BotCommand("stats",    "📊 Stats"),
    BotCommand("resync",   "🔄 Re-sync from channel"),
    BotCommand("delete",   "🗑  Delete track (admin)"),
    BotCommand("status",   "🟢 Status"),
    BotCommand("help",     "❓ Help"),
    BotCommand("restart",  "♻️  Restart (admin)"),
]

async def _reg_cmds(app: Application):
    """
    Auto-update bot commands on every startup.
    - Fetches what Telegram currently has
    - Diffs against BOT_COMMANDS
    - Only calls set_my_commands if something changed
    - Notifies all ADMIN_IDS with a startup summary
    """
    bot = app.bot

    # ── Fetch currently registered commands from Telegram ──
    try:
        current = await bot.get_my_commands()
        current_map = {c.command: c.description for c in current}
    except Exception as e:
        logger.warning(f"_reg_cmds: could not fetch current commands: {e}")
        current_map = {}

    desired_map = {c.command: c.description for c in BOT_COMMANDS}

    added   = [f"  /{k} — {v}" for k,v in desired_map.items() if k not in current_map]
    removed = [f"  /{k}"       for k    in current_map         if k not in desired_map]
    changed = [f"  /{k}  →  {v}" for k,v in desired_map.items()
               if k in current_map and current_map[k] != v]

    needs_update = bool(added or removed or changed)

    if needs_update:
        await bot.set_my_commands(BOT_COMMANDS)
        parts = []
        if added:   parts.append("*Added:*\n"   + "\n".join(added))
        if removed: parts.append("*Removed:*\n" + "\n".join(removed))
        if changed: parts.append("*Updated:*\n" + "\n".join(changed))
        diff_text = "\n\n".join(parts)
        logger.info(f"✅ Commands updated:\n{diff_text}")
    else:
        diff_text = ""
        logger.info("✅ Commands already up to date — no changes pushed")

    # ── Notify every admin ──
    cmd_list = "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS)
    startup_msg = (
        f"🎵 *MusicVault started!*\n\n"
        f"🤖 Commands {'*updated* ✅' if needs_update else 'already up to date ✅'}\n\n"
        f"{diff_text + chr(10) if diff_text else ''}"
        f"*Active commands ({len(BOT_COMMANDS)}):*\n{cmd_list}"
    )
    for uid in ADMIN_IDS:
        try:
            await bot.send_message(uid, startup_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"_reg_cmds: could not notify admin {uid}: {e}")

async def _startup_pull(app: Application):
    """
    Called on every startup (new server or restart).
    Fetches the index from DB_CHANNEL and rebuilds local DB.
    This means musicvault.json is NEVER required to exist in advance.
    """
    if not DB_CHANNEL:
        logger.info("⚠️  DB_CHANNEL not set — running without channel sync")
        return

    logger.info("🔄 Pulling index from DB_CHANNEL…")
    db = await pull_index(app.bot)

    if db is None:
        # Channel exists but has no index yet (truly fresh)
        logger.info("📭 No index in channel — starting fresh library")
        db = {"tracks":{}, "playlists":{}, "favourites":{}}

    # Download any missing thumbnails
    missing_thumbs = 0
    for tid, t in db.get("tracks",{}).items():
        tfid = t.get("thumb_file_id","")
        if tfid and not (THUMB_DIR / f"{tid}.jpg").exists():
            ok = await download_tg_thumb(app.bot, tfid, tid)
            if ok: missing_thumbs += 1

    save_db(db)
    logger.info(f"✅ Startup sync: {len(db['tracks'])} tracks loaded"
                + (f", {missing_thumbs} thumbs downloaded" if missing_thumbs else ""))

async def _post_restart(app: Application):
    if RESTART_FLAG.exists():
        try:
            cid = int(RESTART_FLAG.read_text().strip())
            RESTART_FLAG.unlink()
            await app.bot.send_message(cid,
                "✅ *MusicVault restarted!* 🎵", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"restart notify: {e}")

def _do_restart():
    logger.info("♻️  os.execv …")
    os.execv(sys.executable, [sys.executable]+sys.argv)

# ══════════════════════════════════════════════════════════
# BLOCK 10 — /start
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import WebAppInfo
    db    = load_db()
    total = len(db["tracks"])
    url   = player_url()
    user  = update.effective_user

    rows = []
    if PUBLIC_URL:
        rows.append([InlineKeyboardButton("🎵 Open Player",
                     web_app=WebAppInfo(url=url))])
    rows += [
        [InlineKeyboardButton("🗂 Browse",  callback_data="nav:browse:0"),
         InlineKeyboardButton("🔍 Search", callback_data="nav:search"),
         InlineKeyboardButton("🎲 Random", callback_data="nav:random")],
        [InlineKeyboardButton("❤️ Liked",   callback_data="nav:favs"),
         InlineKeyboardButton("📋 Playlists",callback_data="nav:playlists"),
         InlineKeyboardButton("📊 Stats",   callback_data="nav:stats")],
    ]
    await update.message.reply_text(
        f"🎵 *MusicVault*\n\n"
        f"Hey *{user.first_name}*! 👋\n"
        f"📚 *{total}* track{'s' if total!=1 else ''} in library\n"
        f"🌐 Player: `{url}`\n\n"
        f"{'Upload your first track with /upload' if not total else 'What do you want to hear?'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )

# ══════════════════════════════════════════════════════════
# BLOCK 11 — UPLOAD FLOW
# 1. User sends audio file
# 2. Bot downloads it, extracts metadata + thumbnail
# 3. Posts to DB_CHANNEL (source of truth)
# 4. Saves to local index
# ══════════════════════════════════════════════════════════

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Only admins can upload."); return ConversationHandler.END
    if not DB_CHANNEL:
        await update.message.reply_text(
            "⚠️ *DB_CHANNEL not set!*\n\nAdd `DB_CHANNEL=your_channel_id` to `.env`",
            parse_mode=ParseMode.MARKDOWN); return ConversationHandler.END
    await update.message.reply_text(
        "⬆️ *Upload a Track*\n\nSend the audio file (MP3, FLAC, M4A, OGG…)\n\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN)
    return UPL_FILE

async def _upl_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    audio = (msg.audio or (msg.document
             if msg.document and (msg.document.mime_type or "").startswith("audio/") else None))
    if not audio:
        await msg.reply_text("Please send an audio file. /cancel to abort."); return UPL_FILE

    await msg.reply_text("⏳ Processing…")

    # Download locally for metadata extraction
    tmp_path = CACHE_DIR / f"tmp_{int(time.time()*1000)}"
    try:
        f  = await context.bot.get_file(audio.file_id)
        await f.download_to_drive(str(tmp_path))
        meta = extract_meta(str(tmp_path))
    except Exception as e:
        meta = {"title":"","artist":"","album":"","genre":"","duration":0,"thumb_bytes":None}
        logger.warning(f"meta extract: {e}")

    # Fill in from Telegram fields if mutagen got nothing
    if not meta["title"]:
        meta["title"]  = getattr(audio,"title","")  or getattr(audio,"file_name","") or ""
    if not meta["artist"]:
        meta["artist"] = getattr(audio,"performer","") or ""
    if not meta.get("duration"):
        meta["duration"] = getattr(audio,"duration",0) or 0

    context.user_data["upl"] = {
        "file_id":   audio.file_id,
        "file_name": getattr(audio,"file_name","track.mp3"),
        "tmp_path":  str(tmp_path),
        "meta":      meta,
    }
    await msg.reply_text(
        f"🎵 *{meta['title'] or 'Unknown'}*\n"
        f"⏱ {fmt_dur(meta['duration'])}\n\n"
        f"Enter *title* (or `-` to keep `{meta['title'] or 'Unknown'}`):",
        parse_mode=ParseMode.MARKDOWN)
    return UPL_TITLE

async def _upl_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t != "-": context.user_data["upl"]["meta"]["title"] = t
    cur = context.user_data["upl"]["meta"].get("artist") or "Unknown"
    await update.message.reply_text(f"👤 Artist (or `-` to keep `{cur}`):")
    return UPL_ARTIST

async def _upl_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t != "-": context.user_data["upl"]["meta"]["artist"] = t
    await update.message.reply_text("🎸 Genre (e.g. Pop, Rock — or `-` to skip):")
    return UPL_GENRE

async def _upl_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t != "-": context.user_data["upl"]["meta"]["genre"] = t

    ud   = context.user_data.pop("upl")
    meta = ud["meta"]
    uid  = update.effective_user.id

    tid = f"t{int(time.time()*1000)}"

    # Save thumbnail locally
    thumb_saved = False
    if meta.get("thumb_bytes"):
        thumb_saved = save_thumb(tid, meta["thumb_bytes"])

    # Upload thumbnail to Telegram for cross-server access
    thumb_file_id = ""
    if thumb_saved:
        try:
            thumb_path = THUMB_DIR / f"{tid}.jpg"
            sent_photo = await context.bot.send_photo(
                chat_id=int(DB_CHANNEL), photo=open(thumb_path,"rb"),
                caption=f"thumb:{tid}", disable_notification=True,
            )
            thumb_file_id = sent_photo.photo[-1].file_id
            await context.bot.delete_message(int(DB_CHANNEL), sent_photo.message_id)
        except Exception as e:
            logger.warning(f"thumb upload: {e}")

    # Build track record
    track = {
        "tid":          tid,
        "title":        meta.get("title","") or ud["file_name"],
        "artist":       meta.get("artist","") or "Unknown Artist",
        "album":        meta.get("album",""),
        "genre":        meta.get("genre",""),
        "duration":     meta.get("duration",0),
        "file_id":      ud["file_id"],
        "thumb_file_id":thumb_file_id,
        "plays":        0,
        "uploaded_by":  uid,
        "uploaded_at":  int(time.time()),
    }

    # 1. Post audio to DB_CHANNEL (persistent storage)
    msg_id = await post_audio_to_channel(context.bot, track, ud["file_id"])
    track["message_id"] = msg_id

    # 2. Add to local DB + push full index back to channel
    db = load_db()
    db["tracks"][tid] = track
    await push_index(context.bot, db)   # ← updates channel index message

    # Cleanup temp
    try: Path(ud["tmp_path"]).unlink(missing_ok=True)
    except: pass

    await update.message.reply_text(
        f"✅ *Saved!*\n\n{track_card(track, tid)}\n\n"
        f"{'🖼 Thumbnail extracted!' if thumb_saved else '🎵 No artwork found.'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Play now", callback_data=f"play:{tid}")
        ]]),
    )
    return ConversationHandler.END

async def _upl_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data.pop("upl", {})
    try: Path(ud.get("tmp_path","")).unlink(missing_ok=True)
    except: pass
    txt = "❌ Upload cancelled."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt)
    else:
        await update.message.reply_text(txt)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════
# BLOCK 12 — /resync
# Re-builds the local index from DB_CHANNEL.
# Works across servers: point bot at same DB_CHANNEL and run /resync.
# ══════════════════════════════════════════════════════════

async def cmd_resync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Rebuild local DB entirely from the DB_CHANNEL index message.
    Safe to run on a brand-new server with zero local files.
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    if not DB_CHANNEL:
        await update.message.reply_text("⚠️ DB_CHANNEL not set in .env"); return

    msg = await update.message.reply_text("🔄 Pulling index from channel…")

    db = await pull_index(context.bot)
    if db is None:
        await msg.edit_text(
            "📭 *No index found in channel.*\n\n"
            "This means no tracks have been uploaded yet, OR the index message "
            "was manually deleted.\n\n"
            "Upload tracks with /upload to create the index.",
            parse_mode=ParseMode.MARKDOWN); return

    # Re-download all missing thumbnails from Telegram
    dl = 0
    for tid, t in db.get("tracks",{}).items():
        tfid = t.get("thumb_file_id","")
        if tfid and not (THUMB_DIR / f"{tid}.jpg").exists():
            ok = await download_tg_thumb(context.bot, tfid, tid)
            if ok: dl += 1

    save_db(db)

    await msg.edit_text(
        f"✅ *Sync complete!*\n\n"
        f"🎵 *{len(db['tracks'])}* tracks loaded from channel\n"
        f"🖼 *{dl}* thumbnails downloaded\n"
        f"📋 *{len(db.get('playlists',{}))}* playlists restored\n\n"
        f"Library is ready on this server.",
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════
# BLOCK 13 — BROWSE / SEARCH / PLAY / RANDOM / FAVS
# ══════════════════════════════════════════════════════════

async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_browse(update, context, 0)

async def _show_browse(update, context, page: int):
    db   = load_db()
    all_ = sorted(db["tracks"].items(),
                  key=lambda x: x[1].get("uploaded_at",0), reverse=True)
    if not all_:
        txt = "📚 Library is empty. Use /upload to add tracks."
        s = (update.message.reply_text if hasattr(update,"message") and update.message
             else update.callback_query.edit_message_text)
        await s(txt); return

    chunk, page, pages = paginate(all_, page, 4)
    lines = "\n\n".join(track_card(t,tid,i+1+page*4) for i,(tid,t) in enumerate(chunk))
    text  = f"🗂 *Library* — {len(all_)} tracks\n\n{lines}"
    btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
             for tid,t in chunk]
    nr = nav_row("browse", page, pages)
    if nr: btns.append(nr)
    kb = InlineKeyboardMarkup(btns)
    if hasattr(update,"message") and update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await _do_search(update, context, " ".join(context.args), 0)
        return ConversationHandler.END
    await update.message.reply_text("🔍 What do you want to hear?")
    return SEARCH_Q

async def _search_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_search(update, context, update.message.text.strip(), 0)
    return ConversationHandler.END

async def _do_search(update, context, q: str, page: int):
    db   = load_db()
    ql   = q.lower()
    hits = [(tid,t) for tid,t in db["tracks"].items()
            if ql in (t.get("title","")+t.get("artist","")+
                      t.get("album","")+t.get("genre","")).lower()]
    if not hits:
        txt = f"😕 Nothing found for *{q}*"
        s = (update.callback_query.edit_message_text
             if update.callback_query else update.message.reply_text)
        await s(txt, parse_mode=ParseMode.MARKDOWN); return

    chunk, page, pages = paginate(hits, page, 4)
    lines = "\n\n".join(track_card(t,tid,i+1+page*4) for i,(tid,t) in enumerate(chunk))
    text  = f"🔍 *\"{q}\"* — {len(hits)} result{'s' if len(hits)!=1 else ''}\n\n{lines}"
    btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
             for tid,t in chunk]
    nr = nav_row(f"search:{q}", page, pages)
    if nr: btns.append(nr)
    btns.append([InlineKeyboardButton("🔍 New search", callback_data="nav:search")])
    kb = InlineKeyboardMarkup(btns)
    s = (update.callback_query.edit_message_text
         if update.callback_query else update.message.reply_text)
    await s(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def _play(q_or_update, context, tid: str):
    db = load_db()
    t  = db["tracks"].get(tid)
    if not t:
        if hasattr(q_or_update,"answer"):
            await q_or_update.answer("Track not found", show_alert=True)
        return
    cid = (q_or_update.message.chat_id
           if hasattr(q_or_update,"message") and q_or_update.message
           else q_or_update.effective_chat.id
           if hasattr(q_or_update,"effective_chat")
           else q_or_update.message.chat_id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Like",      callback_data=f"fav:{tid}"),
        InlineKeyboardButton("➕ Playlist",  callback_data=f"pl_add:{tid}"),
        InlineKeyboardButton("🔍 Browse",    callback_data="nav:browse:0"),
    ]])
    await context.bot.send_audio(
        chat_id=cid, audio=t["file_id"],
        caption=track_card(t,tid), parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

async def cmd_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db["tracks"]:
        await update.message.reply_text("📚 Library is empty."); return
    tid = random.choice(list(db["tracks"].keys()))
    await update.message.reply_text("🎲 *Random pick!*", parse_mode=ParseMode.MARKDOWN)
    await _play(update, context, tid)

async def cmd_favs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db    = load_db()
    uid   = str(update.effective_user.id)
    fids  = db.get("favourites",{}).get(uid,[])
    items = [(tid,db["tracks"][tid]) for tid in fids if tid in db["tracks"]]
    if not items:
        await update.message.reply_text("❤️ No liked tracks yet.\n\nPlay a track and tap ❤️ Like.")
        return
    lines = "\n\n".join(track_card(t,tid,i+1) for i,(tid,t) in enumerate(items))
    btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
             for tid,t in items]
    await update.message.reply_text(
        f"❤️ *Liked Tracks* — {len(items)}\n\n{lines}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns))

# ══════════════════════════════════════════════════════════
# BLOCK 14 — PLAYLISTS
# ══════════════════════════════════════════════════════════

async def cmd_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db  = load_db()
    uid = str(update.effective_user.id)
    pls = {pid:p for pid,p in db.get("playlists",{}).items()
           if str(p.get("owner_id"))==uid}
    btns = [[InlineKeyboardButton(
                f"📋 {p['name']} ({len(p.get('tracks',[]))})",
                callback_data=f"pl_view:{pid}")]
            for pid,p in pls.items()]
    btns.append([InlineKeyboardButton("➕ New playlist", callback_data="pl_new")])
    await update.message.reply_text(
        f"📋 *Your Playlists* ({len(pls)})",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns))

# ══════════════════════════════════════════════════════════
# BLOCK 15 — STATS / STATUS / HELP / DELETE / RESTART
# ══════════════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db(); tr = db.get("tracks",{})
    arts   = len({t.get("artist","") for t in tr.values() if t.get("artist")})
    genres = len({t.get("genre","")  for t in tr.values() if t.get("genre")})
    dur    = sum(t.get("duration",0) for t in tr.values())
    plays  = sum(t.get("plays",0)    for t in tr.values())
    top5   = sorted(tr.items(), key=lambda x:x[1].get("plays",0), reverse=True)[:5]
    top_t  = "\n".join(f"  {i+1}. {t.get('title','?')} — {t.get('plays',0)} plays"
                       for i,(_,t) in enumerate(top5)) or "  —"
    await update.message.reply_text(
        f"📊 *Library Stats*\n\n"
        f"🎵 Tracks: *{len(tr)}*\n👤 Artists: *{arts}*\n🎸 Genres: *{genres}*\n"
        f"⏱ Total: *{fmt_dur(dur)}*\n▶️ Plays: *{plays}*\n"
        f"📋 Playlists: *{len(db.get('playlists',{}))}*\n\n"
        f"🏆 *Top Tracks:*\n{top_t}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db  = load_db()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cache_n = len(list(CACHE_DIR.iterdir()))
    thumb_n = len(list(THUMB_DIR.iterdir()))
    await update.message.reply_text(
        f"🟢 *MusicVault*\n\n"
        f"🐍 Python `{platform.python_version()}`\n"
        f"🕐 `{now}`\n"
        f"🎵 Tracks: *{len(db['tracks'])}*\n"
        f"📡 DB Channel: `{DB_CHANNEL or 'not set'}`\n"
        f"🌐 Player: `{player_url()}`\n"
        f"💾 Cache: {cache_n} files  |  Thumbs: {thumb_n}",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS)
    await update.message.reply_text(
        f"🎵 *MusicVault Help*\n\n{lines}\n\n"
        f"🌐 Web player: `{player_url()}`\n"
        f"📡 DB Channel stores all audio — works across servers.",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    if not context.args:
        await update.message.reply_text("Usage: `/delete <track_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    tid = context.args[0].strip()
    db  = load_db()
    t   = db["tracks"].pop(tid, None)
    if not t:
        await update.message.reply_text("❌ Track not found."); return
    for p in db.get("playlists",{}).values():
        if tid in p.get("tracks",[]): p["tracks"].remove(tid)
    for fl in db.get("favourites",{}).values():
        if tid in fl: fl.remove(tid)
    (CACHE_DIR/f"{tid}.audio").unlink(missing_ok=True)
    (THUMB_DIR/f"{tid}.jpg").unlink(missing_ok=True)
    # Delete audio message from channel too
    if t.get("message_id") and DB_CHANNEL:
        try: await context.bot.delete_message(int(DB_CHANNEL), t["message_id"])
        except: pass
    # Push updated index so other servers see the deletion
    await push_index(context.bot, db)
    await update.message.reply_text(
        f"🗑 Deleted *{t.get('title','?')}*", parse_mode=ParseMode.MARKDOWN)

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    await update.message.reply_text("🔄 Restarting…", parse_mode=ParseMode.MARKDOWN)
    RESTART_FLAG.write_text(str(update.effective_chat.id))
    asyncio.get_event_loop().call_later(1.0, _do_restart)

# ══════════════════════════════════════════════════════════
# BLOCK 16 — CALLBACK ROUTER
# ══════════════════════════════════════════════════════════

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data.startswith("play:"):
        await _play(q, context, data[5:]); return
    if data.startswith("browse:"):
        await _show_browse(update, context, int(data.split(":")[1])); return
    if data.startswith("search:"):
        pts = data.split(":",2)
        await _do_search(update, context, pts[2], int(pts[1])); return

    if data.startswith("fav:"):
        tid = data[4:]; db = load_db(); uid = str(q.from_user.id)
        db.setdefault("favourites",{}).setdefault(uid,[])
        fl = db["favourites"][uid]
        if tid in fl: fl.remove(tid); lbl="💔 Removed"
        else:         fl.append(tid); lbl="❤️ Liked!"
        await push_index(context.bot, db)
        await q.answer(lbl, show_alert=False); return

    if data.startswith("pl_view:"):
        pid=data[8:]; db=load_db(); pl=db.get("playlists",{}).get(pid)
        if not pl: await q.answer("Not found",show_alert=True); return
        items=[(tid,db["tracks"][tid]) for tid in pl.get("tracks",[]) if tid in db["tracks"]]
        lines="\n\n".join(track_card(t,tid,i+1) for i,(tid,t) in enumerate(items)) or "_Empty_"
        btns=[[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}",callback_data=f"play:{tid}")]
              for tid,t in items]
        btns.append([InlineKeyboardButton("🗑 Delete playlist",callback_data=f"pl_del:{pid}")])
        await q.edit_message_text(f"📋 *{pl['name']}* — {len(items)} tracks\n\n{lines}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns)); return

    if data.startswith("pl_del:"):
        pid=data[7:]; db=load_db(); uid=str(q.from_user.id)
        pl=db.get("playlists",{}).get(pid)
        if pl and str(pl.get("owner_id"))==uid:
            del db["playlists"][pid]
            await push_index(context.bot, db)
            await q.edit_message_text("🗑 Playlist deleted.")
        else: await q.answer("Not your playlist",show_alert=True); return

    if data.startswith("pl_add:"):
        tid=data[7:]; db=load_db(); uid=str(q.from_user.id)
        pls={pid:p for pid,p in db.get("playlists",{}).items() if str(p.get("owner_id"))==uid}
        if not pls: await q.answer("No playlists. Create with /playlist",show_alert=True); return
        btns=[[InlineKeyboardButton(p["name"],callback_data=f"pl_into:{pid}:{tid}")]
              for pid,p in pls.items()]
        await q.edit_message_reply_markup(InlineKeyboardMarkup(btns)); return

    if data.startswith("pl_into:"):
        _,pid,tid=data.split(":")
        db=load_db(); uid=str(q.from_user.id)
        pl=db.get("playlists",{}).get(pid)
        if pl and str(pl.get("owner_id"))==uid:
            if tid not in pl.setdefault("tracks",[]): pl["tracks"].append(tid)
            await push_index(context.bot, db)
            await q.answer(f"✅ Added to {pl['name']}")
        else: await q.answer("Error",show_alert=True); return

    if data=="pl_new":
        await q.edit_message_text("📋 Enter a name for your new playlist:")
        context.user_data["awaiting"]="pl_name"; return

    if data=="nav:search":
        await q.edit_message_text("🔍 What do you want to hear?")
        context.user_data["awaiting"]="search"; return
    if data=="nav:random":   await q.delete_message(); await cmd_random(update,context);   return
    if data=="nav:stats":    await q.delete_message(); await cmd_stats(update,context);    return
    if data=="nav:favs":     await q.delete_message(); await cmd_favs(update,context);     return
    if data=="nav:playlists":await q.delete_message(); await cmd_playlist(update,context); return
    if data.startswith("nav:browse:"):
        await _show_browse(update,context,int(data.split(":")[2])); return
    if data=="noop": return

# ══════════════════════════════════════════════════════════
# BLOCK 17 — FREE-TEXT + AUDIO MESSAGE HANDLER
# ══════════════════════════════════════════════════════════

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg    = update.message
    await_ = context.user_data.pop("awaiting", None)

    is_audio = (msg.audio or
                (msg.document and (msg.document.mime_type or "").startswith("audio/")))
    if is_audio:
        if is_admin(msg.from_user.id):
            context.user_data["upl"] = {}
            await _upl_file(update, context)
        else:
            await msg.reply_text("⛔ Only admins can upload tracks.")
        return

    if await_ == "search":
        await _do_search(update, context, msg.text.strip(), 0); return

    if await_ == "pl_name":
        name = msg.text.strip()[:50]
        db   = load_db()
        pid  = f"pl{int(time.time()*1000)}"
        db.setdefault("playlists",{})[pid] = {
            "name": name, "owner_id": msg.from_user.id, "tracks": []
        }
        await push_index(context.bot, db)
        await msg.reply_text(
            f"✅ Playlist *{name}* created!\nTap ➕ Playlist on any track to add songs.",
            parse_mode=ParseMode.MARKDOWN); return

    await msg.reply_text("Use /help to see all commands.")

# ══════════════════════════════════════════════════════════
# BLOCK 18 — MAIN
# ══════════════════════════════════════════════════════════

def main():
    global APP

    if not BOT_TOKEN:
        print("❌  BOT_TOKEN missing in .env"); sys.exit(1)
    if not DB_CHANNEL:
        print("⚠️  DB_CHANNEL not set — uploads won't be persisted to channel")
        print("   Set DB_CHANNEL=your_channel_id (bot must be admin in channel)\n")
    if not PUBLIC_URL:
        print(f"ℹ️  PUBLIC_URL not set — Mini App button hidden")
        print(f"   Use ngrok: ngrok http {HTTP_PORT}  →  set PUBLIC_URL in .env\n")
    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS empty — anyone can upload/delete/restart\n")

    start_http()

    app = (Application.builder().token(BOT_TOKEN)
           .post_init(_reg_cmds)
           .post_init(_startup_pull)
           .post_init(_post_restart)
           .build())
    APP = app
    BOT_LOOP = asyncio.get_event_loop()

    upl_conv = ConversationHandler(
        entry_points=[CommandHandler("upload", cmd_upload)],
        states={
            UPL_FILE:   [MessageHandler(filters.AUDIO | filters.Document.AUDIO, _upl_file)],
            UPL_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _upl_title)],
            UPL_ARTIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, _upl_artist)],
            UPL_GENRE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _upl_genre)],
        },
        fallbacks=[CommandHandler("cancel", _upl_cancel),
                   CallbackQueryHandler(_upl_cancel, pattern="^upl:cancel$")],
        allow_reentry=True,
    )
    srch_conv = ConversationHandler(
        entry_points=[CommandHandler("search", cmd_search)],
        states={SEARCH_Q: [MessageHandler(filters.TEXT & ~filters.COMMAND, _search_recv)]},
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
        allow_reentry=True,
    )

    for cmd, fn in [
        ("start",    cmd_start),
        ("browse",   cmd_browse),
        ("random",   cmd_random),
        ("favs",     cmd_favs),
        ("playlist", cmd_playlist),
        ("stats",    cmd_stats),
        ("resync",   cmd_resync),
        ("delete",   cmd_delete),
        ("status",   cmd_status),
        ("help",     cmd_help),
        ("restart",  cmd_restart),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(upl_conv)
    app.add_handler(srch_conv)
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(
        (filters.AUDIO | filters.Document.AUDIO | filters.TEXT) & ~filters.COMMAND,
        msg_handler))

    logger.info("🎵 MusicVault started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
