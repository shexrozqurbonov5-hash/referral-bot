"""
Taklif (referal) boti, 2-versiya.

Model:
  - Kanal egasi sizga to'laydi, siz botga KANAL qo'shasiz.
  - Foydalanuvchi do'stlarini havola orqali taklif qiladi.
  - Do'st kanalga obuna bo'lgan zahoti taklif qilganga pul yoziladi (tekshiruvda).
  - HOLD_DAYS (1 kun) o'tgach, do'st hali kanalda bo'lsa, pul tasdiqlanadi
    va yechib olish mumkin bo'ladi (xabar yuboriladi).
  - Shu muddatda do'st kanalni tark etsa, pul olib tashlanadi (jarima) va xabar yuboriladi.
  - Yechib olish faqat TASDIQLANGAN summadan mumkin.
  - Foydalanuvchi botga hech qachon pul kiritmaydi.

O'rnatish:  pip install aiogram
Ishga tushirish:  python referral_bot_v2.py

Admin buyruqlari:
  /addchannel @kanal SONI MUKOFOT   masalan: /addchannel @mening_kanalim 100 300
        SONI    - nechta obunachi uchun to'langan
        MUKOFOT - har bir obunachi uchun taklif qilgan odamga beriladigan summa (so'm)
  /channels                         kanallar ro'yxati
  /stopchannel ID                   kanalni to'xtatish
  /stat                             statistika

Eslatma: bot reklama qilinadigan kanalda ADMIN bo'lishi kerak
(obunani tekshirish uchun). Faqat ochiq (@username li) kanallar qo'llab-quvvatlanadi.
"""
import os
import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ====================== SOZLAMALAR (o'zingizniki bilan almashtiring) ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")   # Tokenni hech kimga bermang!
ADMIN_ID = 6860639975                               # Sizning Telegram ID raqamingiz (@userinfobot)
MIN_WITHDRAW = 5000                                # Minimal yechib olish summasi (so'm)
HOLD_DAYS = 1                                      # Obuna shuncha kun saqlanib tursa, pul tasdiqlanadi
DB_PATH = "referral.db"
# ==========================================================================================

logging.basicConfig(level=logging.INFO)
router = Router()


# ---------------------------- Yordamchi ----------------------------
def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


# ---------------------------- Ma'lumotlar bazasi ----------------------------
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def db_init() -> None:
    with db() as con:
        con.executescript(
            """
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
            """
        )


def user_add(uid: int, username: str | None, ref: int | None) -> bool:
    """Yangi foydalanuvchi qo'shadi. Yangi bo'lsa True qaytaradi."""
    with db() as con:
        if con.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            return False
        if ref is None or ref == uid or not con.execute(
            "SELECT 1 FROM users WHERE user_id=?", (ref,)
        ).fetchone():
            ref = None
        con.execute(
            "INSERT INTO users (user_id, username, referrer_id, balance, joined_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (uid, username, ref, now()),
        )
        return True


def admin_change_balance(uid: int, amount: int):
    with db() as con:
        user = con.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

        if not user:
            return None

        new_balance = user["balance"] + amount

        if new_balance < 0:
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, uid)
        )

        return new_balance


def channels_active():
    with db() as con:
        return con.execute(
            "SELECT * FROM campaigns WHERE active=1 AND slots_left>0 ORDER BY id"
        ).fetchall()


def channel_get(cid: int):
    with db() as con:
        return con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()


def channel_add(channel: str, title: str, slots: int, reward: int) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO campaigns (channel, title, slots_left, reward, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, title, slots, reward, now()),
        )
        return cur.lastrowid


def referral_claim(uid: int, cid: int):
    """Do'stning obunasini ro'yxatga oladi. (holat, taklif qilgan odam, mukofot) qaytaradi.
    Pul darrov 'tekshiruvda' holatida yoziladi."""
    with db() as con:
        u = con.execute("SELECT referrer_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u or not u["referrer_id"]:
            return "no_ref", None, 0
        c = con.execute(
            "SELECT * FROM campaigns WHERE id=? AND active=1", (cid,)
        ).fetchone()
        if not c or c["slots_left"] <= 0:
            return "closed", None, 0
        if con.execute(
            "SELECT 1 FROM referrals WHERE referred_id=? AND campaign_id=?", (uid, cid)
        ).fetchone():
            return "exists", None, 0
        release = (datetime.now() + timedelta(days=HOLD_DAYS)).isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO referrals (referred_id, referrer_id, campaign_id, reward, created_at, release_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, u["referrer_id"], cid, c["reward"], now(), release),
        )
        con.execute("UPDATE campaigns SET slots_left = slots_left - 1 WHERE id=?", (cid,))
        return "ok", u["referrer_id"], c["reward"]


