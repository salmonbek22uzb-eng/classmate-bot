import asyncio
import hmac
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
# HTTP kutubxonalari URL va tokenlarni logga yozmasin.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('classbot')

TOKEN = os.getenv('BOT_TOKEN', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-flash-lite-latest')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
SUBJECT_ADMIN_PASSWORD = os.getenv('SUBJECT_ADMIN_PASSWORD', '')
TEACHER_PASSWORD = os.getenv('TEACHER_PASSWORD', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', '')
DB_FILE = os.getenv('DB_FILE', 'class_bot.db')
TZ = ZoneInfo('Asia/Tashkent')
EMAKTAB_URL = 'https://login.emaktab.uz/'
AI_USER_LIMIT = 7
AI_GLOBAL_LIMIT = 150
MAX_PW_TRIES = 3
LOCK_MINUTES = 10
DAYS = ['Dushanba', 'Seshanba', 'Chorshanba', 'Payshanba', 'Juma', 'Shanba', 'Yakshanba']
SCHEDULE = {
    0: ['Kelajak soati', 'Rus tili', 'Texnologiya', 'Biologiya', 'Algebra', 'Ona tili'],
    1: ['Geografiya', "O'zbekiston tarixi", 'Informatika', 'Geometriya', 'Ingliz tili', 'Kimyo'],
    2: ['Huquq', 'Adabiyot', 'Rus tili', 'Algebra', 'Informatika', 'Jahon tarixi'],
    3: ['Ingliz tili', 'Kimyo', 'Ona tili', 'Tarbiya', 'Jismoniy tarbiya'],
    4: ['Biologiya', 'Fizika', 'Jismoniy tarbiya', "O'zbekiston tarixi", 'Geometriya', 'Geografiya'],
    5: ['Algebra', 'Ingliz tili', 'Chizmachilik', 'Ona tili', 'Adabiyot', 'Fizika'],
}
BELLS = [(time(8,30),time(9,15)), (time(9,20),time(10,5)), (time(10,10),time(10,55)), (time(11,0),time(11,45)), (time(12,5),time(12,50)), (time(12,55),time(13,40))]
SUBJECTS = {'ona_tili': 'Ona tili', 'adabiyot': 'Adabiyot'}
AI_SYSTEM_PROMPT = ("Sen maktab o'quvchilari uchun muloyim o'quv yordamchisisan. O'zbek tilida qisqa va tushunarli javob ber. Faqat o'quv mavzulariga javob ber. Uy vazifasini shunchaki tayyor yechib berma, yechish yo'lini tushuntir. Javob 150 so'zdan oshmasin.")


def now():
    return datetime.now(TZ)


@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, created_at TEXT, reminder INTEGER DEFAULT 1)')
        c.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS subject_admins (user_id INTEGER PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS ai_usage (user_id INTEGER, day TEXT, count INTEGER, PRIMARY KEY(user_id,day))')
        c.execute('CREATE TABLE IF NOT EXISTS ai_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, prompt TEXT, response TEXT, created_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS homework (subject TEXT PRIMARY KEY, task_text TEXT, updated_at TEXT)')
        # Eski ochiq parolli jadval o'qilmaydi va avtomatik ko'chirilmaydi.
        c.execute('''CREATE TABLE IF NOT EXISTS secure_emaktab_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL,
            login_encrypted BLOB NOT NULL, password_encrypted BLOB NOT NULL,
            added_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''')


def cipher():
    try:
        return Fernet(ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        return None


def save_user(user):
    with db() as c:
        c.execute('''INSERT INTO users(user_id,name,username,created_at) VALUES(?,?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username''',
                  (user.id, user.first_name or '', user.username or '', now().strftime('%Y-%m-%d %H:%M')))


def role(uid, table):
    if table == 'admins' and ADMIN_ID and uid == ADMIN_ID:
        return True
    if table not in ('admins', 'subject_admins'):
        return False
    with db() as c:
        return c.execute(f'SELECT 1 FROM {table} WHERE user_id=?', (uid,)).fetchone() is not None


def set_role(uid, table, enabled):
    if table not in ('admins', 'subject_admins'):
        raise ValueError('Noto‘g‘ri rol')
    with db() as c:
        if enabled:
            c.execute(f'INSERT OR IGNORE INTO {table}(user_id) VALUES(?)', (uid,))
        else:
            c.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))


def day_text(day, prefix='📅'):
    lessons = SCHEDULE.get(day)
    if not lessons:
        return f'{prefix} {DAYS[day]}: dars yo‘q, dam olish kuni 😊'
    lines = [f'{prefix} {DAYS[day]}\n']
    for i, subject in enumerate(lessons):
        a, b = BELLS[i]
        lines.append(f'{i+1}. {subject} ({a:%H:%M}–{b:%H:%M})')
    return '\n'.join(lines)


def current_lesson_text():
    n = now()
    lessons = SCHEDULE.get(n.weekday())
    if not lessons:
        return 'Bugun dars yo‘q, dam olish kuni 😊'
    t = n.time()
    if t < BELLS[0][0]:
        return f'Darslar hali boshlanmadi. 1-dars {BELLS[0][0]:%H:%M} da: {lessons[0]}'
    for i, subject in enumerate(lessons):
        start, end = BELLS[i]
        if start <= t < end:
            left = int((datetime.combine(n.date(), end) - datetime.combine(n.date(), t)).total_seconds() // 60)
            result = f'⏰ Hozir {i+1}-dars: {subject}\nTugashiga ~{left} daqiqa qoldi.'
            if i + 1 < len(lessons):
                result += f'\nKeyingi dars: {lessons[i+1]}'
            return result
        if i + 1 < len(lessons) and end <= t < BELLS[i+1][0]:
            return f'🔔 Tanaffus. Keyingi dars {BELLS[i+1][0]:%H:%M} da: {lessons[i+1]}'
    return 'Bugungi darslar tugadi ✅'


def markup(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback) for label, callback in row] for row in rows])


