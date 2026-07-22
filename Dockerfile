FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && apt-get install -y wget gnupg
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium
COPY . .
CMD ["uvicorn", "vers6:app", "--host", "0.0.0.0", "--port", "8000"]