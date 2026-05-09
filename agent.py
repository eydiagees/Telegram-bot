#!/usr/bin/env python3
“””
Telegram AI Agent – Sprache, Kalender & Notizen
Für zwei Nutzer (DE + EN), iCloud Kalender, GPT-4 + Whisper
“””

import os
import json
import tempfile
import logging
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI
import caldav
from icalendar import Calendar, Event
import pytz

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(**name**)

# ── Konfiguration ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get(“TELEGRAM_TOKEN”)
OPENAI_API_KEY = os.environ.get(“OPENAI_API_KEY”)
ICLOUD_USER     = os.environ.get(“ICLOUD_USER”)       # Apple ID (E-Mail)
ICLOUD_PASSWORD = os.environ.get(“ICLOUD_PASSWORD”)   # App-spezifisches Passwort
ALLOWED_USERS   = os.environ.get(“ALLOWED_USERS”, “”).split(”,”)  # Telegram User-IDs

# Nutzernamen für persönliche Ansprache

USER_NAMES = {}
for entry in os.environ.get(“USER_NAMES”, “”).split(”,”):
if “:” in entry:
uid, name = entry.split(”:”, 1)
USER_NAMES[uid.strip()] = name.strip()

client = OpenAI(api_key=OPENAI_API_KEY)

# ── Datenpersistenz ───────────────────────────────────────────────────────────

DATA_FILE = “agent_data.json”

def load_data():
if os.path.exists(DATA_FILE):
with open(DATA_FILE, “r”, encoding=“utf-8”) as f:
return json.load(f)
return {“notes”: [], “todos”: [], “conversation”: []}

def save_data(data):
with open(DATA_FILE, “w”, encoding=“utf-8”) as f:
json.dump(data, f, ensure_ascii=False, indent=2)

# ── iCloud Kalender ───────────────────────────────────────────────────────────

def get_calendar_client():
try:
client_cal = caldav.DAVClient(
url=“https://caldav.icloud.com”,
username=ICLOUD_USER,
password=ICLOUD_PASSWORD
)
principal = client_cal.principal()
calendars = principal.calendars()
return calendars[0] if calendars else None
except Exception as e:
logger.error(f”Kalender-Fehler: {e}”)
return None

def get_upcoming_events(days=7):
try:
cal = get_calendar_client()
if not cal:
return “Kalender nicht verfügbar.”
now = datetime.now(pytz.UTC)
end = now + timedelta(days=days)
events = cal.date_search(start=now, end=end)
if not events:
return f”Keine Termine in den nächsten {days} Tagen.”
result = []
for event in events:
cal_obj = Calendar.from_ical(event.data)
for component in cal_obj.walk():
if component.name == “VEVENT”:
summary = str(component.get(“SUMMARY”, “Kein Titel”))
dtstart = component.get(“DTSTART”).dt
if hasattr(dtstart, ‘strftime’):
date_str = dtstart.strftime(”%d.%m.%Y %H:%M”)
else:
date_str = str(dtstart)
result.append(f”• {date_str}: {summary}”)
return “\n”.join(result) if result else “Keine Termine gefunden.”
except Exception as e:
return f”Fehler beim Laden der Termine: {e}”

def add_calendar_event(title, start_dt, end_dt=None):
try:
cal = get_calendar_client()
if not cal:
return False
if end_dt is None:
end_dt = start_dt + timedelta(hours=1)
cal_obj = Calendar()
event = Event()
event.add(“SUMMARY”, title)
event.add(“DTSTART”, start_dt)
event.add(“DTEND”, end_dt)
cal_obj.add_component(event)
cal.add_event(cal_obj.to_ical())
return True
except Exception as e:
logger.error(f”Event-Fehler: {e}”)
return False

# ── GPT-4 Agent ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = “”“Du bist ein persönlicher KI-Assistent für ein Paar (ein deutschsprachiger Mann und eine englischsprachige Frau).

Du hilfst bei:

- Kalender & Terminen (iCloud)
- Notizen & Aufgaben
- Allgemeinen Fragen

Antworte IMMER in der Sprache, in der du angesprochen wirst.
Sei freundlich, kurz und direkt – die Nutzer haben ADHS und brauchen klare, strukturierte Antworten.
Vermeide lange Texte. Nutze Emojis sparsam aber hilfreich.

Wenn du Termine aus dem Kalender zeigst, formatiere sie übersichtlich.
Wenn du einen Termin hinzufügst, bestätige kurz mit Datum und Uhrzeit.

