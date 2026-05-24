#!/usr/bin/env python3
"""
Telegram Bot with Spotify Mini App
✅ Self-installs dependencies
✅ Auto-registers bot commands with Telegram on startup
✅ /restart re-deploys itself (hot-reload via os.execv)
"""

import sys
import subprocess
import os

# ──────────────────────────────────────────────
# STEP 1: Auto-install required packages
# ──────────────────────────────────────────────
REQUIRED_PACKAGES = [
    "python-telegram-bot==20.7",
    "aiohttp",
    "python-dotenv",
]

def install_packages():
    print("📦 Checking and installing required packages...")
    for pkg in REQUIRED_PACKAGES:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"  ✅ {pkg}")
        except subprocess.CalledProcessError:
            print(f"  ❌ Failed to install {pkg}. Try: pip install {pkg}")
            sys.exit(1)
    print("✅ All packages ready!\n")

install_packages()

# ──────────────────────────────────────────────
# STEP 2: Imports (after packages are installed)
# ──────────────────────────────────────────────
import logging
import signal
import asyncio
from dotenv import load_dotenv
from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────
# STEP 3: Configuration
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MINI_APP_URL = os.getenv(
    "MINI_APP_URL",
    "https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M?utm_source=generator",
)
# Only users whose Telegram ID is here can use /restart (leave empty to allow anyone)
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# STEP 4: Command definitions (single source of truth)
# Editing this list automatically updates Telegram's command menu.
# ──────────────────────────────────────────────
BOT_COMMANDS = [
    BotCommand("start",   "🎧 Open the Spotify Mini App"),
    BotCommand("play",    "▶️  Launch the Spotify player"),
    BotCommand("charts",  "📈 View Global Top 50 charts"),
    BotCommand("help",    "❓ Show all available commands"),
    BotCommand("status",  "🟢 Check if the bot is alive"),
    BotCommand("restart", "🔄 Restart & redeploy the bot"),
]

# ──────────────────────────────────────────────
# STEP 5: Mini App HTML (served at localhost:8080 if needed)
# ──────────────────────────────────────────────
MINI_APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Spotify Mini App</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:var(--tg-theme-bg-color,#121212);color:var(--tg-theme-text-color,#fff);
      font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;display:flex;flex-direction:column;
      align-items:center;min-height:100vh;padding:20px}
    .header{display:flex;align-items:center;gap:12px;margin-bottom:24px;width:100%}
    h1{font-size:22px;font-weight:700;color:#1DB954}
    .player-container{width:100%;max-width:500px;border-radius:16px;overflow:hidden;
      box-shadow:0 8px 32px rgba(0,0,0,.5);margin-bottom:20px}
    iframe{border:none;display:block}
    .btn{display:inline-flex;align-items:center;gap:8px;background:#1DB954;color:#000;
      font-weight:700;font-size:15px;border:none;border-radius:50px;padding:14px 32px;
      cursor:pointer;text-decoration:none;margin-top:8px;transition:background .2s}
    .btn:hover{background:#1ed760}
    .footer{margin-top:24px;font-size:12px;opacity:.5;text-align:center}
  </style>
</head>
<body>
  <div class="header">
    <svg width="40" height="40" viewBox="0 0 24 24" fill="#1DB954">
      <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
    </svg>
    <h1>Spotify Player</h1>
  </div>
  <div class="player-container">
    <iframe
      src="https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M?utm_source=generator&theme=0"
      width="100%" height="380"
      allow="autoplay;clipboard-write;encrypted-media;fullscreen;picture-in-picture"
      loading="lazy"></iframe>
  </div>
  <a class="btn" href="https://open.spotify.com" target="_blank">Open Spotify App</a>
  <div class="footer">Powered by Spotify Web Embed · Telegram Mini App</div>
  <script>
    const tg = window.Telegram.WebApp;
    tg.ready(); tg.expand(); tg.MainButton.hide();
    document.body.style.background = tg.colorScheme==='dark' ? '#121212' : '#f8f8f8';
  </script>
</body>
</html>
"""

# ──────────────────────────────────────────────
# STEP 6: Helpers
# ──────────────────────────────────────────────
def _keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 Open Spotify", web_app=WebAppInfo(url=url))
    ]])

def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

# ──────────────────────────────────────────────
# STEP 7: Command auto-registration (called on startup)
# ──────────────────────────────────────────────
async def register_commands(app: Application) -> None:
    """Push BOT_COMMANDS to Telegram so they appear in the menu automatically."""
    await app.bot.set_my_commands(BOT_COMMANDS)
    names = ", ".join(f"/{c.command}" for c in BOT_COMMANDS)
    logger.info(f"✅ Commands registered with Telegram: {names}")

# ──────────────────────────────────────────────
# STEP 8: Handler functions
# ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = context.bot_data["mini_app_url"]
    user = update.effective_user
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Open Spotify", web_app=WebAppInfo(url=url))],
        [
            InlineKeyboardButton("🔍 Search", callback_data="search"),
            InlineKeyboardButton("📈 Charts", callback_data="charts"),
        ],
    ])
    await update.message.reply_text(
        f"👋 Hey {user.first_name}!\n\n"
        "🎧 *Welcome to the Spotify Bot!*\n\n"
        "Tap the button below to open the player right inside Telegram.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS)
    await update.message.reply_text(
        f"🎵 *Available Commands*\n\n{lines}",
        parse_mode="Markdown",
    )


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = context.bot_data["mini_app_url"]
    await update.message.reply_text(
        "🎵 Ready to listen? Tap below!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Play Now", web_app=WebAppInfo(url=url))
        ]]),
    )


async def charts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = context.bot_data["mini_app_url"]
    await update.message.reply_text(
        "🌍 *Global Top Charts*\n\nTap to open the trending playlist!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📈 View Top Charts", web_app=WebAppInfo(url=url))
        ]]),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import platform, datetime
    uptime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(
        f"🟢 *Bot is running!*\n\n"
        f"🐍 Python `{platform.python_version()}`\n"
        f"🖥 `{platform.system()} {platform.release()}`\n"
        f"🕐 Server time: `{uptime}`\n"
        f"🌐 Mini App URL set: `{'Yes' if context.bot_data.get('mini_app_url') else 'No'}`",
        parse_mode="Markdown",
    )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Re-executes the current Python process from scratch using os.execv.
    This is a true hot-restart: the running process replaces itself with a
    fresh invocation of bot.py — packages are re-checked, commands re-registered.
    Admin-only if ADMIN_IDS is set.
    """
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ You don't have permission to restart the bot.")
        return

    await update.message.reply_text(
        "🔄 *Restarting bot…*\n\n"
        "The bot will shut down and redeploy itself.\n"
        "You'll receive a message when it's back online.",
        parse_mode="Markdown",
    )
    logger.info(f"🔄 Restart requested by user {user.id} (@{user.username})")

    # Store the chat_id so the restarted process can send a "back online" message
    # We use a tiny temp file as IPC between the old and new process
    restart_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".restart_chat")
    with open(restart_flag, "w") as f:
        f.write(str(update.effective_chat.id))

    # Schedule the os.execv slightly after this coroutine returns so the
    # reply message above is flushed first
    asyncio.get_event_loop().call_later(1.0, _do_restart)


def _do_restart():
    """Replace the current process image with a fresh bot.py invocation."""
    logger.info("♻️  Executing os.execv — handing off to new process...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    url = context.bot_data["mini_app_url"]

    if query.data == "search":
        await query.edit_message_text(
            "🔍 *Search Spotify*\n\n_(Open the player and use the built-in search bar)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎵 Open Player", web_app=WebAppInfo(url=url))
            ]]),
        )
    elif query.data == "charts":
        await query.edit_message_text(
            "📈 *Global Top 50*\n\nOpening charts now...",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Open Charts", web_app=WebAppInfo(url=url))
            ]]),
        )


