import random
import sqlite3
import string
from datetime import datetime
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from cfg import ADMIN_IDS, TOKEN

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "bot.sqlite3"
IMAGES_DIR = BASE_DIR / "images"

(
    REFUND_TOKEN,
    REFUND_ORDER_ID,
    REFUND_PHOTO_CHOICE,
    REFUND_PHOTO,
    REFUND_TEXT,
    REFUND_CONFIRM,
    ADMIN_WAIT_USER_ID,
) = range(7)


def init_db() -> None:
    """Создание базы данных и таблиц."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_allowed INTEGER DEFAULT 0,
                refunds_count INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                token2 TEXT,
                order_id TEXT,
                has_photo INTEGER,
                photo_filename TEXT,
                text TEXT,
                request_user_id TEXT,
                request_message_id TEXT,
                response_status INTEGER,
                response_body TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()


def upsert_user(update: Update) -> None:
    """Логируем пользователя при старте."""
    user = update.effective_user
    now = datetime.utcnow().isoformat()
    is_admin = 1 if user.id in ADMIN_IDS else 0
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, is_allowed, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                is_allowed=MAX(users.is_allowed, excluded.is_allowed)
            """,
            (user.id, user.username, user.first_name, user.last_name, is_admin, now),
        )
        conn.commit()


def is_allowed(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_allowed FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return bool(row and row[0])


def get_refunds_count(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT refunds_count FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def increment_refunds_count(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET refunds_count = refunds_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def log_refund(
    user_id: int,
    token2: str,
    order_id: str,
    has_photo: bool,
    photo_filename: str | None,
    text: str,
    request_user_id: str,
    request_message_id: str,
    response_status: int,
    response_body: str,
) -> None:
    """Записываем возврат в базу."""
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO refunds (
                user_id, token2, order_id, has_photo, photo_filename, text,
                request_user_id, request_message_id, response_status, response_body, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                token2,
                order_id,
                1 if has_photo else 0,
                photo_filename,
                text,
                request_user_id,
                request_message_id,
                response_status,
                response_body,
                now,
            ),
        )
        conn.commit()


def generate_request_user_id() -> str:
    letters = "".join(random.choices(string.ascii_letters, k=5))
    digits = "".join(random.choices(string.digits, k=5))
    return f"{letters}{digits}"


def generate_message_id() -> str:
    return "".join(random.choices(string.digits, k=15))


def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("👤 Профиль", callback_data="menu_profile")],
        [InlineKeyboardButton("💸 Вернуть деньги", callback_data="menu_refund")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("🛠️ Админка", callback_data="menu_admin")])
    return InlineKeyboardMarkup(buttons)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    is_admin = user.id in ADMIN_IDS
    keyboard = main_menu_keyboard(is_admin)
    text = "✨ Главное меню"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text=text, reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    init_db()
    upsert_user(update)
    await show_main_menu(update, context)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    refunds_count = get_refunds_count(user_id)
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💸 Возвратов: <b>{refunds_count}</b>"
    )
    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=main_menu_keyboard(user_id in ADMIN_IDS))


async def refund_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await query.edit_message_text(
            "⛔️ У вас нет доступа к возвратам. Обратитесь к администратору.",
            reply_markup=main_menu_keyboard(user_id in ADMIN_IDS),
        )
        return ConversationHandler.END
    context.user_data.clear()
    await query.edit_message_text("🔐 Введите token2:")
    return REFUND_TOKEN


async def refund_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["token2"] = update.message.text.strip()
    await update.message.reply_text("🧾 Отправьте номер заказа:")
    return REFUND_ORDER_ID


async def refund_order_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order_id"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📷 С фото", callback_data="refund_with_photo")],
            [InlineKeyboardButton("📝 Без фото", callback_data="refund_no_photo")],
        ]
    )
    await update.message.reply_text("Добавим фото?", reply_markup=keyboard)
    return REFUND_PHOTO_CHOICE


async def refund_photo_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == "refund_with_photo":
        context.user_data["has_photo"] = True
        await query.edit_message_text("📤 Отправьте фото:")
        return REFUND_PHOTO
    context.user_data["has_photo"] = False
    await query.edit_message_text("✍️ Напишите текст для возврата:")
    return REFUND_TEXT


async def refund_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("⚠️ Пожалуйста, отправьте фото.")
        return REFUND_PHOTO
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    photo = update.message.photo[-1]
    file = await photo.get_file()
    filename = f"refund_{update.effective_user.id}_{int(datetime.utcnow().timestamp())}.jpg"
    filepath = IMAGES_DIR / filename
    await file.download_to_drive(filepath)
    context.user_data["photo_filename"] = filename
    await update.message.reply_text("✍️ Теперь напишите текст для возврата:")
    return REFUND_TEXT


async def refund_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["text"] = update.message.text.strip()
    summary = (
        "📋 <b>Проверьте данные</b>\n\n"
        f"🧾 Заказ: <code>{context.user_data.get('order_id')}</code>\n"
        f"📝 Текст: {context.user_data.get('text')}\n"
    )
    if context.user_data.get("has_photo"):
        summary += f"📷 Фото: {context.user_data.get('photo_filename')}\n"
    else:
        summary += "📷 Фото: нет\n"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Вернуть", callback_data="refund_confirm")],
            [InlineKeyboardButton("❌ Отмена", callback_data="refund_cancel")],
        ]
    )
    await update.message.reply_text(summary, parse_mode="HTML", reply_markup=keyboard)
    return REFUND_CONFIRM


async def refund_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await show_main_menu(update, context)
    return ConversationHandler.END