def back():
    return markup([[('⬅️ Bosh menyu', 'back')]])


def main_menu(uid):
    with db() as c:
        r = c.execute('SELECT reminder FROM users WHERE user_id=?', (uid,)).fetchone()
    rem = 'yoqilgan ✅' if not r or r['reminder'] else 'o‘chirilgan ❌'
    return markup([[('📚 Dars jadvali','schedule')], [('📚 Uyga vazifa','homework_menu')],
        [('⏰ Hozir qaysi dars?','now')], [('👨‍🏫 O‘qituvchi (E-maktab)','teacher')],
        [('🤖 AI yordamchi','ai')], [(f'🔔 Kunlik eslatma: {rem}','toggle_rem')],
        [('👑 Lider admin','admin')], [('📝 Fan admini','subject_admin')], [('ℹ️ Bot haqida','about')]])


def admin_menu():
    return markup([[('📢 E’lon yuborish','adm_broadcast')], [('👥 Foydalanuvchilar','adm_users')], [('📊 AI tarixi','adm_history')], [('🚪 Chiqish','adm_logout')], [('⬅️ Bosh menyu','back')]])


def subject_menu():
    return markup([[('✍️ Uyga vazifani yangilash','sub_update')], [('🚪 Chiqish','sub_logout')], [('⬅️ Bosh menyu','back')]])


def teacher_menu():
    return markup([[('➕ Akkaunt qo‘shish','teacher_add')], [('📋 Akkauntlar','teacher_list')], [('🚪 Chiqish','teacher_logout')], [('⬅️ Bosh menyu','back')]])


def subject_choices(prefix):
    return markup([[('📘 Ona tili',f'{prefix}:ona_tili'), ('📙 Adabiyot',f'{prefix}:adabiyot')], [('⬅️ Bosh menyu','back')]])


# Brauzersiz bir marta kirishga urinish. Hech qanday CAPTCHA/2FA chetlab o'tilmaydi.
def safe_url(url):
    p = urlparse(url)
    return p.scheme == 'https' and p.port in (None, 443) and p.username is None and p.password is None and (p.hostname == 'emaktab.uz' or (p.hostname or '').endswith('.emaktab.uz'))


