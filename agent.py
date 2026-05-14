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
        now_local=datetime.now(tz)
        current=max(day_start,now_local)
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

def process_calendar(response,data=None):
    if "KALENDER_TERMIN:" not in response:
        return response,None
    results=[]
    pending=None
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
                pending={"title":title,"dt":dt.isoformat().split("+")[0]}
            else:
                ok=add_calendar_event(title,dt,end_dt)
                if ok:
                    results.append("Termin eingetragen: "+title+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr"))
                else:
                    results.append("Fehler bei: "+title)
        except Exception as e:
            results.append("Fehler: "+str(e))
    if results:
        return "\n".join(results),pending
    return response,None


USERS = {
    "281391093": {"name":"Karsten","lang":"de","partner_id":"934428072"},
    "934428072": {"name":"Kate","lang":"en","partner_id":"281391093"}
}

INTENT_SYSTEM = """Du bist ein Intent-Erkennungs-System fuer einen persoenlichen Assistenten eines Paares.
Karsten (ID: 281391093) spricht Deutsch.
Kate (ID: 934428072) spricht Englisch.
Sie teilen einen gemeinsamen Google Kalender.

Analysiere die Nachricht und gib NUR ein JSON-Objekt zurueck, kein anderer Text.

Moegliche Intents:
- create_event: Termin erstellen
- query_events: Termine abfragen
- create_note: Notiz speichern
- create_todo: Aufgabe erstellen
- query_todos: Aufgaben abfragen
- delete_event: Termin loeschen
- general: Allgemeine Frage oder Konversation

Fuer "affects" entscheide wer betroffen ist:
- "self": nur die schreibende Person
- "partner": nur der Partner
- "both": beide

Beispiele:
- "remind me tomorrow" → affects: "self"
- "remind Kate about yoga" → affects: "partner"
- "we have dinner Saturday" → affects: "both"
- "my doctor appointment" → affects: "self"
- "Kate is teaching Thursday" → affects: "partner"

Format:
{
  "intent": "create_event",
  "title": "Titel des Termins",
  "date": "YYYY-MM-DD oder null",
  "time": "HH:MM oder null",
  "duration_hours": 1,
  "allday": false,
  "affects": "self",
  "persons": [],
  "location": null,
  "notes": null,
  "text": "Originalnachricht fuer allgemeine Antwort"
}"""

def detect_intent(user_message, user_id, context_data):
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    user_name=USER_NAMES.get(user_id,"")
    user_info=USERS.get(user_id,{})
    partner_id=user_info.get("partner_id","")
    partner_name=USER_NAMES.get(partner_id,"Partner")
    
    user_context = "\nSchreibende Person: " + user_name + " (ID: " + user_id + ")"
    user_context += "\nPartner: " + partner_name + " (ID: " + partner_id + ")"
    user_context += "\nSprache: " + user_info.get("lang","de")
    
    prompt = INTENT_SYSTEM + "\n\nAktuelle Zeit: " + now_str + user_context + "\n\nNachricht: " + user_message
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role":"user","content":prompt}],
        max_tokens=300,
        response_format={"type":"json_object"}
    )
    
    try:
        result = json.loads(response.choices[0].message.content)
        return result
    except:
        return {"intent":"general","text":user_message}

