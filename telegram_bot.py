#!/usr/bin/env python3
"""
Telegram Bot – Notizen & Aufgaben
Abhängigkeiten: pip install python-telegram-bot
"""

import json
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters
)

# ── Datenpersistenz ──────────────────────────────────────────────────────────
DATA_FILE = "bot_data.json"

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user_data(user_id: str) -> dict:
    data = load_data()
    if user_id not in data:
        data[user_id] = {"notes": [], "todos": []}
        save_data(data)
    return data[user_id]

def update_user_data(user_id: str, user_data: dict):
    data = load_data()
    data[user_id] = user_data
    save_data(data)

# ── Zustände für ConversationHandler ─────────────────────────────────────────
WAITING_NOTE = 1
WAITING_TODO = 2

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Willkommen bei deinem persönlichen Assistenten!*\n\n"
        "Hier sind alle verfügbaren Befehle:\n\n"
        "📝 *Notizen*\n"
        "  /notiz\_neu – Neue Notiz erstellen\n"
        "  /notizen – Alle Notizen anzeigen\n"
        "  /notiz\_loeschen – Notiz löschen\n\n"
        "✅ *Aufgaben (To-Do)*\n"
        "  /todo\_neu – Neue Aufgabe erstellen\n"
        "  /todos – Alle Aufgaben anzeigen\n"
        "  /todo\_erledigt – Aufgabe als erledigt markieren\n"
        "  /todo\_loeschen – Aufgabe löschen\n\n"
        "ℹ️  /hilfe – Diese Hilfe erneut anzeigen"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def hilfe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ── NOTIZEN ───────────────────────────────────────────────────────────────────
async def notiz_neu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Bitte gib den Text deiner Notiz ein:")
    return WAITING_NOTE

