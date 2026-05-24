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
BRIEFING_KEYWORDS=["tagesplan","briefing","was steht an","mein tag","morning briefing","daily briefing","was haben wir heute","ueberblick","schick briefing"]

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

def add_allday_event(title,dt,end_dt=None):
    try:
        service=get_calendar_service()
        if not service:
            return False
        # end_dt is exclusive in Google Calendar (day after last day)
        if end_dt is None:
            end_date=(dt+timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            end_date=(end_dt+timedelta(days=1)).strftime("%Y-%m-%d")
        event={"summary":title,"start":{"date":dt.strftime("%Y-%m-%d")},"end":{"date":end_date}}
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

def find_events_by_title(title_hint,days_ahead=60):
    """Sucht Kalender-Events anhand eines Titels (fuzzy, score-basiert)."""
    # Stop-Woerter die nicht als Suchbegriff taugen
    STOP={"der","die","das","den","dem","ein","eine","einen","und","oder","fuer","vom","von","am","an","im","in",
          "the","a","an","and","or","for","on","at","in","my","our"}
    try:
        service=get_calendar_service()
        if not service:
            return []
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz)
        end=(now+timedelta(days=days_ahead)).isoformat()
        start=(now-timedelta(days=14)).isoformat()  # 2 Wochen rueckwirkend
        result=service.events().list(
            calendarId=CALENDAR_ID,timeMin=start,timeMax=end,
            maxResults=100,singleEvents=True,orderBy="startTime"
        ).execute()
        events=result.get("items",[])
        hint_low=title_hint.lower()
        # Sinnvolle Suchwoerter (>2 Zeichen, keine Stop-Woerter)
        hint_words=[w for w in hint_low.split() if len(w)>2 and w not in STOP]
        scored=[]
        for e in events:
            summary=e.get("summary","").lower()
            if not hint_words:
                if hint_low in summary:
                    scored.append((1,e))
                continue
            # Score: Anteil der Hint-Woerter die im Titel vorkommen
            matches_count=sum(1 for w in hint_words if w in summary)
            if matches_count>0:
                score=matches_count/len(hint_words)
                scored.append((score,e))
        # Sortiert nach Score absteigend, mindestens 1 Wort muss matchen
        scored.sort(key=lambda x:-x[0])
        # Nur Events mit Score >= 0.5 (mind. Haelfte der Woerter)
        # Oder wenn nur 1 Suchwort: direkt nehmen
        threshold=0.5 if len(hint_words)>1 else 0.01
        return [e for score,e in scored if score>=threshold]
    except Exception as e:
        logger.error(f"find_events error: {e}")
        return []

def delete_calendar_event(event_id):
    """Loescht ein Kalender-Event anhand der ID."""
    try:
        service=get_calendar_service()
        if not service:
            return False
        service.events().delete(calendarId=CALENDAR_ID,eventId=event_id).execute()
        return True
    except Exception as e:
        logger.error(f"delete_event error: {e}")
        return False

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

def save_fact(fact_key,fact_value,data=None):
    """Speichert ein dauerhaftes Faktum im memory.facts Dictionary."""
    if data is None:
        data=load_data()
    if "memory" not in data:
        data["memory"]={"persons":{},"patterns":{},"preferences":{},"facts":{}}
    if "facts" not in data["memory"]:
        data["memory"]["facts"]={}
    data["memory"]["facts"][fact_key]=fact_value
    save_data(data)

def get_facts_context():
    """Gibt gespeicherte Fakten als Kontext-String zurueck."""
    data=load_data()
    facts=data.get("memory",{}).get("facts",{})
    if not facts:
        return ""
    lines=[f"- {k}: {v}" for k,v in facts.items()]
    return "\nGespeicherte Fakten:\n"+"\n".join(lines)+"\n"

def detect_and_save_fact(user_message,user_id,data=None):
    """
    Erkennt wenn jemand sagt 'merke dir X' oder 'X ist Y' als Fakt.
    Speichert dauerhaft in memory.facts.
    """
    if data is None:
        data=load_data()
    triggers=["merke dir","remember that","speicher","note that","wichtig:","bitte merken",
              "ab jetzt","von nun an","from now on","immer wenn","always when"]
    msg_low=user_message.lower()
    if not any(t in msg_low for t in triggers):
        return None
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    extract_prompt="""Extrahiere einen dauerhaften Fakt aus der Nachricht. Gib NUR JSON zurueck oder {}.
Format: {"key":"kurzer_schluessel","value":"was gespeichert werden soll"}
Beispiele:
- "Merke dir: Kita ist Mo-Fr" -> {"key":"kita_oeffnungszeiten","value":"Kita ist nur Montag bis Freitag geoeffnet, am Wochenende geschlossen"}
- "Marlene hat ab September Schule" -> {"key":"marlene_schule","value":"Marlene geht ab September in die Schule"}
- "Kate unterrichtet jetzt auch mittwochs" -> {"key":"kate_unterricht","value":"Kate unterrichtet Mittwoch, Donnerstag und Sonntag"}
Nachricht: """+user_message
    try:
        response=client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"user","content":extract_prompt}],
            max_tokens=100,
            response_format={"type":"json_object"}
        )
        extracted=json.loads(response.choices[0].message.content)
        if extracted and "key" in extracted and "value" in extracted:
            save_fact(extracted["key"],extracted["value"],data)
            return extracted["value"]
        return None
    except:
        return None

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

# ── Kontext-System ───────────────────────────────────────────────────────────
# Regelplan: Wer ist wann verfuegbar / verantwortlich
DEFAULT_SCHEDULE={
    "karsten":{
        "work_days":["mo","di","mi","do","fr"],
        "work_hours":"9-18",
        "childcare_default": False,   # Karsten ist im Buero -> kann Marlene nicht abholen
    },
    "kate":{
        "teaching_days":["donnerstag","sonntag"],
        "teaching_hours":"10-13",
        "childcare_default": True,    # Kate ist normalerweise fuer Marlene zustaendig
    },
    "marlene":{
        "kita_start":"9:30",
        "kita_end":"16:00",
        "pickup_default":"kate",      # Wer holt normalerweise ab
        "kita_days":["mo","di","mi","do","fr"],
    }
}

def get_context(data=None):
    """Gibt den aktuellen dynamischen Kontext zurueck."""
    if data is None:
        data=load_data()
    return data.get("context",{"current":{},"schedule":{}})

def save_context(ctx,data=None):
    if data is None:
        data=load_data()
    data["context"]=ctx
    save_data(data)