def execute_intent(intent_data, user_id, context_data):
    intent = intent_data.get("intent","general")
    affects = intent_data.get("affects","self")
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    user_name = USER_NAMES.get(user_id,"")
    user_info = USERS.get(user_id,{})
    partner_id = user_info.get("partner_id","")
    partner_name = USER_NAMES.get(partner_id,"Partner")
    
    # Determine who the action is for
    if affects == "partner":
        target_name = partner_name
        notify_id = partner_id
    elif affects == "both":
        target_name = user_name + " & " + partner_name
        notify_id = None
    else:
        target_name = user_name
        notify_id = None
    
    if intent == "create_event":
        title = intent_data.get("title","Termin")
        date_str = intent_data.get("date")
        time_str = intent_data.get("time","09:00")
        allday = intent_data.get("allday",False)
        duration = intent_data.get("duration_hours",1)
        
        if not date_str:
            return "Ich brauche noch ein Datum fuer den Termin. Wann soll er stattfinden?"
        
        if not time_str:
            time_str = "09:00"
        
        try:
            dt_str = date_str + " " + time_str
            dt = tz.localize(datetime.strptime(dt_str, "%Y-%m-%d %H:%M"))
            end_dt = dt + timedelta(hours=duration)
            
            conflicts = check_collision(dt, end_dt)
            if conflicts:
                free = find_free_slots(dt)
                msg = "Konflikt! '" + title + "' kollidiert mit: " + ", ".join(conflicts)
                if free:
                    msg += "\nFreie Slots: " + ", ".join(free)
                msg += "\nTrotzdem eintragen? Antworte mit JA."
                return msg, {"title":title,"dt":dt.isoformat().split("+")[0]}
            
            ok = add_calendar_event(title, dt, end_dt)
            if ok:
                return "Termin eingetragen: " + title + " am " + dt.strftime("%d.%m.%Y um %H:%M Uhr"), None
            else:
                return "Fehler beim Eintragen!", None
        except Exception as e:
            return "Fehler: " + str(e), None
    
    elif intent == "query_events":
        events = get_upcoming_events(14)
        if user_name:
            return "Hier sind eure naechsten Termine, " + user_name + ":\n" + events, None
        return "Hier sind eure naechsten Termine:\n" + events, None
    
    elif intent == "create_note":
        text = intent_data.get("text","")
        data_file = load_data()
        data_file["notes"].append({"text":text,"datum":now.strftime("%d.%m.%Y %H:%M")})
        save_data(data_file)
        return "Notiz gespeichert: " + text, None
    
    elif intent == "create_todo":
        text = intent_data.get("text","")
        data_file = load_data()
        data_file["todos"].append({"text":text,"erledigt":False,"datum":now.strftime("%d.%m.%Y %H:%M")})
        save_data(data_file)
        return "Aufgabe gespeichert: " + text, None
    
    elif intent == "query_todos":
        data_file = load_data()
        todos = data_file.get("todos",[])
        offen = [t for t in todos if not t["erledigt"]]
        if not offen:
            return "Keine offenen Aufgaben!", None
        out = "\n".join(["- " + t["text"] for t in offen])
        return "Offene Aufgaben:\n" + out, None
    
    else:
        return None, None

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

JA_WORDS=["JA","YES","J","Y","YEP","YA","JO","SURE","OK","OKAY","DO IT","MACH ES","EINTRAGEN","ADD IT","GO","JETZT","NOW"]
NEIN_WORDS=["NEIN","NO","CANCEL","ABBRECHEN","STOP","NOPE","NEE","NAH","NICHT","VERGISS ES","FORGET IT","SKIP","LASS ES"]

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
        if any(txt==w or txt.startswith(w) for w in JA_WORDS):
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
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("OK, Termin nicht eingetragen.")
            return
    await update.message.chat.send_action("typing")
    intent_data = detect_intent(text, user_id, data)
    intent = intent_data.get("intent","general")
    pending = None
    if intent in ["create_event","query_events","create_note","create_todo","query_todos"]:
        result = execute_intent(intent_data, user_id, data)
        if isinstance(result, tuple):
            response, pending = result
        else:
            response = result
        if response is None:
            response = await ask_gpt(text, user_id, data)
            response, pending = process_calendar(response, data)
    else:
        response = await ask_gpt(text, user_id, data)
        response, pending = process_calendar(response, data)
    if pending:
        data["pending_event"] = pending
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
    pending=data.get("pending_event",None)
    if pending:
        await update.message.chat.send_action("typing")
        voice=update.message.voice
        voice_file=await context.bot.get_file(voice.file_id)
        import tempfile as tf
        with tf.NamedTemporaryFile(suffix=".ogg",delete=False) as tmp:
            tmp_path=tmp.name
        await voice_file.download_to_drive(tmp_path)
        txt_voice=await transcribe_voice(tmp_path)
        os.unlink(tmp_path)
        txt_upper=txt_voice.strip().upper().replace("!","").replace(".","").strip()
        if any(txt_upper==w or txt_upper.startswith(w) for w in JA_WORDS):
            tz=pytz.timezone(TIMEZONE)
            dt=tz.localize(datetime.fromisoformat(pending["dt"]))
            end_dt=dt+timedelta(hours=1)
            ok=add_calendar_event(pending["title"],dt,end_dt)
            del data["pending_event"]
            save_data(data)
            msg="Termin eingetragen: "+pending["title"]+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if ok else "Fehler beim Eintragen!"
            await update.message.reply_text(msg)
            return
        elif any(txt_upper==w or txt_upper.startswith(w) for w in NEIN_WORDS):
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("OK, Termin nicht eingetragen.")
            return
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
    intent_data = detect_intent(text, user_id, data)
    intent = intent_data.get("intent","general")
    pending = None
    if intent in ["create_event","query_events","create_note","create_todo","query_todos"]:
        result = execute_intent(intent_data, user_id, data)
        if isinstance(result, tuple):
            response, pending = result
        else:
            response = result
        if response is None:
            response = await ask_gpt(text, user_id, data)
            response, pending = process_calendar(response, data)
    else:
        response = await ask_gpt(text, user_id, data)
        response, pending = process_calendar(response, data)
    if pending:
        data["pending_event"] = pending
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
    