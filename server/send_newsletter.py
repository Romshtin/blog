"""Рассылка писем подписчикам блога (roman-kirillov.ru).

Запуск на VPS из папки /home/bot/blog-server:

    ./venv/bin/python send_newsletter.py письмо.txt

Формат письма.txt: первая строка — тема, дальше — текст.
Пустая строка = граница абзаца. Каждому подписчику письмо уходит
отдельным письмом (адреса не видны друг другу) с персональной
ссылкой отписки. Реквизиты SMTP — те же server/mail.txt.
"""

import smtplib
import sqlite3
import sys
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "server" / "blog.db"
MAIL = BASE / "server" / "mail.txt"


def read_config() -> tuple[str, int, str, str, str]:
    lines = [l.strip() for l in MAIL.read_text(encoding="utf-8").splitlines()]
    if len(lines) < 4:
        sys.exit("server/mail.txt неполный — рассылка выключена")
    host, port, login, password = lines[0], int(lines[1]), lines[2], lines[3]
    site = lines[4] if len(lines) > 4 else ""
    return host, port, login, password, site


def load_letter(path: str) -> tuple[str, str, str]:
    """(тема, html, plain) из текстового файла."""
    raw = Path(path).read_text(encoding="utf-8")
    lines = raw.splitlines()
    subject = lines[0].strip()
    paragraphs = [p.strip() for p in "\n".join(lines[1:]).split("\n\n") if p.strip()]
    body_html = "".join(
        f"<p style='margin:0 0 18px'>{p.replace(chr(10), '<br>')}</p>"
        for p in paragraphs
    )
    body_plain = "\n\n".join(paragraphs)
    return subject, body_html, body_plain


def make_letter(subject: str, body_html: str, login: str, site: str, token: str):
    """Письмо в стиле блога: тёмный фон, кремовый текст, медовые ссылки."""
    html = (
        "<div style='font-family:Onest,Segoe UI,Arial,sans-serif;"
        "color:#e8ded0;background:#211b15;padding:36px 30px;border-radius:12px'>"
        f"<div style='font-size:16px;line-height:1.7'>{body_html}</div>"
        "<hr style='border:none;border-top:1px solid #3a3129;margin:28px 0 18px'>"
        f"<p style='font-size:12.5px;color:#a89a88;margin:0'>"
        f"Вы получили это письмо, потому что подписались на "
        f"<a href='{site}' style='color:#d9a05b'>roman-kirillov.ru</a>.</p>"
        f"<p style='font-size:12.5px;color:#a89a88;margin:6px 0 0'>"
        f"<a href='{site}/api/unsubscribe/{token}' style='color:#d9a05b'>"
        "Отписаться</a></p></div>"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    # имя и адрес кодируются раздельно: единый encoded-word на «Имя <адрес>»
    # Mail.ru бракует («your mailer sends invalid headers»)
    msg["From"] = formataddr((str(Header("Роман Кириллов", "utf-8")), login))
    msg["To"] = login
    msg["Date"] = formatdate(localtime=True)
    msg["List-Unsubscribe"] = f"<{site}/api/unsubscribe/{token}>"
    msg.attach(MIMEText("", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Использование: python send_newsletter.py письмо.txt")
    subject, body_html, body_plain = load_letter(sys.argv[1])
    host, port, login, password, site = read_config()

    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT email, token FROM subscribers WHERE unsubscribed = 0 ORDER BY id"
        ).fetchall()
    if not rows:
        sys.exit("Подписчиков нет — отправлять некому.")
    print(f"Подписчиков: {len(rows)}. Тема: «{subject}»")

    sent = failed = 0
    with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
        smtp.login(login, password)
        for email, token in rows:
            msg = make_letter(subject, body_html, login, site, token)
            msg.replace_header("To", email)
            try:
                smtp.sendmail(login, [email], msg.as_string())
                sent += 1
                print(f"  ✓ {email}")
            except smtplib.SMTPException as e:
                failed += 1
                print(f"  ✗ {email}: {type(e).__name__}")
            time.sleep(1.5)  # бережно к лимитам Mail.ru
    print(f"Готово {datetime.now():%Y-%m-%d %H:%M}: отправлено {sent}, ошибок {failed}.")


if __name__ == "__main__":
    main()