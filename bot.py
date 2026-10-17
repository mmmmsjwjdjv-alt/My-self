# -*- coding: utf-8 -*-
"""
MAXO SELF - سلف اکانت یکپارچه تلگرام (Telethon)
همه چیز از طریق پیام‌های ذخیره‌شده (Saved Messages) و داخل PV کنترل می‌شود.
برای تست روی Termux آماده است.
"""

import os
import re
import time
import random
import pickle
import base64
import asyncio
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, utils as tl_utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.types import (
    MessageEntityBold, MessageEntityBlockquote, MessageEntityItalic,
    MessageEmpty, MessageMediaWebPage, PeerChannel,
    ChatInviteAlready,
)
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest


# ============================================================
# CONFIG
# ============================================================
# روی ترموکس: همین مقادیر پیش‌فرض کار می‌کنن، لازم نیست چیزی ست کنی.
# روی Railway: این سه‌تا رو به عنوان Environment Variable بذار:
#   API_ID, API_HASH, SESSION_STRING
# (SESSION_STRING رو با اسکریپت generate_session.py می‌سازی — پایین توضیح داده شده)

API_ID = int(os.environ.get("API_ID", "32644"))
API_HASH = os.environ.get("API_HASH", "a524e0a1a02f005d")
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# روی Railway این رو به مسیر ولوم (مثلاً /data) ست کن تا دیتابیس و کش
# رسانه‌ها بین دیپلوی‌ها و ری‌استارت‌ها از بین نره. روی ترموکس نیازی نیست
# و خودش کنار همین فایل ذخیره می‌کنه.
DATA_DIR = os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or APP_DIR
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

SESSION = os.path.join(DATA_DIR, "MAXO")   # فقط وقتی SESSION_STRING خالی باشه استفاده می‌شه
DB_FILE = os.path.join(DATA_DIR, "MAXO.db")
MEDIA_DIR = Path(DATA_DIR) / "maxo_media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

TEHRAN = ZoneInfo("Asia/Tehran")

MAX_NAME_LEN = 64            # حد امن اسم
MAX_BIO_LEN = 70             # حد امن بیو (اکانت عادی تلگرام)
CLOCK_LABEL = "MAXO"         # برچسب پیش‌فرض جلوی ساعت در اسم

MAX_MEDIA_MB = 50            # حداکثر حجم رسانه‌ای که کش می‌شود
ARCHIVE_TTL_HOURS = 24       # بعد این مدت، کش پیام‌های حذف‌نشده پاک می‌شود

