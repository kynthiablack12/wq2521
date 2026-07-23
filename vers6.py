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
from urllib.parse import urlparse, urlunparse, quote
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from jinja2 import Template

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    },
    "vseinstrumenty": {
        "name": "ВсеИнструменты.ру",
        "url": "https://market.yandex.ru/business--vseinstrumenty-ru/183049902"
    }
}

COOKIES_FILE = "cookies.json"

# --- НАСТРОЙКА БАЗЫ ДАННЫХ ДЛЯ RAILWAY ---
DB_DIR = "/data" if os.path.exists("/data") else "."
DB_FILE = os.path.join(DB_DIR, "products.db")

CONCURRENCY_LIMIT = 2

TELEGRAM_BOT_TOKEN = "8966210466:AAFNMdDHg54ZdHiUw6APQO0zB57b9_EzGB4"
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
    "last_run": "Еще не запускался",
    "last_activity": 0.0,
    "forced_stop": False
}

LOG_QUEUE = asyncio.Queue()

# --- ЛОГИРОВАНИЕ И БД ---
async def log_worker():
    while True:
        timestamp, formatted_msg = await LOG_QUEUE.get()
        try:
            def _write():
                conn = sqlite3.connect(DB_FILE, timeout=15)
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
    PARSER_STATE["last_activity"] = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else datetime.now().timestamp()
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
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    
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
    db_log(f"База данных успешно инициализирована по пути: {DB_FILE}")

init_db()

def record_price_history(cursor, link: str, price_num: int, now_str: str):
    cursor.execute("SELECT price_num FROM price_history WHERE link = ? ORDER BY id DESC LIMIT 1", (link,))
    last_entry = cursor.fetchone()
    if not last_entry or last_entry[0] != price_num:
        cursor.execute("INSERT INTO price_history (link, price_num, recorded_at) VALUES (?, ?, ?)",
                       (link, price_num, now_str))

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
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'all' else ''}🌐 Все категории уведомлений", callback_data="sub_all")],
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'yandex_fabrika' else ''}🏭 Яндекс Фабрика", callback_data="sub_yandex_fabrika")],
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'yandex_market' else ''}🛒 Яндекс Маркет", callback_data="sub_yandex_market")],
        [InlineKeyboardButton(f"{'✅ ' if current_cat == 'vseinstrumenty' else ''}🛠 ВсеИнструменты.ру", callback_data="sub_vseinstrumenty")]
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

async def test_cookies_validity():
    if not os.path.exists(COOKIES_FILE):
        return {"status": "error", "message": "Файл cookies.json не найден на сервере."}
    
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception as e:
        return {"status": "error", "message": f"Ошибка чтения JSON структуры: {e}"}

    for cookie in cookies:
        if cookie.get("sameSite") == "no_restriction":
            cookie["sameSite"] = "None"
        elif cookie.get("sameSite") is None:
            cookie["sameSite"] = "Lax"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-gpu', '--no-sandbox'])
        context = await browser.new_context()
        try:
            await context.add_cookies(cookies)
            page = await context.new_page()
            response = await page.goto("https://market.yandex.ru/", wait_until="commit", timeout=10000)
            await page.wait_for_timeout(2000)
            
            content = await page.content()
            if "капча" in content.lower() or "подтвердите, что вы не робот" in content.lower():
                await browser.close()
                return {"status": "warning", "message": "⚠️ Куки работают, но Яндекс затребовал капчу!"}
            
            await browser.close()
            return {"status": "success", "message": "✅ Сессия активна! Куки прошли валидацию успешно."}
        except Exception as e:
            await browser.close()
            return {"status": "error", "message": f"❌ Ошибка проверки соединения: {str(e)}"}

async def load_all_products(page, store_name):
    previous_count = 0
    no_change_attempts = 0
    max_no_change = 5

    db_log(f"📜 Сканирование витрины ({store_name})...")

    while True:
        if PARSER_STATE["forced_stop"]:
            break
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            await page.wait_for_timeout(2500)

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

async def fetch_market_median_price(context, title: str) -> int:
    page = None
    try:
        page = await context.new_page()
        await page.route("**/*", handle_route)
        
        encoded_query = quote(title[:60])
        search_url = f"https://market.yandex.ru/search?text={encoded_query}"
        
        await page.goto(search_url, wait_until="commit", timeout=7000)
        await page.wait_for_timeout(1500)
        
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        
        price_elems = soup.find_all("span", {"data-auto": "snippet-price-current"})
        prices = []
        for p in price_elems[:3]:
            val = parse_price_value(p.text)
            if val > 0:
                prices.append(val)
                
        if prices:
            return max(prices)
    except Exception:
        pass
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
    return 0

