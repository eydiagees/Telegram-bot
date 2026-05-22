import os,json,tempfile,logging,re
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

USERS={
    "281391093":{"name":"Karsten","lang":"de","partner_id":"934428072"},
    "934428072":{"name":"Kate","lang":"en","partner_id":"281391093"}
}

JA_WORDS=["JA","YES","J","Y","YEP","YA","JO","SURE","OK","OKAY","DO IT","MACH ES","EINTRAGEN","ADD IT","GO","JETZT","NOW"]
NEIN_WORDS=["NEIN","NO","CANCEL","ABBRECHEN","STOP","NOPE","NEE","NAH","NICHT","VERGISS ES","FORGET IT","SKIP","LASS ES"]
BRIEFING_KEYWORDS=["briefing","was steht an","mein tag","morning briefing","daily briefing","was haben wir heute","ueberblick","schick briefing"]

# ── Datenpersistenz ───────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {"notes":[],"todos":[],"conversation":[],"memory":{"persons":{},"patterns":{},"preferences":{}},"pending_event":None}

def save_data(data):
    with open(DATA_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

# ── Google Calendar ───────────────────────────────────────────────────────────
def get_calendar_service():
    try:
        creds=service_account.Credentials.from_service_account_file(GOOGLE_CREDS,scopes=["https://www.googleapis.com/auth/calendar"])
        return build("calendar","v3",credentials=creds)
    except Exception as e:
        logger.error(f"Cal error: {e}")
        return None

def check_collision(start_dt,end_dt):
    try:
        service=get_calendar_service()
        if not service:
            return []
        result=service.events().list(calendarId=CALENDAR_ID,timeMin=start_dt.isoformat(),timeMax=end_dt.isoformat(),singleEvents=True,orderBy="startTime").execute()
        return [e.get("summary","?") for e in result.get("items",[])]
    except:
        return []

def find_free_slots(dt,duration_hours=1):
    try:
        service=get_calendar_service()
        if not service:
            return []
        tz=pytz.timezone(TIMEZONE)
        day_start=tz.localize(datetime(dt.year,dt.month,dt.day,8,0))
        day_end=tz.localize(datetime(dt.year,dt.month,dt.day,20,0))
        result=service.events().list(calendarId=CALENDAR_ID,timeMin=day_start.isoformat(),timeMax=day_end.isoformat(),singleEvents=True,orderBy="startTime").execute()
        events=result.get("items",[])
        slots=[]
        now_local=datetime.now(tz)
        current=max(day_start,now_local)
        for e in events:
            e_start=datetime.fromisoformat(e["start"].get("dateTime",e["start"].get("date","")))
            if not e_start.tzinfo:
                e_start=tz.localize(e_start)
            if (e_start-current).total_seconds()/3600>=duration_hours:
                slots.append(current.strftime("%H:%M")+" - "+e_start.strftime("%H:%M"))
            e_end=datetime.fromisoformat(e["end"].get("dateTime",e["end"].get("date","")))
            if not e_end.tzinfo:
                e_end=tz.localize(e_end)
            current=max(current,e_end)
        if (day_end-current).total_seconds()/3600>=duration_hours:
            slots.append(current.strftime("%H:%M")+" - "+day_end.strftime("%H:%M"))
        return slots[:3]
    except:
        return []

def add_calendar_event(title,start_dt,end_dt=None):
    try:
        service=get_calendar_service()
        if not service:
            return False
        if end_dt is None:
            end_dt=start_dt+timedelta(hours=1)
        event={"summary":title,"start":{"dateTime":start_dt.isoformat(),"timeZone":TIMEZONE},"end":{"dateTime":end_dt.isoformat(),"timeZone":TIMEZONE}}
        service.events().insert(calendarId=CALENDAR_ID,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"Event error: {e}")
        return False

def add_allday_event(title,dt):
    try:
        service=get_calendar_service()
        if not service:
            return False
        event={"summary":title,"start":{"date":dt.strftime("%Y-%m-%d")},"end":{"date":(dt+timedelta(days=1)).strftime("%Y-%m-%d")}}
        service.events().insert(calendarId=CALENDAR_ID,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"Allday error: {e}")
        return False

def get_upcoming_events(days=7):
    try:
        service=get_calendar_service()
        if not service:
            return "Kalender nicht verfuegbar."
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz).isoformat()
        end=(datetime.now(tz)+timedelta(days=days)).isoformat()
        result=service.events().list(calendarId=CALENDAR_ID,timeMin=now,timeMax=end,maxResults=20,singleEvents=True,orderBy="startTime").execute()
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