AUTO_REPLY_COOLDOWN = 600    # پاسخ خودکار حداکثر هر ۱۰ دقیقه یکبار برای هر نفر
AUTO_REPLY_MAX_DELAY = 0    # حداکثر تاخیر قبل از ارسال پاسخ خودکار (ثانیه)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS blocked (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    created_at TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS chats (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    received INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    last_message TEXT,
    last_seen TEXT,
    last_auto_reply_at INTEGER DEFAULT 0
)
""")

# مهاجرت امن برای دیتابیس‌های قدیمی‌تر که این ستون را ندارند
try:
    db.execute("ALTER TABLE chats ADD COLUMN last_auto_reply_at INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass

db.execute("""
CREATE TABLE IF NOT EXISTS muted (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    created_at TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS deleted_archive (
    msg_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    sender_id INTEGER,
    name TEXT,
    username TEXT,
    text TEXT,
    media_type TEXT,
    media_path TEXT,
    created_at INTEGER
)
""")
db.commit()

# اگه از نسخه‌ی قبلی یه جدول deleted_archive با ستون‌های قدیمی مونده،
# خودش می‌سازدش دوباره (این جدول فقط کش موقتِ ۲۴ ساعته‌ست، جای نگرانی نیست).
_required_archive_cols = {
    "msg_id", "chat_id", "sender_id", "name", "username",
    "text", "media_type", "media_path", "created_at"
}
_existing_archive_cols = {
    row[1] for row in db.execute("PRAGMA table_info(deleted_archive)").fetchall()
}
if not _required_archive_cols.issubset(_existing_archive_cols):
    db.execute("DROP TABLE IF EXISTS deleted_archive")
    db.execute("""
    CREATE TABLE deleted_archive (
        msg_id INTEGER PRIMARY KEY,
        chat_id INTEGER,
        sender_id INTEGER,
        name TEXT,
        username TEXT,
        text TEXT,
        media_type TEXT,
        media_path TEXT,
        created_at INTEGER
    )
    """)
    db.commit()


def get_setting(key, default=""):
    cur = db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_setting(key, value):
    db.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value))
    )
    db.commit()


DEFAULT_SETTINGS = {
    "auto_reply": "0",
    "auto_text": "سلام، فعلاً آفلاین هستم. بعداً پیام شما را می‌بینم.",
    "auto_style": "as_typed",
    "waiting_for_text": "0",
    "name_clock": "0",
    "name_font": "1",
    "name_label": CLOCK_LABEL,
    "name_original": "",
    "bio_clock": "0",
    "bio_font": "1",
    "bio_original": "",
    "archive_enabled": "1",
    "chat_font_enabled": "0",
    "chat_font_style": "normal",
}

for _key, _value in DEFAULT_SETTINGS.items():
    if get_setting(_key, "") == "":
        set_setting(_key, _value)


def get_last_auto_reply(user_id):
    row = db.execute(
        "SELECT last_auto_reply_at FROM chats WHERE user_id=?",
        (int(user_id),)
    ).fetchone()
    return row[0] if row and row[0] else 0


def set_last_auto_reply(user_id, ts):
    db.execute(
        "UPDATE chats SET last_auto_reply_at=? WHERE user_id=?",
        (int(ts), int(user_id))
    )
    db.commit()


def set_auto_entities(entities):
    try:
        blob = pickle.dumps(entities or [])
        encoded = base64.b64encode(blob).decode("ascii")
    except Exception as e:
        print("ENTITIES SAVE ERROR:", e)
        encoded = ""
    set_setting("auto_text_entities", encoded)


def load_auto_entities():
    raw = get_setting("auto_text_entities", "")
    if not raw:
        return []
    try:
        return pickle.loads(base64.b64decode(raw))
    except Exception as e:
        print("ENTITIES LOAD ERROR:", e)
        return []


# ============================================================
# TELEGRAM CLIENT
# ============================================================

# اگه SESSION_STRING تنظیم شده باشه (حالت Railway) از همون استفاده می‌کنه
# وگرنه از فایل سشن محلی (حالت Termux) استفاده می‌کنه.
if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION, API_ID, API_HASH)

ME_ID = None
suppress_delete_ids = set()


# ============================================================
# فونت‌های یونیکد برای اسم / بیو (۴ فونت، هم حروف هم عدد)
# ============================================================
# فونت ۱ = Sans-Serif Bold   ->  𝗠𝗔𝗫𝗢 | 𝟯:𝟮𝟬
# فونت ۲ = Bold (سریف)       ->  𝐌𝐀𝐗𝐎 | 𝟑:𝟐𝟎
# فونت ۳ = Monospace         ->  𝙼𝙰𝚇𝙾 | 𝟹:𝟸𝟶
# فونت ۴ = Sans-Serif ساده   ->  𝖬𝖠𝖷𝖮 | 𝟦:𝟤𝟢

def _build_font_map(upper_start, lower_start, digit_start):
    mapping = {}
    for i in range(26):
        mapping[chr(ord("A") + i)] = chr(upper_start + i)
        mapping[chr(ord("a") + i)] = chr(lower_start + i)
    for i in range(10):
        mapping[chr(ord("0") + i)] = chr(digit_start + i)
    return mapping


FONT_MAPS = {
    1: _build_font_map(0x1D5D4, 0x1D5EE, 0x1D7EC),   # Sans-Serif Bold
    2: _build_font_map(0x1D400, 0x1D41A, 0x1D7CE),   # Bold
    3: _build_font_map(0x1D670, 0x1D68A, 0x1D7F6),   # Monospace
    4: _build_font_map(0x1D5A0, 0x1D5BA, 0x1D7E2),   # Sans-Serif
}


def apply_font(text, font):
    mapping = FONT_MAPS.get(int(font), FONT_MAPS[1])
    return "".join(mapping.get(ch, ch) for ch in text)


def clock_12h(now_dt):
    hour = now_dt.hour % 12
    if hour == 0:
        hour = 12
    return f"{hour}:{now_dt.minute:02d}"


def build_name_text(font):
    now_dt = datetime.now(TEHRAN)
    label = get_setting("name_label", CLOCK_LABEL) or CLOCK_LABEL
    raw = f"{label} | {clock_12h(now_dt)}"
    styled = apply_font(raw, font)
    return styled[:MAX_NAME_LEN]


def build_bio_text(font):
    now_dt = datetime.now(TEHRAN)
    original = get_setting("bio_original", "")
    raw_line = f"TIME : {clock_12h(now_dt)}"
    styled_line = apply_font(raw_line, font)

    if original:
        full = f"{original}\n{styled_line}"
    else:
        full = styled_line

    if len(full) > MAX_BIO_LEN:
        keep = MAX_BIO_LEN - len(styled_line) - 1
        if keep > 0:
            full = f"{original[:keep]}\n{styled_line}"
        else:
            full = styled_line[:MAX_BIO_LEN]

    return full


# ============================================================
# پیام پاسخ خودکار: دقیقاً با همون فرمتی که تایپ شده، یا استایل انتخابی
# ============================================================

def build_bold_entity(text):
    length = len(tl_utils.add_surrogate(text))
    if length <= 0:
        return []
    return [MessageEntityBold(offset=0, length=length)]


def build_quote_entity(text):
    length = len(tl_utils.add_surrogate(text))
    if length <= 0:
        return []
    try:
        return [MessageEntityBlockquote(offset=0, length=length, collapsed=False)]
    except TypeError:
        return [MessageEntityBlockquote(offset=0, length=length)]


def get_auto_reply_payload():
    raw_text = get_setting("auto_text", "")
    style = get_setting("auto_style", "as_typed")

    if not raw_text:
        return "", []

    if style == "bold":
        return raw_text, build_bold_entity(raw_text)
    elif style == "quote":
        return raw_text, build_quote_entity(raw_text)
    else:
        return raw_text, load_auto_entities()


# ============================================================
# HELP MENU
# ============================================================

HELP = """
<b>⚡ MAXO SELF</b>
<blockquote>مدیریت کامل از طریق پیام‌های ذخیره‌شده</blockquote>

━━━━━━━━━━━━━━━━━━

<b>اسپم</b>
<code>اسپم 10 سلام</code> — تکرار پیام (حداکثر ۳۰ بار)

<b>فونت پیام</b>
<code>فونت روشن</code> / <code>فونت خاموش</code>
<code>فونت کشیده</code> / <code>فونت بولد</code> / <code>فونت ایتالیک</code> / <code>فونت بولد کشیده</code> / <code>فونت ساده</code>

<b>انتقال محتوای قابل دسترسی</b>
<code>خالی کردن محتوا لینک</code> — لینک عمومی/خصوصی (t.me/+...)، @username یا t.me/c/...
اول فوروارد می‌کنه؛ اگه محدود بود دانلود و آپلود می‌کنه (همه نوع محتوا)
<code>خالی کردن محتوا لینک 200</code> — فقط ۲۰۰ پیام اول
<code>خالی کردن محتوا لینک از اول</code> — شروع از ابتدا (پیش‌فرض: ادامه از جایی که مونده)
<code>توقف انتقال</code>

<b>🎛 پاسخ خودکار</b>
<code>روشن</code> / <code>خاموش</code>
<code>متن</code> — نمایش متن و استایل فعلی
<code>تغییر متن</code> — پیام بعدی رو با هر فرمتی بفرستی (بولد/ایتالیک/نقل‌قول/...) دقیقاً همونجوری ذخیره می‌شه
<code>لغو</code> — لغو حالت «تغییر متن»
<code>استایل ساده</code> — دقیقاً مثل نوشتنت
<code>استایل بولد</code> — کل متن بولد فرستاده شود
<code>استایل نقل قول</code> — کل متن به شکل نقل‌قول فرستاده شود
<i>حداکثر هر ۱۰ دقیقه یکبار برای هر نفر، با تاخیر تصادفی تا ۲۰ ثانیه</i>

━━━━━━━━━━━━━━━━━━

<b>🕒 ساعت در اسم</b> (بر اساس ساعت ایران، هر دقیقه دقیق آپدیت می‌شود)
<code>اسم 1</code> تا <code>اسم 4</code> — فعال با فونت شماره ۱ تا ۴
<code>اسم MAXO</code> — تغییر برچسب اسم (هر متنی بخوای)
<code>حذف اسم</code> — خاموش و بازگشت به اسم قبلی

<b>🕒 ساعت در بیو</b>
<code>بیو 1</code> تا <code>بیو 4</code> — فعال با فونت انتخابی
(بیو قبلی حفظ می‌شود و زیرش زمان اضافه می‌شود)
<code>حذف بیو</code> — خاموش و بازگشت به بیوی قبلی

━━━━━━━━━━━━━━━━━━

<b>🔇 سکوت</b>
<blockquote>این دو دستور را داخل PV همون شخص بزن</blockquote>
<code>سکوت</code> — پیام‌های بعدی این شخص خودکار برای هردو پاک می‌شود
<code>حذف سکوت</code> — لغو سکوت
<code>سکوت‌ها</code> — لیست افراد ساکت‌شده

━━━━━━━━━━━━━━━━━━

<b>🚫 بلاک</b>
<code>بلاک @username</code> یا <code>بلاک 123456</code>
<code>انبلاک @username</code>
<code>بلاک‌ها</code> — لیست بلاک‌شده‌ها
<i>داخل PV هم فقط با تایپ «بلاک» همون شخص بلاک می‌شود</i>

━━━━━━━━━━━━━━━━━━

<b>🗑 حذف پیام</b>
<code>حذف 30</code> — داخل همون PV بزن، ۳۰ پیام آخر پاک می‌شود
<code>حذف</code> — روی هر پیامی (چه مال من چه مال طرف) ریپلای کن، همون یکی سریع پاک می‌شود

━━━━━━━━━━━━━━━━━━

<b>📦 آرشیو پیام‌های حذف‌شده</b>
هر پیام PV (متن، عکس، فیلم، ویس، حتی عکس/فیلم زمان‌دار) کش می‌شود؛
اگر طرف مقابل پاکش کند، خودش با فرستنده و زمان برات میاد اینجا.
<code>آرشیو روشن</code> / <code>آرشیو خاموش</code>

━━━━━━━━━━━━━━━━━━

<b>👤 اطلاعات</b>
<code>وضعیت</code> — وضعیت کلی ربات
<code>اکانت</code> — اطلاعات اکانت
<code>دستورات</code> — همین راهنما

━━━━━━━━━━━━━━━━━━
"""


# ============================================================
# UTILS
# ============================================================

def now():
    return datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M:%S")


def normalize_number(text):
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def user_name(user):
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or "بدون نام"


def user_username(user):
    username = getattr(user, "username", None)
    return f"@{username}" if username else "ندارد"


async def resolve_user(value):
    value = value.strip()
    if value.startswith("@"):
        value = value[1:]
    try:
        return await client.get_entity(value)
    except Exception as e:
        print("RESOLVE ERROR:", e)
        return None


def save_chat(user, message_text=""):
    try:
        db.execute("""
        INSERT OR IGNORE INTO chats
        (user_id, username, name, received, replies, last_message, last_seen, last_auto_reply_at)
        VALUES (?, ?, ?, 0, 0, '', ?, 0)
        """, (user.id, getattr(user, "username", None), user_name(user), now()))

        db.execute("""
        UPDATE chats
        SET username=?, name=?, last_message=?, last_seen=?, received=received+1
        WHERE user_id=?
        """, (
            getattr(user, "username", None),
            user_name(user),
            message_text[:500],
            now(),
            user.id
        ))
        db.commit()
    except Exception as e:
        print("DB CHAT ERROR:", e)


# ============================================================
# BLOCK / UNBLOCK
# ============================================================

async def block_user(user):
    if not user:
        return "❌ <b>کاربر پیدا نشد.</b>"
    try:
        input_user = await client.get_input_entity(user)
        await client(BlockRequest(id=input_user))

        db.execute("""
        INSERT OR REPLACE INTO blocked (user_id, username, name, created_at)
        VALUES (?, ?, ?, ?)
        """, (user.id, getattr(user, "username", None), user_name(user), now()))
        db.commit()

        return (
            "🚫 <b>کاربر بلاک شد.</b>\n\n"
            f"<b>نام:</b> {user_name(user)}\n"
            f"<b>ID:</b> <code>{user.id}</code>\n"
            f"<b>Username:</b> <code>{user_username(user)}</code>"
        )
    except Exception as e:
        return f"❌ <b>بلاک نشد.</b>\n\n<code>{str(e)[:700]}</code>"


async def unblock_user(user):
    if not user:
        return "❌ <b>کاربر پیدا نشد.</b>"
    try:
        input_user = await client.get_input_entity(user)
        await client(UnblockRequest(id=input_user))

        db.execute("DELETE FROM blocked WHERE user_id=?", (user.id,))
        db.commit()

        return f"🟢 <b>کاربر آنبلاک شد.</b>\n\n<b>{user_name(user)}</b>"
    except Exception as e:
        return f"❌ <b>آنبلاک نشد.</b>\n\n<code>{str(e)[:700]}</code>"


def block_list():
    cur = db.execute("""
    SELECT user_id, username, name, created_at FROM blocked
    ORDER BY created_at DESC
    """)
    rows = cur.fetchall()

    if not rows:
        return "🚫 <b>لیست بلاک خالی است.</b>"

    text = "🚫 <b>کاربران بلاک‌شده</b>\n\n━━━━━━━━━━━━━━━━━━\n\n"
    for i, row in enumerate(rows, 1):
        uid, username, name, _ = row
        text += (
            f"<b>{i}. {name}</b>\n"
            f"ID: <code>{uid}</code>\n"
            f"Username: <code>@{username or 'ندارد'}</code>\n\n"
        )
    text += f"━━━━━━━━━━━━━━━━━━\n<b>تعداد:</b> {len(rows)}"
    return text


# ============================================================
# MUTE (سکوت)
# ============================================================

def is_muted(user_id):
    row = db.execute("SELECT 1 FROM muted WHERE user_id=?", (int(user_id),)).fetchone()
    return row is not None


def add_mute(user):
    db.execute("""
    INSERT OR REPLACE INTO muted (user_id, username, name, created_at)
    VALUES (?, ?, ?, ?)
    """, (user.id, getattr(user, "username", None), user_name(user), now()))
    db.commit()


def remove_mute(user_id):
    db.execute("DELETE FROM muted WHERE user_id=?", (int(user_id),))
    db.commit()


def mute_list():
    cur = db.execute("""
    SELECT user_id, username, name, created_at FROM muted
    ORDER BY created_at DESC
    """)
    rows = cur.fetchall()

    if not rows:
        return "🔇 <b>لیست سکوت خالی است.</b>"

    text = "🔇 <b>افراد ساکت‌شده</b>\n\n━━━━━━━━━━━━━━━━━━\n\n"
    for i, row in enumerate(rows, 1):
        uid, username, name, _ = row
        text += (
            f"<b>{i}. {name}</b>\n"
            f"ID: <code>{uid}</code>\n"
            f"Username: <code>@{username or 'ندارد'}</code>\n\n"
        )
    text += f"━━━━━━━━━━━━━━━━━━\n<b>تعداد:</b> {len(rows)}"
    return text


# ============================================================
# DELETE MESSAGES IN PV
# ============================================================

async def delete_private_messages(event, count):
    if count <= 0:
        return
    if count > 1000:
        count = 1000

    try:
        message_ids = []
        async for message in client.iter_messages(entity=event.chat_id, limit=count):
            message_ids.append(message.id)

        if not message_ids:
            return

        for mid in message_ids:
            suppress_delete_ids.add(int(mid))

        await client.delete_messages(entity=event.chat_id, message_ids=message_ids, revoke=True)
    except Exception as e:
        print("DELETE ERROR:", type(e).__name__, str(e))


async def delete_replied_message(event):
    """پاک کردن سریع فقط همون پیامی که روش ریپلای شده (مال خودم یا طرف مقابل)."""
    try:
        reply_id = event.message.reply_to_msg_id
        if not reply_id:
            return False

        ids = [int(reply_id), int(event.message.id)]
        for mid in ids:
            suppress_delete_ids.add(mid)

        await client.delete_messages(entity=event.chat_id, message_ids=ids, revoke=True)
        return True
    except Exception as e:
        print("REPLY DELETE ERROR:", e)
        return False


# ============================================================
# آرشیو پیام‌های PV (متن + عکس/فیلم/ویس، حتی زمان‌دار)
# ============================================================

async def cache_incoming_message(event):
    if get_setting("archive_enabled", "1") != "1":
        return

    try:
        message = event.message
        sender = await event.get_sender()

        media_type = ""
        media_path = ""

        if message.media:
            try:
                size = getattr(message.file, "size", None)

                if size and size > MAX_MEDIA_MB * 1024 * 1024:
                    media_type = "large_skipped"
                else:
                    chat_dir = MEDIA_DIR / str(event.chat_id)
                    chat_dir.mkdir(parents=True, exist_ok=True)

                    path = await client.download_media(
                        message,
                        file=str(chat_dir / f"{message.id}")
                    )

                    if path:
                        media_path = str(path)

                    if message.photo:
                        media_type = "photo"
                    elif message.video_note:
                        media_type = "round_video"
                    elif message.video:
                        media_type = "video"
                    elif message.voice:
                        media_type = "voice"
                    elif message.gif:
                        media_type = "gif"
                    elif message.sticker:
                        media_type = "sticker"
                    elif message.document:
                        media_type = "document"
                    else:
                        media_type = "media"

            except Exception as e:
                media_type = "media_error:" + type(e).__name__

        db.execute("""
        INSERT OR REPLACE INTO deleted_archive
        (msg_id, chat_id, sender_id, name, username, text, media_type, media_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(message.id),
            int(event.chat_id),
            getattr(sender, "id", 0) or 0,
            user_name(sender) if sender else "نامشخص",
            (f"@{getattr(sender, 'username', None)}" if sender and getattr(sender, "username", None) else ""),
            message.message or "",
            media_type,
            media_path,
            int(time.time())
        ))
        db.commit()
    except Exception as e:
        print("ARCHIVE CACHE ERROR:", e)


