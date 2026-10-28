"""
Telegram-бот: скачивает видео / фото / музыку из TikTok, YouTube и Яндекс Музыки.

Как работает:
  1. Пользователь кидает ссылку.
  2. Бот определяет сервис и что там лежит, и показывает кнопки (например, разрешения для YouTube).
  3. После нажатия бот скачивает файл и присылает его пользователю в личку (без подписей).
     Если у пользователя привязан канал, под результатом есть кнопка «Отправить в канал» —
     по ней бот копирует этот же результат в канал, тоже без подписей.
     У каждого пользователя канал свой.

Кроме того: если прислать боту аудиофайл с музыкой, он сразу отправит его в привязанный канал.

Запуск:  python bot.py
"""

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from dotenv import load_dotenv
from yandex_music import ClientAsync

# ======================================================================
#                              НАСТРОЙКИ
# ======================================================================
load_dotenv()  # читает файл .env рядом с bot.py (на хостинге не нужен — там переменные задаются в панели)

BOT_TOKEN = os.environ["BOT_TOKEN"]                  # токен бота (на Bothost подставляется автоматически)
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "")         # токен Яндекс Музыки (нужен только для неё)
YT_COOKIES = os.getenv("YT_COOKIES", "")             # путь к cookies.txt для YouTube (необязательно)
MAX_MB = int(os.getenv("MAX_FILE_MB", "50"))         # лимит Telegram для ботов — 50 МБ
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))   # сколько скачиваний одновременно (меньше = меньше памяти)

# Кому разрешено пользоваться ботом (id через запятую). Пусто = всем.
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

# Папка для базы данных. На Bothost это /app/data — только она сохраняется между обновлениями бота.
# Если запускаете у себя на компьютере — база ляжет рядом с bot.py.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data" if Path("/app").is_dir() else "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

URL_RE = re.compile(r"https?://\S+")

router = Router()
download_slots = asyncio.Semaphore(MAX_PARALLEL)

# Запросы, которые ждут нажатия кнопки: token -> данные о ссылке.
# (В callback_data кнопки нельзя положить длинную ссылку, поэтому кладём короткий token.)
PENDING: dict[str, dict] = {}

# Уже отправленные пользователю результаты: token -> id сообщений (нужны для кнопки «Отправить в канал»)
SENT: dict[str, dict] = {}

# Пользователи, которые сейчас должны переслать пост из канала (для привязки)
WAITING_FORWARD: set[int] = set()


def remember(store: dict, token: str, value: dict, limit: int = 500):
    """Кладёт запись в память и стирает самые старые, чтобы память не росла."""
    store[token] = value
    while len(store) > limit:
        store.pop(next(iter(store)))


# ======================================================================
#          БАЗА ДАННЫХ: какой канал привязан к какому пользователю
# ======================================================================
def db_exec(sql: str, params=(), fetch: bool = False):
    """Выполняет SQL-запрос к файлу bot.db."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows


def db_init():
    """Создаёт таблицу при первом запуске."""
    db_exec(
        """CREATE TABLE IF NOT EXISTS users (
               user_id       INTEGER PRIMARY KEY,
               channel_id    INTEGER,
               channel_title TEXT
           )"""
    )


def db_get(user_id: int) -> dict | None:
    """Возвращает настройки пользователя (привязанный канал) или None."""
    rows = db_exec("SELECT * FROM users WHERE user_id=?", (user_id,), fetch=True)
    return dict(rows[0]) if rows else None


def db_set_channel(user_id: int, channel_id: int, title: str):
    """Привязывает канал к пользователю (у каждого пользователя свой)."""
    db_exec(
        """INSERT INTO users (user_id, channel_id, channel_title)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               channel_id=excluded.channel_id,
               channel_title=excluded.channel_title""",
        (user_id, channel_id, title),
    )


def db_clear_channel(user_id: int):
    """Отвязывает канал."""
    db_exec("UPDATE users SET channel_id=NULL, channel_title=NULL WHERE user_id=?", (user_id,))


def has_channel(user_id: int) -> bool:
    u = db_get(user_id)
    return bool(u and u["channel_id"])


# ======================================================================
#                           ВСПОМОГАТЕЛЬНОЕ
# ======================================================================
def is_allowed(user_id: int) -> bool:
    return not ALLOWED or user_id in ALLOWED


def detect_platform(url: str) -> str | None:
    """ВАЖНО: по домену определяет сервис: youtube / tiktok / yandex."""
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if re.search(r"music\.yandex\.", host):
        return "yandex"
    return None


def safe_name(text: str) -> str:
    """Убирает из названия символы, которые нельзя в имени файла."""
    return re.sub(r'[\\/:*?"<>|\n\r]', "", text or "file").strip()[:80] or "file"


def check_size(path: Path):
    """Проверяет, что файл влезает в лимит Telegram."""
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise RuntimeError(
            f"Файл весит {size / 1024 / 1024:.0f} МБ, а Telegram разрешает боту максимум {MAX_MB} МБ. "
            "Выберите качество пониже."
        )


async def download_file(url: str, dest: Path):
    """Скачивает файл по прямой ссылке на диск."""
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    f.write(chunk)


# ======================================================================
#                              TIKTOK
#  Используется бесплатный сервис tikwm.com: он отдаёт видео без водяного
#  знака, музыку и фото из фото-постов (yt-dlp фото-посты TikTok не умеет).
# ======================================================================
async def tiktok_info(url: str) -> dict:
    """Узнаёт, что в посте TikTok: фото, видео, музыка."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1}) as resp:
            data = await resp.json(content_type=None)

    if data.get("code") != 0:
        raise RuntimeError(data.get("msg") or "TikTok не отдал данные по этой ссылке")

    d = data["data"]

    def absolute(u: str | None) -> str | None:
        if u and u.startswith("/"):
            return "https://www.tikwm.com" + u
        return u

    return {
        "title": d.get("title") or "TikTok",
        "images": [absolute(u) for u in (d.get("images") or [])],   # не пусто => фото-пост
        "video": absolute(d.get("hdplay") or d.get("play")),
        "music": absolute(d.get("music")),
    }


