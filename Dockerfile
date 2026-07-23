FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && apt-get install -y wget gnupg
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium
COPY . .

# ИСПРАВЛЕННАЯ СТРОКА (подставляет PORT из окружения Railway или 8080 по умолчанию):
CMD sh -c "uvicorn vers6:app --host 0.0.0.0 --port ${PORT:-8080}"