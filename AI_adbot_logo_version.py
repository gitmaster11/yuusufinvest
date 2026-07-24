"""
AI_adbot.py — Mahsulot reklama Telegram boti
============================================================
Kerakli kutubxonalar (o'rnatish):
    pip install aiogram==3.13.1 pandas openpyxl playwright aiohttp
    playwright install chromium
    playwright install-deps chromium   # Linux serverda kerak bo'lishi mumkin

Ishga tushirish:
    python AI_adbot.py
"""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
import urllib.parse
import json
import asyncio
import base64
import io
import logging
import os
import re
import sqlite3
from pathlib import Path
import aiohttp
import pandas as pd
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove


class OrderForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_age = State()
    waiting_for_phone = State()

class RegistrationStates(StatesGroup):
    waiting_for_job_status = State()

class AdminStates(StatesGroup):
    waiting_for_rate = State()
    waiting_for_ad = State()  





NO_IMAGE_SVG = '<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:24px;font-family:sans-serif;">Rasm mavjud emas</div>'

from collections import OrderedDict

# ── LRU Kesh sinfi ────────────────────────────────────────────────────────────
class LRUCache:
    """
    Cheklangan hajmli LRU kesh.
    Eng kam ishlatilgan element avtomatik o'chiriladi.
    """
    def __init__(self, maxsize: int):
        self._cache: OrderedDict = OrderedDict()
        self._maxsize = maxsize

    def get(self, key):
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)   # eng so'nggi ishlatilgan
        return self._cache[key]

    def set(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)  # eng eski o'chiriladi

    def clear(self):
        self._cache.clear()

    def __contains__(self, key):
        return key in self._cache

    def __len__(self):
        return len(self._cache)


# Kesh o'lchamlari:
#   _image_cache: 200 rasm × ~300KB = ~60MB RAM (xavfsiz)
#   _card_cache:  200 karta × ~200KB = ~40MB RAM (xavfsiz)
_image_cache: LRUCache = LRUCache(maxsize=200)
_card_cache:  LRUCache = LRUCache(maxsize=200)
_http_session: aiohttp.ClientSession | None = None
_shared_page = None  # <--- Buni qo'shing

# ═══════════════════════════════════════════════════════════════════════════════
#  SOZLAMALAR
# ═══════════════════════════════════════════════════════════════════════════════

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS  = [1306354017]   # Admin(lar)ning Telegram ID si

DB_PATH  = "adbot.db"
CARD_W   = 900   # Karta kengligi (HTML/CSS shablon shu kenglikka mo'ljallangan)
LOGO_PATH = "logo.png"

# ═══════════════════════════════════════════════════════════════════════════════
#  MA'LUMOTLAR BAZASI
# ═══════════════════════════════════════════════════════════════════════════════

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            
            -- Eski jadvalni o'chirib, yangisini yaratish kerak (chunki struktura o'zgardi)
            -- Agar bazada muhim ma'lumotlar bo'lsa, avval ularni saqlab olish kerak.
            
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT    NOT NULL,
                brand       TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                image       TEXT    DEFAULT '',
                base_price  INTEGER NOT NULL,
                tavsif      TEXT    NOT NULL
               
            );
            
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            INSERT OR IGNORE INTO settings VALUES ('usd_rate', '12800'); -- Boshlang'ich kurs
                           
            
            -- Qidiruv tezligi uchun indekslar
            CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);
            CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
        """)


def clear_media_cache():
    _image_cache.clear()
    _card_cache.clear()
    logging.info("Kesh tozalandi.")


def db_clear_products():
    with db_connect() as conn:
        conn.execute("DELETE FROM products")
    clear_media_cache()


def db_save_products(rows: list[dict]):
    with db_connect() as conn:
        conn.executemany("""
            INSERT INTO products (category, brand, name, image, base_price, tavsif)
            VALUES (:category, :brand, :name, :image, :base_price, :tavsif)
        """, rows)


def db_get_categories() -> list[str]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM products ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]


def db_get_rate() -> float:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'usd_rate'").fetchone()
        return float(row[0]) if row else 12800.0

def db_set_rate(rate: float):
    with db_connect() as conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'usd_rate'", (str(rate),))

def db_get_by_category(category: str) -> list[dict]:
    with db_connect() as conn:
        rows = conn.execute( 
            "SELECT * FROM products WHERE category = ? ORDER BY id",
            (category,)
        ).fetchall()
    return [dict(r) for r in rows]


def db_save_user(user_id: int, username: str, full_name: str):
    with db_connect() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
        """, (user_id, username or "", full_name or ""))


