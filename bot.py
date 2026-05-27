#!/usr/bin/env python3
"""
🎵 MusicVault — Telegram Music Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Core architecture by user — improved & extended by Claude
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Self-installs packages, skips if already present
✅ Real audio streaming  (bot downloads from Telegram, serves over HTTP)
✅ ThreadingHTTPServer   (multiple simultaneous listeners)
✅ Range-request support (seek bar works in browser)
✅ Mini App HTML player  (search, now-playing, prev/next, progress)
✅ Upload → saved to JSON DB (title / artist / genre / duration / plays)
✅ /browse  paginated library
✅ /search  full-text search
✅ /random  random track
✅ /favs    per-user favourites  (stored in DB)
✅ /playlist create / view / delete
✅ /stats   library statistics
✅ /delete  admin: remove a track
✅ Auto-register commands with Telegram on every startup
✅ /restart hot-redeploy via os.execv + back-online notification
"""

# ═══════════════════════════════════════════════════════
# 1 ── SELF-INSTALL  (skips packages already present)
# ═══════════════════════════════════════════════════════
import sys, subprocess, os

from importlib.metadata import version as _pkgver, PackageNotFoundError as _PNFE

REQUIRED = {
    "python-telegram-bot==20.7": ("python-telegram-bot", "20.7"),
    "aiohttp":                   ("aiohttp",              None),
    "python-dotenv":             ("python-dotenv",        None),
    "mutagen":                   ("mutagen",              None),
}

def _ok(dist, pin):
    try:    return not pin or _pkgver(dist) == pin
    except _PNFE: return False

def _bootstrap():
    missing = [s for s,(d,v) in REQUIRED.items() if not _ok(d,v)]
    if not missing:
        print("📦 All packages present — skipping install.\n"); return
    print("📦 Installing missing packages…")
    for s in missing:
        print(f"  ⬇️  {s}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", s, "--quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  ✅ {s}")
        except subprocess.CalledProcessError:
            print(f"  ❌ Failed: {s}  →  pip install {s}"); sys.exit(1)
    print("✅ All packages ready!\n")

_bootstrap()

# ═══════════════════════════════════════════════════════
# 2 ── IMPORTS
# ═══════════════════════════════════════════════════════
import json, logging, asyncio, time, datetime, platform, threading
import http.server, socketserver, urllib.parse, mimetypes, random
from pathlib  import Path
from dotenv   import load_dotenv

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
from telegram.error     import TelegramError

try:
    from mutagen.mp3  import MP3
    from mutagen.id3  import ID3
    from mutagen.flac import FLAC
    from mutagen.mp4  import MP4
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

# ═══════════════════════════════════════════════════════
# 3 ── CONFIG
# ═══════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_IDS  = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()]
HTTP_PORT  = int(os.getenv("HTTP_PORT", "8080"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")   # e.g. https://abc.ngrok.io

BOT_DIR      = Path(__file__).parent.resolve()
DB_FILE      = BOT_DIR / "musicvault.json"
CACHE_DIR    = BOT_DIR / "cache"
RESTART_FLAG = BOT_DIR / ".restart_chat"

CACHE_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ConversationHandler states
(
    UPL_WAITING_FILE,
    UPL_TITLE, UPL_ARTIST, UPL_GENRE,
    PL_NAME,
    SEARCH_Q,
) = range(6)

# Global app reference (needed by HTTP streaming thread)
APP: Application = None   # type: ignore

# ═══════════════════════════════════════════════════════
# 4 ── DATABASE  (JSON flat-file)
# ═══════════════════════════════════════════════════════
# Schema:
#  tracks   : { tid: {title,artist,album,genre,duration,file_id,plays,
#                      uploaded_by,uploaded_at} }
#  playlists: { pid: {name, owner_id, tracks:[tid,...]} }
#  favourites:{ uid: [tid,...] }
# ═══════════════════════════════════════════════════════

def load_db() -> dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"tracks": {}, "playlists": {}, "favourites": {}}

def save_db(db: dict):
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2), "utf-8")
    if DB_FILE.exists(): DB_FILE.unlink()
    tmp.rename(DB_FILE)

# ═══════════════════════════════════════════════════════
# 5 ── AUDIO METADATA  (mutagen, graceful fallback)
# ═══════════════════════════════════════════════════════

def read_meta(path: str) -> dict:
    m = {"title":"","artist":"","album":"","genre":"","duration":0}
    if not MUTAGEN: return m
    try:
        p = path.lower()
        if p.endswith(".mp3"):
            a = MP3(path); m["duration"] = int(a.info.length)
            try:
                tags = ID3(path)
                m["title"]  = str(tags.get("TIT2",""))
                m["artist"] = str(tags.get("TPE1",""))
                m["album"]  = str(tags.get("TALB",""))
                m["genre"]  = str(tags.get("TCON",""))
            except Exception: pass
        elif p.endswith(".flac"):
            a = FLAC(path); m["duration"] = int(a.info.length)
            m["title"]  = (a.get("title",  [""])[0])
            m["artist"] = (a.get("artist", [""])[0])
            m["album"]  = (a.get("album",  [""])[0])
            m["genre"]  = (a.get("genre",  [""])[0])
        elif p.endswith((".m4a",".mp4",".aac")):
            a = MP4(path);  m["duration"] = int(a.info.length)
            m["title"]  = (a.get("\xa9nam", [""])[0])
            m["artist"] = (a.get("\xa9ART", [""])[0])
            m["album"]  = (a.get("\xa9alb", [""])[0])
            m["genre"]  = (a.get("\xa9gen", [""])[0])
    except Exception as e:
        logger.warning(f"mutagen: {e}")
    return m