async def send_refund_request(
    token2: str,
    order_id: str,
    text: str,
) -> tuple[int, str, str, str]:
    request_user_id = generate_request_user_id()
    message_id = generate_message_id()
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache,no-store,must-revalidate,max-age=-1,private",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) yandex-taxi/700.108.0.501438",
        "Referer": "https://m.taxi.yandex.ru/",
        "Origin": "https://m.taxi.yandex.ru",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Site": "same-origin",
        "x-requested-uri": "https://m.taxi.yandex.ru/help/ridetech/yandex/pa/ru_ru/eats/chat",
        "Connection": "keep-alive",
        "Authorization": f"Bearer {token2}",
        "Expires": "-1",
        "Accept-Language": "ru",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "X-YaTaxi-UserId": request_user_id,
        "Sec-Fetch-Mode": "cors",
    }
    payload = {
        "message_id": message_id,
        "message": text,
        "type": "text",
        "message_metadata": {
            "attachments": [],
            "user_id": request_user_id,
            "lang": "ru",
            "country": "rus",
            "service": "eats",
            "ticket_subject": "В заказе чего-то не хватает",
            "brand": "yandex",
            "eats_order_id": order_id,
            "city": "omsk",
        },
    }
    url = "https://m.taxi.yandex.ru/help/4.0/support_chat/v2/create_chat?service=taxi&handler_type=realtime"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
    return response.status_code, response.text, request_user_id, message_id


async def refund_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    token2 = context.user_data.get("token2")
    order_id = context.user_data.get("order_id")
    text = context.user_data.get("text")
    status_code, response_text, request_user_id, message_id = await send_refund_request(
        token2=token2,
        order_id=order_id,
        text=text,
    )
    log_refund(
        user_id=user_id,
        token2=token2,
        order_id=order_id,
        has_photo=context.user_data.get("has_photo", False),
        photo_filename=context.user_data.get("photo_filename"),
        text=text,
        request_user_id=request_user_id,
        request_message_id=message_id,
        response_status=status_code,
        response_body=response_text,
    )
    increment_refunds_count(user_id)
    context.user_data.clear()
    pretty_response = f"📨 Ответ сервера:\n<code>{response_text}</code>"
    await query.edit_message_text(pretty_response, parse_mode="HTML")
    await query.message.reply_text(
        "✨ Главное меню", reply_markup=main_menu_keyboard(user_id in ADMIN_IDS)
    )
    return ConversationHandler.END


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔️ Нет доступа.")
        return ConversationHandler.END
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Выдать доступ", callback_data="admin_grant")],
            [InlineKeyboardButton("🔎 Возвраты пользователя", callback_data="admin_refunds")],
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")],
        ]
    )
    await query.edit_message_text("🛠️ Админка", reply_markup=keyboard)
    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM refunds")
        total_refunds = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"💸 Возвратов: <b>{total_refunds}</b>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard(True))


async def admin_request_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔️ Нет доступа.")
        return ConversationHandler.END
    context.user_data["admin_action"] = query.data
    await query.edit_message_text("🆔 Введите ID пользователя:")
    return ADMIN_WAIT_USER_ID


async def admin_handle_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id_text = update.message.text.strip()
    if not user_id_text.isdigit():
        await update.message.reply_text("⚠️ ID должен быть числом. Попробуйте снова.")
        return ADMIN_WAIT_USER_ID
    target_user_id = int(user_id_text)
    action = context.user_data.get("admin_action")
    if action == "admin_grant":
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (user_id, is_allowed, created_at)
                VALUES (?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET is_allowed = 1
                """,
                (target_user_id, datetime.utcnow().isoformat()),
            )
            conn.commit()
        await update.message.reply_text("✅ Доступ выдан.")
    elif action == "admin_refunds":
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT refunds_count FROM users WHERE user_id = ?",
                (target_user_id,),
            )
            row = cursor.fetchone()
        refunds_count = int(row[0]) if row else 0
        await update.message.reply_text(
            f"📋 Возвратов у пользователя {target_user_id}: <b>{refunds_count}</b>",
            parse_mode="HTML",
        )
    context.user_data.clear()
    await show_main_menu(update, context)
    return ConversationHandler.END


def build_app():
    app = ApplicationBuilder().token(TOKEN).build()

    refund_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(refund_entry, pattern="^menu_refund$")],
        states={
            REFUND_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, refund_token)],
            REFUND_ORDER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, refund_order_id)],
            REFUND_PHOTO_CHOICE: [CallbackQueryHandler(refund_photo_choice, pattern="^refund_")],
            REFUND_PHOTO: [MessageHandler(filters.PHOTO, refund_photo)],
            REFUND_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, refund_text)],
            REFUND_CONFIRM: [
                CallbackQueryHandler(refund_confirm, pattern="^refund_confirm$"),
                CallbackQueryHandler(refund_cancel, pattern="^refund_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    admin_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_request_user_id, pattern="^admin_grant$"),
            CallbackQueryHandler(admin_request_user_id, pattern="^admin_refunds$"),
        ],
        states={
            ADMIN_WAIT_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_handle_user_id)]
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(profile, pattern="^menu_profile$"))
    app.add_handler(CallbackQueryHandler(admin_menu, pattern="^menu_admin$"))
    app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(show_main_menu, pattern="^admin_back$"))
    app.add_handler(CallbackQueryHandler(refund_cancel, pattern="^refund_cancel$"))
    app.add_handler(refund_handler)
    app.add_handler(admin_handler)

    return app


def main():
    init_db()
    app = build_app()
    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