def get_childcare_situation(data=None):
    """
    Analysiert den aktuellen Kontext und gibt zurueck wer Marlene betreut.
    Wenn Karsten auf Reise oder Buero -> Kate muss Kita-Pickup uebernehmen.
    Wenn Kate unterrichtet UND Karsten weg -> Problem, flaggen.
    """
    if data is None:
        data=load_data()
    ctx=get_context(data)
    current=ctx.get("current",{})
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    today_str=now.strftime("%Y-%m-%d")

    karsten_away=False
    kate_teaching=False
    karsten_status=""

    # Pruefen ob Karsten abwesend – mit automatischem Reset wenn until-Datum vorbei
    k_ctx=current.get("karsten",{})
    if k_ctx:
        status=k_ctx.get("status","")
        until=k_ctx.get("until","")
        if status in ["geschaeftsreise","reise","urlaub","abwesend","dienstreise"]:
            if until:
                try:
                    if len(until)==5:  # "03.06"
                        d,m=until.split(".")
                        until_dt=tz.localize(datetime(now.year,int(m),int(d)))
                    elif len(until)==10:  # "2026-06-03"
                        until_dt=tz.localize(datetime.fromisoformat(until))
                    else:
                        until_dt=now+timedelta(days=1)
                    # Abgelaufen? Kontext automatisch zuruecksetzen
                    if now>until_dt+timedelta(days=1):
                        ctx["current"]["karsten"]={}
                        save_context(ctx,data)
                        logger.info("Kontext Karsten automatisch zurueckgesetzt (Reise abgelaufen)")
                    else:
                        karsten_away=True
                        karsten_status=status
                except:
                    karsten_away=True
                    karsten_status=status
            else:
                karsten_away=True
                karsten_status=status

    # Pruefen ob Kate heute unterrichtet
    weekday_map={"mo":0,"di":1,"mi":2,"do":3,"fr":4,"sa":5,"so":6,
                 "montag":0,"dienstag":1,"mittwoch":2,"donnerstag":3,"freitag":4,"samstag":5,"sonntag":6}
    kate_teaching_days=DEFAULT_SCHEDULE["kate"]["teaching_days"]
    today_wd=now.weekday()
    for td in kate_teaching_days:
        if weekday_map.get(td.lower(),99)==today_wd:
            kate_teaching=True
            break

    # Kita nur Mo-Fr geoeffnet
    kita_open=today_wd<=4  # 0=Mo, 4=Fr, 5=Sa, 6=So

    result={
        "karsten_away":karsten_away,
        "karsten_status":karsten_status,
        "kate_teaching_today":kate_teaching,
        "kita_open":kita_open,
        "pickup_person":"kate",  # default
        "conflict":False,
        "conflict_reason":"",
    }
    if karsten_away and kita_open:
        result["pickup_person"]="kate"
        if kate_teaching:
            result["conflict"]=True
            result["conflict_reason"]="Karsten ist "+karsten_status+" und Kate unterrichtet heute – wer holt Marlene ab?"
    return result

def get_context_summary(data=None):
    """Gibt einen kurzen Kontext-String fuer GPT-Prompts zurueck."""
    if data is None:
        data=load_data()
    ctx=get_context(data)
    current=ctx.get("current",{})
    lines=[]

    for person,info in current.items():
        if info:
            status=info.get("status","")
            until=info.get("until","")
            note=info.get("note","")
            name=person.capitalize()
            line=f"- {name}: {status}"
            if until:
                line+=f" bis {until}"
            if note:
                line+=f" ({note})"
            lines.append(line)

    childcare=get_childcare_situation(data)
    if childcare["karsten_away"]:
        if childcare.get("kita_open"):
            lines.append(f"- Karsten abwesend ({childcare['karsten_status']}) → Kate ist fuer Marlene/Kita zustaendig")
        else:
            lines.append(f"- Karsten abwesend ({childcare['karsten_status']}) → Kita heute geschlossen (Wochenende/Feiertag)")
    if childcare["conflict"]:
        lines.append(f"⚠️ KONFLIKT: {childcare['conflict_reason']}")

    if not lines:
        return ""
    return "\nAktueller Kontext:\n"+"\n".join(lines)+"\n"

def update_context_from_message(user_message,user_id,data=None):
    """
    Erkennt automatisch Kontextaenderungen aus Nachrichten.
    z.B. 'Ich bin ab Montag auf Geschaeftsreise bis Freitag' -> speichert Kontext fuer Karsten.
    """
    if data is None:
        data=load_data()
    user_name=USER_NAMES.get(user_id,"").lower()
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)

    extract_prompt="""Analysiere die Nachricht auf Kontextaenderungen (Abwesenheiten, Reisen, Urlaub, Homeoffice, besondere Situationen).
Gib NUR JSON zurueck oder {}.
Format: {"person":"karsten/kate","status":"geschaeftsreise/urlaub/homeoffice/krank/normal","from":"YYYY-MM-DD","until":"YYYY-MM-DD","note":"..."}
Nur besetzen wenn eine echte Kontextaenderung erkannt wird. Sonst: {}
Heute: """+now.strftime("%Y-%m-%d")+"\nSchreiber: "+user_name+"\nNachricht: "+user_message
    try:
        response=client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"user","content":extract_prompt}],
            max_tokens=200,
            response_format={"type":"json_object"}
        )
        extracted=json.loads(response.choices[0].message.content)
        if not extracted or "person" not in extracted:
            return
        person=extracted.get("person","")
        if person not in ["karsten","kate"]:
            return
        ctx=get_context(data)
        if "current" not in ctx:
            ctx["current"]={}
        ctx["current"][person]={
            "status":extracted.get("status",""),
            "from":extracted.get("from",""),
            "until":extracted.get("until",""),
            "note":extracted.get("note",""),
            "updated":now.strftime("%Y-%m-%d %H:%M"),
        }
        save_context(ctx,data)

        # Wenn Karsten weg ist: Kate proaktiv informieren
        if person=="karsten" and extracted.get("status") in ["geschaeftsreise","reise","urlaub","abwesend","dienstreise"]:
            childcare=get_childcare_situation(data)
            if childcare.get("conflict"):
                return "⚠️ "+childcare["conflict_reason"]
        return None
    except:
        return None

async def notify_partner_if_needed(bot,user_id,context_change_msg,data=None):
    """Benachrichtigt den Partner wenn ein relevanter Kontext erkannt wurde."""
    if not context_change_msg:
        return
    user_info=USERS.get(user_id,{})
    partner_id=user_info.get("partner_id","")
    if not partner_id:
        return
    partner_lang=USERS.get(partner_id,{}).get("lang","de")
    childcare=get_childcare_situation(data)

    if partner_lang=="en":
        msg="📋 FYI: "+context_change_msg
        if childcare.get("karsten_away"):
            msg+="\n\nThis means you're responsible for Marlene's kindergarten pickup (by 16:00)."
        if childcare.get("conflict"):
            msg+="\n\n⚠️ "+childcare["conflict_reason"]
    else:
        msg="📋 Info: "+context_change_msg
        if childcare.get("karsten_away"):
            msg+="\n\nDas bedeutet: Du bist fuer Marlenes Kita-Abholung zustaendig (bis 16:00 Uhr)."
        if childcare.get("conflict"):
            msg+="\n\n⚠️ "+childcare["conflict_reason"]
    try:
        await bot.send_message(chat_id=int(partner_id),text=msg)
    except Exception as e:
        logger.error(f"Partner notify error: {e}")


# ── Self-Improvement System ──────────────────────────────────────────────────
IMPROVEMENTS_FILE="improvements.json"
FEEDBACK_TRIGGERS_DE=["das hat nicht geklappt","falsch","stimmt nicht","du verstehst","funktioniert nicht",
    "das ist falsch","wieder falsch","schon wieder","immer noch","kapierst du nicht","verstehst du nicht",
    "das war falsch","nicht richtig","klappt nicht","geht nicht"]
FEEDBACK_TRIGGERS_EN=["that's wrong","not working","you don't understand","still broken","wrong again",
    "doesn't work","that was wrong","not right","failed again","you keep","still not"]

