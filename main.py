import html

import re
import time
import json
from pathlib import Path
from typing import Dict, Optional, Set, List

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from config import ADMIN_ID, BOT_TOKEN


MENU_WRITE = "✍️ Написать"
MENU_DONATE = "⭐ Задонатить"

MAIN_MENU = ReplyKeyboardMarkup(
    [[MENU_WRITE, MENU_DONATE]],
    resize_keyboard=True,
    input_field_placeholder="Пиши сообщение или жми кнопку…",
)

PRESET_STARS = (50, 100, 250, 500, 1000)

DATA_PATH = Path(__file__).with_name("bot_data.json")
FLOOD_WINDOW_SEC = 30
FLOOD_MAX_MESSAGES = 4


def _h(s: str) -> str:
    return html.escape(s or "")


def _norm_button_text(s: Optional[str]) -> str:
    # Некоторые клиенты могут присылать эмодзи без variation selector (FE0F)
    return (s or "").replace("\ufe0f", "").strip()


def _is_menu_press(text: Optional[str], expected: str) -> bool:
    return _norm_button_text(text) == _norm_button_text(expected)


def _profile_url_by_user_id(user_id: int) -> str:
    return f"tg://user?id={user_id}"


def _profile_url(u) -> str:
    if getattr(u, "username", None):
        return f"https://t.me/{u.username}"
    return _profile_url_by_user_id(u.id)


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
    full_name = " ".join(p for p in [u.first_name, u.last_name] if p) or "Пользователь"
    link = _profile_url(u)
    return (
        f"<b>Сообщение от</b>: <a href=\"{_h(link)}\">{_h(full_name)}</a>\n"
        f"— <b>Username</b>: {_h(username)}\n"
        f"— <b>ID</b>: <code>{u.id}</code>\n"
    )


def _donate_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for i, stars in enumerate(PRESET_STARS, start=1):
        row.append(InlineKeyboardButton(f"{stars} ⭐", callback_data=f"donate:{stars}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Своя сумма", callback_data="donate:custom")])
    return InlineKeyboardMarkup(rows)


def _relay_index(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, int]:
    # admin_message_id -> user_id
    if "relay_index" not in context.application.bot_data:
        context.application.bot_data["relay_index"] = {}
    return context.application.bot_data["relay_index"]


def _reply_prompts(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, int]:
    # prompt_message_id -> user_id
    if "reply_prompts" not in context.application.bot_data:
        context.application.bot_data["reply_prompts"] = {}
    return context.application.bot_data["reply_prompts"]


def _banned_users(context: ContextTypes.DEFAULT_TYPE) -> Set[int]:
    if "banned_users" not in context.application.bot_data:
        context.application.bot_data["banned_users"] = set()
    return context.application.bot_data["banned_users"]


def _flood_index(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, List[float]]:
    # user_id -> timestamps (monotonic)
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if context.args and context.args[0].lower() in {"donate", "gift"}:
        await menu_donate(update, context)
        return
    text = (
        "Привет! Я работаю как <b>автоответчик‑курьер</b>.\n\n"
        "Ты пишешь мне — я мгновенно доставляю сообщение админу.\n"
        "Если нужно — админ ответит тебе через меня.\n\n"
        "Кнопки снизу — для удобства (донат ⭐ и подсказка)."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU, parse_mode=ParseMode.HTML)


async def menu_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Я уже в режиме автоответчика 🙂\n\n"
        "Просто напиши сообщение (текст/фото/видео/голос/файл) — я доставлю его админу.",
        reply_markup=MAIN_MENU,
    )


