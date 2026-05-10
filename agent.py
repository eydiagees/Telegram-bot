#!/usr/bin/env python3
“””
Telegram AI Agent – Sprache, Google Kalender & Notizen
Für zwei Nutzer (DE + EN), Google Calendar, GPT-4 + Whisper
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
from googleapiclient.discovery import build
from google.oauth2 import service_account
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(**name**)

TELEGRAM_TOKEN  = os.environ.get(“TELEGRAM_TOKEN”)
OPENAI_API_KEY  = os.environ.get(“OPENAI_API_KEY”)
GOOGLE_CREDS    = os.environ.get(“GOOGLE_CREDS”, “/root/telegram-agent-495819-78c6f6fa86cd.json”)
CALENDAR_ID     = os.environ.get(“CALENDAR_ID”, “primary”)
ALLOWED_USERS   = os.environ.get(“ALLOWED_USERS”, “”).split(”,”)
TIMEZONE        = os.environ.get(“TIMEZONE”, “Europe/Vienna”)

USER_NAMES = {}
for entry in os.environ.get(“USER_NAMES”, “”).split(”,”):
if “:” in entry:
uid, name = entry.split(”:”, 1)
USER_NAMES[uid.strip()] = name.strip()

client = OpenAI(api_key=OPENAI_API_KEY)

DATA_FILE = “agent_data.json”

def load_data():
if os.path.exists(DATA_FILE):
with open(DATA_FILE, “r”, encoding=“utf-8”) as f:
return json.load(f)
return {“notes”: [], “todos”: [], “conversation”: []}

def save_data(data):
with open(DATA_FILE, “w”, encoding=“utf-8”) as f:
json.dump(data, f, ensure_ascii=False, indent=2)