def load_improvements():
    if os.path.exists(IMPROVEMENTS_FILE):
        with open(IMPROVEMENTS_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {"improvements":[]}

def save_improvements(data):
    with open(IMPROVEMENTS_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

def add_improvement(description,source="error",context="",status="open"):
    """Fügt einen Verbesserungsvorschlag hinzu. Verhindert Duplikate."""
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    data=load_improvements()
    # Duplikat-Check: ähnliche Einträge nicht doppelt speichern
    desc_low=description.lower()
    for item in data["improvements"]:
        if item.get("status")=="open" and desc_low[:50] in item.get("description","").lower():
            return  # schon vorhanden
    data["improvements"].append({
        "id":len(data["improvements"])+1,
        "date":now,
        "source":source,
        "description":description,
        "context":context,
        "status":"open"
    })
    save_improvements(data)
    logger.info(f"Improvement logged: {description[:60]}")

def log_error_as_improvement(error_msg,context_msg=""):
    """Wenn ein Fehler auftritt, GPT formuliert daraus einen Verbesserungsvorschlag."""
    try:
        prompt=("Ein Telegram-Bot hat folgenden Fehler gehabt. Formuliere in einem Satz was verbessert werden sollte.\n"
            "Fehler: "+error_msg+"\nKontext: "+context_msg+"\n"
            "Antworte NUR mit dem Verbesserungsvorschlag, max 100 Zeichen.")
        response=client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"user","content":prompt}],
            max_tokens=80
        )
        suggestion=response.choices[0].message.content.strip()
        add_improvement(suggestion,source="error",context=context_msg)
    except:
        add_improvement("Fehler: "+error_msg[:100],source="error",context=context_msg)

def detect_user_frustration(user_message,user_id,last_bot_response=""):
    """Erkennt wenn User frustriert ist und extrahiert was schiefging."""
    msg_low=user_message.lower()
    lang=USERS.get(user_id,{}).get("lang","de")
    triggers=FEEDBACK_TRIGGERS_DE if lang=="de" else FEEDBACK_TRIGGERS_EN
    if not any(t in msg_low for t in triggers):
        return
    try:
        prompt=("Ein Nutzer ist unzufrieden mit einem Telegram-Assistenten. "
            "Formuliere in einem Satz was der Bot verbessern sollte.\n"
            "Nutzer sagte: \'"+user_message+"\'\n"
            "Bot hatte vorher geantwortet: \'"+last_bot_response[:200]+"\'\n"
            "Antworte NUR mit dem Verbesserungsvorschlag auf Deutsch, max 120 Zeichen.")
        response=client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"user","content":prompt}],
            max_tokens=80
        )
        suggestion=response.choices[0].message.content.strip()
        user_name=USER_NAMES.get(user_id,"?")
        add_improvement(suggestion,source="user_feedback",
            context=f"{user_name}: \"{user_message[:80]}\"")
    except:
        pass

def get_improvements_summary(status_filter=None):
    """Gibt eine formatierte Liste der Verbesserungsvorschläge zurück."""
    data=load_improvements()
    items=data.get("improvements",[])
    if status_filter:
        items=[i for i in items if i.get("status")==status_filter]
    if not items:
        return "Keine Einträge."
    lines=[]
    for i in items:
        icon={"open":"🔴","in_progress":"🟡","fixed":"✅"}.get(i.get("status","open"),"⚪")
        lines.append(f"{icon} #{i['id']} [{i['date'][:10]}] ({i['source']})\n   {i['description']}")
    return "\n\n".join(lines)

def mark_improvement_fixed(item_id):
    """Markiert einen Verbesserungsvorschlag als erledigt."""
    data=load_improvements()
    for item in data["improvements"]:
        if item.get("id")==item_id:
            item["status"]="fixed"
            tz=pytz.timezone(TIMEZONE)
            item["fixed_date"]=datetime.now(tz).strftime("%Y-%m-%d")
            save_improvements(data)
            return True
    return False

