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
TIMEZONE=os.environ.get("TIMEZONE","Europe/Vienna")

USER_NAMES={}
for entry in os.environ.get("USER_NAMES","").split(","):
    if ":" in entry:
        uid,name=entry.split(":",1)
        USER_NAMES[uid.strip()]=name.strip()

client=OpenAI(api_key=OPENAI_API_KEY)
DATA_FILE="agent_data.json"
IMPROVEMENTS_FILE="improvements.json"

USERS={
    "281391093":{"name":"Karsten","lang":"de","partner_id":"934428072"},
    "934428072":{"name":"Kate","lang":"en","partner_id":"281391093"}
}
JA_WORDS=["JA","YES","J","Y","YEP","YA","JO","SURE","OK","OKAY","DO IT","MACH ES","EINTRAGEN","ADD IT","GO","JETZT","NOW"]
NEIN_WORDS=["NEIN","NO","CANCEL","ABBRECHEN","STOP","NOPE","NEE","NAH","NICHT","VERGISS ES","FORGET IT","SKIP","LASS ES"]
BRIEFING_KEYWORDS=["tagesplan","briefing","was steht an","mein tag","morning briefing","daily briefing","was haben wir heute","ueberblick","schick briefing"]

# ── Datenpersistenz ────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {"notes":[],"todos":[],"conversation":[],"memory":{"persons":{},"patterns":{},"preferences":{},"facts":{}},"context":{"current":{}}}

def save_data(data):
    with open(DATA_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

def load_improvements():
    if os.path.exists(IMPROVEMENTS_FILE):
        with open(IMPROVEMENTS_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {"improvements":[]}

def save_improvements(data):
    with open(IMPROVEMENTS_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

def add_improvement(description,source="error",context=""):
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    data=load_improvements()
    desc_low=description.lower()
    for item in data["improvements"]:
        if item.get("status")=="open" and desc_low[:50] in item.get("description","").lower():
            return
    data["improvements"].append({"id":len(data["improvements"])+1,"date":now,"source":source,"description":description,"context":context,"status":"open"})
    save_improvements(data)

# ── Google Calendar ────────────────────────────────────────────────────────────
def get_calendar_service():
    try:
        creds=service_account.Credentials.from_service_account_file(GOOGLE_CREDS,scopes=["https://www.googleapis.com/auth/calendar"])
        return build("calendar","v3",credentials=creds)
    except Exception as e:
        logger.error(f"Cal error: {e}")
        return None

def add_calendar_event(title,start_dt,end_dt=None):
    try:
        service=get_calendar_service()
        if not service: return False
        if end_dt is None: end_dt=start_dt+timedelta(hours=1)
        event={"summary":title,"start":{"dateTime":start_dt.isoformat(),"timeZone":TIMEZONE},"end":{"dateTime":end_dt.isoformat(),"timeZone":TIMEZONE}}
        service.events().insert(calendarId=CALENDAR_ID,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"Event error: {e}")
        return False

def add_allday_event(title,start_date_str,end_date_str=None):
    try:
        service=get_calendar_service()
        if not service: return False
        end=end_date_str if end_date_str else (date.fromisoformat(start_date_str)+timedelta(days=1)).strftime("%Y-%m-%d")
        # end_date in Google Calendar is exclusive, so add 1 day to last day
        if end_date_str:
            end=(date.fromisoformat(end_date_str)+timedelta(days=1)).strftime("%Y-%m-%d")
        event={"summary":title,"start":{"date":start_date_str},"end":{"date":end}}
        service.events().insert(calendarId=CALENDAR_ID,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"Allday error: {e}")
        return False

def get_upcoming_events(days=7):
    try:
        service=get_calendar_service()
        if not service: return "Kalender nicht verfügbar."
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz).isoformat()
        end=(datetime.now(tz)+timedelta(days=days)).isoformat()
        result=service.events().list(calendarId=CALENDAR_ID,timeMin=now,timeMax=end,maxResults=20,singleEvents=True,orderBy="startTime").execute()
        events=result.get("items",[])
        if not events: return "Keine Termine."
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

def find_events_by_title(title_hint,days_ahead=60):
    STOP={"der","die","das","den","dem","ein","eine","und","oder","the","a","and","or","for","my"}
    try:
        service=get_calendar_service()
        if not service: return []
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz)
        result=service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=(now-timedelta(days=14)).isoformat(),
            timeMax=(now+timedelta(days=days_ahead)).isoformat(),
            maxResults=100,singleEvents=True,orderBy="startTime"
        ).execute()
        events=result.get("items",[])
        hint_low=title_hint.lower()
        hint_words=[w for w in hint_low.split() if len(w)>2 and w not in STOP]
        scored=[]
        for e in events:
            summary=e.get("summary","").lower()
            if not hint_words:
                if hint_low in summary: scored.append((1,e))
                continue
            matches=sum(1 for w in hint_words if w in summary)
            if matches>0: scored.append((matches/len(hint_words),e))
        scored.sort(key=lambda x:-x[0])
        threshold=0.5 if len(hint_words)>1 else 0.01
        return [e for s,e in scored if s>=threshold]
    except Exception as e:
        logger.error(f"find_events error: {e}")
        return []

def delete_calendar_event(event_id):
    try:
        service=get_calendar_service()
        if not service: return False
        service.events().delete(calendarId=CALENDAR_ID,eventId=event_id).execute()
        return True
    except Exception as e:
        logger.error(f"delete error: {e}")
        return False

def check_collision(start_dt,end_dt):
    try:
        service=get_calendar_service()
        if not service: return []
        result=service.events().list(calendarId=CALENDAR_ID,timeMin=start_dt.isoformat(),timeMax=end_dt.isoformat(),singleEvents=True,orderBy="startTime").execute()
        return [e.get("summary","?") for e in result.get("items",[])]
    except: return []