# ── Gedaechtnis ───────────────────────────────────────────────────────────────
def get_memory_context():
    data=load_data()
    memory=data.get("memory",{})
    if not memory.get("persons") and not memory.get("patterns"):
        return ""
    ctx="\nBekannte Personen:\n"
    for name,info in memory.get("persons",{}).items():
        ctx+=f"- {name}: {info.get('relation','')} von {info.get('relevant_for','')}"
        if info.get("notes"):
            ctx+=f" ({info['notes']})"
        ctx+="\n"
    if memory.get("patterns"):
        ctx+="\nBekannte Muster:\n"
        for key,val in memory["patterns"].items():
            ctx+=f"- {key}: {val}\n"
    return ctx

def update_memory_from_message(user_message,user_id,gpt_response):
    try:
        user_name=USER_NAMES.get(user_id,"")
        extract_prompt="""Analysiere und extrahiere merkenswerte Informationen. Gib NUR JSON zurueck oder {}.
Format: {"persons":{"Name":{"relation":"...","relevant_for":"...","notes":"..."}},"patterns":{"beschreibung":"wann"},"preferences":{"key":"value"}}
Nutzer: """+user_name+"\nNachricht: "+user_message+"\nAntwort: "+gpt_response
        response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":extract_prompt}],max_tokens=300,response_format={"type":"json_object"})
        extracted=json.loads(response.choices[0].message.content)
        if not extracted:
            return
        data=load_data()
        memory=data.get("memory",{"persons":{},"patterns":{},"preferences":{}})
        for name,info in extracted.get("persons",{}).items():
            if name not in memory["persons"]:
                memory["persons"][name]=info
            else:
                memory["persons"][name].update(info)
        memory["patterns"].update(extracted.get("patterns",{}))
        memory["preferences"].update(extracted.get("preferences",{}))
        data["memory"]=memory
        save_data(data)
    except:
        pass

# ── Intent Engine ─────────────────────────────────────────────────────────────
INTENT_SYSTEM="""Du bist ein Intent-Erkennungs-System. Analysiere die Nachricht und gib NUR ein JSON-Array zurueck.
Karsten (ID: 281391093) spricht Deutsch. Kate (ID: 934428072) spricht Englisch.

Intents:
- create_event: Termin mit Uhrzeit
- create_task: Aufgabe/Todo ohne Uhrzeit -> ganztaegig im Kalender
- create_list: Einkaufsliste -> ganztaegig mit Emoji
- create_note: Kurze Notiz
- query_events: Termine abfragen
- general: Alles andere

Affects: "self"=nur Schreiber, "partner"=nur Partner, "both"=beide

Format (IMMER ein Array):
[
  {"intent":"create_event","title":"...","date":"YYYY-MM-DD","time":"HH:MM","duration_hours":1,"affects":"self"},
  {"intent":"create_task","title":"...","date":null,"affects":"self"},
  {"intent":"create_list","items":["item1","item2"],"affects":"self"},
  {"intent":"create_note","text":"...","affects":"self"},
  {"intent":"general","text":"..."}
]
Beispiel "Anzughose gerissen, Chili-Oel kaufen":
[{"intent":"create_task","title":"Anzughose Ersatz","date":null,"affects":"self"},{"intent":"create_list","items":["Chili-Oel"],"affects":"self"}]"""

def detect_intents(user_message,user_id,context_data):
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    user_name=USER_NAMES.get(user_id,"")
    user_info=USERS.get(user_id,{})
    partner_id=user_info.get("partner_id","")
    partner_name=USER_NAMES.get(partner_id,"Partner")
    prompt=INTENT_SYSTEM+"\n\nZeit: "+now_str+"\nSchreiber: "+user_name+"\nPartner: "+partner_name+"\nNachricht: "+user_message
    try:
        response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=500)
        text=response.choices[0].message.content.strip()
        match=re.search(r'\[.*\]',text,re.DOTALL)
        if match:
            return json.loads(match.group())
        return [{"intent":"general","text":user_message}]
    except:
        return [{"intent":"general","text":user_message}]

def detect_intent(user_message,user_id,context_data):
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    user_name=USER_NAMES.get(user_id,"")
    user_info=USERS.get(user_id,{})
    partner_id=user_info.get("partner_id","")
    partner_name=USER_NAMES.get(partner_id,"Partner")
    prompt=INTENT_SYSTEM+"\n\nZeit: "+now_str+"\nSchreiber: "+user_name+"\nPartner: "+partner_name+"\nNachricht: "+user_message
    try:
        response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=300,response_format={"type":"json_object"})
        return json.loads(response.choices[0].message.content)
    except:
        return {"intent":"general","text":user_message}