def login_emaktab(login, password):
    try:
        with requests.Session() as session:
            session.headers.update({'User-Agent': 'Mozilla/5.0'})
            # Cookie saqlanadi, ammo POST redirect avtomatik yuborilmaydi.
            response = session.get(EMAKTAB_URL, timeout=20, allow_redirects=False)
            if response.status_code != 200:
                return 'unknown', 'Kirish sahifasi ochilmadi.'
            soup = BeautifulSoup(response.text, 'html.parser')
            form = soup.find('form', attrs={'method': lambda x: x and x.lower() == 'post'})
            if not form or not form.find('input', attrs={'name': 'login'}) or not form.find('input', attrs={'name': 'password'}):
                return 'unknown', 'Kirish sahifasi o‘zgargan.'
            captcha = form.select_one('.login__body__captcha')
            if captcha and 'login__body__captcha_hidden' not in captcha.get('class', []):
                return 'manual', 'CAPTCHA talab qilinmoqda. Saytda qo‘lda kiring.'
            action = urljoin(EMAKTAB_URL, form.get('action') or EMAKTAB_URL)
            if not safe_url(action):
                return 'unknown', 'Kirish manzili xavfsizlik tekshiruvidan o‘tmadi.'
            payload = {i.get('name'): i.get('value', '') for i in form.select('input[type=hidden][name]')}
            payload.update({'login': login, 'password': password})
            response = session.post(action, data=payload, timeout=20, allow_redirects=False)
            # Faqat rasmiy HTTPS domenlaridagi redirectlarga GET yuboriladi.
            for _ in range(8):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get('Location', '')
                next_url = urljoin(response.url, location)
                if not location or not safe_url(next_url):
                    return 'unknown', 'Kirishdan keyingi manzilni tasdiqlab bo‘lmadi.'
                if '2fa' in next_url.lower() or 'verification' in next_url.lower():
                    return 'manual', 'Ikki bosqichli tasdiqlash kerak. Saytda qo‘lda kiring.'
                # 307/308 POSTni qayta yuborish talab qilishi mumkin; parolni takror yubormaymiz.
                if response.status_code in (307, 308):
                    return 'unknown', 'Kirish yo‘naltirishini tasdiqlab bo‘lmadi.'
                response = session.get(next_url, timeout=20, allow_redirects=False)
            else:
                return 'unknown', 'Juda ko‘p yo‘naltirish.'
            if response.status_code != 200:
                return 'unknown', 'Sayt kirish natijasini tasdiqlamadi.'
            soup = BeautifulSoup(response.text, 'html.parser')
            if '2fa' in response.url.lower() or 'verification' in response.url.lower():
                return 'manual', 'Ikki bosqichli tasdiqlash kerak. Saytda qo‘lda kiring.'
            # Kirish formasi yana chiqsa, logout havolasi bo'lsa ham muvaffaqiyat emas.
            if soup.find('input', attrs={'name': 'password'}):
                return 'manual', 'Kirish tasdiqlanmadi: login/parol, CAPTCHA yoki qo‘lda tasdiqlashni tekshiring.'
            # 200 javobi o'zi muvaffaqiyat emas. Faqat logout belgisi aniqlansa success.
            for element in soup.find_all(['a', 'form']):
                target = element.get('href') or element.get('action') or ''
                path = urlparse(urljoin(response.url, target)).path.lower()
                if target and safe_url(urljoin(response.url, target)) and ('logout' in path or 'logoff' in path or 'signout' in path):
                    return 'success', 'Kirish tasdiqlandi.'
            return 'unknown', 'Sayt javobidan kirish muvaffaqiyatini aniqlab bo‘lmadi.'
    except requests.RequestException:
        return 'network', 'Sayt bilan bog‘lanib bo‘lmadi.'
    except (ValueError, TypeError):
        return 'unknown', 'Sayt javobini tekshirib bo‘lmadi.'