async def parse_single_product(context, item, semaphore, counter, total_items, market_search_mode: bool):
    if PARSER_STATE["forced_stop"]:
        return {**item, "discount": "—", "discount_num": 0}
        
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

            if market_search_mode and discount == "—":
                market_price = await fetch_market_median_price(context, item['title'])
                if market_price > item['price_num']:
                    calculated_discount = int(round((1 - item['price_num'] / market_price) * 100))
                    if calculated_discount > 0:
                        discount = f"-{calculated_discount}%"
                        discount_num = calculated_discount

            if discount != "—" and discount_num == 0:
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
            
            await asyncio.sleep(0.8 if not market_search_mode else 1.5)

        counter['done'] += 1
        PARSER_STATE["progress_text"] = f"Детали [{item['category_name']}]: {counter['done']}/{total_items}"

        return {
            **item,
            "discount": discount,
            "discount_num": discount_num
        }

async def parse_store(store_key: str, with_discounts: bool, send_tg: bool, market_search_mode: bool, browser, cookies):
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

        if PARSER_STATE["forced_stop"]:
            return

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

        if PARSER_STATE["forced_stop"]:
            return

        def process_db_sync(products, store_id):
            conn = sqlite3.connect(DB_FILE, timeout=15)
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
        if (with_discounts or market_search_mode) and total_items > 0 and not PARSER_STATE["forced_stop"]:
            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
            counter = {'done': 0}

            tasks = [
                parse_single_product(context, item, semaphore, counter, total_items, market_search_mode)
                for item in processed_products
            ]
            final_products = await asyncio.gather(*tasks)
        else:
            final_products = [{**item, "discount": "—", "discount_num": 0} for item in processed_products]

        if PARSER_STATE["forced_stop"]:
            return

        def update_details_in_db(items, is_active_mode):
            conn = sqlite3.connect(DB_FILE, timeout=15)
            cursor = conn.cursor()
            for item in items:
                if is_active_mode:
                    cursor.execute("""
                        UPDATE products SET discount = ?, discount_num = ? WHERE link = ?
                    """, (item['discount'], item['discount_num'], item['link']))
            conn.commit()
            conn.close()

        await asyncio.to_thread(update_details_in_db, final_products, (with_discounts or market_search_mode))

        if new_items and send_tg and not PARSER_STATE["forced_stop"]:
            db_log(f"🔔 Найдено новых товаров: {len(new_items)}. Отправка в Telegram...")
            new_links_set = {ni['link'] for ni in new_items}
            for item in final_products:
                if item['link'] in new_links_set:
                    await send_telegram_notification(item)
        elif new_items and not send_tg:
            db_log(f"🔕 Найдено новых товаров: {len(new_items)}, но отправка в Telegram отключена в настройках.")

        db_log(f"✅ Магазин {store_info['name']} успешно обновлен! (Всего товаров: {total_items})")

    finally:
        try:
            await context.close()
        except Exception:
            pass

