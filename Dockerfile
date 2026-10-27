# Dockerfile для Bothost (включите галочку «Использовать собственный Dockerfile»).
# Нужен, чтобы в контейнере был ffmpeg — без него не склеится видео YouTube и не получится mp3.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Папка для базы с привязанными каналами (на Bothost она сохраняется между обновлениями)
RUN mkdir -p /app/data && chmod 777 /app/data

CMD ["python", "bot.py"]