async def account_check(context, row):
    f = cipher()
    if not f:
        status, message = 'manual', 'ENCRYPTION_KEY noto‘g‘ri yoki kiritilmagan.'
    else:
        try:
            login = f.decrypt(row['login_encrypted']).decode('utf-8')
            password = f.decrypt(row['password_encrypted']).decode('utf-8')
            status, message = await asyncio.to_thread(login_emaktab, login, password)
        except (InvalidToken, UnicodeDecodeError, TypeError):
            status, message = 'manual', 'Akkauntni ochib bo‘lmadi. Shifrlash kalitini tekshiring.'
    # CAPTCHA / noto'g'ri parol / noaniq sayt javoblarida avtomatik urinishlarni to'xtatamiz.
    if status in ('manual', 'unknown'):
        with db() as c:
            c.execute('UPDATE secure_emaktab_accounts SET active=0 WHERE id=?', (row['id'],))
        message += ' Avtomatik urinishlar to‘xtatildi. Tekshirib, akkauntni qayta qo‘shing.'
    try:
        await context.bot.send_message(row['teacher_id'], f"👨‍🏫 E-maktab, akkaunt #{row['id']}: {message}")
    except Exception:
        log.warning('E-maktab natijasini yuborib bo‘lmadi: %s', row['teacher_id'])


async def emaktab_job(context):
    with db() as c:
        rows = c.execute('SELECT * FROM secure_emaktab_accounts WHERE active=1 ORDER BY id').fetchall()
    for row in rows:
        await account_check(context, row)
        await asyncio.sleep(1)


def ai_call(text):
    if not GEMINI_API_KEY:
        return None, 'AI hali sozlanmagan.'
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
    body = {'systemInstruction': {'parts': [{'text': AI_SYSTEM_PROMPT}]}, 'contents': [{'role':'user','parts':[{'text':text[:1000]}]}], 'generationConfig':{'maxOutputTokens':600}}
    try:
        r = requests.post(url, json=body, headers={'x-goog-api-key': GEMINI_API_KEY}, timeout=40)
        if r.status_code == 429:
            return None, 'AI hozir band. Keyinroq urinib ko‘ring.'
        r.raise_for_status()
        content = ''.join(p.get('text','') for p in r.json()['candidates'][0]['content']['parts']).strip()
        if not content:
            return None, 'AI javob bera olmadi.'
        return content[:3500], None
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        return None, 'AI bilan bog‘lanib bo‘lmadi yoki javob olinmadi.'