async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = update.message.web_app_data.data
    logger.info(f"Mini App data received: {data}")
    await update.message.reply_text(
        f"🎵 Received from Mini App:\n`{data}`",
        parse_mode="Markdown",
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = context.bot_data["mini_app_url"]
    await update.message.reply_text(
        "Use /help to see all commands, or tap below to open Spotify!",
        reply_markup=_keyboard(url),
    )

# ──────────────────────────────────────────────
# STEP 9: Post-restart notification
# Runs after startup to notify the user who triggered /restart
# ──────────────────────────────────────────────
async def post_restart_notify(app: Application) -> None:
    restart_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".restart_chat")
    if os.path.exists(restart_flag):
        try:
            with open(restart_flag) as f:
                chat_id = int(f.read().strip())
            os.remove(restart_flag)
            await app.bot.send_message(
                chat_id=chat_id,
                text="✅ *Bot restarted successfully!*\n\nAll systems nominal. Commands re-registered.",
                parse_mode="Markdown",
            )
            logger.info(f"✅ Post-restart notification sent to chat {chat_id}")
        except Exception as e:
            logger.warning(f"Could not send restart notification: {e}")

# ──────────────────────────────────────────────
# STEP 10: Main
# ──────────────────────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌  BOT_TOKEN not set!")
        print("    Create a .env file:  BOT_TOKEN=your_token_here")
        print("    Or:  export BOT_TOKEN=your_token_here")
        print("    Get a token from @BotFather on Telegram.\n")
        sys.exit(1)

    print("🤖 Starting Spotify Telegram Bot...")
    print(f"🌐 Mini App URL: {MINI_APP_URL}")
    if ADMIN_IDS:
        print(f"🔐 Admin IDs: {ADMIN_IDS}")
    else:
        print("⚠️  No ADMIN_IDS set — anyone can use /restart")
    print()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(register_commands)   # ← auto-registers commands on every startup
        .post_init(post_restart_notify) # ← sends "back online" message after restart
        .build()
    )
    app.bot_data["mini_app_url"] = MINI_APP_URL

    # Register handlers
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("play",    play_command))
    app.add_handler(CommandHandler("charts",  charts_command))
    app.add_handler(CommandHandler("status",  status_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    print("✅ Bot is running! Press Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