async def send_deleted_archive(row):
    try:
        (msg_id, chat_id, sender_id, name, username, text,
         media_type, media_path, created_at) = row

        time_text = datetime.fromtimestamp(created_at, TEHRAN).strftime("%Y-%m-%d %H:%M:%S")

        caption = (
            "🗑 <b>پیام حذف‌شده</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>فرستنده:</b> {name}\n"
        )
        if username:
            caption += f"<b>یوزرنیم:</b> {username}\n"
        caption += f"<b>ID:</b> <code>{sender_id}</code>\n<b>زمان:</b> {time_text}\n"

        if text:
            caption += f"\n<b>متن:</b>\n{text}"

        if media_path and Path(media_path).exists():
            try:
                await client.send_file("me", media_path, caption=caption[:1024], parse_mode="html")
                try:
                    Path(media_path).unlink()
                except Exception:
                    pass
                return
            except Exception as e:
                print("SEND MEDIA ARCHIVE ERROR:", e)

        if media_type == "large_skipped":
            caption += "\n\n<i>حجم رسانه بیشتر از حد مجاز بود و دانلود نشد.</i>"
        elif media_type and not media_path:
            caption += "\n\n<i>این پیام رسانه داشت ولی دانلودش موفق نبود.</i>"

        await client.send_message("me", caption[:4000], parse_mode="html")
    except Exception as e:
        print("SEND ARCHIVE ERROR:", e)


