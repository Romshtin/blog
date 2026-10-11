"""API блога Романа Кириллова: счётчик просмотров, комментарии, статика.

Сайт остаётся статическим; этот сервис даёт только /api/... и отдаёт файлы
блога, чтобы локально всё было в одном origin (относительные /api/... в
страницах работают и здесь, и на VPS за nginx). SQLite — один файл blog.db.

Запуск из папки блога:  python -m uvicorn server.main:app --port 8000
или двойным кликом по запустить-сервер.cmd
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Соль для хэша IP — чтобы не хранить адреса читателей в открытом виде.
# На VPS задаётся переменной окружения BLOG_IP_SALT в systemd-юните
# (локально по умолчанию — тестовое значение).
IP_SALT = os.environ.get("BLOG_IP_SALT", "rk-blog-local")

BLOG_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(__file__).resolve().parent / "blog.db"
# Telegram-уведомления о новых комментариях: 1-я строка — токен бота,
# 2-я — chat_id владельца. Файла нет или там пусто — уведомления выключены.
TELEGRAM_PATH = Path(__file__).resolve().parent / "telegram.txt"
# SMTP для писем читателям об ответах на их комментарии: 1-я строка — сервер,
# 2-я — порт, 3-я — логин, 4-я — пароль приложения, 5-я — адрес сайта (для
# ссылки в письме). Файла нет или он неполный — письма выключены.
# (Пароль приложения, не основной: mail.ru → Настройки → «Пароли для внешних
# приложений». Прямую отправку с IP VPS не используем — уходит в спам.)
SMTP_PATH = Path(__file__).resolve().parent / "mail.txt"
# Почта владельца: его комментарии помечаются «Автор», лимит частоты для него не действует.
OWNER_EMAIL = "kirillov4@mail.ru"
# VK ID владельца (профиль Roman Kirillov): комментарии, оставленные под этим
# аккаунтом после входа через VK, тоже помечаются «Автор» (VK не даёт почту,
# поэтому по email владельца их не узнать)
OWNER_VK_ID = 1048836436
# Ключ для удаления комментариев (DELETE ...?key=...); владелец открывает
# пост по ссылке с ?key=... — страница запоминает его в localStorage.
# Тот же ключ защищает публикацию posts.js из админки (/api/admin/save).
ADMIN_PATH = Path(__file__).resolve().parent / "admin.txt"
# Вход через VK (VK ID): 1-я строка — ID приложения, 2-я — защищённый ключ
# (в протоколе VK ID не используется, оставлен на будущее), 3-я — адрес сайта
# (по нему строится redirect_uri; VK требует, чтобы он совпадал с доверенным
# в настройках приложения). Файла нет — вход выключен.
# Протокол — VK ID без SDK (id.vk.ru/authorize + PKCE): старый oauth.vk.com
# приложения из нового кабинета отклоняет («Security Error»).
VK_PATH = Path(__file__).resolve().parent / "vk.txt"
VK_COOKIE = "vk_session"
# Кука-«паспорт» браузера: по ней читатель без VK-входа опознаёт свои
# комментарии (редактирование/удаление). Случайный токен, живёт 10 лет.
UID_COOKIE = "rk_uid"
# Куда пишется posts.js при публикации из админки: на VPS это симлинк
# server/posts.js → /var/www/blog/posts.js (запись через симлинк попадает
# в файл на диске nginx'а), локально — posts.js в папке блога.
POSTS_PUBLISH_PATH = Path(__file__).resolve().parent.parent / "posts.js"
# sitemap.xml пишется рядом с posts.js при каждой публикации (см. _write_sitemap):
# на VPS это тоже симлинк → /var/www/blog/sitemap.xml, локально — файл в корне блога.
SITEMAP_PUBLISH_PATH = POSTS_PUBLISH_PATH.parent / "sitemap.xml"
# Статические страницы sitemap без lastmod (контент правится редко);
# посты и главная (lastmod = дата свежего поста) добавляются из POSTS.
SITEMAP_STATIC_PAGES = ("obo-mne.html", "statyi.html", "otzyvy.html", "zapis.html", "politika-pd.html")
# IndexNow (ускоренный обход Яндекса и др., indexnow.org): при публикации
# поисковикам отправляются URL новых/изменённых постов + главная.
# Ключ публичный по дизайну — он же раздаётся файлом <KEY>.txt из корня сайта
# (файл в белом списке publish.ps1), поэтому в git не секрет.
INDEXNOW_KEY = "7f95ebf3b18e4943b308ded2c28ec006"
INDEXNOW_API = "https://api.indexnow.org/indexnow"
# Состояние прошлой публикации {post_id: md5(title+content)} — как blog.db,
# рядом с main.py (на VPS /home/bot/blog-server/server/), не в BLOG_DIR —
# иначе раздавался бы nginx'ом.
INDEXNOW_STATE_PATH = Path(__file__).resolve().parent / "indexnow-state.json"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        # Один просмотр = один уникальный читатель навсегда: повторное
        # открытие страницы не накручивает счётник, даже в другой день.
        # (Раньше уникальность была по дню — миграция ниже переносит старые
        # данные без потери цифр.)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS views (
                post_id INTEGER NOT NULL,
                ip_hash TEXT    NOT NULL,
                UNIQUE(post_id, ip_hash)
            )
            """
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(views)")]
        if "ymd" in cols:  # старая схема «один просмотр в день»
            conn.execute("ALTER TABLE views RENAME TO views_old")
            conn.execute(
                """
                CREATE TABLE views (
                    post_id INTEGER NOT NULL,
                    ip_hash TEXT    NOT NULL,
                    UNIQUE(post_id, ip_hash)
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO views (post_id, ip_hash)"
                " SELECT DISTINCT post_id, ip_hash FROM views_old"
            )
            conn.execute("DROP TABLE views_old")
        # Комментарии публикуются сразу; почта хранится, но никогда не
        # показывается на странице. created_ts — для лимита частоты.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                email      TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                created_ts REAL    NOT NULL,
                ip_hash    TEXT    NOT NULL,
                reply_to   INTEGER,
                notify     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Миграция старой базы: колонки для веток-ответов и согласия
        # на уведомления появились позже остальной схемы.
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(comments)")]
        if "reply_to" not in ccols:
            conn.execute("ALTER TABLE comments ADD COLUMN reply_to INTEGER")
        if "notify" not in ccols:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN notify INTEGER NOT NULL DEFAULT 0"
            )
        # Правка и удаление своих комментариев (2026-09-11): edited_at — метка
        # «изменено», deleted — удалён с ответами (на месте остаётся
        # «комментарий удалён»), uid — кука-паспорт автора комментария.
        if "edited_at" not in ccols:
            conn.execute("ALTER TABLE comments ADD COLUMN edited_at TEXT")
        if "deleted" not in ccols:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
            )
        if "uid" not in ccols:
            conn.execute("ALTER TABLE comments ADD COLUMN uid TEXT")
        # Голоса «+»/«−» за комментарии: один голос на читателя, голос можно
        # поменять или снять (повторное нажатие той же кнопки).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_votes (
                comment_id INTEGER NOT NULL,
                ip_hash    TEXT    NOT NULL,
                vote       INTEGER NOT NULL,
                UNIQUE(comment_id, ip_hash)
            )
            """
        )
        # Эмодзи-реакции на комментарии (палитра на кнопке «+»):
        # одна реакция на читателя, можно сменить или снять.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_reactions (
                comment_id INTEGER NOT NULL,
                ip_hash    TEXT    NOT NULL,
                emoji      TEXT    NOT NULL,
                UNIQUE(comment_id, ip_hash)
            )
            """
        )
        # Оценка поста (лайк/дизлайк под текстом): один голос на читателя,
        # голос можно поменять или снять (повторное нажатие той же кнопки).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS post_votes (
                post_id    INTEGER NOT NULL,
                ip_hash    TEXT    NOT NULL,
                vote       INTEGER NOT NULL,
                UNIQUE(post_id, ip_hash)
            )
            """
        )
        # Отзывы читателей со страницы «Отзывы»: публикуются после проверки
        # владельцем (approved=1). created_ts — для лимита частоты.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                approved   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL,
                created_ts REAL    NOT NULL,
                ip_hash    TEXT    NOT NULL
            )
            """
        )
        # Заявки на консультацию со страницы «Запись» (уведомление — почтой/Telegram).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                contact    TEXT    NOT NULL,
                message    TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL,
                created_ts REAL    NOT NULL,
                ip_hash    TEXT    NOT NULL
            )
            """
        )
        # Вход через VK (2026-09-03): пользователи VK и их сессии-куки.
        # Почта пишется, только если читатель разрешил её VK'у (scope=email) —
        # иначе она пустая и письма об ответах ему не отправляются.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                vk_id      INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                photo      TEXT NOT NULL DEFAULT '',
                email      TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vk_sessions (
                token      TEXT PRIMARY KEY,
                vk_id      INTEGER NOT NULL,
                created_ts REAL NOT NULL
            )
            """
        )
        # Какой комментарий оставлен через VK — аватар берётся из users.
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(comments)")]
        if "vk_id" not in ccols:
            conn.execute("ALTER TABLE comments ADD COLUMN vk_id INTEGER")
        # Подписчики рассылки: token — персональная ссылка отписки из писем.
        # unsubscribed=1 у отписавшихся — адрес остаётся, чтобы повторная
        # подписка той же почтой не создавала дубликаты.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT UNIQUE NOT NULL,
                token        TEXT UNIQUE NOT NULL,
                unsubscribed INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL,
                created_ts   REAL NOT NULL,
                ip_hash      TEXT NOT NULL
            )
            """
        )
        # Посты, о которых рассылка уже уходила: повторная публикация
        # (правка с опечаткой) не должна слать письмо второй раз.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_posts (post_id INTEGER PRIMARY KEY)"
        )