def execute_intent(intent_data,user_id,context_data):
    intent=intent_data.get("intent","general")
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    user_name=USER_NAMES.get(user_id,"")

    if intent=="create_event":
        title=intent_data.get("title","Termin")
        date_str=intent_data.get("date")
        time_str=intent_data.get("time","09:00")
        duration=intent_data.get("duration_hours",1)
        if not date_str:
            return "Ich brauche noch ein Datum. Wann soll der Termin stattfinden?",None
        if not time_str:
            time_str="09:00"
        try:
            dt=tz.localize(datetime.strptime(date_str+" "+time_str,"%Y-%m-%d %H:%M"))
            end_dt=dt+timedelta(hours=duration)
            conflicts=check_collision(dt,end_dt)
            if conflicts:
                free=find_free_slots(dt)
                msg="Konflikt! '"+title+"' kollidiert mit: "+", ".join(conflicts)
                if free:
                    msg+="\nFreie Slots: "+", ".join(free)
                msg+="\nTrotzdem eintragen? Antworte mit JA."
                return msg,{"title":title,"dt":dt.isoformat().split("+")[0]}
            ok=add_calendar_event(title,dt,end_dt)
            return ("Termin eingetragen: "+title+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if ok else "Fehler beim Eintragen!"),None
        except Exception as e:
            return "Fehler: "+str(e),None

    elif intent=="create_task":
        text=intent_data.get("text") or intent_data.get("title","Aufgabe")
        deadline=intent_data.get("date")
        try:
            if deadline:
                d=date.fromisoformat(deadline)
                dt=datetime(d.year,d.month,d.day)
            else:
                dt=now
            ok=add_allday_event("✅ "+text,dt)
            return ("Aufgabe eingetragen: ✅ "+text if ok else "Fehler!"),None
        except Exception as e:
            return "Fehler: "+str(e),None

    elif intent=="create_list":
        items=intent_data.get("items") or intent_data.get("text","Liste")
        items_str=", ".join(items) if isinstance(items,list) else str(items)
        ok=add_allday_event("🛒 "+items_str,now)
        return ("Liste eingetragen: 🛒 "+items_str if ok else "Fehler!"),None

    elif intent=="create_note":
        text=intent_data.get("text","")
        data=load_data()
        if "notes" not in data:
            data["notes"]=[]
        data["notes"].append({"text":text,"datum":now.strftime("%d.%m.%Y %H:%M")})
        save_data(data)
        return "Notiz gespeichert: "+text,None

    elif intent=="query_events":
        text_lower=intent_data.get("text","").lower()
        if any(w in text_lower for w in ["heute","today"]):
            days,label=1,"Heute"
        elif any(w in text_lower for w in ["morgen","tomorrow"]):
            days,label=2,"Morgen"
        else:
            days,label=7,"Naechste 7 Tage"
        return label+":\n"+get_upcoming_events(days),None

    return None,None

def process_calendar(response,data=None):
    if "KALENDER_TERMIN:" not in response:
        return response,None
    results=[]
    pending=None
    for line in response.strip().split("\n"):
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
                dt=tz.localize(datetime.fromisoformat(dt_str[:16]))
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
                results.append("Termin eingetragen: "+title+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if ok else "Fehler bei: "+title)
        except Exception as e:
            results.append("Fehler: "+str(e))
    return ("\n".join(results) if results else response),pending

async def ask_gpt(user_message,user_id,context_data):
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    user_name=USER_NAMES.get(user_id,"")
    notes="\n".join([f"- {n['text']}" for n in context_data.get("notes",[])[-5:]])
    todos="\n".join([f"- {t['text']}" for t in context_data.get("todos",[])[-5:]])
    events=get_upcoming_events()
    system=("Du bist ein KI-Assistent fuer ein Paar mit ADHS. Antworte in der Sprache des Nutzers. Kurz und klar, max 3-4 Saetze. "
        "Zeit: "+now_str+" Nutzer: "+user_name+"\nTermine:\n"+events+"\nNotizen:\n"+(notes if notes else "Keine")+
        "\nAufgaben:\n"+(todos if todos else "Keine")+get_memory_context()+
        "\nWICHTIG: Wenn jemand Termine erstellen will, gib JEDEN Termin in einer eigenen Zeile aus. "
        "Format: KALENDER_TERMIN:Titel|YYYY-MM-DD HH:MM\nKeine anderen Texte wenn Termine erstellt werden!")
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