async def menu_donate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    # Пользователь может сразу написать сумму сообщением
    context.user_data["awaiting_donate_amount"] = True
    text = (
        "Спасибо, что хочешь поддержать!\n\n"
        "Оплата — в <b>Telegram Stars</b> (официальная валюта Telegram).\n"
        "Выбери сумму кнопкой ниже или просто напиши число звёзд сообщением."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU, parse_mode=ParseMode.HTML)
    await update.message.reply_text("Сколько звёзд отправим? ⭐", reply_markup=_donate_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if context.user_data.get("awaiting_donate_amount"):
        context.user_data.pop("awaiting_donate_amount", None)
        await update.message.reply_text("Окей, отменил ввод суммы.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text("Окей.", reply_markup=MAIN_MENU)


async def gift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /gift <stars> — быстрый способ отправить Stars без кнопок.
    """
    if not update.message:
        return
    user = update.effective_user
    if not user or user.id == ADMIN_ID:
        return

    parts = (update.message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not re.fullmatch(r"\d{1,6}", parts[1]):
        await update.message.reply_text("Использование: /gift <кол-во_звёзд> (например: /gift 123)")
        return
    stars = int(parts[1])
    if stars <= 0:
        await update.message.reply_text("Сумма должна быть больше нуля.")
        return
    await _send_stars_invoice(user_id=user.id, stars=stars, context=context)


async def donate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith("donate:"):
        return

    value = data.split(":", 1)[1]
    if value == "custom":
        context.user_data["awaiting_donate_amount"] = True
        await q.message.reply_text(
            "Введи число звёзд (например: <code>123</code>). Чтобы отменить — /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        stars = int(value)
    except ValueError:
        await q.message.reply_text("Не понял сумму. Попробуй ещё раз.", reply_markup=_donate_keyboard())
        return

    context.user_data.pop("awaiting_donate_amount", None)
    await _send_stars_invoice(user_id=q.from_user.id, stars=stars, context=context)


async def _send_stars_invoice(
    *,
    user_id: int,
    stars: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    title = "Подарок"
    description = "Анонимный подарок"
    payload = f"donate_{user_id}_{stars}"

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
    # Не отправляем доп. подтверждение — Telegram сам показывает инвойс


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
                f"— <b>От</b>: {_h(user.first_name)}\n"
                f"— <b>Username</b>: {_h(username)}\n"
                f"— <b>ID</b>: <code>{user.id}</code>\n"
                f"— <b>Сумма</b>: <b>{total} ⭐</b>\n"
            ),
            parse_mode=ParseMode.HTML,
        )


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
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "admin":
        return

    action, raw_user_id = parts[1], parts[2]
    try:
        user_id = int(raw_user_id)
    except ValueError:
        return

    if action == "reply":
        prompt = await q.message.reply_text(
            "Напиши ответ пользователю (ответь <b>reply</b> на это сообщение):\n"
            f"— <b>ID</b>: <code>{user_id}</code>\n"
            f"— <b>Профиль</b>: <a href=\"{_h(_profile_url_by_user_id(user_id))}\">открыть</a>",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
        _reply_prompts(context)[prompt.message_id] = user_id
        return

    if action == "ban":
        banned = _banned_users(context)
        banned.add(user_id)
        _save_persistent_data(banned_users=banned)
        await q.message.reply_text(
            f"Готово. Пользователь <code>{user_id}</code> забанен: новые сообщения от него не будут приниматься.",
            parse_mode=ParseMode.HTML,
        )
        try:
            await q.edit_message_reply_markup(
                reply_markup=_admin_user_keyboard(
                    user_id=user_id,
                    profile_url=_profile_url_by_user_id(user_id),
                    is_banned=True,
                )
            )
        except Exception:
            pass
        return

    if action == "unban":
        banned = _banned_users(context)
        banned.discard(user_id)
        _save_persistent_data(banned_users=banned)
        await q.message.reply_text(
            f"Готово. Пользователь <code>{user_id}</code> разбанен.",
            parse_mode=ParseMode.HTML,
        )
        try:
            await q.edit_message_reply_markup(
                reply_markup=_admin_user_keyboard(
                    user_id=user_id,
                    profile_url=_profile_url_by_user_id(user_id),
                    is_banned=False,
                )
            )
        except Exception:
            pass
        return


async def admin_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    actor = update.effective_user
    if not actor or actor.id != ADMIN_ID:
        return

    # 1) ответ на ForceReply-подсказку (кнопка "Ответить")
    prompts = _reply_prompts(context)
    target_user_id = prompts.pop(msg.reply_to_message.message_id, None)
    if target_user_id:
        await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        await msg.reply_text("Доставлено пользователю ✅")
        raise ApplicationHandlerStop

    # 2) ответ reply на пересланное/скопированное сообщение пользователя
    relay = _relay_index(context)
    target_user_id2: Optional[int] = relay.get(msg.reply_to_message.message_id)
    if target_user_id2:
        await context.bot.copy_message(
            chat_id=target_user_id2,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        await msg.reply_text("Доставлено пользователю ✅")
        raise ApplicationHandlerStop


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


async def user_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if not user:
        return

    # Меню должно работать и у админа (например, для теста доната),
    # но сообщения админа не должны пересылаться самому себе.
    if user.id == ADMIN_ID:
        if _is_menu_press(msg.text, MENU_DONATE):
            await menu_donate(update, context)
            raise ApplicationHandlerStop
        if context.user_data.get("awaiting_donate_amount"):
            raw = (msg.text or "").strip()
            if not re.fullmatch(r"\d{1,6}", raw):
                await msg.reply_text(
                    "Нужно число звёзд (только цифры). Чтобы отменить — /cancel.",
                    reply_markup=MAIN_MENU,
                )
                raise ApplicationHandlerStop
            stars = int(raw)
            if stars <= 0:
                await msg.reply_text("Сумма должна быть больше нуля. Попробуй ещё раз.")
                raise ApplicationHandlerStop
            context.user_data.pop("awaiting_donate_amount", None)
            await _send_stars_invoice(user_id=user.id, stars=stars, context=context)
            raise ApplicationHandlerStop
        # Любые другие сообщения админа игнорируем (ответы обрабатываются отдельным хендлером)
        return

    if user.id in _banned_users(context):
        await msg.reply_text(
            "Упс. Этот автоответчик для тебя закрыт.\n"
            "Если думаешь, что это ошибка — попробуй связаться с админом другим способом.",
            reply_markup=MAIN_MENU,
        )
        raise ApplicationHandlerStop

    # Кнопки меню (текстом)
    if _is_menu_press(msg.text, MENU_WRITE):
        await menu_write(update, context)
        raise ApplicationHandlerStop
    if _is_menu_press(msg.text, MENU_DONATE):
        await menu_donate(update, context)
        raise ApplicationHandlerStop

    # Ввод своей суммы доната
    if context.user_data.get("awaiting_donate_amount"):
        raw = (msg.text or "").strip()
        if not re.fullmatch(r"\d{1,6}", raw):
            await msg.reply_text("Нужно число звёзд (только цифры). Чтобы отменить — /cancel.")
            raise ApplicationHandlerStop
        stars = int(raw)
        if stars <= 0:
            await msg.reply_text("Сумма должна быть больше нуля. Попробуй ещё раз.")
            raise ApplicationHandlerStop
        context.user_data.pop("awaiting_donate_amount", None)
        await _send_stars_invoice(user_id=user.id, stars=stars, context=context)
        raise ApplicationHandlerStop

    if _is_flooding(user_id=user.id, context=context):
        await msg.reply_text("Слишком быстро 🙂 Подожди 20–30 секунд и попробуй ещё раз.", reply_markup=MAIN_MENU)
        raise ApplicationHandlerStop

    # Автоответчик: всегда пересылаем админу
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

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=update.effective_chat.id,
        message_id=msg.message_id,
    )
    _relay_index(context)[copied.message_id] = user.id

    await msg.reply_text(
        "Принято! Я передал твоё сообщение админу. Если будет ответ — я доставлю его сюда.",
        reply_markup=MAIN_MENU,
    )
    raise ApplicationHandlerStop


def build_app() -> Application:
    if not BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Заполни BOT_TOKEN в config.py")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise RuntimeError("Заполни ADMIN_ID (целое число) в config.py")

    app = Application.builder().token(BOT_TOKEN).build()

    data = _load_persistent_data()
    banned = set(int(x) for x in (data.get("banned_users") or []) if str(x).isdigit())
    app.bot_data["banned_users"] = banned

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("gift", gift_cmd))
    app.add_handler(CommandHandler("bans", bans_list))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))

    app.add_handler(CallbackQueryHandler(donate_callback, pattern=r"^donate:"))
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^admin:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Ответы админа (reply): на ForceReply-подсказку или на скопированное сообщение пользователя
    app.add_handler(MessageHandler(filters.User(user_id=ADMIN_ID) & filters.REPLY, admin_reply_router))

    # Все сообщения пользователей (автоответчик)
    app.add_handler(MessageHandler(~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT, user_message_router))
    return app


if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)

