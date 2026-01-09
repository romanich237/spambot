import html
import logging
from logging.handlers import RotatingFileHandler

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


ERROR_LOG_PATH = Path(__file__).with_name("errors.log")
_err_logger = logging.getLogger("bot-errors")
_err_logger.setLevel(logging.ERROR)
if not _err_logger.handlers:
    _handler = RotatingFileHandler(ERROR_LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _handler.setLevel(logging.ERROR)
    _handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    _err_logger.addHandler(_handler)


def _h(s: str) -> str:
    return html.escape(s or "")


def _norm_button_text(s: Optional[str]) -> str:
    # Некоторые клиенты могут присылать эмодзи без variation selector (FE0F)
    return (s or "").replace("\ufe0f", "").strip()


def _is_menu_press(text: Optional[str], expected: str) -> bool:
    return _norm_button_text(text) == _norm_button_text(expected)


def _parse_stars_amount(text: Optional[str]) -> Optional[int]:
    """
    Достаёт сумму Stars из сообщения.

    Принимает: "50", "50⭐", "50 ⭐", "⭐50", "gift 50" и т.п.
    Возвращает int или None, если нельзя понять однозначно.
    """
    s = (text or "").strip()
    if not s:
        return None
    nums = re.findall(r"\d{1,7}", s)
    if len(nums) != 1:
        return None
    try:
        stars = int(nums[0])
    except ValueError:
        return None
    if stars <= 0 or stars > 1_000_000:
        return None
    return stars


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


def _user_continue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(MENU_WRITE, callback_data="user:write")]])


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


def _broadcast_prompts(context: ContextTypes.DEFAULT_TYPE) -> Set[int]:
    # prompt_message_id set
    if "broadcast_prompts" not in context.application.bot_data:
        context.application.bot_data["broadcast_prompts"] = set()
    return context.application.bot_data["broadcast_prompts"]


def _users_db(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, object]]:
    # user_id(str) -> {username, first_name, last_name, first_seen, last_seen}
    if "users" not in context.application.bot_data:
        context.application.bot_data["users"] = {}
    return context.application.bot_data["users"]


def _touch_user(context: ContextTypes.DEFAULT_TYPE, u) -> None:
    users = _users_db(context)
    uid = str(u.id)
    now = int(time.time())
    rec = users.get(uid) or {}
    if "first_seen" not in rec:
        rec["first_seen"] = now
    rec["last_seen"] = now
    rec["username"] = u.username or ""
    rec["first_name"] = u.first_name or ""
    rec["last_name"] = u.last_name or ""
    users[uid] = rec


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


def _save_persistent_data(*, banned_users: Set[int], users: Optional[Dict[str, object]] = None) -> None:
    data: Dict[str, object] = {"banned_users": sorted(banned_users)}
    if users is not None:
        data["users"] = users
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def _persist(context: ContextTypes.DEFAULT_TYPE) -> None:
    _save_persistent_data(
        banned_users=_banned_users(context),
        users=_users_db(context),
    )


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
    # сбрасываем возможные “режимы ввода” при обычном /start
    context.user_data.pop("awaiting_donate_amount", None)
    text = (
        "Привет, я <b>бот обратной связи</b>.\n\n"
        "Просто напиши сообщение — я передам его администратору.\n"
        "Когда админ ответит, ты сможешь продолжить диалог прямо здесь."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU, parse_mode=ParseMode.HTML)


async def menu_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    # Если человек передумал донатить и пошёл писать — не трактуем следующий текст как сумму
    context.user_data.pop("awaiting_donate_amount", None)
    await update.message.reply_text(
        "Пиши сообщение — я сразу отправлю его администратору.\n\n"
        "Можно отправлять: текст, фото, видео, голос, файл.",
        reply_markup=MAIN_MENU,
    )