def db_count_users() -> int:
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def db_count_products() -> int:
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

# ═══════════════════════════════════════════════════════════════════════════════
#  EXCEL O'QISH
# ═══════════════════════════════════════════════════════════════════════════════

def parse_excel(path: str) -> tuple[list[dict], str | None]:
    try:
        df = pd.read_excel(path).fillna("")
        products = []
        for _, row in df.iterrows():
            products.append({
                "category": str(row["Mahsulot turi"]).strip(),
                "name":     str(row["Mahsulot nomi"]).strip(),
                "image":    str(row["Rasmi"]).strip(),
                "base_price": int(row["Tannarx"]),
                "brand":    str(row["Brendi"]).strip(),
                "tavsif":   str(row["Tavsif"]).strip()
            })
        return products, None
    except Exception as e:
        return [], f"Excel o'qishda xatolik: {e}"
  

# ═══════════════════════════════════════════════════════════════════════════════
#  FORMATLASH YORDAMCHILARI
# ═══════════════════════════════════════════════════════════════════════════════

def format_number(value: str) -> str:
    """
    Raqamlarni minglik ajratgich bilan formatlaydi: 12000000 -> 12 000 000
    Qisqa raqamlar (3 xonagacha, masalan oy soni) o'zgarishsiz qoladi.
    """
    if not value:
        return value

    def _format_match(m: re.Match) -> str:
        digits = m.group(0)
        if len(digits) <= 3:
            return digits
        return f"{int(digits):,}".replace(",", " ")

    return re.sub(r"\d+", _format_match, value)


def format_duration(value: str) -> str:
    """
    Muddat qiymatiga avtomatik 'oy' so'zini qo'shadi.
    '12' -> '12 oy', '24 oy' -> '24 oy' (o'zgarishsiz), '' -> ''
    """
    if not value:
        return value

    value = value.strip()

    if re.search(r"[a-zA-Zа-яА-ЯёЁ\u0400-\u04FF]", value):
        return value

    if re.fullmatch(r"\d+", value):
        return f"{value} oy"

    return value


def _esc(text: str) -> str:
    """HTML maxsus belgilarini xavfsizlashtiradi."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  RASM YARATISH (HTML/CSS + Playwright)
# ═══════════════════════════════════════════════════════════════════════════════
 
CARD_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'DejaVu Sans', Arial, sans-serif; }
    body { width: 900px; background: #083234; }
    .card { width: 900px; padding: 48px; color: #f1f5f9; }
    
    /* Logotip tepada alohida */
    .header-logo-box { text-align: center; margin-bottom: 30px; }
    .company-logo { height: 70px; object-fit: contain; }
 
    /* Rasm qutisi */
    .image-box {
        width: 100%; height: 420px;
        background: #ffffff;
        border-radius: 24px;
        display: flex; align-items: center; justify-content: center;
        overflow: hidden;
        box-shadow: 0 20px 40px rgba(0,0,0,0.4);
    }
    .product-image {
        max-width: 95%; max-height: 380px;
        object-fit: contain;
    }
   
    .divider { height: 3px; background: #38bdf8; margin: 40px 0 30px 0; border-radius: 2px; }
    .category { color: #38bdf8; font-size: 38px; font-weight: 700; text-transform: uppercase; margin-bottom: 18px; }
    .product-name { font-size: 45px; font-weight: 500; line-height: 1.1; color: #f1f5f9; margin-bottom: 30px; }
    .divider-thin { height: 1px; background: rgba(148,163,184,0.3); margin-bottom: 32px; }
    
   
    .dot { width: 20px; height: 20px; border-radius: 50%; display: inline-block; margin-right: 14px; }
    .info-value { font-size: 60px; font-weight: 700; margin-left: 34px; margin-top: 10px; }
    
    .price .dot { background: #22c55e; } .price .info-value { color: #22c55e; }
    .footer { text-align: center; color: rgba(148,163,184,0.55); font-size: 24px; margin-top: 28px; }
</style>
</head>
<body>
    <div class="card">
        <div class="header-logo-box">{{LOGO_CONTENT}}</div>
        <div class="image-box">{{IMAGE_CONTENT}}</div>
        <div class="divider"></div>
        <div class="category">{{CATEGORY}}</div>
        <div class="product-name">{{NAME}} {{TAVSIF}}</div>
         <div class="divider"></div>
        
        
       <div class="footer" style="text-align: center;">Yuusuf Invest | Qadriyatingizga mos</div>

    </div>
</body>
</html>
"""
 