# ======================================================================
#                              YOUTUBE (yt-dlp)
# ======================================================================
def ydl_base_opts() -> dict:
    """Общие настройки yt-dlp."""
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if YT_COOKIES:
        opts["cookiefile"] = YT_COOKIES
    return opts


def youtube_info_sync(url: str) -> dict:
    """
    ВАЖНО: получает информацию о видео и список доступных разрешений
    с примерным размером файла для каждого.
    """
    with yt_dlp.YoutubeDL(ydl_base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats") or []

    def fsize(f):
        return f.get("filesize") or f.get("filesize_approx") or 0

    # самый большой аудиопоток — его размер прибавляем к видео без звука
    best_audio = max(
        (fsize(f) for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")),
        default=0,
    )

    sizes: dict[int, int] = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = fsize(f)
        if f.get("acodec") in (None, "none"):
            size += best_audio
        sizes[h] = max(sizes.get(h, 0), size)

    if not sizes:
        raise RuntimeError("Не нашёл доступных форматов (возможно, это стрим или видео закрыто)")

    return {"title": info.get("title") or "YouTube", "sizes": dict(sorted(sizes.items()))}


def youtube_download_sync(url: str, workdir: Path, mode: str, height: int | None = None):
    """
    ВАЖНО: скачивает видео в нужном разрешении (mode="video")
    или только звук в mp3 (mode="audio"). Для этого нужен установленный ffmpeg.
    """
    opts = ydl_base_opts() | {"outtmpl": str(workdir / "%(id)s.%(ext)s")}
    if mode == "audio":
        opts |= {
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }
    else:
        # пробуем mp4/h264 (нормально играет в Telegram), потом — всё остальное
        opts |= {
            "format": (
                f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]"
                f"/b[height<={height}][ext=mp4]"
                f"/bv*[height<={height}]+ba"
                f"/b[height<={height}]"
            ),
            "merge_output_format": "mp4",
        }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [p for p in workdir.iterdir() if p.suffix not in (".part", ".ytdl")]
    return max(files, key=lambda p: p.stat().st_size), info


# ======================================================================
#                              ЯНДЕКС МУЗЫКА
# ======================================================================
yandex_client: ClientAsync | None = None


async def get_yandex() -> ClientAsync:
    """Создаёт клиент Яндекс Музыки один раз и переиспользует."""
    global yandex_client
    if yandex_client is None:
        if not YANDEX_TOKEN:
            raise RuntimeError("Для Яндекс Музыки не задан YANDEX_TOKEN")
        yandex_client = await ClientAsync(YANDEX_TOKEN).init()
    return yandex_client


def parse_yandex_track(url: str) -> str | None:
    """Достаёт id трека (и альбома) из ссылки. Альбомы/плейлисты не поддерживаются."""
    track = re.search(r"track/(\d+)", url)
    if not track:
        return None
    album = re.search(r"album/(\d+)", url)
    return f"{track.group(1)}:{album.group(1)}" if album else track.group(1)


async def yandex_fetch_track(key: str):
    client = await get_yandex()
    tracks = await client.tracks([key])
    if not tracks:
        raise RuntimeError("Трек не найден")
    return tracks[0]


async def yandex_download(key: str, workdir: Path):
    """ВАЖНО: скачивает трек Яндекс Музыки в mp3."""
    track = await yandex_fetch_track(key)
    path = workdir / f"{track.id}.mp3"
    await track.download_async(str(path))
    return path, track.title, ", ".join(track.artists_name())


# ======================================================================
#              ОТПРАВКА РЕЗУЛЬТАТА ПОЛЬЗОВАТЕЛЮ (сердце бота)
# ======================================================================
async def deliver(bot: Bot, req: dict, kind: str, workdir: Path, chat_id: int) -> list[int]:
    """
    ВАЖНО: скачивает выбранное медиа и отправляет в личку пользователя БЕЗ подписей.
    Возвращает id отправленных сообщений — по ним потом работает кнопка «Отправить в канал».
    kind — что выбрал пользователь: photo / video / music / a (аудио YouTube) / v720 (видео 720p) / ym.
    """
    platform, url = req["platform"], req["url"]
    ids: list[int] = []

    # ---------------- TikTok ----------------
    if platform == "tiktok":
        d = req["data"]
        if kind == "photo":
            paths = []
            for i, img_url in enumerate(d["images"]):
                p = workdir / f"{i}.jpg"
                await download_file(img_url, p)
                paths.append(p)
            # Telegram принимает до 10 фото в одном альбоме
            for start in range(0, len(paths), 10):
                media = [InputMediaPhoto(media=FSInputFile(p)) for p in paths[start:start + 10]]
                msgs = await bot.send_media_group(chat_id, media)
                ids += [x.message_id for x in msgs]
        elif kind == "video":
            p = workdir / "video.mp4"
            await download_file(d["video"], p)
            check_size(p)
            msg = await bot.send_video(chat_id, FSInputFile(p), supports_streaming=True)
            ids.append(msg.message_id)
        elif kind == "music":
            p = workdir / "music.mp3"
            await download_file(d["music"], p)
            check_size(p)
            msg = await bot.send_audio(chat_id, FSInputFile(p, filename=f"{safe_name(req['title'])}.mp3"))
            ids.append(msg.message_id)

    # ---------------- YouTube ----------------
    elif platform == "youtube":
        if kind == "a":
            path, info = await asyncio.to_thread(youtube_download_sync, url, workdir, "audio")
            check_size(path)
            msg = await bot.send_audio(
                chat_id,
                FSInputFile(path, filename=f"{safe_name(info.get('title'))}.mp3"),
                title=info.get("title"),
                performer=info.get("uploader"),
                duration=int(info.get("duration") or 0) or None,
            )
        else:  # kind вида "v720"
            height = int(kind[1:])
            path, info = await asyncio.to_thread(youtube_download_sync, url, workdir, "video", height)
            check_size(path)
            msg = await bot.send_video(
                chat_id,
                FSInputFile(path),
                supports_streaming=True,
                width=info.get("width"),
                height=info.get("height"),
                duration=int(info.get("duration") or 0) or None,
            )
        ids.append(msg.message_id)

    # ---------------- Яндекс Музыка ----------------
    elif platform == "yandex":
        path, title, artist = await yandex_download(req["data"]["key"], workdir)
        check_size(path)
        msg = await bot.send_audio(
            chat_id,
            FSInputFile(path, filename=f"{safe_name(artist)} - {safe_name(title)}.mp3"),
            title=title,
            performer=artist,
        )
        ids.append(msg.message_id)

    return ids


async def add_channel_button(bot: Bot, chat_id: int, ids: list[int], user_id: int):
    """
    ВАЖНО: добавляет под результат кнопку «Отправить в канал».
    Если сообщение одно — кнопка прямо под ним. Если это альбом из фото
    (к альбому кнопку приделать нельзя) — отдельным сообщением следом.
    """
    token = uuid.uuid4().hex[:8]
    remember(SENT, token, {"user_id": user_id, "chat_id": chat_id, "message_ids": ids})
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Отправить в канал", callback_data=f"ch:{token}")]]
    )
    if len(ids) == 1:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=ids[0], reply_markup=markup)
    else:
        await bot.send_message(chat_id, "Отправить эти фото в канал?", reply_markup=markup)