pw_fails = {}
teacher_logged_in = set()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    context.user_data.clear()
    await update.message.reply_text('Salom! Quyidagilardan birini tanlang 👇', reply_markup=main_menu(update.effective_user.id))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text('Bekor qilindi.', reply_markup=main_menu(update.effective_user.id))


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, data = q.from_user.id, q.data or ''
    save_user(q.from_user)
    context.user_data.clear()
    private = update.effective_chat.type == 'private'
    if data == 'back':
        await q.edit_message_text('Quyidagilardan birini tanlang 👇', reply_markup=main_menu(uid))
    elif data == 'schedule':
        await q.edit_message_text('Qaysi jadval kerak?', reply_markup=markup([[('📅 Bugungi','today'),('🗓 Haftalik','week')],[('⬅️ Bosh menyu','back')]]))
    elif data in ('today','week'):
        value = day_text(now().weekday()) if data == 'today' else '\n\n'.join(day_text(i) for i in range(6))
        await q.edit_message_text(value, reply_markup=back())
    elif data == 'now':
        await q.edit_message_text(current_lesson_text(), reply_markup=back())
    elif data == 'about':
        await q.edit_message_text('ℹ️ Sinf uchun yordamchi bot: jadval, uyga vazifa, eslatmalar va AI.\n\nBot yaratuvchi: Eshdavlatov Salmon', reply_markup=back())
    elif data == 'toggle_rem':
        with db() as c:
            c.execute('UPDATE users SET reminder=CASE WHEN reminder=1 THEN 0 ELSE 1 END WHERE user_id=?',(uid,))
        await q.edit_message_text('Eslatma sozlamasi o‘zgartirildi.', reply_markup=main_menu(uid))
    elif data == 'homework_menu':
        await q.edit_message_text('Qaysi fandan uyga vazifa?', reply_markup=subject_choices('hw'))
    elif data.startswith('hw:'):
        subj = data.split(':',1)[1]
        if subj not in SUBJECTS:
            return
        with db() as c:
            r = c.execute('SELECT task_text,updated_at FROM homework WHERE subject=?',(subj,)).fetchone()
        value = f"{r['task_text']}\n\n🕒 {r['updated_at']}" if r and r['task_text'] else 'Hozircha uyga vazifa kiritilmagan 📭'
        await q.edit_message_text(f'📚 {SUBJECTS[subj]}\n\n{value}', reply_markup=subject_choices('hw'))
    elif data == 'ai':
        context.user_data['state'] = 'ai'
        await q.edit_message_text('🤖 Savolingizni yozing. Kunlik limit: 7 ta.\nShaxsiy ma’lumotlaringizni yubormang.', reply_markup=back())
    elif data in ('admin','subject_admin','teacher'):
        if not private:
            await q.edit_message_text('Bu panelni faqat bot bilan shaxsiy chatda oching.', reply_markup=back())
            return
        if data == 'admin' and role(uid,'admins'):
            await q.edit_message_text('🔐 Lider admin panel', reply_markup=admin_menu())
        elif data == 'subject_admin' and role(uid,'subject_admins'):
            await q.edit_message_text('📝 Fan admini paneli', reply_markup=subject_menu())
        elif data == 'teacher' and uid in teacher_logged_in:
            await q.edit_message_text('👨‍🏫 O‘qituvchi paneli', reply_markup=teacher_menu())
        else:
            password = {'admin':ADMIN_PASSWORD, 'subject_admin':SUBJECT_ADMIN_PASSWORD, 'teacher':TEACHER_PASSWORD}[data]
            if not password:
                await q.edit_message_text('Bu panel paroli serverda sozlanmagan.', reply_markup=back())
                return
            fail, locked = pw_fails.get((uid,data),(0,None))
            if locked and now() < locked:
                await q.edit_message_text('Juda ko‘p urinish. Keyinroq urinib ko‘ring.', reply_markup=back())
                return
            context.user_data['state'] = 'pw:' + data
            await q.edit_message_text('🔐 Panel parolini yozing:', reply_markup=back())
    elif data == 'teacher_logout':
        teacher_logged_in.discard(uid)
        await q.edit_message_text('Paneldan chiqdingiz.', reply_markup=main_menu(uid))
    elif data == 'teacher_add':
        if not private or uid not in teacher_logged_in:
            await q.edit_message_text('Avval shaxsiy chatda o‘qituvchi paneliga kiring.', reply_markup=back()); return
        if not cipher():
            await q.edit_message_text('Serverda to‘g‘ri ENCRYPTION_KEY sozlanmagan.', reply_markup=teacher_menu()); return
        context.user_data['state'] = 'emaktab_login'
        await q.edit_message_text('Faqat akkaunt egasining roziligi bilan va eMaktab qoidalariga muvofiq ishlating.\n\nE-maktab loginini yuboring. Parol va login Telegram orqali serverga uzatiladi.', reply_markup=back())
    elif data == 'teacher_list':
        if not private or uid not in teacher_logged_in:
            await q.edit_message_text('Avval o‘qituvchi paneliga kiring.', reply_markup=back()); return
        with db() as c:
            rows = c.execute('SELECT id,added_at,active FROM secure_emaktab_accounts WHERE teacher_id=? ORDER BY id',(uid,)).fetchall()
        if not rows:
            await q.edit_message_text('Akkauntlar yo‘q.', reply_markup=teacher_menu()); return
        lines = ['📋 Akkauntlar:']
        keys = []
        for r in rows[:30]:
            lines.append(f"#{r['id']} — {'faol' if r['active'] else 'to‘xtatilgan'} — {r['added_at']}")
            keys.append([(f"❌ O‘chirish #{r['id']}",f"del:{r['id']}")])
        if len(rows) > 30:
            lines.append('Faqat dastlabki 30 ta ko‘rsatildi.')
        keys.append([('⬅️ O‘qituvchi paneli','teacher')])
        await q.edit_message_text('\n'.join(lines), reply_markup=markup(keys))
    elif data.startswith('del:'):
        if not private or uid not in teacher_logged_in:
            return
        try: account_id = int(data.split(':',1)[1])
        except ValueError: return
        with db() as c:
            c.execute('DELETE FROM secure_emaktab_accounts WHERE id=? AND teacher_id=?',(account_id,uid))
        await q.edit_message_text('Akkaunt o‘chirildi.', reply_markup=teacher_menu())
    elif data == 'sub_update':
        if not role(uid,'subject_admins'): return
        await q.edit_message_text('Qaysi fan?', reply_markup=subject_choices('edit'))
    elif data.startswith('edit:'):
        subj = data.split(':',1)[1]
        if not role(uid,'subject_admins') or subj not in SUBJECTS: return
        context.user_data.update(state='homework', subject=subj)
        await q.edit_message_text(f'{SUBJECTS[subj]} fanidan uyga vazifani yozing:', reply_markup=back())
    elif data == 'sub_logout':
        set_role(uid,'subject_admins',False)
        await q.edit_message_text('Fan adminidan chiqdingiz.', reply_markup=main_menu(uid))
    elif data == 'adm_logout':
        set_role(uid,'admins',False)
        await q.edit_message_text('Admin paneldan chiqdingiz. ADMIN_ID egasi doim admin bo‘lib qoladi.', reply_markup=main_menu(uid))
    elif data == 'adm_users' and role(uid,'admins'):
        with db() as c: rows = c.execute('SELECT user_id,name,username FROM users ORDER BY created_at DESC LIMIT 100').fetchall()
        lines = [f'👥 Foydalanuvchilar (so‘nggi {len(rows)} ta):']
        lines += [f"{r['user_id']} — {r['name'] or 'Ismsiz'}" + (f" (@{r['username']})" if r['username'] else '') for r in rows]
        await q.edit_message_text('\n'.join(lines)[:3900], reply_markup=admin_menu())
    elif data == 'adm_history' and role(uid,'admins'):
        with db() as c: rows = c.execute('SELECT user_id,prompt,response,created_at FROM ai_history ORDER BY id DESC LIMIT 10').fetchall()
        lines = ['📊 Oxirgi AI so‘rovlari:']
        for r in rows: lines.append(f"\nID: {r['user_id']} | {r['created_at']}\nSavol: {r['prompt'][:200]}\nJavob: {r['response'][:150]}")
        await q.edit_message_text('\n'.join(lines)[:3900], reply_markup=admin_menu())
    elif data == 'adm_broadcast' and role(uid,'admins'):
        context.user_data['state'] = 'broadcast'
        await q.edit_message_text('📢 E’lon matnini yozing:', reply_markup=back())
    elif data == 'adm_send' and role(uid,'admins'):
        # Confirmed broadcast content is stored in bot_data, not cleared user_data.
        text = context.bot_data.pop(f'broadcast:{uid}', None)
        if not text:
            await q.edit_message_text('E’lon topilmadi.', reply_markup=admin_menu()); return
        await q.edit_message_text('Yuborilmoqda...')
        with db() as c: ids = [r['user_id'] for r in c.execute('SELECT user_id FROM users')]
        ok = 0
        for target in ids:
            try:
                await context.bot.send_message(target, '📢 E’lon\n\n' + text)
                ok += 1
            except Exception: pass
            await asyncio.sleep(0.05)
        await context.bot.send_message(uid, f'✅ {ok} ta foydalanuvchiga yuborildi.', reply_markup=admin_menu())
    elif data == 'adm_cancel':
        context.bot_data.pop(f'broadcast:{uid}', None)
        await q.edit_message_text('Bekor qilindi.', reply_markup=admin_menu() if role(uid,'admins') else back())


