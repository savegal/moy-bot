"""Presentation helpers for the Telegram admin console."""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .ui import cb, kb

PAGE_SIZE = 8

ORDER_STATUS = {
    'pending': 'Ожидает оплаты',
    'review': 'На проверке',
    'paid': 'Оплачен',
    'expired': 'Счёт истёк',
    'failed': 'Закрыт',
    'refunded': 'Возврат',
}
PROVIDERS = {'stars': 'Telegram Stars', 'manual': 'Ручной перевод', 'admin': 'Ручная выдача',
             'cryptopay': 'Crypto Pay', '2328': 'Карта РФ (2328/1Fin)'}
GRANT_STATUS = {
    'need_link': 'В очереди', 'creating': 'Создаётся', 'ready': 'Ссылка готова',
    'joined': 'Пользователь вошёл', 'error': 'Ошибка', 'expired': 'Истёк', 'revoked': 'Отозван',
}


def order_amount(order: dict) -> str:
    if order['provider'] == 'admin':
        return 'Ручная выдача'
    if order['provider'] == 'stars':
        return f"{order['amount_stars']} ⭐"
    return f"{order['amount_rub']} ₽"


def short_date(value: str | None) -> str:
    return value[:16].replace('T', ' ') + ' UTC' if value else '—'


def pager(route: str, page: int, more: bool):
    buttons = []
    if page:
        buttons.append(cb('◀️', f'{route}:{page - 1}'))
    if more:
        buttons.append(cb('▶️', f'{route}:{page + 1}'))
    return buttons


def form_nav(label: str, route: str):
    return kb((cb(label, route),), (cb('🏠 Главное меню', 'adm:home'),))


async def screen(target: Message | CallbackQuery, text: str, *, reply_markup: InlineKeyboardMarkup):
    """Edit an admin screen in place. Open a screen from ordinary chat messages."""
    if isinstance(target, Message):
        return await target.answer(text, reply_markup=reply_markup)
    message = target.message
    if isinstance(message, Message) and message.text and message.from_user and message.from_user.is_bot:
        old = message.reply_markup
        if old and all((button.callback_data or '').startswith('adm:')
                       for row in old.inline_keyboard for button in row):
            try:
                return await message.edit_text(text, reply_markup=reply_markup)
            except TelegramBadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    return message
                if not any(reason in str(exc).lower() for reason in (
                    "message can't be edited", 'message to edit not found', 'message_id_invalid'
                )):
                    raise
    if isinstance(message, Message):
        return await message.answer(text, reply_markup=reply_markup)
    return await target.bot.send_message(target.from_user.id, text, reply_markup=reply_markup)