# ======================================================================
#                      КОМАНДЫ: старт и справка
# ======================================================================
HELP_TEXT = (
    "Пришлите ссылку на TikTok, YouTube или трек Яндекс Музыки — я предложу, что скачать.\n"
    "Результат приходит вам в личку, а под ним будет кнопка «Отправить в канал» "
    "(если канал привязан).\n"
    "Пришлите мне аудиофайл с музыкой — сразу отправлю его в ваш канал, без вопросов.\n\n"
    "Канал:\n"
    "/setchannel — привязать свой канал (бот должен быть админом канала)\n"
    "/mychannel — какой канал привязан\n"
    "/unsetchannel — отвязать канал\n"
    "/id — узнать свой Telegram-id"
)


@router.message(Command("start", "help"))
async def cmd_start(m: Message):
    if not is_allowed(m.from_user.id):
        return await m.answer("У вас нет доступа к этому боту.")
    await m.answer(HELP_TEXT)


@router.message(Command("id"))
async def cmd_id(m: Message):
    """Показывает Telegram-id пользователя (нужен для списка ALLOWED_USERS). Работает для всех."""
    await m.answer(f"Ваш id: {m.from_user.id}")


# ======================================================================
#                 ПРИВЯЗКА КАНАЛА (у каждого пользователя свой)
# ======================================================================
async def bind_channel(m: Message, bot: Bot, chat_ref):
    """
    ВАЖНО: проверяет канал и привязывает его к пользователю.
    Проверки: 1) это канал, 2) бот там админ с правом публикации,
    3) сам пользователь — админ/владелец этого канала (чтобы нельзя было привязать чужой).
    """
    try:
        chat = await bot.get_chat(chat_ref)
        if chat.type != "channel":
            return await m.answer("Это не канал. Нужен именно канал (не группа и не чат).")

        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status != ChatMemberStatus.ADMINISTRATOR or not getattr(me, "can_post_messages", False):
            return await m.answer("Сделайте бота администратором канала с правом публиковать сообщения и повторите.")

        user = await bot.get_chat_member(chat.id, m.from_user.id)
        if user.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            return await m.answer("Привязать можно только канал, где вы администратор.")
    except TelegramAPIError:
        return await m.answer(
            "Не получилось найти канал. Проверьте, что бот добавлен в канал админом, "
            "и что вы указали @username или id канала."
        )

    db_set_channel(m.from_user.id, chat.id, chat.title or str(chat.id))
    WAITING_FORWARD.discard(m.from_user.id)
    await m.answer(f"✅ Канал «{chat.title}» привязан. Под скачанным файлом появится кнопка «Отправить в канал».")


