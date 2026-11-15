# Dockerfile для Bothost (включите галочку «Использовать собственный Dockerfile»).
# ffmpeg нужен, чтобы склеивать видео YouTube и делать mp3.
# Deno нужен для свежих версий yt-dlp: без него YouTube чаще блокирует скачивание.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno (среда выполнения JavaScript для yt-dlp) — берём готовый файл из официального образа
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Папка для базы с привязанными каналами и cookies (на Bothost она сохраняется между обновлениями)
RUN mkdir -p /app/data && chmod 777 /app/data

CMD ["python", "bot.py"]
