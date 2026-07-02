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

class OrderForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_age = State()
    waiting_for_phone = State()


import urllib.parse
import json
import asyncio
import base64
import io
import logging
import os
import sqlite3
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  SOZLAMALAR
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN  = "8827872980:AAHvvV97pfvG5zCCfC6Tw0ghOitW-avxa4Q"
ADMIN_IDS  = [1306354017,6301717496]   # Admin(lar)ning Telegram ID si

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
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                image       TEXT    DEFAULT '',
                price       TEXT    DEFAULT '',
                duration    TEXT    DEFAULT '',
                monthly     TEXT    DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)


def db_clear_products():
    with db_connect() as conn:
        conn.execute("DELETE FROM products")


def db_save_products(rows: list[dict]):
    with db_connect() as conn:
        conn.executemany("""
            INSERT INTO products (category, name, image, price, duration, monthly)
            VALUES (:category, :name, :image, :price, :duration, :monthly)
        """, rows)


def db_get_categories() -> list[str]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM products ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]


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
    """
    Ustunlar:
        A = # (tartib)   B = Mahsulot turi   C = Mahsulot nomi
        D = Rasmi        E = Narxi            F = Muddati   G = Oylik to'lov
    """
    try:
    
        df = pd.read_excel(path, header=0, dtype=str).fillna("")
        cols = df.shape[1]

        if cols < 3:
            return [], "Kamida 3 ta ustun bo'lishi kerak (tartib, tur, nom)."

        def col(row, i, default=""):
            return str(row.iloc[i]).strip() if i < cols else default
        products = []
        for _, row in df.iterrows():
            category = col(row, 1)
            name     = col(row, 2)
            if not category or not name:
                continue
            products.append({
                "category": category,
                "name":     name,
                "image":    col(row, 3),
                "price":    col(row, 4),
                "duration": col(row, 5),
                "monthly":  col(row, 6),
            })

        if not products:
            return [], "Faylda hech qanday mahsulot topilmadi."

        return products, None

    except Exception as e:
        return [], f"Faylni o'qishda xatolik: {e}"

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

    import re

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

    # Agar allaqachon harf mavjud bo'lsa (oy, yil, kun va h.k.) — tegmaymiz
    import re
    if re.search(r"[a-zA-Zа-яА-ЯёЁ\u0400-\u04FF]", value):
        return value

    # Faqat raqam bo'lsa — 'oy' qo'shamiz
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
    .product-name { font-size: 60px; font-weight: 700; line-height: 1.1; color: #f1f5f9; margin-bottom: 30px; }
    .divider-thin { height: 1px; background: rgba(148,163,184,0.3); margin-bottom: 32px; }
    
    .info-row { background: rgba(30,41,59,0.75); border-radius: 20px; padding: 24px 32px; margin-bottom: 22px; }
    .info-label { display: flex; align-items: center; gap: 14px; font-size: 30px; color: #94a3b8; }
    .dot { width: 20px; height: 20px; border-radius: 50%; display: inline-block; margin-right: 14px; }
    .info-value { font-size: 60px; font-weight: 700; margin-left: 34px; margin-top: 10px; }
    
    .price .dot { background: #22c55e; } .price .info-value { color: #22c55e; }
    .duration .dot { background: #ffffff; } .duration .info-value { color: #f1f5f9; }
    .monthly .dot { background: #38bdf8; } .monthly .info-value { color: #38bdf8; }
    .footer { text-align: center; color: rgba(148,163,184,0.55); font-size: 24px; margin-top: 28px; }
</style>
</head>
<body>
    <div class="card">
        <div class="header-logo-box">{{LOGO_CONTENT}}</div>
        <div class="image-box">{{IMAGE_CONTENT}}</div>
        <div class="divider"></div>
        <div class="category">{{CATEGORY}}</div>
        <div class="product-name">{{NAME}}</div>
        <div class="divider-thin"></div>
        <div class="info-row price">
            <div class="info-label"><span class="dot"></span>Narxi</div>
            <div class="info-value">{{PRICE}}</div>
        </div>
        <div class="info-row duration">
            <div class="info-label"><span class="dot"></span>Muddati</div>
            <div class="info-value">{{DURATION}}</div>
        </div>
        <div class="info-row monthly">
            <div class="info-label"><span class="dot"></span>Oylik to'lov</div>
            <div class="info-value">{{MONTHLY}}</div>
        </div>
        <div class="footer">Yuusuf Invest | Qadriyatingizga mos</div>
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
            "--disable-dev-shm-usage"])
    return _browser


async def close_browser():
    global _playwright_ctx, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright_ctx is not None:
        await _playwright_ctx.stop()
        _playwright_ctx = None


def _build_card_html(product: dict, product_img_bytes: bytes | None) -> str:
    # Logotipni alohida ajratib oldik
    logo_html = ""
    if os.path.exists(LOGO_PATH):
        with open(LOGO_PATH, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode()
            logo_html = f'<img src="data:image/png;base64,{logo_b64}" class="company-logo">'

    # Rasm qutisi faqat mahsulot rasmi uchun
    if product_img_bytes:
        b64 = base64.b64encode(product_img_bytes).decode()
        image_content = f'<img src="data:image/png;base64,{b64}" class="product-image">'
    else:
        image_content = NO_IMAGE_SVG

    html = CARD_HTML_TEMPLATE
    html = html.replace("{{LOGO_CONTENT}}", logo_html)
    html = html.replace("{{IMAGE_CONTENT}}", image_content)
    # ... qolgan qatorlar o'zgarishsiz ...
    html = html.replace("{{CATEGORY}}", _esc(product["category"].upper()))
    html = html.replace("{{NAME}}", _esc(product["name"]))
    html = html.replace("{{PRICE}}", _esc(format_number(product["price"]) or "—"))
    html = html.replace("{{DURATION}}", _esc(format_duration(product["duration"]) or "—"))
    html = html.replace("{{MONTHLY}}", _esc(format_number(product["monthly"]) or "—"))
    return html

async def build_card(product: dict, product_img_bytes: bytes | None) -> bytes:
    """
    Mahsulot kartasini HTML/CSS orqali yaratadi va PNG bytes qaytaradi.
    Brauzer renderingidan foydalanadi — bu Pillow'ga qaraganda ancha aniq
    va professional shrift natijasini beradi, hatto Telegram'ning preview
    kichraytirishida ham matn aniq qoladi.
    """
    html = _build_card_html(product, product_img_bytes)

    browser = await get_browser()
    page = await browser.new_page(viewport={"width": CARD_W, "height": 100})
    try:
        await page.set_content(html)
        height = await page.evaluate("document.querySelector('.card').offsetHeight")
        await page.set_viewport_size({"width": CARD_W, "height": height})
        png_bytes = await page.screenshot(type="png")
    finally:
        await page.close()

    return png_bytes

# ═══════════════════════════════════════════════════════════════════════════════
#  RASM YUKLASH (URL yoki Telegram file_id)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_image(bot: Bot, image_ref: str) -> bytes | None:
    """URL yoki Telegram file_id dan rasm yuklab oladi."""
    if not image_ref:
        return None
    try:
        import aiohttp
        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            async with aiohttp.ClientSession() as session:
                async with session.get(image_ref, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.read()
            return None
        else:
            # Telegram file_id
            file = await bot.get_file(image_ref)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, destination=buf)
            return buf.getvalue()
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
#  KLAVIATURALAR
# ═══════════════════════════════════════════════════════════════════════════════

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
async def cmd_start(message: types.Message):
    user = message.from_user
    db_save_user(user.id, user.username, user.full_name)

    # Admin bo'lsa — excel yuklash taklifi
    if user.id in ADMIN_IDS:
        await message.answer(
            f"👋 Salom, Admin!\n\n"
            f"📊 Hozir bazada *{db_count_products()}* ta mahsulot mavjud.\n\n"
            f"Yangi Excel fayl yuklaysizmi?\n"
            f"Yuklash uchun faylni shu yerga yuboring yoki /catalog buyrug'ini bosing.",
            parse_mode="Markdown"
        )
        return

    # Oddiy foydalanuvchi
    categories = db_get_categories()
    if not categories:
        await message.answer(
            "👋 Xush kelibsiz!\n\n"
            "⏳ Hozircha mahsulotlar mavjud emas. Tez orada qo'shiladi!"
        )
        return

    await message.answer(
        "👋 Xush kelibsiz!\n\n"
        "📦 Quyidagi kategoriyalardan birini tanlang:",
        reply_markup=categories_keyboard(categories)
    )

# ── /catalog ─────────────────────────────────────────────────────────────────

@dp.message(Command("catalog"))
async def cmd_catalog(message: types.Message):
    categories = db_get_categories()
    if not categories:
        await message.answer("❌ Hozircha mahsulotlar mavjud emas.")
        return
    await message.answer(
        "📦 Kategoriyani tanlang:",
        reply_markup=categories_keyboard(categories)
    )

# ── /stats (faqat admin) ──────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        f"📊 *Statistika*\n\n"
        f"👥 Foydalanuvchilar: {db_count_users()}\n"
        f"📦 Mahsulotlar: {db_count_products()}\n"
        f"🗂 Kategoriyalar: {len(db_get_categories())}",
        parse_mode="Markdown"
    )

# ── Kategoriya tugmasi bosilganda ─────────────────────────────────────────────

# ── Kategoriya tugmasi bosilganda ─────────────────────────────────────────────

# @dp.callback_query(F.data.startswith("cat:"))
# async def cb_category(call: types.CallbackQuery):
#     category = call.data[4:]
#     products = db_get_by_category(category)

#     if not products:
#         await call.answer("Mahsulot topilmadi!", show_alert=True)
#         return

#     await call.answer()
#     await call.message.answer(
#         f"📦 *{category}* — {len(products)} ta mahsulot yuborilmoqda...",
#         parse_mode="Markdown"
#     )

#     # Har bir mahsulot uchun karta yaratib yuborish
#     for product in products:
#         try:
#             img_bytes    = await fetch_image(bot_instance, product["image"])
#             card_bytes   = await build_card(product, img_bytes)
#             input_file   = BufferedInputFile(card_bytes, filename="product.png")
#             kb = InlineKeyboardMarkup(inline_keyboard=[
#     [InlineKeyboardButton(
#         text="🛒 Ariza yuborish", 
#         callback_data=f"buy:{product['id']}" # Oddiy tugma
#     )]
# ])

@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(call: types.CallbackQuery):
    category = call.data[4:]
    products = db_get_by_category(category)

    if not products:
        await call.answer("Mahsulot topilmadi!", show_alert=True)
        return

    await call.answer()
    
    for product in products:
        try:
            img_bytes    = await fetch_image(bot_instance, product["image"])
            card_bytes   = await build_card(product, img_bytes)
            input_file   = BufferedInputFile(card_bytes, filename="product.png")
            
            # WebAppInfo o'rniga oddiy tugma (buy:ID)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Ariza yuborish", callback_data=f"buy:{product['id']}")]
            ])
            
            await call.message.answer_photo(photo=input_file, reply_markup=kb)
        except Exception as e:
            logging.error(f"Karta yaratishda xatolik: {e}")

            
            # --- MANA BU YERDA TUGMANI QO'SHAMIZ ---
            # cb_category funksiyasi ichidagi tugma qismi:
# Mahsulot nomini urlga qo'shamiz (bo'sh joylar bo'lsa, uni kodlab yuborish kerak)


            product_name_encoded = urllib.parse.quote(product["name"])
            web_app_url = f"https://gitmaster11.github.io/wep_page/?product={product_name_encoded}"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                text="🛒 Ariza yuborish", 
                web_app=WebAppInfo(url=web_app_url)
                                                    )]
                                                            ])
                                                                               
            
            await call.message.answer_photo(photo=input_file, reply_markup=kb)
            # ----------------------------------------
            
        except Exception as e:
            logging.error(f"Karta yaratishda xatolik ({product['name']}): {e}")
            await call.message.answer(
                f"📦 *{product['name']}*\n"
                f"💰 Narxi: {product['price'] or '—'}\n"
                f"📅 Muddati: {product['duration'] or '—'}\n"
                f"📆 Oylik: {product['monthly'] or '—'}",
                parse_mode="Markdown"
            )