init_db()


class ViewsInfo(BaseModel):
    views: int


class CommentIn(BaseModel):
    name: str
    email: str
    text: str
    reply_to: int | None = None   # id комментария, на который отвечаем
    notify: bool = False          # согласие получать письма об ответах


class CommentOut(BaseModel):
    id: int
    name: str
    text: str
    created_at: str
    is_author: bool = False
    reply_to: int | None = None
    photo: str | None = None   # аватар из VK (None — обезличенный силуэт)
    likes: int = 0      # сколько «нравится»
    dislikes: int = 0   # сколько «не нравится»
    my: int = 0         # голос текущего читателя: 1 / 0 / -1
    reactions: dict[str, int] = {}    # эмодзи → сколько читателей поставили
    my_reaction: str | None = None    # реакция текущего читателя
    edited_at: str | None = None      # не пусто — на странице метка «изменено»
    deleted: bool = False  # удалён, но с ответами: на месте «комментарий удалён»
    mine: bool = False     # комментарий текущего читателя — можно править/удалить


class VoteIn(BaseModel):
    vote: int


REACTION_EMOJIS = ("❤️", "😊", "😢", "🙏", "🔥", "🎉")


class ReactionIn(BaseModel):
    emoji: str


class BookingIn(BaseModel):
    name: str
    contact: str
    message: str = ""


class ReviewIn(BaseModel):
    name: str
    text: str


class CommentsList(BaseModel):
    comments: list[CommentOut]


app = FastAPI(title="Блог — API", docs_url=None, redoc_url=None)

# CORS: админка открывается с file:// (Origin: null) и публикует posts.js
# напрямую на сервер. На остальные /api/... это лишь разрешает чужим сайтам
# их вызывать — там и так нет секретов, а запись защищена ключом.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def _posts_from_js() -> dict[int, str]:
    """id → заголовок из posts.js (сервер не исполняет JS, парсим текстом)."""
    js = (BLOG_DIR / "posts.js").read_text(encoding="utf-8")
    posts: dict[int, str] = {}
    for m in re.finditer(r'"id":\s*(\d+)[\s\S]*?"title":\s*"((?:[^"\\]|\\.)*)"', js):
        try:
            posts[int(m.group(1))] = json.loads('"' + m.group(2) + '"')
        except ValueError:
            posts[int(m.group(1))] = m.group(2)
    return posts


def _check_post_exists(post_id: int) -> None:
    if post_id not in _posts_from_js():
        raise HTTPException(status_code=404, detail="Запись не найдена")


def _tg_config() -> tuple[str, str] | None:
    try:
        token, chat_id = TELEGRAM_PATH.read_text(encoding="utf-8").split()[:2]
        if token and chat_id:
            return token, chat_id
    except (OSError, ValueError):
        pass
    return None


