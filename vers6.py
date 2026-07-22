import sys
import asyncio
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
import os
import json
import re
import html
import sqlite3
import secrets
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, status, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from jinja2 import Template

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest

# --- КОНФИГУРАЦИЯ ---
STORES = {
    "yandex_fabrika": {
        "name": "Яндекс Фабрика",
        "url": "https://market.yandex.ru/business--yandex-fabrika/83022309?generalContext=t%3DshopInShop%3Bi%3D1%3Bbi%3D83022309%3B&how=aprice&rs=eJwzUv7EqMDBKLDwEKsEg8azbh6N-0dYNXqOsmqcPc2qsQrIft7NAwDdwwz7&searchContext=sins_ctx&resale_goods=resale_resale"
    },
    "yandex_market": {
        "name": "Яндекс Маркет",
        "url": "https://market.yandex.ru/business--yandex-market/924574"
    }
}

COOKIES_FILE = "cookies.json"
DB_FILE = "products.db"
CONCURRENCY_LIMIT = 3 

# ⚠️ ВСТАВЬТЕ СВОЙ ТОКЕН СЮДА
TELEGRAM_BOT_TOKEN = "8966210466:AAEqwK-CoT0Bwl07utwqErgf5MkR2Ylo86o"

# 🌐 ВАШ HTTPS АДРЕС (нужен для Mini App: например, ngrok URL или адрес сервера)
WEB_APP_URL = "https://wq2521-production.up.railway.app/"

ADMIN_SECRET_URL = "/secret-admin-manage-2026-panel"
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"

security = HTTPBasic()
FAILED_ATTEMPTS = {}

PARSER_STATE = {
    "is_active": False,
    "current_store": "—",
    "mode": "В ожидании",
    "progress_text": "Готов к запуску",
    "last_run": "Еще не запускался"
}

LOG_QUEUE = asyncio.Queue()

# --- ЛОГИРОВАНИЕ И БД ---
async def log_worker():
    while True:
        timestamp, formatted_msg = await LOG_QUEUE.get()
        try:
            def _write():
                conn = sqlite3.connect(DB_FILE, timeout=10)
                cursor = conn.cursor()
                cursor.execute("INSERT INTO logs (timestamp, message) VALUES (?, ?)", (timestamp, formatted_msg))
                conn.commit()
                conn.close()
            await asyncio.to_thread(_write)
        except Exception as e:
            print(f"Ошибка фоновой записи лога: {e}", flush=True)
        finally:
            LOG_QUEUE.task_done()

def db_log(msg: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    try:
        LOG_QUEUE.put_nowait((timestamp, formatted))
    except Exception:
        pass

def get_db_logs(limit=100):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in reversed(rows)]
    except Exception:
        return []

def clean_url(url: str) -> str:
    if not url or url == "#":
        return url
    parsed = urlparse(url)
    clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    return clean.rstrip('/')

def parse_price_value(price_str: str) -> int:
    digits = re.findall(r'\d+', price_str.replace(" ", ""))
    return int(digits[0]) if digits else 0

def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            link TEXT PRIMARY KEY,
            category_id TEXT,
            title TEXT,
            price TEXT,
            price_num INTEGER DEFAULT 0,
            discount TEXT,
            discount_num INTEGER,
            first_seen TEXT,
            last_updated TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT,
            price_num INTEGER,
            recorded_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            message TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            subscribed_category TEXT DEFAULT 'all',
            joined_at TEXT
        )
    """)
    
    try:
        cursor.execute("ALTER TABLE subscribers ADD COLUMN subscribed_category TEXT DEFAULT 'all'")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

init_db()

def record_price_history(cursor, link: str, price_num: int, now_str: str):
    cursor.execute("SELECT price_num FROM price_history WHERE link = ? ORDER BY id DESC LIMIT 1", (link,))
    last_entry = cursor.fetchone()
    if not last_entry or last_entry[0] != price_num:
        cursor.execute("INSERT INTO price_history (link, price_num, recorded_at) VALUES (?, ?, ?)",
                       (link, price_num, now_str))

# --- TELEGRAM УВЕДОМЛЕНИЯ И КНОПКИ ---
def get_subscribers_for_category(category_id: str):
    conn = sqlite3.connect(DB_FILE, timeout=5)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM subscribers WHERE subscribed_category = 'all' OR subscribed_category = ?", (category_id,))
    users = cursor.fetchall()
    conn.close()
    return [u[0] for u in users]

def get_user_subscription(chat_id: int) -> str:
    conn = sqlite3.connect(DB_FILE, timeout=5)
    cursor = conn.cursor()
    cursor.execute("SELECT subscribed_category FROM subscribers WHERE chat_id = ?", (chat_id,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else "all"

def set_user_subscription(chat_id: int, category: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE, timeout=5)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO subscribers (chat_id, subscribed_category, joined_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET subscribed_category = excluded.subscribed_category
    """, (chat_id, category, now_str))
    conn.commit()
    conn.close()

