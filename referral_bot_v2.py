
import asyncio
import html
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, EditMessageText
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ====================== SOZLAMALAR ======================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

_payout = os.getenv("PAYOUT_CHANNEL", "").strip()
PAYOUT_CHANNEL = int(_payout) if _payout.lstrip("-").isdigit() else _payout

DB_PATH = os.getenv("DB_PATH", "referral.db")

MIN_WITHDRAW = 5000
HOLD_DAYS = 1
UZ_TZ = timezone(timedelta(hours=5))

# ====================== PREMIUM EMOJILAR ======================

PREMIUM_EMOJI = {
    "🆔": "5334890573281114250",
    "👋": "5213124260414889588",
    "🆕": "5866494420857131357",
    "🔗": "5409257566939134596",
    "📊": "5231200819986047254",
    "🕐": "5408910404732595664",
    "💎": "5059923597305316725",
    "👥": "5316740582654112585",
    "📢": "5217943819311389632",
    "💰": "5059989537938211890",
    "💸": "5267027242440992339",
    "ℹ️": "5334544901428229844",
    "💳": "5316523549366716514",
    "⚙️": "5350396951407895212",
    "➕": "5397916757333654639",
    "➖": "5382261056078881010",
    "👤": "5317006024517912643",
    "✅": "5282892142962811394",
    "❌": "5273914604752216432",
    "🤖": "5327820601845369150",
    "🎁": "5427315847129478207",
    "⏳": "5213452215527677338",
    "⚠️": "5269744182917866822",
}


def premium_text(text: str) -> str:
    """Oddiy emojilarni Telegram Premium emoji entitylariga almashtiradi."""
    placeholders = {}

    for i, (emoji, emoji_id) in enumerate(
        sorted(PREMIUM_EMOJI.items(), key=lambda x: len(x[0]), reverse=True)
    ):
        placeholder = f"PREMIUMPLACEHOLDER{i}X"
        text = text.replace(emoji, placeholder)
        placeholders[placeholder] = (
            f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
        )

    text = html.escape(text)

    for placeholder, replacement in placeholders.items():
        text = text.replace(placeholder, replacement)

    return text


class PremiumBot(Bot):
    """Xabarlarni Premium emoji bilan yuboradi."""

    async def __call__(self, method, request_timeout=None):

        if isinstance(method, (SendMessage, EditMessageText)):
            text = getattr(method, "text", None)

            # Telegram kanallariga Premium emoji yuborilmaydi.
            # Bot API custom emoji uchun shaxsiy, guruh va superguruh chatlarini qo'llaydi.
            is_payout_channel = (
                isinstance(method, SendMessage)
                and str(method.chat_id) == str(PAYOUT_CHANNEL)
            )

            if text and not is_payout_channel:
                method.text = premium_text(text)
                method.parse_mode = "HTML"

        return await super().__call__(method, request_timeout)


logging.basicConfig(level=logging.INFO)
router = Router()


# ====================== YORDAMCHI ======================

def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def mask_name(username: str | None, uid: int) -> str:
    if username:
        if len(username) <= 3:
            return f"@{username[0]}***"
        return f"@{username[:2]}***{username[-1]}"
    return f"ID ***{str(uid)[-3:]}"