def referrals_due():
    with db() as con:
        return con.execute(
            "SELECT r.*, c.channel, c.title FROM referrals r JOIN campaigns c ON c.id = r.campaign_id "
            "WHERE r.status='pending' AND r.release_at <= ?",
            (now(),),
        ).fetchall()


def referral_finish(rid: int, still_member: bool) -> None:
    """Obuna saqlangan bo'lsa pulni tasdiqlaydi (yechish mumkin),
    chiqib ketgan bo'lsa pulni olib tashlaydi (jarima) va o'rinni qaytaradi."""
    with db() as con:
        r = con.execute(
            "SELECT * FROM referrals WHERE id=? AND status='pending'", (rid,)
        ).fetchone()
        if not r:
            return
        if still_member:
            con.execute("UPDATE referrals SET status='paid' WHERE id=?", (rid,))
            con.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (r["reward"], r["referrer_id"]),
            )
        else:
            con.execute("UPDATE referrals SET status='cancelled' WHERE id=?", (rid,))
            con.execute(
                "UPDATE campaigns SET slots_left = slots_left + 1 WHERE id=?",
                (r["campaign_id"],),
            )


def user_stats(uid: int):
    with db() as con:
        u = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        row = con.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status='pending' THEN reward END),0) AS pending_sum, "
            "COALESCE(SUM(CASE WHEN status='paid' THEN 1 END),0) AS paid_cnt "
            "FROM referrals WHERE referrer_id=?",
            (uid,),
        ).fetchone()
        friends = con.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,)
        ).fetchone()[0]
        return {
            "balance": u["balance"] if u else 0,   # tasdiqlangan, yechish mumkin
            "pending_sum": row["pending_sum"],     # tekshiruvda
            "paid_cnt": row["paid_cnt"],
            "friends": friends,
        }


def withdraw_create(uid: int, card: str):
    """Tasdiqlangan balansni yechib, so'rov yaratadi. Muvaffaqiyatli bo'lsa (id, summa) qaytaradi."""
    with db() as con:
        u = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u or u["balance"] < MIN_WITHDRAW:
            return None
        amount = u["balance"]
        con.execute("UPDATE users SET balance = 0 WHERE user_id=?", (uid,))
        cur = con.execute(
            "INSERT INTO withdrawals (user_id, amount, card, created_at) VALUES (?, ?, ?, ?)",
            (uid, amount, card, now()),
        )
        return cur.lastrowid, amount


def withdraw_finish(wid: int, paid: bool):
    with db() as con:
        w = con.execute(
            "SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)
        ).fetchone()
        if not w:
            return None
        if paid:
            con.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
        else:
            con.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
            con.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (w["amount"], w["user_id"]),
            )
        return w


# ---------------------------- Telegram tekshiruvi ----------------------------
async def is_member(bot: Bot, channel: str, uid: int):
    """True/False, tekshirib bo'lmasa None."""
    try:
        m = await bot.get_chat_member(channel, uid)
        return m.status in ("member", "administrator", "creator")
    except (TelegramBadRequest, TelegramForbiddenError):
        return None


async def safe_send(bot: Bot, uid: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(uid, text, **kwargs)
    except Exception:
        logging.info("Xabar yuborib bo'lmadi: %s", uid)


# ---------------------------- Tugmalar ----------------------------

def main_menu(is_admin_user=False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="👥 Do'stlarni taklif qilish")],
        [KeyboardButton(text="📢 Kanallar"), KeyboardButton(text="💰 Balans")],
        [KeyboardButton(text="💸 Yechib olish"), KeyboardButton(text="ℹ️ Qoidalar")],
    ]

    if is_admin_user:
        keyboard.append([KeyboardButton(text="⚙️ Admin panel")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
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
    f"• Yechib olish faqat tasdiqlangan summadan mumkin. Minimal summa: {fmt(MIN_WITHDRAW)} so'm. "
    "Pul admin tomonidan kartaga o'tkaziladi.\n"
    "• Mukofotlar kanal egalari to'lovi hisobidan beriladi. "
    "Botga hech qanday pul kiritish talab qilinmaydi.\n"
    "• O'zingizni taklif qilish, soxta akkauntlar va aldov taqiqlanadi, "
    "bunday hisoblar bloklanadi."
)


# ---------------------------- Foydalanuvchi qismi ----------------------------
@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ref = int(command.args) if command.args and command.args.isdigit() else None
    user_add(message.from_user.id, message.from_user.username, ref)
    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Bu bot orqali do'stlaringizni kanallarga taklif qilib pul ishlashingiz mumkin.\n\n"
        "1) «Do'stlarni taklif qilish» orqali havolangizni oling\n"
        "2) Do'stlaringizga yuboring\n"
        "3) Ular kanallarga obuna bo'lsa, hisobingizga pul tushadi\n\n"
        "Batafsil qoidalar: «ℹ️ Qoidalar»",
        reply_markup=main_menu(),
    )


