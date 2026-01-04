import json
import sqlite3
from datetime import datetime, timezone
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

(
    ADD_ACCOUNT_TOKEN,
    ADD_ACCOUNT_CONFIRM,
    ADMIN_WAIT_USER_ID,
) = range(3)


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
                created_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                token2 TEXT,
                authorized INTEGER,
                token_valid INTEGER,
                can_make_more_orders TEXT,
                rating TEXT,
                status_value TEXT,
                is_loyal INTEGER,
                active_subscriptions TEXT,
                debt_flow_enabled INTEGER,
                debt_limit INTEGER,
                phone TEXT,
                phones TEXT,
                personal_phone_id TEXT,
                phone_id TEXT,
                uuid TEXT,
                account_id TEXT,
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
    now = datetime.now(timezone.utc).isoformat()
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


def get_accounts_count(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def token_exists(token2: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM accounts WHERE token2 = ? LIMIT 1", (token2,))
        row = cursor.fetchone()
    return bool(row)


def log_account(
    user_id: int,
    token2: str,
    parsed: dict[str, object],
    response_status: int,
    response_body: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO accounts (
                user_id,
                token2,
                authorized,
                token_valid,
                can_make_more_orders,
                rating,
                status_value,
                is_loyal,
                active_subscriptions,
                debt_flow_enabled,
                debt_limit,
                phone,
                phones,
                personal_phone_id,
                phone_id,
                uuid,
                account_id,
                response_status,
                response_body,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                token2,
                1 if parsed.get("authorized") else 0,
                1 if parsed.get("token_valid") else 0,
                parsed.get("can_make_more_orders"),
                parsed.get("rating"),
                parsed.get("status_value"),
                1 if parsed.get("is_loyal") else 0,
                json.dumps(parsed.get("active_subscriptions"), ensure_ascii=False),
                1 if parsed.get("debt_flow_enabled") else 0,
                parsed.get("debt_limit"),
                parsed.get("phone"),
                json.dumps(parsed.get("phones"), ensure_ascii=False),
                parsed.get("personal_phone_id"),
                parsed.get("phone_id"),
                parsed.get("uuid"),
                parsed.get("account_id"),
                response_status,
                response_body,
                now,
            ),
        )
        conn.commit()


def parse_typed_experiments(items: list[dict]) -> dict[str, dict]:
    flags: dict[str, dict] = {}
    for item in items:
        name = item.get("name")
        if name:
            flags[name] = item.get("value") or {}
    return flags


def format_active_subscriptions(active_subscriptions: list[dict]) -> str:
    if not active_subscriptions:
        return "нет"
    ids = [sub.get("subscription_id") for sub in active_subscriptions if sub.get("subscription_id")]
    return ", ".join(ids) if ids else "есть"


def get_nested(data: dict, keys: list[str]) -> object | None:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("👤 Профиль", callback_data="menu_profile")],
        [InlineKeyboardButton("Добавить аккаунт", callback_data="menu_add_account")],
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
    accounts_count = get_accounts_count(user_id)
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👥 Аккаунтов: <b>{accounts_count}</b>"
    )
    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=main_menu_keyboard(user_id in ADMIN_IDS))


async def add_account_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("🔐 Отправьте token2:")
    return ADD_ACCOUNT_TOKEN


async def send_launch_request(token2: str) -> tuple[int, str, dict | None]:
    headers = {
        "User-Agent": "yandex-taxi/1.6.0.49 go-platform/0.1.19 Android/",
        "Pragma": "no-cache",
        "Accept": "*/*",
        "Host": "tc.mobile.yandex.net",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token2}",
        "x-oauth-token": token2,
    }
    url = "https://tc.mobile.yandex.net/3.0/launch"
    body = "{}"
    print("➡️ Запрос launch", {"url": url, "headers": headers, "body": body})
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.post(url, headers=headers, content=body)
    print("⬅️ Ответ launch", {"status": response.status_code, "body": response.text})
    response_json = None
    try:
        response_json = response.json()
    except (json.JSONDecodeError, ValueError):
        try:
            response_json = json.loads(response.text)
        except json.JSONDecodeError:
            response_json = None
    if not isinstance(response_json, dict):
        response_json = None
    return response.status_code, response.text, response_json