async def delete_sensitive(message):
    try:
        await message.delete()
        return True
    except Exception:
        return False


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = context.user_data.get('state', '')
    text = update.message.text or ''
    if state.startswith('pw:'):
        which = state.split(':',1)[1]
        deleted = await delete_sensitive(update.message)
        if not deleted:
            await context.bot.send_message(uid, '⚠️ Parol xabarini avtomatik o‘chirib bo‘lmadi. Uni o‘zingiz o‘chiring.')
        expected = {'admin':ADMIN_PASSWORD,'subject_admin':SUBJECT_ADMIN_PASSWORD,'teacher':TEACHER_PASSWORD}.get(which,'')
        count, locked = pw_fails.get((uid,which),(0,None))
        if locked and now() < locked:
            context.user_data.clear()
            await context.bot.send_message(uid, 'Vaqtinchalik bloklangan.', reply_markup=back()); return
        if expected and hmac.compare_digest(text.strip().encode(), expected.encode()):
            pw_fails.pop((uid,which),None)
            context.user_data.clear()
            if which == 'admin': set_role(uid,'admins',True); menu=admin_menu()
            elif which == 'subject_admin': set_role(uid,'subject_admins',True); menu=subject_menu()
            else: teacher_logged_in.add(uid); menu=teacher_menu()
            await context.bot.send_message(uid, '✅ Panel ochildi.', reply_markup=menu)
        else:
            count += 1
            if count >= MAX_PW_TRIES:
                pw_fails[(uid,which)] = (0,now()+timedelta(minutes=LOCK_MINUTES))
                context.user_data.clear()
                msg = f'Parol xato. {LOCK_MINUTES} daqiqaga bloklandi.'
            else:
                pw_fails[(uid,which)] = (count,None)
                msg = f'Parol xato. {MAX_PW_TRIES-count} ta urinish qoldi.'
            await context.bot.send_message(uid, msg, reply_markup=back())
    elif state in ('emaktab_login','emaktab_password'):
        deleted = await delete_sensitive(update.message)
        if not deleted:
            await context.bot.send_message(uid, '⚠️ Xabarni avtomatik o‘chirib bo‘lmadi. Uni o‘zingiz o‘chiring.')
        if update.effective_chat.type != 'private' or uid not in teacher_logged_in:
            context.user_data.clear(); return
        if state == 'emaktab_login':
            if not text.strip() or len(text) > 200:
                await context.bot.send_message(uid,'Login noto‘g‘ri. Qayta yuboring.',reply_markup=back()); return
            context.user_data['login'] = text.strip()
            context.user_data['state'] = 'emaktab_password'
            await context.bot.send_message(uid,'🔑 Endi parolni yuboring:',reply_markup=back())
        else:
            login = context.user_data.get('login')
            f = cipher()
            if not login or not text or len(text) > 500 or not f:
                context.user_data.clear()
                await context.bot.send_message(uid,'Ma’lumot yoki shifrlash kaliti noto‘g‘ri.',reply_markup=teacher_menu()); return
            with db() as c:
                cur = c.execute('INSERT INTO secure_emaktab_accounts(teacher_id,login_encrypted,password_encrypted,added_at) VALUES(?,?,?,?)',
                    (uid,f.encrypt(login.encode()),f.encrypt(text.encode()),now().strftime('%Y-%m-%d %H:%M')))
                account_id = cur.lastrowid
                row = c.execute('SELECT * FROM secure_emaktab_accounts WHERE id=?',(account_id,)).fetchone()
            context.user_data.clear()
            await context.bot.send_message(uid,f'Akkaunt #{account_id} shifrlab saqlandi. Kirish tekshirilmoqda...')
            await account_check(context,row)
            await context.bot.send_message(uid,'O‘qituvchi paneli:',reply_markup=teacher_menu())
    elif state == 'homework' and role(uid,'subject_admins'):
        subj = context.user_data.get('subject')
        if subj not in SUBJECTS or len(text) > 3000:
            await update.message.reply_text('Matn noto‘g‘ri yoki juda uzun.'); return
        with db() as c:
            c.execute('''INSERT INTO homework(subject,task_text,updated_at) VALUES(?,?,?) ON CONFLICT(subject) DO UPDATE SET task_text=excluded.task_text,updated_at=excluded.updated_at''',(subj,text,now().strftime('%Y-%m-%d %H:%M')))
        context.user_data.clear()
        await update.message.reply_text('✅ Uyga vazifa yangilandi.',reply_markup=subject_menu())
    elif state == 'broadcast' and role(uid,'admins'):
        if len(text) > 3500:
            await update.message.reply_text('E’lon juda uzun. 3500 belgidan oshmasin.'); return
        context.bot_data[f'broadcast:{uid}'] = text
        context.user_data.clear()
        await update.message.reply_text('Shu e’lon yuboriladi:\n\n'+text,reply_markup=markup([[('✅ Yuborish','adm_send'),('❌ Bekor qilish','adm_cancel')]]))
    elif state == 'ai':
        day = now().strftime('%Y-%m-%d')
        with db() as c:
            r = c.execute('SELECT count FROM ai_usage WHERE user_id=? AND day=?',(uid,day)).fetchone()
            used = r['count'] if r else 0
            total = c.execute('SELECT COALESCE(SUM(count),0) FROM ai_usage WHERE day=?',(day,)).fetchone()[0]
        if used >= AI_USER_LIMIT or total >= AI_GLOBAL_LIMIT:
            await update.message.reply_text('Bugungi AI limit tugadi.',reply_markup=back()); return
        wait = await update.message.reply_text('🤔 O‘ylayapman...')
        answer, error = await asyncio.to_thread(ai_call,text)
        try: await wait.delete()
        except Exception: pass
        if error:
            await update.message.reply_text(error,reply_markup=back()); return
        with db() as c:
            c.execute('INSERT INTO ai_usage(user_id,day,count) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1',(uid,day))
            c.execute('INSERT INTO ai_history(user_id,prompt,response,created_at) VALUES(?,?,?,?)',(uid,text[:1000],answer,now().strftime('%Y-%m-%d %H:%M:%S')))
        await update.message.reply_text(answer+f'\n\n(Bugun yana {AI_USER_LIMIT-used-1} ta savol qoldi)',reply_markup=back())
    else:
        await update.message.reply_text('Menyudan tanlang 👇',reply_markup=main_menu(uid))