async def archive_cleanup_loop():
    """پیام‌های کش‌شده‌ای که حذف نشدن رو بعد از مدتی از دیسک و دیتابیس پاک می‌کنه."""
    while True:
        try:
            cutoff = int(time.time()) - (ARCHIVE_TTL_HOURS * 3600)
            rows = db.execute(
                "SELECT msg_id, media_path FROM deleted_archive WHERE created_at < ?",
                (cutoff,)
            ).fetchall()

            for msg_id, media_path in rows:
                if media_path:
                    try:
                        p = Path(media_path)
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass

            db.execute("DELETE FROM deleted_archive WHERE created_at < ?", (cutoff,))
            db.commit()
        except Exception as e:
            print("ARCHIVE CLEANUP ERROR:", e)

        await asyncio.sleep(3600)


# ============================================================
# پاسخ خودکار با محدودیت (هر ۱۰ دقیقه یکبار برای هر نفر)
# ============================================================

async def send_delayed_autoreply(event, sender_id):
    try:
        await asyncio.sleep(0)

        # اگر تو همین فاصله یک پاسخ دیگه ارسال شده، دوباره نفرست
        last = get_last_auto_reply(sender_id)
        if time.time() - last < AUTO_REPLY_COOLDOWN:
            return

        raw_text, entities = get_auto_reply_payload()
        if not raw_text:
            return

        await event.reply(raw_text, formatting_entities=entities)
        set_last_auto_reply(sender_id, int(time.time()))

        db.execute("UPDATE chats SET replies=replies+1 WHERE user_id=?", (sender_id,))
        db.commit()
    except Exception as e:
        print("AUTO REPLY ERROR:", e)


# ============================================================
# STATUS / ACCOUNT
# ============================================================

