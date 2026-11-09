import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import hmac
from contextlib import contextmanager
import sqlite3
import time as pytime
from datetime import datetime, time, timedelta

import pytz
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "asusmac")
SUBJECT_ADMIN_PASSWORD = os.getenv("SUBJECT_ADMIN_PASSWORD", "hppro")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  

TZ = pytz.timezone("Asia/Tashkent")
DB_FILE = "class_bot.db"
EMAKTAB_URL = "https://emaktab.uz"
REMINDER_TIME = time(20, 0, tzinfo=TZ)
AI_USER_LIMIT = 7
AI_GLOBAL_LIMIT = 150
MAX_PW_TRIES = 3
LOCK_MINUTES = 10

DAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]

SCHEDULE = {
    0: ["Kelajak soati", "Rus tili", "Texnologiya", "Biologiya", "Algebra", "Ona tili"],
    1: ["Geografiya", "O'zbekiston tarixi", "Informatika", "Geometriya", "Ingliz tili", "Kimyo"],
    2: ["Huquq", "Adabiyot", "Rus tili", "Algebra", "Informatika", "Jahon tarixi"],
    3: ["Ingliz tili", "Kimyo", "Ona tili", "Tarbiya", "Jismoniy tarbiya"],
    4: ["Biologiya", "Fizika", "Jismoniy tarbiya", "O'zbekiston tarixi", "Geometriya", "Geografiya"],
    5: ["Algebra", "Ingliz tili", "Chizmachilik", "Ona tili", "Adabiyot", "Fizika"],
}

BELLS = [
    (time(8, 30), time(9, 15)),
    (time(9, 20), time(10, 5)),
    (time(10, 10), time(10, 55)),
    (time(11, 0), time(11, 45)),
    (time(12, 5), time(12, 50)),
    (time(12, 55), time(13, 40)),
]

ABOUT_TEXT = (
    "ℹ️ Bot haqida\n\n"
    "Bu bot sinfimiz uchun yaratilgan yordamchi:\n"
    "📚 dars jadvali (bugungi va haftalik)\n"
    "⏰ hozir qaysi dars ketayotgani\n"
    "🔔 har kuni ertangi jadval eslatmasi\n"
    "🤖 AI yordamchi (kuniga cheklangan)\n\n"
    "Bot bepul va faqat o'quvchilar uchun.\n\n"
    "<b>Bot yaratuvchi: Eshdavlatov Salmon</b>"
)

AI_SYSTEM_PROMPT = (
    "Sen maktab o'quvchilariga yordam beradigan o'quv yordamchisan. "
    "agar senga qo'pol sozlar ishlatsa yoki seni kim yaratganlik haqida sorasa , Men senga o'xshagan "
    "dangasalarga qulaylik bolsin deb yaratilganman deb ayt"
    "O'zbek tilida (o'quvchi boshqa tilda yozsa, o'sha tilda) qisqa va tushunarli javob ber. "
    "Faqat o'quv mavzulariga javob ber. Uy vazifasini shunchaki tayyor yechib berma, "
    "yechish yo'lini tushuntir. Javob 150 so'zdan oshmasin."
)

def now():
    return datetime.now(TZ)