# Playwright brauzerini butun bot hayoti davomida bitta marta ochib, qayta
# ishlatamiz — har bir karta uchun yangi brauzer ochish juda sekin bo'lardi.
_playwright_ctx = None
_browser = None




async def get_browser():
    global _playwright_ctx, _browser
    if _browser is None:
        _playwright_ctx = await async_playwright().start()
        _browser = await _playwright_ctx.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
    return _browser


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _http_session


async def close_http_session():
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def close_browser():
    global _playwright_ctx, _browser, _shared_page
    if _shared_page:
        await _shared_page.close()
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright_ctx is not None:
        await _playwright_ctx.stop()
        _playwright_ctx = None
    await close_http_session()

# Logo bir marta o'qilib, xotiraga saqlanadi (har karta uchun diskdan qayta o'qilmaydi)
_logo_b64_cache: str | None = None

def _get_logo_html() -> str:
    global _logo_b64_cache
    if _logo_b64_cache is None:
        if os.path.exists(LOGO_PATH):
            with open(LOGO_PATH, "rb") as f:
                _logo_b64_cache = base64.b64encode(f.read()).decode()
        else:
            _logo_b64_cache = ""
    if _logo_b64_cache:
        return f'<img src="data:image/png;base64,{_logo_b64_cache}" class="company-logo">'
    return ""


def _build_card_html(product: dict, product_img_bytes: bytes | None) -> str:
    current_rate = db_get_rate()
    price_in_uzs = int(product["base_price"] * current_rate)
    formatted_price = f"{price_in_uzs:,}".replace(",", " ") + " so'm"
    logo_html = _get_logo_html()
    if product_img_bytes:
        b64 = base64.b64encode(product_img_bytes).decode()
        image_content = f'<img src="data:image/png;base64,{b64}" class="product-image">'
    else:
        image_content = NO_IMAGE_SVG

    html = CARD_HTML_TEMPLATE
    html = html.replace("{{LOGO_CONTENT}}", logo_html)
    html = html.replace("{{IMAGE_CONTENT}}", image_content)
    html = html.replace("{{CATEGORY}}", _esc(product["category"].upper()))
    html = html.replace("{{NAME}}", _esc(product["name"]))
    html = html.replace("{{TAVSIF}}", _esc(product["tavsif"]))
    # Eski o'zgaruvchilarni bo'sh string bilan to'ldiramiz (shablon buzilmasligi uchun)
    return html

async def build_card(product: dict, product_img_bytes: bytes | None) -> bytes:
    global _shared_page

    # Keshni tekshirish
    cache_key = product.get("id") or hash(tuple(sorted(product.items())))
    cached = _card_cache.get(cache_key)
    if cached is not None:
        return cached

    # HTML yaratish
    html = _build_card_html(product, product_img_bytes)
    browser = await get_browser()

    # Yagona sahifadan foydalanish
    if _shared_page is None or _shared_page.is_closed():
        _shared_page = await browser.new_page()

    try:
        await _shared_page.set_content(html, wait_until="networkidle")
        height = await _shared_page.evaluate("document.querySelector('.card').offsetHeight")
        await _shared_page.set_viewport_size({"width": CARD_W, "height": height})
        png_bytes = await _shared_page.screenshot(type="png")
    except Exception as e:
        logging.error(f"Render xatosi: {e}")
        _shared_page = None
        raise

    _card_cache.set(cache_key, png_bytes)
    return png_bytes