# ── Morgen-Briefing ───────────────────────────────────────────────────────────
async def generate_personal_briefing(user_id,user_name,lang="de"):
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    weekdays_de=["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    weekdays_en=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    weekday=weekdays_de[now.weekday()] if lang=="de" else weekdays_en[now.weekday()]
    events=get_upcoming_events(2)
    memory_ctx=get_memory_context()
    if lang=="de":
        prompt=("Erstelle ein kurzes persoenliches Morgen-Briefing auf Deutsch fuer "+user_name+" (ADHS).\n"
            "Heute: "+weekday+" "+now.strftime("%d.%m.%Y")+"\nAlle Termine:\n"+events+"\n"+memory_ctx+
            "\nWICHTIG: Unterscheide klar zwischen:\n"
            "- "+user_name+"s eigene Termine/Aufgaben (✅ und Termine ohne Emoji die fuer ihn sind)\n"
            "- Gemeinsame Termine (beide betroffen)\n"
            "- Partner-Termine die "+user_name+" kennen sollte zur Koordination\n"
            "Aufgaben mit ✅ die fuer den Partner sind NUR kurz erwaehnen zur Info.\n"
            "Briefing soll:\n- Mit 'Guten Morgen "+user_name+"!' beginnen\n"
            "- Eigene Termine zuerst\n- Gemeinsame Termine\n"
            "- Partner-Info kurz\n- Offene eigene Aufgaben\n- Max 8 Zeilen")
    else:
        prompt=("Create a short personal morning briefing in English for "+user_name+" (ADHD).\n"
            "Today: "+weekday+" "+now.strftime("%d.%m.%Y")+"\nAll events:\n"+events+"\n"+memory_ctx+
            "\nIMPORTANT: Clearly distinguish between:\n"
            "- "+user_name+"'s own events/tasks (her Reformer classes, her tasks)\n"
            "- Shared events (both affected)\n"
            "- Partner's events "+user_name+" should know for coordination\n"
            "Tasks with ✅ that belong to the partner should only be briefly mentioned.\n"
            "Briefing should:\n- Start with 'Good morning "+user_name+"!'\n"
            "- Own events first\n- Shared events\n"
            "- Partner info briefly\n- Open own tasks\n- Max 8 lines")
    response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=300)
    return response.choices[0].message.content

async def generate_briefing(bot,target_user_id=None):
    user_configs=[
        {"id":"281391093","name":"Karsten","lang":"de"},
        {"id":"934428072","name":"Kate","lang":"en"},
    ]
    for user in user_configs:
        if target_user_id and user["id"]!=target_user_id:
            continue
        try:
            briefing=await generate_personal_briefing(user["id"],user["name"],user["lang"])
            await bot.send_message(chat_id=int(user["id"]),text=briefing)
        except Exception as e:
            logger.error(f"Briefing error for {user['name']}: {e}")

async def evening_checkin(bot):
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    service=get_calendar_service()
    if not service:
        return
    day_start=tz.localize(datetime(now.year,now.month,now.day,0,0))
    day_end=tz.localize(datetime(now.year,now.month,now.day,23,59))
    result=service.events().list(calendarId=CALENDAR_ID,timeMin=day_start.isoformat(),timeMax=day_end.isoformat(),singleEvents=True,orderBy="startTime").execute()
    tasks=[e for e in result.get("items",[]) if e.get("summary","").startswith("✅")]
    if not tasks:
        return
    msg="Guten Abend! 🌙 Kurzes Check-in:\n\n"
    for i,t in enumerate(tasks,1):
        msg+=f"{i}. {t.get('summary','').replace('✅ ','')}\n"
    msg+="\nWas habt ihr erledigt? z.B. '1,3' oder 'alles' oder 'nichts'"
    data=load_data()
    chat_id=data.get("chat_id")
    if chat_id:
        try:
            await bot.send_message(chat_id=int(chat_id),text=msg)
        except:
            pass

