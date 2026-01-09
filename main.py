import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional

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

    # 1) карточка отправителя
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=_user_card(user),
        parse_mode=ParseMode.HTML,
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


def build_app() -> Application:
    if not BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Заполни BOT_TOKEN в config.py")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise RuntimeError("Заполни ADMIN_ID (целое число) в config.py")

    app = Application.builder().token(BOT_TOKEN).build()

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
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Админ отвечает reply'ем на скопированное сообщение
    app.add_handler(
        MessageHandler(
            filters.User(user_id=ADMIN_ID) & filters.REPLY & ~filters.COMMAND,
            admin_reply_to_user,
        )
    )

    return app


if __name__ == "__main__":
    app = build_app()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

