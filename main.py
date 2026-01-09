import logging
import re
import time
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Optional, Set, List, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    ConversationHandler,
    filters,
)

from config import ADMIN_ID, BOT_TOKEN


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("relay-bot")


WRITE_WAITING_MESSAGE = 1
DONATE_WAITING_CUSTOM_AMOUNT = 2


MENU_WRITE = "✍️ Написать"
MENU_DONATE = "⭐ Задонатить"


MAIN_MENU = ReplyKeyboardMarkup(
    [[MENU_WRITE, MENU_DONATE]],
    resize_keyboard=True,
    input_field_placeholder="Выбирай кнопку или просто пиши…",
)


PRESET_STARS = (50, 100, 250, 500, 1000)

DATA_PATH = Path(__file__).with_name("bot_data.json")
FLOOD_WINDOW_SEC = 30
FLOOD_MAX_MESSAGES = 4


def _profile_url(u) -> str:
    # Если username нет, tg://user?id=... открывает профиль в большинстве клиентов
    if getattr(u, "username", None):
        return f"https://t.me/{u.username}"
    return f"tg://user?id={u.id}"


def _admin_user_keyboard(*, user_id: int, profile_url: str, is_banned: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Ответить", callback_data=f"admin:reply:{user_id}"),
                InlineKeyboardButton(
                    "Разбанить" if is_banned else "Забанить",
                    callback_data=f"admin:{'unban' if is_banned else 'ban'}:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton("Написать", url=profile_url),
            ],
        ]
    )


def _user_card(u) -> str:
    username = f"@{u.username}" if u.username else "—"
    full_name = " ".join(p for p in [u.first_name, u.last_name] if p) or "—"
    return (
        f"<b>Новый сигнал в эфир</b>\n"
        f"— <b>От</b>: {full_name}\n"
        f"— <b>Username</b>: {username}\n"
        f"— <b>ID</b>: <code>{u.id}</code>\n"
    )


def _donate_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, stars in enumerate(PRESET_STARS, start=1):
        row.append(InlineKeyboardButton(f"{stars} ⭐", callback_data=f"donate:{stars}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Своя сумма", callback_data="donate:custom")])
    return InlineKeyboardMarkup(rows)


@dataclass
class RelayIndex:
    # admin_message_id -> user_id
    by_admin_msg_id: Dict[int, int]


def _relay_index(context: ContextTypes.DEFAULT_TYPE) -> RelayIndex:
    if "relay_index" not in context.application.bot_data:
        context.application.bot_data["relay_index"] = RelayIndex(by_admin_msg_id={})
    return context.application.bot_data["relay_index"]


def _banned_users(context: ContextTypes.DEFAULT_TYPE) -> Set[int]:
    if "banned_users" not in context.application.bot_data:
        context.application.bot_data["banned_users"] = set()
    return context.application.bot_data["banned_users"]


def _admin_pending_reply(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, int]:
    # admin_id -> user_id
    if "admin_pending_reply" not in context.application.bot_data:
        context.application.bot_data["admin_pending_reply"] = {}
    return context.application.bot_data["admin_pending_reply"]


def _flood_index(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, List[float]]:
    # user_id -> timestamps (monotonic) последних сообщений
    if "flood_index" not in context.application.bot_data:
        context.application.bot_data["flood_index"] = {}
    return context.application.bot_data["flood_index"]


def _load_persistent_data() -> Dict[str, object]:
    if not DATA_PATH.exists():
        return {}
    try:
        raw = DATA_PATH.read_text(encoding="utf-8")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        log.exception("Failed to load %s", DATA_PATH)
        return {}