async def menu_donate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    # Пользователь может сразу написать сумму сообщением
    context.user_data["awaiting_donate_amount"] = True
    text = (
        "🎁 <b>Подарок</b>\n\n"
        "Напиши сумму звёздами одним сообщением (например: <code>50</code> или <code>50 ⭐</code>)\n"
        "или выбери кнопку ниже."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU, parse_mode=ParseMode.HTML)
    await update.message.reply_text("Выбери сумму:", reply_markup=_donate_keyboard())


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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Логируем только ошибки (в файл errors.log)
    try:
        _err_logger.exception("Unhandled error", exc_info=context.error)
    except Exception:
        pass

    # Дополнительно (лучше для поддержки): пингуем админа кратким сообщением
    try:
        err = context.error
        msg = f"⚠️ Ошибка в боте: {type(err).__name__}: {err}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg)
    except Exception:
        # если сломалось уведомление — молча игнорируем, чтобы не зациклиться
        pass


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
        _persist(context)
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
        _persist(context)
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
        await context.bot.send_message(
            chat_id=target_user_id,
            text="Если хочешь продолжить — нажми кнопку ниже и напиши сообщение.",
            reply_markup=_user_continue_keyboard(),
        )
        await msg.reply_text("Доставлено пользователю ✅")
        raise ApplicationHandlerStop

    # 1.5) рассылка: админ отвечает на prompt рассылки
    bprompts = _broadcast_prompts(context)
    if msg.reply_to_message.message_id in bprompts:
        bprompts.discard(msg.reply_to_message.message_id)
        users = _users_db(context)
        banned = _banned_users(context)
        targets = [int(uid) for uid in users.keys() if int(uid) != ADMIN_ID and int(uid) not in banned]
        ok = 0
        fail = 0
        for uid in targets:
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
                ok += 1
            except Exception:
                fail += 1
        await msg.reply_text(f"Рассылка завершена. Успешно: {ok}, ошибок: {fail}.")
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
        await context.bot.send_message(
            chat_id=target_user_id2,
            text="Если хочешь продолжить — нажми кнопку ниже и напиши сообщение.",
            reply_markup=_user_continue_keyboard(),
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
    _persist(context)
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
    _persist(context)
    await update.message.reply_text(f"Разбанен: <code>{user_id}</code>", parse_mode=ParseMode.HTML)


async def user_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if not user:
        return

    _touch_user(context, user)
    # сохраняем пользователей лениво (не идеально, но просто и надёжно)
    _persist(context)

    # Меню должно работать и у админа (например, для теста доната),
    # но сообщения админа не должны пересылаться самому себе.
    if user.id == ADMIN_ID:
        if _is_menu_press(msg.text, MENU_WRITE):
            context.user_data.pop("awaiting_donate_amount", None)
            await menu_write(update, context)
            raise ApplicationHandlerStop
        if _is_menu_press(msg.text, MENU_DONATE):
            await menu_donate(update, context)
            raise ApplicationHandlerStop
        if context.user_data.get("awaiting_donate_amount"):
            stars = _parse_stars_amount(msg.text)
            if stars is None:
                await msg.reply_text(
                    "Напиши сумму звёздами (например: <code>50</code> или <code>50 ⭐</code>). "
                    "Чтобы отменить — /cancel.",
                    reply_markup=MAIN_MENU,
                    parse_mode=ParseMode.HTML,
                )
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
        context.user_data.pop("awaiting_donate_amount", None)
        await menu_write(update, context)
        raise ApplicationHandlerStop
    if _is_menu_press(msg.text, MENU_DONATE):
        await menu_donate(update, context)
        raise ApplicationHandlerStop

    # Ввод своей суммы доната
    if context.user_data.get("awaiting_donate_amount"):
        stars = _parse_stars_amount(msg.text)
        if stars is None:
            await msg.reply_text(
                "Напиши сумму звёздами (например: <code>50</code> или <code>50 ⭐</code>). "
                "Чтобы отменить — /cancel.",
                reply_markup=MAIN_MENU,
                parse_mode=ParseMode.HTML,
            )
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


async def user_write_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    context.user_data.pop("awaiting_donate_amount", None)
    await q.message.reply_text(
        "Пиши сообщение — я передам его администратору.",
        reply_markup=MAIN_MENU,
    )


def _fmt_user_row(uid: str, rec: Dict[str, object]) -> str:
    name = " ".join([str(rec.get("first_name") or "").strip(), str(rec.get("last_name") or "").strip()]).strip()
    if not name:
        name = "Пользователь"
    username = str(rec.get("username") or "").strip()
    u = f"@{username}" if username else "—"
    return f"{name} | {u} | <code>{uid}</code>"


async def op_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    users = _users_db(context)
    banned = _banned_users(context)
    now = int(time.time())
    active_24h = 0
    for rec in users.values():
        last_seen = int(rec.get("last_seen") or 0)
        if now - last_seen <= 24 * 3600:
            active_24h += 1

    text = (
        "<b>/op — панель администратора</b>\n\n"
        f"— <b>Пользователей</b>: {len(users)}\n"
        f"— <b>Активных за 24ч</b>: {active_24h}\n"
        f"— <b>В бане</b>: {len(banned)}\n"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Пользователи", callback_data="op:users:0")],
            [InlineKeyboardButton("📣 Рассылка всем", callback_data="op:broadcast")],
        ]
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def op_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    actor = update.effective_user
    if not actor or actor.id != ADMIN_ID:
        await q.answer("Только для администратора.", show_alert=True)
        return
    await q.answer()

    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "op":
        return

    users = _users_db(context)
    banned = _banned_users(context)

    if parts[1] == "broadcast":
        prompt = await q.message.reply_text(
            "Рассылка всем пользователям.\n"
            "Ответь <b>reply</b> на это сообщение текстом/медиа — я разошлю всем (кроме забаненных).",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
        _broadcast_prompts(context).add(prompt.message_id)
        return

    if parts[1] == "users":
        page = 0
        if len(parts) >= 3 and (parts[2] or "").isdigit():
            page = int(parts[2])
        context.user_data["op_page"] = page

        sorted_ids = sorted(
            users.keys(),
            key=lambda uid: int((users[uid].get("last_seen") or 0)),
            reverse=True,
        )
        per = 10
        start = page * per
        chunk = sorted_ids[start : start + per]

        lines = [_fmt_user_row(uid, users[uid]) for uid in chunk] or ["(пока пусто)"]
        text = "<b>Пользователи</b>\n\n" + "\n".join(lines)

        kb_rows: List[List[InlineKeyboardButton]] = []
        for uid in chunk:
            rec = users[uid]
            name = " ".join([str(rec.get("first_name") or ""), str(rec.get("last_name") or "")]).strip() or uid
            kb_rows.append([InlineKeyboardButton(f"{name} ({uid})", callback_data=f"op:user:{uid}")])

        nav: List[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"op:users:{page-1}"))
        if start + per < len(sorted_ids):
            nav.append(InlineKeyboardButton("➡️", callback_data=f"op:users:{page+1}"))
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("↩️ Назад", callback_data="op:home")])

        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    if parts[1] == "home":
        # Перерисовываем панель без “трюков” с Update
        users = _users_db(context)
        banned = _banned_users(context)
        now = int(time.time())
        active_24h = 0
        for rec in users.values():
            last_seen = int(rec.get("last_seen") or 0)
            if now - last_seen <= 24 * 3600:
                active_24h += 1
        text = (
            "<b>/op — панель администратора</b>\n\n"
            f"— <b>Пользователей</b>: {len(users)}\n"
            f"— <b>Активных за 24ч</b>: {active_24h}\n"
            f"— <b>В бане</b>: {len(banned)}\n"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("👥 Пользователи", callback_data="op:users:0")],
                [InlineKeyboardButton("📣 Рассылка всем", callback_data="op:broadcast")],
            ]
        )
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if parts[1] == "user" and len(parts) >= 3:
        uid = parts[2]
        rec = users.get(uid)
        if not rec:
            await q.message.reply_text("Пользователь не найден.")
            return
        is_b = int(uid) in banned
        text = (
            "<b>Пользователь</b>\n"
            f"{_fmt_user_row(uid, rec)}\n\n"
            f"Профиль: <a href=\"{_h(_profile_url_by_user_id(int(uid)))}\">открыть</a>"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✉️ Написать", callback_data=f"op:msg:{uid}")],
                [
                    InlineKeyboardButton(
                        "✅ Разбанить" if is_b else "🚫 Забанить",
                        callback_data=f"op:{'unban' if is_b else 'ban'}:{uid}",
                    )
                ],
                [InlineKeyboardButton("↩️ К списку", callback_data=f"op:users:{int(context.user_data.get('op_page') or 0)}")],
            ]
        )
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if parts[1] in {"ban", "unban"} and len(parts) >= 3:
        uid = parts[2]
        if not uid.isdigit():
            return
        n = int(uid)
        if parts[1] == "ban":
            banned.add(n)
        else:
            banned.discard(n)
        _persist(context)
        await q.message.reply_text("Готово.")
        return

    if parts[1] == "msg" and len(parts) >= 3:
        uid = parts[2]
        if not uid.isdigit():
            return
        n = int(uid)
        prompt = await q.message.reply_text(
            "Напиши сообщение пользователю (ответь <b>reply</b> на это сообщение):\n"
            f"— <b>ID</b>: <code>{n}</code>\n"
            f"— <b>Профиль</b>: <a href=\"{_h(_profile_url_by_user_id(n))}\">открыть</a>",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
        _reply_prompts(context)[prompt.message_id] = n
        return


def build_app() -> Application:
    if not BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Заполни BOT_TOKEN в config.py")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise RuntimeError("Заполни ADMIN_ID (целое число) в config.py")

    app = Application.builder().token(BOT_TOKEN).build()

    data = _load_persistent_data()
    banned = set(int(x) for x in (data.get("banned_users") or []) if str(x).isdigit())
    app.bot_data["banned_users"] = banned
    users = data.get("users") if isinstance(data.get("users"), dict) else {}
    app.bot_data["users"] = users

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("gift", gift_cmd))
    app.add_handler(CommandHandler("op", op_cmd))
    app.add_handler(CommandHandler("bans", bans_list))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))

    app.add_handler(CallbackQueryHandler(donate_callback, pattern=r"^donate:"))
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(user_write_callback, pattern=r"^user:write$"))
    app.add_handler(CallbackQueryHandler(op_callback, pattern=r"^op:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Ответы админа (reply): на ForceReply-подсказку или на скопированное сообщение пользователя
    app.add_handler(MessageHandler(filters.User(user_id=ADMIN_ID) & filters.REPLY, admin_reply_router))

    # Все сообщения пользователей (автоответчик)
    app.add_handler(MessageHandler(~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT, user_message_router))
    app.add_error_handler(on_error)
    return app


if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)