_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    """Фоновая задача с живой ссылкой (иначе GC может убить её до выполнения)."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _notify_telegram(post_title: str, name: str, text: str) -> None:
    cfg = _tg_config()
    if cfg is None:
        return
    token, chat_id = cfg
    message = f"💬 Новый комментарий к «{post_title}»\n{name}: {text[:300]}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
    except httpx.HTTPError as e:
        # уведомление не должно ломать публикацию комментария
        print("Telegram: уведомление не отправлено:", type(e).__name__)


async def _notify_booking(name: str, contact: str, message: str) -> None:
    """Заявка на консультацию — владельцу в Telegram."""
    cfg = _tg_config()
    if cfg is None:
        return
    token, chat_id = cfg
    text = f"📅 Заявка на консультацию\nИмя: {name}\nКонтакт: {contact}"
    if message:
        text += f"\nЗапрос: {message[:500]}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except httpx.HTTPError as e:
        print("Telegram: заявка не отправлена:", type(e).__name__)


async def _notify_review(name: str, text: str) -> None:
    """Новый отзыв на модерации — владельцу в Telegram."""
    cfg = _tg_config()
    if cfg is None:
        return
    token, chat_id = cfg
    message = f"🌟 Новый отзыв на модерации\n{name}: {text[:300]}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
    except httpx.HTTPError as e:
        print("Telegram: уведомление об отзыве не отправлено:", type(e).__name__)


def _smtp_send_sync(cfg: tuple[str, int, str, str, str], to: str,
                    subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage
    host, port, login, password, _site = cfg
    msg = EmailMessage()
    msg["From"] = login
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
        smtp.login(login, password)
        smtp.send_message(msg)


async def _notify_reply(parent_email: str, parent_name: str, post_title: str,
                        post_id: int, comment_id: int,
                        replier_name: str, text: str) -> None:
    """Письмо читателю, ответившему согласием, о новом ответе на его комментарий."""
    cfg = _mail_config()
    if cfg is None:
        return
    _host, _port, _login, _password, site = cfg
    link = f"{site}/post.html?id={post_id}#comment-{comment_id}"
    subject = "Ответ на ваш комментарий в блоге Романа Кириллова"
    body = (
        f"{parent_name}, здравствуйте!\n\n"
        f"{replier_name} ответил(а) на ваш комментарий к посту «{post_title}»:\n\n"
        f"«{text[:500]}»\n\n"
        f"Прочитать ответ: {link}\n\n"
        f"— это письмо отправлено автоматически, отвечать на него не нужно."
    )
    try:
        # smtplib блокирующий — уводим из цикла событий в поток
        await asyncio.to_thread(_smtp_send_sync, cfg, parent_email, subject, body)
    except Exception as e:
        # письмо не должно ломать публикацию комментария
        print("SMTP: письмо об ответе не отправлено:", type(e).__name__)


def _mail_config() -> tuple[str, int, str, str, str] | None:
    """(сервер, порт, логин, пароль приложения, адрес сайта) или None."""
    try:
        lines = [l.strip() for l in SMTP_PATH.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if len(lines) >= 5:
            return lines[0], int(lines[1]), lines[2], lines[3], lines[4].rstrip("/")
    except (OSError, ValueError):
        pass
    return None


def _site_url() -> str:
    """Адрес сайта для ссылок в письмах (5-я строка mail.txt)."""
    cfg = _mail_config()
    return (cfg[4] if cfg else "https://roman-kirillov.ru").rstrip("/")


async def _notify_owner(subject: str, body: str) -> None:
    """Письмо владельцу о событии сайта. Telegram-бот перестал доходить
    (заявлено владельцем 2026-09-09), поэтому новые комментарии, оценки
    постов, отзывы и заявки дублируются почтой — SMTP из mail.txt, тот же
    механизм, что у писем читателям об ответах."""
    cfg = _mail_config()
    if cfg is None:
        return
    try:
        # smtplib блокирующий — уводим из цикла событий в поток
        await asyncio.to_thread(_smtp_send_sync, cfg, OWNER_EMAIL, subject, body)
    except Exception as e:
        # письмо не должно ломать обработку запроса
        print("SMTP: письмо владельцу не отправлено:", type(e).__name__)


def _vk_config() -> tuple[str, str, str] | None:
    """(ID приложения, защищённый ключ, адрес сайта) или None."""
    try:
        lines = [l.strip() for l in VK_PATH.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if len(lines) >= 3:
            return lines[0], lines[1], lines[2].rstrip("/")
    except (OSError, ValueError):
        pass
    return None


def _vk_identity(request: Request) -> tuple[int, str, str, str] | None:
    """Из cookie сессии VK: (vk_id, имя, почта, аватар) или None."""
    tok = request.cookies.get(VK_COOKIE)
    if not tok:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT u.vk_id, u.name, u.email, u.photo FROM vk_sessions s"
            " JOIN users u ON u.vk_id = s.vk_id WHERE s.token = ?",
            (tok,),
        ).fetchone()
    return row if row else None


# Незаконченные входы: state → (истекает, путь страницы для возврата,
# PKCE-верификатор, device_id). В памяти — достаточно: одиночный worker,
# а просроченные состояния просто отклоняются (читатель нажмёт «Войти» ещё раз).
_vk_states: dict[str, tuple[float, str, str, str]] = {}


def _vk_safe_back(back: str) -> str:
    """Возврат только на страницы самого сайта, не на чужой домен."""
    return back if back.startswith("/") and not back.startswith("//") else "/"


# ---------- Подписка на рассылку ----------


class SubscribeIn(BaseModel):
    email: str


@app.post("/api/subscribe")
async def subscribe(sub: SubscribeIn, request: Request) -> dict:
    """Подписка на письма. Почта проверяется тут, не только в форме;
    повторная подписка той же почтой — не ошибка. Лимит от спама:
    не чаще раза в 5 минут с одного адреса."""
    email = sub.email.strip().lower()
    if len(email) > 100 or not EMAIL_RE.match(email):
        raise HTTPException(
            status_code=400, detail="Укажите настоящую почту — например, ivan@mail.ru"
        )
    viewer = ip_hash(request)
    now = datetime.now()
    with db() as conn:
        (recent,) = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE ip_hash = ? AND created_ts > ?",
            (viewer, now.timestamp() - 5 * 60),
        ).fetchone()
        known = conn.execute(
            "SELECT unsubscribed FROM subscribers WHERE email = ?", (email,)
        ).fetchone()
        if known is None:
            conn.execute(
                "INSERT INTO subscribers (email, token, unsubscribed,"
                " created_at, created_ts, ip_hash) VALUES (?, ?, 0, ?, ?, ?)",
                (email, secrets.token_urlsafe(24),
                 now.isoformat(timespec="seconds"), now.timestamp(), viewer),
            )
        elif recent and not known[0]:
            raise HTTPException(
                status_code=429,
                detail="Только что вы уже подписывались — попробуйте позже",
            )
        elif known[0]:  # был отписан — возвращаем в списке, новый токен не нужен
            conn.execute(
                "UPDATE subscribers SET unsubscribed = 0 WHERE email = ?", (email,)
            )
    _spawn_bg(_notify_subscribed(email))
    return {"ok": True}


async def _notify_subscribed(email: str) -> None:
    """Новая подписка — владельцу в Telegram."""
    cfg = _tg_config()
    if cfg is None:
        return
    token, chat_id = cfg
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id,
                      "text": f"✉️ Новая подписка на рассылку: {email}"},
            )
    except httpx.HTTPError as e:
        print("Telegram: подписка не отправлена:", type(e).__name__)


@app.get("/api/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(token: str) -> HTMLResponse:
    """Отписка по персональной ссылке из письма. Отписываем сразу,
    страница подтверждает это в стиле блога."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE subscribers SET unsubscribed = 1 WHERE token = ?", (token,)
        )
    done = cur.rowcount > 0
    msg = ("Вы отписаны от писем. Мы больше не будем вам писать."
           if done else
           "Ссылка недействительна — возможно, вы уже отписаны раньше.")
    return HTMLResponse(
        "<!DOCTYPE html><html lang='ru'><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>Отписка — Роман Кириллов</title>"
        "<style>body{background:#211b15;color:#e8ded0;"
        "font-family:'Onest','Segoe UI',Arial,sans-serif;line-height:1.85;"
        "display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0;text-align:center;padding:0 22px}"
        "a{color:#d9a05b}</style></head><body><div>"
        f"<p>{msg}</p>"
        "<p><a href='/'>Вернуться на главную</a></p>"
        "</div></body></html>"
    )


@app.get("/api/vk/login")
def vk_login(request: Request, back: str = "/"):
    """Начало входа через VK ID: переводим читателя на id.vk.ru/authorize."""
    cfg = _vk_config()
    if cfg is None:
        raise HTTPException(status_code=404, detail="Вход через VK не настроен")
    app_id, _secret, site = cfg
    now = time.time()
    for s in [s for s, v in _vk_states.items() if v[0] < now]:
        _vk_states.pop(s, None)
    state = secrets.token_urlsafe(24)
    # PKCE (RFC-7636): случайный verifier (43..128 знаков),
    # challenge = BASE64URL(SHA256(verifier)) без «=»
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    device_id = secrets.token_hex(16)
    _vk_states[state] = (now + 600, _vk_safe_back(back), verifier, device_id)
    redirect_uri = site + "/api/vk/callback"
    url = (
        "https://id.vk.ru/authorize?response_type=code"
        + "&client_id=" + app_id
        + "&scope=email"
        + "&redirect_uri=" + urllib.parse.quote(redirect_uri, safe="")
        + "&state=" + urllib.parse.quote(state, safe="")
        + "&code_challenge=" + challenge
        + "&code_challenge_method=S256"
    )
    return RedirectResponse(url, status_code=302)


