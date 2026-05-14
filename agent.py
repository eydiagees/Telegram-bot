import os,json,tempfile,logging
from datetime import datetime,timedelta,date
from telegram import Update
from telegram.ext import ApplicationBuilder,MessageHandler,CommandHandler,ContextTypes,filters
from openai import OpenAI
from googleapiclient.discovery import build
from google.oauth2 import service_account
import pytz

logging.basicConfig(level=logging.INFO)
logger=logging.getLogger(__name__)

TELEGRAM_TOKEN=os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY=os.environ.get("OPENAI_API_KEY")
GOOGLE_CREDS=os.environ.get("GOOGLE_CREDS","/root/telegram-agent-495819-78c6f6fa86cd.json")
CALENDAR_ID=os.environ.get("CALENDAR_ID","primary")
ALLOWED_USERS=os.environ.get("ALLOWED_USERS","").split(",")
TIMEZONE=os.environ.get("TIMEZONE","Europe/Vienna")

USER_NAMES={}
for entry in os.environ.get("USER_NAMES","").split(","):
    if ":" in entry:
        uid,name=entry.split(":",1)
        USER_NAMES[uid.strip()]=name.strip()

client=OpenAI(api_key=OPENAI_API_KEY)
DATA_FILE="agent_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {"notes":[],"todos":[],"conversation":[]}