# ═══════════════════════════════════════════════════════════════════════════════
#  RASM YUKLASH (URL yoki Telegram file_id)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_image(bot: Bot, image_ref: str) -> bytes | None:
    """URL yoki Telegram file_id dan rasm yuklab oladi."""
    if not image_ref:
        return None

    cached = _image_cache.get(image_ref)
    if cached is not None:
        return cached

    try:
        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            session = await get_http_session()
            async with session.get(image_ref, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    _image_cache.set(image_ref, data)
                    return data
            return None
        else:
            file = await bot.get_file(image_ref)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, destination=buf)
            data = buf.getvalue()
            _image_cache.set(image_ref, data)
            return data
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
#  KLAVIATURALAR
# ═══════════════════════════════════════════════════════════════════════════════
def brands_keyboard(brands: list[str]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=f"📱 {b}", callback_data=f"brd:{b}")] for b in brands]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def categories_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"📦 {cat}", callback_data=f"cat:{cat}")]
        for cat in categories
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ═══════════════════════════════════════════════════════════════════════════════
#  BOT VA DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

bot_instance = Bot(token=BOT_TOKEN)
dp           = Dispatcher(storage=MemoryStorage())

# ── /start ────────────────────────────────────────────────────────────────────



@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    # 1. Admin ekanligini tekshirish
    db_save_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or ""
    )
    if message.from_user.id in ADMIN_IDS:
        await message.answer("Xush kelibsiz, Admin! \n\n/stats - Statistika\n\nFayl yuboring (Excel) bazani yangilash uchun.")
        await state.clear()
        return

    # 2. Oddiy foydalanuvchi bo'lsa, avvalgi holatga (savolga) o'tadi
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ HA"), KeyboardButton(text="❌ YO‘Q")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        is_persistent=True
    )
    
    await message.answer(f"*Rasmiy ish joyiga egamisiz?*", reply_markup=kb,parse_mode="Markdown")
    await state.set_state(RegistrationStates.waiting_for_job_status)
# ── /catalog ─────────────────────────────────────────────────────────────────




# Yangi holat (faqat 'start' jarayoni tugaganlarga ruxsat beradi):
def db_get_categories():
    with db_connect() as conn:
        cursor = conn.execute("SELECT DISTINCT category FROM products")
        # Tuples ro'yxatini tekis ro'yxatga o'tkazish
        return [row[0] for row in cursor.fetchall() if row[0]]


async def cmd_catalog(message: types.Message, state: FSMContext = None):
    # Agar state bo'lsa, uni tozalash
    if state:
        await state.clear()
        
    categories = db_get_categories()
    
    # DEBUG: Kategoriyalar borligini tekshiramiz
 
    
    if not categories:
        await message.answer(f"*Hozircha katalogda mahsulotlar mavjud emas*", parse_mode="Markdown")
        return
    
    # Kategoriyalar uchun tugmalar yaratish
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.row(InlineKeyboardButton(text=cat, callback_data=f"cat:{cat}"))
    
    await message.answer("Kategoriyalardan birini tanlang:", 
                        reply_markup=builder.as_markup())
  
@dp.message(RegistrationStates.waiting_for_job_status, F.text.in_({"✅ HA", "❌ YO‘Q"}))
async def process_job_status(message: types.Message, state: FSMContext):
    if message.text == "❌ YO‘Q":
        await message.answer(
            f"*Biz faqat rasmiy ish joyiga ega mijozlar bilan birga ishlaymiz. Tushunganingiz uchun rahmat.*", 
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return
    
    if message.text == "✅ HA":
        await message.answer("Ajoyib! Katalogimizga xush kelibsiz.", reply_markup=ReplyKeyboardRemove())
        await state.clear() 
        
        # TO'G'RI CHAQIRISH:
        # Funksiyaga message BILAN BIRGA state ni ham uzatamiz
        await cmd_catalog(message, state)


# ── /stats (faqat admin) ──────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        f"📊 *Statistika*\n\n"
        f"👥 Foydalanuvchilar: {db_count_users()}\n"
        f"📦 Mahsulotlar: {db_count_products()}\n"
        f"🗂 Kategoriyalar: {len(db_get_categories())}\n\n"
        f"🧠 *Kesh holati:*\n"
        f"  🖼 Rasmlar keshlangan: {len(_image_cache)}/200\n"
        f"  🃏 Kartalar keshlangan: {len(_card_cache)}/100",
        parse_mode="Markdown"
    )