def _save_persistent_data(*, banned_users: Set[int]) -> None:
    data = {"banned_users": sorted(banned_users)}
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def _is_flooding(*, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    now = time.monotonic()
    idx = _flood_index(context)
    stamps = idx.get(user_id, [])
    stamps = [t for t in stamps if now - t <= FLOOD_WINDOW_SEC]
    stamps.append(now)
    idx[user_id] = stamps
    return len(stamps) > FLOOD_MAX_MESSAGES


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    text = (
        "Привет! Я — маленький телеграм‑почтальон.\n\n"
        "Моя работа простая и важная: ты пишешь — я аккуратно доставляю это админу.\n"
        "Хочешь поддержать проект звёздами? Тоже умею.\n\n"
        "<b>Выбирай действие кнопками ниже.</b>"
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def menu_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    user = update.effective_user
    if user and user.id in _banned_users(context):
        await update.message.reply_text(
            "Упс. Этот почтовый ящик для тебя закрыт.\n"
            "Если думаешь, что это ошибка — попробуй связаться с админом другим способом.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    text = (
        "Окей! Сейчас я включу режим «радиостанция».\n\n"
        "Напиши одно сообщение (текст/фото/видео/файл/голос — что угодно), и я отправлю это админу.\n"
        "Чтобы выйти без отправки — напиши <code>/cancel</code>."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU, parse_mode=ParseMode.HTML)
    return WRITE_WAITING_MESSAGE


async def write_receive_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    user = update.effective_user
    if not user:
        return ConversationHandler.END

    if user.id in _banned_users(context):
        await update.message.reply_text(
            "Сообщение не отправлено: доступ к боту для тебя ограничен.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    if _is_flooding(user_id=user.id, context=context):
        await update.message.reply_text(
            "Слишком быстро 🙂 Дай мне 20–30 секунд перевести дух и попробуй ещё раз.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    # 1) карточка отправителя
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=_user_card(user),
        parse_mode=ParseMode.HTML,
        reply_markup=_admin_user_keyboard(
            user_id=user.id,
            profile_url=_profile_url(user),
            is_banned=(user.id in _banned_users(context)),
        ),
    )

    # 2) копия исходного сообщения (с сохранением медиа)
    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=update.effective_chat.id,
        message_id=update.message.message_id,
    )

    idx = _relay_index(context)
    idx.by_admin_msg_id[copied.message_id] = user.id

    # 3) подтверждение пользователю
    await update.message.reply_text(
        "Готово! Доставил админу. Если понадобится уточнение — он ответит через меня.",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(
            "Принято. Сворачиваю крылья и жду следующую команду.",
            reply_markup=MAIN_MENU,
        )
    return ConversationHandler.END


async def user_fallback_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Если пользователь пишет не нажимая "Написать" — мягко направляем.
    if not update.message:
        return
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        return
    await update.message.reply_text(
        "Я тебя услышал 🙂\n\n"
        "Чтобы я доставил сообщение админу, нажми кнопку <b>«Написать»</b> и отправь сообщение ещё раз.",
        reply_markup=MAIN_MENU,
        parse_mode=ParseMode.HTML,
    )


async def menu_donate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    text = (
        "Спасибо, что хочешь поддержать!\n\n"
        "Оплата — в <b>Telegram Stars</b> (официальная валюта Telegram).\n"
        "Выбери сумму или введи свою."
    )
    await update.message.reply_text(
        text,
        reply_markup=MAIN_MENU,
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("Сколько звёзд отправим? ⭐", reply_markup=_donate_keyboard())
    return ConversationHandler.END


async def donate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END

    await q.answer()
    data = q.data or ""
    if not data.startswith("donate:"):
        return ConversationHandler.END

    value = data.split(":", 1)[1]
    if value == "custom":
        await q.message.reply_text(
            "Введи число звёзд (например: <code>123</code>).",
            parse_mode=ParseMode.HTML,
        )
        return DONATE_WAITING_CUSTOM_AMOUNT

    try:
        stars = int(value)
    except ValueError:
        await q.message.reply_text("Не понял сумму. Попробуй ещё раз.", reply_markup=_donate_keyboard())
        return ConversationHandler.END

    return await _send_stars_invoice(q.from_user.id, stars, context, q.message)


async def donate_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    m = re.fullmatch(r"\d{1,6}", raw)
    if not m:
        await update.message.reply_text("Нужно число звёзд (только цифры). Попробуй ещё раз.")
        return DONATE_WAITING_CUSTOM_AMOUNT

    stars = int(raw)
    if stars <= 0:
        await update.message.reply_text("Сумма должна быть больше нуля 🙂 Попробуй ещё раз.")
        return DONATE_WAITING_CUSTOM_AMOUNT

    if stars > 1_000_000:
        await update.message.reply_text("Слишком много за раз. Введи сумму поменьше 🙂")
        return DONATE_WAITING_CUSTOM_AMOUNT

    return await _send_stars_invoice(update.effective_user.id, stars, context, update.message)


async def _send_stars_invoice(
    user_id: int,
    stars: int,
    context: ContextTypes.DEFAULT_TYPE,
    reply_to,
) -> int:
    title = "Поддержка проекта ⭐"
    description = "Спасибо! Это помогает проекту жить, развиваться и не терять чувство юмора."
    payload = f"donate_{user_id}_{stars}"

    # Telegram Stars: currency="XTR", provider_token="" (пустая строка).
    await context.bot.send_invoice(
        chat_id=user_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{stars} Stars", amount=stars)],
        start_parameter="donate",
    )
    if reply_to:
        await reply_to.reply_text(
            f"Супер! Сформировал счёт на <b>{stars} ⭐</b>.",
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if not q:
        return

    ok = bool(q.invoice_payload and q.invoice_payload.startswith("donate_"))
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Что-то пошло не так с платежом. Попробуй ещё раз.")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.successful_payment:
        return

    sp = update.message.successful_payment
    user = update.effective_user
    total = sp.total_amount

    await update.message.reply_text(
        f"Принято! Спасибо за поддержку: <b>{total} ⭐</b>\n"
        "Пусть карма будет с тобой, а баги — с кем-нибудь другим.",
        reply_markup=MAIN_MENU,
        parse_mode=ParseMode.HTML,
    )

    if user:
        username = f"@{user.username}" if user.username else "—"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "<b>Новый донат ⭐</b>\n"
                f"— <b>От</b>: {user.first_name}\n"
                f"— <b>Username</b>: {username}\n"
                f"— <b>ID</b>: <code>{user.id}</code>\n"
                f"— <b>Сумма</b>: <b>{total} ⭐</b>\n"
            ),
            parse_mode=ParseMode.HTML,
        )


async def admin_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    idx = _relay_index(context)
    target_user_id: Optional[int] = idx.by_admin_msg_id.get(msg.reply_to_message.message_id)
    if not target_user_id:
        await msg.reply_text(
            "Не понял, кому отвечать. Ответь (reply) на сообщение, которое я скопировал от пользователя."
        )
        return

    await context.bot.copy_message(
        chat_id=target_user_id,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
    )

    await msg.reply_text("Доставлено пользователю ✅")


async def admin_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return

    actor = update.effective_user
    if not actor or actor.id != ADMIN_ID:
        await q.answer("Эта панель только для администратора.", show_alert=True)
        return

    await q.answer()
    data = q.data or ""
    if not data.startswith("admin:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        return

    action, raw_user_id = parts[1], parts[2]
    try:
        user_id = int(raw_user_id)
    except ValueError:
        return

    if action == "reply":
        pending = _admin_pending_reply(context)
        pending[ADMIN_ID] = user_id
        await q.message.reply_text(
            "Режим ответа включён.\n"
            "Напиши сообщение следующим сообщением — я доставлю его пользователю.\n"
            "Чтобы отменить — напиши /cancel.",
            reply_markup=MAIN_MENU,
        )
        return

    if action == "ban":
        banned = _banned_users(context)
        banned.add(user_id)
        _save_persistent_data(banned_users=banned)
        pending = _admin_pending_reply(context)
        if pending.get(ADMIN_ID) == user_id:
            pending.pop(ADMIN_ID, None)
        await q.message.reply_text(
            f"Готово. Пользователь <code>{user_id}</code> забанен: новые сообщения от него не будут приниматься.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "unban":
        banned = _banned_users(context)
        if user_id in banned:
            banned.remove(user_id)
            _save_persistent_data(banned_users=banned)
        await q.message.reply_text(
            f"Готово. Пользователь <code>{user_id}</code> разбанен.",
            parse_mode=ParseMode.HTML,
        )
        return


async def admin_send_pending_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return

    actor = update.effective_user
    if not actor or actor.id != ADMIN_ID:
        return

    pending = _admin_pending_reply(context)
    target_user_id = pending.get(ADMIN_ID)
    if not target_user_id:
        return

    await context.bot.copy_message(
        chat_id=target_user_id,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
    )
    pending.pop(ADMIN_ID, None)
    await msg.reply_text("Доставлено пользователю ✅")


async def bans_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    banned = sorted(_banned_users(context))
    if not banned:
        await update.message.reply_text("Бан‑лист пуст.")
        return
    text = "Бан‑лист:\n" + "\n".join(f"— <code>{uid}</code>" for uid in banned)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _parse_id_arg(text: str) -> Optional[int]:
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    user_id = _parse_id_arg(update.message.text or "")
    if not user_id:
        await update.message.reply_text("Использование: /ban <user_id>")
        return
    banned = _banned_users(context)
    banned.add(user_id)
    _save_persistent_data(banned_users=banned)
    await update.message.reply_text(f"Забанен: <code>{user_id}</code>", parse_mode=ParseMode.HTML)


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    user_id = _parse_id_arg(update.message.text or "")
    if not user_id:
        await update.message.reply_text("Использование: /unban <user_id>")
        return
    banned = _banned_users(context)
    banned.discard(user_id)
    _save_persistent_data(banned_users=banned)
    await update.message.reply_text(f"Разбанен: <code>{user_id}</code>", parse_mode=ParseMode.HTML)


def build_app() -> Application:
    if not BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Заполни BOT_TOKEN в config.py")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise RuntimeError("Заполни ADMIN_ID (целое число) в config.py")

    app = Application.builder().token(BOT_TOKEN).build()

    # загрузка данных (бан‑лист)
    data = _load_persistent_data()
    banned = set(int(x) for x in (data.get("banned_users") or []) if str(x).isdigit())
    app.bot_data["banned_users"] = banned

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & filters.Regex(f"^{re.escape(MENU_WRITE)}$"), menu_write),
            MessageHandler(filters.TEXT & filters.Regex(f"^{re.escape(MENU_DONATE)}$"), menu_donate),
        ],
        states={
            WRITE_WAITING_MESSAGE: [
                MessageHandler(
                    # принимаем практически всё (кроме команд), чтобы переслать админу
                    ~filters.COMMAND,
                    write_receive_any,
                )
            ],
            DONATE_WAITING_CUSTOM_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, donate_custom_amount)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(donate_callback, pattern=r"^donate:"))
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^admin:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CommandHandler("bans", bans_list))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))

    # Админ отвечает reply'ем на скопированное сообщение
    app.add_handler(
        MessageHandler(
            filters.User(user_id=ADMIN_ID) & filters.REPLY & ~filters.COMMAND,
            admin_reply_to_user,
        )
    )

    # Админ нажал "Ответить" и отправляет следующее сообщение без reply
    app.add_handler(
        MessageHandler(
            filters.User(user_id=ADMIN_ID) & ~filters.COMMAND,
            admin_send_pending_reply,
        )
    )

    # Пользовательский “фолбэк”, если пишет вне сценария
    app.add_handler(MessageHandler(~filters.COMMAND, user_fallback_any))

    return app


if __name__ == "__main__":
    app = build_app()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