# ====================== MA'LUMOTLAR BAZASI ======================

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def db_init():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            referrer_id INTEGER,
            balance INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            title TEXT,
            slots_left INTEGER NOT NULL,
            reward INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referred_id INTEGER NOT NULL,
            referrer_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reward INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            release_at TEXT NOT NULL,
            UNIQUE (referred_id, campaign_id)
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            card TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        """)


def user_add(uid, username, ref):
    with db() as con:
        if con.execute(
            "SELECT 1 FROM users WHERE user_id=?", (uid,)
        ).fetchone():
            return False

        if ref is None or ref == uid or not con.execute(
            "SELECT 1 FROM users WHERE user_id=?", (ref,)
        ).fetchone():
            ref = None

        con.execute(
            "INSERT INTO users "
            "(user_id, username, referrer_id, balance, joined_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (uid, username, ref, now()),
        )
        return True


def user_exists(uid):
    with db() as con:
        return con.execute(
            "SELECT 1 FROM users WHERE user_id=?", (uid,)
        ).fetchone() is not None


def admin_change_balance(uid, amount):
    with db() as con:
        user = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (uid,)
        ).fetchone()

        if not user:
            return None

        new_balance = user["balance"] + amount

        if new_balance < 0:
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, uid),
        )
        return new_balance


def channels_active():
    with db() as con:
        return con.execute(
            "SELECT * FROM campaigns "
            "WHERE active=1 AND slots_left>0 ORDER BY id"
        ).fetchall()


def channel_get(cid):
    with db() as con:
        return con.execute(
            "SELECT * FROM campaigns WHERE id=?", (cid,)
        ).fetchone()


def channel_add(channel, title, slots, reward):
    with db() as con:
        cur = con.execute(
            "INSERT INTO campaigns "
            "(channel, title, slots_left, reward, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, title, slots, reward, now()),
        )
        return cur.lastrowid


def referral_claim(uid, cid):
    with db() as con:
        u = con.execute(
            "SELECT referrer_id FROM users WHERE user_id=?", (uid,)
        ).fetchone()

        if not u or not u["referrer_id"]:
            return "no_ref", None, 0

        c = con.execute(
            "SELECT * FROM campaigns WHERE id=? AND active=1",
            (cid,),
        ).fetchone()

        if not c or c["slots_left"] <= 0:
            return "closed", None, 0

        if con.execute(
            "SELECT 1 FROM referrals "
            "WHERE referred_id=? AND campaign_id=?",
            (uid, cid),
        ).fetchone():
            return "exists", None, 0

        release = (
            datetime.now() + timedelta(days=HOLD_DAYS)
        ).isoformat(timespec="seconds")

        con.execute(
            "INSERT INTO referrals "
            "(referred_id, referrer_id, campaign_id, reward, created_at, release_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, u["referrer_id"], cid, c["reward"], now(), release),
        )

        con.execute(
            "UPDATE campaigns SET slots_left=slots_left-1 WHERE id=?",
            (cid,),
        )

        return "ok", u["referrer_id"], c["reward"]


def referrals_due():
    with db() as con:
        return con.execute(
            "SELECT r.*, c.channel, c.title "
            "FROM referrals r JOIN campaigns c ON c.id=r.campaign_id "
            "WHERE r.status='pending' AND r.release_at<=?",
            (now(),),
        ).fetchall()


def referral_finish(rid, still_member):
    with db() as con:
        r = con.execute(
            "SELECT * FROM referrals WHERE id=? AND status='pending'",
            (rid,),
        ).fetchone()

        if not r:
            return

        if still_member:
            con.execute(
                "UPDATE referrals SET status='paid' WHERE id=?", (rid,)
            )
            con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (r["reward"], r["referrer_id"]),
            )
        else:
            con.execute(
                "UPDATE referrals SET status='cancelled' WHERE id=?",
                (rid,),
            )
            con.execute(
                "UPDATE campaigns SET slots_left=slots_left+1 WHERE id=?",
                (r["campaign_id"],),
            )


def user_stats(uid):
    with db() as con:
        u = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (uid,)
        ).fetchone()

        row = con.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status='pending' THEN reward END),0) "
            "AS pending_sum, "
            "COALESCE(SUM(CASE WHEN status='paid' THEN 1 END),0) "
            "AS paid_cnt "
            "FROM referrals WHERE referrer_id=?",
            (uid,),
        ).fetchone()

        friends = con.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,)
        ).fetchone()[0]

        return {
            "balance": u["balance"] if u else 0,
            "pending_sum": row["pending_sum"],
            "paid_cnt": row["paid_cnt"],
            "friends": friends,
        }


def withdraw_create(uid, card):
    with db() as con:
        u = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (uid,)
        ).fetchone()

        if not u or u["balance"] < MIN_WITHDRAW:
            return None

        amount = u["balance"]

        con.execute(
            "UPDATE users SET balance=0 WHERE user_id=?", (uid,)
        )

        cur = con.execute(
            "INSERT INTO withdrawals "
            "(user_id, amount, card, created_at) VALUES (?, ?, ?, ?)",
            (uid, amount, card, now()),
        )

        return cur.lastrowid, amount


def withdraw_finish(wid, paid):
    with db() as con:
        w = con.execute(
            "SELECT * FROM withdrawals "
            "WHERE id=? AND status='pending'",
            (wid,),
        ).fetchone()

        if not w:
            return None

        if paid:
            con.execute(
                "UPDATE withdrawals SET status='paid' WHERE id=?",
                (wid,),
            )
        else:
            con.execute(
                "UPDATE withdrawals SET status='rejected' WHERE id=?",
                (wid,),
            )
            con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (w["amount"], w["user_id"]),
            )

        return w


# ====================== TELEGRAM TEKSHIRUVI ======================

async def is_member(bot, channel, uid):
    try:
        m = await bot.get_chat_member(channel, uid)
        return m.status in ("member", "administrator", "creator")
    except (TelegramBadRequest, TelegramForbiddenError):
        return None


async def safe_send(bot, uid, text, **kwargs):
    try:
        await bot.send_message(uid, text, **kwargs)
    except Exception:
        logging.exception("Xabar yuborib bo'lmadi: %s", uid)


async def notify_new_user(bot, user):
    if is_admin(user.id):
        return

    with db() as con:
        row = con.execute(
            "SELECT referrer_id FROM users WHERE user_id=?",
            (user.id,),
        ).fetchone()

        total = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        ref_text = "yo'q (havolasiz kirgan)"

        if row and row["referrer_id"]:
            ref = con.execute(
                "SELECT username FROM users WHERE user_id=?",
                (row["referrer_id"],),
            ).fetchone()

            ref_name = (
                f"@{ref['username']}"
                if ref and ref["username"]
                else "username yo'q"
            )

            ref_text = f"{ref_name} (ID {row['referrer_id']})"

    username = f"@{user.username}" if user.username else "username yo'q"

    await safe_send(
        bot,
        ADMIN_ID,
        "🆕 Botga yangi foydalanuvchi qo'shildi\n\n"
        f"👤 Ism: {user.full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: {user.id}\n"
        f"👥 Taklif qilgan: {ref_text}\n\n"
        f"📊 Jami foydalanuvchilar: {total} ta",
    )


async def post_payout(bot, w):
    if not PAYOUT_CHANNEL:
        return

    with db() as con:
        row = con.execute(
            "SELECT username FROM users WHERE user_id=?",
            (w["user_id"],),
        ).fetchone()

    username = row["username"] if row else None
    who = f"@{username}" if username else mask_name(None, w["user_id"])

    me = await bot.me()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Botga o'tish",
                    url=f"https://t.me/{me.username}",
                    icon_custom_emoji_id="5327820601845369150",
                    style="danger",
                )
            ]
        ]
    )

    text = (
        "✅ To'lov amalga oshirildi\n\n"
        f"👤 {who}\n"
        f"💸 {fmt(w['amount'])} so'm yechib oldi\n"
        f"🕐 {datetime.now(UZ_TZ).strftime('%d.%m.%Y %H:%M')}"
    )

    try:
        await bot.send_message(PAYOUT_CHANNEL, text, reply_markup=kb)
    except Exception:
        logging.exception("To'lovlar kanaliga yuborib bo'lmadi")
        await safe_send(
            bot,
            ADMIN_ID,
            "⚠️ To'lovlar kanaliga xabar yuborib bo'lmadi. "
            "PAYOUT_CHANNEL va bot adminligini tekshiring.",
        )


# ====================== TUGMALAR ======================

def main_menu(is_admin_user=False):
    keyboard = [
        [
            KeyboardButton(
                text="Do'stlarni taklif qilish",
                icon_custom_emoji_id="5316740582654112585",
            )
        ],
        [
            KeyboardButton(
                text="Kanallar",
                icon_custom_emoji_id="5217943819311389632",
            ),
            KeyboardButton(
                text="Balans",
                icon_custom_emoji_id="5059989537938211890",
            ),
        ],
        [
            KeyboardButton(
                text="Yechib olish",
                icon_custom_emoji_id="5267027242440992339",
            ),
            KeyboardButton(
                text="Qoidalar",
                icon_custom_emoji_id="5334544901428229844",
            ),
        ],
        [
            KeyboardButton(
                text="To'lovlar kanali",
                icon_custom_emoji_id="5316523549366716514",
            )
        ],
    ]

    if is_admin_user:
        keyboard.append(
            [
                KeyboardButton(
                    text="Admin panel",
                    icon_custom_emoji_id="5350396951407895212",
                )
            ]
        )

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Bekor qilish",
                    icon_custom_emoji_id="5273914604752216432",
                )
            ]
        ],
        resize_keyboard=True,
    )


class Withdraw(StatesGroup):
    card = State()


class AdminBalance(StatesGroup):
    add_user_id = State()
    add_amount = State()
    subtract_user_id = State()
    subtract_amount = State()
    check_user_id = State()


RULES = (
    "ℹ️ Qoidalar\n\n"
    "• Siz do'stlaringizni o'z havolangiz orqali botga taklif qilasiz.\n"
    "• Do'stingiz «Kanallar» bo'limidagi kanalga obuna bo'lgan zahoti sizga pul yoziladi "
    "(⏳ tekshiruvda).\n"
    f"• {HOLD_DAYS} kun o'tgach, do'stingiz hali kanalda bo'lsa, pul tasdiqlanadi va "
    "yechib olish mumkin bo'ladi. Sizga xabar yuboriladi.\n"
    f"• Agar do'stingiz {HOLD_DAYS} kun ichida kanalni tark etsa, yozilgan pul "
    "olib tashlanadi (jarima) va sizga xabar yuboriladi.\n"
    f"• Yechib olish faqat tasdiqlangan summadan mumkin. Minimal summa: "
    f"{fmt(MIN_WITHDRAW)} so'm. Pul admin tomonidan kartaga o'tkaziladi.\n"
    "• To'lov amalga oshirilgach, username'ingiz (karta raqamisiz) "
    "to'lovlar kanalida ko'rsatiladi.\n"
    "• Mukofotlar kanal egalari to'lovi hisobidan beriladi. "
    "Botga hech qanday pul kiritish talab qilinmaydi.\n"
    "• O'zingizni taklif qilish, soxta akkauntlar va aldov taqiqlanadi, "
    "bunday hisoblar bloklanadi."
)


# ====================== FOYDALANUVCHI QISMI ======================

@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()

    ref = (
        int(command.args)
        if command.args and command.args.isdigit()
        else None
    )

    is_new = user_add(
        message.from_user.id,
        message.from_user.username,
        ref,
    )

    if is_new:
        await notify_new_user(bot, message.from_user)

    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Bu bot orqali do'stlaringizni kanallarga taklif qilib pul ishlashingiz mumkin.\n\n"
        "1) «Do'stlarni taklif qilish» orqali havolangizni oling\n"
        "2) Do'stlaringizga yuboring\n"
        "3) Ular kanallarga obuna bo'lsa, hisobingizga pul tushadi\n\n"
        "Batafsil qoidalar: «Qoidalar»",
        reply_markup=main_menu(is_admin(message.from_user.id)),
    )


@router.message(F.text == "Qoidalar")
async def rules(message: Message):
    await message.answer(RULES)


# ====================== ADMIN PANEL ======================

@router.message(F.text == "Admin panel")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Hisobga pul qo‘shish",
                    icon_custom_emoji_id="5397916757333654639",
                )
            ],
            [
                KeyboardButton(
                    text="Hisobdan pul olish",
                    icon_custom_emoji_id="5382261056078881010",
                )
            ],
            [
                KeyboardButton(
                    text="Balansni tekshirish",
                    icon_custom_emoji_id="5317006024517912643",
                )
            ],
            [KeyboardButton(text="Asosiy menyu")],
        ],
        resize_keyboard=True,
    )

    await message.answer("⚙️ Admin panel", reply_markup=kb)


@router.message(F.text == "Asosiy menyu")
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Asosiy menyu:",
        reply_markup=main_menu(is_admin(message.from_user.id)),
    )


@router.message(F.text == "Hisobga pul qo‘shish")
async def admin_add_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminBalance.add_user_id)
    await message.answer(
        "Pul qo‘shmoqchi bo‘lgan foydalanuvchining Telegram ID raqamini yuboring:"
    )


@router.message(AdminBalance.add_user_id)
async def admin_add_get_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text or ""

    if not text.isdigit():
        await message.answer("Faqat raqamli Telegram ID yuboring.")
        return

    uid = int(text)

    if not user_exists(uid):
        await message.answer(
            "Foydalanuvchi topilmadi. U avval botni ishga tushirgan bo‘lishi kerak."
        )
        return

    await state.update_data(target_uid=uid)
    await state.set_state(AdminBalance.add_amount)
    await message.answer("Qancha pul qo‘shmoqchisiz? Summani so‘mda yuboring:")


@router.message(AdminBalance.add_amount)
async def admin_add_get_amount(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text or ""

    if not text.isdigit() or int(text) <= 0:
        await message.answer("Summani musbat raqamda yuboring.")
        return

    amount = int(text)
    data = await state.get_data()
    uid = data["target_uid"]

    new_balance = admin_change_balance(uid, amount)

    if new_balance is None:
        await message.answer("Foydalanuvchi topilmadi.")
    else:
        await message.answer(
            f"Foydalanuvchi ID: {uid}\n"
            f"Qo‘shilgan pul: {fmt(amount)} so‘m\n"
            f"Yangi balans: {fmt(new_balance)} so‘m"
        )

        await safe_send(
            bot,
            uid,
            f"💰 Hisobingizga {fmt(amount)} so'm qo'shildi (admin tomonidan).\n"
            f"Yangi balans: {fmt(new_balance)} so'm",
        )

    await state.clear()


@router.message(F.text == "Hisobdan pul olish")
async def admin_sub_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminBalance.subtract_user_id)
    await message.answer(
        "Hisobidan pul ayirmoqchi bo‘lgan foydalanuvchining Telegram ID raqamini yuboring:"
    )


@router.message(AdminBalance.subtract_user_id)
async def admin_sub_get_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text or ""

    if not text.isdigit():
        await message.answer("Faqat raqamli Telegram ID yuboring.")
        return

    uid = int(text)

    if not user_exists(uid):
        await message.answer("Foydalanuvchi topilmadi.")
        return

    await state.update_data(target_uid=uid)
    await state.set_state(AdminBalance.subtract_amount)
    await message.answer("Qancha pul ayirmoqchisiz? Summani so‘mda yuboring:")


@router.message(AdminBalance.subtract_amount)
async def admin_sub_get_amount(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text or ""

    if not text.isdigit() or int(text) <= 0:
        await message.answer("Summani musbat raqamda yuboring.")
        return

    amount = int(text)
    data = await state.get_data()
    uid = data["target_uid"]

    new_balance = admin_change_balance(uid, -amount)

    if new_balance is None:
        await message.answer("Foydalanuvchi topilmadi.")
    elif new_balance is False:
        await message.answer(
            "Foydalanuvchining tasdiqlangan balansida buncha pul yo‘q."
        )
    else:
        await message.answer(
            f"Foydalanuvchi ID: {uid}\n"
            f"Ayirilgan pul: {fmt(amount)} so‘m\n"
            f"Yangi balans: {fmt(new_balance)} so‘m"
        )

        await safe_send(
            bot,
            uid,
            f"➖ Hisobingizdan {fmt(amount)} so'm yechildi (admin tomonidan).\n"
            f"Yangi balans: {fmt(new_balance)} so'm",
        )

    await state.clear()


@router.message(F.text == "Balansni tekshirish")
async def admin_check_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminBalance.check_user_id)
    await message.answer(
        "Balansini tekshirmoqchi bo‘lgan foydalanuvchining Telegram ID raqamini yuboring:"
    )


@router.message(AdminBalance.check_user_id)
async def admin_check_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text or ""

    if not text.isdigit():
        await message.answer("Faqat raqamli Telegram ID yuboring.")
        return

    uid = int(text)

    with db() as con:
        user = con.execute(
            "SELECT username FROM users WHERE user_id=?", (uid,)
        ).fetchone()

    if not user:
        await message.answer("Bunday foydalanuvchi topilmadi.")
    else:
        stats = user_stats(uid)
        username = user["username"] or "Username yo‘q"

        await message.answer(
            f"👤 Foydalanuvchi: {username}\n"
            f"🆔 ID: {uid}\n"
            f"💰 Tasdiqlangan balans: {fmt(stats['balance'])} so‘m\n"
            f"⏳ Tekshiruvdagi pul: {fmt(stats['pending_sum'])} so‘m\n"
            f"👥 Taklif qilgan do‘stlari: {stats['friends']} ta"
        )

    await state.clear()


# ====================== KANALLAR VA TAKLIFLAR ======================

@router.message(F.text == "To'lovlar kanali")
async def payouts_channel(message: Message):
    if not PAYOUT_CHANNEL or not isinstance(PAYOUT_CHANNEL, str):
        await message.answer("To'lovlar kanali hozircha sozlanmagan.")
        return

    url = "https://t.me/" + PAYOUT_CHANNEL.lstrip("@")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="To'lovlar kanaliga o'tish",
                    url=url,
                    icon_custom_emoji_id="5316523549366716514",
                    style="danger",
                )
            ]
        ]
    )

    await message.answer(
        "To'lovlar kanalida amalga oshirilgan barcha to'lovlarni ko'rishingiz mumkin 👇",
        reply_markup=kb,
    )


@router.message(F.text == "Do'stlarni taklif qilish")
async def invite(message: Message, bot: Bot):
    user_add(
        message.from_user.id,
        message.from_user.username,
        None,
    )

    me = await bot.me()
    link = f"https://t.me/{me.username}?start={message.from_user.id}"

    per_friend = sum(c["reward"] for c in channels_active())
    s = user_stats(message.from_user.id)

    await message.answer(
        f"Sizning havolangiz:\n{link}\n\n"
        f"Hozirgi kanallarning hammasiga obuna bo'lgan har bir do'stingiz uchun "
        f"sizga {fmt(per_friend)} so'm yoziladi.\n\n"
        f"Taklif qilgan do'stlaringiz: {s['friends']} ta"
    )


@router.message(F.text == "Kanallar")
async def channels(message: Message):
    user_add(
        message.from_user.id,
        message.from_user.username,
        None,
    )

    items = channels_active()

    if not items:
        await message.answer(
            "Hozircha faol kanallar yo'q. Keyinroq qaytib keling."
        )
        return

    await message.answer(
        "Quyidagi kanallarga obuna bo'ling, so'ng «Tekshirish» tugmasini bosing.\n"
        "Bu sizni taklif qilgan do'stingizga pul yozilishiga yordam beradi."
    )

    for c in items:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Kanalga o'tish",
                        url=f"https://t.me/{c['channel'].lstrip('@')}",
                        icon_custom_emoji_id="5217943819311389632",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Tekshirish",
                        callback_data=f"check:{c['id']}",
                        icon_custom_emoji_id="5282892142962811394",
                        style="danger",
                    )
                ],
            ]
        )

        await message.answer(
            f"{c['title'] or c['channel']}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("check:"))
async def check_sub(call: CallbackQuery, bot: Bot):
    cid = int(call.data.split(":")[1])
    c = channel_get(cid)

    if not c:
        await call.answer("Kanal topilmadi.", show_alert=True)
        return

    uid = call.from_user.id

    user_add(uid, call.from_user.username, None)

    st = await is_member(bot, c["channel"], uid)

    if st is None:
        await call.answer(
            "Tekshirib bo'lmadi. Keyinroq urinib ko'ring.",
            show_alert=True,
        )
        return

    if not st:
        await call.answer(
            "Siz hali kanalga obuna bo'lmagansiz.",
            show_alert=True,
        )
        return

    status, referrer, reward = referral_claim(uid, cid)

    if status == "ok":
        await call.answer(
            "Obuna tasdiqlandi. Rahmat!",
            show_alert=True,
        )

        await safe_send(
            bot,
            referrer,
            f"🎉 Do'stingiz «{c['title'] or c['channel']}» kanaliga obuna bo'ldi.\n"
            f"+{fmt(reward)} so'm hisobingizga yozildi (⏳ tekshiruvda).\n\n"
            f"{HOLD_DAYS} kundan keyin, do'stingiz kanalda qolgan bo'lsa, "
            "pul tasdiqlanadi va yechib olish mumkin bo'ladi. "
            f"Agar {HOLD_DAYS} kun ichida kanalni tark etsa, "
            f"-{fmt(reward)} so'm jarima bo'ladi.",
        )

    elif status == "exists":
        await call.answer(
            "Bu kanal allaqachon hisobga olingan.",
            show_alert=True,
        )

    elif status == "closed":
        await call.answer(
            "Bu kanal uchun obunachi yig'ish yopilgan.",
            show_alert=True,
        )

    else:
        await call.answer(
            "Siz taklif havolasi orqali kelmagansiz, shuning uchun hech kimga "
            "mukofot yozilmaydi. Lekin o'z havolangizni olib, do'stlaringizni "
            "taklif qilishingiz mumkin.",
            show_alert=True,
        )


@router.message(F.text == "Balans")
async def balance(message: Message):
    user_add(
        message.from_user.id,
        message.from_user.username,
        None,
    )

    s = user_stats(message.from_user.id)
    total = s["balance"] + s["pending_sum"]

    await message.answer(
        f"💰 Jami: {fmt(total)} so'm\n\n"
        f"✅ Yechib olish mumkin: {fmt(s['balance'])} so'm\n"
        f"⏳ Tekshiruvda ({HOLD_DAYS} kunlik): "
        f"{fmt(s['pending_sum'])} so'm\n\n"
        f"👥 Do'stlaringiz: {s['friends']} ta\n"
        f"✅ Tasdiqlangan obunalar: {s['paid_cnt']} ta\n\n"
        f"Minimal yechib olish: {fmt(MIN_WITHDRAW)} so'm"
    )


# ====================== YECHIB OLISH ======================

@router.message(F.text == "Bekor qilish")
async def cancel(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "Bekor qilindi.",
        reply_markup=main_menu(is_admin(message.from_user.id)),
    )


@router.message(F.text == "Yechib olish")
async def withdraw_start(message: Message, state: FSMContext):
    user_add(
        message.from_user.id,
        message.from_user.username,
        None,
    )

    s = user_stats(message.from_user.id)

    if s["balance"] < MIN_WITHDRAW:
        await message.answer(
            f"Yechib olish mumkin bo'lgan summa: {fmt(s['balance'])} so'm.\n"
            f"Tekshiruvda: {fmt(s['pending_sum'])} so'm.\n\n"
            f"Yechib olish uchun tasdiqlangan summa kamida "
            f"{fmt(MIN_WITHDRAW)} so'm bo'lishi kerak."
        )
        return

    await state.set_state(Withdraw.card)

    await message.answer(
        f"{fmt(s['balance'])} so'm yechib olinadi.\n"
        "Pul o'tkaziladigan karta raqamini (16 raqam) yozing:",
        reply_markup=cancel_kb(),
    )


@router.message(Withdraw.card, F.text)
async def withdraw_card(message: Message, state: FSMContext, bot: Bot):
    card = message.text.replace(" ", "")

    if not (card.isdigit() and len(card) == 16):
        await message.answer(
            "Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qaytadan yozing."
        )
        return

    res = withdraw_create(message.from_user.id, card)

    await state.clear()

    if not res:
        await message.answer(
            "Tasdiqlangan balans yetarli emas.",
            reply_markup=main_menu(is_admin(message.from_user.id)),
        )
        return

    wid, amount = res

    await message.answer(
        f"✅ So'rov #{wid} qabul qilindi: {fmt(amount)} so'm.\n"
        "Admin tekshirib, kartangizga o'tkazadi.",
        reply_markup=main_menu(is_admin(message.from_user.id)),
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="To'landi",
                    callback_data=f"wpaid:{wid}",
                    icon_custom_emoji_id="5282892142962811394",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="Rad etish",
                    callback_data=f"wreject:{wid}",
                    icon_custom_emoji_id="5273914604752216432",
                    style="danger",
                ),
            ]
        ]
    )

    who = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else f"ID {message.from_user.id}"
    )

    await bot.send_message(
        ADMIN_ID,
        f"💸 Yechib olish #{wid}\n"
        f"Foydalanuvchi: {who}\n"
        f"Summa: {fmt(amount)} so'm\n"
        f"Karta: {card}",
        reply_markup=kb,
    )


# ====================== ADMIN TO'LOVLARI ======================

@router.callback_query(F.data.startswith("wpaid:"))
async def w_paid(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return

    wid = int(call.data.split(":")[1])
    w = withdraw_finish(wid, True)

    if not w:
        await call.answer(
            "Bu so'rov allaqachon ko'rib chiqilgan.",
            show_alert=True,
        )
        return

    await safe_send(
        bot,
        w["user_id"],
        f"✅ So'rov #{wid}: {fmt(w['amount'])} so'm kartangizga o'tkazildi.",
    )

    await post_payout(bot, w)

    await call.message.edit_text(
        call.message.text + "\n\n✅ TO'LANDI"
    )

    await call.answer()


@router.callback_query(F.data.startswith("wreject:"))
async def w_reject(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return

    wid = int(call.data.split(":")[1])
    w = withdraw_finish(wid, False)

    if not w:
        await call.answer(
            "Bu so'rov allaqachon ko'rib chiqilgan.",
            show_alert=True,
        )
        return

    await safe_send(
        bot,
        w["user_id"],
        f"❌ So'rov #{wid} rad etildi. "
        f"{fmt(w['amount'])} so'm balansingizga qaytarildi.",
    )

    await call.message.edit_text(
        call.message.text + "\n\n❌ RAD ETILDI"
    )

    await call.answer()


# ====================== ADMIN BUYRUQLARI ======================

@router.message(Command("addchannel"))
async def add_channel(message: Message, command: CommandObject, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    parts = (command.args or "").split()

    if (
        len(parts) != 3
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        await message.answer(
            "Format: /addchannel @kanal SONI MUKOFOT\n"
            "Masalan: /addchannel @mening_kanalim 100 300"
        )
        return

    channel = (
        parts[0]
        if parts[0].startswith("@")
        else "@" + parts[0]
    )

    slots = int(parts[1])
    reward = int(parts[2])

    if slots <= 0 or reward <= 0:
        await message.answer(
            "Obunachi soni va mukofot 0 dan katta bo'lishi kerak."
        )
        return

    try:
        me = await bot.me()

        member = await bot.get_chat_member(channel, me.id)

        if member.status not in ("administrator", "creator"):
            await message.answer(
                "Bot bu kanalda admin emas. Avval botni kanalga admin qiling."
            )
            return

        chat = await bot.get_chat(channel)

    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer(
            "Kanal topilmadi yoki bot unga kira olmayapti. "
            "Username'ni va bot adminligini tekshiring."
        )
        return

    cid = channel_add(
        channel,
        chat.title or channel,
        slots,
        reward,
    )

    await message.answer(
        f"✅ Kanal #{cid} qo'shildi\n"
        f"Kanal: {channel}\n"
        f"Obunachi soni: {slots}\n"
        f"Taklif qilganga mukofot: {fmt(reward)} so'm / obunachi"
    )


@router.message(Command("channels"))
async def list_channels(message: Message):
    if not is_admin(message.from_user.id):
        return

    with db() as con:
        rows = con.execute(
            "SELECT * FROM campaigns ORDER BY id DESC LIMIT 30"
        ).fetchall()

    if not rows:
        await message.answer("Kanallar yo'q.")
        return

    lines = [
        f"#{r['id']} {r['channel']} | qolgan: {r['slots_left']} | "
        f"{fmt(r['reward'])} so'm | "
        + ("faol" if r["active"] else "to'xtatilgan")
        for r in rows
    ]

    await message.answer("\n".join(lines))


@router.message(Command("stopchannel"))
async def stop_channel(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    if not command.args or not command.args.strip().isdigit():
        await message.answer("Format: /stopchannel ID")
        return

    with db() as con:
        cur = con.execute(
            "UPDATE campaigns SET active=0 WHERE id=?",
            (int(command.args),),
        )

    if cur.rowcount:
        await message.answer("Kanal to'xtatildi.")
    else:
        await message.answer("Bunday kanal ID topilmadi.")


@router.message(Command("stat"))
async def stat(message: Message):
    if not is_admin(message.from_user.id):
        return

    with db() as con:
        users = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        confirmed = con.execute(
            "SELECT COALESCE(SUM(balance),0) FROM users"
        ).fetchone()[0]

        pend = con.execute(
            "SELECT COALESCE(SUM(reward),0) FROM referrals "
            "WHERE status='pending'"
        ).fetchone()[0]

        paid_out = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM withdrawals "
            "WHERE status='paid'"
        ).fetchone()[0]

        wait = con.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE status='pending'"
        ).fetchone()[0]

    await message.answer(
        f"📊 Statistika\n"
        f"Foydalanuvchilar: {users}\n"
        f"Tasdiqlangan balanslar (sizning qarzingiz): "
        f"{fmt(confirmed)} so'm\n"
        f"Tekshiruvda turgan summa: {fmt(pend)} so'm\n"
        f"Kutilayotgan yechish so'rovlari: {wait} ta\n"
        f"Jami to'langan: {fmt(paid_out)} so'm"
    )


# ====================== FON VAZIFASI ======================

async def release_loop(bot: Bot):
    while True:
        try:
            for r in referrals_due():
                st = await is_member(
                    bot,
                    r["channel"],
                    r["referred_id"],
                )

                if st is None:
                    continue

                referral_finish(r["id"], st)
                name = r["title"] or r["channel"]

                if st:
                    await safe_send(
                        bot,
                        r["referrer_id"],
                        f"✅ «{name}» kanali uchun "
                        f"{fmt(r['reward'])} so'm tasdiqlandi.\n"
                        "Endi uni yechib olishingiz mumkin "
                        "(agar summa yetarli bo'lsa).",
                    )
                else:
                    await safe_send(
                        bot,
                        r["referrer_id"],
                        f"⚠️ Do'stingiz «{name}» kanalini tark etgani "
                        f"uchun jarima: -{fmt(r['reward'])} so'm.",
                    )

        except Exception:
            logging.exception("release_loop xatosi")

        await asyncio.sleep(600)


# ====================== ISHGA TUSHIRISH ======================

async def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise SystemExit(
            "BOT_TOKEN va ADMIN_ID o'rnatilmagan "
            "(Railway Variables'ni tekshiring)."
        )

    db_init()

    bot = PremiumBot(BOT_TOKEN)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(release_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