async def status():
    auto = get_setting("auto_reply", "0") == "1"
    name_clock = get_setting("name_clock", "0") == "1"
    bio_clock = get_setting("bio_clock", "0") == "1"
    archive = get_setting("archive_enabled", "1") == "1"
    style = get_setting("auto_style", "as_typed")
    style_fa = {"as_typed": "ساده (عیناً)", "bold": "بولد", "quote": "نقل‌قول"}.get(style, style)

    blocked = db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
    muted = db.execute("SELECT COUNT(*) FROM muted").fetchone()[0]
    chats = db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    replies = db.execute("SELECT COALESCE(SUM(replies),0) FROM chats").fetchone()[0]
    cached = db.execute("SELECT COUNT(*) FROM deleted_archive").fetchone()[0]

    return (
        "⚡ <b>وضعیت MAXO</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>سلف:</b> 🟢 فعال\n"
        f"<b>پاسخ خودکار:</b> {'🟢 روشن' if auto else '🔴 خاموش'} (استایل: {style_fa})\n"
        f"<b>ساعت در اسم:</b> {'🟢 روشن' if name_clock else '🔴 خاموش'}\n"
        f"<b>ساعت در بیو:</b> {'🟢 روشن' if bio_clock else '🔴 خاموش'}\n"
        f"<b>آرشیو حذف‌شده‌ها:</b> {'🟢 روشن' if archive else '🔴 خاموش'}\n\n"
        f"<b>پاسخ‌های ارسال‌شده:</b> {replies}\n"
        f"<b>PVهای ثبت‌شده:</b> {chats}\n"
        f"<b>کاربران بلاک:</b> {blocked}\n"
        f"<b>کاربران ساکت:</b> {muted}\n"
        f"<b>پیام در کش آرشیو:</b> {cached}\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )


async def account_info():
    me = await client.get_me()
    blocked = db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
    unanswered = db.execute("SELECT COUNT(*) FROM chats WHERE received > replies").fetchone()[0]

    return (
        "👤 <b>اطلاعات اکانت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>نام:</b> {user_name(me)}\n"
        f"<b>Username:</b> <code>{user_username(me)}</code>\n"
        f"<b>ID:</b> <code>{me.id}</code>\n"
        f"<b>شماره:</b> <code>{me.phone or 'مخفی'}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>کاربران بلاک:</b> {blocked}\n"
        f"<b>PV بدون پاسخ:</b> {unanswered}\n"
    )


# ============================================================
# ساعت در اسم / بیو - فعال‌سازی، خاموش‌سازی، حلقه بروزرسانی
# ============================================================

async def enable_name_clock(font=None, label=None):
    try:
        me = await client.get_me()
        if get_setting("name_original", "") == "":
            set_setting("name_original", me.first_name or "")

        if font is not None:
            set_setting("name_font", str(font))
        if label is not None:
            set_setting("name_label", label[:20])

        set_setting("name_clock", "1")
        chosen_font = int(get_setting("name_font", "1"))
        await client(UpdateProfileRequest(first_name=build_name_text(chosen_font)))
        return True
    except Exception as e:
        print("NAME CLOCK ENABLE ERROR:", e)
        return False


async def disable_name_clock():
    set_setting("name_clock", "0")
    original = get_setting("name_original", "")
    try:
        await client(UpdateProfileRequest(first_name=original))
    except Exception as e:
        print("NAME CLOCK DISABLE ERROR:", e)
    set_setting("name_original", "")


async def enable_bio_clock(font):
    try:
        from telethon.tl.functions.users import GetFullUserRequest
        full = await client(GetFullUserRequest("me"))
        current_bio = getattr(full.full_user, "about", "") or ""

        if get_setting("bio_original", "") == "" and get_setting("bio_clock", "0") == "0":
            set_setting("bio_original", current_bio)

        set_setting("bio_font", str(font))
        set_setting("bio_clock", "1")

        await client(UpdateProfileRequest(about=build_bio_text(font)))
        return True
    except Exception as e:
        print("BIO CLOCK ENABLE ERROR:", e)
        return False


async def disable_bio_clock():
    set_setting("bio_clock", "0")
    original = get_setting("bio_original", "")
    try:
        await client(UpdateProfileRequest(about=original))
    except Exception as e:
        print("BIO CLOCK DISABLE ERROR:", e)
    set_setting("bio_original", "")


async def clock_loop():
    """
    هر دقیقه، دقیقاً سر دقیقه (بر اساس ساعت ایران) آپدیت می‌کنه تا دیلی نداشته باشه.
    """
    while True:
        wait_extra = 0

        if get_setting("name_clock", "0") == "1":
            font = int(get_setting("name_font", "1"))
            try:
                await client(UpdateProfileRequest(first_name=build_name_text(font)))
            except FloodWaitError as e:
                wait_extra = max(wait_extra, e.seconds + 1)
            except Exception as e:
                print("NAME CLOCK LOOP ERROR:", e)

        if get_setting("bio_clock", "0") == "1":
            font = int(get_setting("bio_font", "1"))
            try:
                await client(UpdateProfileRequest(about=build_bio_text(font)))
            except FloodWaitError as e:
                wait_extra = max(wait_extra, e.seconds + 1)
            except Exception as e:
                print("BIO CLOCK LOOP ERROR:", e)

        if wait_extra:
            await asyncio.sleep(wait_extra)
            continue

        now_dt = datetime.now(TEHRAN)
        sleep_s = 60 - now_dt.second - (now_dt.microsecond / 1_000_000)
        if sleep_s <= 0.5:
            sleep_s += 60
        await asyncio.sleep(sleep_s)


# ============================================================
# SAVED MESSAGES (دستورات)
# ============================================================


def stylize_chat_text(value, style):
    if style == "keshide":
        return "ـ".join(list(value)), []
    if style == "bold":
        return value, [MessageEntityBold(offset=0, length=len(tl_utils.add_surrogate(value)))] if value else []
    if style == "italic":
        return value, [MessageEntityItalic(offset=0, length=len(tl_utils.add_surrogate(value)))] if value else []
    if style == "bold_keshide":
        stretched = "ـ".join(list(value))
        return stretched, [MessageEntityBold(offset=0, length=len(tl_utils.add_surrogate(stretched)))] if stretched else []
    return value, []

# ------------------------------------------------------------
# خالی کردن محتوا (کانال / گروه / لینک خصوصی -> Saved Messages)
# ------------------------------------------------------------
# منطق: اول فوروارد (دسته‌ای، آلبوم‌ها سالم می‌مونن). اگه چت محدودیت
# «ذخیره/فوروارد ممنوع» داشت -> دانلود و بعد آپلود در Saved Messages.

EXPORT_STATE = {"running": False, "cancel": False}
EXPORT_BATCH = 100          # تعداد پیام در هر فوروارد دسته‌ای
CAPTION_LIMIT = 1024        # حد کپشن اکانت عادی


async def tg_retry(fn, *args, **kwargs):
    """اجرای یک درخواست و صبر کردن در صورت FloodWait."""
    while True:
        try:
            return await fn(*args, **kwargs)
        except FloodWaitError as e:
            print(f"FLOOD WAIT {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)


async def _entity_by_channel_id(channel_id):
    try:
        return await client.get_entity(PeerChannel(channel_id))
    except Exception:
        async for d in client.iter_dialogs():
            if getattr(d.entity, "id", None) == channel_id:
                return d.entity
        raise


async def resolve_export_target(raw):
    """@username / t.me/username / t.me/c/ID/.. / t.me/+HASH / joinchat / آیدی عددی"""
    raw = raw.strip().strip("<>")

    m = re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)([A-Za-z0-9_-]+)", raw) \
        or re.search(r"tg://join\?invite=([A-Za-z0-9_-]+)", raw)
    if m:
        invite_hash = m.group(1)
        invite = await client(CheckChatInviteRequest(invite_hash))
        if isinstance(invite, ChatInviteAlready):
            return invite.chat
        # هنوز عضو نیست -> با لینک عضو می‌شه
        updates = await client(ImportChatInviteRequest(invite_hash))
        return updates.chats[0]

    m = re.search(r"(?:t\.me|telegram\.me)/c/(\d+)", raw)
    if m:
        return await _entity_by_channel_id(int(m.group(1)))

    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", raw)
    if m:
        return await client.get_entity(m.group(1))

    if re.fullmatch(r"-?\d+", raw):
        return await client.get_entity(int(raw))

    return await client.get_entity(raw)


def _split_albums(msgs):
    units = []
    for m in msgs:
        if m.grouped_id and units and units[-1][0].grouped_id == m.grouped_id:
            units[-1].append(m)
        else:
            units.append([m])
    return units


def _unlink(*paths):
    for p in paths:
        try:
            if p:
                Path(p).unlink()
        except Exception:
            pass


async def _send_text(text, entities=None):
    if not text:
        return
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        await tg_retry(
            client.send_message, "me", chunk,
            formatting_entities=entities if len(text) <= 4000 else None,
            link_preview=False,
        )


async def _reupload_single(entity_id, m):
    """دانلود + آپلود یک پیام. True یعنی چیزی ارسال شد."""
    text = m.message or ""
    media = m.media

    # --- فقط متن / پیش‌نمایش لینک ---
    if media is None or isinstance(media, MessageMediaWebPage):
        if text:
            await _send_text(text, m.entities)
            return True
        return False

    # --- استیکر و گیف: همون فایل رو مستقیم دوباره می‌فرستیم (بدون خراب شدن نوع) ---
    if m.sticker or m.gif:
        try:
            await tg_retry(client.send_file, "me", media)
            return True
        except Exception as e:
            print("STICKER/GIF DIRECT FAILED:", type(e).__name__, str(e))

    # --- مدیای غیرفایلی: لوکیشن، مخاطب، نظرسنجی، تاس ---
    if not (m.photo or m.document):
        try:
            await tg_retry(client.send_file, "me", media)
            return True
        except Exception:
            pass
        poll = getattr(media, "poll", None)
        if poll:
            q = getattr(poll.question, "text", poll.question)
            opts = [getattr(a.text, "text", a.text) for a in poll.answers]
            await _send_text("📊 " + str(q) + "\n" + "\n".join("• " + str(o) for o in opts))
            return True
        if text:
            await _send_text(text, m.entities)
            return True
        return False

    # --- عکس / ویدیو / فایل / ویس / موزیک / ویدیو‌مسیج: دانلود و آپلود ---
    base = str(MEDIA_DIR / f"exp_{entity_id}_{m.id}")
    path = thumb = None
    try:
        path = await tg_retry(client.download_media, m, file=base)
        if not path or not Path(path).exists():
            return False

        kwargs = {}
        doc = m.document
        if doc:
            kwargs["attributes"] = doc.attributes
            kwargs["force_document"] = not (
                m.video or m.audio or m.voice or m.video_note or m.gif or m.sticker
            )
            if m.video or m.video_note or m.gif or m.audio:
                try:
                    thumb = await client.download_media(m, file=base + "_thumb", thumb=-1)
                    if thumb:
                        kwargs["thumb"] = thumb
                except Exception:
                    thumb = None
            if m.voice:
                kwargs["voice_note"] = True
            if m.video_note:
                kwargs["video_note"] = True
            if m.video:
                kwargs["supports_streaming"] = True

        caption = text if len(text) <= CAPTION_LIMIT else ""
        await tg_retry(
            client.send_file, "me", path,
            caption=caption,
            formatting_entities=m.entities if caption else None,
            **kwargs
        )
        if text and not caption:
            await _send_text(text, None)
        return True
    finally:
        _unlink(path, thumb)


async def _reupload_album(entity_id, unit):
    paths = []
    caps = []
    try:
        for m in unit:
            p = await tg_retry(client.download_media, m, file=str(MEDIA_DIR / f"exp_{entity_id}_{m.id}"))
            if p and Path(p).exists():
                paths.append(p)
                caps.append((m.message or "")[:CAPTION_LIMIT])
        if not paths:
            return 0
        plain_docs = all(m.document and not (m.video or m.audio or m.gif) for m in unit)
        await tg_retry(client.send_file, "me", paths, caption=caps, force_document=plain_docs)
        return len(paths)
    finally:
        _unlink(*paths)


async def _process_batch(entity, entity_id, buf, state, stats):
    # 1) تلاش برای فوروارد دسته‌ای
    if not state["restricted"] and not any(getattr(m, "noforwards", False) for m in buf):
        try:
            await tg_retry(client.forward_messages, "me", [m.id for m in buf], from_peer=entity)
            stats["forwarded"] += len(buf)
            return
        except Exception as e:
            name = type(e).__name__
            print("FORWARD FAILED:", name, str(e))
            if "RESTRICT" in name.upper() or "RESTRICT" in str(e).upper() or "NOFORWARD" in name.upper():
                state["restricted"] = True

    # 2) جایگزین: دانلود + آپلود
    for unit in _split_albums(buf):
        if EXPORT_STATE["cancel"]:
            return
        try:
            if len(unit) > 1:
                n = await _reupload_album(entity_id, unit)
                stats["uploaded"] += n
                stats["failed"] += len(unit) - n
            else:
                ok = await _reupload_single(entity_id, unit[0])
                if ok:
                    stats["uploaded"] += 1
                else:
                    stats["failed"] += 1
        except Exception as e:
            stats["failed"] += len(unit)
            print("EXPORT ITEM ERROR:", type(e).__name__, str(e))
        await asyncio.sleep(0.3)


async def export_chat_content(raw_target, status_msg=None, restart=False, limit=None):
    entity = await resolve_export_target(raw_target)
    entity_id = tl_utils.get_peer_id(entity)
    label = tl_utils.get_display_name(entity) or str(entity_id)

    key = f"export_last:{entity_id}"
    last_id = 0 if restart else int(get_setting(key, "0") or 0)

    state = {"restricted": bool(getattr(entity, "noforwards", False))}
    stats = {"forwarded": 0, "uploaded": 0, "failed": 0}

    await tg_retry(client.send_message, "me", f"📥 شروع انتقال از «{label}»" + (f" (ادامه از پیام {last_id})" if last_id else ""))

    last_edit = 0.0
    buf = []
    last_processed = last_id

    async def flush():
        nonlocal buf, last_processed, last_edit
        if not buf:
            return
        await _process_batch(entity, entity_id, buf, state, stats)
        if not EXPORT_STATE["cancel"]:
            last_processed = buf[-1].id
            set_setting(key, last_processed)
        buf = []
        if status_msg and time.time() - last_edit > 8:
            last_edit = time.time()
            try:
                done = stats["forwarded"] + stats["uploaded"]
                await status_msg.edit(
                    f"⏳ در حال انتقال از «{label}»\n"
                    f"✅ {done} مورد (فوروارد: {stats['forwarded']} | دانلود/آپلود: {stats['uploaded']})\n"
                    f"⚠️ ناموفق: {stats['failed']}\n"
                    f"برای توقف: توقف انتقال"
                )
            except Exception:
                pass

    async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=limit):
        if EXPORT_STATE["cancel"]:
            break
        if not msg or isinstance(msg, MessageEmpty) or getattr(msg, "action", None):
            continue
        if buf and len(buf) >= EXPORT_BATCH and (
            msg.grouped_id is None or msg.grouped_id != buf[-1].grouped_id
        ):
            await flush()
            if EXPORT_STATE["cancel"]:
                break
        buf.append(msg)

    if buf and not EXPORT_STATE["cancel"]:
        await flush()

    return stats, label