# ═══════════════════════════════════════════════════════
# 6 ── HELPERS
# ═══════════════════════════════════════════════════════

def is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS

def fmt_dur(s: int) -> str:
    if not s: return "?:??"
    m, sec = divmod(int(s), 60)
    h, m   = divmod(m, 60)
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"

def track_card(t: dict, tid: str, pos: int = 0) -> str:
    prefix = f"{pos}. " if pos else ""
    return (
        f"{prefix}🎵 *{t.get('title') or 'Unknown'}*\n"
        f"   👤 {t.get('artist') or '—'}   💿 {t.get('album') or '—'}\n"
        f"   🎸 {t.get('genre') or '—'}   ⏱ {fmt_dur(t.get('duration',0))}\n"
        f"   ▶️ {t.get('plays',0)} plays  |  `{tid}`"
    )

def paginate(items, page, size=5):
    pages = max(1, (len(items)+size-1)//size)
    page  = max(0, min(page, pages-1))
    return items[page*size:(page+1)*size], page, pages

def nav_row(prefix, page, pages):
    row = []
    if page > 0:       row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{page-1}"))
    row.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages-1: row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{page+1}"))
    return row

def mini_app_url() -> str:
    base = PUBLIC_URL or f"http://localhost:{HTTP_PORT}"
    return base + "/"

# ═══════════════════════════════════════════════════════
# 7 ── MINI APP HTML  (full player, served by HTTP server)
# ═══════════════════════════════════════════════════════

def build_html(tracks_json: str) -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<title>MusicVault</title>
<style>
:root{--g:#1DB954;--bg:#111;--card:#1a1a1a;--border:#252525;--muted:#888}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:#fff;font-family:-apple-system,BlinkMacSystemFont,
  'Segoe UI',Arial,sans-serif;display:flex;flex-direction:column;
  height:100vh;overflow:hidden;padding-bottom:0}

/* ── header ── */
.hdr{display:flex;align-items:center;gap:10px;padding:12px 14px;
  background:var(--card);border-bottom:1px solid var(--border)}
.logo{font-size:18px;font-weight:800;color:var(--g);white-space:nowrap}
.search-wrap{flex:1;display:flex;align-items:center;background:#222;
  border-radius:20px;padding:6px 12px;gap:6px;min-width:0}
.search-wrap input{flex:1;background:none;border:none;outline:none;
  color:#fff;font-size:13px;min-width:0}

/* ── tabs ── */
.tabs{display:flex;background:var(--card);border-bottom:1px solid var(--border)}
.tab{flex:1;padding:9px 0;text-align:center;font-size:11px;font-weight:700;
  color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:.2s}
.tab.on{color:var(--g);border-color:var(--g)}

/* ── genre chips ── */
.chips{display:flex;gap:6px;overflow-x:auto;padding:8px 12px;
  scrollbar-width:none;flex-shrink:0}
.chips::-webkit-scrollbar{display:none}
.chip{background:#222;border-radius:14px;padding:4px 12px;font-size:11px;
  font-weight:600;white-space:nowrap;cursor:pointer;border:1px solid transparent;transition:.15s}
.chip.on{background:var(--g);color:#000}

/* ── track list ── */
.list{flex:1;overflow-y:auto;padding:6px 10px}
.row{display:flex;align-items:center;gap:10px;padding:9px 6px;
  border-radius:10px;cursor:pointer;transition:background .15s}
.row:active,.row.now{background:rgba(29,185,84,.1)}
.num{width:20px;text-align:center;font-size:11px;color:var(--muted);flex-shrink:0}
.num.eq{color:var(--g);font-size:14px}
.art{width:42px;height:42px;border-radius:8px;background:#222;
  display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.info{flex:1;min-width:0}
.t1{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t1.now{color:var(--g)}
.t2{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dur{font-size:10px;color:var(--muted);flex-shrink:0}
.hrt{font-size:15px;background:none;border:none;cursor:pointer;padding:2px 4px;flex-shrink:0}

.empty{display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:160px;gap:8px;color:var(--muted)}
.empty-ico{font-size:40px}

/* ── now-playing bar ── */
.np{background:var(--card);border-top:1px solid var(--border);padding:10px 14px;flex-shrink:0}
.np-row{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.np-art{width:38px;height:38px;border-radius:8px;background:#222;
  display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.np-txt{flex:1;min-width:0}
.np-t1{font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.np-t2{font-size:11px;color:var(--muted)}
.np-hrt{font-size:17px;background:none;border:none;cursor:pointer}
.time-row{display:flex;justify-content:space-between;font-size:10px;
  color:var(--muted);margin-bottom:4px}
.pbar{width:100%;height:4px;background:#333;border-radius:2px;
  margin-bottom:8px;cursor:pointer;position:relative}
.pfill{height:100%;background:var(--g);border-radius:2px;transition:width .4s linear;
  pointer-events:none}
.ctrl{display:flex;align-items:center;justify-content:center;gap:16px}
.cb{background:none;border:none;cursor:pointer;color:#fff;font-size:20px;padding:4px;transition:.15s}
.cb.play{font-size:32px;color:var(--g)}
.cb.dim{color:var(--muted)}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">🎵 Vault</div>
  <div class="search-wrap">
    <span style="font-size:13px">🔍</span>
    <input id="q" placeholder="Search…" oninput="doFilter()"/>
    <span id="qClear" style="cursor:pointer;display:none" onclick="clearQ()">✕</span>
  </div>
</div>

<div class="tabs">
  <div class="tab on"  onclick="setTab('all')">All</div>
  <div class="tab"     onclick="setTab('genre')">Genre</div>
  <div class="tab"     onclick="setTab('favs')">❤️ Favs</div>
</div>

<div class="chips" id="chips" style="display:none"></div>

<div class="list" id="list"></div>

<div class="np" id="np" style="display:none">
  <div class="np-row">
    <div class="np-art">🎵</div>
    <div class="np-txt">
      <div class="np-t1" id="npT">—</div>
      <div class="np-t2" id="npA">—</div>
    </div>
    <button class="np-hrt" id="npHrt" onclick="hrtNP()">🤍</button>
  </div>
  <div class="time-row"><span id="tCur">0:00</span><span id="tTot">0:00</span></div>
  <div class="pbar" id="pbar" onclick="seek(event)">
    <div class="pfill" id="pfill" style="width:0%"></div>
  </div>
  <div class="ctrl">
    <button class="cb" onclick="prev()">⏮</button>
    <button class="cb dim" id="shuf" onclick="togShuf()">🔀</button>
    <button class="cb play" id="playBtn" onclick="togPlay()">▶️</button>
    <button class="cb dim" id="rep"  onclick="togRep()">🔁</button>
    <button class="cb" onclick="next()">⏭</button>
  </div>
</div>

<audio id="aud"></audio>

<script>
const DATA  = __TRACKS_JSON__;
const IDs   = Object.keys(DATA);

let tab     = 'all';
let genre   = null;
let filter  = '';
let queue   = [...IDs];
let qi      = 0;
let shuffle = false;
let repeat  = false;
let favs    = JSON.parse(localStorage.getItem('mv_favs')||'[]');

const aud   = document.getElementById('aud');

// ── utils ──
const fmt = s => { if(!s) return '?:??'; const m=Math.floor(s/60),sec=s%60; return `${m}:${String(sec).padStart(2,'0')}`; };
const isFav = id => favs.includes(id);
const saveFavs = () => localStorage.setItem('mv_favs', JSON.stringify(favs));

// ── filter ──
function getIds(){
  return IDs.filter(id=>{
    const t=DATA[id];
    if(tab==='favs' && !isFav(id)) return false;
    if(tab==='genre' && genre && (t.genre||'').toLowerCase()!==genre) return false;
    if(!filter) return true;
    return (t.title+t.artist+t.album+t.genre).toLowerCase().includes(filter);
  });
}

function doFilter(){
  filter = document.getElementById('q').value.toLowerCase();
  document.getElementById('qClear').style.display = filter ? 'inline' : 'none';
  render();
}
function clearQ(){ document.getElementById('q').value=''; doFilter(); }

// ── tabs ──
function setTab(t){
  tab=t; genre=null;
  document.querySelectorAll('.tab').forEach((el,i)=>{
    el.classList.toggle('on',['all','genre','favs'][i]===t);
  });
  document.getElementById('chips').style.display = t==='genre' ? 'flex' : 'none';
  if(t==='genre') buildChips();
  render();
}

function buildChips(){
  const gs=[...new Set(IDs.map(id=>(DATA[id].genre||'').toLowerCase()).filter(Boolean))];
  document.getElementById('chips').innerHTML=
    `<div class="chip ${!genre?'on':''}" onclick="setGenre(null)">All</div>`+
    gs.map(g=>`<div class="chip ${genre===g?'on':''}" onclick="setGenre('${g}')">${g}</div>`).join('');
}
function setGenre(g){ genre=g; buildChips(); render(); }

// ── render ──
function render(){
  const ids = getIds();
  const el  = document.getElementById('list');
  if(!ids.length){
    el.innerHTML='<div class="empty"><div class="empty-ico">🎵</div><div>Nothing here</div></div>';
    return;
  }
  el.innerHTML = ids.map((id,i)=>{
    const t=DATA[id], now=(id===queue[qi] && !aud.paused);
    return `<div class="row ${now?'now':''}" onclick="play('${id}')">
      <div class="num ${now?'eq':''}">${now?'♫':i+1}</div>
      <div class="art">🎵</div>
      <div class="info">
        <div class="t1 ${now?'now':''}">${t.title||'Unknown'}</div>
        <div class="t2">${t.artist||'—'} · ${t.album||'—'}</div>
      </div>
      <div class="dur">${fmt(t.duration)}</div>
      <button class="hrt" onclick="event.stopPropagation();hrt('${id}')">${isFav(id)?'❤️':'🤍'}</button>
    </div>`;
  }).join('');
}

// ── playback ──
function play(id){
  const ids=getIds();
  const idx=ids.indexOf(id);
  queue = ids;
  qi    = idx>=0 ? idx : 0;
  load(queue[qi]);
}

function load(id){
  const t=DATA[id];
  if(!t) return;
  aud.src = t.stream_url;
  aud.load();
  aud.play().catch(e=>{ alert('Playback error: '+e.message); });
  updateNP(id);
  render();
}

function updateNP(id){
  const t=DATA[id];
  document.getElementById('np').style.display='block';
  document.getElementById('npT').textContent  = t.title||'Unknown';
  document.getElementById('npA').textContent  = t.artist||'—';
  document.getElementById('npHrt').textContent= isFav(id)?'❤️':'🤍';
  document.getElementById('playBtn').textContent='⏸️';
}

function togPlay(){
  if(aud.paused){ aud.play(); document.getElementById('playBtn').textContent='⏸️'; }
  else           { aud.pause(); document.getElementById('playBtn').textContent='▶️'; }
}

function next(){
  if(shuffle) qi=Math.floor(Math.random()*queue.length);
  else        qi=(qi+1)%queue.length;
  load(queue[qi]);
}
function prev(){
  if(aud.currentTime>3){ aud.currentTime=0; return; }
  qi=(qi-1+queue.length)%queue.length;
  load(queue[qi]);
}
function togShuf(){ shuffle=!shuffle; document.getElementById('shuf').style.color=shuffle?'#1DB954':''; }
function togRep(){  repeat=!repeat;   document.getElementById('rep').style.color=repeat?'#1DB954':''; }

function seek(e){
  if(!aud.duration) return;
  aud.currentTime=(e.offsetX/document.getElementById('pbar').clientWidth)*aud.duration;
}

// ── favourites ──
function hrt(id){ if(isFav(id)) favs=favs.filter(x=>x!==id); else favs.push(id); saveFavs(); render(); }
function hrtNP(){
  const id=queue[qi]; if(!id) return;
  hrt(id); document.getElementById('npHrt').textContent=isFav(id)?'❤️':'🤍';
}

// ── audio events ──
aud.addEventListener('timeupdate',()=>{
  if(!aud.duration) return;
  const pct=(aud.currentTime/aud.duration)*100;
  document.getElementById('pfill').style.width=pct+'%';
  document.getElementById('tCur').textContent=fmt(Math.floor(aud.currentTime));
  document.getElementById('tTot').textContent=fmt(Math.floor(aud.duration));
});
aud.addEventListener('ended',()=>{ if(repeat) aud.play(); else next(); });
aud.addEventListener('pause',()=>{ document.getElementById('playBtn').textContent='▶️'; render(); });
aud.addEventListener('play', ()=>{ document.getElementById('playBtn').textContent='⏸️'; render(); });

render();
</script>
</body>
</html>""".replace("__TRACKS_JSON__", tracks_json)

# ═══════════════════════════════════════════════════════
# 8 ── THREADING HTTP SERVER  (your architecture)
# ═══════════════════════════════════════════════════════

class _ThreadHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p      = parsed.path

        if p == "/":
            self._home(); return
        if p.startswith("/stream/"):
            self._stream(p.split("/")[-1]); return
        self.send_error(404)

    # ── serve player page ──
    def _home(self):
        db = load_db()
        tracks = {
            tid: {**t, "stream_url": f"/stream/{tid}"}
            for tid, t in db.get("tracks",{}).items()
        }
        html = build_html(json.dumps(tracks)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    # ── stream audio with Range support ──
    def _stream(self, tid: str):
        db    = load_db()
        track = db.get("tracks",{}).get(tid)
        if not track:
            self.send_error(404, "Track not found"); return

        # Cache file to disk so seeking works properly
        cache = CACHE_DIR / f"{tid}.audio"
        if not cache.exists():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                tg_file = loop.run_until_complete(APP.bot.get_file(track["file_id"]))
                loop.run_until_complete(tg_file.download_to_drive(str(cache)))
                loop.close()
            except Exception as e:
                logger.exception(e)
                self.send_error(500, str(e)); return

        size  = cache.stat().st_size
        mime  = mimetypes.guess_type(cache.name)[0] or "audio/mpeg"

        # Parse Range header
        rng   = self.headers.get("Range","")
        start, end = 0, size - 1
        status = 200

        if rng.startswith("bytes="):
            try:
                r0, r1 = rng[6:].split("-", 1)
                start  = int(r0) if r0 else 0
                end    = int(r1) if r1 else size - 1
                status = 206
            except Exception:
                pass

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range",  f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges",  "bytes")
        self.end_headers()

        with cache.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256*1024, remaining))
                if not chunk: break
                self.wfile.write(chunk)
                remaining -= len(chunk)

        # Increment play count asynchronously
        try:
            db["tracks"][tid]["plays"] = db["tracks"].get(tid,{}).get("plays",0) + 1
            save_db(db)
        except Exception: pass

    def log_message(self, *_): pass   # silence request logs


def start_http_server():
    srv = _ThreadHTTP(("0.0.0.0", HTTP_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"🌐 HTTP server  →  http://0.0.0.0:{HTTP_PORT}")

# ═══════════════════════════════════════════════════════
# 9 ── BOT COMMANDS LIST
# ═══════════════════════════════════════════════════════

BOT_COMMANDS = [
    BotCommand("start",    "🏠 Home"),
    BotCommand("upload",   "⬆️  Upload a track (admin)"),
    BotCommand("browse",   "🗂  Browse library"),
    BotCommand("search",   "🔍 Search tracks"),
    BotCommand("random",   "🎲 Play a random track"),
    BotCommand("favs",     "❤️  Your favourites"),
    BotCommand("playlist", "📋 Manage playlists"),
    BotCommand("stats",    "📊 Library stats"),
    BotCommand("delete",   "🗑  Delete a track (admin)"),
    BotCommand("status",   "🟢 Bot status"),
    BotCommand("help",     "❓ Help"),
    BotCommand("restart",  "🔄 Restart bot (admin)"),
]

async def _register_commands(app: Application):
    await app.bot.set_my_commands(BOT_COMMANDS)
    logger.info("✅ Commands registered: " + ", ".join(f"/{c.command}" for c in BOT_COMMANDS))

async def _post_restart(app: Application):
    if RESTART_FLAG.exists():
        try:
            cid = int(RESTART_FLAG.read_text().strip())
            RESTART_FLAG.unlink()
            await app.bot.send_message(cid,
                "✅ *MusicVault restarted!* All systems nominal 🎵",
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"Restart notify: {e}")

def _exec_restart():
    logger.info("♻️  os.execv — restarting…")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ═══════════════════════════════════════════════════════
# 10 ── /start
# ═══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db    = load_db()
    total = len(db["tracks"])
    url   = mini_app_url()
    user  = update.effective_user

    from telegram import WebAppInfo
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Open MusicVault Player",
                              web_app=WebAppInfo(url=url))
         ] if PUBLIC_URL else [],
        [InlineKeyboardButton("🗂 Browse",    callback_data="nav:browse:0"),
         InlineKeyboardButton("🔍 Search",   callback_data="nav:search"),
         InlineKeyboardButton("🎲 Random",   callback_data="nav:random")],
        [InlineKeyboardButton("❤️ Favs",     callback_data="nav:favs"),
         InlineKeyboardButton("📋 Playlists",callback_data="nav:playlists"),
         InlineKeyboardButton("📊 Stats",    callback_data="nav:stats")],
    ])
    # Remove empty rows
    kb.inline_keyboard = [r for r in kb.inline_keyboard if r]

    await update.message.reply_text(
        f"🎵 *MusicVault*\n\n"
        f"👋 Hey *{user.first_name}*!\n"
        f"📚 Library: *{total} track{'s' if total!=1 else ''}*\n"
        f"🌐 Player: `{url}`\n\n"
        f"{'Use /upload to add your first track!' if not total else 'Browse or search below.'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

# ═══════════════════════════════════════════════════════
# 11 ── UPLOAD  (ConversationHandler, your flow + improved)
# ═══════════════════════════════════════════════════════

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Only admins can upload tracks.")
        return ConversationHandler.END
    await update.message.reply_text(
        "⬆️ *Upload a Track*\n\nSend me the audio file now "
        "(MP3, FLAC, M4A, OGG…)\n\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return UPL_WAITING_FILE

async def _upl_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    audio = (msg.audio or
             (msg.document if msg.document and
              (msg.document.mime_type or "").startswith("audio/") else None))
    if not audio:
        await msg.reply_text("Please send an audio file. /cancel to abort.")
        return UPL_WAITING_FILE

    meta = {
        "title":    getattr(audio,"title","") or getattr(audio,"file_name","") or "",
        "artist":   getattr(audio,"performer","") or "",
        "album":    "",
        "genre":    "",
        "duration": getattr(audio,"duration",0) or 0,
    }
    context.user_data["upl"] = {
        "file_id":  audio.file_id,
        "file_name":getattr(audio,"file_name","track.mp3"),
        "meta":     meta,
    }
    cur_title = meta["title"] or "Unknown"
    await msg.reply_text(
        f"🎵 Got *{meta['title'] or audio.file_id[:12]}*\n"
        f"⏱ Duration: {fmt_dur(meta['duration'])}\n\n"
        f"Enter *title* (or `-` to keep `{cur_title}`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return UPL_TITLE

async def _upl_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt != "-": context.user_data["upl"]["meta"]["title"] = txt
    cur = context.user_data["upl"]["meta"].get("artist") or "Unknown"
    await update.message.reply_text(f"👤 Artist (or `-` to keep `{cur}`):")
    return UPL_ARTIST

async def _upl_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt != "-": context.user_data["upl"]["meta"]["artist"] = txt
    await update.message.reply_text("🎸 Genre (e.g. Pop, Rock — or `-` to skip):")
    return UPL_GENRE

async def _upl_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt != "-": context.user_data["upl"]["meta"]["genre"] = txt

    ud   = context.user_data.pop("upl")
    meta = ud["meta"]
    uid  = update.effective_user.id

    tid = f"t{int(time.time()*1000)}"
    db  = load_db()
    db["tracks"][tid] = {
        "title":       meta.get("title","") or ud["file_name"],
        "artist":      meta.get("artist","") or "Unknown Artist",
        "album":       meta.get("album",""),
        "genre":       meta.get("genre",""),
        "duration":    meta.get("duration",0),
        "file_id":     ud["file_id"],
        "plays":       0,
        "uploaded_by": uid,
        "uploaded_at": int(time.time()),
    }
    save_db(db)

    await update.message.reply_text(
        f"✅ *Saved to library!*\n\n{track_card(db['tracks'][tid], tid)}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Play now", callback_data=f"play:{tid}")
        ]]),
    )
    return ConversationHandler.END

async def _upl_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("upl", None)
    txt = "❌ Upload cancelled."
    if update.callback_query:
        await update.callback_query.answer(); await update.callback_query.edit_message_text(txt)
    else:
        await update.message.reply_text(txt)
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════
# 12 ── BROWSE  (paginated, your /browse idea expanded)
# ═══════════════════════════════════════════════════════

async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_browse(update, context, 0)

async def _show_browse(update, context, page: int):
    db   = load_db()
    all_ = sorted(db["tracks"].items(),
                  key=lambda x: x[1].get("uploaded_at",0), reverse=True)
    if not all_:
        txt = "📚 Library is empty. Use /upload to add tracks."
        _send = update.message.reply_text if hasattr(update,"message") and update.message else update.callback_query.edit_message_text
        await _send(txt); return

    chunk, page, pages = paginate(all_, page, 4)
    lines = "\n\n".join(track_card(t,tid,i+1+page*4) for i,(tid,t) in enumerate(chunk))
    text  = f"🗂 *Library* — {len(all_)} track{'s' if len(all_)!=1 else ''}\n\n{lines}"

    btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
             for tid,t in chunk]
    nr    = nav_row("browse", page, pages)
    if nr: btns.append(nr)

    kb = InlineKeyboardMarkup(btns)
    if hasattr(update,"message") and update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ═══════════════════════════════════════════════════════
# 13 ── SEARCH
# ═══════════════════════════════════════════════════════

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await _do_search(update, context, " ".join(context.args), 0)
        return ConversationHandler.END
    await update.message.reply_text(
        "🔍 Send your search query (title, artist, genre…):", )
    return SEARCH_Q

async def _search_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_search(update, context, update.message.text.strip(), 0)
    return ConversationHandler.END

async def _do_search(update, context, q: str, page: int):
    db   = load_db()
    ql   = q.lower()
    hits = [(tid,t) for tid,t in db["tracks"].items()
            if ql in (t.get("title","") + t.get("artist","") +
                      t.get("album","") + t.get("genre","")).lower()]
    if not hits:
        txt = f"😕 Nothing found for *{q}*."
        _s  = (update.callback_query.edit_message_text
               if update.callback_query else update.message.reply_text)
        await _s(txt, parse_mode=ParseMode.MARKDOWN); return

    chunk, page, pages = paginate(hits, page, 4)
    lines = "\n\n".join(track_card(t,tid,i+1+page*4) for i,(tid,t) in enumerate(chunk))
    text  = f"🔍 *\"{q}\"* — {len(hits)} result{'s' if len(hits)!=1 else ''}\n\n{lines}"

    btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
             for tid,t in chunk]
    nr    = nav_row(f"search:{q}", page, pages)
    if nr: btns.append(nr)
    btns.append([InlineKeyboardButton("🔍 New search", callback_data="nav:search")])

    kb = InlineKeyboardMarkup(btns)
    _s = (update.callback_query.edit_message_text
          if update.callback_query else update.message.reply_text)
    await _s(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ═══════════════════════════════════════════════════════
# 14 ── PLAY  (your button_handler, expanded)
# ═══════════════════════════════════════════════════════

async def _play(update_or_q, context, tid: str):
    db = load_db()
    t  = db["tracks"].get(tid)
    if not t:
        if hasattr(update_or_q,"answer"): await update_or_q.answer("Track not found",show_alert=True)
        return

    cid = (update_or_q.message.chat_id
           if hasattr(update_or_q,"message") and update_or_q.message
           else update_or_q.effective_chat.id
           if hasattr(update_or_q,"effective_chat")
           else update_or_q.message.chat_id)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Fav",       callback_data=f"fav:{tid}"),
        InlineKeyboardButton("➕ Playlist",  callback_data=f"pl_add:{tid}"),
        InlineKeyboardButton("🔍 More",      callback_data="nav:browse:0"),
    ]])
    await context.bot.send_audio(
        chat_id      = cid,
        audio        = t["file_id"],
        caption      = track_card(t, tid),
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = kb,
    )

# ═══════════════════════════════════════════════════════
# 15 ── /random
# ═══════════════════════════════════════════════════════

async def cmd_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db["tracks"]:
        await update.message.reply_text("📚 Library is empty."); return
    tid = random.choice(list(db["tracks"].keys()))
    await update.message.reply_text("🎲 *Random pick!*", parse_mode=ParseMode.MARKDOWN)
    await _play(update, context, tid)

# ═══════════════════════════════════════════════════════
# 16 ── /favs
# ═══════════════════════════════════════════════════════

async def cmd_favs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db    = load_db()
    uid   = str(update.effective_user.id)
    fids  = db.get("favourites",{}).get(uid,[])
    items = [(tid,db["tracks"][tid]) for tid in fids if tid in db["tracks"]]
    if not items:
        await update.message.reply_text(
            "❤️ No favourites yet.\n\nPlay a track and tap ❤️ Fav."); return
    lines = "\n\n".join(track_card(t,tid,i+1) for i,(tid,t) in enumerate(items))
    btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
             for tid,t in items]
    await update.message.reply_text(
        f"❤️ *Your Favourites* — {len(items)} track{'s' if len(items)!=1 else ''}\n\n{lines}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns),
    )

# ═══════════════════════════════════════════════════════
# 17 ── /playlist
# ═══════════════════════════════════════════════════════

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
        reply_markup=InlineKeyboardMarkup(btns),
    )

# ═══════════════════════════════════════════════════════
# 18 ── /stats
# ═══════════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db  = load_db()
    tr  = db.get("tracks",{})
    total  = len(tr)
    arts   = len({t.get("artist","") for t in tr.values() if t.get("artist")})
    genres = len({t.get("genre","")  for t in tr.values() if t.get("genre")})
    dur    = sum(t.get("duration",0)  for t in tr.values())
    plays  = sum(t.get("plays",0)     for t in tr.values())
    pls    = len(db.get("playlists",{}))
    top5   = sorted(tr.items(), key=lambda x:x[1].get("plays",0), reverse=True)[:5]
    top_t  = "\n".join(f"  {i+1}. {t.get('title','?')} — {t.get('plays',0)} plays"
                       for i,(_,t) in enumerate(top5)) or "  —"
    await update.message.reply_text(
        f"📊 *MusicVault Stats*\n\n"
        f"🎵 Tracks: *{total}*\n"
        f"👤 Artists: *{arts}*\n"
        f"🎸 Genres: *{genres}*\n"
        f"⏱ Total time: *{fmt_dur(dur)}*\n"
        f"▶️ Total plays: *{plays}*\n"
        f"📋 Playlists: *{pls}*\n\n"
        f"🏆 *Top Tracks:*\n{top_t}",
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════
# 19 ── /delete  (admin)
# ═══════════════════════════════════════════════════════

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/delete <track_id>`\nGet the ID from /browse.",
            parse_mode=ParseMode.MARKDOWN); return
    tid = context.args[0].strip()
    db  = load_db()
    t   = db["tracks"].pop(tid, None)
    if not t:
        await update.message.reply_text("❌ Track not found."); return
    # Remove from playlists & favourites
    for p in db.get("playlists",{}).values():
        p.get("tracks",[]).remove(tid) if tid in p.get("tracks",[]) else None
    for flist in db.get("favourites",{}).values():
        if tid in flist: flist.remove(tid)
    # Remove cache
    (CACHE_DIR/f"{tid}.audio").unlink(missing_ok=True)
    save_db(db)
    await update.message.reply_text(f"🗑 Deleted *{t.get('title','?')}*", parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════
# 20 ── /status  /help  /restart
# ═══════════════════════════════════════════════════════

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db  = load_db()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(
        f"🟢 *MusicVault running*\n\n"
        f"🐍 Python `{platform.python_version()}`\n"
        f"🖥 `{platform.system()} {platform.release()}`\n"
        f"🕐 `{now}`\n"
        f"🎵 Tracks: *{len(db['tracks'])}*\n"
        f"🌐 Player: `{mini_app_url()}`\n"
        f"📁 DB: `{DB_FILE}`\n"
        f"💾 Cache: `{CACHE_DIR}` ({len(list(CACHE_DIR.iterdir()))} files)",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS)
    await update.message.reply_text(
        f"🎵 *MusicVault Help*\n\n{lines}\n\n"
        "Send an audio file directly to upload it (admin only).\n"
        f"Open the web player at `{mini_app_url()}`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    await update.message.reply_text(
        "🔄 *Restarting…* You'll get a message when back online.",
        parse_mode=ParseMode.MARKDOWN)
    RESTART_FLAG.write_text(str(update.effective_chat.id))
    asyncio.get_event_loop().call_later(1.0, _exec_restart)

# ═══════════════════════════════════════════════════════
# 21 ── CALLBACK BUTTON ROUTER
# ═══════════════════════════════════════════════════════

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    # play
    if data.startswith("play:"):
        await _play(q, context, data[5:]); return

    # browse pagination
    if data.startswith("browse:"):
        await _show_browse(update, context, int(data.split(":")[1])); return

    # search pagination
    if data.startswith("search:"):
        parts = data.split(":", 2)     # search : page : query
        await _do_search(update, context, parts[2], int(parts[1])); return

    # favourites toggle
    if data.startswith("fav:"):
        tid = data[4:]
        db  = load_db()
        uid = str(q.from_user.id)
        db.setdefault("favourites",{}).setdefault(uid,[])
        fl  = db["favourites"][uid]
        if tid in fl: fl.remove(tid); msg = "💔 Removed from favourites"
        else:         fl.append(tid); msg = "❤️ Added to favourites!"
        save_db(db); await q.answer(msg, show_alert=False); return

    # playlist view
    if data.startswith("pl_view:"):
        pid = data[8:]
        db  = load_db()
        pl  = db.get("playlists",{}).get(pid)
        if not pl: await q.answer("Not found", show_alert=True); return
        items = [(tid,db["tracks"][tid]) for tid in pl.get("tracks",[]) if tid in db["tracks"]]
        lines = "\n\n".join(track_card(t,tid,i+1) for i,(tid,t) in enumerate(items)) or "_Empty_"
        btns  = [[InlineKeyboardButton(f"▶️ {t.get('title','?')[:26]}", callback_data=f"play:{tid}")]
                 for tid,t in items]
        btns.append([InlineKeyboardButton("🗑 Delete playlist", callback_data=f"pl_del:{pid}")])
        await q.edit_message_text(
            f"📋 *{pl['name']}* — {len(items)} tracks\n\n{lines}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(btns)); return

    # playlist delete
    if data.startswith("pl_del:"):
        pid = data[7:]
        db  = load_db()
        uid = str(q.from_user.id)
        pl  = db.get("playlists",{}).get(pid)
        if pl and str(pl.get("owner_id"))==uid:
            del db["playlists"][pid]; save_db(db)
            await q.edit_message_text("🗑 Playlist deleted.")
        else:
            await q.answer("Not your playlist", show_alert=True); return

    # add to playlist — step 1: pick playlist
    if data.startswith("pl_add:"):
        tid = data[7:]
        db  = load_db()
        uid = str(q.from_user.id)
        pls = {pid:p for pid,p in db.get("playlists",{}).items()
               if str(p.get("owner_id"))==uid}
        if not pls:
            await q.answer("No playlists. Create one with /playlist", show_alert=True); return
        btns = [[InlineKeyboardButton(p["name"], callback_data=f"pl_into:{pid}:{tid}")]
                for pid,p in pls.items()]
        await q.edit_message_reply_markup(InlineKeyboardMarkup(btns)); return

    # add to playlist — step 2: confirm add
    if data.startswith("pl_into:"):
        _, pid, tid = data.split(":")
        db  = load_db()
        uid = str(q.from_user.id)
        pl  = db.get("playlists",{}).get(pid)
        if pl and str(pl.get("owner_id"))==uid:
            if tid not in pl.setdefault("tracks",[]): pl["tracks"].append(tid)
            save_db(db); await q.answer(f"✅ Added to {pl['name']}")
        else:
            await q.answer("Error", show_alert=True); return

    # new playlist (triggers text conversation via user_data flag)
    if data == "pl_new":
        await q.edit_message_text("📋 Enter a name for your new playlist:")
        context.user_data["awaiting"] = "pl_name"; return

    # nav shortcuts
    if data == "nav:search":
        await q.edit_message_text("🔍 Send your search query:")
        context.user_data["awaiting"] = "search"; return
    if data == "nav:random":
        await q.delete_message(); await cmd_random(update, context); return
    if data == "nav:stats":
        await q.delete_message(); await cmd_stats(update, context); return
    if data == "nav:favs":
        await q.delete_message(); await cmd_favs(update, context); return
    if data == "nav:playlists":
        await q.delete_message(); await cmd_playlist(update, context); return
    if data.startswith("nav:browse:"):
        await _show_browse(update, context, int(data.split(":")[2])); return

    if data == "noop": return

# ═══════════════════════════════════════════════════════
# 22 ── FREE-TEXT + AUDIO MESSAGE HANDLER
# ═══════════════════════════════════════════════════════

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = update.message
    await_ = context.user_data.pop("awaiting", None)

    # Direct audio upload
    is_audio = (msg.audio or
                (msg.document and
                 (msg.document.mime_type or "").startswith("audio/")))
    if is_audio:
        if is_admin(msg.from_user.id):
            # Jump straight into the upload flow
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
            "name":     name,
            "owner_id": msg.from_user.id,
            "tracks":   [],
        }
        save_db(db)
        await msg.reply_text(
            f"✅ Playlist *{name}* created!\n\nPlay a track and tap ➕ Playlist to add songs.",
            parse_mode=ParseMode.MARKDOWN); return

    await msg.reply_text("Use /help to see all commands.")

# ═══════════════════════════════════════════════════════
# 23 ── MAIN
# ═══════════════════════════════════════════════════════

def main():
    global APP

    if not BOT_TOKEN:
        print("❌  BOT_TOKEN missing in .env"); sys.exit(1)

    if not PUBLIC_URL:
        print(f"ℹ️  PUBLIC_URL not set — Mini App button won't appear.")
        print(f"   Expose port {HTTP_PORT} with ngrok and set PUBLIC_URL in .env\n")

    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS empty — anyone can upload/delete/restart\n")

    start_http_server()

    app = Application.builder().token(BOT_TOKEN)\
        .post_init(_register_commands)\
        .post_init(_post_restart)\
        .build()
    APP = app

    # Upload conversation
    upl_conv = ConversationHandler(
        entry_points=[CommandHandler("upload", cmd_upload)],
        states={
            UPL_WAITING_FILE: [
                MessageHandler(filters.AUDIO | filters.Document.AUDIO, _upl_file),
            ],
            UPL_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _upl_title)],
            UPL_ARTIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, _upl_artist)],
            UPL_GENRE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _upl_genre)],
        },
        fallbacks=[
            CommandHandler("cancel", _upl_cancel),
            CallbackQueryHandler(_upl_cancel, pattern="^upl:cancel$"),
        ],
        allow_reentry=True,
    )

    # Search conversation
    srch_conv = ConversationHandler(
        entry_points=[CommandHandler("search", cmd_search)],
        states={SEARCH_Q: [MessageHandler(filters.TEXT & ~filters.COMMAND, _search_recv)]},
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("browse",   cmd_browse))
    app.add_handler(CommandHandler("random",   cmd_random))
    app.add_handler(CommandHandler("favs",     cmd_favs))
    app.add_handler(CommandHandler("playlist", cmd_playlist))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("delete",   cmd_delete))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("restart",  cmd_restart))
    app.add_handler(upl_conv)
    app.add_handler(srch_conv)
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(
        (filters.AUDIO | filters.Document.AUDIO | filters.TEXT) & ~filters.COMMAND,
        msg_handler,
    ))

    logger.info("🎵 MusicVault started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
