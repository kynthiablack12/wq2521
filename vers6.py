import os
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Конфигурация путей и базы данных
# На Railway папка /data монтируется как Persistent Volume. Локально используем текущую директорию.
DB_DIR = "/data" if os.path.exists("/data") else "."
DB_PATH = os.path.join(DB_DIR, "products.db")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Таблица товаров
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT
        )
    """)
    # Таблица истории цен
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            old_price REAL,
            new_price REAL,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Таблица подписчиков (для бота)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    # Добавим тестовый товар, если база пустая
    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO products (title, price, description) VALUES (?, ?, ?)", 
                       ("Цифровой купон Magnitoland", 500.0, "Моментальная выдача промокода"))
        conn.commit()
    conn.close()
    print(f"[БД] База данных успешно инициализирована по пути: {DB_PATH}")

# Инициализируем БД при старте модуля
init_db()

# --- TELEGRAM BOT LOGIC ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        "👋 Привет! Добро пожаловать в бот-магазин.\n"
        "Нажмите на кнопку ниже, чтобы открыть каталог товаров.",
        reply_markup={
            "inline_keyboard": [[
                {"text": "🚀 Открыть каталог", "web_app": {"url": "https://ваш-домен-на-railway.up.railway.app/"}}
            ]]
        }
    )

async def run_telegram_bot():
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[Telegram] Токен бота не задан, фоновый бот пропущен.")
        return
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    
    print("[Telegram] 🤖 Telegram-бот запущен!")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

# --- FASTAPI LIFESPAN & APP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем телеграм-бота в фоновой задаче при старте FastAPI
    bot_task = asyncio.create_task(run_telegram_bot())
    yield
    # Отменяем задачу при выключении
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)

# HTML-шаблон главной страницы в стиле Telegram Mini App
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Magnitoland — Биржа цифровых товаров</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --bg-color: #f4f6f8;
            --card-bg: #ffffff;
            --text-main: #111827;
            --text-secondary: #6b7280;
            --accent: #2563eb;
            --shadow: 0 4px 20px rgba(0, 0, 0, 0.04);
            --radius: 20px;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 0;
            padding-bottom: 80px;
        }
        .container {
            max-width: 480px;
            margin: 0 auto;
            padding: 16px;
        }
        /* Шапка */
        .header {
            text-align: center;
            margin-top: 10px;
            margin-bottom: 20px;
        }
        .logo-title {
            font-size: 24px;
            font-weight: 800;
            color: #1f2937;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        .logo-title span { color: #0284c7; }
        .logo-subtitle {
            font-size: 13px;
            color: var(--text-secondary);
            margin-top: 4px;
        }
        /* Верхняя панель профиля */
        .top-panel {
            background: var(--card-bg);
            border-radius: var(--radius);
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: var(--shadow);
            margin-bottom: 16px;
        }
        .profile-info {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            font-weight: 600;
        }
        .avatar-placeholder {
            width: 28px;
            height: 28px;
            background: #e2e8f0;
            border-radius: 50%;
        }
        .notifications {
            background: #fee2e2;
            color: #ef4444;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 700;
        }
        /* Карточки баланса */
        .balance-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 16px;
        }
        .balance-card {
            background: var(--card-bg);
            border-radius: var(--radius);
            padding: 16px;
            box-shadow: var(--shadow);
        }
        .balance-title {
            font-size: 11px;
            text-transform: uppercase;
            color: var(--text-secondary);
            font-weight: 700;
            letter-spacing: 0.5px;
        }
        .balance-value {
            font-size: 18px;
            font-weight: 800;
            margin-top: 6px;
        }
        .fee-info {
            background: var(--card-bg);
            border-radius: var(--radius);
            padding: 12px 16px;
            font-size: 12px;
            color: var(--text-secondary);
            margin-bottom: 20px;
            box-shadow: var(--shadow);
        }
        .fee-info span { color: var(--text-main); font-weight: 600; }
        
        /* Сетка разделов */
        .section-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 24px;
        }
        .menu-card {
            background: var(--card-bg);
            border-radius: var(--radius);
            padding: 16px;
            box-shadow: var(--shadow);
            cursor: pointer;
            transition: transform 0.1s ease;
            text-decoration: none;
            color: inherit;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 90px;
        }
        .menu-card:active { transform: scale(0.97); }
        .menu-card-title {
            font-size: 15px;
            font-weight: 800;
            margin-bottom: 4px;
        }
        .menu-card-desc {
            font-size: 12px;
            color: var(--text-secondary);
            line-height: 1.3;
        }
        
        /* Список товаров (Каталог) */
        .catalog-title {
            font-size: 18px;
            font-weight: 800;
            margin-bottom: 12px;
        }
        .product-card {
            background: var(--card-bg);
            border-radius: var(--radius);
            padding: 16px;
            margin-bottom: 12px;
            box-shadow: var(--shadow);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .product-name { font-weight: 700; font-size: 15px; }
        .product-price { font-weight: 800; color: #059669; font-size: 16px; margin-top: 4px; }
        
        /* Нижняя навигация */
        .nav-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: var(--card-bg);
            display: flex;
            justify-content: space-around;
            padding: 10px 0;
            box-shadow: 0 -4px 20px rgba(0,0,0,0.05);
            border-top-left-radius: 20px;
            border-top-right-radius: 20px;
            z-index: 100;
        }
        .nav-item {
            text-align: center;
            font-size: 11px;
            color: var(--text-secondary);
            text-decoration: none;
            font-weight: 600;
        }
        .nav-item.active { color: #ef4444; }
        .nav-item div { font-size: 18px; margin-bottom: 2px; }
    </style>
</head>
<body>
    <div class="container">
        <!-- Шапка -->
        <div class="header">
            <div class="logo-title">💎 <span>Magnitoland</span></div>
            <div class="logo-subtitle">Биржа цифровых товаров — купоны, промокоды, чеки</div>
        </div>

        <!-- Профиль и уведомления -->
        <div class="top-panel">
            <div class="profile-info">
                <div class="avatar-placeholder"></div>
                <span>Профиль</span>
            </div>
            <div class="notifications">Уведомления 7</div>
        </div>

        <!-- Балансы -->
        <div class="balance-grid">
            <div class="balance-card">
                <div class="balance-title">Баланс</div>
                <div class="balance-value">5,00 ₽</div>
            </div>
            <div class="balance-card">
                <div class="balance-title">Бонусы</div>
                <div class="balance-value">0,00 💎</div>
            </div>
        </div>

        <div class="fee-info">
            Комиссия за покупки: <span>10%</span><br>
            💎 Комиссия полностью оплачивается бонусами
        </div>

        <!-- Меню плитками -->
        <div class="section-grid">
            <div class="menu-card">
                <div>
                    <div class="menu-card-title">Чёрный список</div>
                    <div class="menu-card-desc">Кому запрещено покупать Ваши лоты</div>
                </div>
            </div>
            <div class="menu-card">
                <div>
                    <div class="menu-card-title">Розыгрыши</div>
                    <div class="menu-card-desc">Участвуйте и выигрывайте призы</div>
                </div>
            </div>
            <div class="menu-card">
                <div>
                    <div class="menu-card-title">Промокоды</div>
                    <div class="menu-card-desc">Скидки в популярных сервисах</div>
                </div>
            </div>
            <div class="menu-card">
                <div>
                    <div class="menu-card-title">Прокси</div>
                    <div class="menu-card-desc">Доступ через защищенный VPN</div>
                </div>
            </div>
        </div>

        <!-- Каталог товаров из SQLite -->
        <div class="catalog-title">Доступные товары</div>
        {% for product in products %}
        <div class="product-card">
            <div>
                <div class="product-name">{{ product[1] }}</div>
                <div style="font-size: 12px; color: #6b7280;">{{ product[3] }}</div>
                <div class="product-price">{{ product[2] }} ₽</div>
            </div>
        </div>
        {% endfor %}
    </div>

    <!-- Нижняя панель навигации -->
    <div class="nav-bar">
        <a href="#" class="nav-item active">
            <div>🏠</div>Главная
        </a>
        <a href="#" class="nav-item">
            <div>🔥</div>Лоты
        </a>
        <a href="#" class="nav-item">
            <div>💬</div>Чаты
        </a>
        <a href="#" class="nav-item">
            <div>👥</div>Люди
        </a>
    </div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def read_root():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, price, description FROM products")
    products = cursor.fetchall()
    conn.close()
    
    # Рендерим HTML вручную с подстановкой товаров
    html_content = HTML_TEMPLATE.replace("{% for product in products %}", "")
    html_content = html_content.replace("{% endfor %}", "")
    
    products_html = ""
    for p in products:
        products_html += f"""
        <div class="product-card">
            <div>
                <div class="product-name">{p[1]}</div>
                <div style="font-size: 12px; color: #6b7280;">{p[3]}</div>
                <div class="product-price">{p[2]} ₽</div>
            </div>
        </div>
        """
    
    # Простая замена блока товаров в шаблоне
    if "{% for product in products %}" not in HTML_TEMPLATE:
        pass
    # Интегрируем динамический список в шаблон
    parts = HTML_TEMPLATE.split("{% for product in products %}")
    if len(parts) > 1:
        end_parts = parts[1].split("{% endfor %}")
        final_html = parts[0] + products_html + end_parts[1]
    else:
        final_html = HTML_TEMPLATE
        
    return HTMLResponse(content=final_html)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("vers6:app", host="0.0.0.0", port=port)