@client.on(events.NewMessage(outgoing=True))
async def saved_messages(event):
    if event.chat_id != ME_ID:
        return

    # پیام‌های فوروارد‌شده یا خروجی خودِ انتقال نباید به‌عنوان دستور اجرا بشن
    if event.message.fwd_from:
        return

    text = (event.raw_text or "").strip()
    if not text:
        return

    cmd = text.strip()

    if cmd == "توقف انتقال":
        if EXPORT_STATE["running"]:
            EXPORT_STATE["cancel"] = True
            await event.reply("⛔ در حال توقف انتقال...")
        else:
            await event.reply("انتقالی در حال اجرا نیست.")
        return

    # وسط انتقال، متن‌هایی که خودِ انتقال می‌فرسته (آپلود‌شده‌ها) دستور حساب نمی‌شن
    if EXPORT_STATE["running"]:
        return

    spam_match = re.fullmatch(r"اسپم\s+([0-9۰-۹]+)\s+([\s\S]+)", cmd)
    if spam_match:
        count = int(normalize_number(spam_match.group(1)))
        payload = spam_match.group(2).strip()
        if 1 <= count <= 30 and payload:
            for _ in range(count):
                await client.send_message(event.chat_id, payload)
            await event.reply("انجام شد.")
        else:
            await event.reply("تعداد باید بین ۱ تا ۳۰ باشد.")
        return

    if cmd in ("فونت روشن", "فونت خاموش"):
        set_setting("chat_font_enabled", "1" if cmd == "فونت روشن" else "0")
        await event.reply("حالت فونت " + ("روشن شد." if cmd == "فونت روشن" else "خاموش شد."))
        return
    font_commands = {
        "فونت کشیده": "keshide",
        "فونت بولد": "bold",
        "فونت ایتالیک": "italic",
        "فونت بولد کشیده": "bold_keshide",
        "فونت ساده": "normal",
    }
    if cmd in font_commands:
        set_setting("chat_font_style", font_commands[cmd])
        await event.reply("فونت انتخاب شد: " + cmd.replace("فونت ", ""))
        return

    export_match = re.fullmatch(r"(?:خالی کردن محتوا|خالی‌کردن محتوا)\s+(.+)", cmd)
    if export_match:
        if EXPORT_STATE["running"]:
            await event.reply("یک انتقال در حال اجراست. برای توقف: <code>توقف انتقال</code>", parse_mode="html")
            return
        args = export_match.group(1).strip()
        restart = "از اول" in args
        args = args.replace("از اول", " ").strip()
        limit = None
        parts = args.split()
        target = parts[0]
        for extra in parts[1:]:
            n = normalize_number(extra)
            if n.isdigit():
                limit = int(n)
        EXPORT_STATE["running"] = True
        EXPORT_STATE["cancel"] = False
        try:
            status_msg = await event.reply("⏳ در حال آماده‌سازی انتقال...")
            stats, label = await export_chat_content(target, status_msg, restart=restart, limit=limit)
            done = stats["forwarded"] + stats["uploaded"]
            head = "⛔ انتقال متوقف شد." if EXPORT_STATE["cancel"] else "✅ انتقال تمام شد."
            await event.reply(
                f"{head}\n«{label}»\n"
                f"✅ منتقل‌شده: {done} (فوروارد: {stats['forwarded']} | دانلود/آپلود: {stats['uploaded']})\n"
                f"⚠️ ناموفق: {stats['failed']}"
            )
        except Exception as e:
            print("EXPORT ERROR:", type(e).__name__, str(e))
            await event.reply(f"انتقال ناموفق بود: {type(e).__name__}\n{str(e)[:300]}")
        finally:
            EXPORT_STATE["running"] = False
            EXPORT_STATE["cancel"] = False
        return

    # -------- در حال منتظر گرفتن متن جدید پاسخ خودکار --------
    if get_setting("waiting_for_text", "0") == "1":
        if cmd == "لغو":
            set_setting("waiting_for_text", "0")
            await event.reply("❌ <b>لغو شد.</b>", parse_mode="html")
            return

        raw_text = event.message.message or ""
        entities = event.message.entities or []
        set_setting("auto_text", raw_text)
        set_auto_entities(entities)
        set_setting("waiting_for_text", "0")
        await event.reply(
            "✅ <b>متن پاسخ خودکار ذخیره شد</b> (دقیقاً با همون فرمتی که فرستادی).",
            parse_mode="html"
        )
        return

    # -------- HELP --------
    if cmd in ("دستورات", "لیست دستورات", "لیست", "منو"):
        await event.reply(HELP, parse_mode="html")
        return

    # -------- AUTO REPLY --------
    if cmd == "روشن":
        set_setting("auto_reply", "1")
        await event.reply("🟢 <b>پاسخ خودکار:</b> <code>روشن</code>", parse_mode="html")
        return

    if cmd == "خاموش":
        set_setting("auto_reply", "0")
        await event.reply("🔴 <b>پاسخ خودکار:</b> <code>خاموش</code>", parse_mode="html")
        return

    if cmd == "متن":
        raw_text = get_setting("auto_text", "")
        if not raw_text:
            await event.reply(
                "📝 هنوز متنی تنظیم نشده.\n\nبرای تنظیم:\n<code>تغییر متن</code>",
                parse_mode="html"
            )
            return

        style = get_setting("auto_style", "as_typed")
        style_fa = {"as_typed": "ساده (دقیقاً مثل نوشتنت)", "bold": "بولد", "quote": "نقل‌قول"}.get(style, style)

        await event.reply(
            f"📝 <b>متن فعلی پاسخ خودکار</b>\n<b>استایل:</b> {style_fa}\n━━━━━━━━━━━━━━━━━━",
            parse_mode="html"
        )
        preview_text, preview_entities = get_auto_reply_payload()
        await client.send_message("me", preview_text, formatting_entities=preview_entities)
        return

    if cmd == "تغییر متن":
        set_setting("waiting_for_text", "1")
        await event.reply(
            "✍️ <b>پیام بعدیت رو به هر شکلی که می‌خوای بفرست</b> "
            "(بولد، ایتالیک، نقل‌قول، لینک، ساده...) — دقیقاً همونجوری ذخیره می‌شه.\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="html"
        )
        return

    if cmd == "استایل ساده":
        set_setting("auto_style", "as_typed")
        await event.reply("✅ <b>استایل پاسخ خودکار:</b> ساده (دقیقاً مثل نوشتنت)", parse_mode="html")
        return

    if cmd == "استایل بولد":
        set_setting("auto_style", "bold")
        await event.reply("✅ <b>استایل پاسخ خودکار:</b> بولد", parse_mode="html")
        return

    if cmd in ("استایل نقل قول", "استایل نقل‌قول"):
        set_setting("auto_style", "quote")
        await event.reply("✅ <b>استایل پاسخ خودکار:</b> نقل‌قول", parse_mode="html")
        return

    # -------- NAME CLOCK --------
    if cmd.startswith("اسم "):
        arg = text[4:].strip()
        norm = normalize_number(arg)

        if norm.isdigit() and 1 <= int(norm) <= len(FONT_MAPS):
            font = int(norm)
            ok = await enable_name_clock(font=font)
            preview = build_name_text(font)
            await event.reply(
                f"🟢 <b>ساعت در اسم فعال شد (فونت {font}).</b>\n<code>{preview}</code>"
                if ok else "❌ <b>فعال‌سازی ناموفق بود.</b>",
                parse_mode="html"
            )
        elif arg:
            ok = await enable_name_clock(label=arg)
            font = int(get_setting("name_font", "1"))
            preview = build_name_text(font)
            await event.reply(
                f"🟢 <b>برچسب اسم تغییر کرد و ساعت فعال شد.</b>\n<code>{preview}</code>"
                if ok else "❌ <b>فعال‌سازی ناموفق بود.</b>",
                parse_mode="html"
            )
        return

    if cmd == "حذف اسم":
        await disable_name_clock()
        await event.reply("🔴 <b>ساعت در اسم خاموش شد و اسم قبلی برگشت.</b>", parse_mode="html")
        return

    # -------- BIO CLOCK --------
    if cmd.startswith("بیو "):
        arg = normalize_number(text[4:].strip())
        if arg.isdigit() and 1 <= int(arg) <= len(FONT_MAPS):
            font = int(arg)
            ok = await enable_bio_clock(font)
            await event.reply(
                f"🟢 <b>ساعت در بیو فعال شد (فونت {font}).</b>" if ok else "❌ <b>فعال‌سازی ناموفق بود.</b>",
                parse_mode="html"
            )
        return

    if cmd == "حذف بیو":
        await disable_bio_clock()
        await event.reply("🔴 <b>ساعت در بیو خاموش شد و بیوی قبلی برگشت.</b>", parse_mode="html")
        return

    # -------- ARCHIVE TOGGLE --------
    if cmd == "آرشیو روشن":
        set_setting("archive_enabled", "1")
        await event.reply("🟢 <b>آرشیو پیام‌های حذف‌شده روشن شد.</b>", parse_mode="html")
        return

    if cmd == "آرشیو خاموش":
        set_setting("archive_enabled", "0")
        await event.reply("🔴 <b>آرشیو پیام‌های حذف‌شده خاموش شد.</b>", parse_mode="html")
        return

    # -------- STATUS / ACCOUNT --------
    if cmd == "وضعیت":
        await event.reply(await status(), parse_mode="html")
        return

    if cmd == "اکانت":
        await event.reply(await account_info(), parse_mode="html")
        return

    # -------- BLOCK LIST / MUTE LIST --------
    if cmd == "بلاک‌ها":
        await event.reply(block_list(), parse_mode="html")
        return

    if cmd == "سکوت‌ها":
        await event.reply(mute_list(), parse_mode="html")
        return

    # -------- BLOCK / UNBLOCK BY VALUE --------
    if cmd.startswith("بلاک "):
        value = text[5:].strip()
        user = await resolve_user(value)
        result = await block_user(user)
        await event.reply(result, parse_mode="html")
        return

    if cmd.startswith("انبلاک "):
        value = text[7:].strip()
        user = await resolve_user(value)
        result = await unblock_user(user)
        await event.reply(result, parse_mode="html")
        return