async def execute_parsing_task(target_store: str = "all", with_discounts: bool = False, send_tg: bool = True, market_search_mode: bool = False):
    global PARSER_STATE

    if PARSER_STATE["is_active"]:
        return

    PARSER_STATE["is_active"] = True
    PARSER_STATE["forced_stop"] = False
    PARSER_STATE["last_activity"] = asyncio.get_event_loop().time()
    
    mode_desc = "ГЛУБОКИЙ АНАЛИЗ РЫНКА" if market_search_mode else ("ПОЛНЫЙ (со скидками)" if with_discounts else "БЫСТРЫЙ")
    PARSER_STATE["mode"] = mode_desc
    
    db_log(f"🚀 Запуск парсинга [{target_store.upper()}] [{mode_desc}] [TG-отправка: {'ВКЛ' if send_tg else 'ВЫКЛ'}]")

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
                    if PARSER_STATE["forced_stop"]:
                        break
                    await parse_store(key, with_discounts, send_tg, market_search_mode, browser, cookies)
                    if not PARSER_STATE["forced_stop"]:
                        await asyncio.sleep(2.0)
            else:
                if target_store in STORES:
                    await parse_store(target_store, with_discounts, send_tg, market_search_mode, browser, cookies)

            if PARSER_STATE["forced_stop"]:
                db_log("⚠️ Парсинг был принудительно остановлен пользователем. Все успешно обработанные данные сохранены в каталоге.")
                PARSER_STATE["progress_text"] = "Остановлено пользователем (данные сохранены)"
            else:
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
        "Откройте каталог через кнопку меню (Web App) слева от поля ввода или настройте категории уведомлений:",
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
        "Измените настройки подписок при необходимости:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    log_task = asyncio.create_task(log_worker())
    
    async def start_telegram_bot():
        await asyncio.sleep(2.0)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "ВАШ_ТОКЕН_ОТ_BOTFATHER":
            try:
                req = HTTPXRequest(connect_timeout=15.0, read_timeout=15.0)
                tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(req).build()
                tg_app.add_handler(CommandHandler("start", start_telegram_cmd))
                tg_app.add_handler(CommandHandler("settings", settings_telegram_cmd))
                tg_app.add_handler(CallbackQueryHandler(category_callback_handler, pattern="^sub_"))
                
                await tg_app.initialize()
                await tg_app.start()
                await tg_app.updater.start_polling(drop_pending_updates=True)
                db_log("🤖 Telegram-бот успешно запущен в фоне!")
            except Exception as e:
                db_log(f"⚠️ Ошибка запуска бота: {e}")

    bot_task = asyncio.create_task(start_telegram_bot())

    yield
    
    log_task.cancel()
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- ЭНДПОИНТ ДЛЯ RAILWAY HEALTHCHECK ---
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# --- АДАПТИВНЫЕ СТИЛИ И ШАБЛОНЫ ---
LIGHT_THEME_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
    :root {
        --bg-color: #f8fafc; --card-bg: #ffffff; --text-primary: #0f172a;
        --text-secondary: #64748b; --border-color: #f1f5f9; --accent-blue: #3b82f6;
        --accent-green: #10b981; --accent-red: #f43f5e; --accent-gold: #f59e0b;
        --radius-card: 20px; --radius-btn: 14px; --shadow-soft: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.05);
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: var(--bg-color); color: var(--text-primary); margin: 0; padding: 16px 12px; overflow-x: hidden; }
    .container { max-width: 1200px; margin: 0 auto; width: 100%; }
    .card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--radius-card); padding: 20px; margin-bottom: 16px; box-shadow: var(--shadow-soft); }
    
    h1 { font-size: 1.65rem; font-weight: 800; margin: 0; letter-spacing: -0.02em; }
    
    .btn { padding: 10px 18px; border-radius: var(--radius-btn); border: none; font-weight: 600; font-size: 0.9rem; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 8px; text-decoration: none; color: #fff; transition: all 0.2s ease; }
    .btn:active { transform: scale(0.97); }
    .btn-blue { background-color: var(--accent-blue); box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }
    .btn-outline { background: var(--card-bg); border: 1px solid var(--border-color); color: var(--text-primary); box-shadow: 0 2px 4px rgba(0,0,0,0.02); }
    .btn-outline.active { background: var(--accent-blue); color: #fff; border-color: var(--accent-blue); box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }
    
    .nav-tabs { display: flex; gap: 10px; margin-bottom: 16px; overflow-x: auto; padding-bottom: 6px; -webkit-overflow-scrolling: touch; }
    .nav-tabs::-webkit-scrollbar { display: none; }
    .tab-item { padding: 10px 20px; border-radius: var(--radius-btn); background: var(--card-bg); border: 1px solid var(--border-color); color: var(--text-secondary); text-decoration: none; font-weight: 700; white-space: nowrap; font-size: 0.9rem; box-shadow: var(--shadow-soft); }
    .tab-item.active { background: var(--accent-blue); color: #ffffff; border-color: var(--accent-blue); box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }
    
    .table-container { background: var(--card-bg); border-radius: var(--radius-card); border: 1px solid var(--border-color); overflow-x: auto; -webkit-overflow-scrolling: touch; box-shadow: var(--shadow-soft); }
    table { width: 100%; border-collapse: collapse; text-align: left; min-width: 650px; }
    th { background: #fafafa; padding: 14px 16px; color: var(--text-secondary); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-color); }
    td { padding: 16px; border-bottom: 1px solid var(--border-color); vertical-align: middle; font-size: 0.95rem; }
    tr:last-child td { border-bottom: none; }
    
    .discount-pill { background: #fff1f2; color: var(--accent-red); font-weight: 800; padding: 5px 10px; border-radius: 8px; font-size: 0.85rem; display: inline-block; }
    .fav-btn { background: none; border: none; font-size: 1.3rem; cursor: pointer; padding: 4px; transition: transform 0.2s; }
    .fav-btn:active { transform: scale(1.3); }
    
    .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(15, 23, 42, 0.6); backdrop-filter: blur(4px); display: none; justify-content: center; align-items: center; z-index: 1000; padding: 16px; }
    .modal-content { background: #fff; padding: 24px; border-radius: var(--radius-card); max-width: 650px; width: 100%; max-height: 90vh; overflow-y: auto; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1); }
    .terminal-box { background: #0f172a; color: #38bdf8; border-radius: 14px; padding: 16px; height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.85rem; line-height: 1.5; }
    
    .counter-badge {
        background: #eff6ff; color: var(--accent-blue); border: 1px solid #dbeafe;
        padding: 6px 14px; border-radius: 20px; font-weight: 700; font-size: 0.85rem;
        display: inline-flex; align-items: center; gap: 6px;
    }

    @media (max-width: 768px) {
        body { padding: 8px; }
        h1 { font-size: 1.4rem; }
        .card { padding: 14px; border-radius: 16px; margin-bottom: 12px; }
        .controls-grid { flex-direction: column; align-items: stretch !important; }
        #searchInput, #sortSelect, #filterSuperBtn { width: 100% !important; text-align: center; }
        td, th { padding: 12px 10px; font-size: 0.85rem; }
        .btn { padding: 8px 14px; font-size: 0.85rem; }
    }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
"""

USER_TEMPLATE = LIGHT_THEME_CSS + """
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Витрина Скидок</title></head>
<body>
    <div class="container">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; gap: 12px;">
            <div>
                <h1>🔥 Витрина Скидок</h1>
                <div style="color: var(--text-secondary); font-size: 0.85rem; margin-top: 4px; margin-bottom: 8px;">🕒 Обновлено: {{ last_update }}</div>
                <div class="counter-badge">📦 Товаров в каталоге: {{ total_count }}</div>
            </div>
            <div>
                <button class="btn btn-outline" id="favFilterBtn" onclick="toggleFavFilter()" style="border-radius: 20px;">❤️ Избранное (<span id="favCount">0</span>)</button>
            </div>
        </div>

        <div class="nav-tabs">
            {% for key, store in stores.items() %}
            <a href="/?category={{ key }}" class="tab-item {% if current_category == key %}active{% endif %}">{{ store.name }}</a>
            {% endfor %}
        </div>

        <div class="card controls-grid" style="padding: 14px; display: flex; gap: 10px; align-items: center;">
            <input type="text" id="searchInput" class="btn btn-outline" style="flex-grow: 1; text-align: left; justify-content: flex-start; cursor: text;" placeholder="🔍 Поиск по названию товара..." onkeyup="applyFilters()">
            <button class="btn btn-outline" id="filterSuperBtn" onclick="toggleSuperDiscount()">💥 Скидки >30%</button>
            
            <select id="sortSelect" class="btn btn-outline" onchange="applyFilters()" style="cursor: pointer;">
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
                        <th style="width: 40px; text-align: center;">❤️</th>
                        <th>Наименование товара</th>
                        <th style="width: 130px;">Цена</th>
                        <th style="text-align: center; width: 110px;">Скидка</th>
                        <th style="text-align: right; width: 170px;">Действия</th>
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
                        
                        <td style="text-align: center;">
                            <button class="fav-btn" onclick="toggleFav('{{ p.link }}')">🤍</button>
                        </td>
                        <td>
                            <strong style="cursor: pointer; color: var(--text-primary); line-height: 1.4;" onclick="openChart('{{ p.link }}', '{{ p.title|e }}')">{{ p.title }}</strong>
                        </td>
                        <td style="font-weight: 800; color: var(--accent-blue); white-space: nowrap;">{{ p.price }}</td>
                        <td style="text-align: center;">
                            {% if p.discount != '—' %}<span class="discount-pill">{{ p.discount }}</span>{% else %}<span style="color: var(--text-secondary);">—</span>{% endif %}
                        </td>
                        <td style="text-align: right;">
                            <div style="display: flex; gap: 6px; justify-content: flex-end;">
                                <button class="btn btn-outline" style="padding: 6px 10px;" onclick="openChart('{{ p.link }}', '{{ p.title|e }}')" title="График цен">📈</button>
                                <button class="btn btn-outline" style="padding: 6px 10px;" onclick="copyPost(this)" title="Копировать пост">📋</button>
                                <a href="{{ p.link }}" target="_blank" class="btn btn-blue" style="padding: 6px 14px; font-size: 0.85rem;">Купить ↗</a>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <div class="card" style="text-align: center; padding: 50px 20px;">
            <h3 style="margin-top: 0; margin-bottom: 8px;">Товары не найдены</h3>
            <p style="color: var(--text-secondary); margin: 0;">Запустите парсинг через админ-панель, чтобы наполнить витрину.</p>
        </div>
        {% endif %}
    </div>

    <div class="modal-overlay" id="chartModal" onclick="if(event.target === this) closeChart();">
        <div class="modal-content">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <h3 id="modalTitle" style="margin: 0; font-size: 1.05rem; padding-right: 12px; font-weight: 700;">История цен</h3>
                <button class="btn btn-outline" style="padding: 6px 12px; border-radius: 50%; width: 32px; height: 32px;" onclick="closeChart()">✕</button>
            </div>
            <div style="position: relative; height: 260px; width: 100%;">
                <canvas id="priceChartCanvas"></canvas>
            </div>
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
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        borderWidth: 3,
                        fill: true,
                        tension: 0.35,
                        pointRadius: 4,
                        pointBackgroundColor: '#3b82f6'
                    }]
                },
                options: { 
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } }
                }
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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Панель Управления</title></head>
<body>
    <div class="container">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <h1>⚙️ Админ-панель</h1>
            <a href="/" class="btn btn-outline">← На витрину</a>
        </div>

        <!-- УПРАВЛЕНИЕ COOKIES -->
        <div class="card" style="border: 1px solid var(--accent-blue);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <h2 style="margin: 0; font-size: 1.15rem;">🔑 Управление сессией (Cookies)</h2>
                <button type="button" class="btn btn-outline" onclick="testCookies()" style="padding: 6px 12px; font-size: 0.85rem;">🔍 Проверить куки</button>
            </div>
            <p style="color: var(--text-secondary); font-size: 0.9rem; margin-top: 0; margin-bottom: 14px;">
                Обновите файл сессии, если Яндекс запросил капчу или сбросил авторизацию.
            </p>
            <div id="cookieTestResult" style="display: none; padding: 10px 14px; border-radius: 12px; margin-bottom: 14px; font-weight: 600; font-size: 0.9rem;"></div>
            
            <form method="POST" action="{{ admin_url }}/update-cookies" enctype="multipart/form-data" style="display: flex; flex-direction: column; gap: 10px;">
                <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                    <input type="file" name="cookie_file" accept=".json" class="btn btn-outline" style="flex: 1; padding: 8px; cursor: pointer;">
                    <button type="submit" class="btn btn-blue" style="padding: 10px 18px;">Загрузить файл</button>
                </div>
            </form>
        </div>

        <div class="card">
            <h2 style="margin-top: 0; font-size: 1.15rem; margin-bottom: 12px;">🚀 Запуск сбора данных</h2>
            <form method="POST" action="{{ admin_url }}/start" style="display: flex; flex-direction: column; gap: 14px;">
                <div>
                    <label style="font-weight: 700; display: block; margin-bottom: 6px; color: var(--text-secondary); font-size: 0.85rem;">Категория:</label>
                    <select name="target_store" class="btn btn-outline" style="width: 100%; text-align: left; padding: 12px; cursor: pointer;">
                        <option value="all">🌐 Все магазины по очереди</option>
                        {% for key, store in stores.items() %}
                        <option value="{{ key }}">🏬 {{ store.name }}</option>
                        {% endfor %}
                    </select>
                </div>

                <div style="display: flex; flex-direction: column; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 10px; background: var(--bg-color); padding: 10px 14px; border-radius: 12px;">
                        <input type="checkbox" id="send_telegram" name="send_telegram" value="true" checked style="width: 18px; height: 18px; accent-color: var(--accent-blue); cursor: pointer;">
                        <label for="send_telegram" style="font-weight: 600; font-size: 0.9rem; cursor: pointer;">📢 Отправлять уведомления о новых товарах в Telegram</label>
                    </div>

                    <div style="display: flex; align-items: center; gap: 10px; background: #fffbeb; border: 1px solid #fef3c7; padding: 10px 14px; border-radius: 12px;">
                        <input type="checkbox" id="market_search" name="market_search" value="true" style="width: 18px; height: 18px; accent-color: var(--accent-gold); cursor: pointer;">
                        <label for="market_search" style="font-weight: 600; font-size: 0.9rem; color: #b45309; cursor: pointer;">🔍 Глубокий анализ: сверять цены с общим поиском Маркета</label>
                    </div>
                </div>

                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    <button type="submit" name="mode" value="fast" class="btn btn-blue action-btn" style="flex: 1; min-width: 140px; padding: 12px;" {% if state.is_active %}disabled{% endif %}>⚡ Быстрый сбор</button>
                    <button type="submit" name="mode" value="full" class="btn btn-blue action-btn" style="flex: 1; min-width: 140px; background: var(--accent-green); box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3); padding: 12px;" {% if state.is_active %}disabled{% endif %}>🔎 Со скидками</button>
                </div>
            </form>
        </div>

        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <h2 style="margin: 0; font-size: 1.15rem;">📊 Мониторинг</h2>
                <button type="button" class="btn btn-blue" id="stopButton" onclick="forceStopParser()" style="background: var(--accent-red); display: none; padding: 6px 14px; font-size: 0.85rem;">🛑 Остановить парсинг</button>
            </div>
            
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; font-size: 0.9rem;">
                <div style="background: var(--bg-color); padding: 10px 14px; border-radius: 12px;">
                    <span style="color: var(--text-secondary); font-size: 0.75rem; text-transform: uppercase; font-weight: 700;">Статус:</span>
                    <div id="statusBadge" style="font-weight: 800; margin-top: 4px;"></div>
                </div>
                <div style="background: var(--bg-color); padding: 10px 14px; border-radius: 12px;">
                    <span style="color: var(--text-secondary); font-size: 0.75rem; text-transform: uppercase; font-weight: 700;">Магазин:</span>
                    <div id="currentStore" style="font-weight: 800; margin-top: 4px; color: var(--accent-blue);">{{ state.current_store }}</div>
                </div>
                <div style="background: var(--bg-color); padding: 10px 14px; border-radius: 12px; grid-column: span 2;">
                    <span style="color: var(--text-secondary); font-size: 0.75rem; text-transform: uppercase; font-weight: 700;">Прогресс:</span>
                    <div id="progressText" style="font-weight: 800; margin-top: 4px;">{{ state.progress_text }}</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2 style="margin-top: 0; font-size: 1.15rem; margin-bottom: 12px;">💻 Консоль логов</h2>
            <div class="terminal-box" id="terminal"></div>
        </div>
    </div>

    <!-- ВСПЛЫВАЮЩЕЕ ОКНО ПРИ ЗАВИСАНИИ -->
    <div class="modal-overlay" id="freezeModal" style="display: none;">
        <div class="modal-content" style="border: 2px solid var(--accent-red); text-align: center;">
            <h3 style="color: var(--accent-red); margin-top: 0;">⚠️ Скрипт долго не отвечает (завис?)</h3>
            <p style="color: var(--text-secondary); font-size: 0.95rem; margin-bottom: 20px;">
                Похоже, процесс парсинга застрял на одном месте более 3 минут. Вы можете принудительно остановить его прямо сейчас — все товары, которые скрипт <strong>уже успел спарсить и обработать</strong>, полностью сохранены в базе данных и витрине!
            </p>
            <div style="display: flex; gap: 10px; justify-content: center;">
                <button class="btn btn-blue" onclick="closeFreezeModal()" style="background: var(--text-secondary);">Продолжить ждать</button>
                <button class="btn btn-blue" onclick="forceStopParser()" style="background: var(--accent-red);">🛑 Сохранить то что есть и выйти</button>
            </div>
        </div>
    </div>

    <script>
        let freezeModalShown = false;

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

                const stopBtn = document.getElementById('stopButton');
                stopBtn.style.display = data.is_active ? 'inline-flex' : 'none';

                const term = document.getElementById('terminal');
                term.innerHTML = data.logs.map(log => `<div>${log}</div>`).join('');
                term.scrollTop = term.scrollHeight;

                if (data.is_active && data.seconds_inactive > 180 && !freezeModalShown) {
                    document.getElementById('freezeModal').style.display = 'flex';
                    freezeModalShown = true;
                } else if (!data.is_active) {
                    freezeModalShown = false;
                }
            } catch (e) {}
        }

        async function testCookies() {
            const resDiv = document.getElementById('cookieTestResult');
            resDiv.style.display = 'block';
            resDiv.style.background = '#eff6ff';
            resDiv.style.color = '#3b82f6';
            resDiv.textContent = '⏳ Проверяем куки и соединение с Яндексом...';

            try {
                const resp = await fetch('{{ admin_url }}/api/test-cookies', { method: 'POST' });
                const data = await resp.json();
                
                resDiv.textContent = data.message;
                if (data.status === 'success') {
                    resDiv.style.background = '#ecfdf5';
                    resDiv.style.color = '#10b981';
                } else {
                    resDiv.style.background = '#fff1f2';
                    resDiv.style.color = '#f43f5e';
                }
            } catch (e) {
                resDiv.style.background = '#fff1f2';
                resDiv.style.color = '#f43f5e';
                resDiv.textContent = '❌ Ошибка выполнения запроса проверки.';
            }
        }

        function closeFreezeModal() {
            document.getElementById('freezeModal').style.display = 'none';
        }

        async function forceStopParser() {
            await fetch('{{ admin_url }}/stop', { method: 'POST' });
            document.getElementById('freezeModal').style.display = 'none';
            location.reload();
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
    now_ts = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else datetime.now().timestamp()
    state_copy["seconds_inactive"] = int(now_ts - PARSER_STATE["last_activity"]) if PARSER_STATE["is_active"] else 0
    state_copy["logs"] = await asyncio.to_thread(get_db_logs)
    return JSONResponse(content=state_copy)

@app.post(f"{ADMIN_SECRET_URL}/api/test-cookies")
async def api_test_cookies(request: Request, username: str = Depends(authenticate_admin)):
    res = await test_cookies_validity()
    return JSONResponse(content=res)

@app.post(f"{ADMIN_SECRET_URL}/update-cookies")
async def update_cookies_file(request: Request, cookie_file: UploadFile = File(...), username: str = Depends(authenticate_admin)):
    try:
        content = await cookie_file.read()
        parsed_json = json.loads(content.decode("utf-8"))
        
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(parsed_json, f, ensure_ascii=False, indent=2)
            
        db_log("🔑 Файл cookies.json успешно обновлен через админ-панель.")
    except Exception as e:
        db_log(f"❌ Ошибка загрузки cookies.json: {e}")
        
    return RedirectResponse(url=ADMIN_SECRET_URL, status_code=303)

@app.post(f"{ADMIN_SECRET_URL}/start")
async def start_parsing_job(
    request: Request,
    target_store: str = Form(...), 
    mode: str = Form(...), 
    send_telegram: str = Form(default=None),
    market_search: str = Form(default=None),
    username: str = Depends(authenticate_admin)
):
    if not PARSER_STATE["is_active"]:
        with_discounts = (mode == "full")
        send_tg = (send_telegram == "true")
        market_search_mode = (market_search == "true")
        asyncio.create_task(execute_parsing_task(
            target_store=target_store, 
            with_discounts=with_discounts, 
            send_tg=send_tg, 
            market_search_mode=market_search_mode
        ))
        return RedirectResponse(url=ADMIN_SECRET_URL, status_code=303)

@app.post(f"{ADMIN_SECRET_URL}/stop")
async def stop_parsing_job(request: Request, username: str = Depends(authenticate_admin)):
    PARSER_STATE["forced_stop"] = True
    db_log("🛑 Пользователь запросил экстренную остановку парсера.")
    return {"status": "stopping"}

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
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM products WHERE category_id = ? ORDER BY discount_num DESC", (category,))
            rows = cursor.fetchall()

            cursor.execute("SELECT COUNT(*) FROM products WHERE category_id = ?", (category,))
            total_count_row = cursor.fetchone()
            total_count = total_count_row[0] if total_count_row else 0

            cursor.execute("SELECT last_updated FROM products ORDER BY last_updated DESC LIMIT 1")
            last_upd_row = cursor.fetchone()
            last_update = last_upd_row[0] if last_upd_row else "Еще не обновлялось"

            conn.close()
            return [dict(row) for row in rows], total_count, last_update
        except Exception as e:
            db_log(f"⚠️ Ошибка при чтении витрины из БД: {e}")
            return [], 0, "Ошибка базы"

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