# ── Intent Engine ─────────────────────────────────────────────────────────────
INTENT_SYSTEM="""Du bist ein Intent-Erkennungs-System. Analysiere die Nachricht und gib NUR JSON zurueck.
Karsten (ID: 281391093) spricht Deutsch. Kate (ID: 934428072) spricht Englisch.

Intents:
- create_event: Termin MIT Uhrzeit (z.B. Arzt 10 Uhr) ODER ganztaegiger Termin ohne Uhrzeit (z.B. Geschaeftsreise, Urlaub, Geburtstag)
- create_task: Aufgabe/Todo -> ganztaegig im Kalender mit ✅. Bei mehreren Aufgaben items-Liste verwenden: {"intent":"create_task","items":["Aufgabe 1","Aufgabe 2"],"date":"..."}. NIEMALS create_event fuer Aufgaben/Todos!
- create_list: Einkaufsliste -> ganztaegig mit 🛒
- create_note: Kurze Notiz
- query_events: Termine abfragen
- complete_task: Aufgabe als erledigt markieren (z.B. "erledigt", "done", "habe X gemacht", "fertig mit X")
- assign_task: Aufgabe jemandem zuweisen (z.B. "X macht Y", "Y ist fuer Kate", "Karsten uebernimmt Z")
- query_tasks: Offene Aufgaben anzeigen (z.B. "was steht noch aus", "offene todos", "open tasks")
- delete_event: Termin oder Aufgabe loeschen/absagen/stornieren (z.B. "loesch den Zahnarzt", "cancel dentist", "Termin X absagen")
- query_improvements: Verbesserungsliste anzeigen (z.B. "zeig Verbesserungen", "show improvements", "offene Bugs")
- add_improvement: Verbesserung manuell eintragen (z.B. "notiere: X klappt nicht", "merke dir als Bug: X", "add to improvements: X")
- adhd_emergency: Notfallmodus bei Ueberforderung (z.B. "ich komme nicht in die Gaenge", "ich weiss nicht wo anfangen", "overwhelmed", "zu viel", "ich schaffe das nicht", "can't get started", "I don't know where to begin")
- general: Alles andere

WICHTIG fuer Reisen/Urlaub/Abwesenheiten:
- Reisen, Geschaeftsreisen, Urlaub, Abwesenheiten IMMER als create_event mit allday:true
- Bei mehrtaegigen Ereignissen: date=Startdatum, end_date=letzter Tag (inklusiv), allday:true
- Keine Zeit angeben wenn allday:true

WICHTIG fuer Datumsberechnung – nutze die angegebene aktuelle Zeit:
- "heute" -> aktuelles Datum aus "Zeit:" Feld
- "morgen" -> aktuelles Datum + 1 Tag
- "uebermorgen" -> aktuelles Datum + 2 Tage
- "naechsten Montag/Dienstag/..." -> naechster entsprechender Wochentag
- "naechste Woche" -> aktuelles Datum + 7 Tage
- "in zwei Wochen" -> aktuelles Datum + 14 Tage
- IMMER konkretes YYYY-MM-DD berechnen und ausgeben, NIE relative Begriffe im date-Feld

WICHTIG fuer Verneinungen und Korrekturen:
- "Heute ist keine Geschaeftsreise", "ich habe keinen Termin", "das ist kein Meeting" -> intent: general (KEIN create_event!)
- "loesch", "absagen", "cancel", "stornieren", "entfernen", "remove" -> delete_event
- Verneinungen ("kein", "keine", "not a", "isn't") bei Terminen -> general, NIEMALS create_event

WICHTIG bei mehreren Terminen in einer Nachricht:
- Mehrere Termine -> intent: general, Bot nutzt KALENDER_TERMIN Format fuer jeden
- Termine MIT Uhrzeit: KALENDER_TERMIN:Titel|YYYY-MM-DD HH:MM
- Termine OHNE Uhrzeit (Reisen, Urlaub, Geburtstage): KALENDER_TERMIN:Titel|YYYY-MM-DD (NUR Datum, keine Uhrzeit!)
- Aufgaben/Todos: KALENDER_AUFGABE:Titel (ganztaegig, kein Datum noetig)

Affects: "self"=nur Schreiber, "partner"=nur Partner, "both"=beide

Format: {"intent":"create_event","title":"...","date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","time":"HH:MM","duration_hours":1,"allday":false,"affects":"self","text":"...","items":[]}"""

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
        end_date_str=intent_data.get("end_date")
        time_str=intent_data.get("time","")
        duration=intent_data.get("duration_hours",1)
        allday=intent_data.get("allday",False)
        if not date_str:
            return "Ich brauche noch ein Datum. Wann soll der Termin stattfinden?",None
        # Auto-detect allday: no time given, or explicitly flagged
        if not time_str or allday:
            try:
                start_d=date.fromisoformat(date_str)
                end_d=date.fromisoformat(end_date_str) if end_date_str else start_d
                ok=add_allday_event(title,datetime(start_d.year,start_d.month,start_d.day),
                                    datetime(end_d.year,end_d.month,end_d.day) if end_date_str else None)
                if ok:
                    if end_date_str and end_date_str!=date_str:
                        return "Eingetragen: "+title+" vom "+start_d.strftime("%d.%m.%Y")+" bis "+end_d.strftime("%d.%m.%Y"),None
                    else:
                        return "Eingetragen: "+title+" am "+start_d.strftime("%d.%m.%Y")+" (ganztaegig)",None
                return "Fehler beim Eintragen!",None
            except Exception as e:
                return "Fehler: "+str(e),None
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
        deadline=intent_data.get("date")
        lang=USERS.get(user_id,{}).get("lang","de")
        try:
            if deadline:
                d=date.fromisoformat(deadline)
                dt=datetime(d.year,d.month,d.day)
            else:
                dt=now
            items=intent_data.get("items",[])
            if not items:
                single=intent_data.get("text") or intent_data.get("title",("Aufgabe" if lang=="de" else "Task"))
                items=[single] if single else []
            if not items:
                return ("Was soll ich als Aufgabe eintragen?" if lang=="de" else "What task should I add?"),None
            saved=[]
            failed=[]
            for task_text in items:
                task_text=str(task_text).strip()
                if not task_text:
                    continue
                ok=add_allday_event("✅ "+task_text,dt)
                if ok:
                    saved.append(task_text)
                else:
                    failed.append(task_text)
            if saved and not failed:
                if len(saved)==1:
                    return ("Aufgabe eingetragen: ✅ " if lang=="de" else "Task added: ✅ ")+saved[0],None
                else:
                    label="✅ "+str(len(saved))+(" Aufgaben eingetragen:\n" if lang=="de" else " tasks added:\n")
                    return label+"\n".join("• "+t for t in saved),None
            elif saved and failed:
                return "✅ "+", ".join(saved)+"\n❌ "+("Fehler bei: " if lang=="de" else "Failed: ")+", ".join(failed),None
            return ("Fehler beim Eintragen!" if lang=="de" else "Error adding tasks!"),None
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

    elif intent=="query_tasks":
        lang=USERS.get(user_id,{}).get("lang","de")
        tasks=get_open_tasks(days_back=30)
        if not tasks:
            return ("Keine offenen Aufgaben! 🎉" if lang=="de" else "No open tasks! 🎉"),None
        lines=[]
        for i,t in enumerate(tasks,1):
            assignee=" → "+t["assignee"] if t["assignee"] else ""
            overdue=" ⚠️" if t["date"]<now.strftime("%Y-%m-%d") else ""
            lines.append(str(i)+". "+t["title"]+assignee+overdue)
        header=("Offene Aufgaben:" if lang=="de" else "Open tasks:")
        return header+"\n"+"\n".join(lines),None

    elif intent=="complete_task":
        title_hint=intent_data.get("title","") or intent_data.get("text","")
        tasks=get_open_tasks(days_back=30)
        if not tasks:
            return "Keine offenen Aufgaben gefunden.",None
        # Fuzzy match: find best matching task
        matched=None
        for t in tasks:
            if title_hint.lower() in t["title"].lower() or t["title"].lower() in title_hint.lower():
                matched=t
                break
        if not matched and tasks:
            # Try to match by number in text
            nums=re.findall(r"\d+",title_hint)
            if nums:
                idx=int(nums[0])-1
                if 0<=idx<len(tasks):
                    matched=tasks[idx]
        if matched:
            ok=complete_task(matched["id"],matched["title"])
            return ("✅✅ Erledigt: "+matched["title"] if ok else "Fehler beim Abhaken!"),None
        return "Welche Aufgabe meinst du? Schreib z.B. 'erledigt: "+tasks[0]["title"]+"'",None

    elif intent=="assign_task":
        title_hint=intent_data.get("title","") or intent_data.get("text","")
        assignee=intent_data.get("assignee","")
        tasks=get_open_tasks(days_back=30)
        matched=None
        for t in tasks:
            if title_hint.lower() in t["title"].lower():
                matched=t
                break
        if matched and assignee:
            ok=assign_task(matched["id"],matched["title"],assignee)
            return ("📌 "+matched["title"]+" → "+assignee if ok else "Fehler!"),None
        return "Bitte sag mir welche Aufgabe und wer sie übernimmt.",None

    elif intent=="delete_event":
        title_hint=intent_data.get("title","") or intent_data.get("text","")
        if not title_hint:
            return "Welchen Termin soll ich loeschen?",None
        matches=find_events_by_title(title_hint)
        if not matches:
            return "Keinen Termin gefunden mit '"+title_hint+"'. Schau mal in deinen Kalender?",None
        # Immer frische Daten laden fuer pending State
        fresh=load_data()
        if len(matches)==1:
            e=matches[0]
            event_title=e.get("summary","?")
            event_date=e["start"].get("date",e["start"].get("dateTime",""))[:10]
            fresh["pending_delete"]={"id":e["id"],"title":event_title,"date":event_date}
            if "pending_delete_list" in fresh:
                del fresh["pending_delete_list"]
            save_data(fresh)
            return "Soll ich '"+event_title+"' am "+event_date+" loeschen? Antworte mit JA oder NEIN.",None
        else:
            # Mehrere Treffer -> Liste zeigen, besten Treffer vorschlagen
            lines=["Mehrere Termine gefunden, welchen meinst du?"]
            candidates=[]
            for i,e in enumerate(matches[:5],1):
                t=e.get("summary","?")
                d=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                lines.append(str(i)+". "+t+" ("+d+")")
                candidates.append({"id":e["id"],"title":t,"date":d})
            lines.append("Antworte mit der Nummer (z.B. '1').")
            fresh["pending_delete_list"]=candidates
            if "pending_delete" in fresh:
                del fresh["pending_delete"]
            save_data(fresh)
            return "\n".join(lines),None

    elif intent=="query_improvements":
        text_low=intent_data.get("text","").lower()
        if any(w in text_low for w in ["fix","erledigt","done","closed"]):
            result=get_improvements_summary(status_filter="fixed")
            return "✅ Erledigte Verbesserungen:\n\n"+result,None
        result=get_improvements_summary(status_filter="open")
        total=len(load_improvements().get("improvements",[]))
        return "🔴 Offene Verbesserungen ("+str(total)+" gesamt):\n\n"+result,None

    elif intent=="add_improvement":
        description=intent_data.get("text","") or intent_data.get("title","")
        user_name=USER_NAMES.get(user_id,"?")
        if description:
            add_improvement(description,source="user_manual",context=user_name+": manuell eingetragen")
            return "📝 Notiert: "+description,None
        return "Was soll ich notieren?",None

    elif intent=="adhd_emergency":
        lang=USERS.get(user_id,{}).get("lang","de")
        user_name=USER_NAMES.get(user_id,"")
        tasks=get_open_tasks(days_back=60)
        tz_local=pytz.timezone(TIMEZONE)
        today=datetime.now(tz_local).strftime("%Y-%m-%d")
        overdue=[t for t in tasks if t["date"]<today]
        upcoming=[t for t in tasks if t["date"]>=today]
        priority=overdue[:3] if overdue else (upcoming[:3] if upcoming else tasks[:3])
        task_list="\n".join("- "+t["title"] for t in priority) if priority else ""
        try:
            if lang=="de":
                prompt=("Du bist ein ADHS-Coach. "+user_name+" fuehlt sich ueberfordert. "
                    "Antworte kurz (max 4 Zeilen), ruhig und motivierend auf Deutsch.\n"
                    +("Offene Aufgaben:\n"+task_list if task_list else "Keine Aufgaben offen.")+"\n"
                    "Nenne EINE kleinste konkrete naechste Handlung.")
            else:
                prompt=("You are an ADHD coach. "+user_name+" feels overwhelmed. "
                    "Reply briefly (max 4 lines), calm and motivating in English.\n"
                    +("Open tasks:\n"+task_list if task_list else "No open tasks.")+"\n"
                    "Name ONE smallest concrete next action.")
            resp=client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role":"user","content":prompt}],
                max_tokens=150
            )
            return resp.choices[0].message.content.strip(),None
        except:
            if lang=="de":
                return ("Erstmal durchatmen.\nFang hier an: "+priority[0]["title"] if priority else "Erstmal durchatmen. Du schaffst das."),None
            else:
                return ("Take a breath.\nStart here: "+priority[0]["title"] if priority else "Take a breath. You've got this."),None

    # Handle KALENDER_AUFGABE (tasks) - no collision check, no confirm
    if "KALENDER_AUFGABE:" in response:
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz)
        task_results=[]
        clean_lines=[]
        for line in response.strip().split("\n"):
            line=line.strip()
            if line.startswith("KALENDER_AUFGABE:"):
                title=line.replace("KALENDER_AUFGABE:","").strip()
                ok=add_allday_event("✅ "+title,now)
                task_results.append("✅ "+title if ok else "❌ Fehler: "+title)
            else:
                clean_lines.append(line)
        prefix="\n".join(t for t in task_results)
        rest="\n".join(l for l in clean_lines if l)
        return (prefix+"\n"+rest).strip() if rest else prefix,None
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
            end_dt_str=parts[2].strip() if len(parts)>2 else None
            tz=pytz.timezone(TIMEZONE)
            if len(dt_str)==10:
                # Date-only string -> all-day event (travel, holidays, etc.)
                d=date.fromisoformat(dt_str)
                end_d=date.fromisoformat(end_dt_str) if end_dt_str and len(end_dt_str)==10 else None
                ok=add_allday_event(title,datetime(d.year,d.month,d.day),
                                    datetime(end_d.year,end_d.month,end_d.day) if end_d else None)
                if ok:
                    if end_d and end_d!=d:
                        results.append("Eingetragen: "+title+" vom "+d.strftime("%d.%m.%Y")+" bis "+end_d.strftime("%d.%m.%Y"))
                    else:
                        results.append("Eingetragen: "+title+" am "+d.strftime("%d.%m.%Y")+" (ganztaegig)")
                else:
                    results.append("Fehler bei: "+title)
                continue
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
    context_summary=get_context_summary(context_data)
    childcare=get_childcare_situation(context_data)
    childcare_note=""
    if childcare["karsten_away"]:
        childcare_note="\nWICHTIG: Karsten ist "+childcare["karsten_status"]+" – Kate ist fuer Marlenes Kita-Abholung (bis 16:00) zustaendig!"
    if childcare["conflict"]:
        childcare_note+="\n\u26a0\ufe0f KONFLIKT: "+childcare["conflict_reason"]
    system=("Du bist ein KI-Assistent fuer ein Paar mit ADHS. Antworte in der Sprache des Nutzers. Kurz und klar, max 3-4 Saetze. "
        "Zeit: "+now_str+" Nutzer: "+user_name+"\nTermine:\n"+events+"\nNotizen:\n"+(notes if notes else "Keine")+
        "\nAufgaben:\n"+(todos if todos else "Keine")+get_memory_context()+get_facts_context()+context_summary+childcare_note+
        "\nWICHTIG: Wenn jemand Termine erstellen will, gib JEDEN Termin in einer eigenen Zeile aus. "
        "Format MIT Uhrzeit: KALENDER_TERMIN:Titel|YYYY-MM-DD HH:MM"
        "Format NUR Datum (Reisen/Urlaub/Geburtstage): KALENDER_TERMIN:Titel|YYYY-MM-DD"
        "Aufgaben/Todos: KALENDER_AUFGABE:Titel"
        "NIEMALS Reisen/Urlaub mit Uhrzeit eintragen! NIEMALS andere Texte wenn Termine erstellt werden!")
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
    data=load_data()
    ctx_summary=get_context_summary(data)
    childcare=get_childcare_situation(data)
    childcare_briefing=""
    if childcare["karsten_away"]:
        if lang=="de":
            childcare_briefing="\n\u26a0\ufe0f Karsten ist "+childcare["karsten_status"]+" – du bist heute fuer Marlenes Kita-Abholung zustaendig (bis 16:00 Uhr)!"
        else:
            childcare_briefing="\n\u26a0\ufe0f Karsten is "+childcare["karsten_status"]+" – you are responsible for Marlene's kindergarten pickup today (by 16:00)!"
    if childcare["conflict"]:
        childcare_briefing+="\n\u26a0\ufe0f "+childcare["conflict_reason"]
    if lang=="de":
        prompt=("Erstelle ein kurzes persoenliches Morgen-Briefing auf Deutsch fuer "+user_name+" (ADHS).\n"
            "Heute: "+weekday+" "+now.strftime("%d.%m.%Y")+"\nTermine:\n"+events+"\n"+memory_ctx+ctx_summary+childcare_briefing+
            "\nBriefing soll:\n- Mit 'Guten Morgen "+user_name+"!' beginnen\n- Heutigen Tag nennen\n"
            "- Relevante Termine erwaehnen\n- Kita/Betreuung erwaehnen wenn relevant\n- Koordination mit Partner\n- Offene Aufgaben\n- Morgen kurz\n- Max 8 Zeilen")
    else:
        prompt=("Create a short personal morning briefing in English for "+user_name+" (ADHD).\n"
            "Today: "+weekday+" "+now.strftime("%d.%m.%Y")+"\nEvents:\n"+events+"\n"+memory_ctx+ctx_summary+childcare_briefing+
            "\nBriefing should:\n- Start with 'Good morning "+user_name+"!'\n- Mention today\n"
            "- Relevant events\n- Mention childcare if relevant\n- Coordination with partner\n- Open tasks\n- Brief look tomorrow\n- Max 8 lines")
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


