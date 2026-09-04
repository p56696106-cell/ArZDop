FROM python:3.11-slim

WORKDIR /app

# Установка зависимостей системы
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копирование зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование приложения
COPY . .

# Порт для Render
EXPOSE 8000

# Запуск сервера
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
