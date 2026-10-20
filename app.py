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
AI_USER_LIMIT = 7  # Limit 7 taga oshirildi
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
        # AI so'rovlari tarixini saqlash uchun jadval
        c.execute("""
            CREATE TABLE IF NOT EXISTS ai_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                prompt TEXT,
                response TEXT,
                created_at TEXT
            )
        """)
        # Ona tili va adabiyot uyga vazifalari uchun jadval
        c.execute("""
            CREATE TABLE IF NOT EXISTS homework (
                subject TEXT PRIMARY KEY,
                task_text TEXT,
                updated_at TEXT
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
        return "AI hali sozlanmagan.", True
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": AI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": question[:1000]}]}],
        "generationConfig": {"maxOutputTokens": 600},
    }
    try:
        r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=40)
    except requests.RequestException:
        return "AI bilan bog'lanib bo'lmadi. Birozdan keyin urinib ko'r.", True

    if r.status_code == 429:
        return "AI hozir band (limit tugagan). Birozdan keyin urinib ko'r.", True
    if r.status_code != 200:
        return "AI hozir ishlamayapti.", True
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, ValueError):
        return "AI javob bera olmadi. Savolni boshqacha yozib ko'r.", True
    return (text[:3500] or "AI javob bera olmadi."), False

# ---------------------------------------------------------------
# TUGMALAR
# ---------------------------------------------------------------
def main_menu(user_id):
    rem = "yoqilgan ✅" if get_reminder(user_id) else "o'chirilgan ❌"
    keyboard = [
        [InlineKeyboardButton("📚 Dars jadvali", callback_data="schedule")],
        [InlineKeyboardButton("📚 Uyga vazifa (Ona tili)", callback_data="homework_view")],
        [InlineKeyboardButton("⏰ Hozir qaysi dars?", callback_data="now")],
        [InlineKeyboardButton("🤖 AI yordamchi", callback_data="ai")],
        [InlineKeyboardButton(f"🔔 Kunlik eslatma: {rem}", callback_data="toggle_rem")],
        [InlineKeyboardButton("👑 Lider admin", callback_data="admin")],
        [InlineKeyboardButton("📝 Fan admini (Ona tili)", callback_data="subject_admin")],
        [InlineKeyboardButton("ℹ️ Bot haqida", callback_data="about")],
    ]
    return InlineKeyboardMarkup(keyboard)

def schedule_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Bugungi", callback_data="today"),
         InlineKeyboardButton("🗓 Haftalik", callback_data="week")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back")],
    ])

def back_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="back")]])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 E'lon yuborish", callback_data="adm_broadcast")],
        [InlineKeyboardButton("👥 Foydalanuvchilar ro'yxati va profillari", callback_data="adm_users_list")],
        [InlineKeyboardButton("📊 AI Tarixini ko'rish", callback_data="adm_ai_history")],
        [InlineKeyboardButton("🚪 Admin paneldan chiqish", callback_data="adm_logout")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back")],
    ])

def subject_admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Uyga vazifani yangilash", callback_data="sub_update_hw")],
        [InlineKeyboardButton("🚪 Fan adminidan chiqish", callback_data="sub_logout")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back")],
    ])

def confirm_menu():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yuborish", callback_data="adm_send"),
        InlineKeyboardButton("❌ Bekor qilish", callback_data="adm_cancel"),
    ]])

# ---------------------------------------------------------------
# HANDLERLAR
# ---------------------------------------------------------------
pw_fails = {}
subject_pw_fails = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    context.user_data.clear()
    await update.message.reply_text("Salom! Quyidagilardan birini tanla 👇",
                                    reply_markup=main_menu(update.effective_user.id))

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    save_user(query.from_user)

    if data != "adm_send":
        if data not in ("adm_cancel",):
            context.user_data.pop("state", None)

    if data == "back":
        context.user_data.clear()
        await query.edit_message_text("Quyidagilardan birini tanla 👇", reply_markup=main_menu(uid))
    elif data == "schedule":
        await query.edit_message_text("Qaysi jadval kerak?", reply_markup=schedule_menu())
    elif data == "today":
        await query.edit_message_text(day_text(now().weekday()), reply_markup=schedule_menu())
    elif data == "week":
        await query.edit_message_text(week_text(), reply_markup=schedule_menu())
    elif data == "now":
        await query.edit_message_text(current_lesson_text(), reply_markup=back_menu())
    elif data == "homework_view":
        with db() as c:
            row = c.execute("SELECT task_text, updated_at FROM homework WHERE subject='ona_tili'").fetchone()
        if row and row["task_text"]:
            hw_text = f"📚 **Ona tili va adabiyotdan uyga vazifa:**\n\n{row['task_text']}\n\n🕒 *Yangilangan vaqti:* {row['updated_at']}"
        else:
            hw_text = "📚 **Ona tili va adabiyotdan uyga vazifa:**\n\nHozircha uyga vazifa kiritilmagan 📭"
        await query.edit_message_text(hw_text, reply_markup=back_menu(), parse_mode="Markdown")
    elif data == "about":
        await query.edit_message_text(ABOUT_TEXT, reply_markup=back_menu(), parse_mode="HTML")
    elif data == "toggle_rem":
        toggle_reminder(uid)
        await query.edit_message_text("Quyidagilardan birini tanla 👇", reply_markup=main_menu(uid))

    elif data == "ai":
        used, _ = ai_used_today(uid)
        left = max(AI_USER_LIMIT - used, 0)
        context.user_data["state"] = "ai"
        await query.edit_message_text(
            f"🤖 AI yordamchi\n\nBugun yana {left} ta savol bera olasan.\n"
            "Savolingni yoz 👇\n\n"
            "⚠️ Ismingni, telefon raqamingni va boshqa shaxsiy ma'lumotlarni yozma.",
            reply_markup=back_menu())

    elif data == "admin":
        if is_admin(uid):
            await query.edit_message_text("🔐 Lider admin panel", reply_markup=admin_menu())
        else:
            fails, locked_until = pw_fails.get(uid, (0, None))
            if locked_until and now() < locked_until:
                await query.edit_message_text("Ko'p marta xato kiritding. Keyinroq urinib ko'r.",
                                              reply_markup=back_menu())
                return
            context.user_data["state"] = "admin_pw"
            await query.edit_message_text("Lider admin parolini yoz 👇", reply_markup=back_menu())

    elif data == "subject_admin":
        if is_subject_admin(uid):
            await query.edit_message_text("📝 Fan admini paneli (Ona tili va adabiyot)", reply_markup=subject_admin_menu())
        else:
            fails, locked_until = subject_pw_fails.get(uid, (0, None))
            if locked_until and now() < locked_until:
                await query.edit_message_text("Ko'p marta xato kiritding. Keyinroq urinib ko'r.",
                                              reply_markup=back_menu())
                return
            context.user_data["state"] = "subject_admin_pw"
            await query.edit_message_text("Fan admini parolini (hppro) yoz 👇", reply_markup=back_menu())

    elif data == "sub_update_hw":
        if not is_subject_admin(uid):
            await query.edit_message_text("Avval fan admini sifatida kiring.", reply_markup=back_menu())
            return
        context.user_data["state"] = "update_homework"
        await query.edit_message_text("📝 Ona tili va adabiyot fanidan янги уйга вазифа матnini yozing:", reply_markup=back_menu())

    elif data == "sub_logout":
        set_subject_admin(uid, False)
        context.user_data.clear()
        await query.edit_message_text("Fan admini panelidan chiqdingiz.", reply_markup=main_menu(uid))
          
    elif data == "adm_ai_history":
        if not is_admin(uid):
            return
        with db() as c:
            rows = c.execute("SELECT user_id, prompt, response, created_at FROM ai_history ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            await query.edit_message_text("Hozircha AI so'rovlari tarixi bo'sh.", reply_markup=admin_menu())
            return
        text = "📊 **Oxirgi 10 ta AI so'rovi tarixi:**\n\n"
        for r in rows:
            text += f"👤 UserID: `{r['user_id']}`\n❓ Savol: {r['prompt']}\n💬 Javob: {r['response'][:100]}...\n🕒 Vaqt: {r['created_at']}\n-------------------\n"
        await query.edit_message_text(text, reply_markup=admin_menu(), parse_mode="Markdown")

    elif data == "adm_users_list":
        if not is_admin(uid):
            await query.edit_message_text("Avval admin paneldan kir.", reply_markup=back_menu())
            return
        records = get_all_user_records()
        if not records:
            await query.edit_message_text("Hozircha bazada foydalanuvchilar yo'q.", reply_markup=admin_menu())
            return
            
        keyboard = []
        for r in records:
            name = r["name"] or f"ID: {r['user_id']}"
            uname = f" (@{r['username']})" if r["username"] else ""
            keyboard.append([InlineKeyboardButton(f"👤 {name}{uname}", callback_data=f"adm_user_prof:{r['user_id']}")])
        
        keyboard.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin")])
        await query.edit_message_text(
            f"👥 **Foydalanuvchilar ro'yxati** (Jami: {len(records)} ta)\n\n"
            "Profilini ko'rish uchun foydalanuvchini tanlang:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        
    elif data.startswith("adm_user_prof:"):
        if not is_admin(uid):
            return
        target_id = int(data.split(":")[1])
        u_data = get_user_by_id(target_id)
        if not u_data:
            await query.edit_message_text("Foydalanuvchi topilmadi.", reply_markup=admin_menu())
            return
            
        username_str = f"@{u_data['username']}" if u_data["username"] else "Mavjud emas"
        reg_date = u_data["created_at"] or "Noma'lum"
        rem_status = "Yoqilgan ✅" if u_data["reminder"] else "O'chirilgan ❌"
        
        text = (
            f"👤 **Foydalanuvchi Profili**\n\n"
            f"🆔 **ID:** `{u_data['user_id']}`\n"
            f"📛 **Ism:** {u_data['name']}\n"
            f"🌐 **Username:** {username_str}\n"
            f"🔔 **Kunlik eslatma:** {rem_status}\n"
            f"📅 **Ro'yxatdan o'tgan vaqti:** {reg_date}"
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Ro'yxatga qaytish", callback_data="adm_users_list")],
            [InlineKeyboardButton("⬅️ Admin menyu", callback_data="admin")]
        ])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif data in ("adm_broadcast", "adm_stats", "adm_logout", "adm_send", "adm_cancel"):
        if not is_admin(uid):
            await query.edit_message_text("Avval admin paneldan kir.", reply_markup=back_menu())
            return
        if data == "adm_stats":
            await query.edit_message_text(f"👥 Botdan foydalanuvchilar soni: {len(all_users())}",
                                          reply_markup=admin_menu())
        elif data == "adm_logout":
            set_admin(uid, False)
            context.user_data.clear()
            await query.edit_message_text("Admin paneldan chiqding.", reply_markup=main_menu(uid))
        elif data == "adm_broadcast":
            context.user_data["state"] = "broadcast"
            await query.edit_message_text("📢 E'lon matnini yoz 👇", reply_markup=back_menu())
        elif data == "adm_cancel":
            context.user_data.clear()
            await query.edit_message_text("Bekor qilindi.", reply_markup=admin_menu())
        elif data == "adm_send":
            text = context.user_data.pop("broadcast_text", None)
            context.user_data.pop("state", None)
            if not text:
                await query.edit_message_text("E'lon topilmadi.", reply_markup=admin_menu())
                return
            await query.edit_message_text("Yuborilmoqda...")
            ok = 0
            for u in all_users():
                try:
                    await context.bot.send_message(u, f"📢 E'lon\n\n{text}")
                    ok += 1
                except Exception:
                    pass
                pytime.sleep(0.05)
            await context.bot.send_message(uid, f"✅ {ok} ta foydalanuvchiga yuborildi.",
                                           reply_markup=admin_menu())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    uid = update.effective_user.id
    text = update.message.text or ""

    if state == "admin_pw":
        try:
            await update.message.delete()
        except Exception:
            pass
        if hmac.compare_digest(text.strip().encode(), ADMIN_PASSWORD.encode()):
            set_admin(uid, True)
            pw_fails.pop(uid, None)
            context.user_data.clear()
            await context.bot.send_message(uid, "✅ Parol to'g'ri. Lider admin panel:", reply_markup=admin_menu())
        else:
            fails = pw_fails.get(uid, (0, None))[0] + 1
            if fails >= MAX_PW_TRIES:
                pw_fails[uid] = (0, now() + timedelta(minutes=LOCK_MINUTES))
                context.user_data.clear()
                await context.bot.send_message(uid, f"❌ Parol xato. {LOCK_MINUTES} daqiqaga bloklandi.",
                                              reply_markup=back_menu())
            else:
                pw_fails[uid] = (fails, None)
                await context.bot.send_message(uid, f"❌ Parol xato. Yana urinib ko'r ({MAX_PW_TRIES - fails} ta urinish qoldi).",
                                              reply_markup=back_menu())

    elif state == "subject_admin_pw":
        try:
            await update.message.delete()
        except Exception:
            pass
        if hmac.compare_digest(text.strip().encode(), SUBJECT_ADMIN_PASSWORD.encode()):
            set_subject_admin(uid, True)
            subject_pw_fails.pop(uid, None)
            context.user_data.clear()
            await context.bot.send_message(uid, "✅ Parol to'g'ri. Fan admini paneli:", reply_markup=subject_admin_menu())
        else:
            fails = subject_pw_fails.get(uid, (0, None))[0] + 1
            if fails >= MAX_PW_TRIES:
                subject_pw_fails[uid] = (0, now() + timedelta(minutes=LOCK_MINUTES))
                context.user_data.clear()
                await context.bot.send_message(uid, f"❌ Parol xato. {LOCK_MINUTES} daqiqaga bloklandi.",
                                              reply_markup=back_menu())
            else:
                subject_pw_fails[uid] = (fails, None)
                await context.bot.send_message(uid, f"❌ Parol xato. Yana urinib ko'r ({MAX_PW_TRIES - fails} ta urinish qoldi).",
                                              reply_markup=back_menu())

    elif state == "update_homework" and is_subject_admin(uid):
        current_time = now().strftime("%Y-%m-%d %H:%M")
        with db() as c:
            c.execute("""
                INSERT INTO homework (subject, task_text, updated_at) 
                VALUES ('ona_tili', ?, ?)
                ON CONFLICT(subject) DO UPDATE SET 
                    task_text=excluded.task_text, 
                    updated_at=excluded.updated_at
            """, (text, current_time))
        context.user_data.clear()
        await update.message.reply_text("✅ Ona tili va adabiyot fanidan uyga vazifa muvaffaqiyatli yangilandi!", reply_markup=subject_admin_menu())

    elif state == "broadcast" and is_admin(uid):
        context.user_data["broadcast_text"] = text
        await update.message.reply_text(f"Shu e'lon yuboriladi:\n\n{text}", reply_markup=confirm_menu())

    elif state == "ai":
        used, total = ai_used_today(uid)
        if used >= AI_USER_LIMIT:
            await update.message.reply_text("Bugungi AI limiting (7 ta) tugadi. Ertaga qaytib kel 🙂",
                                            reply_markup=back_menu())
            return
        if total >= AI_GLOBAL_LIMIT:
            await update.message.reply_text("Bugun AI umumiy limiti tugadi. Ertaga urinib ko'r.",
                                            reply_markup=back_menu())
            return
        wait = await update.message.reply_text("🤔 O'ylayapman...")
        answer, error = ask_ai(text)
        if not error:
            ai_add_usage(uid, text, answer)
            left = AI_USER_LIMIT - used - 1
            answer += f"\n\n(Bugun yana {left} ta savol qoldi)"
        try:
            await wait.delete()
        except Exception:
            pass
        await update.message.reply_text(answer, reply_markup=back_menu())

    else:
        await update.message.reply_text("Menyudan tanla 👇", reply_markup=main_menu(uid))

# ---------------------------------------------------------------
# KUNLIK ESLATMA
# ---------------------------------------------------------------
async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    tomorrow = (now() + timedelta(days=1)).weekday()
    if not SCHEDULE.get(tomorrow):
        return
    text = day_text(tomorrow, prefix="🔔 Ertangi dars jadvali:\n📅")
    text += f"\n\n📝 E-maktab saytiga kirib, uy vazifalaringni tekshirishni unutma!\n{EMAKTAB_URL}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 E-maktabni ochish", url=EMAKTAB_URL)]])
    for u in reminder_users():
        try:
            await context.bot.send_message(u, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            pass 
        pytime.sleep(0.05)

# ---------------------------------------------------------------
# RENDER UCHUN SOXTA VEB-SERVER (HEALTH CHECK)
# ---------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def main():
    init_db()
    if not TOKEN:
        print("XATO: BOT_TOKEN topilmadi!")
        return

    threading.Thread(target=start_health_check_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    if app.job_queue:
        app.job_queue.run_daily(send_reminders, REMINDER_TIME)

    print("Bot ishga tushdi...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