def get_calendar_service():
try:
creds = service_account.Credentials.from_service_account_file(
GOOGLE_CREDS,
scopes=[“https://www.googleapis.com/auth/calendar”]
)
service = build(“calendar”, “v3”, credentials=creds)
return service
except Exception as e:
logger.error(f”Google Calendar Fehler: {e}”)
return None

def get_upcoming_events(days=7):
try:
service = get_calendar_service()
if not service:
return “Kalender nicht verfügbar.”
tz = pytz.timezone(TIMEZONE)
now = datetime.now(tz).isoformat()
end = (datetime.now(tz) + timedelta(days=days)).isoformat()
events_result = service.events().list(
calendarId=CALENDAR_ID,
timeMin=now,
timeMax=end,
maxResults=20,
singleEvents=True,
orderBy=“startTime”
).execute()
events = events_result.get(“items”, [])
if not events:
return f”Keine Termine in den naechsten {days} Tagen.”
result = []
for event in events:
start = event[“start”].get(“dateTime”, event[“start”].get(“date”, “”))
try:
dt = datetime.fromisoformat(start)
date_str = dt.strftime(”%d.%m.%Y %H:%M”)
except:
date_str = start
summary = event.get(“summary”, “Kein Titel”)
result.append(f”- {date_str}: {summary}”)
return “\n”.join(result)
except Exception as e:
return f”Fehler: {e}”

def add_calendar_event(title, start_dt, end_dt=None):
try:
service = get_calendar_service()
if not service:
return False
if end_dt is None:
end_dt = start_dt + timedelta(hours=1)
event = {
“summary”: title,
“start”: {“dateTime”: start_dt.isoformat(), “timeZone”: TIMEZONE},
“end”:   {“dateTime”: end_dt.isoformat(),   “timeZone”: TIMEZONE},
}
service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
return True
except Exception as e:
logger.error(f”Event-Fehler: {e}”)
return False

SYSTEM_PROMPT = “”“Du bist ein persönlicher KI-Assistent für ein Paar.
Der Mann spricht Deutsch, die Frau spricht Englisch.

Regeln:

- Antworte in der Sprache des Nutzers
- Kurz und klar (beide haben ADHS) - max 3-4 Saetze
- Bei Terminen immer Datum und Uhrzeit bestaetigen
- Emojis sparsam einsetzen
  “””

async def ask_gpt(user_message: str, user_id: str, context_data: dict) -> str:
user_name = USER_NAMES.get(user_id, “”)
tz = pytz.timezone(TIMEZONE)
now_str = datetime.now(tz).strftime(”%d.%m.%Y %H:%M”)
notes_text = “\n”.join([f”- {n[‘text’]} ({n[‘datum’]})” for n in context_data.get(“notes”, [])[-5:]])
todos_text = “\n”.join([f”- {‘erledigt’ if t[‘erledigt’] else ‘offen’}: {t[‘text’]}” for t in context_data.get(“todos”, [])[-5:]])
events_text = get_upcoming_events()

```
context_msg = f"""
```

Nutzer: {user_name if user_name else user_id}
Zeit: {now_str} ({TIMEZONE})
Termine:\n{events_text}
Notizen:\n{notes_text if notes_text else ‘Keine’}
Aufgaben:\n{todos_text if todos_text else ‘Keine’}
“””

```
messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context_msg}]
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

async def transcribe_voice(file_path: str) -> str:
with open(file_path, “rb”) as audio_file:
transcript = client.audio.transcriptions.create(
model=“whisper-1”,
file=audio_file
)
return transcript.text

def is_allowed(update: Update) -> bool:
if not ALLOWED_USERS or ALLOWED_USERS == [””]:
return True
return str(update.effective_user.id) in ALLOWED_USERS

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_allowed(update):
await update.message.reply_text(“Kein Zugriff.”)
return
user_id = str(update.effective_user.id)
text = update.message.text
data = load_data()
await update.message.chat.send_action(“typing”)
response = await ask_gpt(text, user_id, data)
data[“conversation”].append({“role”: “user”, “content”: text})
data[“conversation”].append({“role”: “assistant”, “content”: response})
if len(data[“conversation”]) > 20:
data[“conversation”] = data[“conversation”][-20:]
save_data(data)
await update.message.reply_text(response)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_allowed(update):
await update.message.reply_text(“Kein Zugriff.”)
return
user_id = str(update.effective_user.id)
data = load_data()
await update.message.chat.send_action(“typing”)
voice = update.message.voice
voice_file = await context.bot.get_file(voice.file_id)
with tempfile.NamedTemporaryFile(suffix=”.ogg”, delete=False) as tmp:
tmp_path = tmp.name
await voice_file.download_to_drive(tmp_path)
text = await transcribe_voice(tmp_path)
os.unlink(tmp_path)
user_name = USER_NAMES.get(user_id, “”)
await update.message.reply_text(f”Gehoert: {user_name + ’: ’ if user_name else ‘’}{text}”)
response = await ask_gpt(text, user_id, data)
data[“conversation”].append({“role”: “user”, “content”: text})
data[“conversation”].append({“role”: “assistant”, “content”: response})
if len(data[“conversation”]) > 20:
data[“conversation”] = data[“conversation”][-20:]
save_data(data)
await update.message.reply_text(response)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = str(update.effective_user.id)
user_name = USER_NAMES.get(user_id, “”)
name_str = f”, {user_name}” if user_name else “”
await update.message.reply_text(
f”Hallo{name_str}! Ich bin euer Assistent.\n\n”
“Schreib mir oder schick eine Sprachnachricht!\n\n”
“Ich kann:\n”
“- Termine anzeigen und erstellen\n”
“- Notizen speichern\n”
“- Aufgaben verwalten\n”
“- Fragen beantworten\n\n”
f”Deine Telegram-ID: {user_id}”
)

def main():
if not TELEGRAM_TOKEN:
print(“TELEGRAM_TOKEN fehlt!”)
return
if not OPENAI_API_KEY:
print(“OPENAI_API_KEY fehlt!”)
return
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“start”, start))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
print(“Agent laeuft…”)
app.run_polling()

if **name** == “**main**”:
main()