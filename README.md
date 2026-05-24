# 🎵 Spotify Telegram Mini App Bot

A Telegram bot that opens a **Spotify player as a Mini App** inside Telegram.  
Fully self-bootstrapping — installs packages, registers commands, and can restart itself.

---

## ✨ Features

| Feature | Details |
|---|---|
| 📦 Self-installing | Auto-installs all Python dependencies on first run |
| 🤖 Auto command registration | Pushes commands to Telegram's menu on every startup |
| 🔄 `/restart` redeploy | Hot-restarts the process via `os.execv` — no shell scripts needed |
| ✅ Restart notification | Sends "back online" message to whoever triggered `/restart` |
| 🎵 Spotify Mini App | Embedded Spotify player opens natively inside Telegram |
| 🎨 Theme-aware | Adapts to Telegram's light/dark theme |
| 🔐 Admin control | Restrict `/restart` to specific Telegram user IDs |

---

## 📋 Requirements

- Python **3.8+**
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

---

## 🚀 Quick Start

### 1. Get a Bot Token

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the token (e.g. `7123456789:AAF...`)

### 2. Create a `.env` file

```env
BOT_TOKEN=your_telegram_bot_token_here
```

### 3. Run

```bash
python bot.py
```

The bot will:
1. Auto-install `python-telegram-bot`, `aiohttp`, `python-dotenv`
2. Register all commands with Telegram (they appear in the `/` menu instantly)
3. Start polling for messages

---

## ⚙️ Full `.env` Reference

```env
# Required
BOT_TOKEN=your_telegram_bot_token_here

# Optional: Custom Mini App URL (must be HTTPS)
MINI_APP_URL=https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M?utm_source=generator

# Optional: Restrict /restart to specific Telegram user IDs (comma-separated)
# Leave empty to allow anyone to restart the bot
ADMIN_IDS=123456789,987654321
```

---

## 🤖 Bot Commands

All commands are **auto-registered** with Telegram every time the bot starts.  
To add a new command, edit `BOT_COMMANDS` in `bot.py` — no BotFather interaction needed.

| Command | Description |
|---|---|
| `/start` | Open the Spotify Mini App |
| `/play` | Launch the Spotify player |
| `/charts` | View Global Top 50 charts |
| `/status` | Check if the bot is alive |
| `/help` | Show all commands |
| `/restart` | Restart & redeploy the bot |

---

## 🔄 How `/restart` Works

```
User sends /restart
       │
       ▼
Bot replies "Restarting…"
       │
       ▼
Saves chat_id to .restart_chat (temp file)
       │
       ▼
os.execv() — replaces the running process
with a fresh invocation of bot.py
       │
       ▼
New process starts:
  • Re-installs/checks packages
  • Re-registers commands with Telegram
  • Reads .restart_chat → sends "✅ Back online!" message
  • Deletes .restart_chat
```

This is a **true hot-restart** — no external process manager, shell script,  
or systemd unit required. The process replaces itself in memory.

---

## 🔐 Restricting `/restart` to Admins

Set `ADMIN_IDS` in `.env` to a comma-separated list of Telegram user IDs:

```env
ADMIN_IDS=123456789
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

If `ADMIN_IDS` is empty, **any user** can trigger a restart.

---

## ➕ Adding New Commands

1. Add an entry to `BOT_COMMANDS` in `bot.py`:
   ```python
   BotCommand("newcmd", "🆕 My new command"),
   ```

2. Write the handler function:
   ```python
   async def newcmd(update, context):
       await update.message.reply_text("Hello from new command!")
   ```

3. Register the handler in `main()`:
   ```python
   app.add_handler(CommandHandler("newcmd", newcmd))
   ```

4. Send `/restart` in Telegram — the new command appears in the menu immediately.

---

## 🌐 Hosting Your Own Mini App (Optional)

The default setup uses Spotify's embed URL directly.  
For a fully custom Mini App (with your own HTML), host it at an HTTPS URL:

### Quick option — ngrok

```bash
# Serve the built-in HTML
python -c "
import http.server, threading
html = open('bot.py').read().split('MINI_APP_HTML = \"\"\"')[1].split('\"\"\"')[0]
open('/tmp/index.html','w').write(html)
"
# Then serve /tmp with any static server and tunnel via ngrok
ngrok http 8080
```

Set the HTTPS ngrok URL in `.env`:
```env
MINI_APP_URL=https://abc123.ngrok.io
```

---

## 🗂 Project Structure

```
.
├── bot.py           # Everything — bot logic, Mini App HTML, self-install
├── .env             # Your secrets (never commit this)
├── .restart_chat    # Temp file created during /restart (auto-deleted)
└── README.md        # This file
```

---

## 🐛 Troubleshooting

| Problem | Fix |
|---|---|
| `BOT_TOKEN not set` | Create `.env` with `BOT_TOKEN=...` |
| Commands not showing in Telegram | Wait ~30 seconds; Telegram caches the menu |
| `/restart` not working | Check `ADMIN_IDS` — your user ID must be listed, or leave it empty |
| `pip install` fails | Run manually: `pip install python-telegram-bot==20.7 aiohttp python-dotenv` |
| `Conflict: terminated by other getUpdates` | Only one bot instance can run at a time |

---

## 📄 License

MIT — free to use, modify, and distribute.