# ── Closed Loop ───────────────────────────────────────────────────────────────

def get_open_tasks(days_back=7):
    """Holt alle offenen (nicht erledigten) Aufgaben der letzten N Tage aus dem Kalender."""
    try:
        service=get_calendar_service()
        if not service:
            return []
        tz=pytz.timezone(TIMEZONE)
        now=datetime.now(tz)
        start=(now-timedelta(days=days_back)).isoformat()
        end=(now+timedelta(days=14)).isoformat()
        result=service.events().list(
            calendarId=CALENDAR_ID,timeMin=start,timeMax=end,
            maxResults=50,singleEvents=True,orderBy="startTime"
        ).execute()
        tasks=[]
        for e in result.get("items",[]):
            summary=e.get("summary","")
            # Offene Aufgaben: ✅ aber NICHT ✅✅ (doppelt = erledigt)
            if summary.startswith("✅") and not summary.startswith("✅✅"):
                event_date=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                assignee=e.get("description","").strip()  # wir speichern Zustaendigen in description
                tasks.append({
                    "id":e["id"],
                    "title":summary.replace("✅ ","").replace("✅","").strip(),
                    "date":event_date,
                    "assignee":assignee,
                    "raw":summary,
                })
        return tasks
    except Exception as e:
        logger.error(f"get_open_tasks error: {e}")
        return []