@app.get("/api/vk/callback")
async def vk_callback(request: Request, code: str = "", state: str = "",
                      error: str = "", error_description: str = "",
                      device_id: str = "") -> RedirectResponse:
    """VK ID вернул читателя: обмениваем code на токен (с code_verifier),
    запоминаем профиль (user_info), ставим сессионную cookie и возвращаем
    на исходную страницу."""
    entry = _vk_states.pop(state, None)
    cfg = _vk_config()

    def fail() -> RedirectResponse:
        back = _vk_safe_back(entry[1]) if entry else "/"
        return RedirectResponse(back + "#vk-err", status_code=302)

    if cfg is None or entry is None or entry[0] < time.time() or error or not code:
        return fail()
    _exp, back, verifier, login_device = entry
    app_id, _secret, site = cfg
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tok = (await client.post("https://id.vk.ru/oauth2/auth", data={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "code": code,
                "code_verifier": verifier,
                # VK отдаёт свой device_id в строке колбэка — обмен с нашим
                # (сгенерированным при /login) он отвергает («device_id is invalid»).
                # Внимание: FastAPI привязывает query-параметры по имени аргумента,
                # поэтому аргумент обязан называться device_id, как в строке колбэка
                "device_id": device_id or login_device,
                "redirect_uri": site + "/api/vk/callback",
            })).json()
            if "access_token" not in tok:
                # в журнал — причина от VK (без токенов); access_token не печатаем
                bad = {k: v for k, v in tok.items()
                       if k not in ("access_token", "refresh_token", "id_token")}
                print("VK ID: обмен code не удался:", bad)
                return fail()
            info = (await client.post("https://id.vk.ru/oauth2/user_info", data={
                "access_token": tok["access_token"],
                "client_id": app_id,
            })).json()
            if "user" not in info:
                print("VK ID: user_info не удался:",
                      {k: v for k, v in info.items() if k != "user"})
                return fail()
        user = info["user"]
        uid = int(user["user_id"])
        name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
        photo = user.get("avatar", "") or ""
        email = user.get("email") or ""
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as ex:
        print("VK ID: ошибка обработки профиля:", type(ex).__name__, ex)
        return fail()
    now = datetime.now()
    session = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO users (vk_id, name, photo, email, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(vk_id) DO UPDATE SET name = excluded.name,"
            " photo = excluded.photo, email = excluded.email",
            (uid, name, photo, email, now.isoformat(timespec="seconds")),
        )
        conn.execute(
            "INSERT INTO vk_sessions (token, vk_id, created_ts) VALUES (?, ?, ?)",
            (session, uid, now.timestamp()),
        )
        conn.execute(  # сессии старше 60 дней неактуальны — подчищаем
            "DELETE FROM vk_sessions WHERE created_ts < ?", (now.timestamp() - 60 * 86400,)
        )
    resp = RedirectResponse(_vk_safe_back(entry[1]) if entry else "/", status_code=302)
    resp.set_cookie(VK_COOKIE, session, max_age=60 * 86400,
                    httponly=True, samesite="lax", secure=site.startswith("https"))
    return resp


@app.get("/api/vk/me")
def vk_me(request: Request) -> dict:
    """Кто вошёл через VK в этом браузере (или 401 — никто)."""
    vk = _vk_identity(request)
    if vk is None:
        raise HTTPException(status_code=401, detail="Вход через VK не выполнен")
    return {"name": vk[1], "email": vk[2], "photo": vk[3]}


@app.post("/api/vk/logout")
def vk_logout(request: Request) -> dict:
    tok = request.cookies.get(VK_COOKIE)
    if tok:
        with db() as conn:
            conn.execute("DELETE FROM vk_sessions WHERE token = ?", (tok,))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(VK_COOKIE)
    return resp


def ip_hash(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    return hashlib.sha256((IP_SALT + ip).encode()).hexdigest()


def _ensure_uid_cookie(request: Request, response: Response) -> str:
    """Кука-паспорт читателя: выдаётся при первом запросе комментариев,
    дальше по ней опознаются «его» комментарии (правка/удаление).
    Вошедшие через VK опознаются по сессии, кука им не мешает."""
    uid = request.cookies.get(UID_COOKIE)
    if not uid:
        uid = secrets.token_urlsafe(16)
        response.set_cookie(UID_COOKIE, uid, max_age=10 * 365 * 86400,
                            httponly=True, samesite="lax")
    return uid


@app.post("/api/views/{post_id}", response_model=ViewsInfo)
def add_view(post_id: int, request: Request) -> ViewsInfo:
    """Отметить просмотр поста; вернуть суммарное число просмотров."""
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO views (post_id, ip_hash) VALUES (?, ?)",
            (post_id, ip_hash(request)),
        )
        (total,) = conn.execute(
            "SELECT COUNT(*) FROM views WHERE post_id = ?", (post_id,)
        ).fetchone()
    return ViewsInfo(views=total)


@app.get("/api/views", response_model=dict[int, int])
def all_views() -> dict[int, int]:
    """Счётчики всех постов разом — для ленты на главной."""
    with db() as conn:
        rows = conn.execute(
            "SELECT post_id, COUNT(*) FROM views GROUP BY post_id"
        ).fetchall()
    return {post_id: n for post_id, n in rows}


@app.get("/api/stats")
def stats() -> dict:
    """Просмотры и комментарии всех постов разом — для лент (главная, «Практики»)."""
    with db() as conn:
        views = dict(conn.execute(
            "SELECT post_id, COUNT(*) FROM views GROUP BY post_id"
        ).fetchall())
        comments = dict(conn.execute(
            "SELECT post_id, COUNT(*) FROM comments GROUP BY post_id"
        ).fetchall())
    return {"views": views, "comments": comments}


@app.get("/api/comments/{post_id}", response_model=CommentsList)
def get_comments(post_id: int, request: Request,
                 response: Response) -> CommentsList:
    """Комментарии поста, старые сверху — как ходы разговора.
    С каждым — рейтинг, голос текущего читателя и метка «мой» (по куке-паспорту
    или сессии VK): у своих на странице появляются кнопки изменить/удалить."""
    _check_post_exists(post_id)
    viewer = ip_hash(request)
    uid = _ensure_uid_cookie(request, response)
    vk = _vk_identity(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.text, c.created_at, c.email, c.reply_to,"
            " c.vk_id, c.uid, c.edited_at, c.deleted,"
            " COALESCE((SELECT COUNT(*) FROM comment_votes v"
            "           WHERE v.comment_id = c.id AND v.vote = 1), 0),"
            " COALESCE((SELECT COUNT(*) FROM comment_votes v"
            "           WHERE v.comment_id = c.id AND v.vote = -1), 0),"
            " COALESCE((SELECT v2.vote FROM comment_votes v2"
            "           WHERE v2.comment_id = c.id AND v2.ip_hash = ?), 0)"
            " FROM comments c WHERE c.post_id = ? ORDER BY c.id",
            (viewer, post_id),
        ).fetchall()
        ids = [r[0] for r in rows]
        vkphotos: dict[int, str] = {}
        if ids:
            marks0 = ",".join("?" * len(ids))
            for cid, photo in conn.execute(
                "SELECT c.id, u.photo FROM comments c"
                " JOIN users u ON u.vk_id = c.vk_id"
                f" WHERE c.id IN ({marks0})",
                ids,
            ):
                vkphotos[cid] = photo
        marks = ",".join("?" * len(ids)) if ids else ""
        reactions: dict[int, dict[str, int]] = {i: {} for i in ids}
        mine: dict[int, str] = {}
        if ids:
            for cid, emoji, n in conn.execute(
                "SELECT comment_id, emoji, COUNT(*) FROM comment_reactions"
                f" WHERE comment_id IN ({marks}) GROUP BY comment_id, emoji",
                ids,
            ):
                reactions[cid][emoji] = n
            for cid, emoji in conn.execute(
                "SELECT comment_id, emoji FROM comment_reactions"
                f" WHERE ip_hash = ? AND comment_id IN ({marks})",
                [viewer] + ids,
            ):
                mine[cid] = emoji
    return CommentsList(
        comments=[CommentOut(id=r[0], name=r[1], text=r[2], created_at=r[3],
                             is_author=(r[4] == OWNER_EMAIL or r[6] == OWNER_VK_ID),
                             reply_to=r[5],
                             photo=vkphotos.get(r[0]) or None,
                             likes=r[10], dislikes=r[11], my=r[12],
                             reactions=reactions[r[0]], my_reaction=mine.get(r[0]),
                             edited_at=r[8], deleted=bool(r[9]),
                             mine=((r[7] is not None and r[7] == uid)
                                   or (vk is not None and r[6] == vk[0])))
                  for r in rows]
    )