def build_account_summary(response_data: dict) -> tuple[str, dict[str, object]]:
    flags = parse_typed_experiments(response_data.get("typed_experiments", {}).get("items", []))
    debt_flow = flags.get("turboapp_debt_flow", {})
    active_subscriptions = response_data.get("subscriptions", {}).get("active_subscriptions", [])
    can_make_more_orders = get_nested(response_data, ["orders_state", "can_make_more_orders"])
    summary_lines = [
        "✅ <b>Аккаунт добавлен</b>",
        f"🔐 Авторизация: <b>{'да' if response_data.get('authorized') else 'нет'}</b>",
        f"🧩 Валидность токена: <b>{'да' if response_data.get('token_valid') else 'нет'}</b>",
        f"🚦 Разрешение на новые заказы: <b>{can_make_more_orders or 'нет данных'}</b>",
        f"⭐ Рейтинг: <b>{get_nested(response_data, ['passenger_profile', 'rating']) or 'нет данных'}</b>",
        f"🧑‍💼 Статус пассажира: <b>{get_nested(response_data, ['passenger_profile', 'status', 'value']) or 'нет данных'}</b>",
        f"🎁 Лояльность: <b>{'да' if response_data.get('is_loyal') else 'нет'}</b>",
        f"📦 Активные подписки: <b>{format_active_subscriptions(active_subscriptions)}</b>",
        f"💳 Долговой флоу: <b>{'включен' if debt_flow.get('enabled') else 'выключен'}</b>",
        f"📉 Лимит долга: <b>{debt_flow.get('debt_limit', 'нет данных')}</b>",
        f"📱 Телефон: <b>{response_data.get('phone', 'нет данных')}</b>",
        f"📞 Телефоны (карта): <b>{', '.join(response_data.get('phones', {}).keys()) or 'нет данных'}</b>",
        f"🆔 Личный phone id: <b>{response_data.get('personal_phone_id', 'нет данных')}</b>",
        f"🆔 Phone ID: <b>{response_data.get('phone_id', 'нет данных')}</b>",
        f"🆔 UUID клиента: <b>{response_data.get('uuid', 'нет данных')}</b>",
        f"🆔 Внутренний ID пользователя: <b>{response_data.get('id', 'нет данных')}</b>",
    ]

    parsed = {
        "authorized": response_data.get("authorized"),
        "token_valid": response_data.get("token_valid"),
        "can_make_more_orders": can_make_more_orders,
        "rating": get_nested(response_data, ["passenger_profile", "rating"]),
        "status_value": get_nested(response_data, ["passenger_profile", "status", "value"]),
        "is_loyal": response_data.get("is_loyal"),
        "active_subscriptions": active_subscriptions,
        "debt_flow_enabled": debt_flow.get("enabled"),
        "debt_limit": debt_flow.get("debt_limit"),
        "phone": response_data.get("phone"),
        "phones": response_data.get("phones"),
        "personal_phone_id": response_data.get("personal_phone_id"),
        "phone_id": response_data.get("phone_id"),
        "uuid": response_data.get("uuid"),
        "account_id": response_data.get("id"),
    }
    return "\n".join(summary_lines), parsed


async def process_add_account(message, user, token2: str) -> None:
    status_code, response_text, response_json = await send_launch_request(token2)
    if response_json is None:
        log_account(
            user_id=user.id,
            token2=token2,
            parsed={},
            response_status=status_code,
            response_body=response_text,
        )
        await message.reply_text(
            "⚠️ Не удалось распарсить ответ сервера. "
            "Ответ сохранён в базе, проверьте token2 и попробуйте снова."
        )
    else:
        summary, parsed = build_account_summary(response_json)
        log_account(
            user_id=user.id,
            token2=token2,
            parsed=parsed,
            response_status=status_code,
            response_body=response_text,
        )
        await message.reply_text(summary, parse_mode="HTML")
    await message.reply_text(
        "✨ Главное меню", reply_markup=main_menu_keyboard(user.id in ADMIN_IDS)
    )


async def add_account_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token2 = update.message.text.strip()
    context.user_data["token2"] = token2
    if token_exists(token2):
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Да", callback_data="add_account_confirm_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data="add_account_confirm_no")],
            ]
        )
        await update.message.reply_text(
            "⚠️ Такой token2 уже добавляли. Точно добавить этот аккаунт?",
            reply_markup=keyboard,
        )
        return ADD_ACCOUNT_CONFIRM
    await process_add_account(update.message, update.effective_user, token2)
    context.user_data.clear()
    return ConversationHandler.END


async def add_account_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "add_account_confirm_no":
        context.user_data.clear()
        await query.edit_message_text("🔐 Отправьте другой token2:")
        return ADD_ACCOUNT_TOKEN
    token2 = context.user_data.get("token2")
    await query.edit_message_text("⏳ Проверяю token2...")
    await process_add_account(query.message, update.effective_user, token2)
    context.user_data.clear()
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
            [InlineKeyboardButton("🔎 Аккаунты пользователя", callback_data="admin_refunds")],
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
        cursor.execute("SELECT COUNT(*) FROM accounts")
        total_accounts = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"👤 Аккаунтов: <b>{total_accounts}</b>"
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
                (target_user_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        await update.message.reply_text("✅ Доступ выдан.")
    elif action == "admin_refunds":
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM accounts WHERE user_id = ?",
                (target_user_id,),
            )
            row = cursor.fetchone()
        accounts_count = int(row[0]) if row else 0
        await update.message.reply_text(
            f"📋 Аккаунтов у пользователя {target_user_id}: <b>{accounts_count}</b>",
            parse_mode="HTML",
        )
    context.user_data.clear()
    await show_main_menu(update, context)
    return ConversationHandler.END


def build_app():
    app = ApplicationBuilder().token(TOKEN).build()

    add_account_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_account_entry, pattern="^menu_add_account$")],
        states={
            ADD_ACCOUNT_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_token)],
            ADD_ACCOUNT_CONFIRM: [
                CallbackQueryHandler(add_account_confirm, pattern="^add_account_confirm_")
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
    app.add_handler(add_account_handler)
    app.add_handler(admin_handler)

    return app


def main():
    init_db()
    app = build_app()
    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