async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    tomorrow = (now()+timedelta(days=1)).weekday()
    if not SCHEDULE.get(tomorrow): return
    message = day_text(tomorrow, '🔔 Ertangi jadval:') + '\n\nUy vazifangizni E-maktabda tekshirishni unutmang.'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('🌐 E-maktabni ochish',url=EMAKTAB_URL)]])
    with db() as c: ids = [r['user_id'] for r in c.execute('SELECT user_id FROM users WHERE reminder=1')]
    for uid in ids:
        try: await context.bot.send_message(uid,message,reply_markup=keyboard,disable_web_page_preview=True)
        except Exception: pass
        await asyncio.sleep(0.05)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args):
        pass


def serve_health():
    HTTPServer(('0.0.0.0',int(os.getenv('PORT','8080'))),HealthCheckHandler).serve_forever()


def main():
    if not TOKEN:
        raise RuntimeError('BOT_TOKEN sozlanmagan')
    init_db()
    threading.Thread(target=serve_health,daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('cancel',cancel))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    if app.job_queue is None:
        raise RuntimeError('JobQueue yo‘q: python-telegram-bot[job-queue] o‘rnating')
    app.job_queue.run_daily(send_reminders,time(20,0,tzinfo=TZ),name='daily_reminder')
    # python-telegram-bot 20+: 0 yakshanba, 1 dushanba, 4 payshanba.
    app.job_queue.run_daily(emaktab_job,time(11,0,tzinfo=TZ),days=(1,4),name='emaktab_login')
    # Python 3.14 asosiy oqimda event loopni avtomatik yaratmaydi.
    asyncio.set_event_loop(asyncio.new_event_loop())
    log.info('Bot ishga tushmoqda')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
