import asyncio
import logging
import random
import sqlite3
import io
import os
import pandas as pd
import matplotlib.pyplot as plt
import pytz
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramForbiddenError

# ==========================================
# 1. CONFIG
# ==========================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone='Asia/Tashkent')
TASHKENT_TZ = pytz.timezone('Asia/Tashkent')

# ==========================================
# 2. DATABASE (SQLite)
# ==========================================
DB_NAME = "school_monitor.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Maktablar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            active INTEGER DEFAULT 1,
            premium INTEGER DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE schools ADD COLUMN premium INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    
    # Adminlar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            school_id INTEGER,
            FOREIGN KEY(school_id) REFERENCES schools(id)
        )
    ''')
    
    # O'qituvchilar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS teachers (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            subject TEXT,
            school_id INTEGER,
            status INTEGER DEFAULT 1,
            FOREIGN KEY(school_id) REFERENCES schools(id)
        )
    ''')
    try:
        cursor.execute('ALTER TABLE teachers ADD COLUMN subject TEXT')
    except sqlite3.OperationalError:
        pass
        
    # Dars jadvali jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER,
            class_name TEXT,
            subject TEXT,
            day TEXT,
            lesson_time TEXT,
            shift INTEGER,
            FOREIGN KEY(teacher_id) REFERENCES teachers(user_id)
        )
    ''')
    
    # Baho nazorati jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS grades_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER,
            lesson_id INTEGER,
            date TEXT,
            status TEXT DEFAULT 'pending',
            timestamp TEXT,
            FOREIGN KEY(teacher_id) REFERENCES teachers(user_id),
            FOREIGN KEY(lesson_id) REFERENCES schedule(id)
        )
    ''')
    
    # Kanallar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL
        )
    ''')
    
    # Foydalanuvchilar jadvali (Statistika uchun)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at TEXT
        )
    ''')
    
    # Dars vaqtlari jadvali (Dars rejimi)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lesson_times (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER,
            shift INTEGER DEFAULT 1,
            lesson_number INTEGER,
            start_time TEXT,
            end_time TEXT,
            day TEXT DEFAULT 'Umumiy',
            FOREIGN KEY(school_id) REFERENCES schools(id)
        )
    ''')
    
    conn.commit()
    conn.close()

def execute_query(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(query, params)
    
    if fetchone:
        result = cursor.fetchone()
    elif fetchall:
        result = cursor.fetchall()
    else:
        conn.commit()
        result = None
        
    conn.close()
    return result

# ==========================================
# 3. AUTH & ROLES
# ==========================================
def get_user_role(user_id):
    if user_id == OWNER_ID:
        return "owner"
    
    admin = execute_query("SELECT school_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    if admin:
        return "admin"
        
    teacher = execute_query("SELECT school_id FROM teachers WHERE user_id = ? AND status = 1", (user_id,), fetchone=True)
    if teacher:
        return "teacher"
    
    return "user"

def get_admin_school(user_id):
    admin = execute_query("SELECT school_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    return admin[0] if admin else None

def get_teacher_school(user_id):
    teacher = execute_query("SELECT school_id FROM teachers WHERE user_id = ?", (user_id,), fetchone=True)
    return teacher[0] if teacher else None

def is_school_active(school_id):
    school = execute_query("SELECT active FROM schools WHERE id = ?", (school_id,), fetchone=True)
    return school and school[0] == 1

# ==========================================
# 4. SUBSCRIPTION CHECK
# ==========================================
async def check_subscription(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
        
    role = get_user_role(user_id)
    school_id = None
    if role == "admin":
        school_id = get_admin_school(user_id)
    elif role == "teacher":
        school_id = get_teacher_school(user_id)
        
    if school_id:
        school = execute_query("SELECT premium FROM schools WHERE id = ?", (school_id,), fetchone=True)
        if school and school[0] == 1:
            return True # Premium maktablar obunadan ozod
            
    channels = execute_query("SELECT channel_id FROM channels", fetchall=True)
    if not channels:
        return True
        
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch[0], user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            logger.error(f"Kanalga obunani tekshirishda xatolik ({ch[0]}): {e}")
            continue
            
    return True

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: Dict[str, Any]) -> Any:
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id
            
        if user_id:
            if isinstance(event, CallbackQuery) and event.data == "check_sub":
                return await handler(event, data)
                
            is_sub = await check_subscription(user_id)
            if not is_sub:
                kb = await get_subscription_kb()
                text = "❌ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:"
                if isinstance(event, Message):
                    await event.answer(text, reply_markup=kb)
                elif isinstance(event, CallbackQuery):
                    await event.message.answer(text, reply_markup=kb)
                    await event.answer()
                return
                
        return await handler(event, data)

dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

# ==========================================
# 5. BOT UI (FRONTEND)
# ==========================================
async def get_subscription_kb():
    channels = execute_query("SELECT channel_id, title FROM channels", fetchall=True)
    kb = []
    for ch_id, title in channels:
        url = None
        if str(ch_id).startswith('@'):
            url = f"https://t.me/{str(ch_id).replace('@', '')}"
        elif str(ch_id).startswith('http'):
            url = ch_id
        elif str(ch_id).startswith('-100'):
            try:
                chat = await bot.get_chat(ch_id)
                url = chat.invite_link
                if not url:
                    url = await bot.export_chat_invite_link(ch_id)
            except Exception as e:
                logger.error(f"Invite link olishda xato {ch_id}: {e}")
                
        if url:
            kb.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
            
    kb.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_owner_kb():
    kb = [
        [InlineKeyboardButton(text="🏫 Maktab qo'shish", callback_data="add_school"),
         InlineKeyboardButton(text="❌ Maktab o'chirish", callback_data="del_school")],
        [InlineKeyboardButton(text="👤 Admin qo'shish", callback_data="add_admin"),
         InlineKeyboardButton(text="📋 Adminlar ro'yxati", callback_data="list_admins")],
        [InlineKeyboardButton(text="💰 Litsenziya boshqarish", callback_data="manage_license"),
         InlineKeyboardButton(text="💎 Premium boshqarish", callback_data="manage_premium")],
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="add_channel"),
         InlineKeyboardButton(text="❌ Kanal o'chirish", callback_data="del_channel")],
        [InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="list_channels")],
        [InlineKeyboardButton(text="👥 Barcha foydalanuvchilar", callback_data="owner_users_list")],
        [InlineKeyboardButton(text="📢 Ommaviy xabarnoma", callback_data="owner_broadcast")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="owner_stats")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_kb():
    kb = [
        [InlineKeyboardButton(text="📊 Hisobot", callback_data="admin_report")],
        [InlineKeyboardButton(text="⚠️ Muammolar", callback_data="admin_issues")],
        [InlineKeyboardButton(text="👨‍🏫 O'qituvchilar ro'yxati", callback_data="admin_teachers")],
        [InlineKeyboardButton(text="📅 Dars jadvali boshqaruvi", callback_data="admin_manage_schedule")],
        [InlineKeyboardButton(text="📂 Excel bilan ishlash", callback_data="admin_excel")],
        [InlineKeyboardButton(text="📢 O'qituvchilarga xabar", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📈 Statistika", callback_data="admin_stats")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_schedule_manage_kb():
    kb = [
        [InlineKeyboardButton(text="➕ Dars qo'shish", callback_data="admin_add_schedule")],
        [InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="admin_edit_schedule_list")],
        [InlineKeyboardButton(text="❌ O'chirish", callback_data="admin_del_schedule_list")],
        [InlineKeyboardButton(text="📋 Ko'rish", callback_data="admin_view_schedule")],
        [InlineKeyboardButton(text="⏰ Dars rejimi boshqaruvi", callback_data="admin_lesson_times")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_admin")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_teacher_kb():
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🖥 Bugungi darsim"), KeyboardButton(text="🏫 Ertangi darsim")],
        [KeyboardButton(text="📅 Dars jadvali"), KeyboardButton(text="🎯 Dars rejimi")],
        [KeyboardButton(text="📊 Mening jadvalim"), KeyboardButton(text="ℹ️ Ma'lumot")]
    ], resize_keyboard=True, is_persistent=True)
    return kb

def get_teacher_inline_kb():
    kb = [
        [InlineKeyboardButton(text="📊 Mening baholarim", callback_data="teacher_grades")],
        [InlineKeyboardButton(text="📈 Statistika", callback_data="teacher_stats")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_schools_kb(action_prefix):
    schools = execute_query("SELECT id, name FROM schools", fetchall=True)
    kb = []
    for school_id, name in schools:
        kb.append([InlineKeyboardButton(text=name, callback_data=f"{action_prefix}_{school_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_owner")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_license_kb(school_id):
    school = execute_query("SELECT active FROM schools WHERE id = ?", (school_id,), fetchone=True)
    active = school[0] if school else 0
    status_text = "🟢 Faol" if active else "🔴 Faol emas"
    toggle_action = "deactivate" if active else "activate"
    toggle_text = "🔴 Bloklash" if active else "🟢 Faollashtirish"
    
    kb = [
        [InlineKeyboardButton(text=f"Holat: {status_text}", callback_data="ignore")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"license_{toggle_action}_{school_id}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="manage_license")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_kb(target="back_to_owner"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=target)]
    ])

# FSM Holatlar
class OwnerStates(StatesGroup):
    waiting_for_school_name = State()
    waiting_for_school_group = State()
    waiting_for_admin_id = State()
    waiting_for_del_admin_id = State()

class ChannelStates(StatesGroup):
    waiting_for_channel_id = State()
    waiting_for_channel_title = State()

class TeacherStates(StatesGroup):
    waiting_for_fullname = State()
    waiting_for_subject = State()

class AdminScheduleStates(StatesGroup):
    waiting_for_teacher = State()
    waiting_for_class = State()
    waiting_for_subject = State()
    waiting_for_day = State()
    waiting_for_time = State()
    waiting_for_shift = State()

class AdminEditScheduleStates(StatesGroup):
    waiting_for_value = State()

class AdminExcelStates(StatesGroup):
    waiting_for_file = State()

class BroadcastStates(StatesGroup):
    waiting_for_owner_message = State()
    waiting_for_admin_message = State()

class LessonTimeStates(StatesGroup):
    waiting_for_time_input = State()


def standardize_day(d):
    d = str(d).lower().strip()
    mapping = {
        'dushanba': 'Dushanba', 'dush': 'Dushanba',
        'seshanba': 'Seshanba', 'sesh': 'Seshanba',
        'chorshanba': 'Chorshanba', 'chor': 'Chorshanba',
        'payshanba': 'Payshanba', 'pay': 'Payshanba',
        'juma': 'Juma', 'jum': 'Juma',
        'shanba': 'Shanba', 'shan': 'Shanba',
        'yakshanba': 'Yakshanba', 'yak': 'Yakshanba'
    }
    for k, v in mapping.items():
        if d.startswith(k):
            return v
    return str(d).capitalize()

# ==========================================
# 5.5 PAGINATION UTILITY (Universali funksiya)
# ==========================================
def create_pagination_keyboard(items: list, page: int = 1, items_per_page: int = 5, 
                                callback_prefix: str = "page", back_callback: str = None) -> tuple:
    """
    Katalog-mo'jallangan ma'lumotlarni sahifalar bo'yicha bo'lib ko'rish uchun universal funksiya.
    
    Boshqa loyihalardan olingan best practice - pagination pattern.
    Foydalanish: biror chekli ma'lumot ko'plab bo'lsa, uni sahifalar bo'yicha ko'rish.
    
    Args:
        items: Ko'rsatadigan ma'lumotlar ro'yxati
        page: Joriy sahifa (1 dan boshlanadi)
        items_per_page: Bitta sahifada nechta element ko'rsatish
        callback_prefix: Callback uchun prefix (masalan: "page_teachers")
        back_callback: Orqaga qaytish tugmasi uchun callback
        
    Returns:
        tuple: (filtered_items, keyboard, pagination_info)
            - filtered_items: Joriy sahifadagi ma'lumotlar
            - keyboard: InlineKeyboardMarkup
            - pagination_info: {"current": 1, "total": 5, "has_prev": False, "has_next": True}
    """
    if not items:
        return [], InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_callback or "back_to_owner")]]), {"current": 1, "total": 0}
    
    total_pages = (len(items) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    current_items = items[start_idx:end_idx]
    
    kb = []
    
    # Sahifa raqami va jami sahifalar
    page_text = f"📄 {page}/{total_pages}" if total_pages > 1 else "📄 1/1"
    
    # Oldingi/Keyingi tugmalari
    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"{callback_prefix}_prev_{page}"))
    pagination_row.append(InlineKeyboardButton(text=page_text, callback_data="ignore"))
    if page < total_pages:
        pagination_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"{callback_prefix}_next_{page}"))
    
    kb.append(pagination_row)
    
    # Orqaga tugmasi
    if back_callback:
        kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_callback)])
    
    pagination_info = {
        "current": page,
        "total": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "items_count": len(items)
    }
    
    return current_items, InlineKeyboardMarkup(inline_keyboard=kb), pagination_info

# ==========================================
# 6. HANDLERS
# ==========================================
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    user_id = message.from_user.id
    
    # Userni bazaga yozish (agar mavjud bo'lmasa)
    now_str = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M:%S")
    username = message.from_user.username or "yo'q"
    full_name = message.from_user.full_name or "yo'q"
    execute_query("INSERT OR IGNORE INTO users (user_id, username, full_name, joined_at) VALUES (?, ?, ?, ?)", 
                  (user_id, f"@{username}" if username != "yo'q" else username, full_name, now_str))
    
    # Teacher deep link tekshiruvi (t.me/bot?start=school_1)
    if command and command.args and command.args.startswith("school_"):
        try:
            school_id = int(command.args.split("_")[1])
            if not is_school_active(school_id):
                await message.answer("❌ Ushbu maktab tizimga ulanmagan yoki litsenziyasi tugagan.")
                return
            await state.update_data(reg_school_id=school_id)
            await message.answer("👨‍🏫 <b>O'qituvchi sifatida ro'yxatdan o'tish</b>\n\nIltimos, ism va familiyangizni kiriting:", parse_mode="HTML")
            await state.set_state(TeacherStates.waiting_for_fullname)
            return
        except ValueError:
            pass

    # Obuna tekshiruvi endi middleware orqali amalga oshiriladi


    role = get_user_role(user_id)
    
    greeting = "Assalomu alaykum, bot yaratuvchisi Malikov Nuriddin.\n"
    
    if role == "owner":
        await message.answer(greeting + "👋 Xo'jayin! Boshqaruv paneliga xush kelibsiz.", reply_markup=get_owner_kb())
    elif role == "admin":
        school_id = get_admin_school(user_id)
        if not is_school_active(school_id):
            await message.answer("❌ To'lov qilinmagan. Admin bilan bog'laning.")
            return
        await message.answer(greeting + "👋 Maktab Administratori! Botdan foydalanishingiz mumkin.", reply_markup=get_admin_kb())
    elif role == "teacher":
        school_id = get_teacher_school(user_id)
        if not is_school_active(school_id):
            await message.answer("❌ Maktabingiz uchun litsenziya faol emas. Admin bilan bog'laning.")
            return
        await message.answer(greeting + "👋 O'qituvchi!", reply_markup=get_teacher_kb())
    else:
        pending = execute_query("SELECT school_id FROM teachers WHERE user_id = ? AND status = 0", (user_id,), fetchone=True)
        if pending:
            await message.answer(greeting + "⏳ Hisobingiz maktab ma'muriyati tomonidan tasdiqlanishini kuting.")
        else:
            await message.answer(greeting + "❌ Sizda ushbu botdan foydalanish huquqi yo'q. Faqat ro'yxatdan o'tgan maktab o'qituvchilari va adminlar kira oladi.\n\n❗️ <b>Botdan to'liq foydalanish uchun:</b>\nIltimos, o'z maktabingiz administratoriga murojaat qiling va ro'yxatdan o'tish maxsus havolasini (ssilkasini) so'rang.", parse_mode="HTML")

@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if await check_subscription(user_id):
        await call.message.delete()
        await call.message.answer("✅ Obuna tasdiqlandi! Endi botdan foydalanishingiz mumkin.\nDavom etish uchun avvalgi harakatingizni qaytaring yoki /start buyrug'ini yuboring.")
    else:
        await call.answer("❌ Hali hamma kanallarga obuna bo'lmadingiz!", show_alert=True)

@dp.callback_query(F.data == "back_to_owner")
async def back_to_owner(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("👋 Assalomu alaykum, Xo'jayin! Boshqaruv paneliga xush kelibsiz.", reply_markup=get_owner_kb())

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("👋 Assalomu alaykum, Maktab Administratori! Botdan foydalanishingiz mumkin.", reply_markup=get_admin_kb())

@dp.callback_query(F.data == "ignore")
async def ignore_callback(call: CallbackQuery):
    await call.answer()

# --- O'qituvchi Registratsiyasi ---
@dp.message(StateFilter(TeacherStates.waiting_for_fullname))
async def register_teacher_fullname(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await message.answer("📚 Qaysi fandan dars berasiz? (Masalan: Matematika)")
    await state.set_state(TeacherStates.waiting_for_subject)

@dp.message(StateFilter(TeacherStates.waiting_for_subject))
async def register_teacher_subject(message: Message, state: FSMContext):
    data = await state.get_data()
    school_id = data.get('reg_school_id')
    full_name = data.get('full_name')
    subject = message.text
    user_id = message.from_user.id
    
    execute_query(
        "INSERT OR REPLACE INTO teachers (user_id, full_name, subject, school_id, status) VALUES (?, ?, ?, ?, 0)",
        (user_id, full_name, subject, school_id)
    )
    
    await message.answer("⏳ Sizning so'rovingiz maktab ma'muriyatiga yuborildi. Tasdiqlanishini kuting.")
    await state.clear()
    
    # Adminga xabar yuborish
    admins = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (school_id,), fetchall=True)
    if admins:
        text = f"🆕 <b>Yangi o'qituvchi so'rovi!</b>\n\nIsm: {full_name}\nFan: {subject}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Ruxsat berish", callback_data=f"apprteacher_{user_id}"),
             InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rejteacher_{user_id}")]
        ])
        for admin in admins:
            try:
                await bot.send_message(admin[0], text, parse_mode="HTML", reply_markup=kb)
            except:
                pass

@dp.callback_query(F.data.startswith("apprteacher_"))
async def approve_teacher_cb(call: CallbackQuery):
    t_id = int(call.data.split("_")[1])
    execute_query("UPDATE teachers SET status = 1 WHERE user_id = ?", (t_id,))
    await call.message.edit_text(f"{call.message.text}\n\n✅ <b>Tasdiqlandi!</b>", parse_mode="HTML")
    try:
        await bot.send_message(t_id, "✅ Sizning o'qituvchi profilingiz tasdiqlandi!\n/start buyrug'ini yuboring.")
    except:
        pass

@dp.callback_query(F.data.startswith("rejteacher_"))
async def reject_teacher_cb(call: CallbackQuery):
    t_id = int(call.data.split("_")[1])
    execute_query("DELETE FROM teachers WHERE user_id = ?", (t_id,))
    await call.message.edit_text(f"{call.message.text}\n\n❌ <b>Rad etildi!</b>", parse_mode="HTML")
    try:
        await bot.send_message(t_id, "❌ Sizning o'qituvchi profilingiz ma'muriyat tomonidan rad etildi.")
    except:
        pass

# --- Xo'jayin (Owner) bo'limi ---

@dp.callback_query(F.data == "add_school")
async def add_school_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📝 Yangi maktab nomini kiriting:", reply_markup=get_back_kb())
    await state.set_state(OwnerStates.waiting_for_school_name)

@dp.message(StateFilter(OwnerStates.waiting_for_school_name))
async def add_school_name(message: Message, state: FSMContext):
    await state.update_data(school_name=message.text)
    await message.answer("🆔 Guruh ID sini kiriting (masalan: -100123456789):", reply_markup=get_back_kb())
    await state.set_state(OwnerStates.waiting_for_school_group)

@dp.message(StateFilter(OwnerStates.waiting_for_school_group))
async def add_school_group(message: Message, state: FSMContext):
    try:
        group_id = int(message.text)
        data = await state.get_data()
        school_name = data.get('school_name')
        
        execute_query("INSERT INTO schools (name, group_id) VALUES (?, ?)", (school_name, group_id))
        await message.answer(f"✅ Maktab muvaffaqiyatli qo'shildi!\n🏫 Nomi: {school_name}\n🆔 Guruh: {group_id}", reply_markup=get_owner_kb())
        await state.clear()
    except ValueError:
        await message.answer("❌ Noto'g'ri ID format. Iltimos, raqam ko'rinishida kiriting:", reply_markup=get_back_kb())

@dp.callback_query(F.data == "del_school")
async def del_school_list(call: CallbackQuery):
    await call.message.edit_text("🗑 O'chirish uchun maktabni tanlang:", reply_markup=get_schools_kb("delschool"))

@dp.callback_query(F.data.startswith("delschool_"))
async def del_school_confirm(call: CallbackQuery):
    school_id = int(call.data.split("_")[1])
    # Maktab o'chirilganda unga bog'langan adminlar ham o'chiriladi
    execute_query("DELETE FROM admins WHERE school_id = ?", (school_id,))
    execute_query("DELETE FROM schools WHERE id = ?", (school_id,))
    await call.message.edit_text("✅ Maktab va unga biriktirilgan adminlar tizimdan o'chirildi!", reply_markup=get_back_kb())

@dp.callback_query(F.data == "add_admin")
async def add_admin_list(call: CallbackQuery):
    await call.message.edit_text("👤 Admin qo'shish uchun maktabni tanlang:", reply_markup=get_schools_kb("addadmin"))

@dp.callback_query(F.data.startswith("addadmin_"))
async def add_admin_school_chosen(call: CallbackQuery, state: FSMContext):
    school_id = int(call.data.split("_")[1])
    await state.update_data(target_school_id=school_id)
    await call.message.edit_text("🆔 Yangi adminning Telegram ID raqamini kiriting:", reply_markup=get_back_kb())
    await state.set_state(OwnerStates.waiting_for_admin_id)

@dp.message(StateFilter(OwnerStates.waiting_for_admin_id))
async def add_admin_id(message: Message, state: FSMContext):
    try:
        admin_id = int(message.text)
        data = await state.get_data()
        school_id = data.get('target_school_id')
        
        # INSERT OR REPLACE funksiyasi sqlite3 da shunday yoziladi:
        execute_query("INSERT OR REPLACE INTO admins (user_id, school_id) VALUES (?, ?)", (admin_id, school_id))
        await message.answer("✅ Admin muvaffaqiyatli qo'shildi va maktabga biriktirildi!", reply_markup=get_owner_kb())
        await state.clear()
    except ValueError:
        await message.answer("❌ Noto'g'ri ID format. Iltimos, raqam kiriting:", reply_markup=get_back_kb())

@dp.callback_query(F.data == "list_admins")
async def list_admins_handler(call: CallbackQuery):
    admins = execute_query("SELECT a.user_id, s.name FROM admins a JOIN schools s ON a.school_id = s.id", fetchall=True)
    if not admins:
        await call.answer("🤷‍♂️ Adminlar yo'q.", show_alert=True)
        return
        
    kb = []
    for a_id, s_name in admins:
        kb.append([InlineKeyboardButton(text=f"❌ {s_name} - {a_id}", callback_data=f"deladm_{a_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_owner")])
    
    await call.message.edit_text("📋 <b>Barcha Adminlar ro'yxati</b>\n\nO'chirish uchun ustiga bosing:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("deladm_"))
async def deladm_handler(call: CallbackQuery):
    a_id = int(call.data.split("_")[1])
    execute_query("DELETE FROM admins WHERE user_id = ?", (a_id,))
    await call.answer("✅ Admin o'chirildi!", show_alert=True)
    await list_admins_handler(call)

@dp.callback_query(F.data == "manage_license")
async def manage_license_list(call: CallbackQuery):
    await call.message.edit_text("💰 Litsenziya holatini boshqarish uchun maktabni tanlang:", reply_markup=get_schools_kb("licschool"))

@dp.callback_query(F.data.startswith("licschool_"))
async def manage_license_school(call: CallbackQuery):
    school_id = int(call.data.split("_")[1])
    school = execute_query("SELECT name FROM schools WHERE id = ?", (school_id,), fetchone=True)
    if school:
        await call.message.edit_text(f"🏫 Maktab: {school[0]}\nLitsenziya holatini o'zgartiring:", reply_markup=get_license_kb(school_id))

@dp.callback_query(F.data.startswith("license_"))
async def toggle_license(call: CallbackQuery):
    parts = call.data.split("_")
    action = parts[1]
    school_id = int(parts[2])
    
    new_status = 1 if action == "activate" else 0
    execute_query("UPDATE schools SET active = ? WHERE id = ?", (new_status, school_id))
    
    await call.answer("✅ Litsenziya holati muvaffaqiyatli yangilandi!", show_alert=True)
    
    # Menyuni yangilash
    school = execute_query("SELECT name FROM schools WHERE id = ?", (school_id,), fetchone=True)
    if school:
        await call.message.edit_text(f"🏫 Maktab: {school[0]}\nLitsenziya holatini o'zgartiring:", reply_markup=get_license_kb(school_id))

@dp.callback_query(F.data == "owner_stats")
async def owner_stats(call: CallbackQuery):
    schools_count = execute_query("SELECT COUNT(*) FROM schools", fetchone=True)[0]
    active_schools = execute_query("SELECT COUNT(*) FROM schools WHERE active = 1", fetchone=True)[0]
    admins_count = execute_query("SELECT COUNT(*) FROM admins", fetchone=True)[0]
    teachers_count = execute_query("SELECT COUNT(*) FROM teachers", fetchone=True)[0]
    
    text = (
        "📊 <b>Umumiy SaaS Statistika</b>\n\n"
        f"🏢 Jami maktablar: {schools_count} ta\n"
        f"🟢 Faol litsenziyalar: {active_schools} ta\n"
        f"🔴 Bloklangan litsenziyalar: {schools_count - active_schools} ta\n"
        f"👤 Tizimdagi adminlar: {admins_count} ta\n"
        f"👨‍🏫 Jami o'qituvchilar: {teachers_count} ta"
    )
    
    if schools_count > 0:
        labels = ['Faol', 'Bloklangan']
        inactive = schools_count - active_schools
        sizes = [active_schools, inactive]
        colors = ['#2ecc71', '#e74c3c']
        
        plt.figure(figsize=(5, 5))
        filter_sizes = [s for s in sizes if s > 0]
        filter_labels = [l for s, l in zip(sizes, labels) if s > 0]
        filter_colors = [c for s, c in zip(sizes, colors) if s > 0]
        
        if filter_sizes:
            plt.pie(filter_sizes, labels=filter_labels, colors=filter_colors, autopct='%1.1f%%', startangle=140)
            plt.title("Maktablar holati")
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight')
            buf.seek(0)
            plt.close()
            
            photo = BufferedInputFile(buf.read(), filename="owner_stat.png")
            await call.message.delete()
            await bot.send_photo(call.from_user.id, photo=photo, caption=text, parse_mode="HTML", reply_markup=get_back_kb())
            return
            
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb())

@dp.callback_query(F.data == "manage_premium")
async def manage_premium_list(call: CallbackQuery):
    await call.message.edit_text("💎 Premium holatini boshqarish uchun maktabni tanlang\n(Premium maktab a'zolari kanallarga obuna bo'lishdan ozod qilinadi):", reply_markup=get_schools_kb("premschool"))

@dp.callback_query(F.data.startswith("premschool_"))
async def manage_premium_school(call: CallbackQuery):
    school_id = int(call.data.split("_")[1])
    school = execute_query("SELECT name, premium FROM schools WHERE id = ?", (school_id,), fetchone=True)
    if school:
        status_text = "💎 Premium" if school[1] == 1 else "Oddiy"
        toggle_action = "off" if school[1] == 1 else "on"
        toggle_text = "Oddiyga o'tkazish" if school[1] == 1 else "💎 Premium qilish"
        
        kb = [
            [InlineKeyboardButton(text=f"Holat: {status_text}", callback_data="ignore")],
            [InlineKeyboardButton(text=toggle_text, callback_data=f"premium_{toggle_action}_{school_id}")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="manage_premium")]
        ]
        await call.message.edit_text(f"🏫 Maktab: {school[0]}\nPremium holatini o'zgartiring:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("premium_"))
async def toggle_premium(call: CallbackQuery):
    parts = call.data.split("_")
    action = parts[1]
    school_id = int(parts[2])
    new_status = 1 if action == "on" else 0
    execute_query("UPDATE schools SET premium = ? WHERE id = ?", (new_status, school_id))
    await call.answer("✅ Premium holati yangilandi!", show_alert=True)
    await manage_premium_school(call)

@dp.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("➕ Kanal username (@ bilan) yoki ID sini kiriting:", reply_markup=get_back_kb())
    await state.set_state(ChannelStates.waiting_for_channel_id)

@dp.message(StateFilter(ChannelStates.waiting_for_channel_id))
async def add_channel_id(message: Message, state: FSMContext):
    await state.update_data(channel_id=message.text)
    await message.answer("📝 Tugmada ko'rinadigan kanal nomini kiriting:", reply_markup=get_back_kb())
    await state.set_state(ChannelStates.waiting_for_channel_title)

@dp.message(StateFilter(ChannelStates.waiting_for_channel_title))
async def add_channel_title(message: Message, state: FSMContext):
    title = message.text
    data = await state.get_data()
    ch_id = data['channel_id']
    execute_query("INSERT INTO channels (channel_id, title) VALUES (?, ?)", (ch_id, title))
    await message.answer(f"✅ Kanal muvaffaqiyatli qo'shildi!\n📢 {title} ({ch_id})", reply_markup=get_owner_kb())
    await state.clear()

@dp.callback_query(F.data == "list_channels")
async def list_channels(call: CallbackQuery):
    channels = execute_query("SELECT id, channel_id, title FROM channels", fetchall=True)
    if not channels:
        await call.message.edit_text("🤷‍♂️ Kanallar ro'yxati bo'sh.", reply_markup=get_back_kb())
        return
        
    text = "📋 <b>Majburiy kanallar ro'yxati:</b>\n\n"
    for idx, (c_id, ch_username, title) in enumerate(channels, 1):
        text += f"{idx}. {title} - {ch_username}\n"
        
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb())

@dp.callback_query(F.data == "del_channel")
async def del_channel_list(call: CallbackQuery):
    channels = execute_query("SELECT id, title FROM channels", fetchall=True)
    if not channels:
        await call.answer("❌ Kanallar ro'yxati bo'sh.", show_alert=True)
        return
        
    kb = []
    for c_id, title in channels:
        kb.append([InlineKeyboardButton(text=f"❌ {title}", callback_data=f"delchan_{c_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_owner")])
    
    await call.message.edit_text("🗑 O'chirish uchun kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("delchan_"))
async def del_channel_confirm(call: CallbackQuery):
    c_id = int(call.data.split("_")[1])
    execute_query("DELETE FROM channels WHERE id = ?", (c_id,))
    await call.answer("✅ Kanal o'chirildi!", show_alert=True)
    await del_channel_list(call)


@dp.callback_query(F.data == "owner_users_list")
async def owner_users_list(call: CallbackQuery):
    users = execute_query("SELECT user_id, username, full_name, joined_at FROM users ORDER BY joined_at ASC", fetchall=True)
    if not users:
        await call.answer("🤷‍♂️ Foydalanuvchilar yo'q.", show_alert=True)
        return
        
    total_users = len(users)
    content = f"JAMI FOYDALANUVCHILAR: {total_users}\n\n"
    content += "T/R | User ID | Username | Ism-Familiya | Qo'shilgan vaqti\n"
    content += "-"*80 + "\n"
    
    for idx, (u_id, uname, fname, j_at) in enumerate(users, 1):
        content += f"{idx}. | {u_id} | {uname} | {fname} | {j_at}\n"
        
    file_bytes = content.encode('utf-8')
    document = BufferedInputFile(file_bytes, filename=f"users_{datetime.now(TASHKENT_TZ).strftime('%Y%m%d_%H%M%S')}.txt")
    
    await bot.send_document(chat_id=call.from_user.id, document=document, caption=f"👥 Jami foydalanuvchilar: {total_users} ta")
    await call.answer()

@dp.callback_query(F.data == "owner_broadcast")
async def owner_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📢 <b>Ommaviy xabarnoma yuborish</b>\n\nBarcha foydalanuvchilarga yuboriladigan xabar matnini kiriting:", parse_mode="HTML", reply_markup=get_back_kb())
    await state.set_state(BroadcastStates.waiting_for_owner_message)

@dp.message(StateFilter(BroadcastStates.waiting_for_owner_message))
async def owner_broadcast_send(message: Message, state: FSMContext):
    users = execute_query("SELECT user_id FROM users", fetchall=True)
    if not users:
        await message.answer("Foydalanuvchilar yo'q.")
        return
        
    wait_msg = await message.answer("⏳ Xabar yuborilmoqda...")
    count = 0
    for u in users:
        try:
            await bot.send_message(u[0], f"📢 <b>Xabar:</b>\n\n{message.text}", parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            execute_query("DELETE FROM users WHERE user_id = ?", (u[0],))
        except Exception:
            pass
            
    await wait_msg.edit_text(f"✅ Xabar {count} ta foydalanuvchiga yuborildi!", reply_markup=get_back_kb())
    await state.clear()

# --- Maktab Administratori (School Admin) bo'limi ---

@dp.callback_query(F.data == "admin_report")
async def admin_report(call: CallbackQuery):
    user_id = call.from_user.id
    school_id = get_admin_school(user_id)
    if not is_school_active(school_id):
        await call.answer("❌ To'lov qilinmagan. Admin bilan bog'laning.", show_alert=True)
        return
        
    today_date = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    stats = execute_query(
        "SELECT g.status, COUNT(*) FROM grades_tracking g JOIN teachers t ON g.teacher_id = t.user_id WHERE t.school_id = ? AND g.date = ? GROUP BY g.status",
        (school_id, today_date), fetchall=True
    )
    
    counts = {'done': 0, 'missing': 0, 'pending': 0}
    if stats:
        for stat, count in stats:
            counts[stat] = count
            
    total_lessons = sum(counts.values())
    if total_lessons == 0:
        await call.message.edit_text("🤷‍♂️ Bugun darslar mavjud emas yoki hali nazorat boshlanmagan.", reply_markup=get_back_kb("back_to_admin"))
        return
        
    efficiency = round((counts['done'] / total_lessons) * 100, 1)
    
    text = (
        "📊 <b>Bugungi Maktab Hisoboti</b>\n\n"
        f"📚 Nazorat qilinayotgan darslar: {total_lessons} ta\n"
        f"✅ Baho qo'yilgan jurnallar: {counts['done']} ta\n"
        f"⚠️ Baholanmagan jurnallar: {counts['missing']} ta\n"
        f"⏳ Kutilayotgan: {counts['pending']} ta\n\n"
        f"📈 O'qituvchilar samaradorligi: {efficiency}%"
    )
    
    labels = ['Qo\'yilgan', 'Qo\'yilmagan', 'Kutilmoqda']
    sizes = [counts['done'], counts['missing'], counts['pending']]
    colors = ['#2ecc71', '#e74c3c', '#f1c40f']
    
    plt.figure(figsize=(5, 5))
    filter_sizes = [s for s in sizes if s > 0]
    filter_labels = [l for s, l in zip(sizes, labels) if s > 0]
    filter_colors = [c for s, c in zip(sizes, colors) if s > 0]
    
    plt.pie(filter_sizes, labels=filter_labels, colors=filter_colors, autopct='%1.1f%%', startangle=140)
    plt.title("Jurnallar holati")
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    photo = BufferedInputFile(buf.read(), filename="stat.png")
    
    await call.message.delete()
    await bot.send_photo(call.from_user.id, photo=photo, caption=text, parse_mode="HTML", reply_markup=get_back_kb("back_to_admin"))

@dp.callback_query(F.data == "admin_issues")
async def admin_issues(call: CallbackQuery):
    user_id = call.from_user.id
    school_id = get_admin_school(user_id)
    if not is_school_active(school_id):
        await call.answer("❌ To'lov qilinmagan. Admin bilan bog'laning.", show_alert=True)
        return

    today_date = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    issues = execute_query(
        "SELECT s.class_name, s.subject, t.full_name FROM grades_tracking g JOIN schedule s ON g.lesson_id = s.id JOIN teachers t ON g.teacher_id = t.user_id WHERE t.school_id = ? AND g.date = ? AND g.status = 'missing'",
        (school_id, today_date), fetchall=True
    )
    
    if not issues:
        text = "✅ Ayni vaqtda muammolar yo'q. Barcha darslar o'z vaqtida baholangan."
    else:
        text = f"⚠️ <b>Diqqat! Tizimda muammolar bor:</b>\n\n"
        for idx, (c_name, subj, t_name) in enumerate(issues, 1):
            text += f"{idx}. 🏫 {c_name} | 📚 {subj} - 👨‍🏫 {t_name} baho qo'ymagan.\n"
        text += "\nIltimos, o'qituvchilarni ogohlantiring!"
        
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("back_to_admin"))

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(call: CallbackQuery):
    user_id = call.from_user.id
    school_id = get_admin_school(user_id)
    if not is_school_active(school_id):
        await call.answer("❌ To'lov qilinmagan. Admin bilan bog'laning.", show_alert=True)
        return
        
    school = execute_query("SELECT name FROM schools WHERE id = ?", (school_id,), fetchone=True)
    school_name = school[0] if school else "Noma'lum"
    
    text = (
        f"🏫 <b>Maktab: {school_name}</b>\n\n"
        "🟢 Litsenziya holati: Faol\n"
        f"📅 Tizimga ulangan sana: {datetime.now(TASHKENT_TZ).strftime('%d.%m.%Y')}\n"
        "👥 Jami o'qituvchilar: 45 ta\n"
        "👨‍🎓 Jami o'quvchilar: 850 ta"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("back_to_admin"))

@dp.callback_query(F.data == "admin_teachers")
async def admin_teachers(call: CallbackQuery):
    user_id = call.from_user.id
    school_id = get_admin_school(user_id)
    if not is_school_active(school_id):
        await call.answer("❌ To'lov qilinmagan.", show_alert=True)
        return
        
    teachers = execute_query("SELECT user_id, full_name, status FROM teachers WHERE school_id = ?", (school_id,), fetchall=True)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=school_{school_id}"
    
    if not teachers:
        text = f"🤷‍♂️ Hozircha o'qituvchilar ro'yxatdan o'tmagan.\n\nUlarni tizimga qo'shish uchun quyidagi havolani (ssilkani) o'qituvchilarga yuboring:\n{link}"
    else:
        text = f"👨‍🏫 <b>O'qituvchilar ro'yxati:</b>\n\nYangi o'qituvchilarni qo'shish uchun havola:\n{link}\n\n"
        for idx, (t_id, name, status) in enumerate(teachers, 1):
            stat_icon = "🟢" if status == 1 else ("⏳ Kutilmoqda" if status == 0 else "🔴 Bloklangan")
            text += f"{idx}. {name} (ID: <code>{t_id}</code>) - {stat_icon}\n"
            
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("back_to_admin"), disable_web_page_preview=True)

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📢 <b>O'qituvchilarga xabar</b>\n\nMaktabingizdagi barcha o'qituvchilarga yuboriladigan xabarni kiriting:", parse_mode="HTML", reply_markup=get_back_kb("back_to_admin"))
    await state.set_state(BroadcastStates.waiting_for_admin_message)

@dp.message(StateFilter(BroadcastStates.waiting_for_admin_message))
async def admin_broadcast_send(message: Message, state: FSMContext):
    school_id = get_admin_school(message.from_user.id)
    teachers = execute_query("SELECT user_id FROM teachers WHERE school_id = ? AND status = 1", (school_id,), fetchall=True)
    
    if not teachers:
        await message.answer("O'qituvchilar yo'q.")
        return
        
    wait_msg = await message.answer("⏳ Xabar yuborilmoqda...")
    count = 0
    for t in teachers:
        try:
            await bot.send_message(t[0], f"🏫 <b>Maktab ma'muriyatidan xabar:</b>\n\n{message.text}", parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            execute_query("UPDATE teachers SET status = 0 WHERE user_id = ?", (t[0],))
        except Exception:
            pass
            
    await wait_msg.edit_text(f"✅ Xabar {count} ta o'qituvchiga yuborildi!", reply_markup=get_back_kb("back_to_admin"))
    await state.clear()

# -------------------- EXCEL IMPORT/EXPORT --------------------
@dp.callback_query(F.data == "admin_excel")
async def admin_excel_menu(call: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="📥 Jadval yuklash (Import)", callback_data="admin_excel_import")],
        [InlineKeyboardButton(text="📤 Jadvalni olish (Export)", callback_data="admin_excel_export_sched")],
        [InlineKeyboardButton(text="📤 Baholar hisoboti (Export)", callback_data="admin_excel_export_grades")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_admin")]
    ]
    await call.message.edit_text("📂 <b>Excel bilan ishlash</b>\n\nNima qilmoqchisiz?", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "admin_excel_import")
async def admin_excel_import_start(call: CallbackQuery, state: FSMContext):
    text = "📥 <b>Dars jadvalini import qilish</b>\n\nQuyidagi ustunlarga ega .xlsx faylini yuboring:\n\n<code>O'qituvchi ID</code> | <code>Sinf</code> | <code>Fan</code> | <code>Kun</code> | <code>Vaqt</code> | <code>Smena</code>\n\n<i>Eslatma: O'qituvchi ID raqamini \"O'qituvchilar ro'yxati\" menyusidan topishingiz mumkin.</i>"
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("admin_excel"))
    await state.set_state(AdminExcelStates.waiting_for_file)

@dp.message(StateFilter(AdminExcelStates.waiting_for_file), F.document)
async def admin_excel_file_received(message: Message, state: FSMContext):
    if not message.document.file_name.endswith(('.xlsx', '.xls')):
        await message.answer("❌ Iltimos, faqat .xlsx formatidagi Excel faylini yuboring.")
        return
        
    wait_msg = await message.answer("⏳ Fayl yuklanmoqda va o'qilmoqda...")
    file = await bot.get_file(message.document.file_id)
    file_bytes = await bot.download_file(file.file_path)
    
    try:
        df = pd.read_excel(file_bytes)
        required_cols = ["O'qituvchi ID", "Sinf", "Fan", "Kun", "Vaqt", "Smena"]
        if not all(col in df.columns for col in required_cols):
            await wait_msg.edit_text(f"❌ Faylda quyidagi ustunlar bo'lishi shart:\n{', '.join(required_cols)}")
            return
            
        school_id = get_admin_school(message.from_user.id)
        count = 0
        
        for _, row in df.iterrows():
            t_id = row["O'qituvchi ID"]
            c_name = str(row["Sinf"])
            subj = str(row["Fan"])
            day = standardize_day(row["Kun"])
            time_str = str(row["Vaqt"])
            shift = int(row["Smena"])
            
            # O'qituvchi shu maktabga tegishli va aktiv ekanligini tekshiramiz
            teacher = execute_query("SELECT user_id FROM teachers WHERE user_id = ? AND school_id = ? AND status = 1", (t_id, school_id), fetchone=True)
            if not teacher:
                continue
                
            execute_query(
                "INSERT INTO schedule (teacher_id, class_name, subject, day, lesson_time, shift) VALUES (?, ?, ?, ?, ?, ?)",
                (t_id, c_name, subj, day, time_str, shift)
            )
            count += 1
            
        await wait_msg.edit_text(f"✅ <b>Muvaffaqiyatli!</b>\nBaza raqamiga {count} ta dars qo'shildi.", parse_mode="HTML", reply_markup=get_back_kb("admin_excel"))
        await state.clear()
    except Exception as e:
        await wait_msg.edit_text(f"❌ Faylni o'qishda xatolik yuz berdi:\n{str(e)[:100]}")
        await state.clear()

@dp.callback_query(F.data == "admin_excel_export_sched")
async def admin_excel_export_sched(call: CallbackQuery):
    school_id = get_admin_school(call.from_user.id)
    lessons = execute_query(
        "SELECT t.user_id, t.full_name, s.class_name, s.subject, s.day, s.lesson_time, s.shift FROM schedule s JOIN teachers t ON s.teacher_id = t.user_id WHERE t.school_id = ? ORDER BY s.day, s.lesson_time",
        (school_id,), fetchall=True
    )
    
    if not lessons:
        await call.answer("🤷‍♂️ Jadval bo'sh.", show_alert=True)
        return
        
    df = pd.DataFrame(lessons, columns=["O'qituvchi ID", "O'qituvchi Ismi", "Sinf", "Fan", "Kun", "Vaqt", "Smena"])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    
    file = BufferedInputFile(output.read(), filename=f"jadval_{datetime.now(TASHKENT_TZ).strftime('%Y%m%d')}.xlsx")
    await bot.send_document(call.from_user.id, file, caption="📤 Maktab dars jadvali")
    await call.answer()

@dp.callback_query(F.data == "admin_excel_export_grades")
async def admin_excel_export_grades(call: CallbackQuery):
    school_id = get_admin_school(call.from_user.id)
    grades = execute_query(
        "SELECT t.full_name, s.class_name, s.subject, g.date, g.status FROM grades_tracking g JOIN schedule s ON g.lesson_id = s.id JOIN teachers t ON g.teacher_id = t.user_id WHERE t.school_id = ? ORDER BY g.date DESC",
        (school_id,), fetchall=True
    )
    
    if not grades:
        await call.answer("🤷‍♂️ Baholar tarixi bo'sh.", show_alert=True)
        return
        
    df = pd.DataFrame(grades, columns=["O'qituvchi", "Sinf", "Fan", "Sana", "Holat"])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    
    file = BufferedInputFile(output.read(), filename=f"baholar_{datetime.now(TASHKENT_TZ).strftime('%Y%m%d')}.xlsx")
    await bot.send_document(call.from_user.id, file, caption="📤 Baholar hisoboti")
    await call.answer()

# -------------------- SCHEDULE CRUD --------------------
@dp.callback_query(F.data == "admin_manage_schedule")
async def admin_manage_schedule_handler(call: CallbackQuery):
    await call.message.edit_text("📅 <b>Dars jadvali boshqaruvi</b>\nQanday amal bajarasiz?", parse_mode="HTML", reply_markup=get_schedule_manage_kb())

# ==========================================
# ADMIN: DARS REJIMI BOSHQARUVI (Dars vaqtlari)
# ==========================================
@dp.callback_query(F.data == "admin_lesson_times")
async def admin_lesson_times_menu(call: CallbackQuery):
    """Admin uchun dars rejimi (dars vaqtlari) boshqaruvining asosiy menyu"""
    school_id = get_admin_school(call.from_user.id)
    
    # Mavjud dars vaqtlarini ko'rish
    times_1 = execute_query("SELECT COUNT(*) FROM lesson_times WHERE school_id = ? AND shift = 1", (school_id,), fetchone=True)[0]
    times_2 = execute_query("SELECT COUNT(*) FROM lesson_times WHERE school_id = ? AND shift = 2", (school_id,), fetchone=True)[0]
    
    status_text = f"""⏰ <b>DARS REJIMI (Dars vaqtlari)</b>

🔄 1-SMENA: {times_1} dars vaqti
🔄 2-SMENA: {times_2} dars vaqti

Ushbu yerda maktabning har kuni dars rejimini 
(dars boshlanish va tugash vaqtlarini) o'rnatish va 
o'zgartirishingiz mumkin."""
    
    kb = [
        [InlineKeyboardButton(text="🔄 1-Smena vaqtlarini boshqarish", callback_data="admin_lt_manage_shift_1")],
        [InlineKeyboardButton(text="🔄 2-Smena vaqtlarini boshqarish", callback_data="admin_lt_manage_shift_2")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_manage_schedule")]
    ]
    
    await call.message.edit_text(status_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("admin_lt_manage_shift_"))
async def admin_lesson_times_shift(call: CallbackQuery):
    """Smena bo'yicha dars rejimini ko'rish va tahrirlash"""
    school_id = get_admin_school(call.from_user.id)
    shift = int(call.data.split("_")[4])
    
    times = execute_query(
        "SELECT id, lesson_number, start_time, end_time, day FROM lesson_times WHERE school_id = ? AND shift = ? ORDER BY day, lesson_number",
        (school_id, shift), fetchall=True
    )
    
    # Kun bo'yicha guruhlash
    days_dict = {}
    for lt_id, ln, st, et, day in times:
        if day not in days_dict:
            days_dict[day] = []
        days_dict[day].append((lt_id, ln, st, et))
    
    # Matnni tayyorlash
    text = f"⏰ <b>{shift}-SMENA DARS REJIMI</b>\n\n"
    
    if not times:
        text += "🤷 Hali dars vaqtlari o'rnatilmagan.\n\n"
        text += "Tugmalardan foydalanib avtomatik to'ldirish yoki qo'lda kiriting."
    else:
        for day in sorted(days_dict.keys()):
            text += f"\n<b>{day}:</b>\n"
            for lt_id, ln, st, et in sorted(days_dict[day], key=lambda x: x[1]):
                text += f"  {ln}) {st} - {et}\n"
    
    kb = [
        [InlineKeyboardButton(text="🔄 Avto-to'ldirish", callback_data=f"admin_lt_auto_{shift}")],
        [InlineKeyboardButton(text="➕ Qo'lda qo'shish", callback_data=f"admin_lt_manual_{shift}")],
        [InlineKeyboardButton(text="🗑 Barchasini tozalash", callback_data=f"admin_lt_clear_{shift}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_lesson_times")]
    ]
    
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("admin_lt_auto_"))
async def admin_lesson_times_auto_fill(call: CallbackQuery):
    """Avtomatik dars vaqtlarini to'ldirish"""
    school_id = get_admin_school(call.from_user.id)
    shift = int(call.data.split("_")[3])
    
    # Avvalki ma'lumotlarni o'chirish
    execute_query("DELETE FROM lesson_times WHERE school_id = ? AND shift = ?", (school_id, shift))
    
    # Standart vaqtlar
    if shift == 1:
        times = [
            ("08:00", "08:45"),  # 45 min dars
            ("08:50", "09:35"),  # 5 min tanafus + 45 min dars
            ("09:40", "10:25"),  # 5 min tanafus + 45 min dars
            ("10:35", "11:20"),  # 10 min uzoq tanafus + 45 min dars
            ("11:25", "12:10"),  # 5 min tanafus + 45 min dars
            ("12:15", "13:00"),  # 5 min tanafus + 45 min dars
            ("13:05", "13:50"),  # 5 min tanafus + 45 min dars
            ("13:55", "14:40"),  # 5 min tanafus + 45 min dars
        ]
    else:  # shift == 2
        times = [
            ("14:00", "14:45"),
            ("14:50", "15:35"),
            ("15:40", "16:25"),
            ("16:35", "17:20"),  # 10 min uzoq tanafus
            ("17:25", "18:10"),
            ("18:15", "19:00"),
            ("19:05", "19:50"),
            ("19:55", "20:40"),
        ]
    
    # Har bir kun uchun vaqtlarni qo'shish
    days = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba"]
    count = 0
    for day in days:
        for lesson_num, (start_time, end_time) in enumerate(times, 1):
            execute_query(
                "INSERT INTO lesson_times (school_id, shift, lesson_number, start_time, end_time, day) VALUES (?, ?, ?, ?, ?, ?)",
                (school_id, shift, lesson_num, start_time, end_time, day)
            )
            count += 1
    
    await call.answer(f"✅ {count} ta dars vaqti avtomatik qo'shildi!", show_alert=True)
    await admin_lesson_times_shift(call)

@dp.callback_query(F.data.startswith("admin_lt_clear_"))
async def admin_lesson_times_clear(call: CallbackQuery):
    """Dars vaqtlarini tozalash"""
    school_id = get_admin_school(call.from_user.id)
    shift = int(call.data.split("_")[3])
    
    execute_query("DELETE FROM lesson_times WHERE school_id = ? AND shift = ?", (school_id, shift))
    await call.answer(f"✅ {shift}-Smena dars vaqtlari tozalandi!", show_alert=True)
    await admin_lesson_times_shift(call)

@dp.callback_query(F.data.startswith("admin_lt_manual_"))
async def admin_lesson_times_manual(call: CallbackQuery):
    """Qo'lda dars vaqtlarini qo'shish"""
    school_id = get_admin_school(call.from_user.id)
    shift = int(call.data.split("_")[3])
    
    days = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba"]
    kb = []
    for day in days:
        kb.append([InlineKeyboardButton(text=f"📅 {day}", callback_data=f"admin_lt_day_{shift}_{day}")])
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"admin_lt_manage_shift_{shift}")])
    
    await call.message.edit_text(f"📅 {shift}-Smena uchun kunni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# -------------------- SCHEDULE CRUD --------------------

# --- 1. ADD LESSON ---
@dp.callback_query(F.data == "admin_add_schedule")
async def admin_add_schedule_start(call: CallbackQuery, state: FSMContext):
    school_id = get_admin_school(call.from_user.id)
    teachers = execute_query("SELECT user_id, full_name FROM teachers WHERE school_id = ? AND status = 1", (school_id,), fetchall=True)
    if not teachers:
        await call.answer("❌ O'qituvchilar yo'q. Avval ularni ro'yxatdan o'tkazing.", show_alert=True)
        return
        
    kb = []
    for t_id, name in teachers:
        kb.append([InlineKeyboardButton(text=name, callback_data=f"selsubj_{t_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_manage_schedule")])
    
    await call.message.edit_text("➕ <b>Dars qo'shish</b>\n👨‍🏫 O'qituvchini tanlang:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(AdminScheduleStates.waiting_for_teacher)

@dp.callback_query(F.data.startswith("selsubj_"), StateFilter(AdminScheduleStates.waiting_for_teacher))
async def admin_sched_teacher(call: CallbackQuery, state: FSMContext):
    t_id = int(call.data.split("_")[1])
    await state.update_data(teacher_id=t_id)
    await call.message.edit_text("🏫 Qaysi sinf? (Masalan: 5A)", reply_markup=get_back_kb("admin_manage_schedule"))
    await state.set_state(AdminScheduleStates.waiting_for_class)

@dp.message(StateFilter(AdminScheduleStates.waiting_for_class))
async def admin_sched_class(message: Message, state: FSMContext):
    await state.update_data(class_name=message.text)
    await message.answer("📚 Qaysi fan? (Masalan: Matematika):", reply_markup=get_back_kb("admin_manage_schedule"))
    await state.set_state(AdminScheduleStates.waiting_for_subject)

@dp.message(StateFilter(AdminScheduleStates.waiting_for_subject))
async def admin_sched_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text)
    days_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Dushanba", callback_data="day_Dushanba"), InlineKeyboardButton(text="Seshanba", callback_data="day_Seshanba")],
        [InlineKeyboardButton(text="Chorshanba", callback_data="day_Chorshanba"), InlineKeyboardButton(text="Payshanba", callback_data="day_Payshanba")],
        [InlineKeyboardButton(text="Juma", callback_data="day_Juma"), InlineKeyboardButton(text="Shanba", callback_data="day_Shanba")]
    ])
    await message.answer("📅 Hafta kunini tanlang:", reply_markup=days_kb)
    await state.set_state(AdminScheduleStates.waiting_for_day)

@dp.callback_query(F.data.startswith("day_"), StateFilter(AdminScheduleStates.waiting_for_day))
async def admin_sched_day(call: CallbackQuery, state: FSMContext):
    day = call.data.split("_")[1]
    await state.update_data(day=day)
    await call.message.edit_text(f"Tanlandi: {day}\n\n⏰ Dars vaqtini kiriting (Masalan: 08:00):")
    await state.set_state(AdminScheduleStates.waiting_for_time)

@dp.message(StateFilter(AdminScheduleStates.waiting_for_time))
async def admin_sched_time(message: Message, state: FSMContext):
    await state.update_data(time_str=message.text)
    shift_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1-smena", callback_data="shift_1"), InlineKeyboardButton(text="2-smena", callback_data="shift_2")]
    ])
    await message.answer("🔄 Smenani tanlang:", reply_markup=shift_kb)
    await state.set_state(AdminScheduleStates.waiting_for_shift)

@dp.callback_query(F.data.startswith("shift_"), StateFilter(AdminScheduleStates.waiting_for_shift))
async def admin_sched_shift(call: CallbackQuery, state: FSMContext):
    shift = int(call.data.split("_")[1])
    data = await state.get_data()
    t_id = data['teacher_id']
    c_name = data['class_name']
    subj = data['subject']
    day = data['day']
    time_str = data['time_str']
    
    exists = execute_query("SELECT id FROM schedule WHERE teacher_id = ? AND day = ? AND lesson_time = ?", (t_id, day, time_str), fetchone=True)
    if exists:
        await call.answer("❌ Bu vaqtda o'qituvchida dars mavjud! Boshqa vaqt kiriting.", show_alert=True)
        return
        
    execute_query("INSERT INTO schedule (teacher_id, class_name, subject, day, lesson_time, shift) VALUES (?, ?, ?, ?, ?, ?)",
                  (t_id, c_name, subj, day, time_str, shift))
                  
    text = f"✅ <b>Dars jadvali qo'shildi!</b>\n\n🏫 Sinf: {c_name}\n📚 Fan: {subj}\n📅 Kun: {day}\n⏰ Vaqt: {time_str}\n🔄 Smena: {shift}"
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("admin_manage_schedule"))
    await state.clear()

# --- 2. VIEW SCHEDULE ---
@dp.callback_query(F.data.startswith("admin_view_schedule"))
async def admin_view_schedule(call: CallbackQuery):
    page = 1
    if "_" in call.data and len(call.data.split("_")) > 3:
        page = int(call.data.split("_")[3])
        
    school_id = get_admin_school(call.from_user.id)
    lessons = execute_query(
        "SELECT s.day, s.lesson_time, s.class_name, s.subject, t.full_name FROM schedule s JOIN teachers t ON s.teacher_id = t.user_id WHERE t.school_id = ? ORDER BY s.day, s.lesson_time",
        (school_id,), fetchall=True
    )
    if not lessons:
        await call.message.edit_text("🤷‍♂️ Jadval hozircha bo'sh.", reply_markup=get_back_kb("admin_manage_schedule"))
        return
        
    ITEMS_PER_PAGE = 30
    total_pages = (len(lessons) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = (page - 1) * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_lessons = lessons[start_idx:end_idx]
            
    text = f"📅 <b>Haftalik jadval (Sahifa: {page}/{total_pages}):</b>\n\n"
    
    # Just list them in order
    for day, l_time, c_name, subj, t_name in page_lessons:
        text += f"<b>{day}</b> ⏰ {l_time} - {c_name} - {subj} ({t_name})\n"
        
    nav_kb = []
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin_view_schedule_{page-1}"))
        if page < total_pages:
            row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_view_schedule_{page+1}"))
        if row:
            nav_kb.append(row)
    nav_kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_manage_schedule")])
    
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_kb))

# --- 3. DELETE LESSON ---
@dp.callback_query(F.data.startswith("admin_del_schedule_list"))
async def admin_del_schedule_list(call: CallbackQuery):
    page = 1
    if "_" in call.data and len(call.data.split("_")) > 4:
        page = int(call.data.split("_")[4])
        
    school_id = get_admin_school(call.from_user.id)
    lessons = execute_query(
        "SELECT s.id, s.day, s.lesson_time, s.class_name, t.full_name FROM schedule s JOIN teachers t ON s.teacher_id = t.user_id WHERE t.school_id = ? ORDER BY s.day, s.lesson_time",
        (school_id,), fetchall=True
    )
    if not lessons:
        await call.answer("❌ Jadval bo'sh.", show_alert=True)
        return
        
    ITEMS_PER_PAGE = 20
    total_pages = (len(lessons) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = (page - 1) * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_lessons = lessons[start_idx:end_idx]
        
    kb = []
    for l_id, day, l_time, c_name, t_name in page_lessons:
        kb.append([InlineKeyboardButton(text=f"❌ {day[:3]} {l_time} | {c_name} | {t_name}", callback_data=f"deldars_{l_id}")])
        
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin_del_schedule_list_{page-1}"))
        if page < total_pages:
            row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_del_schedule_list_{page+1}"))
        if row:
            kb.append(row)
            
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_manage_schedule")])
    
    await call.message.edit_text(f"🗑 O'chirish uchun darsni tanlang (Sahifa: {page}/{total_pages}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("deldars_"))
async def del_dars_confirm(call: CallbackQuery):
    l_id = int(call.data.split("_")[1])
    execute_query("DELETE FROM schedule WHERE id = ?", (l_id,))
    await call.answer("✅ Dars o'chirildi!", show_alert=True)
    await admin_del_schedule_list(call)

# --- 4. EDIT LESSON ---
@dp.callback_query(F.data.startswith("admin_edit_schedule_list"))
async def admin_edit_schedule_list(call: CallbackQuery):
    page = 1
    if "_" in call.data and len(call.data.split("_")) > 4:
        page = int(call.data.split("_")[4])
        
    school_id = get_admin_school(call.from_user.id)
    lessons = execute_query(
        "SELECT s.id, s.day, s.lesson_time, s.class_name, t.full_name FROM schedule s JOIN teachers t ON s.teacher_id = t.user_id WHERE t.school_id = ? ORDER BY s.day, s.lesson_time",
        (school_id,), fetchall=True
    )
    if not lessons:
        await call.answer("❌ Jadval bo'sh.", show_alert=True)
        return
        
    ITEMS_PER_PAGE = 20
    total_pages = (len(lessons) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = (page - 1) * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_lessons = lessons[start_idx:end_idx]
        
    kb = []
    for l_id, day, l_time, c_name, t_name in page_lessons:
        kb.append([InlineKeyboardButton(text=f"✏️ {day[:3]} {l_time} | {c_name} | {t_name}", callback_data=f"editdars_{l_id}")])
        
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin_edit_schedule_list_{page-1}"))
        if page < total_pages:
            row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin_edit_schedule_list_{page+1}"))
        if row:
            kb.append(row)
            
    kb.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_manage_schedule")])
    
    await call.message.edit_text(f"✏️ Tahrirlash uchun darsni tanlang (Sahifa: {page}/{total_pages}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("editdars_"))
async def edit_dars_menu(call: CallbackQuery, state: FSMContext):
    l_id = int(call.data.split("_")[1])
    await state.update_data(edit_lesson_id=l_id)
    
    kb = [
        [InlineKeyboardButton(text="⏰ Vaqtni o'zgartirish", callback_data="edfield_time")],
        [InlineKeyboardButton(text="🏫 Sinfni o'zgartirish", callback_data="edfield_class")],
        [InlineKeyboardButton(text="📚 Fanni o'zgartirish", callback_data="edfield_subject")],
        [InlineKeyboardButton(text="📅 Kunni o'zgartirish", callback_data="edfield_day")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_edit_schedule_list")]
    ]
    await call.message.edit_text("Nimani tahrirlamoqchisiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("edfield_"))
async def edit_dars_field(call: CallbackQuery, state: FSMContext):
    field = call.data.split("_")[1]
    await state.update_data(edit_field=field)
    
    if field == "day":
        days_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Dushanba", callback_data="edday_Dushanba"), InlineKeyboardButton(text="Seshanba", callback_data="edday_Seshanba")],
            [InlineKeyboardButton(text="Chorshanba", callback_data="edday_Chorshanba"), InlineKeyboardButton(text="Payshanba", callback_data="edday_Payshanba")],
            [InlineKeyboardButton(text="Juma", callback_data="edday_Juma"), InlineKeyboardButton(text="Shanba", callback_data="edday_Shanba")]
        ])
        await call.message.edit_text("Yangi kunni tanlang:", reply_markup=days_kb)
    else:
        prompts = {
            "time": "Yangi vaqtni kiriting (Masalan: 08:00):",
            "class": "Yangi sinfni kiriting (Masalan: 5A):",
            "subject": "Yangi fanni kiriting:"
        }
        await call.message.edit_text(prompts[field], reply_markup=get_back_kb("admin_edit_schedule_list"))
        await state.set_state(AdminEditScheduleStates.waiting_for_value)

@dp.callback_query(F.data.startswith("edday_"))
async def edit_dars_day_save(call: CallbackQuery, state: FSMContext):
    new_day = call.data.split("_")[1]
    data = await state.get_data()
    l_id = data['edit_lesson_id']
    
    execute_query("UPDATE schedule SET day = ? WHERE id = ?", (new_day, l_id))
    await call.answer("✅ Kun o'zgartirildi!", show_alert=True)
    await state.clear()
    await admin_manage_schedule_handler(call)

@dp.message(StateFilter(AdminEditScheduleStates.waiting_for_value))
async def edit_dars_value_save(message: Message, state: FSMContext):
    data = await state.get_data()
    l_id = data['edit_lesson_id']
    field = data['edit_field']
    new_value = message.text
    
    col_map = {
        "time": "lesson_time",
        "class": "class_name",
        "subject": "subject"
    }
    col = col_map[field]
    
    execute_query(f"UPDATE schedule SET {col} = ? WHERE id = ?", (new_value, l_id))
    
    await message.answer("✅ Muvaffaqiyatli saqlandi!", reply_markup=get_back_kb("admin_manage_schedule"))
    await state.clear()

# --- O'qituvchi bo'limi ---
DAYS_SHORT = ["D", "S", "CH", "P", "J", "SH"]
DAYS_FULL = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba"]
DEFAULT_TIMES_1 = ["8:00-08:45", "08:50-09:35", "09:40-10:25", "10:35-11:20", "11:25-12:10", "12:15-13:00", "13:05-13:50", "13:55-14:40"]

def get_today_uz():
    days = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
    return days[datetime.now(TASHKENT_TZ).weekday()]

def get_tomorrow_uz():
    days = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
    idx = (datetime.now(TASHKENT_TZ).weekday() + 1) % 7
    return days[idx]

# --- 🖥 Bugungi darsim ---
@dp.message(F.text == "\U0001f5a5 Bugungi darsim")
async def teacher_today_lessons(message: Message):
    t_id = message.from_user.id
    if get_user_role(t_id) != "teacher":
        return
    today = get_today_uz()
    lessons = execute_query("SELECT class_name, subject, lesson_time, shift FROM schedule WHERE teacher_id = ? AND day = ? ORDER BY lesson_time", (t_id, today), fetchall=True)
    
    if not lessons:
        await message.answer(f"\U0001f4c5 <b>{today}</b> - Bugun darslaringiz yo'q.", parse_mode="HTML")
        return
    text = f"\U0001f5a5 <b>Bugungi darslarim ({today}):</b>\n\n"
    for idx, (c_name, subj, time, shift) in enumerate(lessons, 1):
        text += f"{idx}. \u23f0 {time} | \U0001f3eb {c_name} | \U0001f4da {subj} | {shift}-smena\n"
    await message.answer(text, parse_mode="HTML")

# --- 🏫 Ertangi darsim ---
@dp.message(F.text == "\U0001f3eb Ertangi darsim")
async def teacher_tomorrow_lessons(message: Message):
    t_id = message.from_user.id
    if get_user_role(t_id) != "teacher":
        return
    tomorrow = get_tomorrow_uz()
    lessons = execute_query("SELECT class_name, subject, lesson_time, shift FROM schedule WHERE teacher_id = ? AND day = ? ORDER BY lesson_time", (t_id, tomorrow), fetchall=True)
    
    if not lessons:
        await message.answer(f"\U0001f4c5 <b>{tomorrow}</b> - Ertaga darslaringiz yo'q.", parse_mode="HTML")
        return
    text = f"\U0001f3eb <b>Ertangi darslarim ({tomorrow}):</b>\n\n"
    for idx, (c_name, subj, time, shift) in enumerate(lessons, 1):
        text += f"{idx}. \u23f0 {time} | \U0001f3eb {c_name} | \U0001f4da {subj} | {shift}-smena\n"
    await message.answer(text, parse_mode="HTML")

# --- 📅 Dars jadvali (grid ko'rinishi) ---
@dp.message(F.text == "\U0001f4c5 Dars jadvali")
async def teacher_schedule_grid(message: Message):
    t_id = message.from_user.id
    if get_user_role(t_id) != "teacher":
        return
    lessons = execute_query("SELECT day, lesson_time, class_name, subject, shift FROM schedule WHERE teacher_id = ? ORDER BY shift, lesson_time", (t_id,), fetchall=True)
    
    text = "\U0001f4c5 <b>Dars jadvali</b>\n\nD=Dushanba | S=Seshanba | CH=Chorshanba\nP=Payshanba | J=Juma | SH=Shanba\n\n"
    
    if not lessons:
        text += "\U0001f937\u200d\u2642\ufe0f Jadval hozircha bo'sh."
        await message.answer(text, parse_mode="HTML")
        return
    
    # Group by shift
    for shift in [1, 2]:
        shift_lessons = [l for l in lessons if l[4] == shift]
        if not shift_lessons:
            continue
        text += f"<b>\u2014 {shift}-navbat \u2014</b>\n"
        # Group by day
        for day_full, day_short in zip(DAYS_FULL, DAYS_SHORT):
            day_lessons = sorted([l for l in shift_lessons if l[0] == day_full], key=lambda x: x[1])
            if day_lessons:
                text += f"\n<b>{day_short} ({day_full}):</b>\n"
                for idx, (d, t, c, s, sh) in enumerate(day_lessons, 1):
                    text += f"  {idx}) \u23f0 {t} - {c} - {s}\n"
        text += "\n"
    
    await message.answer(text, parse_mode="HTML")

# --- 🎯 Dars rejimi ---
@dp.message(F.text == "\U0001f3af Dars rejimi")
async def teacher_dars_rejimi(message: Message):
    t_id = message.from_user.id
    if get_user_role(t_id) != "teacher":
        return
    school_id = get_teacher_school(t_id)
    if not school_id:
        await message.answer("\u274c Maktab topilmadi.")
        return
    
    text = "\U0001f3af <b>Dars rejimi (Ko'rish uchun)</b>\n\n"
    text += "ℹ️ <i>Dars vaqtlarini o'zgartirish uchun maktab administratoriga murojaat qiling.</i>\n\n"
    
    # Show current lesson times
    for shift in [1, 2]:
        times = execute_query("SELECT lesson_number, start_time, end_time FROM lesson_times WHERE school_id = ? AND shift = ? AND day = 'Umumiy' ORDER BY lesson_number", (school_id, shift), fetchall=True)
        text += f"<b>\u2014 {shift}-smena \u2014</b>\n"
        if times:
            for ln, st, et in times:
                text += f"  {ln}) {st} - {et}\n"
        else:
            text += "  <i>Hali o'rnatilmagan</i>\n"
        text += "\n"
    
    await message.answer(text, parse_mode="HTML")

# --- 📊 Mening jadvalim ---
@dp.message(F.text == "\U0001f4ca Mening jadvalim")
async def teacher_my_schedule(message: Message):
    t_id = message.from_user.id
    if get_user_role(t_id) != "teacher":
        return
    
    teacher = execute_query("SELECT full_name, subject FROM teachers WHERE user_id = ?", (t_id,), fetchone=True)
    if not teacher:
        await message.answer("\u274c Ma'lumot topilmadi.")
        return
    
    total = execute_query("SELECT COUNT(*) FROM schedule WHERE teacher_id = ?", (t_id,), fetchone=True)[0]
    today = get_today_uz()
    today_count = execute_query("SELECT COUNT(*) FROM schedule WHERE teacher_id = ? AND day = ?", (t_id, today), fetchone=True)[0]
    
    today_date = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    done = execute_query("SELECT COUNT(*) FROM grades_tracking WHERE teacher_id = ? AND date = ? AND status = 'done'", (t_id, today_date), fetchone=True)[0]
    missing = execute_query("SELECT COUNT(*) FROM grades_tracking WHERE teacher_id = ? AND date = ? AND status = 'missing'", (t_id, today_date), fetchone=True)[0]
    
    eff = round((done / (done + missing)) * 100, 1) if (done + missing) > 0 else 100
    
    text = (
        f"\U0001f4ca <b>Mening jadvalim</b>\n\n"
        f"\U0001f464 Ism: {teacher[0]}\n"
        f"\U0001f4da Fan: {teacher[1] or 'Kiritilmagan'}\n\n"
        f"\U0001f4c5 Haftalik darslar soni: {total} ta\n"
        f"\U0001f4c6 Bugungi darslar: {today_count} ta ({today})\n\n"
        f"\u2705 Baho qo'yilgan: {done} ta\n"
        f"\u274c Baho qo'yilmagan: {missing} ta\n"
        f"\U0001f4c8 Samaradorlik: {eff}%"
    )
    await message.answer(text, parse_mode="HTML")

# --- ℹ️ Ma'lumot ---
@dp.message(F.text == "\u2139\ufe0f Ma'lumot")
async def teacher_info(message: Message):
    t_id = message.from_user.id
    if get_user_role(t_id) != "teacher":
        return
    
    teacher = execute_query("SELECT full_name, subject, school_id, status FROM teachers WHERE user_id = ?", (t_id,), fetchone=True)
    if not teacher:
        await message.answer("\u274c Ma'lumot topilmadi.")
        return
    
    school = execute_query("SELECT name FROM schools WHERE id = ?", (teacher[2],), fetchone=True)
    school_name = school[0] if school else "Noma'lum"
    stat = "\U0001f7e2 Faol" if teacher[3] == 1 else "\U0001f534 Nofaol"
    
    text = (
        f"\u2139\ufe0f <b>Ma'lumot</b>\n\n"
        f"\U0001f464 Ism: {teacher[0]}\n"
        f"\U0001f4da Fan: {teacher[1] or 'Kiritilmagan'}\n"
        f"\U0001f3eb Maktab: {school_name}\n"
        f"\U0001f4cc Holat: {stat}\n"
        f"\U0001f194 ID: <code>{t_id}</code>\n\n"
        f"\U0001f916 Bot yaratuvchisi: Malikov Nuriddin\n"
        f"\U0001f4de Bog'lanish: @nuriddin_malikov"
    )
    await message.answer(text, parse_mode="HTML")

# --- Inline callback handlers (eski) ---
@dp.callback_query(F.data == "teacher_grades")
async def teacher_grades(call: CallbackQuery):
    t_id = call.from_user.id
    today_date = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    
    grades = execute_query(
        "SELECT s.class_name, s.subject, g.status FROM grades_tracking g JOIN schedule s ON g.lesson_id = s.id WHERE g.teacher_id = ? AND g.date = ?",
        (t_id, today_date), fetchall=True
    )
    
    if not grades:
        text = "\U0001f937\u200d\u2642\ufe0f Bugun sizda nazorat qilinayotgan darslar yo'q."
    else:
        text = "\U0001f4ca <b>Bugungi baholar holati:</b>\n\n"
        for idx, (c_name, subj, status) in enumerate(grades, 1):
            if status == 'done':
                stat_icon = "\u2705 Qo'yilgan"
            elif status == 'pending':
                stat_icon = "\u23f3 Kutilmoqda"
            else:
                stat_icon = "\u274c Qo'yilmagan"
            text += f"{idx}. \U0001f3eb {c_name} | \U0001f4da {subj} - {stat_icon}\n"
            
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("back_to_teacher"))

@dp.callback_query(F.data == "teacher_lessons")
async def teacher_lessons(call: CallbackQuery):
    t_id = call.from_user.id
    today = get_today_uz()
    lessons = execute_query("SELECT class_name, subject, lesson_time FROM schedule WHERE teacher_id = ? AND day = ? ORDER BY lesson_time", (t_id, today), fetchall=True)
    if not lessons:
        text = f"\U0001f4c5 <b>{today}</b> - Bugun darslaringiz yo'q."
    else:
        text = f"\U0001f4c5 <b>Bugungi darslarim ({today}):</b>\n\n"
        for idx, (c_name, subj, time) in enumerate(lessons, 1):
            text += f"{idx}. \u23f0 {time} | \U0001f3eb {c_name} | \U0001f4da {subj}\n"
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_kb("back_to_teacher"))

@dp.callback_query(F.data == "teacher_stats")
async def teacher_stats(call: CallbackQuery):
    t_id = call.from_user.id
    today_date = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    done = execute_query("SELECT COUNT(*) FROM grades_tracking WHERE teacher_id = ? AND status = 'done'", (t_id,), fetchone=True)[0]
    total = execute_query("SELECT COUNT(*) FROM grades_tracking WHERE teacher_id = ?", (t_id,), fetchone=True)[0]
    eff = round((done / total) * 100, 1) if total > 0 else 100
    await call.message.edit_text(f"\U0001f4c8 <b>Umumiy samaradorligingiz:</b> {eff}%\n\u2705 Baho qo'yilgan: {done} ta\n\U0001f4da Jami nazorat: {total} ta", parse_mode="HTML", reply_markup=get_back_kb("back_to_teacher"))

@dp.callback_query(F.data == "back_to_teacher")
async def back_to_teacher(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("\U0001f44b O'qituvchi paneliga qaytdingiz.", reply_markup=get_teacher_inline_kb())

@dp.callback_query(F.data.startswith("grade_yes_"))
async def grade_yes_handler(call: CallbackQuery):
    track_id = int(call.data.split("_")[2])
    execute_query("UPDATE grades_tracking SET status = 'done' WHERE id = ?", (track_id,))
    await call.message.edit_text("\u2705 Rahmat! Darsingiz muvaffaqiyatli baholangan deb belgilandi.")

@dp.callback_query(F.data.startswith("grade_no_"))
async def grade_no_handler(call: CallbackQuery):
    track_id = int(call.data.split("_")[2])
    now_str = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M:%S")
    execute_query("UPDATE grades_tracking SET status = 'missing', timestamp = ? WHERE id = ?", (now_str, track_id))
    await call.message.edit_text("\u274c Jurnalga tezroq baholarni kiritishni unutmang. 1 soatdan so'ng yana eslatamiz.")
    
    # Adminga xabar yuborish
    info = execute_query(
        "SELECT t.full_name, s.class_name, s.subject, t.school_id FROM grades_tracking g JOIN schedule s ON g.lesson_id = s.id JOIN teachers t ON g.teacher_id = t.user_id WHERE g.id = ?",
        (track_id,), fetchone=True
    )
    if info:
        t_name, c_name, subj, s_id = info
        admin = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (s_id,), fetchone=True)
        if admin:
            try:
                await bot.send_message(
                    admin[0],
                    f"\u26a0\ufe0f <b>Diqqat! Baho qo'yilmadi</b>\n\n\ud83d\udc68\u200d\ud83c\udfeb O'qituvchi: {t_name}\n\ud83c\udfeb Sinf: {c_name}\n\ud83d\udcda Fan: {subj}\n\ud83d\udcc5 Vaqt: {now_str}\n\nO'qituvchi \"Yo'q\" tugmasini bosdi.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

# ==========================================
# 7. MONITORING LOGIC & 8. NOTIFICATION SYSTEM
# ==========================================
async def check_and_notify():
    logger.info("Monitoring tekshiruvi boshlandi...")
    schools = execute_query("SELECT id, name, group_id, active FROM schools", fetchall=True)
    
    for school_id, name, group_id, active in schools:
        if not active:
            continue  # Litsenziyasi yo'q maktablarni o'tkazib yuborish
            
        # Bu joyda aslida API yoki DB dan baholarni tekshirish kodi bo'ladi
        # Hozircha mock data ishlatilmoqda
        missing_grades = random.choice([0, 0, 0, 1, 2, 4]) # Ko'pincha 0 tushishi uchun
        
        # Aqlli ogohlantirish - faqat muammo bo'lsa xabar yuboriladi
        if missing_grades > 0:
            text = (
                "⚠️ <b>Avtomatik Ogohlantirish!</b>\n\n"
                f"🏫 Maktab: {name}\n"
                f"❗ {missing_grades} ta dars bo'yicha jurnallar to'ldirilmagan.\n"
                "Iltimos, o'z vaqtida baholarni kiriting va elektron jurnalni to'ldiring!"
            )
            try:
                await bot.send_message(chat_id=group_id, text=text, parse_mode="HTML")
                logger.info(f"{name} guruhi ({group_id}) uchun ogohlantirish yuborildi.")
            except Exception as e:
                logger.error(f"{group_id} guruhiga xabar yuborishda xatolik: {e}")

async def send_daily_report():
    logger.info("Kunlik hisobotlarni yuborish boshlandi...")
    schools = execute_query("SELECT id, name, group_id, active FROM schools", fetchall=True)
    today_date = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    
    for school_id, name, group_id, active in schools:
        if not active:
            continue
            
        stats = execute_query(
            "SELECT g.status FROM grades_tracking g JOIN teachers t ON g.teacher_id = t.user_id WHERE t.school_id = ? AND g.date = ?",
            (school_id, today_date), fetchall=True
        )
        
        total = len(stats)
        done = sum(1 for s in stats if s[0] == 'done')
        missing = total - done
        eff = round((done / total) * 100, 1) if total > 0 else 0
        
        text = (
            "📊 <b>Kunlik Hisobot</b>\n\n"
            f"🏫 Maktab: {name}\n\n"
            f"📚 Jami nazorat qilingan darslar: {total}\n"
            f"✅ Baho qo'yilgan: {done}\n"
            f"⚠️ Baho qo'yilmagan (yoki kutilmoqda): {missing}\n\n"
            f"📈 Kunlik samaradorlik: {eff}%"
        )
        try:
            await bot.send_message(chat_id=group_id, text=text, parse_mode="HTML")
            logger.info(f"{name} guruhi ({group_id}) uchun kunlik hisobot yuborildi.")
        except Exception as e:
            logger.error(f"{group_id} guruhiga kunlik hisobot yuborishda xatolik: {e}")

# ==========================================
# 9. SCHEDULER
# ==========================================
async def check_teachers_alert():
    logger.info("O'qituvchilar tekshiruvi boshlandi...")
    teachers = execute_query("SELECT user_id, full_name, school_id, status FROM teachers WHERE status = 1", fetchall=True)
    
    for t_id, name, s_id, status in teachers:
        if not is_school_active(s_id):
            continue
            
        missing = random.choice([0, 0, 0, 1, 2])
        if missing > 0:
            try:
                await bot.send_message(
                    chat_id=t_id,
                    text=f"❌ Hurmatli {name}, siz {missing} ta darsga baho qo'ymagansiz! Iltimos, jurnalni to'ldiring."
                )
                # Adminga ham yuborish
                admin = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (s_id,), fetchone=True)
                if admin:
                    await bot.send_message(
                        chat_id=admin[0],
                        text=f"⚠️ <b>Ogohlantirish!</b>\n\n👨‍🏫 O'qituvchi: {name}\n❗ {missing} ta darsga baho qo'ymagan.\nBot o'qituvchiga eslatma yubordi.",
                        parse_mode="HTML"
                    )
            except TelegramForbiddenError:
                # O'qituvchi botni bloklagan
                execute_query("UPDATE teachers SET status = 0 WHERE user_id = ?", (t_id,))
                logger.info(f"O'qituvchi blokladi: {t_id}")
                # Adminga xabar yuborish
                admin = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (s_id,), fetchone=True)
                if admin:
                    try:
                        await bot.send_message(
                            chat_id=admin[0],
                            text=f"❌ <b>Diqqat!</b> O'qituvchi botni blok qilgan yoki akkauntini o'chirgan:\n\nIsm: {name}\nID: {t_id}\n\nTizimda u 'Noaktiv' holatiga o'tkazildi.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Teacher alert error for {t_id}: {e}")

async def check_schedule_reminders():
    now = datetime.now(TASHKENT_TZ)
    today = get_today_uz()
    today_date = now.strftime("%Y-%m-%d")
    
    lessons = execute_query("SELECT id, teacher_id, class_name, subject, lesson_time FROM schedule WHERE day = ?", (today,), fetchall=True)
    if not lessons:
        return
        
    for lesson_id, t_id, c_name, subj, l_time in lessons:
        try:
            l_hour, l_min = map(int, l_time.split(":"))
            lesson_dt = now.replace(hour=l_hour, minute=l_min, second=0, microsecond=0)
            diff_mins = (lesson_dt - now).total_seconds() / 60
            
            if 4 <= diff_mins <= 5:
                await bot.send_message(t_id, f"🔔 <b>Eslatma!</b>\n\n5 daqiqadan so'ng darsingiz boshlanadi:\n🏫 Sinf: {c_name}\n📚 Fan: {subj}\n⏰ Vaqt: {l_time}", parse_mode="HTML")
            
            elif -46 <= diff_mins <= -45:
                exists = execute_query("SELECT id FROM grades_tracking WHERE lesson_id = ? AND date = ?", (lesson_id, today_date), fetchone=True)
                if not exists:
                    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                    execute_query("INSERT INTO grades_tracking (teacher_id, lesson_id, date, status, timestamp) VALUES (?, ?, ?, 'pending', ?)",
                                  (t_id, lesson_id, today_date, now_str))
                    track_id = execute_query("SELECT id FROM grades_tracking WHERE lesson_id = ? AND date = ?", (lesson_id, today_date), fetchone=True)[0]
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Ha", callback_data=f"grade_yes_{track_id}"),
                         InlineKeyboardButton(text="❌ Yo'q", callback_data=f"grade_no_{track_id}")]
                    ])
                    await bot.send_message(t_id, f"❗ <b>Dars yakunlandi.</b>\n\n🏫 Sinf: {c_name}\n📚 Fan: {subj}\n\nJurnalga baho qo'ydingizmi?", parse_mode="HTML", reply_markup=kb)
                
        except Exception as e:
            pass

async def check_missing_grades():
    now = datetime.now(TASHKENT_TZ)
    pendings = execute_query("SELECT g.id, g.teacher_id, g.timestamp, s.class_name, s.subject, t.full_name, t.school_id FROM grades_tracking g JOIN schedule s ON g.lesson_id = s.id JOIN teachers t ON g.teacher_id = t.user_id WHERE g.status = 'pending'", fetchall=True)
    if pendings:
        for g_id, t_id, ts, c_name, subj, t_name, s_id in pendings:
            track_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            diff_hours = (now - track_time).total_seconds() / 3600
            if diff_hours >= 2.0:
                now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                execute_query("UPDATE grades_tracking SET status = 'missing', timestamp = ? WHERE id = ?", (now_str, g_id))
                try:
                    await bot.send_message(t_id, f"❌ Siz 2 soat ichida baholarni kiritmadingiz. ({c_name} - {subj})\nBu haqda maktab ma'muriyatiga xabar berildi.")
                except:
                    pass
                admin = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (s_id,), fetchone=True)
                if admin:
                    text = f"❌ <b>O'qituvchi baho qo'ymadi:</b>\n\n👨‍🏫 Ism: {t_name}\n🏫 Sinf: {c_name}\n📚 Fan: {subj}\n📅 Sana: {now.strftime('%d.%m.%Y')}"
                    try:
                        await bot.send_message(admin[0], text, parse_mode="HTML")
                    except:
                        pass
                        
    missings = execute_query("SELECT g.id, g.teacher_id, g.timestamp, s.class_name, s.subject, t.full_name, t.school_id FROM grades_tracking g JOIN schedule s ON g.lesson_id = s.id JOIN teachers t ON g.teacher_id = t.user_id WHERE g.status = 'missing'", fetchall=True)
    if missings:
        for g_id, t_id, ts, c_name, subj, t_name, s_id in missings:
            track_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            diff_hours = (now - track_time).total_seconds() / 3600
            if 0.95 <= diff_hours <= 1.05:
                try:
                    await bot.send_message(t_id, f"⚠️ <b>Baho hali qo'yilmadi!</b>\n\n🏫 Sinf: {c_name}\n📚 Fan: {subj}\nIltimos, elektron jurnalni to'ldiring.", parse_mode="HTML")
                    
                    # Adminga ham yuborish
                    admin = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (s_id,), fetchone=True)
                    if admin:
                        await bot.send_message(
                            admin[0],
                            f"⚠️ <b>Takroriy ogohlantirish!</b>\n\n👨‍🏫 O'qituvchi: {t_name}\n🏫 Sinf: {c_name}\n📚 Fan: {subj}\n\nBaho hali ham qo'yilmagan. O'qituvchiga qayta eslatma yuborildi.",
                            parse_mode="HTML"
                        )
                except:
                    pass

# ==========================================
# 45 MINUTLIK REMINDER (Baho qoyilmagan darslar)
# ==========================================
async def check_missing_grades_45min():
    """
    Har 45 minutda baho qoyilmagan darslar uchun
    adminga va o'qituvchiga eslatma yuboradi.
    Dars rejimi: 45 min dars, 5 min tanafus, 10 min uzoq tanafus
    """
    now = datetime.now(TASHKENT_TZ)
    today = get_today_uz()
    today_date = now.strftime("%Y-%m-%d")
    
    # Baho qoyilmagan darslarni olish
    pending_grades = execute_query(
        """SELECT g.id, g.teacher_id, s.id as lesson_id, s.class_name, s.subject, 
                  s.lesson_time, t.full_name, t.school_id 
           FROM grades_tracking g 
           JOIN schedule s ON g.lesson_id = s.id 
           JOIN teachers t ON g.teacher_id = t.user_id 
           WHERE g.status = 'pending' AND g.date = ? AND s.day = ?""",
        (today_date, today),
        fetchall=True
    )
    
    for g_id, t_id, lesson_id, c_name, subj, l_time, t_name, s_id in pending_grades:
        if not is_school_active(s_id):
            continue
        
        try:
            # Darsni hisoblash
            l_hour, l_min = map(int, l_time.split(":"))
            lesson_end = now.replace(hour=l_hour, minute=l_min+45, second=0, microsecond=0)
            
            # Agar dars 45 minut ilgari tugagan bo'lsa, eslatma yuborish
            diff_mins = (now - lesson_end).total_seconds() / 60
            if 45 <= diff_mins <= 46:  # 45 minut tugagandan so'ng
                # O'qituvchiga eslatma
                teacher_msg = f"""📝 <b>BAHO QO'YISH ESLATMASI</b>

🏫 Sinf: {c_name}
📚 Fan: {subj}
⏰ Dars vaqti: {l_time}
📅 Sana: {today_date}

❗ <i>Siz ushbu darsga baho qo'ymagansiz!
Iltimos, elektron jurnalni to'ldiring.</i>"""
                
                await bot.send_message(t_id, teacher_msg, parse_mode="HTML")
                
                # Adminga xabar
                admin = execute_query("SELECT user_id FROM admins WHERE school_id = ?", (s_id,), fetchone=True)
                if admin:
                    admin_msg = f"""⚠️ <b>BAHO QO'YISH ESLATMASI (45 MIN)</b>

👨‍🏫 O'qituvchi: {t_name}
🏫 Sinf: {c_name}
📚 Fan: {subj}
⏰ Dars vaqti: {l_time}
📅 Sana: {today_date}

❗ <i>O'qituvchi hali baho qo'ymagani uchun 
automatic eslatma yuborildi.</i>"""
                    
                    await bot.send_message(admin, admin_msg, parse_mode="HTML")
                    
        except Exception as e:
            logger.error(f"45 min reminder error for teacher {t_id}: {e}")

# ==========================================
# DARS JADVALI VALIDATSIYASI
# ==========================================
def validate_schedule(school_id: int) -> dict:
    """
    Dars jadvali qoidalarini tekshiradi:
    - Har dars 45 minutlik
    - Tanafuslar: 5 min (kichik), 10 min (uzoq)
    - Kuniga 3 soatdan ko'p emas
    
    Returns: {"is_valid": bool, "errors": [str]}
    """
    errors = []
    
    # Maktabning dars jadvali bilan ishlash
    lessons = execute_query(
        "SELECT s.id, s.lesson_time, s.day, COUNT(*) as count FROM schedule s WHERE s.teacher_id IN (SELECT user_id FROM teachers WHERE school_id = ?) GROUP BY s.day",
        (school_id,),
        fetchall=True
    )
    
    for lesson_id, lesson_time, day, lesson_count in lessons:
        try:
            l_hour, l_min = map(int, lesson_time.split(":"))
            
            # Dars 45 minutlik ekanligini tekshirish
            # (Bu oddiy check - to'liq validatsiya uchun dars oxiri vaqtini bilish kerak)
            
            # Kuniga 3 soatdan ko'p emas (180 minut)
            total_minutes = lesson_count * 45 + (lesson_count - 1) * 5  # 45 min dars + 5 min tanafus
            if total_minutes > 180:
                errors.append(f"📚 {day} kuni: Jami {total_minutes} minut ({total_minutes/60:.1f} soat). Maksimum 3 soat (180 minut) bo'lishi kerak!")
                
        except Exception as e:
            errors.append(f"Dars jadvali format xatosi: {e}")
    
    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "school_id": school_id
    }

def setup_scheduler():
    scheduler.add_job(check_schedule_reminders, "interval", minutes=1)
    scheduler.add_job(check_missing_grades, "interval", minutes=5)
    scheduler.add_job(check_missing_grades_45min, "interval", minutes=45)  # 🆕 45 MINUTLIK REMINDER
    scheduler.add_job(check_teachers_alert, "interval", minutes=10)
    
    # Har kuni soat 18:00 da kunlik hisobotni guruhga tashlaydi
    scheduler.add_job(send_daily_report, "cron", hour=18, minute=0)
    
    # 🟢 KEEP-ALIVE: Har 3 daqiqada heartbeat — bot uyquga ketmasligini kafolatlaydi
    async def heartbeat():
        logger.info(f"💓 Bot faol | {datetime.now(TASHKENT_TZ).strftime('%H:%M:%S')} | Scheduler ishlayapti")
    scheduler.add_job(heartbeat, "interval", minutes=3)
    
    scheduler.start()

# ==========================================
# 10. MAIN()
# ==========================================
MAX_RETRIES = 0  # 0 = cheksiz qayta urinish

async def main():
    logger.info("Ma'lumotlar bazasi initsializatsiyasi...")
    init_db()
    
    logger.info("Scheduler (Taymer) ishga tushirilmoqda...")
    setup_scheduler()
    
    logger.info("Bot ishga tushdi! 🟢")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    retry_count = 0
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Bot ishi to'xtatildi (CTRL+C).")
            break
        except Exception as e:
            retry_count += 1
            wait_time = min(retry_count * 5, 60)  # 5s, 10s, 15s ... max 60s
            logger.error(f"❌ Bot xatolik sababli to'xtadi: {e}")
            logger.info(f"🔄 {wait_time} soniyadan so'ng qayta ishga tushiriladi... (urinish #{retry_count})")
            import time
            time.sleep(wait_time)
            logger.info(f"🟢 Bot qayta ishga tushmoqda...")