# 1. Tugma bosilganda:
@dp.callback_query(F.data.startswith("buy:"))
async def start_order(call: types.CallbackQuery, state: FSMContext):
    product_id = call.data.split(":")[1]
    await state.update_data(product_id=product_id) # Mahsulot ID ni saqlab qo'yamiz
    await state.set_state(OrderForm.waiting_for_name)
    await call.message.answer("Iltimos, ismingizni yozing:")

# 2. Ismni qabul qilish:
@dp.message(OrderForm.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(OrderForm.waiting_for_age)
    await message.answer("Yoshingizni kiriting:")

# 3. Yoshni qabul qilish:
@dp.message(OrderForm.waiting_for_age)
async def process_age(message: types.Message, state: FSMContext):
    await state.update_data(age=message.text)
    await state.set_state(OrderForm.waiting_for_phone)
    await message.answer("Telefon raqamingizni kiriting (masalan: +998901234567):")

# 4. Telefonni olish va Adminga yuborish:
# Bazadan ID bo'yicha mahsulotni topish uchun yordamchi funksiya
def db_get_product_by_id(product_id: int) -> dict | None:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None

# Yangilangan process_phone funksiyasi
@dp.message(OrderForm.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get('product_id')
    
    # Bazadan mahsulotni qidiramiz
    product = db_get_product_by_id(int(product_id))
    product_name = product["name"] if product else f"ID: {product_id}"
    
    admin_text = (
        f"🔔 *Yangi buyurtma keldi!*\n\n"
        f"📦 *Mahsulot:* {product_name}\n\n"
        f"👤 *Ismi:* {data['name']}\n"
        f"📅 *Yoshi:* {data['age']}\n"
        f"📞 *Telefon:* {message.text}"
    )
    
    # Barcha adminlarga xabar yuborish (xavfsiz usul)
    for admin_id in ADMIN_IDS:
        try:
            await bot_instance.send_message(admin_id, admin_text, parse_mode="Markdown")
        except Exception as e:
            # Agar biror admin botni bloklagan bo'lsa, xatolik chiqadi, 
            # lekin biz uni 'try-except' bilan ushlab qoldik, bot ishlayveradi.
            logging.warning(f"Admin {admin_id} ga xabar yuborishda muammo: {e}")
        
    await message.answer("✅ Buyurtmangiz qabul qilindi! Operatorlarimiz tez orada siz bilan bog'lanishadi.")
    await state.clear()


# Mijoz Web App'dan ma'lumot yuborganda ishga tushadi
import json

# Faylning tepasiga shuni qo'shib qo'y (agar hali yo'q bo'lsa)
from aiogram import F
cb_category
# Quyidagi funksiyani almashtir:
@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    print("Telegramdan ma'lumot keldi first ieoiueoriuoi")
    try:
        # Debug uchun (terminalda ko'rinadi)
        print(f"DEBUG: Web App data keldi: {message.web_app_data.data}")
        print("Telegramdan ma'lumot keldi")
        data = json.loads(message.web_app_data.data)
        
        admin_text = (
            f"🔔 *Yangi buyurtma (Web App)!*\n\n"
            f"📦 *Mahsulot:* {data.get('product')}\n"
            f"👤 *Ismi:* {data.get('name')}\n"
            f"📅 *Yoshi:* {data.get('age')}\n"
            f"📞 *Telefon:* {data.get('phone')}"
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await bot_instance.send_message(admin_id, admin_text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Admin {admin_id} ga yuborishda xatolik: {e}")
        
        await message.answer("✅ Buyurtmangiz qabul qilindi!")
        
    except Exception as e:
        logging.error(f"Web App ma'lumotni qayta ishlashda xatolik: {e}")
        await message.answer("❌ Xatolik yuz berdi, iltimos qayta urinib ko'ring.")

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
        await wait_msg.edit_text(f"❌ Xatolik: {error}")
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