@dp.message(Command("currency"))
async def cmd_currency(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("💰 Hozirgi kurs: " + str(db_get_rate()) + "\nYangi kursni kiriting:")
    await state.set_state(AdminStates.waiting_for_rate)

@dp.message(AdminStates.waiting_for_rate)
async def process_rate(message: types.Message, state: FSMContext):
    await state.clear()
    try:
        new_rate = float(message.text)
        db_set_rate(new_rate)
        await message.answer(f"✅ Kurs yangilandi: {new_rate}")
        await state.clear()
        clear_media_cache()  # Kurs o'zgarganda keshni tozalash foydali bo'lishi mumkin
    except:
        await message.answer("❌ Xatolik! Iltimos, faqat raqam kiriting (masalan: 12850)")


@dp.message(Command("reklama"))
async def cmd_reklama(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await state.clear()
    await message.answer("📢 Reklama matnini, rasmini, video'ni yoki boshqa kontentni yuboring:")
    await state.set_state(AdminStates.waiting_for_ad)

@dp.message(AdminStates.waiting_for_ad)
async def process_ad(message: types.Message, state: FSMContext):
    await state.clear()
    
    # ⭐ QO'SHISH: Format tekshirish
    if not (message.text or message.photo or message.video or 
            message.audio or message.voice or message.location):
        await message.answer(
            "❌ Noto'g'ri format!\n\n"
            "Quyidagilarni yuborishingiz mumkin:\n"
            "• 📝 Matn\n"
            "• 🖼 Rasm (caption bilan)\n"
            "• 🎥 Video (caption bilan)\n"
            "• 🎵 Audio\n"
            "• 🎙 Ovozli xabar\n"
            "• 📍 Lokatsiya"
        )
        return
    
    with db_connect() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()
    
    if not users:
        await message.answer("❌ Bazada foydalanuvchi yo'q")
        return
    
    wait_msg = await message.answer(f"📤 Reklama {len(users)} ta mijozga yuborilmoqda...")
    
    success = 0
    failed = 0
    
    for user_row in users:
        user_id = user_row[0]
        try:
            if message.photo:
                await bot_instance.send_photo(
                    chat_id=user_id,
                    photo=message.photo[-1].file_id,
                    caption=message.caption or "",
                    parse_mode="Markdown"
                )
            elif message.video:
                await bot_instance.send_video(
                    chat_id=user_id,
                    video=message.video.file_id,
                    caption=message.caption or "",
                    parse_mode="Markdown"
                )
            elif message.audio:
                await bot_instance.send_audio(
                    chat_id=user_id,
                    audio=message.audio.file_id,
                    caption=message.caption or "",
                    parse_mode="Markdown"
                )
            elif message.voice:
                await bot_instance.send_voice(
                    chat_id=user_id,
                    voice=message.voice.file_id,
                    caption=message.caption or "",
                    parse_mode="Markdown"
                )
            elif message.location:
                await bot_instance.send_location(
                    chat_id=user_id,
                    latitude=message.location.latitude,
                    longitude=message.location.longitude
                )
            elif message.text:
                await bot_instance.send_message(
                    chat_id=user_id,
                    text=message.text,
                    parse_mode="Markdown"
                )
            
            success += 1
        except Exception as e:
            logging.warning(f"Mijoz {user_id}: {e}")
            failed += 1
    
    await wait_msg.edit_text(
        f"✅ Reklama yuborish tugadi!\n\n"
        f"📤 Muvaffaqiyatli: {success}\n"
        f"❌ Xatolik: {failed}"
    )
# ── Kategoriya tugmasi bosilganda ─────────────────────────────────────────────




@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(call: types.CallbackQuery):
    category = call.data[4:]
    # Tanlangan kategoriyadagi brendlarni bazadan olamiz
    with db_connect() as conn:
        brands = [r["brand"] for r in conn.execute(
            "SELECT DISTINCT brand FROM products WHERE category = ?", (category,)
        ).fetchall()]
    await call.answer()
    await call.message.answer(f"📦 *{category}* uchun brendni tanlang:", 
                              reply_markup=brands_keyboard(brands), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("brd:"))

async def cb_brand(call: types.CallbackQuery):
    current_rate = db_get_rate()
    brand = call.data[4:]
    with db_connect() as conn:
        products = [dict(r) for r in conn.execute("SELECT * FROM products WHERE brand = ?", (brand,)).fetchall()]

    for p in products:
        price_uzs = int(p['base_price'] * current_rate)
        formatted_price = f"{price_uzs:,}".replace(",", " ")
        
        # Tavsifni ham qo'shamiz (bazangizda bo'lsa)
        desc = p.get('tavsif', '')
        text = (f"📦 *{p['name']}* *{desc}*\n\n"
                f"💰 *Tannarxi: {formatted_price} so'm*\n\n"
                f"(Mahsulot rangi uning nomi bilan birga ko'rsatilgan.)")
        
        # FAQAT "Rasm ko'rish" tugmasi
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖼 Rasmni ko‘rish va ariza yuborish", callback_data=f"show:{p['id']}")]
        ])
        await call.message.answer(text, reply_markup=kb, parse_mode="Markdown")