def complete_task(task_id,task_title):
    """Markiert eine Aufgabe als erledigt (Titel bekommt ✅✅)."""
    try:
        service=get_calendar_service()
        if not service:
            return False
        event=service.events().get(calendarId=CALENDAR_ID,eventId=task_id).execute()
        event["summary"]="✅✅ "+task_title  # doppeltes Haekchen = erledigt
        service.events().update(calendarId=CALENDAR_ID,eventId=task_id,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"complete_task error: {e}")
        return False

def assign_task(task_id,task_title,assignee_name):
    """Weist eine Aufgabe einer Person zu (speichert in description)."""
    try:
        service=get_calendar_service()
        if not service:
            return False
        event=service.events().get(calendarId=CALENDAR_ID,eventId=task_id).execute()
        event["description"]=assignee_name
        # Titel bekommt Namen wenn noch nicht drin
        if assignee_name.lower() not in event["summary"].lower():
            event["summary"]="✅ "+task_title+" ("+assignee_name+")"
        service.events().update(calendarId=CALENDAR_ID,eventId=task_id,body=event).execute()
        return True
    except Exception as e:
        logger.error(f"assign_task error: {e}")
        return False

def handle_checkin_response(text,tasks):
    """
    Parst die Antwort auf den Abend-Checkin.
    '1,3' -> Tasks 1 und 3 erledigt
    'alles' / 'all' -> alle erledigt
    'nichts' / 'nothing' -> keine erledigt
    '1 Kate, 2 Karsten' -> Zuweisung
    """
    txt=text.strip().lower()
    completed_indices=[]
    assignments={}  # index -> name

    if any(w in txt for w in ["alles","alle","all","everything","done"]):
        completed_indices=list(range(len(tasks)))
    elif any(w in txt for w in ["nichts","nix","nothing","keine","nope"]):
        completed_indices=[]
    else:
        # Zahlen extrahieren: "1,3" oder "1 und 3" oder "1 3"
        numbers=re.findall(r"\d+",txt)
        completed_indices=[int(n)-1 for n in numbers if 0<int(n)<=len(tasks)]

        # Zuweisungen erkennen: "3 kate" oder "aufgabe 3 fuer karsten"
        for name in ["karsten","kate"]:
            # suche "N name" oder "name N"
            matches=re.findall(r"(\d+)\s*"+name+r"|"+name+r"\s*(\d+)",txt)
            for m in matches:
                idx=int(m[0] or m[1])-1
                if 0<=idx<len(tasks):
                    assignments[idx]=name.capitalize()

    return completed_indices,assignments

async def send_task_list(bot,chat_id,tasks,header=""):
    """Sendet eine nummerierte Aufgabenliste."""
    if not tasks:
        await bot.send_message(chat_id=int(chat_id),text="✅ Keine offenen Aufgaben!")
        return
    msg=header+"\n\n" if header else ""
    for i,t in enumerate(tasks,1):
        assignee=" → "+t["assignee"] if t["assignee"] else ""
        overdue=""
        try:
            task_date=date.fromisoformat(t["date"])
            if task_date<date.today():
                overdue=" ⚠️ überfällig!"
        except:
            pass
        msg+=str(i)+". "+t["title"]+assignee+overdue+"\n"
    await bot.send_message(chat_id=int(chat_id),text=msg)

async def task_reminder(bot):
    """Mittags-Reminder (12 Uhr) für überfällige Aufgaben – nur wenn es welche gibt."""
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    tasks=get_open_tasks(days_back=30)
    overdue=[t for t in tasks if t["date"]<now.strftime("%Y-%m-%d")]
    if not overdue:
        return
    data=load_data()

    for uid,uinfo in USERS.items():
        lang=uinfo.get("lang","de")
        # Aufgaben für diesen User filtern (alle wenn kein Assignee, sonst nur seine)
        my_tasks=[t for t in overdue if not t["assignee"] or t["assignee"].lower()==uinfo["name"].lower()]
        if not my_tasks:
            continue
        if lang=="de":
            header=f"⏰ Reminder: {len(my_tasks)} überfällige Aufgabe(n):"
        else:
            header=f"⏰ Reminder: {len(my_tasks)} overdue task(s):"
        try:
            await send_task_list(bot,uid,my_tasks,header)
        except Exception as e:
            logger.error(f"Reminder error {uinfo['name']}: {e}")

async def weekly_review(bot):
    """Sonntags 18 Uhr: Wöchentliches Review aller offenen Aufgaben."""
    tasks=get_open_tasks(days_back=30)
    data=load_data()

    for uid,uinfo in USERS.items():
        lang=uinfo.get("lang","de")
        if lang=="de":
            header="📊 Wochenreview – offene Aufgaben:"
            footer="\n\nWas soll auf naechste Woche? Einfach antworten!"
        else:
            header="📊 Weekly Review – open tasks:"
            footer="\n\nWhat should move to next week? Just reply!"
        try:
            msg_tasks=tasks if tasks else []
            if not msg_tasks:
                await bot.send_message(chat_id=int(uid),
                    text="🎉 Super Woche! Alle Aufgaben erledigt." if lang=="de" else "🎉 Great week! All tasks done.")
            else:
                await send_task_list(bot,uid,msg_tasks,header)
                await bot.send_message(chat_id=int(uid),text=footer)
        except Exception as e:
            logger.error(f"Weekly review error {uinfo['name']}: {e}")