def find_free_slots(dt,duration_hours=1):
    try:
        service=get_calendar_service()
        if not service: return []
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
            if not e_start.tzinfo: e_start=tz.localize(e_start)
            if (e_start-current).total_seconds()/3600>=duration_hours:
                slots.append(current.strftime("%H:%M")+" - "+e_start.strftime("%H:%M"))
            e_end=datetime.fromisoformat(e["end"].get("dateTime",e["end"].get("date","")))
            if not e_end.tzinfo: e_end=tz.localize(e_end)
            current=max(current,e_end)
        if (day_end-current).total_seconds()/3600>=duration_hours:
            slots.append(current.strftime("%H:%M")+" - "+day_end.strftime("%H:%M"))
        return slots[:3]
    except: return []

def get_open_tasks(days_back=30):
    try:
        service=get_calendar_service()
        if not service: return []
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz)
        result=service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=(now-timedelta(days=days_back)).isoformat(),
            timeMax=(now+timedelta(days=14)).isoformat(),
            maxResults=50,singleEvents=True,orderBy="startTime"
        ).execute()
        tasks=[]
        for e in result.get("items",[]):
            summary=e.get("summary","")
            if summary.startswith("✅") and not summary.startswith("✅✅"):
                event_date=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                tasks.append({"id":e["id"],"title":summary.replace("✅ ","").replace("✅","").strip(),"date":event_date,"assignee":e.get("description","").strip()})
        return tasks
    except Exception as e:
        logger.error(f"get_open_tasks error: {e}")
        return []

def complete_task(task_id,task_title):
    try:
        service=get_calendar_service()
        if not service: return False
        event=service.events().get(calendarId=CALENDAR_ID,eventId=task_id).execute()
        event["summary"]="✅✅ "+task_title
        service.events().update(calendarId=CALENDAR_ID,eventId=task_id,body=event).execute()
        return True
    except: return False

def assign_task(task_id,task_title,assignee_name):
    try:
        service=get_calendar_service()
        if not service: return False
        event=service.events().get(calendarId=CALENDAR_ID,eventId=task_id).execute()
        event["description"]=assignee_name
        if assignee_name.lower() not in event["summary"].lower():
            event["summary"]="✅ "+task_title+" ("+assignee_name+")"
        service.events().update(calendarId=CALENDAR_ID,eventId=task_id,body=event).execute()
        return True
    except: return False

# ── Kontext & Gedächtnis ───────────────────────────────────────────────────────
DEFAULT_SCHEDULE={
    "kate":{"teaching_days":["donnerstag","sonntag"],"teaching_hours":"10-13"},
    "marlene":{"kita_start":"9:30","kita_end":"16:00"}
}

def get_childcare_situation(data=None):
    if data is None: data=load_data()
    ctx=data.get("context",{}).get("current",{})
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    today_wd=now.weekday()
    kita_open=today_wd<=4
    karsten_away=False
    karsten_status=""
    k_ctx=ctx.get("karsten",{})
    if k_ctx:
        status=k_ctx.get("status","")
        until=k_ctx.get("until","")
        if status in ["geschaeftsreise","reise","urlaub","abwesend","dienstreise"]:
            if until:
                try:
                    until_dt=tz.localize(datetime.fromisoformat(until)) if len(until)==10 else tz.localize(datetime(now.year,int(until.split(".")[1]),int(until.split(".")[0])))
                    if now>until_dt+timedelta(days=1):
                        data["context"]["current"]["karsten"]={}
                        save_data(data)
                    else:
                        karsten_away=True
                        karsten_status=status
                except:
                    karsten_away=True
                    karsten_status=status
            else:
                karsten_away=True
                karsten_status=status
    weekday_map={"montag":0,"dienstag":1,"mittwoch":2,"donnerstag":3,"freitag":4,"samstag":5,"sonntag":6}
    kate_teaching=any(weekday_map.get(d.lower(),99)==today_wd for d in DEFAULT_SCHEDULE["kate"]["teaching_days"])
    conflict=karsten_away and kita_open and kate_teaching
    return {
        "karsten_away":karsten_away,"karsten_status":karsten_status,
        "kate_teaching_today":kate_teaching,"kita_open":kita_open,
        "conflict":conflict,
        "conflict_reason":"Karsten ist "+karsten_status+" und Kate unterrichtet – wer holt Marlene ab?" if conflict else ""
    }

def get_memory_context(data):
    memory=data.get("memory",{})
    parts=[]
    if memory.get("persons"):
        parts.append("Bekannte Personen:")
        for name,info in memory["persons"].items():
            parts.append(f"- {name}: {info.get('relation','')} ({info.get('notes','')})")
    if memory.get("facts"):
        parts.append("Gespeicherte Fakten:")
        for k,v in memory["facts"].items():
            parts.append(f"- {v}")
    return "\n".join(parts)

def get_context_summary(data):
    ctx=data.get("context",{}).get("current",{})
    childcare=get_childcare_situation(data)
    lines=[]
    for person,info in ctx.items():
        if info and info.get("status"):
            lines.append(f"- {person.capitalize()}: {info['status']} bis {info.get('until','?')} ({info.get('note','')})")
    if childcare["karsten_away"] and childcare["kita_open"]:
        lines.append("- Karsten abwesend → Kate ist für Kita-Abholung zuständig (bis 16:00)")
    if childcare["conflict"]:
        lines.append("⚠️ KONFLIKT: "+childcare["conflict_reason"])
    return "\n".join(lines)