@router.message(F.text == "ℹ️ Qoidalar")
async def rules(message: Message):
    await message.answer(RULES)



@router.message(F.text == "⚙️ Admin panel")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Hisobga pul qo‘shish")],
            [KeyboardButton(text="➖ Hisobdan pul olish")],
            [KeyboardButton(text="👤 Balansni tekshirish")],
            [KeyboardButton(text="🔙 Asosiy menyu")],
        ],
        resize_keyboard=True,
    )

    await message.answer("⚙️ Admin panel", reply_markup=kb)



@router.message(F.text == "🔙 Asosiy menyu")
async def back_to_main(message: Message):
    await message.answer(
        "Asosiy menyu:",
        reply_markup=main_menu(is_admin(message.from_user.id))
    )

@router.message(F.text == "➕ Hisobga pul qo‘shish")
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

    if not message.text.isdigit():
        await message.answer("Faqat raqamli Telegram ID yuboring.")
        return

    uid = int(message.text)

    if not user_stats(uid):
        await message.answer("Foydalanuvchi topilmadi. U avval botni ishga tushirgan bo‘lishi kerak.")
        return

    await state.update_data(target_uid=uid)
    await state.set_state(AdminBalance.add_amount)
    await message.answer("Qancha pul qo‘shmoqchisiz? Summani so‘mda yuboring:")

@router.message(AdminBalance.add_amount)
async def admin_add_get_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Summani musbat raqamda yuboring.")
        return

    amount = int(message.text)
    data = await state.get_data()
    uid = data["target_uid"]

    new_balance = admin_change_balance(uid, amount)

    if new_balance is None:
        await message.answer("Foydalanuvchi topilmadi.")
    else:
        await message.answer(
            f"✅ Foydalanuvchi ID: {uid}\n"
            f"Qo‘shilgan pul: {fmt(amount)} so‘m\n"
            f"Yangi balans: {fmt(new_balance)} so‘m"
        )

    await state.clear()

@router.message(F.text == "👤 Balansni tekshirish")
async def admin_check_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminBalance.check_user_id)
    await message.answer("Balansini tekshirmoqchi bo‘lgan foydalanuvchining Telegram ID raqamini yuboring:")