# Yangi callback: Rasm so'ralganda generatsiya qilish

@dp.callback_query(F.data.startswith("show:"))
async def cb_show_image(call: types.CallbackQuery):
    product_id = call.data.split(":")[1]
    wait_msg = await call.message.answer("⏳ Rasm yuklanmoqda...")
    
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not row:
            await wait_msg.edit_text("❌ Mahsulot topilmadi.")
            return
        product = dict(row)
        
    try:
        # Narxni hisoblash (bu yerda ham kerak)
        current_rate = db_get_rate()
        price_uzs = int(product['base_price'] * current_rate)
        
        # 1. Rasm yuklash va yaratish
        img_bytes = await fetch_image(bot_instance, product.get("image", ""))
        card_bytes = await build_card(product, img_bytes)
        
        # 2. "Ariza yuborish" tugmasini rasm ostiga qo'shish
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🛒 Ariza yuborish", 
                web_app=WebAppInfo(url=f"https://gitmaster11.github.io/web_sahifa/?product={urllib.parse.quote(product['name'])}&price={price_uzs}&user_id={call.from_user.id}")
            )]
        ])
        
        # 3. Rasm va tugmani yuborish
        input_file = BufferedInputFile(card_bytes, filename="product.png")
        await call.message.answer_photo(photo=input_file, reply_markup=kb)
        await wait_msg.delete()
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ Rasm yaratishda xatolik yuz berdi: {e}")


# Bazadan ID bo'yicha mahsulotni topish uchun yordamchi funksiya
def db_get_product_by_id(product_id: int) -> dict | None:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None
    


# ── Admin: Excel fayl qabul qilish ───────────────────────────────────────────

@dp.message(F.document)
async def handle_document(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    doc = message.document
    if not (doc.file_name.endswith(".xlsx") or doc.file_name.endswith(".xls")):
        await message.answer("❌ Faqat *.xlsx* yoki *.xls* fayl yuboring!", parse_mode="Markdown")
        return

    wait_msg = await message.answer("⏳ Fayl yuklanmoqda...")

    # Faylni saqlash
    os.makedirs("uploads", exist_ok=True)
    save_path = f"uploads/{doc.file_name}"

    file = await bot_instance.get_file(doc.file_id)
    await bot_instance.download_file(file.file_path, destination=save_path)

    # Excel parse
    products, error = parse_excel(save_path)

    if error:
        await wait_msg.edit_text(f"❌1 Xatolik: {error}")
        return

    # Bazani yangilash
    db_clear_products()
    db_save_products(products)

    categories = db_get_categories()
    cat_list   = "\n".join(f"  • {c}" for c in categories)

    await wait_msg.edit_text(
        f"✅ Muvaffaqiyatli yuklandi!\n\n"
        f"📦 Jami mahsulotlar: *{len(products)}* ta\n"
        f"🗂 Kategoriyalar ({len(categories)}):\n{cat_list}",
        parse_mode="Markdown"
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  ISHGA TUSHIRISH
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    db_init()
    logging.info("Brauzer (Playwright) ishga tushirilmoqda...")
    await get_browser()
    logging.info("Bot ishga tushdi ✅")
    
    try:
        # Botni ishga tushiramiz
        await dp.start_polling(bot_instance)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C bosilganda yuz beradigan xatolikni tutib qolamiz
        logging.info("Bot to'xtatilmoqda...")
    finally:
        # Brauzerni xavfsiz yopamiz
        try:
            await close_browser()
        except Exception:
            pass # Agar brauzer allaqachon yopilgan bo'lsa, xatolik chiqarmaydi
        logging.info("Bot muvaffaqiyatli to'xtatildi.")


if __name__ == "__main__":
    asyncio.run(main())