@app.post("/api/comments/{post_id}", response_model=CommentOut, status_code=201)
async def add_comment(post_id: int, comment: CommentIn, request: Request,
                      response: Response) -> CommentOut:
    """Добавить комментарий. Имя, почта и текст обязательны (проверка тут,
    не только в форме). Спам-защита: не чаще раза в минуту с одного адреса.
    Вошедший через VK пишет под именем и аватаром VK — имя и почта из формы
    игнорируются (почта VK может быть неизвестна, тогда поле пустое)."""
    _check_post_exists(post_id)
    uid = _ensure_uid_cookie(request, response)
    vk = _vk_identity(request)
    if vk is not None:
        name, email = vk[1], vk[2] or ""
    else:
        name = comment.name.strip()
        email = comment.email.strip()
    text = comment.text.strip()
    if vk is None:
        if not 1 <= len(name) <= 60:
            raise HTTPException(status_code=400, detail="Укажите имя (до 60 символов)")
        if len(email) > 100 or not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Укажите настоящую почту — например, ivan@mail.ru")
    if not 1 <= len(text) <= 2000:
        raise HTTPException(status_code=400, detail="Напишите текст комментария (до 2000 символов)")
    viewer = ip_hash(request)
    now = datetime.now()
    with db() as conn:
        # владелец (почтой или через VK) отвечает читателям без минутной паузы
        if email != OWNER_EMAIL and not (vk is not None and vk[0] == OWNER_VK_ID):
            (recent,) = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE ip_hash = ? AND created_ts > ?",
                (viewer, now.timestamp() - 60),
            ).fetchone()
            if recent:
                raise HTTPException(
                    status_code=429,
                    detail="Только что вы уже отправили комментарий — попробуйте через минуту",
                )
        # Ответ: комментарий-родитель должен существовать в этом же посте.
        parent = None
        if comment.reply_to is not None:
            parent = conn.execute(
                "SELECT email, name, notify FROM comments"
                " WHERE id = ? AND post_id = ?",
                (comment.reply_to, post_id),
            ).fetchone()
            if parent is None:
                raise HTTPException(
                    status_code=400,
                    detail="Комментарий, на который вы отвечаете, не найден",
                )
        cur = conn.execute(
            "INSERT INTO comments (post_id, name, email, text, created_at,"
            " created_ts, ip_hash, reply_to, notify, vk_id, uid)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (post_id, name, email, text, now.isoformat(timespec="seconds"),
             now.timestamp(), viewer, comment.reply_to, int(comment.notify),
             vk[0] if vk is not None else None, uid),
        )
        comment_id = cur.lastrowid
    # свой комментарий не уведомляет владельца — Telegram-плашка нужна
    # только о чужих (уведомление и так значит «кому-то стоит ответить»)
    is_owner = email == OWNER_EMAIL or (vk is not None and vk[0] == OWNER_VK_ID)
    if not is_owner:
        post_title = _posts_from_js().get(post_id, "")
        _spawn_bg(_notify_telegram(post_title, name, text))
        # Telegram-бот с RU-сервера недоступен — дубль события почтой
        _spawn_bg(_notify_owner(
            f"💬 Новый комментарий к «{post_title}»",
            f"{name} написал(а):\n\n{text[:1000]}\n\n"
            f"Ответить: {_site_url()}/post.html?id={post_id}#comment-{comment_id}",
        ))
    # Письмо об ответе — только с согласия адресата и не самому себе
    # (владелец, отвечая читателю, получает это письмо сам через Telegram).
    if (parent and parent[2] and parent[0] != email
            and EMAIL_RE.match(parent[0])):
        _spawn_bg(_notify_reply(
            parent[0], parent[1], _posts_from_js().get(post_id, ""),
            post_id, comment_id, name, text,
        ))
    return CommentOut(id=comment_id, name=name, text=text,
                      created_at=now.isoformat(timespec="seconds"),
                      is_author=(email == OWNER_EMAIL
                                 or (vk is not None and vk[0] == OWNER_VK_ID)),
                      reply_to=comment.reply_to,
                      photo=(vk[3] or None) if vk is not None else None,
                      mine=True)


class CommentEditIn(BaseModel):
    text: str