async def analyze_notes(bot):
    data=load_data()
    notes=data.get("notes",[])
    if not notes:
        return
    unprocessed=[n for n in notes if not n.get("processed")]
    if not unprocessed:
        return
    notes_text="\n".join([f"- {n['datum']}: {n['text']}" for n in unprocessed])
    prompt="""Analysiere diese Notizen und leite daraus Aufgaben ab. Gib NUR JSON Array zurueck.
Format: [{"type":"task","title":"...","date":"YYYY-MM-DD oder null"},{"type":"info","text":"..."}]
Notizen:
"""+notes_text
    try:
        response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=400,response_format={"type":"json_object"})
        result=json.loads(response.choices[0].message.content)
        actions=result if isinstance(result,list) else result.get("actions",[])
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz)
        added=[]
        for action in actions:
            if action.get("type")=="task":
                title=action.get("title","")
                date_str=action.get("date")
                if date_str:
                    try:
                        d=date.fromisoformat(date_str)
                        dt=datetime(d.year,d.month,d.day)
                    except:
                        dt=now
                else:
                    dt=now
                ok=add_allday_event("✅ "+title,dt)
                if ok:
                    added.append(title)
        for n in unprocessed:
            n["processed"]=True
        data["notes"]=notes
        save_data(data)
        if added:
            msg="📋 Aus euren Notizen habe ich folgende Aufgaben abgeleitet:\n"
            msg+="\n".join(["- ✅ "+a for a in added])
            data2=load_data()
            chat_id=data2.get("chat_id")
            if chat_id:
                await bot.send_message(chat_id=int(chat_id),text=msg)
    except Exception as e:
        logger.error(f"analyze_notes error: {e}")

async def scheduler(bot):
    import asyncio
    tz=pytz.timezone(TIMEZONE)
    while True:
        now=datetime.now(tz)
        if now.hour==6 and now.minute==50:
            await analyze_notes(bot)
            await asyncio.sleep(61)
        elif now.hour==7 and now.minute==0:
            await generate_briefing(bot)
            await asyncio.sleep(61)
        elif now.hour==20 and now.minute==0:
            await evening_checkin(bot)
            await asyncio.sleep(61)
        else:
            await asyncio.sleep(30)

def is_allowed(update):
    return True

# ── Handler ───────────────────────────────────────────────────────────────────
async def handle_text(update,context):
    if not is_allowed(update):
        await update.message.reply_text("Kein Zugriff.")
        return
    user_id=str(update.effective_user.id)
    text=update.message.text
    data=load_data()
    chat_id=str(update.effective_chat.id)
    if data.get("chat_id")!=chat_id:
        data["chat_id"]=chat_id
        save_data(data)
    pending=data.get("pending_event")
    if pending:
        txt=text.strip().upper().replace("!","").replace(".","").strip()
        if any(txt==w or txt.startswith(w) for w in JA_WORDS):
            tz=pytz.timezone(TIMEZONE)
            dt=tz.localize(datetime.fromisoformat(pending["dt"]))
            ok=add_calendar_event(pending["title"],dt,dt+timedelta(hours=1))
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("Termin eingetragen: "+pending["title"]+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if ok else "Fehler!")
            return
        elif ":" in text:
            try:
                time_match=re.search(r"(\d{1,2}:\d{2})",text)
                if time_match:
                    old_dt=datetime.fromisoformat(pending["dt"])
                    h,m=time_match.group(1).split(":")
                    new_dt=old_dt.replace(hour=int(h),minute=int(m))
                    tz=pytz.timezone(TIMEZONE)
                    new_dt=tz.localize(new_dt)
                    ok=add_calendar_event(pending["title"],new_dt,new_dt+timedelta(hours=1))
                    del data["pending_event"]
                    save_data(data)
                    await update.message.reply_text("Termin eingetragen: "+pending["title"]+" am "+new_dt.strftime("%d.%m.%Y um %H:%M Uhr") if ok else "Fehler!")
                    return
            except:
                pass
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("OK, Termin nicht eingetragen.")
            return
    if any(kw in text.lower() for kw in BRIEFING_KEYWORDS):
        await generate_briefing(context.bot,target_user_id=user_id)
        return
    await update.message.chat.send_action("typing")
    intents=detect_intents(text,user_id,data)
    results=[]
    pending=None
    has_action=any(i.get("intent") in ["create_event","create_task","create_list","create_note","query_events"] for i in intents)
    if has_action:
        for intent_data in intents:
            intent=intent_data.get("intent","general")
            if intent in ["create_event","create_task","create_list","create_note","query_events"]:
                result=execute_intent(intent_data,user_id,data)
                if isinstance(result,tuple):
                    r,p=result
                    if p:
                        pending=p
                else:
                    r=result
                if r:
                    results.append(r)
        response="\n".join(results) if results else None
        if not response:
            response=await ask_gpt(text,user_id,data)
            response,pending=process_calendar(response,data)
    else:
        response=await ask_gpt(text,user_id,data)
        response,pending=process_calendar(response,data)
    if pending:
        data["pending_event"]=pending
    update_memory_from_message(text,user_id,response)
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
    pending=data.get("pending_event")
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
    if pending:
        txt=text.strip().upper().replace("!","").replace(".","").strip()
        if any(txt==w or txt.startswith(w) for w in JA_WORDS):
            tz=pytz.timezone(TIMEZONE)
            dt=tz.localize(datetime.fromisoformat(pending["dt"]))
            ok=add_calendar_event(pending["title"],dt,dt+timedelta(hours=1))
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("Termin eingetragen: "+pending["title"]+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if ok else "Fehler!")
            return
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            del data["pending_event"]
            save_data(data)
            await update.message.reply_text("OK, Termin nicht eingetragen.")
            return
    if any(kw in text.lower() for kw in BRIEFING_KEYWORDS):
        await generate_briefing(context.bot,target_user_id=user_id)
        return
    intent_data=detect_intent(text,user_id,data)
    intent=intent_data.get("intent","general")
    pending=None
    if intent in ["create_event","create_task","create_list","create_note","query_events"]:
        result=execute_intent(intent_data,user_id,data)
        if isinstance(result,tuple):
            response,pending=result
        else:
            response=result
        if response is None:
            response=await ask_gpt(text,user_id,data)
            response,pending=process_calendar(response,data)
    else:
        response=await ask_gpt(text,user_id,data)
        response,pending=process_calendar(response,data)
    if pending:
        data["pending_event"]=pending
    update_memory_from_message(text,user_id,response)
    data["conversation"].append({"role":"user","content":text})
    data["conversation"].append({"role":"assistant","content":response})
    if len(data["conversation"])>20:
        data["conversation"]=data["conversation"][-20:]
    save_data(data)
    await update.message.reply_text(response)