async def evening_checkin(bot):
    """20 Uhr: Abend-Checkin mit allen offenen Aufgaben."""
    tz=pytz.timezone(TIMEZONE)
    now=datetime.now(tz)
    tasks=get_open_tasks(days_back=7)
    if not tasks:
        return
    data=load_data()
    data["checkin_tasks"]=tasks
    save_data(data)
    for uid,uinfo in USERS.items():
        lang=uinfo.get("lang","de")
        my_tasks=[t for t in tasks if not t["assignee"] or t["assignee"].lower()==uinfo["name"].lower()]
        if not my_tasks:
            continue
        if lang=="de":
            header="\U0001f319 Guten Abend "+uinfo["name"]+"! Kurzes Check-in:"
            footer="\nWas hast du erledigt? z.B. '1,3' · 'alles' · 'nichts'\nOder zuweisen: '2 Kate' '3 Karsten'"
        else:
            header="\U0001f319 Good evening "+uinfo["name"]+"! Quick check-in:"
            footer="\nWhat did you finish? e.g. '1,3' · 'all' · 'nothing'\nOr assign: '2 Kate' '3 Karsten'"
        try:
            msg=header+"\n\n"
            for i,t in enumerate(my_tasks,1):
                overdue=" \u26a0\ufe0f" if t["date"]<now.strftime("%Y-%m-%d") else ""
                msg+=str(i)+". "+t["title"]+overdue+"\n"
            msg+=footer
            await bot.send_message(chat_id=int(uid),text=msg)
        except Exception as e:
            logger.error(f"Evening checkin error {uinfo['name']}: {e}")


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
        elif now.hour==18 and now.minute==0 and now.weekday()==6:  # Sonntag 18 Uhr
            await weekly_review(bot)
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
    # ── Loeschen bestaetigen ──
    pending_delete=data.get("pending_delete")
    pending_delete_list=data.get("pending_delete_list")
    if pending_delete:
        txt=text.strip().upper().replace("!","").replace(".","").strip()
        if any(txt==w or txt.startswith(w) for w in JA_WORDS):
            try:
                ok=delete_calendar_event(pending_delete["id"])
                msg="🗑️ Gelöscht: "+pending_delete["title"] if ok else "Fehler beim Löschen!"
            except Exception as ex:
                msg="Fehler beim Löschen: "+str(ex)
            for key in ["pending_delete","pending_delete_list"]:
                data.pop(key,None)
            save_data(data)
            await update.message.reply_text(msg)
            return
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            for key in ["pending_delete","pending_delete_list"]:
                data.pop(key,None)
            save_data(data)
            await update.message.reply_text("OK, Termin bleibt.")
            return
        # Wenn weder JA noch NEIN: State behalten, weitermachen
    if pending_delete_list:
        txt_upper=text.strip().upper().replace("!","").replace(".","").strip()
        txt_low=text.strip().lower()
        # Explizites NEIN ohne Lösch-Keyword -> abbrechen
        is_nein=any(txt_upper==w or txt_upper.startswith(w) for w in NEIN_WORDS)
        has_delete_kw=any(kw in txt_low for kw in ["lösch","loesch","delete","entfern","cancel","ja","yes"])
        if is_nein and not has_delete_kw:
            data.pop("pending_delete_list",None)
            save_data(data)
            await update.message.reply_text("OK, nichts gelöscht.")
            return
        # "heute"/"today" -> heutiges Datum matchen
        tz=pytz.timezone(TIMEZONE)
        today_str=datetime.now(tz).strftime("%Y-%m-%d")
        if any(w in txt_low for w in ["heute","today"]):
            for i,e in enumerate(pending_delete_list):
                if e.get("date","")==today_str:
                    data["pending_delete"]=e
                    data.pop("pending_delete_list",None)
                    save_data(data)
                    await update.message.reply_text("Soll ich '"+e["title"]+"' am "+e["date"]+" löschen? JA oder NEIN.")
                    return
            await update.message.reply_text("Kein Treffer für heute. Bitte Nummer eingeben (1-"+str(len(pending_delete_list))+").")
            return
        # Datum direkt nennen z.B. "24.05" oder "2026-06-02"
        date_match=re.search(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?",text)
        if date_match:
            d,m=int(date_match.group(1)),int(date_match.group(2))
            yr=int(date_match.group(3)) if date_match.group(3) else datetime.now(tz).year
            if yr<100: yr+=2000
            try:
                target=f"{yr:04d}-{m:02d}-{d:02d}"
                for e in pending_delete_list:
                    if e.get("date","")==target:
                        data["pending_delete"]=e
                        data.pop("pending_delete_list",None)
                        save_data(data)
                        await update.message.reply_text("Soll ich '"+e["title"]+"' am "+e["date"]+" löschen? JA oder NEIN.")
                        return
            except:
                pass
        nums=re.findall(r"\d+",text)
        if nums:
            try:
                idx=int(nums[0])-1
                if 0<=idx<len(pending_delete_list):
                    e=pending_delete_list[idx]
                    data["pending_delete"]=e
                    data.pop("pending_delete_list",None)
                    save_data(data)
                    await update.message.reply_text("Soll ich '"+e["title"]+"' am "+e["date"]+" löschen? JA oder NEIN.")
                    return
                else:
                    await update.message.reply_text("Ungültige Nummer. Bitte 1-"+str(len(pending_delete_list))+" eingeben.")
                    return
            except Exception:
                pass
    if any(kw in text.lower() for kw in BRIEFING_KEYWORDS):
        await generate_briefing(context.bot,target_user_id=user_id)
        return
    # ── Frustration zuerst prüfen (vor Intent-Erkennung) ──
    last_bot_pre=data["conversation"][-1]["content"] if data.get("conversation") else ""
    FRUSTRATION_TRIGGERS=["hat nicht geklappt","funktioniert nicht","falsch","stimmt nicht",
        "du verstehst","schon wieder","immer noch","kapierst","not working","wrong","doesn't work",
        "still broken","failed","that was wrong","nicht richtig","klappt nicht"]
    if any(t in text.lower() for t in FRUSTRATION_TRIGGERS):
        detect_user_frustration(text,user_id,last_bot_pre)
    # ── Direktes Löschen per Keyword (umgeht GPT-Intent) ──
    DELETE_KEYWORDS=["lösch","loesch","lösche","loeschen","delete","entfern","entfernen","absagen","stornieren","cancel","remove"]
    txt_low=text.lower()
    if any(kw in txt_low for kw in DELETE_KEYWORDS) and not data.get("pending_delete"):
        # Suchbegriff extrahieren: alles nach dem Delete-Keyword
        title_hint=""
        matched_kw=""
        for kw in DELETE_KEYWORDS:
            if kw in txt_low:
                after=txt_low.split(kw,1)[-1].strip()
                before=txt_low.split(kw,1)[0].strip()
                matched_kw=kw
                # Alles nach dem Keyword nehmen; wenn leer, Wörter davor nehmen
                title_hint=after if after else before
                break
        # Füllwörter entfernen
        STOP=["den","die","das","den termin","die aufgabe","bitte","mal","doch","den eintrag",
              "the","please","the appointment","the task","kannst du","can you","würdest du"]
        for fw in STOP:
            title_hint=title_hint.replace(fw,"").strip()
        # Satzzeichen weg
        title_hint=title_hint.strip("?.!,")
        if title_hint and len(title_hint)>1:
            await update.message.chat.send_action("typing")
            matches=find_events_by_title(title_hint)
            if not matches:
                await update.message.reply_text("Keinen Termin gefunden mit \'"+title_hint+"\'. Gibt es ihn noch im Kalender?")
                return
            if len(matches)==1:
                e=matches[0]
                event_title=e.get("summary","?")
                event_date=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                data["pending_delete"]={"id":e["id"],"title":event_title,"date":event_date}
                data.pop("pending_delete_list",None)
                save_data(data)
                await update.message.reply_text("Soll ich '"+event_title+"' am "+event_date+" löschen? JA oder NEIN.")
                return
            else:
                lines=["Mehrere Termine gefunden:"]
                candidates=[]
                for i,e in enumerate(matches[:5],1):
                    t=e.get("summary","?")
                    d=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                    lines.append(str(i)+". "+t+" ("+d+")")
                    candidates.append({"id":e["id"],"title":t,"date":d})
                lines.append("Welchen? (Nummer eingeben)")
                data["pending_delete_list"]=candidates
                data.pop("pending_delete",None)
                save_data(data)
                await update.message.reply_text("\n".join(lines))
                return
    # ── Checkin-Antwort verarbeiten ──
    checkin_tasks=data.get("checkin_tasks",[])
    if checkin_tasks:
        txt_low=text.strip().lower()
        is_checkin_reply=(
            any(w in txt_low for w in ["alles","alle","all","nichts","nix","nothing","done","erledigt","fertig"]) or
            bool(re.search(r"^\s*[\d,\s]+\s*$",txt_low)) or
            bool(re.search(r"\d+\s*(kate|karsten)",txt_low)) or
            bool(re.search(r"(kate|karsten)\s*\d+",txt_low))
        )
        if is_checkin_reply:
            completed_idx,assignments=handle_checkin_response(text,checkin_tasks)
            msgs=[]
            for idx,task in enumerate(checkin_tasks):
                if idx in assignments:
                    assign_task(task["id"],task["title"],assignments[idx])
                    msgs.append("📌 "+task["title"]+" → "+assignments[idx])
                elif idx in completed_idx:
                    complete_task(task["id"],task["title"])
                    msgs.append("✅✅ "+task["title"])
            if not msgs:
                msgs=["OK, nichts markiert." if USERS.get(user_id,{}).get("lang")=="de" else "OK, nothing marked."]
            del data["checkin_tasks"]
            save_data(data)
            await update.message.reply_text("\n".join(msgs))
            return
    await update.message.chat.send_action("typing")
    intent_data=detect_intent(text,user_id,data)
    intent=intent_data.get("intent","general")
    pending=None
    try:
        if intent in ["create_event","create_task","create_list","create_note","query_events","complete_task","assign_task","query_tasks","delete_event","query_improvements","add_improvement","adhd_emergency","adhd_emergency"]:
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
    except Exception as ex:
        log_error_as_improvement(str(ex),f"User: {text[:80]} | Intent: {intent}")
        response="Entschuldigung, da ist etwas schiefgelaufen. Ich habe es notiert."
    if pending:
        data["pending_event"]=pending
    update_memory_from_message(text,user_id,response)
    ctx_change=update_context_from_message(text,user_id,data)
    saved_fact=detect_and_save_fact(text,user_id,data)
    if saved_fact:
        response+="\n\n💾 Gespeichert: "+saved_fact
    # Frustrations-Erkennung: User unzufrieden?
    last_bot=data["conversation"][-1]["content"] if data.get("conversation") else ""
    detect_user_frustration(text,user_id,last_bot)
    data["conversation"].append({"role":"user","content":text})
    data["conversation"].append({"role":"assistant","content":response})
    if len(data["conversation"])>20:
        data["conversation"]=data["conversation"][-20:]
    save_data(data)
    await update.message.reply_text(response)
    await notify_partner_if_needed(context.bot,user_id,ctx_change,data)

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
    # ── Pending Delete (Sprache) ──
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
            await update.message.reply_text(msg)
            return
        elif any(txt==w or txt.startswith(w) for w in NEIN_WORDS):
            for key in ["pending_delete","pending_delete_list"]:
                data.pop(key,None)
            save_data(data)
            await update.message.reply_text("OK, Termin bleibt.")
            return
    if any(kw in text.lower() for kw in BRIEFING_KEYWORDS):
        await generate_briefing(context.bot,target_user_id=user_id)
        return
    # ── Frustration prüfen ──
    last_bot_pre=data["conversation"][-1]["content"] if data.get("conversation") else ""
    FRUSTRATION_TRIGGERS=["hat nicht geklappt","funktioniert nicht","falsch","stimmt nicht",
        "du verstehst","schon wieder","immer noch","not working","wrong","doesn't work","failed"]
    if any(t in text.lower() for t in FRUSTRATION_TRIGGERS):
        detect_user_frustration(text,user_id,last_bot_pre)
    # ── Direktes Löschen per Keyword (Sprache) ──
    DELETE_KEYWORDS=["lösch","loesch","lösche","loeschen","delete","entfern","entfernen","absagen","stornieren","cancel","remove"]
    txt_low=text.lower()
    if any(kw in txt_low for kw in DELETE_KEYWORDS) and not data.get("pending_delete"):
        title_hint=""
        for kw in DELETE_KEYWORDS:
            if kw in txt_low:
                after=txt_low.split(kw,1)[-1].strip()
                before=txt_low.split(kw,1)[0].strip()
                title_hint=after if after else before
                break
        STOP=["den","die","das","den termin","die aufgabe","bitte","mal","doch",
              "the","please","the appointment","kannst du","can you","bitte"]
        for fw in STOP:
            title_hint=title_hint.replace(fw,"").strip()
        title_hint=title_hint.strip("?.!,")
        if title_hint and len(title_hint)>1:
            matches=find_events_by_title(title_hint)
            if not matches:
                await update.message.reply_text("Keinen Termin gefunden mit '"+title_hint+"'.")
                return
            if len(matches)==1:
                e=matches[0]
                event_title=e.get("summary","?")
                event_date=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                data["pending_delete"]={"id":e["id"],"title":event_title,"date":event_date}
                data.pop("pending_delete_list",None)
                save_data(data)
                await update.message.reply_text("Soll ich '"+event_title+"' am "+event_date+" löschen? JA oder NEIN.")
                return
            else:
                lines=["Mehrere Termine gefunden:"]
                candidates=[]
                for i,e in enumerate(matches[:5],1):
                    t=e.get("summary","?")
                    d=e["start"].get("date",e["start"].get("dateTime",""))[:10]
                    lines.append(str(i)+". "+t+" ("+d+")")
                    candidates.append({"id":e["id"],"title":t,"date":d})
                lines.append("Welchen? (Nummer)")
                data["pending_delete_list"]=candidates
                data.pop("pending_delete",None)
                save_data(data)
                await update.message.reply_text("\n".join(lines))
                return
    intent_data=detect_intent(text,user_id,data)
    intent=intent_data.get("intent","general")
    pending=None
    try:
        if intent in ["create_event","create_task","create_list","create_note","query_events","complete_task","assign_task","query_tasks","delete_event","query_improvements","add_improvement","adhd_emergency"]:
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
    except Exception as ex:
        log_error_as_improvement(str(ex),f"Voice: {text[:80]} | Intent: {intent}")
        response="Entschuldigung, da ist etwas schiefgelaufen. Ich habe es notiert."
    if pending:
        data["pending_event"]=pending
    update_memory_from_message(text,user_id,response)
    ctx_change=update_context_from_message(text,user_id,data)
    saved_fact=detect_and_save_fact(text,user_id,data)
    if saved_fact:
        response+="\n\n💾 Gespeichert: "+saved_fact
    data["conversation"].append({"role":"user","content":text})
    data["conversation"].append({"role":"assistant","content":response})
    if len(data["conversation"])>20:
        data["conversation"]=data["conversation"][-20:]
    save_data(data)
    await update.message.reply_text(response)
    await notify_partner_if_needed(context.bot,user_id,ctx_change,data)

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
    print("Agent laeuft!")
    app.run_polling()

if __name__=="__main__":
    main()