Aktuelle Daten & Notizen werden dir als Kontext mitgegeben.
“””

async def ask_gpt(user_message: str, user_id: str, context_data: dict) -> str:
user_name = USER_NAMES.get(user_id, “”)

```
# Kontext aufbauen
notes_text = "\n".join([f"- {n['text']} ({n['datum']})" for n in context_data.get("notes", [])[-5:]])
todos_text = "\n".join([f"- {'✅' if t['erledigt'] else '⏳'} {t['text']}" for t in context_data.get("todos", [])[-5:]])
events_text = get_upcoming_events()

context_msg = f"""
```

Nutzer: {user_name if user_name else user_id}
Aktuelle Zeit: {datetime.now().strftime(’%d.%m.%Y %H:%M’)}

Kommende Termine:
{events_text}

Letzte Notizen:
{notes_text if notes_text else ‘Keine Notizen’}

Aufgaben:
{todos_text if todos_text else ‘Keine Aufgaben’}
“””

```
messages = [
    {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context_msg},
]

# Letzte 10 Nachrichten als Konversationskontext
for msg in context_data.get("conversation", [])[-10:]:
    messages.append(msg)

messages.append({"role": "user", "content": user_message})

response = client.chat.completions.create(
    model="gpt-4o",
    messages=messages,
    max_tokens=500
)

return response.choices[0].message.content
```

# ── Spracherkennung ───────────────────────────────────────────────────────────

async def transcribe_voice(file_path: str) -> str:
with open(file_path, “rb”) as audio_file:
transcript = client.audio.transcriptions.create(
model=“whisper-1”,
file=audio_file
)
return transcript.text

# ── Telegram Handler ──────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
if not ALLOWED_USERS or ALLOWED_USERS == [””]:
return True
return str(update.effective_user.id) in ALLOWED_USERS

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_allowed(update):
await update.message.reply_text(“❌ Kein Zugriff.”)
return

```
user_id = str(update.effective_user.id)
text = update.message.text
data = load_data()

await update.message.chat.send_action("typing")

response = await ask_gpt(text, user_id, data)

# Konversation speichern
data["conversation"].append({"role": "user", "content": text})
data["conversation"].append({"role": "assistant", "content": response})
if len(data["conversation"]) > 20:
    data["conversation"] = data["conversation"][-20:]
save_data(data)

await update.message.reply_text(response)
```

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_allowed(update):
await update.message.reply_text(“❌ Kein Zugriff.”)
return

```
user_id = str(update.effective_user.id)
data = load_data()

await update.message.chat.send_action("typing")

# Sprachdatei herunterladen
voice = update.message.voice
voice_file = await context.bot.get_file(voice.file_id)

with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
    tmp_path = tmp.name

await voice_file.download_to_drive(tmp_path)

# Transkribieren
text = await transcribe_voice(tmp_path)
os.unlink(tmp_path)

user_name = USER_NAMES.get(user_id, "")
await update.message.reply_text(f"🎤 *{user_name + ': ' if user_name else ''}_{text}_*", parse_mode="Markdown")

# GPT-4 antworten lassen
response = await ask_gpt(text, user_id, data)

# Konversation speichern
data["conversation"].append({"role": "user", "content": text})
data["conversation"].append({"role": "assistant", "content": response})
if len(data["conversation"]) > 20:
    data["conversation"] = data["conversation"][-20:]
save_data(data)

await update.message.reply_text(response)
```

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = str(update.effective_user.id)
user_name = USER_NAMES.get(user_id, “”)
name_str = f”, {user_name}” if user_name else “”
await update.message.reply_text(
f”👋 Hallo{name_str}! Ich bin euer gemeinsamer Assistent.\n\n”
“Schreib mir einfach oder schick mir eine 🎤 Sprachnachricht!\n\n”
“Ich kann:\n”
“📅 Termine anzeigen & erstellen\n”
“📝 Notizen speichern\n”
“✅ Aufgaben verwalten\n”
“💬 Fragen beantworten\n\n”
f”Deine Telegram-ID: `{user_id}`”
, parse_mode=“Markdown”
)

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
if not TELEGRAM_TOKEN:
print(“❌ TELEGRAM_TOKEN fehlt!”)
return
if not OPENAI_API_KEY:
print(“❌ OPENAI_API_KEY fehlt!”)
return

```
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("🤖 Agent läuft...")
app.run_polling()
```

if **name** == “**main**”:
main()