@app.put("/api/comments/{comment_id}")
def edit_comment(comment_id: int, body: CommentEditIn,
                 request: Request) -> dict:
    """Редактировать свой комментарий: вошедшему через VK — по сессии,
    остальным — по куке-паспорту. Помечается «изменено»."""
    text = body.text.strip()
    if not 1 <= len(text) <= 2000:
        raise HTTPException(
            status_code=400,
            detail="Напишите текст комментария (до 2000 символов)",
        )
    uid = request.cookies.get(UID_COOKIE, "")
    vk = _vk_identity(request)
    now = datetime.now()
    with db() as conn:
        row = conn.execute(
            "SELECT uid, vk_id, deleted FROM comments WHERE id = ?",
            (comment_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Комментарий не найден")
        if row[2]:
            raise HTTPException(status_code=409, detail="Комментарий уже удалён")
        own = (row[0] is not None and row[0] == uid) or (
            vk is not None and row[1] == vk[0])
        if not own:
            raise HTTPException(status_code=403, detail="Это не ваш комментарий")
        conn.execute(
            "UPDATE comments SET text = ?, edited_at = ? WHERE id = ?",
            (text, now.isoformat(timespec="seconds"), comment_id),
        )
    return {"ok": True, "text": text,
            "edited_at": now.isoformat(timespec="seconds")}


@app.post("/api/comments/{comment_id}/vote")
def vote_comment(comment_id: int, v: VoteIn, request: Request) -> dict:
    """Лайк/дизлайк комментария. Один голос на читателя. Нажатие той же
    кнопки снимает голос, противоположной — сразу переключает его
    (лайк → дизлайк и обратно)."""
    if v.vote not in (1, -1):
        raise HTTPException(status_code=400, detail="Голос может быть +1 или -1")
    viewer = ip_hash(request)
    with db() as conn:
        if not conn.execute(
            "SELECT 1 FROM comments WHERE id = ?", (comment_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="Комментарий не найден")
        (existing,) = conn.execute(
            "SELECT vote FROM comment_votes WHERE comment_id = ? AND ip_hash = ?",
            (comment_id, viewer),
        ).fetchone() or (0,)
        if existing == v.vote:
            # тот же голос снова — снимаем
            conn.execute(
                "DELETE FROM comment_votes WHERE comment_id = ? AND ip_hash = ?",
                (comment_id, viewer),
            )
        else:
            # противоположный голос — сразу переключаем (или голосуем впервые)
            conn.execute(
                "INSERT OR REPLACE INTO comment_votes (comment_id, ip_hash, vote)"
                " VALUES (?, ?, ?)",
                (comment_id, viewer, v.vote),
            )
        (likes, dislikes) = conn.execute(
            "SELECT COALESCE(SUM(v.vote = 1), 0), COALESCE(SUM(v.vote = -1), 0)"
            " FROM comment_votes v WHERE v.comment_id = ?",
            (comment_id,),
        ).fetchone()
    return {"likes": likes, "dislikes": dislikes,
            "my": 0 if existing == v.vote else v.vote}


@app.get("/api/postvote/{post_id}")
def get_postvote(post_id: int, request: Request) -> dict:
    """Оценка поста: счётчики и голос текущего читателя."""
    _check_post_exists(post_id)
    viewer = ip_hash(request)
    with db() as conn:
        (likes, dislikes) = conn.execute(
            "SELECT COALESCE(SUM(v.vote = 1), 0), COALESCE(SUM(v.vote = -1), 0)"
            " FROM post_votes v WHERE v.post_id = ?",
            (post_id,),
        ).fetchone()
        (my,) = conn.execute(
            "SELECT COALESCE((SELECT vote FROM post_votes"
            " WHERE post_id = ? AND ip_hash = ?), 0)",
            (post_id, viewer),
        ).fetchone()
    return {"likes": likes, "dislikes": dislikes, "my": my}


@app.post("/api/postvote/{post_id}")
async def vote_post(post_id: int, v: VoteIn, request: Request) -> dict:
    """Лайк/дизлайк поста. Один голос на читателя. Нажатие той же
    кнопки снимает голос, противоположной — сразу переключает его."""
    if v.vote not in (1, -1):
        raise HTTPException(status_code=400, detail="Голос может быть +1 или -1")
    _check_post_exists(post_id)
    viewer = ip_hash(request)
    with db() as conn:
        (existing,) = conn.execute(
            "SELECT vote FROM post_votes WHERE post_id = ? AND ip_hash = ?",
            (post_id, viewer),
        ).fetchone() or (0,)
        changed = existing != v.vote
        if existing == v.vote:
            # тот же голос снова — снимаем
            conn.execute(
                "DELETE FROM post_votes WHERE post_id = ? AND ip_hash = ?",
                (post_id, viewer),
            )
        else:
            # противоположный голос — сразу переключаем (или голосуем впервые)
            conn.execute(
                "INSERT OR REPLACE INTO post_votes (post_id, ip_hash, vote)"
                " VALUES (?, ?, ?)",
                (post_id, viewer, v.vote),
            )
        (likes, dislikes) = conn.execute(
            "SELECT COALESCE(SUM(v.vote = 1), 0), COALESCE(SUM(v.vote = -1), 0)"
            " FROM post_votes v WHERE v.post_id = ?",
            (post_id,),
        ).fetchone()
    if changed:  # снятие голоса не уведомляет — интересны новые оценки
        title = _posts_from_js().get(post_id, "")
        _spawn_bg(_notify_owner(
            f"{'👍' if v.vote == 1 else '👎'} Оценка поста «{title}»",
            f"Новый голос: {'лайк' if v.vote == 1 else 'дизлайк'}.\n"
            f"Теперь у поста: {likes} 👍 / {dislikes} 👎\n"
            f"{_site_url()}/post.html?id={post_id}",
        ))
    return {"likes": likes, "dislikes": dislikes,
            "my": 0 if existing == v.vote else v.vote}


@app.post("/api/comments/{comment_id}/react")
def react_comment(comment_id: int, r: ReactionIn, request: Request) -> dict:
    """Эмодзи-реакция на комментарий (палитра на кнопке «+»). Одна реакция
    на читателя: та же — снимает, другая — сразу заменяет."""
    if r.emoji not in REACTION_EMOJIS:
        raise HTTPException(status_code=400, detail="Неизвестная реакция")
    viewer = ip_hash(request)
    with db() as conn:
        if not conn.execute(
            "SELECT 1 FROM comments WHERE id = ?", (comment_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="Комментарий не найден")
        (existing,) = conn.execute(
            "SELECT emoji FROM comment_reactions WHERE comment_id = ? AND ip_hash = ?",
            (comment_id, viewer),
        ).fetchone() or (None,)
        if existing == r.emoji:  # та же реакция снова — снимаем
            conn.execute(
                "DELETE FROM comment_reactions WHERE comment_id = ? AND ip_hash = ?",
                (comment_id, viewer),
            )
            my = None
        else:
            conn.execute(
                "INSERT OR REPLACE INTO comment_reactions (comment_id, ip_hash, emoji)"
                " VALUES (?, ?, ?)",
                (comment_id, viewer, r.emoji),
            )
            my = r.emoji
        reactions = dict(conn.execute(
            "SELECT emoji, COUNT(*) FROM comment_reactions"
            " WHERE comment_id = ? GROUP BY emoji",
            (comment_id,),
        ).fetchall())
    return {"reactions": reactions, "my": my}


@app.post("/api/booking", status_code=201)
async def add_booking(b: BookingIn, request: Request) -> dict:
    """Заявка на консультацию со страницы «Запись»: в базу + письмо
    и Telegram владельцу. Контакт — Telegram, почта или телефон,
    на выбор читателя."""
    name = b.name.strip()
    contact = b.contact.strip()
    message = b.message.strip()
    if not 1 <= len(name) <= 60:
        raise HTTPException(status_code=400, detail="Укажите имя (до 60 символов)")
    if not 1 <= len(contact) <= 100:
        raise HTTPException(
            status_code=400,
            detail="Оставьте контакт — Telegram, почту или телефон (до 100 символов)",
        )
    if len(message) > 1000:
        raise HTTPException(
            status_code=400, detail="Опишите запрос короче (до 1000 символов)"
        )
    viewer = ip_hash(request)
    now = datetime.now()
    with db() as conn:
        # Один адрес (дом, Wi-Fi) — часто несколько людей; лимит короткий,
        # только чтобы скрипт не лил заявки потоком.
        (recent,) = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE ip_hash = ? AND created_ts > ?",
            (viewer, now.timestamp() - 120),
        ).fetchone()
        if recent:
            raise HTTPException(
                status_code=429,
                detail="С этого адреса заявка уже отправлена — попробуйте через пару минут"
                       " или напишите напрямую: @Romshtin",
            )
        conn.execute(
            "INSERT INTO bookings (name, contact, message, created_at,"
            " created_ts, ip_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (name, contact, message, now.isoformat(timespec="seconds"),
             now.timestamp(), viewer),
        )
    _spawn_bg(_notify_booking(name, contact, message))
    # Telegram-бот с RU-сервера недоступен — дубль заявки почтой
    _spawn_bg(_notify_owner(
        f"📅 Заявка на консультацию от {name}",
        f"Имя: {name}\nКонтакт: {contact}"
        + (f"\nЗапрос: {message[:500]}" if message else ""),
    ))
    return {"ok": True}


def _require_admin_key(key: str) -> None:
    """Модерация отзывов защищена тем же админ-ключом, что ✕ у комментариев."""
    try:
        real_key = ADMIN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        raise HTTPException(status_code=403, detail="Модерация не настроена")
    if not hmac.compare_digest(key, real_key):
        raise HTTPException(status_code=403, detail="Неверный ключ")


@app.get("/api/reviews")
def get_reviews(key: str = "") -> dict:
    """Одобренные отзывы, свежие сверху. С админ-ключом — ещё и ожидающие."""
    with db() as conn:
        approved = [
            {"id": r[0], "name": r[1], "text": r[2], "created_at": r[3]}
            for r in conn.execute(
                "SELECT id, name, text, created_at FROM reviews"
                " WHERE approved = 1 ORDER BY id DESC"
            )
        ]
        out: dict = {"reviews": approved}
        if key:
            _require_admin_key(key)
            out["pending"] = [
                {"id": r[0], "name": r[1], "text": r[2], "created_at": r[3]}
                for r in conn.execute(
                    "SELECT id, name, text, created_at FROM reviews"
                    " WHERE approved = 0 ORDER BY id"
                )
            ]
    return out


@app.post("/api/reviews", status_code=201)
async def add_review(r: ReviewIn, request: Request) -> dict:
    """Отзыв читателя — сначала на модерацию (approved=0), владельцу
    письмо и Telegram. Спам-защита: не чаще одного отзыва в 5 минут
    с одного адреса."""
    name = r.name.strip()
    text = r.text.strip()
    if not 1 <= len(name) <= 60:
        raise HTTPException(status_code=400, detail="Укажите имя (до 60 символов)")
    if not 1 <= len(text) <= 2000:
        raise HTTPException(status_code=400, detail="Напишите текст отзыва (до 2000 символов)")
    viewer = ip_hash(request)
    now = datetime.now()
    with db() as conn:
        (recent,) = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE ip_hash = ? AND created_ts > ?",
            (viewer, now.timestamp() - 300),
        ).fetchone()
        if recent:
            raise HTTPException(
                status_code=429,
                detail="Отзыв уже отправлен — следующий можно через 5 минут",
            )
        conn.execute(
            "INSERT INTO reviews (name, text, created_at, created_ts, ip_hash)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, text, now.isoformat(timespec="seconds"), now.timestamp(), viewer),
        )
    _spawn_bg(_notify_review(name, text))
    # Telegram-бот с RU-сервера недоступен — дубль отзыва почтой
    _spawn_bg(_notify_owner(
        f"🌟 Новый отзыв на модерации от {name}",
        f"{name} оставил(а) отзыв:\n\n{text[:1000]}",
    ))
    return {"ok": True}


@app.post("/api/reviews/{review_id}/approve")
def approve_review(review_id: int, key: str = "") -> dict:
    """Опубликовать отзыв. Только с админ-ключом (server/admin.txt)."""
    _require_admin_key(key)
    with db() as conn:
        cur = conn.execute("UPDATE reviews SET approved = 1 WHERE id = ?", (review_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Отзыв не найден")
    return {"ok": True}


@app.delete("/api/reviews/{review_id}")
def delete_review(review_id: int, key: str = "") -> dict:
    """Удалить отзыв. Только с админ-ключом (server/admin.txt)."""
    _require_admin_key(key)
    with db() as conn:
        cur = conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Отзыв не найден")
    return {"ok": True}


@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, request: Request, key: str = "") -> dict:
    """Удалить комментарий: свой (по куке-паспорту или сессии VK) либо любой
    с админ-ключом (server/admin.txt). Если на комментарий уже ответили —
    он остаётся плейсхолдером «комментарий удалён» (текст и личные данные
    стираются, ветка ответов не теряется); ответов нет — удаляется целиком."""
    is_admin = False
    if key:
        try:
            real_key = ADMIN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            real_key = ""
        is_admin = bool(real_key) and hmac.compare_digest(key, real_key)
    uid = request.cookies.get(UID_COOKIE, "")
    vk = _vk_identity(request)
    with db() as conn:
        row = conn.execute(
            "SELECT uid, vk_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Комментарий не найден")
        own = (row[0] is not None and row[0] == uid) or (
            vk is not None and row[1] == vk[0])
        if not (is_admin or own):
            raise HTTPException(status_code=403, detail="Неверный ключ")
        (replies,) = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE reply_to = ?", (comment_id,)
        ).fetchone()
        if replies:
            # Ветка жива: стираем текст вместе с личными данными (имя, почта,
            # vk, адрес, кука-паспорт), на месте остаётся «комментарий удалён».
            conn.execute(
                "UPDATE comments SET text = '', name = '', email = '',"
                " ip_hash = '', vk_id = NULL, uid = NULL, notify = 0,"
                " edited_at = NULL, deleted = 1 WHERE id = ?",
                (comment_id,),
            )
            conn.execute(
                "DELETE FROM comment_votes WHERE comment_id = ?", (comment_id,)
            )
            conn.execute(
                "DELETE FROM comment_reactions WHERE comment_id = ?",
                (comment_id,),
            )
            return {"ok": True, "removed": False}
        cur = conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        conn.execute(
            "DELETE FROM comment_votes WHERE comment_id = ?", (comment_id,)
        )
        conn.execute(
            "DELETE FROM comment_reactions WHERE comment_id = ?", (comment_id,)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Комментарий не найден")
    return {"ok": True, "removed": True}


# ---------- Автоматическая рассылка о новом посте ----------
# Публикация posts.js из админки: если среди постов появился новый id,
# подписчикам уходит письмо «Роман Кириллов опубликовал новый пост…».
# Правки существующих постов писем не шлют (id уже в sent_posts).


def _plain_markup(s: str) -> str:
    """Убрать разметку постов (жирный/курсив/маркер) из текста для письма."""
    return re.sub(r"\*+|=+", "", s).strip()


def _newsletter_send_sync(post_id: int, title: str, excerpt: str) -> tuple[int, int]:
    """Разослать анонс активным подписчикам. Возвращает (отправлено, ошибок)."""
    import smtplib
    from email.header import Header
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr

    cfg = _mail_config()
    if cfg is None:
        return 0, 0
    host, port, login, password, site = cfg
    with db() as conn:
        rows = conn.execute(
            "SELECT email, token FROM subscribers WHERE unsubscribed = 0 ORDER BY id"
        ).fetchall()
    if not rows:
        return 0, 0
    sent = failed = 0
    with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
        smtp.login(login, password)
        for email, token in rows:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Роман Кириллов опубликовал новый пост «{title}»"
            # имя и адрес кодируются раздельно: единый encoded-word на
            # «Имя <адрес>» Mail.ru бракует («your mailer sends invalid headers»)
            msg["From"] = formataddr((str(Header("Роман Кириллов", "utf-8")), login))
            msg["To"] = email
            msg["List-Unsubscribe"] = f"<{site}/api/unsubscribe/{token}>"
            msg.attach(MIMEText("", "plain", "utf-8"))
            msg.attach(MIMEText(_newsletter_html(site, token, post_id, title, excerpt),
                                "html", "utf-8"))
            try:
                smtp.sendmail(login, [email], msg.as_string())
                sent += 1
            except smtplib.SMTPException as e:
                failed += 1
                print("Рассылка:", email, type(e).__name__)
            time.sleep(1.5)  # бережно к лимитам Mail.ru
    return sent, failed


def _newsletter_html(site: str, token: str, post_id: int,
                     title: str, excerpt: str) -> str:
    """Письмо в стиле блога: анонс + персональная ссылка отписки."""
    intro = (f"Роман Кириллов опубликовал новый пост "
             f"«{_plain_markup(title)}».")
    body = (f"<p style='margin:0 0 18px'>{_plain_markup(excerpt)}</p>"
            if excerpt else "")
    return (
        "<div style='font-family:Onest,Segoe UI,Arial,sans-serif;"
        "color:#e8ded0;background:#211b15;padding:36px 30px;border-radius:12px;"
        "font-size:16px;line-height:1.7'>"
        f"<p style='margin:0 0 18px'>{intro}</p>"
        f"{body}"
        f"<p style='margin:0 0 18px'>"
        f"Весь пост читайте по "
        f"<a href='{site}/post.html?id={post_id}' style='color:#d9a05b'>"
        "ссылке</a></p>"
        "<hr style='border:none;border-top:1px solid #3a3129;margin:28px 0 18px'>"
        f"<p style='font-size:12.5px;color:#a89a88;margin:0'>"
        f"Вы получили это письмо, потому что подписались на "
        f"<a href='{site}' style='color:#d9a05b'>roman-kirillov.ru</a>.</p>"
        f"<p style='font-size:12.5px;color:#a89a88;margin:6px 0 0'>"
        f"<a href='{site}/api/unsubscribe/{token}' style='color:#d9a05b'>"
        "Отписаться</a></p></div>"
    )


async def _announce_new_post(post_id: int, title: str, excerpt: str) -> None:
    """Письмо подписчикам + отчёт владельцу в Telegram."""
    sent, failed = await asyncio.to_thread(
        _newsletter_send_sync, post_id, title, excerpt
    )
    if not sent and not failed:
        return  # подписчиков нет или почта не настроена
    text = (f"✉️ Рассылка о посте «{title}»\n"
            f"Отправлено: {sent}" + (f", ошибок: {failed}" if failed else ""))
    cfg = _tg_config()
    if cfg is None:
        return
    token, chat_id = cfg
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except httpx.HTTPError as e:
        print("Telegram: отчёт о рассылке не отправлен:", type(e).__name__)


def _write_sitemap(posts: list) -> None:
    """Перегенерировать sitemap.xml из POSTS (вызывается при каждой публикации).

    Публикация и удаление постов идут через /api/admin/save, поэтому этой одной
    точки достаточно: sitemap всегда соответствует текущему POSTS — ловушка
    «добавил пост и забыл вписать в sitemap» больше не срабатывает. lastmod у
    поста — его дата; у главной — дата самого свежего поста; статические
    страницы — без lastmod. Адрес сайта — из mail.txt (как в письмах), при
    отсутствии файла — домен блога. При ошибке записи локальный sitemap не
    трогаем (плохой парс POSTS сюда не доходит — posts_all фильтруется).
    """
    mc = _mail_config()
    site = (mc[4] if mc else "https://roman-kirillov.ru").rstrip("/")
    dated = sorted(
        [(str(p.get("date") or ""), int(p["id"])) for p in posts
         if isinstance(p.get("id"), int)],
        reverse=True,
    )
    entries = [(site + "/", dated[0][0])] if dated else []
    entries += [(f"{site}/post.html?id={pid}", date) for date, pid in dated]
    entries += [(f"{site}/{page}", None) for page in SITEMAP_STATIC_PAGES]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in entries:
        parts.append("  <url>")
        parts.append(f"    <loc>{loc}</loc>")
        if lastmod:
            parts.append(f"    <lastmod>{lastmod}</lastmod>")
        parts.append("  </url>")
    parts.append("</urlset>")
    try:
        SITEMAP_PUBLISH_PATH.write_text("\n".join(parts) + "\n", encoding="utf-8")
    except OSError as e:
        print("sitemap.xml не перегенерирован:", type(e).__name__)


def _indexnow_load_state() -> dict:
    """Состояние прошлой публикации {post_id: md5(title+\\0+content)}.

    Нет файла или битый JSON → {} : все посты считаются изменёнными
    (первый запуск — один полный пинг, для IndexNow это легитимно).
    """
    try:
        data = json.loads(INDEXNOW_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _indexnow_changes(posts_all: list) -> tuple:
    """(urlList | None, новое состояние) из сравнения с прошлой публикацией.

    Пустое/битое старое состояние → все посты изменены. Ничего не изменилось
    → (None, state): вызов пропускается, файл состояния не трогается.
    Удалённые посты в новое состояние не переносятся (отозвать URL из
    индекса IndexNow не умеет). urlList — URL постов + главная.
    """
    site = _site_url()
    current = {
        str(p["id"]): hashlib.md5(
            (str(p.get("title") or "") + "\x00" + str(p.get("content") or "")).encode("utf-8")
        ).hexdigest()
        for p in posts_all if isinstance(p.get("id"), int)
    }
    old = _indexnow_load_state()
    new_state = current  # удалённые id отсюда естественно выпадают
    changed = set(current.items()) - set(old.items())
    if not changed:
        return None, old
    # посты в urlList — свежие сверху (порядок как в sitemap)
    dated = sorted(
        [(str(p.get("date") or ""), str(p["id"]))
         for p in posts_all
         if isinstance(p.get("id"), int) and str(p["id"]) in {c[0] for c in changed}],
        reverse=True,
    )
    urls = [f"{site}/post.html?id={pid}" for _, pid in dated]
    urls.append(site + "/")  # главная: её lastmod = дата свежего поста
    return urls, new_state


async def _indexnow_notify(urls: list, new_state: dict) -> None:
    """Пинг поисковиков (IndexNow, прежде всего Яндекс) о новых/изменённых URL.

    Фоновая (_spawn_bg), не фатальна: ошибка сети или ответ 429 — состояние
    не переписывается, URL уйдут при следующей публикации.
    """
    site = _site_url()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(INDEXNOW_API, json={
                "host": urllib.parse.urlparse(site).hostname,
                "key": INDEXNOW_KEY,
                "keyLocation": f"{site}/{INDEXNOW_KEY}.txt",
                "urlList": urls,
            })
        print("IndexNow:", r.status_code, f"({len(urls)} URL)", flush=True)
        if r.status_code in (200, 202):
            INDEXNOW_STATE_PATH.write_text(
                json.dumps(new_state, ensure_ascii=False), encoding="utf-8")
    except httpx.HTTPError as e:
        print("IndexNow: не отправлено:", type(e).__name__, flush=True)


@app.post("/api/admin/save")
async def admin_save(data: dict, request: Request) -> dict:
    """Опубликовать posts.js из админки. Только с админ-ключом (server/admin.txt)."""
    try:
        real_key = ADMIN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        raise HTTPException(status_code=403, detail="Публикация не настроена")
    key = data.get("key") or ""
    if not isinstance(key, str) or not hmac.compare_digest(key, real_key):
        raise HTTPException(status_code=403, detail="Неверный ключ")
    content = data.get("content") or ""
    # Базовая проверка, что прислали именно posts.js, а не что-то случайное.
    if not isinstance(content, str) or not (
        10 < len(content) < 5_000_000 and "const POSTS" in content
    ):
        raise HTTPException(status_code=400, detail="Файл похож не на posts.js — публикация отменена")
    # Через временный файл: на VPS POSTS_PUBLISH_PATH — симлинк, os.replace
    # по нему заменил бы сам симлинк, поэтому копируем содержимое.
    tmp = ADMIN_PATH.parent / ("posts.js.tmp-" + secrets.token_hex(4))
    try:
        tmp.write_text(content, encoding="utf-8")
        shutil.copyfile(tmp, POSTS_PUBLISH_PATH)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    # Новый пост = новый id в POSTS: анонс подписчикам в фоне.
    # Правки существующих постов (id уже в sent_posts) писем не шлют.
    m = re.search(r"const POSTS = (\[.*?\n\])", content, re.S)
    if m:
        try:
            posts_all = [p for p in json.loads(m.group(1)) if isinstance(p, dict)]
        except ValueError:
            posts_all = []
        # sitemap.xml — из всех постов разом: актуален после каждого
        # добавления и удаления (пустой парс → файл не трогаем).
        if posts_all:
            _write_sitemap(posts_all)
            # IndexNow-пинг Яндекса и др. о новых/изменённых URL (фоном, не фатально)
            urls, new_state = _indexnow_changes(posts_all)
            if urls:
                _spawn_bg(_indexnow_notify(urls, new_state))
        with db() as conn:
            known = {r[0] for r in conn.execute("SELECT post_id FROM sent_posts")}
            fresh = [p for p in posts_all
                     if isinstance(p.get("id"), int) and p["id"] not in known]
            for p in fresh:  # отметка сразу: рестарт не должен удвоить письма
                conn.execute("INSERT OR IGNORE INTO sent_posts VALUES (?)", (p["id"],))
        for p in fresh:
            title = str(p.get("title") or "").strip()
            if title:
                _spawn_bg(_announce_new_post(
                    p["id"], title, _plain_markup(str(p.get("excerpt") or ""))
                ))
    return {"ok": True}


# Статика блога — последней, чтобы /api/... не перехватывался.
# html=True: "/" отдаёт index.html; /post.html?id=... работает как обычно.
app.mount("/", StaticFiles(directory=str(BLOG_DIR), html=True), name="blog")