def get_settings_keyboard(current_cat: str):
    buttons = [
        [InlineKeyboardButton("🔥 Открыть каталог (Mini App)", web_app=WebAppInfo(url=WEB_APP_URL))],
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'all' else ''}🌐 Все категории уведомлений", callback_data="sub_all")],
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'yandex_fabrika' else ''}🏭 Яндекс Фабрика", callback_data="sub_yandex_fabrika")],
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'yandex_market' else ''}🛒 Яндекс Маркет", callback_data="sub_yandex_market")]
    ]
    return InlineKeyboardMarkup(buttons)

async def send_telegram_notification(product: dict):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "ВАШ_ТОКЕН_ОТ_BOTFATHER":
        return

    subscribers = await asyncio.to_thread(get_subscribers_for_category, product['category_id'])
    if not subscribers:
        return

    discount_str = f"🔥 <b>Скидка:</b> {product['discount']}\n" if product.get('discount') and product['discount'] != "—" else ""
    
    msg_text = (
        f"🚨 <b>НАЙДЕН НОВЫЙ ТОВАР!</b>\n\n"
        f"📦 <b>{html.escape(product['title'])}</b>\n\n"
        f"💰 <b>Цена:</b> {product['price']}\n"
        f"{discount_str}"
        f"🏬 <b>Категория:</b> {product['category_name']}\n\n"
        f"🔗 <a href='{product['link']}'>Открыть карточку товара</a>"
    )

    req = HTTPXRequest(connect_timeout=10.0, read_timeout=10.0)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, request=req)
    
    for chat_id in subscribers:
        try:
            await bot.send_message(
                chat_id=chat_id, 
                text=msg_text, 
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            db_log(f"⚠️ Ошибка отправки пользователю {chat_id}: {e}")

# --- ЗАЩИТА АДМИНКИ ---
def authenticate_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    client_ip = request.client.host
    now = datetime.now()

    if client_ip in FAILED_ATTEMPTS:
        attempt_info = FAILED_ATTEMPTS[client_ip]
        if attempt_info.get("blocked_until") and now < attempt_info["blocked_until"]:
            time_left = int((attempt_info["blocked_until"] - now).total_seconds())
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Заблокировано на {time_left} сек."
            )

    is_user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
    is_pass_ok = secrets.compare_digest(credentials.password, ADMIN_PASS)

    if not (is_user_ok and is_pass_ok):
        if client_ip not in FAILED_ATTEMPTS:
            FAILED_ATTEMPTS[client_ip] = {"count": 1, "blocked_until": None}
        else:
            FAILED_ATTEMPTS[client_ip]["count"] += 1

        if FAILED_ATTEMPTS[client_ip]["count"] >= 3:
            FAILED_ATTEMPTS[client_ip]["blocked_until"] = now + timedelta(minutes=15)

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )

    if client_ip in FAILED_ATTEMPTS:
        del FAILED_ATTEMPTS[client_ip]

    return credentials.username

# --- ДВИЖОК ПАРСИНГА ---
async def load_all_products(page, store_name):
    previous_count = 0
    no_change_attempts = 0
    max_no_change = 5  # Увеличено для упорного скролла

    db_log(f"📜 Сканирование витрины ({store_name})...")

    while True:
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            await page.wait_for_timeout(2500)  # Увеличенная пауза для подгрузки

            more_button = await page.query_selector('button[data-auto="pagination-next"], [data-zone-name="show-more-button"]')
            if more_button and await more_button.is_visible():
                await more_button.scroll_into_view_if_needed()
                await more_button.click()
                await page.wait_for_timeout(2500)

            current_cards = await page.query_selector_all('div[data-data-source="ss-product"], div[data-zone-name="title"]')
            current_count = len(current_cards)
            
            PARSER_STATE["progress_text"] = f"Скроллинг ({store_name}): найдено ~{current_count} товаров"

            if current_count == previous_count:
                no_change_attempts += 1
                await page.evaluate("window.scrollBy(0, -500);")
                await page.wait_for_timeout(1000)
                await page.evaluate("window.scrollBy(0, 500);")
                await page.wait_for_timeout(1500)

                if no_change_attempts >= max_no_change:
                    db_log("🎉 Витрина полностью прокручена.")
                    break
            else:
                no_change_attempts = 0
                previous_count = current_count
        except Exception as e:
            db_log(f"⚠️ Предупреждение во время скроллинга: {e}")
            break