async def notiz_neu_speichern(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    notiz = {
        "id": len(ud["notes"]) + 1,
        "text": update.message.text,
        "datum": datetime.now().strftime("%d.%m.%Y %H:%M")
    }
    ud["notes"].append(notiz)
    update_user_data(uid, ud)
    await update.message.reply_text(f"✅ Notiz gespeichert!\n\n_{notiz['text']}_", parse_mode="Markdown")
    return ConversationHandler.END

async def notizen_anzeigen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    if not ud["notes"]:
        await update.message.reply_text("📭 Keine Notizen vorhanden.")
        return
    text = "📝 *Deine Notizen:*\n\n"
    for n in ud["notes"]:
        text += f"*#{n['id']}* – {n['datum']}\n{n['text']}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def notiz_loeschen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    if not ud["notes"]:
        await update.message.reply_text("📭 Keine Notizen zum Löschen.")
        return
    keyboard = [
        [InlineKeyboardButton(f"#{n['id']}: {n['text'][:30]}…" if len(n['text']) > 30 else f"#{n['id']}: {n['text']}", callback_data=f"del_note_{n['id']}")]
        for n in ud["notes"]
    ]
    await update.message.reply_text(
        "🗑️ Welche Notiz möchtest du löschen?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ── TO-DO ─────────────────────────────────────────────────────────────────────
async def todo_neu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bitte gib deine neue Aufgabe ein:")
    return WAITING_TODO

async def todo_neu_speichern(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    todo = {
        "id": len(ud["todos"]) + 1,
        "text": update.message.text,
        "erledigt": False,
        "datum": datetime.now().strftime("%d.%m.%Y %H:%M")
    }
    ud["todos"].append(todo)
    update_user_data(uid, ud)
    await update.message.reply_text(f"✅ Aufgabe gespeichert!\n\n_{todo['text']}_", parse_mode="Markdown")
    return ConversationHandler.END

async def todos_anzeigen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    if not ud["todos"]:
        await update.message.reply_text("📭 Keine Aufgaben vorhanden.")
        return
    offen = [t for t in ud["todos"] if not t["erledigt"]]
    erledigt = [t for t in ud["todos"] if t["erledigt"]]
    text = "📋 *Deine Aufgaben:*\n\n"
    if offen:
        text += "⏳ *Offen:*\n"
        for t in offen:
            text += f"  ▪️ *#{t['id']}* {t['text']}\n"
    if erledigt:
        text += "\n✅ *Erledigt:*\n"
        for t in erledigt:
            text += f"  ~~#{t['id']} {t['text']}~~\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def todo_erledigt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    offen = [t for t in ud["todos"] if not t["erledigt"]]
    if not offen:
        await update.message.reply_text("🎉 Alle Aufgaben sind bereits erledigt!")
        return
    keyboard = [
        [InlineKeyboardButton(f"#{t['id']}: {t['text'][:30]}…" if len(t['text']) > 30 else f"#{t['id']}: {t['text']}", callback_data=f"done_todo_{t['id']}")]
        for t in offen
    ]
    await update.message.reply_text(
        "✅ Welche Aufgabe ist erledigt?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def todo_loeschen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ud = get_user_data(uid)
    if not ud["todos"]:
        await update.message.reply_text("📭 Keine Aufgaben zum Löschen.")
        return
    keyboard = [
        [InlineKeyboardButton(f"#{t['id']}: {t['text'][:30]}…" if len(t['text']) > 30 else f"#{t['id']}: {t['text']}", callback_data=f"del_todo_{t['id']}")]
        for t in ud["todos"]
    ]
    await update.message.reply_text(
        "🗑️ Welche Aufgabe möchtest du löschen?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ── Callback-Handler (Inline-Buttons) ────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    ud = get_user_data(uid)
    data = query.data

    if data.startswith("del_note_"):
        nid = int(data.split("_")[-1])
        ud["notes"] = [n for n in ud["notes"] if n["id"] != nid]
        update_user_data(uid, ud)
        await query.edit_message_text(f"🗑️ Notiz #{nid} wurde gelöscht.")

    elif data.startswith("done_todo_"):
        tid = int(data.split("_")[-1])
        for t in ud["todos"]:
            if t["id"] == tid:
                t["erledigt"] = True
        update_user_data(uid, ud)
        await query.edit_message_text(f"✅ Aufgabe #{tid} als erledigt markiert!")

    elif data.startswith("del_todo_"):
        tid = int(data.split("_")[-1])
        ud["todos"] = [t for t in ud["todos"] if t["id"] != tid]
        update_user_data(uid, ud)
        await query.edit_message_text(f"🗑️ Aufgabe #{tid} wurde gelöscht.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Abgebrochen.")
    return ConversationHandler.END

# ── Hauptprogramm ─────────────────────────────────────────────────────────────
def main():
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        print("❌ Bitte setze die Umgebungsvariable TELEGRAM_BOT_TOKEN")
        print("   Beispiel: export TELEGRAM_BOT_TOKEN='dein_token_hier'")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    # ConversationHandler für Notizen
    notiz_conv = ConversationHandler(
        entry_points=[CommandHandler("notiz_neu", notiz_neu_start)],
        states={WAITING_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, notiz_neu_speichern)]},
        fallbacks=[CommandHandler("abbrechen", cancel)]
    )

    # ConversationHandler für Todos
    todo_conv = ConversationHandler(
        entry_points=[CommandHandler("todo_neu", todo_neu_start)],
        states={WAITING_TODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, todo_neu_speichern)]},
        fallbacks=[CommandHandler("abbrechen", cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hilfe", hilfe))
    app.add_handler(notiz_conv)
    app.add_handler(todo_conv)
    app.add_handler(CommandHandler("notizen", notizen_anzeigen))
    app.add_handler(CommandHandler("notiz_loeschen", notiz_loeschen))
    app.add_handler(CommandHandler("todos", todos_anzeigen))
    app.add_handler(CommandHandler("todo_erledigt", todo_erledigt))
    app.add_handler(CommandHandler("todo_loeschen", todo_loeschen))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Bot läuft... Drücke Ctrl+C zum Beenden.")
    app.run_polling()

if __name__ == "__main__":
    main()