# ============================================================
# PRIVATE CHAT (داخل PV هر شخص)
# ============================================================

@client.on(events.NewMessage)
async def private_handler(event):
    if not event.is_private:
        return

    if event.chat_id == ME_ID:
        return

    text = (event.raw_text or "").strip()

    # =====================================================
    # OUTGOING (پیام‌هایی که خودم داخل PV می‌فرستم)
    # =====================================================

    if event.out:

        if get_setting("chat_font_enabled", "0") == "1" and text and not text.startswith(("فونت ", "بلاک", "انبلاک", "حذف", "سکوت")):
            style = get_setting("chat_font_style", "normal")
            if style != "normal":
                try:
                    new_text, entities = stylize_chat_text(text, style)
                    await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
                    await client.send_message(event.chat_id, new_text, formatting_entities=entities)
                except Exception as e:
                    print("FONT SEND ERROR:", type(e).__name__, str(e))
                return

        # -------- بلاک همین PV --------
        if text == "بلاک":
            try:
                user = await client.get_entity(event.chat_id)
                result = await block_user(user)
                suppress_delete_ids.add(int(event.message.id))
                await event.reply(result, parse_mode="html")
            except Exception as e:
                print("BLOCK PV ERROR:", e)
            return

        # -------- سکوت همین PV --------
        if text == "سکوت":
            try:
                user = await client.get_entity(event.chat_id)
                add_mute(user)
                suppress_delete_ids.add(int(event.message.id))
                await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
                await client.send_message(
                    "me",
                    f"🔇 <b>سکوت فعال شد برای:</b>\n{user_name(user)}\n<code>{user.id}</code>",
                    parse_mode="html"
                )
            except Exception as e:
                print("MUTE PV ERROR:", e)
            return

        # -------- حذف سکوت همین PV --------
        if text == "حذف سکوت":
            try:
                remove_mute(event.chat_id)
                suppress_delete_ids.add(int(event.message.id))
                await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
                await client.send_message("me", "🔊 <b>سکوت این مخاطب برداشته شد.</b>", parse_mode="html")
            except Exception as e:
                print("UNMUTE PV ERROR:", e)
            return

        # -------- حذف سریع با ریپلای روی یک پیام خاص --------
        if text == "حذف" and event.is_reply:
            await delete_replied_message(event)
            return

        # -------- حذف N پیام --------
        if text.startswith("حذف"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return
            value = normalize_number(parts[1].strip())
            if not value.isdigit():
                return
            await delete_private_messages(event, int(value))
            return

        return

    # =====================================================
    # INCOMING (پیام‌هایی که طرف مقابل می‌فرستد)
    # =====================================================

    sender = await event.get_sender()
    if not sender:
        return

    save_chat(sender, text)

    # -------- اگر ساکت است: کش کن و پاک کن برای هردو --------
    if is_muted(sender.id):
        try:
            await cache_incoming_message(event)
        except Exception:
            pass
        try:
            suppress_delete_ids.add(int(event.message.id))
            await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
        except Exception as e:
            print("MUTE DELETE ERROR:", e)
        return

    # -------- بلاک از PV --------
    if text == "بلاک":
        result = await block_user(sender)
        await event.reply(result, parse_mode="html")
        return

    # -------- کش کردن پیام (متن/عکس/فیلم/ویس/زمان‌دار) برای آرشیو حذف --------
    try:
        await cache_incoming_message(event)
    except Exception:
        pass

    # =====================================================
    # AUTO REPLY (حداکثر هر ۱۰ دقیقه یکبار، با تاخیر تصادفی تا ۲۰ ثانیه)
    # =====================================================

    if get_setting("auto_reply", "0") != "1":
        return

    last = get_last_auto_reply(sender.id)
    if time.time() - last < AUTO_REPLY_COOLDOWN:
        return

    asyncio.create_task(send_delayed_autoreply(event, sender.id))


# ============================================================
# DELETED MESSAGE EVENT (برای آرشیو)
# ============================================================

@client.on(events.MessageDeleted())
async def deleted_handler(event):
    if get_setting("archive_enabled", "1") != "1":
        return

    for message_id in (event.deleted_ids or []):
        message_id = int(message_id)

        if message_id in suppress_delete_ids:
            suppress_delete_ids.discard(message_id)
            continue

        try:
            row = db.execute("""
            SELECT msg_id, chat_id, sender_id, name, username, text, media_type, media_path, created_at
            FROM deleted_archive WHERE msg_id=?
            """, (message_id,)).fetchone()

            if not row:
                continue

            db.execute("DELETE FROM deleted_archive WHERE msg_id=?", (message_id,))
            db.commit()

            await send_deleted_archive(row)
        except Exception as e:
            print("DELETED HANDLER ERROR:", e)


# ============================================================
# START
# ============================================================

async def main():
    global ME_ID

    print("=================================")
    print("        MAXO SELF - GOD")
    print("=================================")
    print("Session:", "SESSION_STRING (env)" if SESSION_STRING else SESSION + ".session")
    print("Database:", DB_FILE)
    print("Data dir:", DATA_DIR)

    await client.start()

    me = await client.get_me()
    ME_ID = me.id

    print()
    print("Logged in:", user_name(me))
    print("Username:", user_username(me))
    print("ID:", ME_ID)
    print()
    print("MAXO SELF IS RUNNING...")

    asyncio.create_task(clock_loop())
    asyncio.create_task(archive_cleanup_loop())

    try:
        await client.send_message(
            "me",
            (
                "⚡ <b>MAXO SELF فعال شد.</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "<b>وضعیت:</b> 🟢 فعال\n"
                f"<b>پاسخ خودکار:</b> "
                f"{'🟢 روشن' if get_setting('auto_reply') == '1' else '🔴 خاموش'}\n\n"
                "برای مشاهده تمام دستورات:\n"
                "<code>دستورات</code>\n\n"
                "━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode="html"
        )
    except Exception as e:
        print("STARTUP ERROR:", e)

    await client.run_until_disconnected()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMAXO stopped.")
    except Exception as e:
        print("\nFATAL ERROR:")
        print(type(e).__name__, str(e))