@router.message(AdminBalance.check_user_id)
async def admin_check_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    if not message.text.isdigit():
        await message.answer("Faqat raqamli Telegram ID yuboring.")
        return

    uid = int(message.text)
    stats = user_stats(uid)

    with db() as con:
        user = con.execute(
            "SELECT username FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

    if not user:
        await message.answer("Bunday foydalanuvchi topilmadi.")
    else:
        username = user["username"] or "Username yo‘q"
        await message.answer(
            f"👤 Foydalanuvchi: {username}\n"
            f"🆔 ID: {uid}\n"
            f"💰 Tasdiqlangan balans: {fmt(stats['balance'])} so‘m\n"
            f"⏳ Tekshiruvdagi pul: {fmt(stats['pending_sum'])} so‘m\n"
            f"👥 Taklif qilgan do‘stlari: {stats['friends']} ta"
        )

    await state.clear()

@router.message(F.text == "👥 Do'stlarni taklif qilish")
async def invite(message: Message, bot: Bot):
    user_add(message.from_user.id, message.from_user.username, None)
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


@router.message(F.text == "📢 Kanallar")
async def channels(message: Message):
    user_add(message.from_user.id, message.from_user.username, None)
    items = channels_active()
    if not items:
        await message.answer("Hozircha faol kanallar yo'q. Keyinroq qaytib keling.")
        return
    await message.answer(
        "Quyidagi kanallarga obuna bo'ling, so'ng «Tekshirish» tugmasini bosing.\n"
        "Bu sizni taklif qilgan do'stingizga pul yozilishiga yordam beradi."
    )
    for c in items:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📢 Kanalga o'tish", url=f"https://t.me/{c['channel'].lstrip('@')}")],
                [InlineKeyboardButton(text="✅ Tekshirish", callback_data=f"check:{c['id']}")],
            ]
        )
        await message.answer(f"{c['title'] or c['channel']}", reply_markup=kb)


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
        await call.answer("Tekshirib bo'lmadi. Keyinroq urinib ko'ring.", show_alert=True)
        return
    if not st:
        await call.answer("Siz hali kanalga obuna bo'lmagansiz.", show_alert=True)
        return

    status, referrer, reward = referral_claim(uid, cid)
    if status == "ok":
        await call.answer("✅ Obuna tasdiqlandi. Rahmat!", show_alert=True)
        await safe_send(
            bot,
            referrer,
            f"🎉 Do'stingiz «{c['title'] or c['channel']}» kanaliga obuna bo'ldi.\n"
            f"+{fmt(reward)} so'm hisobingizga yozildi (⏳ tekshiruvda).\n\n"
            f"{HOLD_DAYS} kundan keyin, do'stingiz kanalda qolgan bo'lsa, "
            "pul tasdiqlanadi va yechib olish mumkin bo'ladi. "
            f"Agar {HOLD_DAYS} kun ichida kanalni tark etsa, -{fmt(reward)} so'm jarima bo'ladi.",
        )
    elif status == "exists":
        await call.answer("Bu kanal allaqachon hisobga olingan.", show_alert=True)
    elif status == "closed":
        await call.answer("Bu kanal uchun obunachi yig'ish yopilgan.", show_alert=True)
    else:
        await call.answer(
            "Siz taklif havolasi orqali kelmagansiz, shuning uchun hech kimga mukofot yozilmaydi. "
            "Lekin o'z havolangizni olib, do'stlaringizni taklif qilishingiz mumkin.",
            show_alert=True,
        )


@router.message(F.text == "💰 Balans")
async def balance(message: Message):
    user_add(message.from_user.id, message.from_user.username, None)
    s = user_stats(message.from_user.id)
    total = s["balance"] + s["pending_sum"]
    await message.answer(
        f"💰 Jami: {fmt(total)} so'm\n\n"
        f"✅ Yechib olish mumkin: {fmt(s['balance'])} so'm\n"
        f"⏳ Tekshiruvda ({HOLD_DAYS} kunlik): {fmt(s['pending_sum'])} so'm\n\n"
        f"👥 Do'stlaringiz: {s['friends']} ta\n"
        f"✅ Tasdiqlangan obunalar: {s['paid_cnt']} ta\n\n"
        f"Minimal yechib olish: {fmt(MIN_WITHDRAW)} so'm"
    )


# ---------------------------- Yechib olish ----------------------------
@router.message(F.text == "❌ Bekor qilish")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
    "Bekor qilindi.",
    reply_markup=main_menu(is_admin(message.from_user.id))
)


@router.message(F.text == "💸 Yechib olish")
async def withdraw_start(message: Message, state: FSMContext):
    user_add(message.from_user.id, message.from_user.username, None)
    s = user_stats(message.from_user.id)
    if s["balance"] < MIN_WITHDRAW:
        await message.answer(
            f"Yechib olish mumkin bo'lgan summa: {fmt(s['balance'])} so'm.\n"
            f"Tekshiruvda: {fmt(s['pending_sum'])} so'm.\n\n"
            f"Yechib olish uchun tasdiqlangan summa kamida {fmt(MIN_WITHDRAW)} so'm bo'lishi kerak."
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
        await message.answer("Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qaytadan yozing.")
        return
    res = withdraw_create(message.from_user.id, card)
    await state.clear()
    if not res:
        await message.answer(
    "Tasdiqlangan balans yetarli emas.",
    reply_markup=main_menu(is_admin(message.from_user.id))
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
                InlineKeyboardButton(text="✅ To'landi", callback_data=f"wpaid:{wid}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"wreject:{wid}"),
            ]
        ]
    )
    who = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.from_user.id}"
    await bot.send_message(
        ADMIN_ID,
        f"💸 Yechib olish #{wid}\nFoydalanuvchi: {who}\nSumma: {fmt(amount)} so'm\nKarta: {card}",
        reply_markup=kb,
    )