def save_data(data):
    with open(DATA_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

def get_calendar_service():
    try:
        creds=service_account.Credentials.from_service_account_file(
            GOOGLE_CREDS,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar","v3",credentials=creds)
    except Exception as e:
        logger.error(f"Cal error: {e}")
        return None

def check_collision(start_dt,end_dt):
    try:
        service=get_calendar_service()
        if not service:
            return []
        result=service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events=result.get("items",[])
        return [e.get("summary","?") for e in events]
    except Exception as e:
        return []

def find_free_slots(dt,duration_hours=1):
    try:
        service=get_calendar_service()
        if not service:
            return []
        tz=pytz.timezone(TIMEZONE)
        day_start=tz.localize(datetime(dt.year,dt.month,dt.day,8,0))
        day_end=tz.localize(datetime(dt.year,dt.month,dt.day,20,0))
        result=service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events=result.get("items",[])
        slots=[]
        current=day_start
        for e in events:
            e_start=datetime.fromisoformat(e["start"].get("dateTime",e["start"].get("date","")))
            if not e_start.tzinfo:
                e_start=tz.localize(e_start)
            gap=(e_start-current).total_seconds()/3600
            if gap>=duration_hours:
                slots.append(current.strftime("%H:%M")+" - "+e_start.strftime("%H:%M"))
            e_end=datetime.fromisoformat(e["end"].get("dateTime",e["end"].get("date","")))
            if not e_end.tzinfo:
                e_end=tz.localize(e_end)
            current=max(current,e_end)
        gap=(day_end-current).total_seconds()/3600
        if gap>=duration_hours:
            slots.append(current.strftime("%H:%M")+" - "+day_end.strftime("%H:%M"))
        return slots[:3]
    except Exception as e:
        return []

def add_calendar_event(title,start_dt,end_dt=None):
    try:
        service=get_calendar_service()
        if not service:
            return False
        if end_dt is None:
            end_dt=start_dt+timedelta(hours=1)
        event={
            "summary":title,
            "start":{"dateTime":start_dt.isoformat(),"timeZone":TIMEZONE},
            "end":{"dateTime":end_dt.isoformat(),"timeZone":TIMEZONE}
        }
        service.events().insert(calendarId=CALENDAR_ID,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"Event error: {e}")
        return False

def get_upcoming_events(days=7):
    try:
        service=get_calendar_service()
        if not service:
            return "Kalender nicht verfuegbar."
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz).isoformat()
        end=(datetime.now(tz)+timedelta(days=days)).isoformat()
        result=service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now,
            timeMax=end,
            maxResults=20,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events=result.get("items",[])
        if not events:
            return "Keine Termine."
        out=[]
        for e in events:
            start=e["start"].get("dateTime",e["start"].get("date",""))
            try:
                dt=datetime.fromisoformat(start)
                ds=dt.strftime("%d.%m.%Y %H:%M")
            except:
                ds=start
            out.append(f"- {ds}: {e.get('summary','?')}")
        return "\n".join(out)
    except Exception as e:
        return f"Fehler: {e}"

def process_calendar(response):
    if "KALENDER_TERMIN:" not in response:
        return response
    results=[]
    lines=response.strip().split("\n")
    for line in lines:
        line=line.strip()
        if not line.startswith("KALENDER_TERMIN:"):
            continue
        try:
            parts=line.replace("KALENDER_TERMIN:","").split("|")
            title=parts[0].strip()
            dt_str=parts[1].strip()
            tz=pytz.timezone(TIMEZONE)
            if len(dt_str)==10:
                d=date.fromisoformat(dt_str)
                dt=tz.localize(datetime(d.year,d.month,d.day,9,0))
            else:
                dt_str=dt_str[:16]
                dt=tz.localize(datetime.fromisoformat(dt_str))
            end_dt=dt+timedelta(hours=1)
            conflicts=check_collision(dt,end_dt)
            if conflicts:
                free=find_free_slots(dt)
                msg="Konflikt! '"+title+"' kollidiert mit: "+", ".join(conflicts)
                if free:
                    msg+="\nFreie Slots: "+", ".join(free)
                msg+="\nTrotzdem eintragen? Antworte mit JA."
                results.append(msg)
            else:
                ok=add_calendar_event(title,dt,end_dt)
                if ok:
                    results.append("Termin eingetragen: "+title+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr"))
                else:
                    results.append("Fehler bei: "+title)
        except Exception as e:
            results.append("Fehler: "+str(e))
    if results:
        return "\n".join(results)
    return response

async def ask_gpt(user_message,user_id,context_data):
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    user_name=USER_NAMES.get(user_id,"")
    notes="\n".join([f"- {n['text']}" for n in context_data.get("notes",[])[-5:]])
    todos="\n".join([f"- {t['text']}" for t in context_data.get("todos",[])[-5:]])
    events=get_upcoming_events()
    system=(
        "Du bist ein KI-Assistent fuer ein Paar mit ADHS. "
        "Antworte in der Sprache des Nutzers. Kurz und klar, max 3-4 Saetze. "
        "Zeit: "+now_str+" Nutzer: "+user_name+"\n"
        "Termine:\n"+events+"\n"
        "Notizen:\n"+(notes if notes else "Keine")+"\n"
        "Aufgaben:\n"+(todos if todos else "Keine")+"\n"
        "WICHTIG: Wenn jemand Termine erstellen will, gib JEDEN Termin in einer eigenen Zeile aus. "
        "Format fuer JEDEN Termin: KALENDER_TERMIN:Titel|YYYY-MM-DD HH:MM\n"
        "Beispiel:\nKALENDER_TERMIN:Reformer Elevated|2026-05-14 10:00\n"
        "KALENDER_TERMIN:Reformer Method|2026-05-14 11:15\n"
        "Keine anderen Texte wenn Termine erstellt werden!"
    )
    messages=[{"role":"system","content":system}]
    for msg in context_data.get("conversation",[])[-10:]:
        messages.append(msg)
    messages.append({"role":"user","content":user_message})
    response=client.chat.completions.create(model="gpt-4o",messages=messages,max_tokens=500)
    return response.choices[0].message.content

async def transcribe_voice(file_path):
    with open(file_path,"rb") as f:
        t=client.audio.transcriptions.create(model="whisper-1",file=f)
    return t.text

def is_allowed(update):
    return True

async def handle_text(update,context):
    if not is_allowed(update):
        await update.message.reply_text("Kein Zugriff.")
        return
    user_id=str(update.effective_user.id)
    text=update.message.text
    data=load_data()
    pending=data.get("pending_event",None)
    if pending:
        txt=text.strip().upper()
        if txt=="JA":
            tz=pytz.timezone(TIMEZONE)
            dt=tz.localize(datetime.fromisoformat(pending["dt"]))
            end_dt=dt+timedelta(hours=1)
            ok=add_calendar_event(pending["title"],dt,end_dt)
            del data["pending_event"]
            save_data(data)
            if ok:
                await update.message.reply_text("Termin eingetragen: "+pending["title"]+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr"))
            else:
                await update.message.reply_text("Fehler beim Eintragen!")
            return
        elif ":" in text:
            try:
                import re
                time_match=re.search(r"(\d{1,2}:\d{2})",text)
                if time_match:
                    old_dt=datetime.fromisoformat(pending["dt"])
                    new_time=time_match.group(1).split(":")
                    new_dt=old_dt.replace(hour=int(new_time[0]),minute=int(new_time[1]))
                    tz=pytz.timezone(TIMEZONE)
                    new_dt=tz.localize(new_dt)
                    end_dt=new_dt+timedelta(hours=1)
                    ok=add_calendar_event(pending["title"],new_dt,end_dt)
                    del data["pending_event"]
                    save_data(data)
                    if ok:
                        await update.message.reply_text("Termin eingetragen: "+pending["title"]+" am "+new_dt.strftime("%d.%m.%Y um %H:%M Uhr"))
                    else:
                        await update.message.reply_text("Fehler beim Eintragen!")
                    return
            except:
                pass
        elif txt in ["NEIN","NO","CANCEL","ABBRECHEN"]:
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("OK, Termin nicht eingetragen.")
            return
    await update.message.chat.send_action("typing")
    response=await ask_gpt(text,user_id,data)
    if "Konflikt!" in response and "Trotzdem eintragen?" in response:
        import re
        title_match=re.search(r"Konflikt! '(.+?)'",response)
        dt_match=data.get("last_event_dt",None)
        if title_match and dt_match:
            data["pending_event"]={"title":title_match.group(1),"dt":dt_match}
    response=process_calendar(response)
    if "pending_event" not in data and "KALENDER_TERMIN:" in response:
        pass
    data["conversation"].append({"role":"user","content":text})
    data["conversation"].append({"role":"assistant","content":response})
    if len(data["conversation"])>20:
        data["conversation"]=data["conversation"][-20:]
    save_data(data)
    await update.message.reply_text(response)

async def handle_voice(update,context):
    if not is_allowed(update):
        await update.message.reply_text("Kein Zugriff.")
        return
    user_id=str(update.effective_user.id)
    data=load_data()
    await update.message.chat.send_action("typing")
    voice=update.message.voice
    voice_file=await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg",delete=False) as tmp:
        tmp_path=tmp.name
    await voice_file.download_to_drive(tmp_path)
    text=await transcribe_voice(tmp_path)
    os.unlink(tmp_path)
    user_name=USER_NAMES.get(user_id,"")
    await update.message.reply_text("Gehoert: "+(user_name+": " if user_name else "")+text)
    response=await ask_gpt(text,user_id,data)
    response=process_calendar(response)
    data["conversation"].append({"role":"user","content":text})
    data["conversation"].append({"role":"assistant","content":response})
    if len(data["conversation"])>20:
        data["conversation"]=data["conversation"][-20:]
    save_data(data)
    await update.message.reply_text(response)

async def start(update,context):
    user_id=str(update.effective_user.id)
    user_name=USER_NAMES.get(user_id,"")
    name_str=", "+user_name if user_name else ""
    await update.message.reply_text(
        "Hallo"+name_str+"! Ich bin euer Assistent.\n"
        "Deine ID: "+user_id+"\n"
        "Schreib mir oder schick eine Sprachnachricht!"
    )

def main():
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(MessageHandler(filters.VOICE,handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    print("Agent laeuft!")
    app.run_polling()

if __name__=="__main__":
    main()