@router.message(Command("setchannel"))
async def cmd_setchannel(m: Message, command: CommandObject, bot: Bot):
    """/setchannel @канал  — или без аргумента: тогда перешлите боту любой пост из канала."""
    if not is_allowed(m.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        WAITING_FORWARD.add(m.from_user.id)
        return await m.answer(
            "Сначала добавьте бота в канал администратором (с правом публиковать).\n"
            "Потом либо напишите: /setchannel @username_канала\n"
            "либо просто перешлите мне любой пост из канала (подходит и для приватных каналов)."
        )
    if arg.lstrip("-").isdigit():
        chat_ref = int(arg)
    else:
        chat_ref = arg if arg.startswith("@") else "@" + arg
    await bind_channel(m, bot, chat_ref)


# Пересланный пост из канала (работает только после /setchannel без аргумента)
@router.message(F.forward_origin, lambda m: m.from_user and m.from_user.id in WAITING_FORWARD)
async def on_forward(m: Message, bot: Bot):
    origin = m.forward_origin
    if getattr(origin, "type", None) != "channel":
        return await m.answer("Это сообщение не из канала. Перешлите пост из канала.")
    await bind_channel(m, bot, origin.chat.id)


@router.message(Command("mychannel"))
async def cmd_mychannel(m: Message):
    u = db_get(m.from_user.id)
    if not u or not u["channel_id"]:
        return await m.answer("Канал не привязан. Команда: /setchannel")
    await m.answer(f"Ваш канал: «{u['channel_title']}»")


@router.message(Command("unsetchannel"))
async def cmd_unset(m: Message):
    db_clear_channel(m.from_user.id)
    await m.answer("Канал отвязан.")


# ======================================================================
#     МУЗЫКА, ПРИСЛАННАЯ БОТУ -> СРАЗУ В КАНАЛ (без подтверждения)
# ======================================================================
async def send_music_to_channel(bot: Bot, m: Message, channel_id: int):
    """
    ВАЖНО: отправляет присланный аудиофайл в канал БЕЗ подписи.
    Название трека и исполнитель сохраняются. Если Telegram просит подождать
    (например, прислали сразу много треков) — бот ждёт и повторяет отправку.
    """
    async def _send():
        if m.audio:
            await bot.send_audio(
                channel_id,
                m.audio.file_id,
                title=m.audio.title,
                performer=m.audio.performer,
                duration=m.audio.duration,
            )
        else:  # mp3, присланный как «файл»
            await bot.send_document(channel_id, m.document.file_id)

    try:
        await _send()
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await _send()


@router.message(F.audio | F.document.mime_type.startswith("audio/"))
async def on_music(m: Message, bot: Bot):
    """
    ВАЖНО: срабатывает, когда пользователь присылает боту музыку (аудиофайл).
    Бот сразу отправляет её в привязанный канал этого пользователя — без кнопок и подтверждений.
    """
    if not is_allowed(m.from_user.id):
        return await m.answer("У вас нет доступа к этому боту.")

    u = db_get(m.from_user.id)
    if not u or not u["channel_id"]:
        return await m.answer("Чтобы отправлять музыку в канал, сначала привяжите его: /setchannel")

    try:
        await send_music_to_channel(bot, m, u["channel_id"])
    except TelegramAPIError:
        logging.exception("Не удалось отправить музыку в канал")
        return await m.answer("❌ Не получилось отправить. Проверьте, что бот всё ещё админ канала с правом публикации.")

    await m.answer(f"✅ Отправлено в канал «{u['channel_title']}»")


# ======================================================================
#                 ПРИЁМ ССЫЛКИ -> ПОКАЗ КНОПОК
# ======================================================================
def kb(rows: list[list[tuple[str, str]]], token: str) -> InlineKeyboardMarkup:
    """Собирает клавиатуру из пар (текст кнопки, kind)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=f"dl:{token}:{kind}") for text, kind in row]
            for row in rows
        ]
    )


@router.message(F.text)
async def on_text(m: Message):
    """
    ВАЖНО: главный обработчик. Находит ссылку в сообщении, определяет сервис
    и тип медиа, показывает кнопки с вариантами скачивания.
    """
    if not is_allowed(m.from_user.id):
        return await m.answer("У вас нет доступа к этому боту.")

    found = URL_RE.search(m.text)
    if not found:
        return await m.answer("Пришлите ссылку на TikTok, YouTube или Яндекс Музыку.")
    url = found.group(0)

    platform = detect_platform(url)
    if not platform:
        return await m.answer("Такой сервис я не поддерживаю. Умею: TikTok, YouTube, Яндекс Музыка.")

    status = await m.answer("🔎 Смотрю, что по ссылке…")
    token = uuid.uuid4().hex[:8]

    try:
        if platform == "tiktok":
            d = await tiktok_info(url)
            title, rows = d["title"], []
            if d["images"]:
                rows.append([(f"🖼 Фото ({len(d['images'])})", "photo")])
            elif d["video"]:
                rows.append([("🎬 Видео", "video")])
            if d["music"]:
                rows.append([("🎵 Музыка", "music")])
            if not rows:
                raise RuntimeError("В этом посте нечего скачать")
            data, label = d, "TikTok"

        elif platform == "youtube":
            info = await asyncio.to_thread(youtube_info_sync, url)
            title, data, label = info["title"], info, "YouTube"
            buttons = []
            for h, size in info["sizes"].items():
                mb = size / 1024 / 1024
                text = f"{h}p"
                if size:
                    text += f" · ~{mb:.0f} МБ" + (" ⚠️" if mb > MAX_MB else "")
                buttons.append((text, f"v{h}"))
            rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            rows.append([("🎵 Только звук (mp3)", "a")])

        else:  # yandex
            key = parse_yandex_track(url)
            if not key:
                raise RuntimeError("Поддерживаются только ссылки на отдельные треки (не альбомы и плейлисты)")
            track = await yandex_fetch_track(key)
            title = f"{', '.join(track.artists_name())} — {track.title}"
            data, label = {"key": key}, "Яндекс Музыка"
            rows = [[("🎵 Скачать mp3", "ym")]]

    except Exception as e:
        logging.exception("Ошибка разбора ссылки")
        return await status.edit_text(f"❌ Не получилось обработать ссылку: {e}")

    remember(PENDING, token, {"user_id": m.from_user.id, "platform": platform, "url": url, "title": title, "data": data})
    await status.edit_text(f"{label}: {title}\n\nЧто скачать?", reply_markup=kb(rows, token))


# ======================================================================
#          НАЖАТИЕ КНОПКИ -> СКАЧАТЬ И ПРИСЛАТЬ В ЛИЧКУ
# ======================================================================
@router.callback_query(F.data.startswith("dl:"))
async def on_download(cb: CallbackQuery, bot: Bot):
    """ВАЖНО: срабатывает при нажатии кнопки выбора. Скачивает и присылает результат пользователю в личку."""
    _, token, kind = cb.data.split(":")
    req = PENDING.get(token)
    if not req:
        return await cb.answer("Кнопка устарела — пришлите ссылку заново", show_alert=True)
    if req["user_id"] != cb.from_user.id:
        return await cb.answer("Это не ваш запрос", show_alert=True)
    await cb.answer()

    # YouTube: заранее отсекаем слишком тяжёлые варианты
    if req["platform"] == "youtube" and kind.startswith("v"):
        size = req["data"]["sizes"].get(int(kind[1:]), 0)
        if size > MAX_BYTES:
            return await cb.message.answer(
                f"Это качество весит около {size / 1024 / 1024:.0f} МБ — больше лимита {MAX_MB} МБ. Выберите ниже."
            )

    await cb.message.edit_text("⏳ Скачиваю, подождите…")
    chat_id = cb.message.chat.id   # всегда личка пользователя
    workdir = Path(tempfile.mkdtemp(prefix="dl_"))

    try:
        async with download_slots:
            ids = await deliver(bot, req, kind, workdir, chat_id)

        if has_channel(cb.from_user.id):
            await add_channel_button(bot, chat_id, ids, cb.from_user.id)
            result = "✅ Готово"
        else:
            result = "✅ Готово. Чтобы пересылать результат в канал, привяжите его: /setchannel"
    except TelegramAPIError as e:
        logging.exception("Ошибка Telegram")
        result = f"❌ Telegram не принял файл: {e}"
    except Exception as e:
        logging.exception("Ошибка скачивания")
        result = f"❌ Не получилось: {e}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)   # удаляем временные файлы

    await cb.message.edit_text(result)


# ======================================================================
#          КНОПКА «ОТПРАВИТЬ В КАНАЛ»
# ======================================================================
@router.callback_query(F.data.startswith("ch:"))
async def on_send_to_channel(cb: CallbackQuery, bot: Bot):
    """
    ВАЖНО: копирует уже отправленный пользователю результат в его канал.
    Копирование (а не пересылка) — поэтому в канале нет пометки «Переслано» и нет подписи.
    """
    token = cb.data.split(":", 1)[1]
    item = SENT.get(token)
    if not item:
        return await cb.answer("Кнопка устарела — скачайте заново", show_alert=True)
    if item["user_id"] != cb.from_user.id:
        return await cb.answer("Это не ваш результат", show_alert=True)

    u = db_get(cb.from_user.id)
    if not u or not u["channel_id"]:
        return await cb.answer("Канал не привязан: /setchannel", show_alert=True)

    try:
        await bot.copy_messages(
            chat_id=u["channel_id"],
            from_chat_id=item["chat_id"],
            message_ids=item["message_ids"],
        )
    except TelegramAPIError:
        logging.exception("Не удалось отправить в канал")
        return await cb.answer(
            "Не получилось отправить. Проверьте, что бот всё ещё админ канала с правом публикации.",
            show_alert=True,
        )

    SENT.pop(token, None)   # чтобы случайно не отправить дважды
    await cb.answer("✅ Отправлено в канал")
    if cb.message.text:     # это отдельное сообщение с кнопкой (случай с фото-альбомом)
        await cb.message.edit_text(f"✅ Отправлено в канал «{u['channel_title']}»")
    else:                   # кнопка была прямо под файлом — просто убираем её
        await cb.message.edit_reply_markup(reply_markup=None)


# ======================================================================
#                              ЗАПУСК
# ======================================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    db_init()
    bot = Bot(BOT_TOKEN, session=AiohttpSession(timeout=600))  # большой таймаут для загрузки файлов
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