# ---------------------------- Admin qismi ----------------------------
@router.callback_query(F.data.startswith("wpaid:"))
async def w_paid(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    wid = int(call.data.split(":")[1])
    w = withdraw_finish(wid, True)
    if not w:
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await safe_send(bot, w["user_id"], f"✅ So'rov #{wid}: {fmt(w['amount'])} so'm kartangizga o'tkazildi.")
    await call.message.edit_text(call.message.text + "\n\n✅ TO'LANDI")
    await call.answer()


@router.callback_query(F.data.startswith("wreject:"))
async def w_reject(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    wid = int(call.data.split(":")[1])
    w = withdraw_finish(wid, False)
    if not w:
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await safe_send(
        bot,
        w["user_id"],
        f"❌ So'rov #{wid} rad etildi. {fmt(w['amount'])} so'm balansingizga qaytarildi.",
    )
    await call.message.edit_text(call.message.text + "\n\n❌ RAD ETILDI")
    await call.answer()


@router.message(Command("addchannel"))
async def add_channel(message: Message, command: CommandObject, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Format: /addchannel @kanal SONI MUKOFOT\nMasalan: /addchannel @mening_kanalim 100 300")
        return
    channel = parts[0] if parts[0].startswith("@") else "@" + parts[0]
    slots, reward = int(parts[1]), int(parts[2])

    try:
        me = await bot.me()
        member = await bot.get_chat_member(channel, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("Bot bu kanalda admin emas. Avval botni kanalga admin qiling.")
            return
        chat = await bot.get_chat(channel)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Kanal topilmadi yoki bot unga kira olmayapti. Username'ni va bot adminligini tekshiring.")
        return

    cid = channel_add(channel, chat.title or channel, slots, reward)
    await message.answer(
        f"✅ Kanal #{cid} qo'shildi\nKanal: {channel}\nObunachi soni: {slots}\n"
        f"Taklif qilganga mukofot: {fmt(reward)} so'm / obunachi"
    )


@router.message(Command("channels"))
async def list_channels(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        rows = con.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        await message.answer("Kanallar yo'q.")
        return
    lines = [
        f"#{r['id']} {r['channel']} | qolgan: {r['slots_left']} | {fmt(r['reward'])} so'm | "
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
        con.execute("UPDATE campaigns SET active=0 WHERE id=?", (int(command.args),))
    await message.answer("Kanal to'xtatildi.")


@router.message(Command("stat"))
async def stat(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        confirmed = con.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        pend = con.execute("SELECT COALESCE(SUM(reward),0) FROM referrals WHERE status='pending'").fetchone()[0]
        paid_out = con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone()[0]
        wait = con.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0]
    await message.answer(
        f"📊 Statistika\n"
        f"Foydalanuvchilar: {users}\n"
        f"Tasdiqlangan balanslar (sizning qarzingiz): {fmt(confirmed)} so'm\n"
        f"Tekshiruvda turgan summa: {fmt(pend)} so'm\n"
        f"Kutilayotgan yechish so'rovlari: {wait} ta\n"
        f"Jami to'langan: {fmt(paid_out)} so'm"
    )


# ---------------------------- Fon vazifasi: muddati tugagan obunalarni tekshirish ----------------------------
async def release_loop(bot: Bot) -> None:
    while True:
        try:
            for r in referrals_due():
                st = await is_member(bot, r["channel"], r["referred_id"])
                if st is None:
                    continue  # tekshirib bo'lmadi, keyingi safar
                referral_finish(r["id"], st)
                name = r["title"] or r["channel"]
                if st:
                    await safe_send(
                        bot,
                        r["referrer_id"],
                        f"✅ «{name}» kanali uchun {fmt(r['reward'])} so'm tasdiqlandi.\n"
                        "Endi uni yechib olishingiz mumkin (agar summa yetarli bo'lsa).",
                    )
                else:
                    await safe_send(
                        bot,
                        r["referrer_id"],
                        f"⚠️ Do'stingiz «{name}» kanalini tark etgani uchun jarima: "
                        f"-{fmt(r['reward'])} so'm.",
                    )
        except Exception:
            logging.exception("release_loop xatosi")
        await asyncio.sleep(600)  # har 10 daqiqada


# ---------------------------- Ishga tushirish ----------------------------
async def main():
    db_init()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(release_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