async def handle_photo(update,context):
    if not is_allowed(update):
        await update.message.reply_text("Kein Zugriff.")
        return
    user_id=str(update.effective_user.id)
    data=load_data()
    await update.message.chat.send_action("typing")
    photo=update.message.photo[-1]
    photo_file=await context.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg",delete=False) as tmp:
        tmp_path=tmp.name
    await photo_file.download_to_drive(tmp_path)
    with open(tmp_path,"rb") as f:
        import base64
        image_data=base64.b64encode(f.read()).decode("utf-8")
    os.unlink(tmp_path)
    caption=update.message.caption or ""
    user_name=USER_NAMES.get(user_id,"")
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    system_prompt=(
        "Du bist ein KI-Assistent fuer ein Paar mit ADHS. "
        "Analysiere dieses Bild und extrahiere alle relevanten Informationen. "
        "Zeit: "+now_str+" Nutzer: "+user_name+"\n"
        "Suche nach: Terminen, Aufgaben, Einkaufslistens, wichtigen Infos.\n"
        "Gib eine kurze Zusammenfassung und frage ob du Aktionen durchfuehren sollst.\n"
        "Antworte in der Sprache des Nutzers."
    )
    if caption:
        system_prompt+="\nNutzer-Kommentar zum Bild: "+caption
    try:
        response=client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role":"user",
                "content":[
                    {"type":"text","text":system_prompt},
                    {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+image_data}}
                ]
            }],
            max_tokens=500
        )
        reply=response.choices[0].message.content
        update_memory_from_message("Foto analysiert: "+reply,user_id,reply)
        data["conversation"].append({"role":"user","content":"[Foto] "+caption})
        data["conversation"].append({"role":"assistant","content":reply})
        if len(data["conversation"])>20:
            data["conversation"]=data["conversation"][-20:]
        save_data(data)
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("Fehler beim Analysieren des Fotos: "+str(e))

async def start(update,context):
    user_id=str(update.effective_user.id)
    user_name=USER_NAMES.get(user_id,"")
    name_str=", "+user_name if user_name else ""
    await update.message.reply_text("Hallo"+name_str+"! Ich bin euer Assistent.\nDeine ID: "+user_id+"\nSchreib mir oder schick eine Sprachnachricht!")

def main():
    import asyncio
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    async def post_init(application):
        asyncio.create_task(scheduler(application.bot))
    app.post_init=post_init
    app.add_handler(CommandHandler("start",start))
    app.add_handler(MessageHandler(filters.VOICE,handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    print("Agent laeuft!")
    app.run_polling()

if __name__=="__main__":
    main()