# ── Function Calling Tools Definition ─────────────────────────────────────────
TOOLS=[
    {
        "type":"function",
        "function":{
            "name":"create_event",
            "description":"Erstellt einen Kalendertermin mit Uhrzeit",
            "parameters":{
                "type":"object",
                "properties":{
                    "title":{"type":"string","description":"Titel des Termins"},
                    "date":{"type":"string","description":"Datum YYYY-MM-DD"},
                    "time":{"type":"string","description":"Uhrzeit HH:MM"},
                    "duration_hours":{"type":"number","description":"Dauer in Stunden (default 1)"},
                },
                "required":["title","date","time"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"create_allday_event",
            "description":"Erstellt einen ganztägigen Termin (Reisen, Urlaub, Geburtstage, Abwesenheiten). Für mehrtägige Events end_date setzen.",
            "parameters":{
                "type":"object",
                "properties":{
                    "title":{"type":"string","description":"Titel"},
                    "start_date":{"type":"string","description":"Startdatum YYYY-MM-DD"},
                    "end_date":{"type":"string","description":"Enddatum YYYY-MM-DD (optional, für mehrtägige Events)"},
                },
                "required":["title","start_date"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"create_task",
            "description":"Erstellt eine oder mehrere Aufgaben/Todos als ganztägige Einträge mit ✅",
            "parameters":{
                "type":"object",
                "properties":{
                    "tasks":{"type":"array","items":{"type":"string"},"description":"Liste der Aufgaben-Titel"},
                    "date":{"type":"string","description":"Fälligkeitsdatum YYYY-MM-DD (optional)"},
                },
                "required":["tasks"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"delete_event",
            "description":"Löscht einen Kalendertermin anhand des Titels",
            "parameters":{
                "type":"object",
                "properties":{
                    "title_hint":{"type":"string","description":"Titel oder Stichwort des zu löschenden Termins"},
                    "date":{"type":"string","description":"Datum YYYY-MM-DD zur Eingrenzung (optional)"},
                },
                "required":["title_hint"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"complete_task_by_title",
            "description":"Markiert eine Aufgabe als erledigt",
            "parameters":{
                "type":"object",
                "properties":{
                    "title_hint":{"type":"string","description":"Titel oder Stichwort der Aufgabe"},
                },
                "required":["title_hint"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"assign_task_to_person",
            "description":"Weist eine Aufgabe einer Person zu",
            "parameters":{
                "type":"object",
                "properties":{
                    "title_hint":{"type":"string","description":"Titel der Aufgabe"},
                    "assignee":{"type":"string","description":"Name der Person (Karsten oder Kate)"},
                },
                "required":["title_hint","assignee"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"get_events",
            "description":"Zeigt anstehende Termine",
            "parameters":{
                "type":"object",
                "properties":{
                    "days":{"type":"integer","description":"Anzahl Tage voraus (default 7)"},
                },
                "required":[]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"get_tasks",
            "description":"Zeigt offene Aufgaben",
            "parameters":{
                "type":"object",
                "properties":{
                    "days_back":{"type":"integer","description":"Wie weit zurückschauen (default 30)"},
                },
                "required":[]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"save_note",
            "description":"Speichert eine kurze Notiz",
            "parameters":{
                "type":"object",
                "properties":{
                    "text":{"type":"string","description":"Inhalt der Notiz"},
                },
                "required":["text"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"save_fact",
            "description":"Speichert ein dauerhaftes Faktum das der Bot sich merken soll",
            "parameters":{
                "type":"object",
                "properties":{
                    "key":{"type":"string","description":"Kurzer Schlüssel"},
                    "value":{"type":"string","description":"Was gespeichert werden soll"},
                },
                "required":["key","value"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"get_improvements",
            "description":"Zeigt die Liste der gemerkten Verbesserungsvorschläge",
            "parameters":{
                "type":"object",
                "properties":{
                    "status":{"type":"string","description":"Filter: open oder fixed (default: open)"},
                },
                "required":[]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"add_improvement",
            "description":"Fügt einen Verbesserungsvorschlag zur Liste hinzu",
            "parameters":{
                "type":"object",
                "properties":{
                    "description":{"type":"string","description":"Was verbessert werden sollte"},
                },
                "required":["description"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"adhd_emergency",
            "description":"ADHS-Notfallmodus: gibt konkrete kleine nächste Schritte wenn jemand überwältigt ist",
            "parameters":{
                "type":"object",
                "properties":{},
                "required":[]
            }
        }
    },
]

# ── Tool Execution ─────────────────────────────────────────────────────────────
def execute_tool(tool_name,tool_args,user_id,data):
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    lang=USERS.get(user_id,{}).get("lang","de")
    user_name=USER_NAMES.get(user_id,"")

    if tool_name=="create_event":
        title=tool_args["title"]
        date_str=tool_args["date"]
        time_str=tool_args.get("time","09:00")
        duration=tool_args.get("duration_hours",1)
        try:
            dt=tz.localize(datetime.strptime(date_str+" "+time_str,"%Y-%m-%d %H:%M"))
            end_dt=dt+timedelta(hours=duration)
            conflicts=check_collision(dt,end_dt)
            if conflicts:
                free=find_free_slots(dt)
                msg=("Konflikt! '"+title+"' kollidiert mit: "+", ".join(conflicts))
                if free: msg+="\nFreie Slots: "+", ".join(free)
                msg+=("\nTrotzdem eintragen? JA oder NEIN." if lang=="de" else "\nAdd anyway? YES or NO.")
                data["pending_event"]={"title":title,"dt":dt.isoformat().split("+")[0]}
                save_data(data)
                return msg
            ok=add_calendar_event(title,dt,end_dt)
            if ok:
                return ("✅ Termin eingetragen: "+title+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if lang=="de"
                    else "✅ Event added: "+title+" on "+dt.strftime("%Y-%m-%d at %H:%M"))
            return "Fehler beim Eintragen." if lang=="de" else "Error adding event."
        except Exception as e:
            return "Fehler: "+str(e)

    elif tool_name=="create_allday_event":
        title=tool_args["title"]
        start=tool_args["start_date"]
        end=tool_args.get("end_date")
        ok=add_allday_event(title,start,end)
        if ok:
            if end and end!=start:
                return ("✅ Eingetragen: "+title+" vom "+start+" bis "+end if lang=="de"
                    else "✅ Added: "+title+" from "+start+" to "+end)
            return ("✅ Eingetragen: "+title+" am "+start+" (ganztägig)" if lang=="de"
                else "✅ Added: "+title+" on "+start+" (all day)")
        return "Fehler." if lang=="de" else "Error."

    elif tool_name=="create_task":
        tasks=tool_args.get("tasks",[])
        date_str=tool_args.get("date","")
        try:
            dt=datetime.strptime(date_str,"%Y-%m-%d") if date_str else datetime(now.year,now.month,now.day)
            start_str=dt.strftime("%Y-%m-%d")
        except:
            start_str=now.strftime("%Y-%m-%d")
        saved=[]
        for t in tasks:
            if add_allday_event("✅ "+t.strip(),start_str):
                saved.append(t.strip())
        if len(saved)==1:
            return ("✅ Aufgabe eingetragen: "+saved[0] if lang=="de" else "✅ Task added: "+saved[0])
        elif saved:
            return ("✅ "+str(len(saved))+" Aufgaben eingetragen:\n" if lang=="de" else "✅ "+str(len(saved))+" tasks added:\n")+"\n".join("• "+t for t in saved)
        return "Fehler." if lang=="de" else "Error."

    elif tool_name=="delete_event":
        hint=tool_args["title_hint"]
        date_hint=tool_args.get("date","")
        matches=find_events_by_title(hint)
        # Filter by date if provided
        if date_hint and matches:
            filtered=[e for e in matches if e["start"].get("date",e["start"].get("dateTime",""))[:10]==date_hint]
            if filtered: matches=filtered
        if not matches:
            return ("Keinen Termin gefunden mit '"+hint+"'." if lang=="de" else "No event found matching '"+hint+"'.")
        if len(matches)==1:
            e=matches[0]
            event_title=e.get("summary","?")
            event_date=e["start"].get("date",e["start"].get("dateTime",""))[:10]
            data["pending_delete"]={"id":e["id"],"title":event_title,"date":event_date}
            save_data(data)
            return ("Soll ich '"+event_title+"' am "+event_date+" löschen? JA oder NEIN." if lang=="de"
                else "Delete '"+event_title+"' on "+event_date+"? YES or NO.")
        lines=["Mehrere Treffer:" if lang=="de" else "Multiple matches:"]
        candidates=[]
        for i,e in enumerate(matches[:5],1):
            t=e.get("summary","?")
            d=e["start"].get("date",e["start"].get("dateTime",""))[:10]
            lines.append(str(i)+". "+t+" ("+d+")")
            candidates.append({"id":e["id"],"title":t,"date":d})
        lines.append("Welchen? (Nummer)" if lang=="de" else "Which one? (number)")
        data["pending_delete_list"]=candidates
        save_data(data)
        return "\n".join(lines)

    elif tool_name=="complete_task_by_title":
        hint=tool_args["title_hint"]
        tasks=get_open_tasks(days_back=60)
        matched=next((t for t in tasks if hint.lower() in t["title"].lower() or t["title"].lower() in hint.lower()),None)
        if not matched and tasks:
            nums=re.findall(r"\d+",hint)
            if nums:
                idx=int(nums[0])-1
                if 0<=idx<len(tasks): matched=tasks[idx]
        if matched:
            ok=complete_task(matched["id"],matched["title"])
            return ("✅✅ Erledigt: "+matched["title"] if ok else "Fehler.")
        return ("Aufgabe nicht gefunden." if lang=="de" else "Task not found.")

    elif tool_name=="assign_task_to_person":
        hint=tool_args["title_hint"]
        assignee=tool_args["assignee"]
        tasks=get_open_tasks(days_back=60)
        matched=next((t for t in tasks if hint.lower() in t["title"].lower()),None)
        if matched:
            ok=assign_task(matched["id"],matched["title"],assignee)
            return ("📌 "+matched["title"]+" → "+assignee if ok else "Fehler.")
        return ("Aufgabe nicht gefunden." if lang=="de" else "Task not found.")

    elif tool_name=="get_events":
        days=tool_args.get("days",7)
        return get_upcoming_events(days)

    elif tool_name=="get_tasks":
        days_back=tool_args.get("days_back",30)
        tasks=get_open_tasks(days_back=days_back)
        if not tasks:
            return ("Keine offenen Aufgaben! 🎉" if lang=="de" else "No open tasks! 🎉")
        tz_l=pytz.timezone(TIMEZONE)
        today=datetime.now(tz_l).strftime("%Y-%m-%d")
        lines=[]
        for i,t in enumerate(tasks,1):
            assignee=" → "+t["assignee"] if t["assignee"] else ""
            overdue=" ⚠️" if t["date"]<today else ""
            lines.append(str(i)+". "+t["title"]+assignee+overdue)
        return ("Offene Aufgaben:\n" if lang=="de" else "Open tasks:\n")+"\n".join(lines)

    elif tool_name=="save_note":
        text=tool_args["text"]
        if "notes" not in data: data["notes"]=[]
        data["notes"].append({"text":text,"datum":now.strftime("%d.%m.%Y %H:%M")})
        save_data(data)
        return ("📝 Notiz gespeichert." if lang=="de" else "📝 Note saved.")

    elif tool_name=="save_fact":
        key=tool_args["key"]
        value=tool_args["value"]
        if "memory" not in data: data["memory"]={}
        if "facts" not in data["memory"]: data["memory"]["facts"]={}
        data["memory"]["facts"][key]=value
        save_data(data)
        return "💾 Gespeichert: "+value

    elif tool_name=="get_improvements":
        status_filter=tool_args.get("status","open")
        improvements=load_improvements().get("improvements",[])
        items=[i for i in improvements if i.get("status")==status_filter]
        if not items: return "Keine Einträge."
        lines=[]
        for i in items:
            icon={"open":"🔴","fixed":"✅"}.get(i.get("status","open"),"⚪")
            lines.append(icon+" #"+str(i["id"])+" ["+i["date"][:10]+"] ("+i["source"]+")\n   "+i["description"])
        total=len(improvements)
        return ("🔴 Offene Verbesserungen ("+str(total)+" gesamt):\n\n" if lang=="de" else "🔴 Open improvements ("+str(total)+" total):\n\n")+"\n\n".join(lines)

    elif tool_name=="add_improvement":
        desc=tool_args["description"]
        add_improvement(desc,source="user_manual",context=user_name+": manuell")
        return "📝 Notiert: "+desc

    elif tool_name=="adhd_emergency":
        tasks=get_open_tasks(days_back=60)
        today=now.strftime("%Y-%m-%d")
        overdue=[t for t in tasks if t["date"]<today]
        priority=(overdue[:3] if overdue else tasks[:3])
        task_list="\n".join("- "+t["title"] for t in priority) if priority else ""
        try:
            if lang=="de":
                prompt=("Du bist ein ADHS-Coach. "+user_name+" fühlt sich überfordert. "
                    "Antworte kurz (max 4 Zeilen), ruhig und motivierend auf Deutsch.\n"
                    +("Offene Aufgaben:\n"+task_list if task_list else "Keine Aufgaben offen.")
                    +"\nNenne EINE kleinste konkrete nächste Handlung.")
            else:
                prompt=("You are an ADHD coach. "+user_name+" feels overwhelmed. "
                    "Reply briefly (max 4 lines), calm and motivating.\n"
                    +("Open tasks:\n"+task_list if task_list else "No open tasks.")
                    +"\nName ONE smallest concrete next action.")
            resp=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=150)
            return resp.choices[0].message.content.strip()
        except:
            return ("Erstmal durchatmen.\n→ "+priority[0]["title"] if priority else "Du schaffst das. 🌿")

    return "Unbekanntes Tool: "+tool_name

# ── GPT mit Function Calling ───────────────────────────────────────────────────
async def ask_gpt_with_tools(user_message,user_id,data):
    tz=pytz.timezone(TIMEZONE)
    now_str=datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    user_name=USER_NAMES.get(user_id,"")
    lang=USERS.get(user_id,{}).get("lang","de")
    events=get_upcoming_events()
    memory_ctx=get_memory_context(data)
    context_summary=get_context_summary(data)
    childcare=get_childcare_situation(data)

    childcare_note=""
    if childcare["karsten_away"] and childcare["kita_open"]:
        childcare_note=("\nWICHTIG: Karsten ist abwesend – Kate ist für Marlenes Kita-Abholung zuständig!" if lang=="de"
            else "\nIMPORTANT: Karsten is away – Kate is responsible for Marlene's kindergarten pickup!")
    if childcare["conflict"]:
        childcare_note+="\n⚠️ "+childcare["conflict_reason"]

    system=(
        "Du bist ein KI-Assistent für ein Paar mit ADHS (Karsten & Kate). "
        "Antworte immer in der Sprache des Nutzers. Kurz und klar.\n"
        "Nutzer: "+user_name+" | Zeit: "+now_str+"\n"
        "Termine:\n"+events+"\n"
        +(memory_ctx+"\n" if memory_ctx else "")
        +(context_summary+"\n" if context_summary else "")
        +childcare_note+"\n"
        "Nutze die verfügbaren Tools für alle Kalender- und Aufgaben-Aktionen. "
        "Bei Datumsangaben wie 'heute', 'morgen', 'übermorgen', 'nächsten Montag' – berechne das konkrete Datum selbst."
    )

    messages=[{"role":"system","content":system}]
    for msg in data.get("conversation",[])[-10:]:
        messages.append(msg)
    messages.append({"role":"user","content":user_message})

    # First GPT call
    response=client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=500
    )
    msg=response.choices[0].message

    # No tool call → direct response
    if not msg.tool_calls:
        return msg.content or ""

    # Execute all tool calls
    tool_results=[]
    messages.append(msg)

    for tc in msg.tool_calls:
        tool_name=tc.function.name
        try:
            tool_args=json.loads(tc.function.arguments)
        except:
            tool_args={}
        result=execute_tool(tool_name,tool_args,user_id,data)
        tool_results.append(result)
        messages.append({
            "role":"tool",
            "tool_call_id":tc.id,
            "content":result
        })

    # Second GPT call to formulate final response
    response2=client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=300
    )
    return response2.choices[0].message.content or "\n".join(tool_results)

# ── Kontext-Erkennung & Gedächtnis ────────────────────────────────────────────
def update_context_from_message(user_message,user_id,data):
    user_name=USER_NAMES.get(user_id,"").lower()
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    extract_prompt=("Analysiere auf Kontextänderungen (Reisen, Abwesenheiten, Urlaub). "
        "Gib NUR JSON zurück oder {}.\n"
        "Format: {\"person\":\"karsten/kate\",\"status\":\"geschaeftsreise/urlaub/normal\","
        "\"from\":\"YYYY-MM-DD\",\"until\":\"YYYY-MM-DD\",\"note\":\"...\"}\n"
        "Heute: "+now.strftime("%Y-%m-%d")+"\nSchreiber: "+user_name+"\nNachricht: "+user_message)
    try:
        response=client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"user","content":extract_prompt}],
            max_tokens=150,
            response_format={"type":"json_object"}
        )
        extracted=json.loads(response.choices[0].message.content)
        if not extracted or "person" not in extracted: return None
        person=extracted.get("person","")
        if person not in ["karsten","kate"]: return None
        if "context" not in data: data["context"]={"current":{}}
        if "current" not in data["context"]: data["context"]["current"]={}
        data["context"]["current"][person]={
            "status":extracted.get("status",""),
            "from":extracted.get("from",""),
            "until":extracted.get("until",""),
            "note":extracted.get("note",""),
            "updated":now.strftime("%Y-%m-%d %H:%M"),
        }
        save_data(data)
        if person=="karsten" and extracted.get("status") in ["geschaeftsreise","reise","urlaub","abwesend"]:
            return extracted.get("status","")
        return None
    except: return None

def update_memory_from_message(user_message,user_id,gpt_response,data):
    try:
        user_name=USER_NAMES.get(user_id,"")
        prompt=("Extrahiere merkenswerte Personen-Infos. Gib NUR JSON zurück oder {}.\n"
            "Format: {\"persons\":{\"Name\":{\"relation\":\"...\",\"notes\":\"...\"}},"
            "\"patterns\":{\"key\":\"val\"},\"preferences\":{\"key\":\"val\"}}\n"
            "Nutzer: "+user_name+"\nNachricht: "+user_message+"\nAntwort: "+gpt_response)
        response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=200,response_format={"type":"json_object"})
        extracted=json.loads(response.choices[0].message.content)
        if not extracted: return
        if "memory" not in data: data["memory"]={"persons":{},"patterns":{},"preferences":{}}
        for name,info in extracted.get("persons",{}).items():
            if name not in data["memory"].get("persons",{}):
                data["memory"].setdefault("persons",{})[name]=info
            else:
                data["memory"]["persons"][name].update(info)
        data["memory"].setdefault("patterns",{}).update(extracted.get("patterns",{}))
        data["memory"].setdefault("preferences",{}).update(extracted.get("preferences",{}))
        save_data(data)
    except: pass

async def notify_partner_if_needed(bot,user_id,context_change,data):
    if not context_change: return
    user_info=USERS.get(user_id,{})
    partner_id=user_info.get("partner_id","")
    if not partner_id: return
    partner_lang=USERS.get(partner_id,{}).get("lang","de")
    childcare=get_childcare_situation(data)
    if partner_lang=="en":
        msg="📋 FYI: Karsten is on "+context_change
        if childcare.get("karsten_away"): msg+="\n→ You're responsible for Marlene's kindergarten pickup (by 16:00)!"
        if childcare.get("conflict"): msg+="\n⚠️ "+childcare["conflict_reason"]
    else:
        msg="📋 Info: Karsten ist auf "+context_change
        if childcare.get("karsten_away"): msg+="\n→ Du bist für Marlenes Kita-Abholung zuständig (bis 16:00)!"
        if childcare.get("conflict"): msg+="\n⚠️ "+childcare["conflict_reason"]
    try:
        await bot.send_message(chat_id=int(partner_id),text=msg)
    except Exception as e:
        logger.error(f"Partner notify error: {e}")

def detect_user_frustration(user_message,user_id,last_bot_response,data):
    TRIGGERS=["hat nicht geklappt","funktioniert nicht","falsch","stimmt nicht","du verstehst",
              "schon wieder","immer noch","not working","wrong","doesn't work","failed","still broken"]
    if not any(t in user_message.lower() for t in TRIGGERS): return
    try:
        user_name=USER_NAMES.get(user_id,"?")
        prompt=("Nutzer ist unzufrieden. Formuliere in einem Satz was der Bot verbessern sollte.\n"
            "Nutzer: '"+user_message+"'\nBot vorher: '"+last_bot_response[:200]+"'\n"
            "Antwort NUR auf Deutsch, max 120 Zeichen.")
        resp=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=80)
        add_improvement(resp.choices[0].message.content.strip(),source="user_feedback",context=user_name+": '"+user_message[:60]+"'")
    except: pass

# ── Briefing & Scheduler ───────────────────────────────────────────────────────
async def generate_personal_briefing(user_id,user_name,lang="de"):
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    weekdays_de=["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    weekdays_en=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    weekday=weekdays_de[now.weekday()] if lang=="de" else weekdays_en[now.weekday()]
    events=get_upcoming_events(2)
    data=load_data()
    childcare=get_childcare_situation(data)
    childcare_briefing=""
    if childcare["karsten_away"] and childcare["kita_open"]:
        childcare_briefing=("\n⚠️ Karsten ist abwesend – du bist für Marlenes Kita-Abholung zuständig (bis 16:00)!" if lang=="de"
            else "\n⚠️ Karsten is away – you're responsible for Marlene's kindergarten pickup (by 16:00)!")
    if childcare["conflict"]:
        childcare_briefing+="\n⚠️ "+childcare["conflict_reason"]
    memory_ctx=get_memory_context(data)
    if lang=="de":
        prompt=("Erstelle kurzes persönliches Morgen-Briefing auf Deutsch für "+user_name+" (ADHS).\n"
            "Heute: "+weekday+" "+now.strftime("%d.%m.%Y")+"\nTermine:\n"+events+"\n"+memory_ctx+childcare_briefing+
            "\nBriefing: Mit 'Guten Morgen "+user_name+"!' beginnen, Tag nennen, "
            "Termine erwähnen, Kita wenn relevant, max 8 Zeilen.")
    else:
        prompt=("Create short personal morning briefing in English for "+user_name+" (ADHD).\n"
            "Today: "+weekday+" "+now.strftime("%Y-%m-%d")+"\nEvents:\n"+events+"\n"+memory_ctx+childcare_briefing+
            "\nBriefing: Start with 'Good morning "+user_name+"!', mention today, events, childcare if relevant, max 8 lines.")
    response=client.chat.completions.create(model="gpt-4o",messages=[{"role":"user","content":prompt}],max_tokens=300)
    return response.choices[0].message.content

async def generate_briefing(bot,target_user_id=None):
    for uid,uinfo in USERS.items():
        if target_user_id and uid!=target_user_id: continue
        try:
            briefing=await generate_personal_briefing(uid,uinfo["name"],uinfo.get("lang","de"))
            await bot.send_message(chat_id=int(uid),text=briefing)
        except Exception as e:
            logger.error(f"Briefing error {uinfo['name']}: {e}")

async def evening_checkin(bot):
    tasks=get_open_tasks(days_back=7)
    if not tasks: return
    data=load_data()
    data["checkin_tasks"]=tasks
    save_data(data)
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    for uid,uinfo in USERS.items():
        lang=uinfo.get("lang","de")
        my_tasks=[t for t in tasks if not t["assignee"] or t["assignee"].lower()==uinfo["name"].lower()]
        if not my_tasks: continue
        if lang=="de":
            header="🌙 Guten Abend "+uinfo["name"]+"! Kurzes Check-in:"
            footer="\nWas hast du erledigt? '1,3' · 'alles' · 'nichts'\nOder zuweisen: '2 Kate' '3 Karsten'"
        else:
            header="🌙 Good evening "+uinfo["name"]+"! Quick check-in:"
            footer="\nWhat did you finish? '1,3' · 'all' · 'nothing'\nOr assign: '2 Kate' '3 Karsten'"
        try:
            msg=header+"\n\n"
            for i,t in enumerate(my_tasks,1):
                overdue=" ⚠️" if t["date"]<now.strftime("%Y-%m-%d") else ""
                msg+=str(i)+". "+t["title"]+overdue+"\n"
            msg+=footer
            await bot.send_message(chat_id=int(uid),text=msg)
        except Exception as e:
            logger.error(f"Checkin error {uinfo['name']}: {e}")

async def task_reminder(bot):
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    tasks=get_open_tasks(days_back=30)
    overdue=[t for t in tasks if t["date"]<now.strftime("%Y-%m-%d")]
    if not overdue: return
    for uid,uinfo in USERS.items():
        lang=uinfo.get("lang","de")
        my_tasks=[t for t in overdue if not t["assignee"] or t["assignee"].lower()==uinfo["name"].lower()]
        if not my_tasks: continue
        header=("⏰ "+str(len(my_tasks))+" überfällige Aufgabe(n):" if lang=="de"
            else "⏰ "+str(len(my_tasks))+" overdue task(s):")
        msg=header+"\n"+"\n".join(str(i)+". "+t["title"]+" ⚠️" for i,t in enumerate(my_tasks,1))
        try:
            await bot.send_message(chat_id=int(uid),text=msg)
        except Exception as e:
            logger.error(f"Reminder error {uinfo['name']}: {e}")

async def weekly_review(bot):
    tasks=get_open_tasks(days_back=30)
    for uid,uinfo in USERS.items():
        lang=uinfo.get("lang","de")
        try:
            if not tasks:
                await bot.send_message(chat_id=int(uid),text="🎉 Tolle Woche! Alle Aufgaben erledigt." if lang=="de" else "🎉 Great week! All tasks done.")
            else:
                header="📊 Wochenreview – offene Aufgaben:" if lang=="de" else "📊 Weekly Review – open tasks:"
                msg=header+"\n"+"\n".join(str(i)+". "+t["title"] for i,t in enumerate(tasks,1))
                await bot.send_message(chat_id=int(uid),text=msg)
        except Exception as e:
            logger.error(f"Weekly review error {uinfo['name']}: {e}")

async def scheduler(bot):
    import asyncio
    tz=pytz.timezone(TIMEZONE)
    while True:
        now=datetime.now(tz)
        if now.hour==7 and now.minute==0:
            await generate_briefing(bot)
            await asyncio.sleep(61)
        elif now.hour==12 and now.minute==0:
            await task_reminder(bot)
            await asyncio.sleep(61)
        elif now.hour==20 and now.minute==0:
            await evening_checkin(bot)
            await asyncio.sleep(61)
        elif now.hour==18 and now.minute==0 and now.weekday()==6:
            await weekly_review(bot)
            await asyncio.sleep(61)
        else:
            await asyncio.sleep(30)

# ── Sprachtranskription ────────────────────────────────────────────────────────
async def transcribe_voice(file_path):
    with open(file_path,"rb") as f:
        t=client.audio.transcriptions.create(model="whisper-1",file=f)
    return t.text

# ── Checkin-Response Handler ───────────────────────────────────────────────────
def handle_checkin_response(text,tasks):
    txt=text.strip().lower()
    completed_indices=[]
    assignments={}
    if any(w in txt for w in ["alles","alle","all","everything","done"]):
        completed_indices=list(range(len(tasks)))
    elif any(w in txt for w in ["nichts","nix","nothing","keine","nope"]):
        completed_indices=[]
    else:
        numbers=re.findall(r"\d+",txt)
        completed_indices=[int(n)-1 for n in numbers if 0<int(n)<=len(tasks)]
        for name in ["karsten","kate"]:
            matches_found=re.findall(r"(\d+)\s*"+name+r"|"+name+r"\s*(\d+)",txt)
            for m in matches_found:
                idx=int(m[0] or m[1])-1
                if 0<=idx<len(tasks):
                    assignments[idx]=name.capitalize()
    return completed_indices,assignments

# ── Message Handler ────────────────────────────────────────────────────────────
async def handle_message(update,context,text,user_id):
    """Gemeinsamer Handler für Text und Sprache."""
    data=load_data()
    lang=USERS.get(user_id,{}).get("lang","de")
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)

    # ── Pending Event (Kollision) ──
    pending=data.get("pending_event")
    if pending:
        txt=text.strip().upper().replace("!","").replace(".","").strip()
        if any(txt==w or txt.startswith(w) for w in JA_WORDS):
            dt=tz.localize(datetime.fromisoformat(pending["dt"]))
            ok=add_calendar_event(pending["title"],dt,dt+timedelta(hours=1))
            data.pop("pending_event",None)
            save_data(data)
            return ("✅ Termin eingetragen: "+pending["title"]+" am "+dt.strftime("%d.%m.%Y um %H:%M Uhr") if lang=="de"
                else "✅ Event added: "+pending["title"])
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            data.pop("pending_event",None)
            save_data(data)
            return ("OK, Termin nicht eingetragen." if lang=="de" else "OK, event not added.")

    # ── Pending Delete ──
    pending_delete=data.get("pending_delete")
    if pending_delete:
        txt=text.strip().upper().replace("!","").replace(".","").strip()
        if any(txt==w or txt.startswith(w) for w in JA_WORDS):
            try:
                ok=delete_calendar_event(pending_delete["id"])
                msg="🗑️ Gelöscht: "+pending_delete["title"] if ok else "Fehler beim Löschen!"
            except Exception as ex:
                msg="Fehler: "+str(ex)
            for key in ["pending_delete","pending_delete_list"]:
                data.pop(key,None)
            save_data(data)
            return msg
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            for key in ["pending_delete","pending_delete_list"]:
                data.pop(key,None)
            save_data(data)
            return ("OK, Termin bleibt." if lang=="de" else "OK, event kept.")

    # ── Pending Delete List ──
    pending_delete_list=data.get("pending_delete_list")
    if pending_delete_list:
        txt_upper=text.strip().upper()
        txt_low=text.strip().lower()
        if any(txt_upper==w or txt_upper.startswith(w) for w in NEIN_WORDS) and not any(kw in txt_low for kw in ["lösch","delete","entfern"]):
            data.pop("pending_delete_list",None)
            save_data(data)
            return ("OK, nichts gelöscht." if lang=="de" else "OK, nothing deleted.")
        # "heute"
        today_str=now.strftime("%Y-%m-%d")
        if any(w in txt_low for w in ["heute","today"]):
            for e in pending_delete_list:
                if e.get("date","")==today_str:
                    data["pending_delete"]=e
                    data.pop("pending_delete_list",None)
                    save_data(data)
                    return ("Soll ich '"+e["title"]+"' am "+e["date"]+" löschen? JA oder NEIN." if lang=="de"
                        else "Delete '"+e["title"]+"' on "+e["date"]+"? YES or NO.")
        # Nummer
        nums=re.findall(r"\d+",text)
        if nums:
            try:
                idx=int(nums[0])-1
                if 0<=idx<len(pending_delete_list):
                    e=pending_delete_list[idx]
                    data["pending_delete"]=e
                    data.pop("pending_delete_list",None)
                    save_data(data)
                    return ("Soll ich '"+e["title"]+"' am "+e["date"]+" löschen? JA oder NEIN." if lang=="de"
                        else "Delete '"+e["title"]+"' on "+e["date"]+"? YES or NO.")
            except: pass

    # ── Briefing-Keyword ──
    if any(kw in text.lower() for kw in BRIEFING_KEYWORDS):
        await generate_briefing(context.bot,target_user_id=user_id)
        return None

    # ── Frustrations-Erkennung (vor GPT) ──
    last_bot=data["conversation"][-1]["content"] if data.get("conversation") else ""
    detect_user_frustration(text,user_id,last_bot,data)

    # ── Checkin-Antwort ──
    checkin_tasks=data.get("checkin_tasks",[])
    if checkin_tasks:
        CHECKIN_TRIGGERS=(
            any(w in text.lower() for w in ["alles","alle","all","nichts","nix","nothing","done","erledigt","fertig"]) or
            bool(re.search(r"^\s*[\d,\s]+\s*$",text.lower())) or
            bool(re.search(r"\d+\s*(kate|karsten)",text.lower())) or
            bool(re.search(r"(kate|karsten)\s*\d+",text.lower()))
        )
        if CHECKIN_TRIGGERS:
            completed_idx,assignments_map=handle_checkin_response(text,checkin_tasks)
            msgs=[]
            for idx,task in enumerate(checkin_tasks):
                if idx in assignments_map:
                    assign_task(task["id"],task["title"],assignments_map[idx])
                    msgs.append("📌 "+task["title"]+" → "+assignments_map[idx])
                elif idx in completed_idx:
                    complete_task(task["id"],task["title"])
                    msgs.append("✅✅ "+task["title"])
            if not msgs:
                msgs=["OK, nichts markiert." if lang=="de" else "OK, nothing marked."]
            data.pop("checkin_tasks",None)
            save_data(data)
            return "\n".join(msgs)

    # ── GPT mit Function Calling ──
    try:
        response=await ask_gpt_with_tools(text,user_id,data)
    except Exception as ex:
        add_improvement(str(ex)[:100],source="error",context="User: "+text[:60])
        response=("Entschuldigung, da ist etwas schiefgelaufen. Ich habe es notiert." if lang=="de"
            else "Sorry, something went wrong. I've noted it.")

    # Kontext-Erkennung im Hintergrund
    ctx_change=update_context_from_message(text,user_id,data)
    update_memory_from_message(text,user_id,response,data)

    # Konversation speichern
    data["conversation"].append({"role":"user","content":text})
    data["conversation"].append({"role":"assistant","content":response})
    if len(data["conversation"])>20:
        data["conversation"]=data["conversation"][-20:]
    save_data(data)

    # Partner benachrichtigen wenn Kontext sich geändert hat
    await notify_partner_if_needed(context.bot,user_id,ctx_change,data)

    return response

async def handle_text(update,context):
    user_id=str(update.effective_user.id)
    text=update.message.text
    data=load_data()
    chat_id=str(update.effective_chat.id)
    if data.get("chat_id")!=chat_id:
        data["chat_id"]=chat_id
        save_data(data)
    await update.message.chat.send_action("typing")
    response=await handle_message(update,context,text,user_id)
    if response:
        await update.message.reply_text(response)

async def handle_voice(update,context):
    user_id=str(update.effective_user.id)
    await update.message.chat.send_action("typing")
    voice=update.message.voice
    voice_file=await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg",delete=False) as tmp:
        tmp_path=tmp.name
    await voice_file.download_to_drive(tmp_path)
    text=await transcribe_voice(tmp_path)
    os.unlink(tmp_path)
    user_name=USER_NAMES.get(user_id,"")
    await update.message.reply_text("Gehört: "+(user_name+": " if user_name else "")+text)
    response=await handle_message(update,context,text,user_id)
    if response:
        await update.message.reply_text(response)

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    print("Agent v2 läuft! (Function Calling Architektur)")
    app.run_polling()

if __name__=="__main__":
    main()