async def handle_route(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

async def parse_single_product(context, item, semaphore, counter, total_items):
    async with semaphore:
        if not item['link'] or item['link'] == "#":
            return {**item, "discount": "—", "discount_num": 0}

        discount, discount_num = "—", 0
        page = None

        try:
            page = await context.new_page()
            await page.route("**/*", handle_route)

            await asyncio.wait_for(
                page.goto(item['link'], wait_until="commit", timeout=5000),
                timeout=6.0
            )
            
            discount_pattern = re.compile(r'[\-–—−]\d+%')
            try:
                text = await page.locator('span[class*="ds-text_lead-text"]').filter(has_text=discount_pattern).first.text_content(timeout=1500)
                if text:
                    discount = text.strip()
            except Exception:
                pass

            if discount != "—":
                digits = re.findall(r'\d+', discount)
                if digits:
                    discount_num = int(digits[0])

        except Exception:
            pass
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

        counter['done'] += 1
        PARSER_STATE["progress_text"] = f"Детали [{item['category_name']}]: {counter['done']}/{total_items}"

        return {
            **item,
            "discount": discount,
            "discount_num": discount_num
        }

async def parse_store(store_key: str, with_discounts: bool, browser, cookies):
    store_info = STORES[store_key]
    PARSER_STATE["current_store"] = store_info["name"]
    db_log(f"🛒 === Старт обработки: {store_info['name']} ===")

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        viewport={"width": 1280, "height": 720}
    )
    await context.add_cookies(cookies)
    main_page = await context.new_page()

    try:
        PARSER_STATE["progress_text"] = f"Загрузка {store_info['name']}..."
        await main_page.goto(store_info["url"], wait_until="commit", timeout=15000)
        await main_page.wait_for_timeout(1500)

        await load_all_products(main_page, store_info["name"])

        html_content = await main_page.content()
        
        def parse_soup_sync(content):
            soup = BeautifulSoup(content, "html.parser")
            cards = soup.find_all("div", {"data-data-source": "ss-product"})
            if not cards:
                cards = soup.find_all("div", {"data-zone-name": "title"})
            return cards

        cards = await asyncio.to_thread(parse_soup_sync, html_content)

        raw_products_map = {}
        for card in cards:
            title_elem = card.find("span", {"data-auto": "snippet-title"}) or card.find("span", {"itemprop": "name"})
            link_elem = card.find("a", {"data-auto": "snippet-link"})
            price_elem = card.find("span", {"data-auto": "main-price"}) or card.find("span", {"data-auto": "snippet-price-current"})

            if title_elem:
                title = title_elem.get("title") or title_elem.text.strip()
                raw_link = link_elem["href"] if link_elem and "href" in link_elem.attrs else "#"
                if raw_link != "#" and not raw_link.startswith("http"):
                    raw_link = f"https://market.yandex.ru{raw_link}"

                cleaned_link = clean_url(raw_link)
                price = price_elem.text.strip().replace("\xa0", " ") if price_elem else "0 ₽"
                price_num = parse_price_value(price)

                if cleaned_link not in raw_products_map:
                    raw_products_map[cleaned_link] = {
                        "title": title,
                        "price": price,
                        "price_num": price_num,
                        "link": cleaned_link,
                        "category_id": store_key,
                        "category_name": store_info["name"]
                    }

        raw_products = list(raw_products_map.values())

        def process_db_sync(products, store_id):
            conn = sqlite3.connect(DB_FILE, timeout=10)
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            processed = []
            new_items_list = []

            for item in products:
                cursor.execute("SELECT link, price_num FROM products WHERE link = ?", (item['link'],))
                existing = cursor.fetchone()

                if not existing:
                    item_copy = {**item, "is_new": True, "discount": "—", "discount_num": 0}
                    cursor.execute("""
                        INSERT INTO products (link, category_id, title, price, price_num, discount, discount_num, first_seen, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (item['link'], item['category_id'], item['title'], item['price'], item['price_num'], "—", 0, now_str, now_str))
                    new_items_list.append(item_copy)
                    processed.append(item_copy)
                else:
                    item_copy = {**item, "is_new": False}
                    cursor.execute("""
                        UPDATE products 
                        SET title = ?, price = ?, price_num = ?, last_updated = ?
                        WHERE link = ?
                    """, (item['title'], item['price'], item['price_num'], now_str, item['link']))
                    processed.append(item_copy)

                record_price_history(cursor, item['link'], item['price_num'], now_str)

            conn.commit()
            conn.close()
            return processed, new_items_list

        processed_products, new_items = await asyncio.to_thread(process_db_sync, raw_products, store_key)
        total_items = len(processed_products)

        final_products = []
        if with_discounts and total_items > 0:
            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
            counter = {'done': 0}

            tasks = [
                parse_single_product(context, item, semaphore, counter, total_items)
                for item in processed_products
            ]
            final_products = await asyncio.gather(*tasks)
        else:
            final_products = [{**item, "discount": "—", "discount_num": 0} for item in processed_products]

        def update_details_in_db(items, is_with_discounts):
            conn = sqlite3.connect(DB_FILE, timeout=10)
            cursor = conn.cursor()
            for item in items:
                if is_with_discounts:
                    cursor.execute("""
                        UPDATE products SET discount = ?, discount_num = ? WHERE link = ?
                    """, (item['discount'], item['discount_num'], item['link']))
            conn.commit()
            conn.close()

        await asyncio.to_thread(update_details_in_db, final_products, with_discounts)

        if new_items:
            db_log(f"🔔 Найдено новых товаров: {len(new_items)}. Отправка в Telegram...")
            new_links_set = {ni['link'] for ni in new_items}
            for item in final_products:
                if item['link'] in new_links_set:
                    await send_telegram_notification(item)

        db_log(f"✅ Магазин {store_info['name']} успешно обновлен! (Всего товаров: {total_items})")

    finally:
        try:
            await context.close()
        except Exception:
            pass

async def execute_parsing_task(target_store: str = "all", with_discounts: bool = False):
    global PARSER_STATE

    if PARSER_STATE["is_active"]:
        return

    PARSER_STATE["is_active"] = True
    mode_desc = "ПОЛНЫЙ (со скидками)" if with_discounts else "БЫСТРЫЙ"
    PARSER_STATE["mode"] = mode_desc
    
    db_log(f"🚀 Запуск парсинга [{target_store.upper()}] [{mode_desc}]")

    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except FileNotFoundError:
        db_log("❌ ОШИБКА: cookies.json не найден!")
        PARSER_STATE["is_active"] = False
        return

    for cookie in cookies:
        if cookie.get("sameSite") == "no_restriction":
            cookie["sameSite"] = "None"
        elif cookie.get("sameSite") is None:
            cookie["sameSite"] = "Lax"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-gpu', '--no-sandbox'])
        try:
            if target_store == "all":
                for key in STORES.keys():
                    await parse_store(key, with_discounts, browser, cookies)
            else:
                if target_store in STORES:
                    await parse_store(target_store, with_discounts, browser, cookies)

            PARSER_STATE["last_run"] = datetime.now().strftime("%d.%m.%Y в %H:%M")
            PARSER_STATE["progress_text"] = "Завершено"
            db_log("🎉 ВСЕ ЗАДАЧИ ВЫПОЛНЕНЫ!")

        except Exception as e:
            db_log(f"❌ Ошибка приложения: {e}")
            PARSER_STATE["progress_text"] = f"Ошибка: {e}"
        finally:
            await browser.close()
            PARSER_STATE["is_active"] = False
            PARSER_STATE["current_store"] = "—"

# --- BOT HANDLERS ---
async def start_telegram_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current_cat = await asyncio.to_thread(get_user_subscription, chat_id)
    keyboard = get_settings_keyboard(current_cat)
    
    await update.message.reply_text(
        "👋 **Добро пожаловать в бот скидок!**\n\n"
        "Откройте каталог прямо в Telegram через Mini App или выберите категории для получения уведомлений:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def settings_telegram_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_telegram_cmd(update, context)

async def category_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    cat_code = query.data.replace("sub_", "")
    
    await asyncio.to_thread(set_user_subscription, chat_id, cat_code)
    
    keyboard = get_settings_keyboard(cat_code)
    await query.edit_message_text(
        "✅ **Настройки уведомлений обновлены!**\n\n"
        "Откройте каталог через кнопку Mini App или измените настройки:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    log_task = asyncio.create_task(log_worker())
    
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "ВАШ_ТОКЕН_ОТ_BOTFATHER":
        try:
            req = HTTPXRequest(connect_timeout=10.0, read_timeout=10.0)
            tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(req).build()
            tg_app.add_handler(CommandHandler("start", start_telegram_cmd))
            tg_app.add_handler(CommandHandler("settings", settings_telegram_cmd))
            tg_app.add_handler(CallbackQueryHandler(category_callback_handler, pattern="^sub_"))
            
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling()
            db_log("🤖 Telegram-бот запущен!")
        except Exception as e:
            db_log(f"⚠️ Ошибка бота: {e}")

    yield
    log_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- АДАПТИВНЫЕ СТИЛИ (MOBILE-FRIENDLY) ---
LIGHT_THEME_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    :root {
        --bg-color: #f4f6f8; --card-bg: #ffffff; --text-primary: #1e293b;
        --text-secondary: #64748b; --border-color: #e2e8f0; --accent-blue: #2563eb;
        --accent-green: #10b981; --accent-red: #ef4444; --accent-gold: #f59e0b;
        --radius-card: 16px; --radius-btn: 30px;
    }
    * { box-sizing: border-box; transition: background 0.2s, border 0.2s; }
    body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-primary); margin: 0; padding: 12px 8px; overflow-x: hidden; }
    .container { max-width: 1280px; margin: 0 auto; width: 100%; }
    .card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--radius-card); padding: 16px; margin-bottom: 12px; box-shadow: 0 4px 20px rgba(0, 0, 0, 0.03); }
    h1 { font-size: 1.5rem; font-weight: 800; margin: 0 0 6px 0; }
    .btn { padding: 8px 16px; border-radius: var(--radius-btn); border: none; font-weight: 600; font-size: 0.85rem; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 6px; text-decoration: none; color: #fff; }
    .btn:hover { opacity: 0.9; }
    .btn-blue { background-color: var(--accent-blue); }
    .btn-outline { background: transparent; border: 2px solid var(--border-color); color: var(--text-primary); }
    .btn-outline.active { background: var(--accent-blue); color: #fff; border-color: var(--accent-blue); }
    
    .nav-tabs { display: flex; gap: 8px; margin-bottom: 12px; overflow-x: auto; padding-bottom: 4px; -webkit-overflow-scrolling: touch; }
    .tab-item { padding: 8px 16px; border-radius: var(--radius-btn); background: var(--card-bg); border: 1px solid var(--border-color); color: var(--text-secondary); text-decoration: none; font-weight: 600; white-space: nowrap; font-size: 0.85rem; }
    .tab-item.active { background: var(--accent-blue); color: #ffffff; border-color: var(--accent-blue); }
    
    .table-container { background: var(--card-bg); border-radius: var(--radius-card); border: 1px solid var(--border-color); overflow-x: auto; -webkit-overflow-scrolling: touch; }
    table { width: 100%; border-collapse: collapse; text-align: left; min-width: 600px; }
    th { background: #f8fafc; padding: 12px 10px; color: var(--text-secondary); font-size: 0.75rem; text-transform: uppercase; border-bottom: 1px solid var(--border-color); user-select: none; }
    td { padding: 10px; border-bottom: 1px solid var(--border-color); vertical-align: middle; font-size: 0.9rem; }
    
    .discount-pill { background: #fef2f2; color: var(--accent-red); font-weight: 700; padding: 3px 8px; border-radius: 10px; font-size: 0.8rem; }
    .fav-btn { background: none; border: none; font-size: 1.2rem; cursor: pointer; }
    
    .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); display: none; justify-content: center; align-items: center; z-index: 1000; padding: 10px; }
    .modal-content { background: #fff; padding: 20px; border-radius: var(--radius-card); max-width: 600px; width: 100%; max-height: 85vh; overflow-y: auto; }
    .terminal-box { background: #0f172a; color: #38bdf8; border-radius: 12px; padding: 15px; height: 280px; overflow-y: auto; font-family: monospace; font-size: 0.8rem; }
    
    .counter-badge {
        background: #eff6ff; color: var(--accent-blue); border: 1px solid #bfdbfe;
        padding: 5px 12px; border-radius: 16px; font-weight: 700; font-size: 0.8rem;
        display: inline-flex; align-items: center; gap: 6px;
    }

    @media (max-width: 768px) {
        body { padding: 8px 4px; }
        h1 { font-size: 1.3rem; }
        .card { padding: 12px; border-radius: 12px; }
        #searchInput, #sortSelect, #filterSuperBtn { width: 100%; text-align: center; }
        td, th { padding: 8px 6px; font-size: 0.8rem; }
        .btn { padding: 6px 12px; font-size: 0.8rem; }
    }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
"""

USER_TEMPLATE = LIGHT_THEME_CSS + """
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>Витрина Скидок</title></head>
<body>
    <div class="container">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px;">
            <div>
                <h1>🔥 Витрина Скидок</h1>
                <div style="color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 6px;">🕒 Обновлено: {{ last_update }}</div>
                <div class="counter-badge">📦 Товаров: {{ total_count }}</div>
            </div>
            <div>
                <button class="btn btn-outline" id="favFilterBtn" onclick="toggleFavFilter()">❤️ Избранное (<span id="favCount">0</span>)</button>
            </div>
        </div>

        <div class="nav-tabs">
            {% for key, store in stores.items() %}
            <a href="/?category={{ key }}" class="tab-item {% if current_category == key %}active{% endif %}">{{ store.name }}</a>
            {% endfor %}
        </div>

        <div class="card" style="padding: 12px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center;">
            <input type="text" id="searchInput" class="btn btn-outline" style="flex-grow: 1; text-align: left;" placeholder="🔍 Поиск по названию..." onkeyup="applyFilters()">
            <button class="btn btn-outline" id="filterSuperBtn" onclick="toggleSuperDiscount()">💥 Скидки >30%</button>
            
            <select id="sortSelect" class="btn btn-outline" onchange="applyFilters()">
                <option value="default">Сортировка по умолчанию</option>
                <option value="discount_desc">Скидка: Сначала больше %</option>
                <option value="price_asc">Цена: Сначала дешевле</option>
            </select>
        </div>

        {% if products %}
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th style="width: 35px;">❤️</th>
                        <th>Наименование товара</th>
                        <th style="width: 120px;">Цена</th>
                        <th style="text-align: center; width: 100px;">Скидка</th>
                        <th style="text-align: right; width: 160px;">Действия</th>
                    </tr>
                </thead>
                <tbody id="tableBody">
                    {% for p in products %}
                    <tr data-link="{{ p.link }}" 
                        data-title="{{ p.title }}"
                        data-price="{{ p.price_num }}"
                        data-discount="{{ p.discount_num }}"
                        data-price-str="{{ p.price }}"
                        data-discount-str="{{ p.discount }}">
                        
                        <td>
                            <button class="fav-btn" onclick="toggleFav('{{ p.link }}')">🤍</button>
                        </td>
                        <td>
                            <strong style="cursor: pointer;" onclick="openChart('{{ p.link }}', '{{ p.title|e }}')">{{ p.title }}</strong>
                        </td>
                        <td style="font-weight: 700; color: var(--accent-blue);">{{ p.price }}</td>
                        <td style="text-align: center;">
                            {% if p.discount != '—' %}<span class="discount-pill">{{ p.discount }}</span>{% else %}—{% endif %}
                        </td>
                        <td style="text-align: right; display: flex; gap: 4px; justify-content: flex-end;">
                            <button class="btn btn-outline" style="padding: 5px 10px;" onclick="openChart('{{ p.link }}', '{{ p.title|e }}')" title="График">📈</button>
                            <button class="btn btn-outline" style="padding: 5px 10px;" onclick="copyPost(this)" title="Копировать">📋</button>
                            <a href="{{ p.link }}" target="_blank" class="btn btn-blue" style="padding: 5px 12px; font-size: 0.8rem;">Купить ↗</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <div class="card" style="text-align: center; padding: 40px;">
            <h3>Товары не найдены</h3>
            <p style="color: var(--text-secondary);">Запустите парсинг через админ-панель.</p>
        </div>
        {% endif %}
    </div>

    <!-- MODAL -->
    <div class="modal-overlay" id="chartModal">
        <div class="modal-content">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <h3 id="modalTitle" style="margin: 0; font-size: 1rem;">История цен</h3>
                <button class="btn btn-outline" style="padding: 2px 8px;" onclick="closeChart()">✕</button>
            </div>
            <canvas id="priceChartCanvas"></canvas>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        if (tg) {
            tg.expand();
            if (tg.backgroundColor) {
                document.documentElement.style.setProperty('--bg-color', tg.backgroundColor);
            }
        }

        let favorites = JSON.parse(localStorage.getItem('fav_products') || '[]');
        let showOnlyFavs = false;
        let showOnlySuper = false;
        let chartInstance = null;

        function updateFavIcons() {
            document.querySelectorAll('#tableBody tr').forEach(row => {
                const link = row.getAttribute('data-link');
                const btn = row.querySelector('.fav-btn');
                btn.textContent = favorites.includes(link) ? '❤️' : '🤍';
            });
            document.getElementById('favCount').textContent = favorites.length;
        }

        function toggleFav(link) {
            favorites = favorites.includes(link) ? favorites.filter(id => id !== link) : [...favorites, link];
            localStorage.setItem('fav_products', JSON.stringify(favorites));
            updateFavIcons();
            if (showOnlyFavs) applyFilters();
        }

        function toggleFavFilter() {
            showOnlyFavs = !showOnlyFavs;
            document.getElementById('favFilterBtn').classList.toggle('active', showOnlyFavs);
            applyFilters();
        }

        function toggleSuperDiscount() {
            showOnlySuper = !showOnlySuper;
            document.getElementById('filterSuperBtn').classList.toggle('active', showOnlySuper);
            applyFilters();
        }

        function applyFilters() {
            const query = document.getElementById('searchInput').value.toLowerCase();
            const sortVal = document.getElementById('sortSelect').value;
            const tbody = document.getElementById('tableBody');
            if (!tbody) return;
            const rows = Array.from(tbody.querySelectorAll('tr'));

            rows.forEach(row => {
                const title = row.getAttribute('data-title').toLowerCase();
                const link = row.getAttribute('data-link');
                const discount = parseInt(row.getAttribute('data-discount')) || 0;

                let visible = title.includes(query);
                if (showOnlyFavs && !favorites.includes(link)) visible = false;
                if (showOnlySuper && discount < 30) visible = false;

                row.style.display = visible ? '' : 'none';
            });

            rows.sort((a, b) => {
                if (sortVal === 'discount_desc') {
                    return (parseInt(b.getAttribute('data-discount')) || 0) - (parseInt(a.getAttribute('data-discount')) || 0);
                } else if (sortVal === 'price_asc') {
                    return (parseInt(a.getAttribute('data-price')) || 0) - (parseInt(b.getAttribute('data-price')) || 0);
                }
                return 0;
            });

            rows.forEach(row => tbody.appendChild(row));
        }

        function copyPost(btn) {
            const row = btn.closest('tr');
            const title = row.getAttribute('data-title');
            const price = row.getAttribute('data-price-str');
            const discount = row.getAttribute('data-discount-str');
            const link = row.getAttribute('data-link');

            const text = `💥 ${title}\\n💰 Цена: ${price} ${discount !== '—' ? '(' + discount + ')' : ''}\\n🔗 ${link}`;
            navigator.clipboard.writeText(text);

            const origText = btn.textContent;
            btn.textContent = '✅';
            setTimeout(() => btn.textContent = origText, 1500);
        }

        async function openChart(link, title) {
            document.getElementById('modalTitle').textContent = title;
            document.getElementById('chartModal').style.display = 'flex';

            const resp = await fetch(`/api/price-history?link=${encodeURIComponent(link)}`);
            const history = await resp.json();

            const ctx = document.getElementById('priceChartCanvas').getContext('2d');
            if (chartInstance) chartInstance.destroy();

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: history.map(h => h.date),
                    datasets: [{
                        label: 'Цена (₽)',
                        data: history.map(h => h.price),
                        borderColor: '#2563eb',
                        backgroundColor: 'rgba(37, 99, 235, 0.1)',
                        fill: true,
                        tension: 0.3
                    }]
                },
                options: { responsive: true }
            });
        }

        function closeChart() {
            document.getElementById('chartModal').style.display = 'none';
        }

        updateFavIcons();
    </script>
</body>
</html>
"""

ADMIN_TEMPLATE = LIGHT_THEME_CSS + """
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>Панель Управления</title></head>
<body>
    <div class="container">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
            <h1>⚙️ Админ-панель</h1>
            <a href="/" class="btn btn-outline">← На витрину</a>
        </div>

        <div class="card">
            <h2>🚀 Запуск сбора</h2>
            <form method="POST" action="{{ admin_url }}/start" style="display: flex; flex-direction: column; gap: 12px; margin-top: 12px;">
                <div>
                    <label style="font-weight: 600; display: block; margin-bottom: 4px; color: var(--text-secondary); font-size: 0.85rem;">Категория:</label>
                    <select name="target_store" class="btn btn-outline" style="width: 100%; text-align: left; padding: 10px;">
                        <option value="all">🌐 Все магазины по очереди</option>
                        {% for key, store in stores.items() %}
                        <option value="{{ key }}">🏬 {{ store.name }}</option>
                        {% endfor %}
                    </select>
                </div>

                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    <button type="submit" name="mode" value="fast" class="btn btn-blue action-btn" style="flex: 1; min-width: 140px;" {% if state.is_active %}disabled{% endif %}>⚡ Быстрый сбор</button>
                    <button type="submit" name="mode" value="full" class="btn btn-blue action-btn" style="flex: 1; min-width: 140px; background: var(--accent-green);" {% if state.is_active %}disabled{% endif %}>🔎 Со скидками</button>
                </div>
            </form>
        </div>

        <div class="card">
            <h2>📊 Мониторинг</h2>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; font-size: 0.9rem;">
                <div>
                    <span style="color: var(--text-secondary); font-size: 0.8rem;">Статус:</span>
                    <div id="statusBadge" style="font-weight: bold; margin-top: 2px;"></div>
                </div>
                <div>
                    <span style="color: var(--text-secondary); font-size: 0.8rem;">Магазин:</span>
                    <div id="currentStore" style="font-weight: bold; margin-top: 2px; color: var(--accent-blue);">{{ state.current_store }}</div>
                </div>
                <div>
                    <span style="color: var(--text-secondary); font-size: 0.8rem;">Прогресс:</span>
                    <div id="progressText" style="font-weight: bold; margin-top: 2px;">{{ state.progress_text }}</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>💻 Консоль логов</h2>
            <div class="terminal-box" id="terminal"></div>
        </div>
    </div>

    <script>
        async function fetchState() {
            try {
                const response = await fetch('{{ admin_url }}/api/state');
                const data = await response.json();
                
                document.getElementById('currentStore').textContent = data.current_store;
                document.getElementById('progressText').textContent = data.progress_text;
                
                const badge = document.getElementById('statusBadge');
                badge.textContent = data.is_active ? '⏳ ПАРСИНГ...' : '✅ ГОТОВ';
                badge.style.color = data.is_active ? '#f59e0b' : '#10b981';

                const btns = document.querySelectorAll('.action-btn');
                btns.forEach(btn => btn.disabled = data.is_active);

                const term = document.getElementById('terminal');
                term.innerHTML = data.logs.map(log => `<div>${log}</div>`).join('');
                term.scrollTop = term.scrollHeight;
            } catch (e) {}
        }
        setInterval(fetchState, 1500);
        fetchState();
    </script>
</body>
</html>
"""

# --- МАРШРУТЫ SERVER ---
@app.get(ADMIN_SECRET_URL, response_class=HTMLResponse)
async def get_admin_panel(request: Request, username: str = Depends(authenticate_admin)):
    template = Template(ADMIN_TEMPLATE)
    db_logs = await asyncio.to_thread(get_db_logs)
    return template.render(state=PARSER_STATE, stores=STORES, admin_url=ADMIN_SECRET_URL, logs=db_logs)

@app.get(f"{ADMIN_SECRET_URL}/api/state")
async def get_admin_state(request: Request, username: str = Depends(authenticate_admin)):
    state_copy = dict(PARSER_STATE)
    state_copy["logs"] = await asyncio.to_thread(get_db_logs)
    return JSONResponse(content=state_copy)

@app.post(f"{ADMIN_SECRET_URL}/start")
async def start_parsing_job(
    request: Request,
    target_store: str = Form(...), 
    mode: str = Form(...), 
    username: str = Depends(authenticate_admin)
):
    if not PARSER_STATE["is_active"]:
        with_discounts = (mode == "full")
        asyncio.create_task(execute_parsing_task(target_store=target_store, with_discounts=with_discounts))
    return RedirectResponse(url=ADMIN_SECRET_URL, status_code=303)

@app.get("/api/price-history")
async def get_price_history_api(link: str, request: Request):
    def fetch_history():
        conn = sqlite3.connect(DB_FILE, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT price_num, recorded_at FROM price_history WHERE link = ? ORDER BY id ASC", (link,))
        rows = cursor.fetchall()
        conn.close()
        return [{"price": r[0], "date": r[1].split()[0]} for r in rows]

    data = await asyncio.to_thread(fetch_history)
    return JSONResponse(content=data)

@app.get("/", response_class=HTMLResponse)
async def get_user_dashboard(category: str = "yandex_fabrika"):
    if category not in STORES:
        category = "yandex_fabrika"

    def fetch_products():
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM products WHERE category_id = ? ORDER BY discount_num DESC", (category,))
        rows = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) FROM products WHERE category_id = ?", (category,))
        total_count = cursor.fetchone()[0]

        cursor.execute("SELECT last_updated FROM products ORDER BY last_updated DESC LIMIT 1")
        last_upd_row = cursor.fetchone()
        last_update = last_upd_row[0] if last_upd_row else "Никогда"

        conn.close()
        return [dict(row) for row in rows], total_count, last_update

    products, total_count, last_update = await asyncio.to_thread(fetch_products)
    template = Template(USER_TEMPLATE)
    return template.render(
        products=products, 
        stores=STORES, 
        current_category=category,
        total_count=total_count,
        last_update=last_update
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)