# ---------------------------------------------------------------
# BAZA (SQLite)
# ---------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row  
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                name TEXT, 
                username TEXT,
                created_at TEXT,
                reminder INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        c.execute("CREATE TABLE IF NOT EXISTS subject_admins (user_id INTEGER PRIMARY KEY)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage (
                user_id INTEGER, 
                day TEXT, 
                count INTEGER, 
                PRIMARY KEY (user_id, day)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ai_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                prompt TEXT,
                response TEXT,
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS homework (
                subject TEXT PRIMARY KEY,
                task_text TEXT,
                updated_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS emaktab_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER,
                parent_login TEXT,
                parent_password TEXT,
                added_at TEXT
            )
        """)

def save_user(user):
    current_time = now().strftime("%Y-%m-%d %H:%M")
    with db() as c:
        c.execute("""
            INSERT INTO users (user_id, name, username, created_at) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                name=excluded.name, 
                username=excluded.username
        """, (user.id, user.first_name or "", user.username or "", current_time))

def get_reminder(user_id):
    with db() as c:
        row = c.execute("SELECT reminder FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row["reminder"]) if row else True

def toggle_reminder(user_id):
    new = 0 if get_reminder(user_id) else 1
    with db() as c:
        c.execute("UPDATE users SET reminder=? WHERE user_id=?", (new, user_id))
    return bool(new)

def reminder_users():
    with db() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE reminder=1")]

def all_users():
    with db() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users")]

def get_all_user_records():
    with db() as c:
        return c.execute("SELECT user_id, name, username FROM users").fetchall()

def get_user_by_id(user_id):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def is_admin(user_id):
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None

def set_admin(user_id, value):
    with db() as c:
        if value:
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        else:
            c.execute("DELETE FROM admins WHERE user_id=?", (user_id,))

def is_subject_admin(user_id):
    with db() as c:
        return c.execute("SELECT 1 FROM subject_admins WHERE user_id=?", (user_id,)).fetchone() is not None

def set_subject_admin(user_id, value):
    with db() as c:
        if value:
            c.execute("INSERT OR IGNORE INTO subject_admins (user_id) VALUES (?)", (user_id,))
        else:
            c.execute("DELETE FROM subject_admins WHERE user_id=?", (user_id,))

def ai_used_today(user_id):
    day = now().strftime("%Y-%m-%d")
    with db() as c:
        row = c.execute("SELECT count FROM ai_usage WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        total = c.execute("SELECT COALESCE(SUM(count),0) FROM ai_usage WHERE day=?", (day,)).fetchone()[0]
    return (row["count"] if row else 0), total

def ai_add_usage(user_id, prompt, response):
    day = now().strftime("%Y-%m-%d")
    current_time = now().strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        c.execute("INSERT INTO ai_usage (user_id, day, count) VALUES (?, ?, 1) "
                  "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1", (user_id, day))
        c.execute("INSERT INTO ai_history (user_id, prompt, response, created_at) VALUES (?, ?, ?, ?)",
                  (user_id, prompt, response, current_time))
        c.execute("DELETE FROM ai_usage WHERE day < ?", ((now() - timedelta(days=3)).strftime("%Y-%m-%d"),))

# ---------------------------------------------------------------
# JADVAL VA VAZIFA MATNLARI
# ---------------------------------------------------------------
def fmt(t):
    return t.strftime("%H:%M")

def day_text(weekday, prefix="📅"):
    lessons = SCHEDULE.get(weekday)
    if not lessons:
        return f"{prefix} {DAYS[weekday]}: dars yo'q, dam olish kuni 😊"
    lines = [f"{prefix} {DAYS[weekday]}\n"]
    for i, subject in enumerate(lessons):
        start, end = BELLS[i]
        lines.append(f"{i + 1}. {subject} ({fmt(start)}–{fmt(end)})")
    return "\n".join(lines)

def week_text():
    return "\n\n".join(day_text(d) for d in range(6))

def current_lesson_text():
    n_ = now()
    lessons = SCHEDULE.get(n_.weekday())
    if not lessons:
        return "Bugun dars yo'q, dam olish kuni 😊"

    t = n_.time()
    n = len(lessons)
    if t < BELLS[0][0]:
        return f"Darslar hali boshlanmadi. 1-dars {fmt(BELLS[0][0])} da: {lessons[0]}"

    for i in range(n):
        start, end = BELLS[i]
        if start <= t < end:
            mins_left = int((datetime.combine(n_.date(), end) -
                             datetime.combine(n_.date(), t)).total_seconds() // 60)
            text = f"⏰ Hozir {i + 1}-dars: {lessons[i]}\nTugashiga ~{mins_left} daqiqa qoldi."
            if i + 1 < n:
                text += f"\nKeyingi dars: {lessons[i + 1]}"
            return text
        if i + 1 < n and end <= t < BELLS[i + 1][0]:
            return (f"🔔 Hozir tanaffus.\n"
                    f"Keyingi dars ({i + 2}-dars) {fmt(BELLS[i + 1][0])} da: {lessons[i + 1]}")
    return "Bugungi darslar tugadi ✅"

# ---------------------------------------------------------------
# AI (Gemini REST API)
# ---------------------------------------------------------------
def ask_ai(question):
    if not GEMINI_API_KEY:
