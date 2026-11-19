"""Owner-only in-chat admin console; every mutating route is authorization-gated."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import re

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message, MessageOriginChannel
from aiohttp import ClientError

from .admin_ui import GRANT_STATUS, ORDER_STATUS, PAGE_SIZE, PROVIDERS, form_nav, order_amount, pager, screen, short_date
from .alerts import dispatch_whale_alerts, safe_send
from .pushes import (
    AUDIENCE_LABELS, PROMO_PUSH_ELIGIBLE_SQL, build_review_push, int_setting, parse_push_discount, prepare_promo_push,
    push_settings, resolve_shared_promo, safe_send_media, safe_send_push_text,
    send_promo_push_to_user, send_review_push_to_user,
)
from .analytics import CAMPAIGNS, campaign_summary, checkout_summary, funnel_summary, product_sales
from .config import Config
from .db import DEFAULT_PROJECT_NAME, DEFAULT_SETTINGS, Database, contacts_visible, help_visible, in_seconds, now, reviews_visible
from .live_stats import get_live_tariff_buyers
from .payments import GatewayError, Payments
from .service import (
    MAX_DURATION_DAYS, Shop, ShopError, bind_order_channels, channels_of, parse_date, promo_discount_label, promo_has_discount,
    tariff_delivery, tariff_tier_kind, telegram_links,
)
from .access import AccessManager, validate_sale_channels
from .greetings import inspect_channel
from .ui import (
    MEDIA_TYPES, cb, kb, esc, duration, fmt_rub, has_product_photo, is_https_url, is_menu_navigation_text,
    product_text, rendered_section, send_section, send_text, tariff_view_media, validate_section_html,
)
from .handlers_user import refunded_stars, successful_stars

log = logging.getLogger(__name__)
router = Router(name='administration')

LUXURY_HUB_PROMO_TEXT = (
    "📢 🎁 <b>Дарим скидку! 💜</b>\n\n"
    "<code>{code}</code> - забирай промокод и наслаждайся 💜🎉😊\n"
    "Промокод активен только сегодня ⏳{ttl_note}\n"
    "Нажимай на кнопочку ниже 🔽"
)


class IsAdmin(Filter):
    async def __call__(self, event, config: Config) -> bool:
        return bool(event.from_user and event.from_user.id in config.admin_ids)


router.message.filter(F.chat.type == 'private', IsAdmin())
router.callback_query.filter(F.message.chat.type == 'private', IsAdmin())


@router.message.outer_middleware()
@router.callback_query.outer_middleware()
async def clear_admin_form(handler, event, data):
    config = data.get('config')
    user = getattr(event, 'from_user', None)
    if config and user and user.id in config.admin_ids:
        state = data.get('state')
        current_state = (await state.get_state()) if state else None
        in_admin_state = bool(current_state and current_state.startswith('AdminState:'))
        should_clear = False
        if isinstance(event, CallbackQuery):
            cb_data = event.data or ''
            if cb_data != 'adm:sendbroadcast' and not cb_data.startswith('adm:newt:back:'):
                if cb_data.startswith('adm:') or in_admin_state:
                    should_clear = True
        elif isinstance(event, Message):
            if in_admin_state and is_menu_navigation_text(event.text):
                should_clear = True
        if should_clear and state:
            _clear_broadcast_albums(user.id)
            await state.clear()
            data['raw_state'] = None
    return await handler(event, data)


class AdminState(StatesGroup):
    tariff_field = State()
    tariff_new = State()
    tariff_media = State()
    section_field = State()
    promo_new = State()
    settings_field = State()
    grant = State()
    extension = State()
    revoke = State()
    lookup_user = State()
    lookup_order = State()
    broadcast = State()
    broadcast_confirm = State()
    greeting_channel = State()
    purchase_links = State()
    crypto_token = State()
    promo_push_field = State()
    review_add = State()
    review_caption = State()
    review_push_field = State()


SECTION_NAMES = ['welcome', 'welcome_back', 'join_welcome', 'catalog', 'profile', 'payment', 'help', 'info', 'stats']
MEDIA_ONLY = {'profile', 'payment', 'stats'}
SECTION_TITLES = {
    'welcome': '💜 Приветствие (новый пользователь)',
    'welcome_back': '💜 Приветствие (с возвращением)',
    'join_welcome': '👋 Приветка по заявке в канал',
    'catalog': '🔐 Каталог (описание)',
    'profile': '👤 Профиль / Промокод (баннер)',
    'payment': '💳 Оформление покупки (баннер)',
    'help': '❓ Помощь и FAQ',
    'info': 'ℹ️ Информация о проекте',
    'stats': '📊 Баннер статистики',
}
SECTION_HINTS = {
    'welcome': 'Первое сообщение, когда новый пользователь впервые жмёт /start.',
    'welcome_back': 'Приветствие при повторном /start от уже знакомого пользователя.',
    'join_welcome': 'Сообщение в ЛС сразу после подачи заявки в открытый канал («Приветка»).',
    'catalog': 'Шапка над кнопками тарифов в разделе «🔞 Каталог фуллов».',
    'help': 'Экран «💬 Помощь».',
    'info': 'Экран «💎 Гарантии клуба» в нижнем меню.',
    'profile': 'Только медиа-баннер над личным кабинетом (текст профиля собирается автоматически).',
    'payment': 'Только медиа-баннер на шаге оплаты (сумма и тариф подставляются автоматически).',
    'stats': 'Только медиа-баннер для экрана «🔥 Live-покупки».',
}
_RICH_ENTITY_TYPES = frozenset({
    'bold', 'italic', 'underline', 'strikethrough', 'spoiler',
    'code', 'pre', 'text_link', 'blockquote', 'expandable_blockquote', 'custom_emoji',
})
_RAW_HTML_TAG_RE = re.compile(
    r'</?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|blockquote|tg-spoiler|tg-emoji)\b',
    re.IGNORECASE,
)


def _utf16_len(text: str) -> int:
    return len((text or '').encode('utf-16-le')) // 2


def _section_has_media(row: dict | None) -> bool:
    return bool(row and row.get('media_id') and row.get('media_type') in MEDIA_TYPES)


def _extract_media_upload(message: Message) -> tuple[str, str] | None:
    """Extract (file_id, media_type) from photo/video/animation or ('', '') on 'удалить'."""
    if message.photo:
        return message.photo[-1].file_id, 'photo'
    if getattr(message, 'video', None):
        return message.video.file_id, 'video'
    if getattr(message, 'animation', None):
        return message.animation.file_id, 'animation'
    if (message.text or '').strip().lower() == 'удалить':
        return '', ''
    return None


def _tariff_caption_warning(tariff: dict) -> str:
    """Warn admin if product card or preview text exceeds Telegram's 950 UTF-16 caption budget."""
    warnings = []
    if has_product_photo(tariff):
        card_len = _utf16_len(product_text(tariff, tariff['stars'], buyers=1000))
        if card_len > 950:
            warnings.append(f'Карточка: {card_len}/950')
    prev_id, prev_type = tariff_view_media(tariff, details=True)
    if prev_id and prev_type in MEDIA_TYPES:
        prev_len = _utf16_len(product_text(tariff, tariff['stars'], buyers=None))
        if prev_len > 950:
            warnings.append(f'Что внутри: {prev_len}/950')
    if not warnings:
        return ''
    return f"⚠️ Превышен лимит подписи с медиа ({', '.join(warnings)}) — текст уйдёт 2-м сообщением."


def _tariff_rate_warning(tariff: dict) -> str:
    """Warn admin if Stars price diverges significantly from the store's ~1.92 RUB/Star rate."""
    rub = int(tariff.get('rub') or 0)
    stars = int(tariff.get('stars') or 0)
    if rub < 20 or stars <= 0:
        return ''
    rec = max(1, round(rub / 1.92 / 5) * 5) if rub >= 30 else max(1, round(rub / 1.92))
    if abs(stars - rec) / max(rec, 1) > 0.15:
        return (
            f"⚠️ Перекос цены в Stars: <b>{fmt_rub(stars)} ⭐</b> при <b>{fmt_rub(rub)} ₽</b> "
            f"(по стандартному курсу ~1.92 ₽/⭐ обычно ~<b>{fmt_rub(rec)} ⭐</b>)."
        )
    return ''


def _section_variables_hint(section: str) -> str:
    items = [
        ('<code>{name}</code>', 'имя пользователя'),
        ('<code>{project_name}</code>', 'название проекта'),
    ]
    if section == 'join_welcome':
        items.append(('<code>{channel}</code>', 'название канала заявки'))
    elif section == 'help':
        items.append(('<code>{bot_username}</code>', 'юзернейм бота (для ссылок)'))
    return '\n'.join(f'• {tag} — {desc}' for tag, desc in items)


def _section_length_line(text: str, has_media: bool) -> str:
    length = _utf16_len(text)
    if length > 950:
        status = ('⚠️ с баннером кнопки уйдут вторым сообщением (лимит 950)'
                  if has_media else '⚠️ длиннее 950 (если добавите баннер, пост разобьётся на два)')
    elif has_media:
        status = '✅ уместится в один пост с баннером (лимит 950)'
    else:
        status = '✅ до 950 (подойдёт и для поста с баннером)'
    return f'📏 Длина: <b>{length} / 950</b> · {status}'


def _extract_section_html(message: Message) -> str:
    """Convert rich Telegram entities to HTML unless the admin explicitly pasted raw HTML tags."""
    text = message.text or ''
    entities = getattr(message, 'entities', None) or []
    if any(getattr(e, 'type', None) in _RICH_ENTITY_TYPES for e in entities) and not _RAW_HTML_TAG_RE.search(text):
        return getattr(message, 'html_text', None) or text
    return text


def _extract_caption_html(message: Message) -> str:
    """Convert rich caption entities to HTML unless raw HTML tags were pasted."""
    text = message.caption or ''
    entities = getattr(message, 'caption_entities', None) or []
    if any(getattr(e, 'type', None) in _RICH_ENTITY_TYPES for e in entities) and not _RAW_HTML_TAG_RE.search(text):
        return getattr(message, 'html_text', None) or text
    return text

FIELD_NAMES = {'title': 'Название', 'description': 'Описание', 'rub': 'Цена ₽ (Crypto / база скидки)',
               'stars': 'Цена Stars', 'duration_days': 'Срок (0=бессрочно)',
               'channel_ids': 'Каналы', 'preview_description': 'Что внутри?'}


def home_kb(review: int, delivery: int):
    return kb(
        (cb(f'🧾 На проверке · {review}', 'adm:orders:review:0', style='primary' if review else None),
         cb(f'⚠️ Выдача · {delivery}', 'adm:orders:delivery:0', style='danger' if delivery else None)),
        (cb('💳 Заказы', 'adm:orders'), cb('💰 Платежи', 'adm:payments'), cb('👥 Пользователи', 'adm:users')),
        (cb('📦 Товары', 'adm:tariffs'), cb('🔑 Доступы', 'adm:grants')),
        (cb('👋 Приветка', 'adm:greetings', style='primary'), cb('🖼 Тексты и баннеры', 'adm:sections')),
        (cb('🎫 Промокоды', 'adm:promos'), cb('📢 Рассылка', 'adm:broadcast')),
        (cb('🎁 Пуши промокода', 'adm:ppush'), cb('⭐ Пуши отзывов', 'adm:rpush')),
        (cb('📊 Аналитика', 'adm:stats'), cb('📈 Воронка', 'adm:funnel:7:0'), cb('📋 Журнал', 'adm:audit')),
        (cb('🩺 Готовность', 'adm:health'), cb('⚙️ Настройки', 'adm:settings')),
        (cb('🔄 Обновить', 'adm:home'), cb('Закрыть', 'adm:close')),
    )


DELIVERY_CONDITION = """o.status='paid' AND o.refund_requested_at IS NULL AND o.notice_sent=0 AND EXISTS (
    SELECT 1 FROM purchases p WHERE p.order_id=o.id AND (p.expires_at IS NULL OR p.expires_at>?)
)"""


async def sales_summary(db: Database, days: int):
    cutoff = in_seconds(-days * 86400)
    return await db.read("""SELECT COUNT(*) AS paid,
        COALESCE(SUM(CASE WHEN provider='stars' THEN amount_stars ELSE 0 END),0) AS stars,
        COALESCE(SUM(CASE WHEN provider!='stars' THEN amount_rub ELSE 0 END),0) AS rub,
        (SELECT COUNT(*) FROM users WHERE created_at>=?) AS new_users
        FROM orders WHERE status='paid' AND provider!='admin' AND refund_requested_at IS NULL AND paid_at>=?""", (cutoff, cutoff))


async def admin_home(target: Message | CallbackQuery, db: Database):
    sales = await sales_summary(db, 1)
    queue = await db.read(f"""SELECT
        (SELECT COUNT(*) FROM orders WHERE status='review') AS review,
        (SELECT COUNT(*) FROM orders o WHERE {DELIVERY_CONDITION}) AS delivery,
        (SELECT COUNT(*) FROM users) AS users,
        (SELECT value='1' FROM settings WHERE key='greeting_enabled') AS greeting,
        (SELECT value FROM settings WHERE key='support_url') AS support,
        (SELECT value FROM settings WHERE key='contacts_visible') AS contacts_visible,
        (SELECT value FROM settings WHERE key='help_visible') AS help_visible,
        (SELECT value FROM settings WHERE key='project_name') AS name""", (now(),))
    support_status = 'настроена' if is_https_url(queue['support'] or '') else 'не указана'
    if queue['contacts_visible'] == '0':
        support_status = 'скрыта'
    text = (
        f"<b>🛡 {esc(queue['name'] or DEFAULT_PROJECT_NAME)} · Панель управления</b>\n\n"
        f"<b>За последние 24 часа</b>\n"
        f"Продажи · <b>{sales['paid']}</b> · {sales['stars']} ⭐ · {sales['rub']} ₽\n"
        f"Новые пользователи · <b>{sales['new_users']}</b> (всего {queue['users']})\n\n"
        f"<b>Требует внимания</b>\n"
        f"На проверке · <b>{queue['review']}</b> · в очереди выдачи · <b>{queue['delivery']}</b>\n\n"
        f"👋 Приветка · <b>{'включена' if queue['greeting'] else 'выключена'}</b>\n"
        f"💬 Поддержка · <b>{support_status}</b>\n"
        f"❓ Помощь · <b>{'скрыта' if queue['help_visible'] == '0' else 'открыта'}</b>"
    )
    await screen(target, text, reply_markup=home_kb(queue['review'], queue['delivery']))


@router.message(Command('admin'))
@router.message(F.text.in_({'🛡 Панель управления', '🛡 Админ-панель', 'Панель управления', 'Админ-панель'}))
async def admin_command(message: Message, state: FSMContext, db: Database):
    _clear_broadcast_albums(message.from_user.id)
    await state.clear()
    await admin_home(message, db)


@router.message(F.successful_payment)
async def admin_stars_receipt(message: Message, shop: Shop, config: Config | None = None,
                              access: AccessManager | None = None):
    # Must run before stateful admin message handlers; an admin may also buy a tariff.
    await successful_stars(message, shop, config, access)


@router.message(F.refunded_payment)
async def admin_stars_refund(message: Message, shop: Shop):
    await refunded_stars(message, shop)


@router.callback_query(F.data == 'adm:home')
async def home_callback(call: CallbackQuery, db: Database):
    await call.answer()
    await admin_home(call, db)


@router.callback_query(F.data == 'adm:close')
async def close_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == 'adm:stats')
@router.callback_query(F.data.regexp(r'^adm:stats:(1|7|30)$'))
async def stats(call: CallbackQuery, db: Database, shop: Shop):
    await call.answer()
    days = int(call.data.split(':')[-1]) if call.data.count(':') == 2 else 7
    sales = await sales_summary(db, days)
    totals = await shop.statistics()
    checkout = await checkout_summary(db, days)
    text = (f"<b>📊 Аналитика · {days} дн.</b>\n\n"
            f"Оплачено заказов · <b>{sales['paid']}</b>\n"
            f"Выручка Stars · <b>{sales['stars']} ⭐</b>\n"
            f"Выручка в рублях · <b>{sales['rub']} ₽</b>\n"
            f"Новых пользователей · <b>{sales['new_users']}</b>\n\n"
            f"<b>Воронка новых пользователей</b>\n"
            f"Запустили бота · <b>{checkout['started']}</b>\n"
            f"Получили счёт · <b>{checkout['checkout']}</b> · {conversion(checkout['checkout'], checkout['started'])}\n"
            f"Купили · <b>{checkout['bought']}</b> · {conversion(checkout['bought'], checkout['checkout'])} от получивших счёт\n"
            f"Конверсия из запуска в покупку · <b>{conversion(checkout['bought'], checkout['started'])}</b>\n\n"
            f"Всего пользователей в базе · <b>{totals['users']}</b>\n"
            f"Выдано и готово приглашений · <b>{totals['grants']}</b>\n\n"
            "Возвраты и ручные выдачи исключены из продаж.")
    await screen(call, text, reply_markup=kb(
        tuple(cb(label, f'adm:stats:{value}', style='primary' if value == days else None)
              for value, label in ((1, '24 часа'), (7, '7 дней'), (30, '30 дней'))),
        (cb('👋 Воронка по заявкам', f'adm:funnel:{days}:0'), cb('📦 Продажи товаров', f'adm:products:{days}:0')),
        (cb('📈 Конверсия дожимов', f'adm:campaigns:{days}'),),
        (cb('🏠 Главное меню', 'adm:home'),)))


REPORT_PERIODS = ((1, '24 часа'), (7, '7 дней'), (30, '30 дней'))


def conversion(part: int, total: int) -> str:
    return f'{part / total:.1%}'.replace('.', ',') if total else '—'


@router.callback_query(F.data.regexp(r'^adm:campaigns:(1|7|30)$'))
async def campaign_report(call: CallbackQuery, db: Database):
    await call.answer()
    days = int(call.data.rsplit(':', 1)[1])
    metrics = {row['campaign']: row for row in await campaign_summary(db, days)}
    lines = []
    for key, label in CAMPAIGNS.items():
        row = metrics.get(key)
        if not row:
            lines.append(f'<b>{label}</b> · 0 получателей')
            continue
        lines.append(
            f"<b>{label}</b>\n"
            f"Получили {row['sent']} · нажали {row['clicked']} ({conversion(row['clicked'], row['sent'])})\n"
            f"Купили {row['buyers']} ({conversion(row['buyers'], row['sent'])} от получателей, "
            f"{conversion(row['buyers'], row['clicked'])} от нажавших) · заказов {row['orders']}\n"
            f"Выручка {row['stars']} ⭐ · {row['rub']} ₽"
        )
    text = (
        f"<b>📈 Конверсия дожимов · {days} дн.</b>\n\n"
        + '\n\n'.join(lines)
        + '\n\n<i>Последний переход по кнопке перед оформлением счёта. '
          'Покупки учитываются в течение 7 дней с отправки. Учитываются отправки с момента включения отчёта.</i>'
    )
    await screen(call, text, reply_markup=kb(
        tuple(cb(label, f'adm:campaigns:{value}', style='primary' if value == days else None)
              for value, label in REPORT_PERIODS),
        (cb('◀️ Аналитика', f'adm:stats:{days}'),),
    ))


@router.callback_query(F.data.regexp(r'^adm:funnel:(1|7|30):(?:0|-[0-9]{1,16})$'))
async def greeting_funnel(call: CallbackQuery, db: Database):
    await call.answer()
    _, _, period, channel_id = call.data.split(':')
    days, chat_id = int(period), int(channel_id)
    funnel = await funnel_summary(db, days, chat_id or None)
    channel = await db.read('SELECT title FROM welcome_channels WHERE chat_id=?', (chat_id,)) if chat_id else None
    title = (channel['title'] if channel else '') or (str(chat_id) if chat_id else 'Все каналы')
    requested, delivered, engaged = (funnel[key] for key in ('requested', 'delivered', 'engaged'))
    bought, engaged_bought = funnel['bought'], funnel['engaged_bought']
    text = (f"<b>👋 Воронка по заявкам · {days} дн.</b>\n{esc(title[:100])}\n\n"
            f"1. Подали заявку <b>{requested}</b>\n"
            f"2. Получили сообщение <b>{delivered}</b> · {conversion(delivered, requested)} от заявителей\n"
            f"3. Взаимодействовали с ботом <b>{engaged}</b> · {conversion(engaged, delivered)} от получивших\n\n"
            f"<b>Купили из получивших</b> {bought} из {delivered} · <b>{conversion(bought, delivered)}</b>\n"
            f"<b>Купили после взаимодействия</b> {engaged_bought} из {engaged} · <b>{conversion(engaged_bought, engaged)}</b>\n\n"
            'В каждой строке считаются уникальные люди. Покупка относится к последней привязке перед счётом '
            'в пределах 30 дней. Возвраты и ручные выдачи исключены.\n\n'
            'Период отчёта отбирает дату заявки; последующие покупки учитываются у этой когорты.')
    rows = [tuple(cb(label, f'adm:funnel:{value}:{chat_id}', style='primary' if value == days else None)
                  for value, label in REPORT_PERIODS)]
    if chat_id:
        rows.append((cb('◀️ Все каналы', f'adm:funnel:{days}:0'),))
    else:
        channels = await db.read("""SELECT e.chat_id,c.title,COUNT(DISTINCT e.user_id) AS n
            FROM greeting_events e LEFT JOIN welcome_channels c ON c.chat_id=e.chat_id
            WHERE e.requested_at>=? GROUP BY e.chat_id ORDER BY n DESC,e.chat_id LIMIT 6""",
            (int(datetime.now(timezone.utc).timestamp()) - days * 86400,), many=True)
        rows.extend((cb(f"#{c['chat_id']} · {(c['title'] or 'Канал')[:25]} · {c['n']}",
                        f"adm:funnel:{days}:{c['chat_id']}"),) for c in channels)
    rows.append((cb('📦 Продажи товаров', f'adm:products:{days}:0'), cb('◀️ Аналитика', f'adm:stats:{days}')))
    await screen(call, text, reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:products:(1|7|30):[0-9]{1,6}$'))
async def products_report(call: CallbackQuery, db: Database):
    await call.answer()
    _, _, period, position = call.data.split(':')
    days, page = int(period), int(position)
    items = await product_sales(db, days, PAGE_SIZE + 1, page * PAGE_SIZE)
    lines = [f"#{item['id']} · <b>{esc(item['title'][:75])}</b>\n"
             f"{item['sales']} покупок · {item['buyers']} покупателей · {item['stars']} ⭐ · {item['rub']} ₽"
             for item in items[:PAGE_SIZE]]
    text = f"<b>📦 Продажи товаров · {days} дн.</b>\nСтраница {page + 1}\n\n"
    await screen(call, text + ('\n\n'.join(lines) or 'За этот период продаж нет.'), reply_markup=kb(
        tuple(cb(label, f'adm:products:{value}:0', style='primary' if value == days else None)
              for value, label in REPORT_PERIODS),
        pager(f'adm:products:{days}', page, len(items) > PAGE_SIZE),
        (cb('👋 Воронка', f'adm:funnel:{days}:0'), cb('◀️ Аналитика', f'adm:stats:{days}'))))


AUDIT_ACTIONS = {
    'tariff_new': 'Создание товара', 'tariff_edit': 'Изменение товара',
    'tariff_media': 'Обложка товара', 'tariff_preview_media': 'Медиа «Что внутри?»',
    'section_edit': 'Изменение раздела', 'setting_edit': 'Настройки', 'promo_upsert': 'Сохранение промокода',
    'promo_toggle': 'Статус промокода', 'greeting_setting': 'Режим приветки',
    'greeting_channel': 'Настройки канала', 'greeting_add_channel': 'Подключение канала',
    'manual_payment_approved': 'Ручной платёж подтверждён', 'manual_payment_rejected': 'Ручной платёж отклонён',
    'stars_refund_requested': 'Запрошен возврат Stars', 'stars_refunded': 'Возвращены Stars',
    'grant_access': 'Ручная выдача', 'extend': 'Продление', 'revoke': 'Отзыв доступа',
    'retry_access': 'Повтор выдачи', 'replace_purchase_links': 'Замена ссылок в покупке', 'broadcast': 'Рассылка',
    'broadcast_stopped': 'Остановка рассылки', 'tariff_tier': 'Класс тарифа',
    'retry_revocation': 'Повтор отзыва доступа',
    'promo_push_setting': 'Настройка пушей промокода',
    'promo_push_blast': 'Рассылка пушей промокода',
    'review_add': 'Добавление скрина отзыва',
    'review_edit': 'Изменение отзыва',
    'review_delete': 'Удаление отзыва',
    'review_push_setting': 'Настройка пушей отзывов',
    'review_push_blast': 'Рассылка отзывов',
}


@router.callback_query(F.data == 'adm:audit')
@router.callback_query(F.data.regexp(r'^adm:audit:[0-9]{1,6}$'))
async def audit_log(call: CallbackQuery, db: Database):
    await call.answer()
    page = int(call.data.split(':')[-1]) if call.data.count(':') == 2 else 0
    entries = await db.read('SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?',
                            (PAGE_SIZE + 1, page * PAGE_SIZE), many=True)
    lines = [f"• <b>{esc(AUDIT_ACTIONS.get(entry['action'], entry['action']))}</b>\n"
             f"  {short_date(entry['created_at'])} · <code>{entry['actor']}</code>\n"
             f"  <code>{esc(entry['subject'][:100])}</code>" for entry in entries[:PAGE_SIZE]]
    await screen(call, f"<b>📋 Журнал · стр. {page + 1}</b>\n\n" + ('\n\n'.join(lines) or 'Записей нет.'),
                 reply_markup=kb(pager('adm:audit', page, len(entries) > PAGE_SIZE),
                                 (cb('🏠 Главное меню', 'adm:home'),)))


@router.callback_query(F.data == 'adm:health')
async def readiness(call: CallbackQuery, shop: Shop, db: Database, bot: Bot):
    await call.answer()
    lines = []
    greeting = (await db.setting('greeting_enabled'))['value'] == '1'
    lines.append('👋 Приветка включена. Каналы проверяются в разделе «Приветка».' if greeting
                 else '⚪ Приветка выключена')
    for tariff in await shop.catalogs():
        label = f"#{tariff['id']} {tariff['title'][:45]}"
        try:
            channels, urls = tariff_delivery(tariff)
            if not has_product_photo(tariff):
                raise ShopError('Загрузите фото или видео товара.')
            if not 0 <= tariff['duration_days'] <= MAX_DURATION_DAYS:
                raise ShopError('Некорректный срок доступа.')
            if channels:
                await validate_sale_channels(bot, channels)
        except ValueError as exc:
            lines.append(f"❌ {label} · {exc}")
        else:
            if urls:
                mode = f'готовых ссылок: {len(urls)}'
            elif channels:
                mode = f'каналов с проверенными правами: {len(channels)}'
            else:
                mode = 'продажа без канала'
            lines.append(f"✅ {label} · {mode}")
    body = esc('\n'.join(lines)[:2900])
    await screen(call, f"<b>🩺 Готовность магазина</b>\n\n{body}", reply_markup=kb(
        (cb('📦 Товары', 'adm:tariffs'), cb('⚙️ Настройки', 'adm:settings')),
        (cb('🏠 Главное меню', 'adm:home'),)))


async def payment_settings_screen(target: Message | CallbackQuery, db: Database, payments: Payments,
                                  *, notice: str = ''):
    crypto = payments.crypto
    crypto_status = (
        '🟢 оплата включена (кнопка CryptoBot видна пользователям)'
        if payments.crypto_enabled else
        '⚪ оплата выключена (кнопка CryptoBot скрыта)'
        if crypto.token else
        '⚪ выключен (требуется API-токен, кнопка скрыта)'
    )
    text = (
        '<b>💰 Платежи</b>\n\n'
        '<b>Telegram Stars</b> · оплата цифрового доступа\n\n'
        f"<b>CryptoBot (Crypto Pay)</b> · {crypto_status}\n"
        f"Сеть · {'тестовая' if crypto.testnet else 'рабочая'}\n"
        '<code>/webhooks/cryptopay</code> · путь для HTTPS-вебхука'
    )
    if notice:
        text += '\n\n' + notice
    rows = [
        (cb('⭐ Проверить Stars API', 'adm:payments:stars'), cb('🩺 Готовность магазина', 'adm:health')),
        (cb('📦 Товары', 'adm:tariffs'),),
        (cb('🔑 Рабочий токен Crypto Pay', 'adm:crypto:token:0'),),
        (cb('🧪 Тестовый токен Crypto Pay', 'adm:crypto:token:1'),),
    ]
    if crypto.token:
        rows.append((
            cb('🧪 Проверить Crypto Pay', 'adm:crypto:check'),
            cb('🔴 Выключить CryptoBot', 'adm:crypto:enable:0', style='danger')
            if payments.crypto_enabled else
            cb('🟢 Включить CryptoBot', 'adm:crypto:enable:1', style='success'),
        ))
    rows.append((cb('🏠 Главное меню', 'adm:home'),))
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data == 'adm:payments')
async def payment_settings(call: CallbackQuery, db: Database, payments: Payments):
    await call.answer()
    await payment_settings_screen(call, db, payments)


@router.callback_query(F.data == 'adm:payments:stars')
async def check_stars_api(call: CallbackQuery, bot: Bot, db: Database, payments: Payments):
    await call.answer()
    try:
        await bot.get_star_transactions(limit=1, request_timeout=5)
    except (TelegramAPIError, OSError, TimeoutError):
        notice = '❌ Telegram не подтвердил доступ к журналу Stars.'
    else:
        notice = '✅ Журнал Stars доступен. Проверить товары можно в разделе «Готовность магазина».'
    await payment_settings_screen(call, db, payments, notice=notice)


@router.callback_query(F.data.regexp(r'^adm:crypto:token:[01]$'))
async def crypto_token_start(call: CallbackQuery, state: FSMContext):
    testnet = call.data.endswith(':1')
    await state.set_state(AdminState.crypto_token)
    await state.update_data(crypto_testnet=testnet)
    await call.answer()
    await screen(call, 'Пришли API-токен приложения из @CryptoTestnetBot.' if testnet else
                 'Пришли API-токен приложения из @CryptoBot → Crypto Pay.',
                 reply_markup=form_nav('◀️ Отменить', 'adm:payments'))


@router.message(AdminState.crypto_token, ~F.text.startswith('/'))
async def crypto_token_save(message: Message, bot: Bot, state: FSMContext, db: Database, payments: Payments):
    if not message.text or not message.text.strip():
        return await message.answer('Пришли API-токен одним текстовым сообщением.')
    try:
        deleted = await bot.delete_message(message.chat.id, message.message_id, request_timeout=5)
    except (TelegramAPIError, OSError, TimeoutError):
        deleted = False
    if not deleted:
        return await message.answer('Telegram не удалил сообщение с токеном. Удали его и повтори ввод.')
    testnet = (await state.get_data())['crypto_testnet']
    try:
        await payments.configure_crypto(token=message.text.strip(), testnet=testnet)
    except (GatewayError, ClientError, OSError, TimeoutError):
        return await message.answer('Не удалось проверить или сохранить токен. Проверь сеть и токен, затем повтори.')
    await state.clear()
    await db.audit(message.from_user.id, 'crypto_pay_token_updated', 'testnet' if testnet else 'mainnet')
    await payment_settings_screen(message, db, payments, notice='✅ Crypto Pay подтвердил токен. Настройки сохранены.')


@router.callback_query(F.data == 'adm:crypto:check')
@router.callback_query(F.data.regexp(r'^adm:crypto:enable:[01]$'))
async def crypto_action(call: CallbackQuery, db: Database, payments: Payments):
    await call.answer()
    try:
        if call.data == 'adm:crypto:check':
            await payments.crypto.check_connection()
            notice = '✅ Crypto Pay ответил на запрос.'
        else:
            enabled = call.data.endswith(':1')
            await payments.configure_crypto(enabled=enabled)
            await db.audit(call.from_user.id, 'crypto_pay_reconciliation', str(int(enabled)))
            notice = ('✅ Оплата CryptoBot включена (кнопка появилась у покупателей).' if enabled
                      else '⚪ Оплата CryptoBot выключена (кнопка скрыта у покупателей).')
    except (GatewayError, ClientError, OSError, TimeoutError):
        notice = '❌ Не удалось выполнить операцию. Проверь токен и доступность Crypto Pay.'
    await payment_settings_screen(call, db, payments, notice=notice)


async def greetings_panel(target: Message | CallbackQuery, db: Database, page: int = 0):
    switches = await db.read("SELECT key,value FROM settings WHERE key IN ('greeting_enabled','greeting_auto_new')",
                             many=True)
    values = {row['key']: row['value'] == '1' for row in switches}
    enabled, automatic = values['greeting_enabled'], values['greeting_auto_new']
    page_size = 8
    channels = await db.read("""SELECT * FROM welcome_channels ORDER BY is_admin DESC, title, chat_id
        LIMIT ? OFFSET ?""", (page_size + 1, page * page_size), many=True)
    rows = [
        (cb('⏸ Выключить приветствие' if enabled else '▶️ Включить приветствие',
            f'adm:greetswitch:main:{int(not enabled)}', style='danger' if enabled else 'success'),),
        (cb('⚡ Новые каналы: авто' if automatic else '⚪ Новые каналы: вручную',
            f'adm:greetswitch:auto:{int(not automatic)}'),),
    ]
    protected = await asyncio.gather(*(db.paid_channel(c['chat_id']) for c in channels[:page_size]))
    for channel, paid in zip(channels, protected):
        if paid:
            status = '🔒'
        elif not channel['is_admin'] or not channel['can_invite']:
            status = '⚠️'
        else:
            status = '🟢' if enabled and channel['enabled'] else '⚪'
        rows.append((cb(f"{status} {channel['title'][:42] or channel['chat_id']}",
                        f"adm:greet:{channel['chat_id']}"),))
    navigation = []
    if page:
        navigation.append(cb('◀️', f'adm:greetings:{page - 1}'))
    if len(channels) > page_size:
        navigation.append(cb('▶️', f'adm:greetings:{page + 1}'))
    rows.extend((navigation, (cb('➕ Добавить канал', 'adm:greetadd'),
                             cb('🖼 Текст и баннер', 'adm:section:join_welcome')),
                 (cb('◀️ Меню', 'adm:home'),)))
    await screen(target,
        '<b>👋 Приветка по заявкам</b>\n\n'
        f"Общий режим · <b>{'включён' if enabled else 'выключен'}</b>\n"
        f"Новые каналы · <b>{'автовключение' if automatic else 'ручное включение'}</b>\n\n"
        'Добавьте бота в канал как администратора с правом приглашать пользователей. '
        'Канал появится здесь после обновления прав или первой заявки. Уже известные каналы сохраняют свой режим. '
        'Платные каналы отмечены 🔒.',
        reply_markup=kb(*rows),
    )


@router.callback_query(F.data == 'adm:greetings')
@router.callback_query(F.data.regexp(r'^adm:greetings:[0-9]{1,6}$'))
async def greetings_menu(call: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    await call.answer()
    await greetings_panel(call, db, int(call.data.split(':')[-1]) if call.data.count(':') == 2 else 0)


@router.callback_query(F.data.regexp(r'^adm:greetswitch:(main|auto):[01]$'))
async def greeting_switch(call: CallbackQuery, db: Database):
    _, _, mode, value = call.data.split(':')
    key = 'greeting_enabled' if mode == 'main' else 'greeting_auto_new'
    await db.set_setting(key, value)
    await db.audit(call.from_user.id, 'greeting_setting', f'{key}={value}')
    await call.answer('Настройка сохранена.')
    await greetings_panel(call, db)


async def greeting_channel_card(target: Message | CallbackQuery, db: Database, channel: dict):
    chat_id = channel['chat_id']
    paid = await db.paid_channel(chat_id)
    available = channel['is_admin'] and channel['can_invite']
    global_enabled = (await db.setting('greeting_enabled'))['value'] == '1'
    enabled = channel['enabled']
    auto = channel['auto_approve']
    text = (f"<b>{esc(channel['title'] or 'Канал')}</b>\nID <code>{chat_id}</code>\n\n"
            f"Права · {'есть' if available else 'нужны права администратора на приглашения'}\n")
    if paid:
        text += (f"Приветствие для заявителей без доступа · {'включено' if global_enabled else 'выключено'}\n"
                 '🔒 Бот одобряет заявки пользователей с активным доступом.')
    else:
        text += (f"Приветствие в канале · {'включено' if enabled else 'выключено'}\n"
                 f"Автоодобрение заявок · {'включено' if auto else 'выключено'}")
        if not global_enabled:
            text += '\n\nОбщий режим выключен. Бот начнёт писать после его включения.'
    rows = []
    if not paid and (enabled or available):
        rows.append((cb('⏸ Выключить в канале' if enabled else '▶️ Включить в канале',
                        f'adm:greetset:{chat_id}:enabled:{int(not enabled)}',
                        style='danger' if enabled else 'success'),))
    if not paid and (auto or (enabled and available)):
        rows.append((cb('Выключить автоодобрение' if auto else 'Включить автоодобрение',
                        f'adm:greetset:{chat_id}:auto_approve:{int(not auto)}'),))
    rows.extend(((cb('📊 Воронка канала', f'adm:funnel:7:{chat_id}'),
                  cb('🔄 Проверить права', f'adm:greetcheck:{chat_id}')),
                 (cb('◀️ Каналы', 'adm:greetings'),)))
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:greet:-[0-9]{1,16}$'))
async def greeting_channel(call: CallbackQuery, db: Database):
    chat_id = int(call.data.split(':')[2])
    channel = await db.read('SELECT * FROM welcome_channels WHERE chat_id=?', (chat_id,))
    if not channel:
        return await call.answer('Канал не найден.', show_alert=True)
    await call.answer()
    await greeting_channel_card(call, db, channel)


@router.callback_query(F.data.regexp(r'^adm:greetset:-[0-9]{1,16}:(enabled|auto_approve):[01]$'))
async def set_greeting_channel(call: CallbackQuery, db: Database):
    _, _, value_id, field, value = call.data.split(':')
    chat_id = int(value_id)
    channel = await db.read('SELECT * FROM welcome_channels WHERE chat_id=?', (chat_id,))
    if not channel:
        return await call.answer('Канал не найден.', show_alert=True)
    if value == '1' and (not channel['can_invite'] or not channel['is_admin'] or await db.paid_channel(chat_id)):
        return await call.answer('Проверьте права бота и назначение канала.', show_alert=True)
    if value == '1' and field == 'auto_approve' and not channel['enabled']:
        return await call.answer('Сначала включите приветствие для канала.', show_alert=True)
    await db.execute(f'UPDATE welcome_channels SET {field}=? WHERE chat_id=?', (int(value), chat_id))
    await db.audit(call.from_user.id, 'greeting_channel', f'{chat_id}:{field}={value}')
    await call.answer('Настройка сохранена.')
    await greeting_channel_card(call, db, {**channel, field: int(value)})


@router.callback_query(F.data.regexp(r'^adm:greetcheck:-[0-9]{1,16}$'))
async def check_greeting_channel(call: CallbackQuery, bot: Bot, db: Database):
    await call.answer()
    chat_id = int(call.data.split(':')[2])
    try:
        channel = await inspect_channel(db, bot, chat_id)
    except TelegramAPIError:
        return await call.message.answer('Telegram не подтвердил доступ. Проверьте права бота в канале.')
    if channel:
        await greeting_channel_card(call, db, channel)
    else:
        await call.message.answer('Добавьте бота в администраторы канала и повторите проверку.')


@router.callback_query(F.data == 'adm:greetadd')
async def greeting_add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.greeting_channel)
    await call.answer()
    await call.message.answer('Перешлите любой пост из канала или пришлите его ID, например <code>-1001234567890</code>. '
                              'Боту нужны права администратора на приглашения. Для отмены используйте /cancel.')


@router.message(AdminState.greeting_channel, ~F.text.startswith('/'))
@router.message(AdminState.greeting_channel, F.forward_origin)
async def greeting_add_save(message: Message, state: FSMContext, bot: Bot, db: Database):
    origin = getattr(message, 'forward_origin', None)
    try:
        chat_id = origin.chat.id if isinstance(origin, MessageOriginChannel) else int(message.text or '')
        if not -(2 ** 52) < chat_id < 0:
            raise ValueError
    except ValueError:
        return await message.answer('Перешлите исходный пост из канала или укажите отрицательный числовой ID.')
    try:
        channel = await inspect_channel(db, bot, chat_id)
    except TelegramAPIError:
        return await message.answer('Канал недоступен. Проверьте ID и права бота.')
    if not channel or not channel['is_admin']:
        return await message.answer('Добавьте бота в администраторы канала и попробуйте ещё раз.')
    await db.audit(message.from_user.id, 'greeting_add_channel', str(chat_id))
    await state.clear()
    await greeting_channel_card(message, db, channel)


async def tariff_list(target: Message | CallbackQuery, db: Database, page: int = 0):
    items = await db.read('SELECT * FROM tariffs ORDER BY active DESC,id LIMIT ? OFFSET ?',
                          (PAGE_SIZE + 1, page * PAGE_SIZE), many=True)
    rows = [(cb(f"{'🟢' if t['active'] else '⚪'} #{t['id']} · {t['title'][:28]} · {fmt_rub(t['rub'])} ₽ / {fmt_rub(t['stars'])} ⭐",
                f"adm:t:{t['id']}"),) for t in items[:PAGE_SIZE]]
    rows.extend((pager('adm:tariffs', page, len(items) > PAGE_SIZE),
                 (cb('➕ Создать товар', 'adm:newt', style='success'), cb('🏠 Главное меню', 'adm:home'))))
    await screen(target, f"<b>📦 Товары</b>\nСтраница {page + 1}"
                 + ('' if items else '\n\nВ этом разделе пока пусто.'), reply_markup=kb(*rows))


@router.callback_query(F.data == 'adm:tariffs')
@router.callback_query(F.data.regexp(r'^adm:tariffs:[0-9]{1,6}$'))
async def tariffs(call: CallbackQuery, db: Database):
    await call.answer()
    page = int(call.data.split(':')[-1]) if call.data.count(':') == 2 else 0
    await tariff_list(call, db, page)


async def tariff_screen(target: Message | CallbackQuery, tariff: dict, *, notice: str = ''):
    tid = tariff['id']
    urls = json.loads(tariff['delivery_urls'])
    if urls:
        delivery = f'Готовые ссылки · {len(urls)}'
    elif tariff['channel_ids']:
        delivery = f"Доступ по заявке\n<code>{esc(tariff['channel_ids'])}</code>"
    else:
        delivery = 'Продажа без канала' if tariff.get('allow_no_channel') else 'Требует настройки'
    status = 'Опубликован' if tariff['active'] else 'Черновик'
    media_labels = {'photo': 'Фото загружено', 'video': 'Видео загружено', 'animation': 'GIF загружен'}
    cover = media_labels.get(tariff.get('media_type', '')) if has_product_photo(tariff) else 'Не загружено'
    details = 'Отдельное описание' if tariff['preview_description'] else 'Текст из карточки'
    prev_labels = {'photo': 'Фото ✅', 'video': 'Видео ✅', 'animation': 'GIF ✅'}
    prev_kind = prev_labels.get(tariff.get('preview_media_type', ''))
    prev_media = prev_kind if (tariff.get('preview_media_id') or '').strip() and prev_kind else 'Как в карточке'
    tier = tariff_tier_kind(tariff['title'], tariff.get('tier', ''))
    tier_label = {'ultra': 'ULTRA VIP (флагман)', 'diamond': 'DIAMOND CLUB (элит)'}.get(tier, 'Обычный')
    tier_short = {'ultra': 'ULTRA', 'diamond': 'DIAMOND'}.get(tier, 'Обычный')
    text = (f"<b>📦 Товар #{tid} · {status}</b>\n{esc(tariff['title'][:100])}\n\n"
            f"{esc(tariff['description'][:240]) or 'Описание пока пустое.'}\n\n"
            f"Цена <b>{fmt_rub(tariff['stars'])} ⭐</b> · <b>{fmt_rub(tariff['rub'])} ₽</b> (CryptoBot / Trade-In / скидки)\n"
            f"Срок <b>{duration(tariff['duration_days'])}</b> · класс <b>{tier_label}</b>\n"
            f"Выдача · {delivery}\n"
            f"Обложка карточки · {cover}\n"
            f"Что внутри? · {details} · медиа: {prev_media}")
    if urls:
        text += '\nГотовую ссылку получатель может переслать. Бот не управляет вступлением по ней.'
    if tariff['active'] and not urls and not tariff['channel_ids'] and not tariff.get('allow_no_channel'):
        text += '\n\n⚠️ <b>Товар опубликован без привязанного канала или ссылки — при покупке появится ошибка «Товар пока недоступен».</b>'
    rate_warn = _tariff_rate_warning(tariff)
    if rate_warn:
        text += '\n\n' + rate_warn
    caption_warn = _tariff_caption_warning(tariff)
    if caption_warn:
        text += '\n\n' + esc(caption_warn)
    if notice:
        text += '\n\n' + esc(notice)
    rows = [
        (cb('✏️ Название', f'adm:tf:{tid}:title'), cb('📝 Описание', f'adm:tf:{tid}:description')),
        (cb('⭐ Цена', f'adm:tf:{tid}:stars'), cb('₽ Цена (Crypto)', f'adm:tf:{tid}:rub')),
        (cb('🕓 Срок', f'adm:tf:{tid}:duration_days'),),
        (cb('🔗 Ссылка покупателю', f'adm:tf:{tid}:buyer_link'), cb('🔑 Привязка канала', f'adm:tf:{tid}:delivery')),
        (cb('🖼 Медиа карточки', f'adm:tm:{tid}'), cb('👀 Что внутри?', f'adm:tinside:{tid}')),
        (cb('🎬 Медиа «Что внутри?»', f'adm:tpm:{tid}'), cb('👁 Предпросмотр', f'adm:tpreview:{tid}')),
        (cb(f'👑 Класс: {tier_short}', f'adm:ttier:{tid}'),),
        (cb('Скрыть с витрины' if tariff['active'] else '🚀 Опубликовать',
            f"adm:tset:{tid}:{int(not tariff['active'])}",
            style='danger' if tariff['active'] else 'success'),),
        (cb('◀️ Товары', 'adm:tariffs'), cb('🏠 Главное меню', 'adm:home')),
    ]
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:t:[0-9]{1,16}$'))
async def tariff_details(call: CallbackQuery, shop: Shop):
    tariff = await shop.get_tariff(int(call.data.split(':')[2]))
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    await call.answer()
    await tariff_screen(call, tariff)


async def tariff_inside_screen(target: Message | CallbackQuery, tariff: dict, *, notice: str = ''):
    tid = tariff['id']
    media_type = tariff['preview_media_type'] if tariff['preview_media_id'].strip() else ''
    media_label = {'photo': 'Фото', 'video': 'Видео', 'animation': 'GIF'}.get(media_type)
    media_status = media_label or ('Обложка товара' if has_product_photo(tariff) else 'Не загружено')
    text = (f"<b>👀 Что внутри?</b>\n{esc(tariff['title'])}\n\n"
            f"Медиа · <b>{media_status}</b>\n\n"
            f"{esc(tariff['preview_description']) or 'Используется описание из карточки товара.'}")
    if notice:
        text += f'\n\n{esc(notice)}'
    await screen(target, text, reply_markup=kb(
        (cb('➕ Добавить медиа' if not media_label else '🎬 Заменить медиа', f'adm:tpm:{tid}', style='primary'),),
        (cb('✏️ Изменить описание', f'adm:tf:{tid}:preview_description'),
         cb('👁 Предпросмотр', f'adm:tpreview:{tid}:details')),
        (cb('◀️ К товару', f'adm:t:{tid}'),),
    ))


@router.callback_query(F.data.regexp(r'^adm:tinside:[0-9]{1,16}$'))
async def tariff_inside(call: CallbackQuery, shop: Shop):
    tariff = await shop.get_tariff(int(call.data.rsplit(':', 1)[1]))
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    await call.answer()
    await tariff_inside_screen(call, tariff)


@router.callback_query(F.data.regexp(r'^adm:ttier:[0-9]{1,16}$'))
async def tariff_cycle_tier(call: CallbackQuery, shop: Shop, db: Database):
    tid = int(call.data.split(':')[2])
    tariff = await shop.get_tariff(tid)
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    current = tariff_tier_kind(tariff['title'], tariff.get('tier', ''))
    cycle = {'': 'ultra', 'ultra': 'diamond', 'diamond': 'regular'}
    next_tier = cycle.get(current, 'regular')
    await db.execute('UPDATE tariffs SET tier=? WHERE id=?', (next_tier, tid))
    await db.audit(call.from_user.id, 'tariff_tier', f'{tid}:{next_tier}')
    await call.answer()
    await tariff_screen(call, await shop.get_tariff(tid), notice='Класс тарифа обновлён.')


@router.callback_query(F.data.regexp(r'^adm:tset:[0-9]{1,16}:[01]$'))
async def tariff_publish(call: CallbackQuery, shop: Shop, db: Database, bot: Bot):
    _, _, tid, active = call.data.split(':')
    tariff = await shop.get_tariff(int(tid))
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    await call.answer()
    if active == '1':
        if not tariff['active'] and not has_product_photo(tariff):
            return await tariff_screen(call, tariff, notice='Загрузите фото или видео товара перед публикацией.')
        try:
            channels, _ = tariff_delivery(tariff)
            if channels:
                await validate_sale_channels(bot, channels)
        except ValueError as exc:
            return await tariff_screen(call, tariff, notice=str(exc))
    def publish(con):
        changed = con.execute("""UPDATE tariffs SET active=? WHERE id=? AND active!=?
            AND channel_ids=? AND delivery_urls=? AND duration_days=? AND allow_no_channel=?
            AND (?=0 OR (media_type IN ('photo','video','animation') AND trim(media_id)!=''))""",
            (int(active), int(tid), int(active), tariff['channel_ids'], tariff['delivery_urls'],
             tariff['duration_days'], tariff['allow_no_channel'], int(active))).rowcount
        if changed and active == '0':
            con.execute("""UPDATE orders SET status='expired' WHERE tariff_id=? AND provider='stars'
                AND status='pending' AND checkout_id=''""", (int(tid),))
        return changed
    changed = await db.atomic(publish)
    if changed:
        await db.audit(call.from_user.id, 'tariff_edit', f'{tid}:active={active}')
    current = await shop.get_tariff(int(tid))
    notice = 'Параметры товара изменились. Проверьте их и повторите действие.' if current['active'] != int(active) else ''
    await tariff_screen(call, current, notice=notice)


@router.callback_query(F.data.regexp(r'^adm:tpreview:[0-9]{1,16}(:details)?$'))
async def tariff_preview_admin(call: CallbackQuery, shop: Shop):
    parts = call.data.split(':')
    tid = int(parts[2])
    tariff = await shop.get_tariff(tid)
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    await call.answer()
    details = len(parts) == 4
    buyers = None if details else await get_live_tariff_buyers(shop, shop.db, tariff)
    text = product_text(tariff, tariff['stars'], buyers=buyers, project_name=await shop.db.project_name())
    toggle = (cb('📦 Карточка', f'adm:tpreview:{tid}') if details
              else cb('👀 Что внутри?', f'adm:tpreview:{tid}:details'))
    media_id, media_type = tariff_view_media(tariff, details=details)
    back = cb('◀️ К настройкам', f'adm:tinside:{tid}') if details else cb('◀️ К товару', f'adm:t:{tid}')
    await send_section(call.message, {'value': text, 'media_id': media_id, 'media_type': media_type},
                       markup=kb((toggle,), (back,)))


def tariff_value(field: str, text: str):
    if field in ('rub', 'stars', 'duration_days'):
        try:
            value = int(text)
        except ValueError:
            raise ShopError('Введите целое число.') from None
        if field == 'duration_days':
            if not 0 <= value <= MAX_DURATION_DAYS:
                raise ShopError('Срок должен быть от 0 до 3650 дней.')
        elif not 1 <= value <= 1_000_000:
            raise ShopError('Цена должна быть от 1 до 1 000 000.')
        return value
    if field in ('description', 'preview_description'):
        if len(text) > 650:
            raise ShopError('Описание должно быть не длиннее 650 символов.')
        return '' if text == '-' else text
    if not 1 <= len(text) <= 80:
        raise ShopError('Название должно содержать от 1 до 80 символов.')
    return text


DELIVERY_HINT = (
    'Перешлите любой пост из закрытого канала. Боту нужны права на приглашения и исключение участников. '
    'Он сам создаст ссылку с заявкой на вступление.\n'
    'Чтобы использовать свою ссылку с заявкой, пришлите ID канала, пробел и ссылку. '
    'Например <code>-1001234567890 https://t.me/+abc123</code>. Для нескольких каналов используйте отдельные строки.\n'
    'Бот использует эту ссылку во всех товарах с таким ID. Чтобы убрать её, пришлите ID и «-» через пробел.\n'
    'Можно указать ID каналов через запятую или готовые ссылки для бессрочной выдачи. '
    'Отправьте «-», чтобы продавать товар без привязки к каналу.'
)


def private_join_link(text: str) -> str:
    urls = telegram_links(text)
    if len(urls) != 1 or not urls[0].startswith(('https://t.me/+', 'https://t.me/joinchat/')):
        raise ShopError('Пришлите одну закрытую ссылку Telegram с заявкой на вступление.')
    return urls[0]


async def delivery_values(bot: Bot, text: str, days: int, *, origin=None, current_ids: str = ''):
    if origin:
        if not isinstance(origin, MessageOriginChannel):
            raise ShopError('Перешлите исходный пост именно из канала.')
        ids = list(dict.fromkeys([*channels_of(current_ids), origin.chat.id]))
        text = ','.join(map(str, ids))
    if text.lower().startswith('ссылка '):
        current = channels_of(current_ids)
        if len(current) != 1:
            raise ShopError('Привяжите один канал пересылкой или укажите ID рядом со ссылкой.')
        text = f'{current[0]} {text.split(maxsplit=1)[1]}'
    if text.lower().startswith('https://'):
        if days:
            raise ShopError('Для готовой ссылки установите срок 0 или привяжите канал.')
        return '', json.dumps(telegram_links(text)), {}
    if not text:
        raise ShopError('Пришлите ссылки, ID или «-», чтобы пропустить.')
    if text == '-':
        return '', '[]', {}
    parts = [line.split() for line in text.splitlines() if line.strip()]
    shared = {}
    if any(len(row) == 2 and (row[1] == '-' or row[1].startswith('https://')) for row in parts):
        if any(len(row) not in (1, 2) or not re.fullmatch(r'-[0-9]+', row[0]) for row in parts):
            raise ShopError('Пришлите по одному ID и ссылке на строку.')
        ids = channels_of(','.join(row[0] for row in parts))
        for chat_id, (_, *url_parts) in zip(ids, parts):
            if not url_parts:
                continue
            raw = url_parts[0]
            shared[chat_id] = '' if raw == '-' else private_join_link(raw)
    else:
        try:
            ids = channels_of(text)
        except ValueError:
            raise ShopError('Пришлите HTTPS-ссылки Telegram или числовые ID через запятую.') from None
    await validate_sale_channels(bot, ids)
    return ','.join(map(str, ids)), '[]', shared


def save_product_channel_links(con, links: dict[int, str]):
    for chat_id, url in links.items():
        if not url:
            con.execute('DELETE FROM product_channel_links WHERE chat_id=?', (chat_id,))
            continue
        if con.execute('SELECT 1 FROM product_channel_links WHERE url=? AND chat_id!=?', (url, chat_id)).fetchone():
            raise ShopError('Эта ссылка уже привязана к другому каналу.')
        con.execute("""INSERT INTO product_channel_links(chat_id,url) VALUES(?,?)
            ON CONFLICT(chat_id) DO UPDATE SET url=excluded.url""", (chat_id, url))
        # Existing purchases gain channel access as soon as their link is bound.
        for order in con.execute("""SELECT o.id,o.channels,o.delivery_urls,p.id AS purchase_id,p.user_id
                FROM orders o JOIN purchases p ON p.order_id=o.id
                WHERE o.status='paid'
                  AND EXISTS (SELECT 1 FROM json_each(o.delivery_urls) WHERE value=?)""", (url,)).fetchall():
            for channel in bind_order_channels(con, order):
                con.execute('INSERT OR IGNORE INTO grants(purchase_id,user_id,chat_id) VALUES(?,?,?)',
                            (order['purchase_id'], order['user_id'], channel))


@router.callback_query(F.data.regexp(r'^adm:tf:[0-9]{1,16}:[a-z_]+$'))
async def edit_field_start(call: CallbackQuery, state: FSMContext, shop: Shop):
    _, _, tid, field = call.data.split(':', 3)
    if field not in FIELD_NAMES and field not in ('delivery', 'buyer_link'):
        return await call.answer('Неизвестное поле.', show_alert=True)
    tariff = await shop.get_tariff(int(tid))
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    await state.set_state(AdminState.tariff_field)
    await state.set_data({'tid': int(tid), 'field': field})
    if field in ('delivery', 'channel_ids', 'buyer_link'):
        ids = channels_of(tariff['channel_ids'])
        if field == 'buyer_link':
            prompt = (
                'Пришлите закрытую ссылку, которую получит покупатель после оплаты. '
                'В Telegram при создании ссылки включите «Заявки на вступление». '
                'Бот должен быть администратором канала с правами приглашать и исключать участников.'
            )
            if not ids:
                prompt += '\n\nПосле ссылки перешлите пост из канала для привязки.'
            elif len(ids) > 1:
                prompt += '\n\nПосле ссылки перешлите пост из нужного канала товара.'
            if ids:
                prompt += '\n\nОтправьте «-», чтобы бот сам создавал приглашения для канала.'
            prompt += '\n\nСсылка действует для всех товаров с этим каналом.'
        else:
            prompt = DELIVERY_HINT + '\n\nПересланный пост добавит канал к уже привязанным.'
            prompt += ' Свою ссылку можно задать через кнопку «Ссылка покупателю» в карточке товара.'
        if ids:
            current = await shop.db.read(
                'SELECT chat_id,url FROM product_channel_links WHERE chat_id IN (SELECT value FROM json_each(?))',
                (json.dumps(ids),), many=True,
            )
            if current:
                prompt += '\n\n<b>Закрытые ссылки</b>\n' + '\n'.join(
                    f"<code>{row['chat_id']} {esc(row['url'])}</code>" for row in current
                )
        if tariff['delivery_urls'] != '[]':
            prompt += '\n\n<b>Текущие ссылки</b>\n' + '\n'.join(
                f'<code>{esc(url)}</code>' for url in json.loads(tariff['delivery_urls'])[:8])
    else:
        current = esc(str(tariff[field])[:260]) or '—'
        prompt = f"Текущее значение <code>{current}</code>\n\nПришлите новое значение."
        if field == 'preview_description':
            prompt += '\nДо 650 символов. Отправьте «-», чтобы использовать описание из карточки.'
        elif field == 'stars' and int(tariff.get('rub') or 0) >= 30:
            rec_stars = max(1, round(int(tariff['rub']) / 1.92 / 5) * 5)
            prompt += f"\nОриентир по курсу проекта (~1.92 ₽/⭐) для {fmt_rub(tariff['rub'])} ₽: ~<b>{fmt_rub(rec_stars)} ⭐</b>."
        elif field == 'rub':
            prompt += '\nЭта сумма используется для оплаты в @CryptoBot, зачёта Trade-In и расчёта скидок.'
    await call.answer()
    label = 'Ссылка покупателю' if field == 'buyer_link' else FIELD_NAMES.get(field, 'Выдача доступа')
    back = f'adm:tinside:{tid}' if field == 'preview_description' else f'adm:t:{tid}'
    await screen(call, f"<b>✏️ {label}</b>\n\n{prompt}", reply_markup=form_nav('◀️ Отменить', back))


@router.message(AdminState.tariff_field, ~F.text.startswith('/'))
@router.message(AdminState.tariff_field, F.forward_origin)
async def edit_field_save(message: Message, state: FSMContext, db: Database, bot: Bot, shop: Shop):
    data = await state.get_data()
    tid, field = data['tid'], data['field']
    text = (message.text or '').strip()
    try:
        if field not in FIELD_NAMES and field not in ('delivery', 'buyer_link'):
            raise ShopError('Откройте нужное поле в карточке товара.')
        tariff = await shop.get_tariff(tid)
        if not tariff:
            raise ShopError('Товар не найден.')
        if field in ('delivery', 'channel_ids', 'buyer_link'):
            origin = getattr(message, 'forward_origin', None)
            if field == 'buyer_link':
                channels = channels_of(tariff['channel_ids'])
                if len(json.loads(tariff['delivery_urls'])) > 1:
                    raise ShopError('У товара несколько готовых ссылок. Настройте их в разделе «Привязка канала».')
                pending_link = data.get('pending_buyer_link')
                if pending_link:
                    if not isinstance(origin, MessageOriginChannel):
                        raise ShopError('Перешлите пост из канала, к которому относится ссылка.')
                    chat_id = origin.chat.id
                    if channels and chat_id not in channels:
                        raise ShopError('Перешлите пост из одного из привязанных каналов этого товара.')
                    buyer_link = pending_link
                else:
                    buyer_link = '-' if text == '-' and channels else private_join_link(text)
                    if len(channels) != 1:
                        await state.update_data(pending_buyer_link=buyer_link)
                        return await message.answer('Теперь перешлите пост из канала, к которому относится ссылка.')
                    chat_id = channels[0]
                await validate_sale_channels(bot, [chat_id])
                ids = ','.join(map(str, channels or [chat_id]))
                urls, shared = '[]', {chat_id: '' if buyer_link == '-' else buyer_link}
                allow_no_channel = 0
            else:
                ids, urls, shared = await delivery_values(
                    bot, text, tariff['duration_days'], origin=origin, current_ids=tariff['channel_ids'],
                )
                allow_no_channel = int(text == '-' and not origin)
            def save(con):
                updated = con.execute("""UPDATE tariffs SET channel_ids=?,delivery_urls=?,allow_no_channel=?
                    WHERE id=? AND active=? AND duration_days=? AND channel_ids=? AND delivery_urls=?""",
                    (ids, urls, allow_no_channel, tid, tariff['active'], tariff['duration_days'],
                     tariff['channel_ids'], tariff['delivery_urls'])).rowcount
                if updated:
                    save_product_channel_links(con, shared)
                return updated
            async with shop.delivery_lock, shop.entitlement_lock:
                changed = await db.atomic(save)
        else:
            val = tariff_value(field, text)
            if field == 'duration_days' and val and tariff['delivery_urls'] != '[]':
                raise ShopError('Срок можно задать для выдачи через ID канала.')
            if field == 'duration_days':
                changed = await db.execute('UPDATE tariffs SET duration_days=? WHERE id=? AND delivery_urls=?',
                                           (val, tid, tariff['delivery_urls']))
            else:
                changed = await db.execute(f'UPDATE tariffs SET {field}=? WHERE id=?', (val, tid))
        if not changed:
            raise ShopError('Товар изменился в другой сессии. Проверьте параметры и повторите ввод.')
    except ValueError as exc:
        return await message.answer(esc(exc))
    await db.audit(message.from_user.id, 'tariff_edit', f'{tid}:{field}')
    await state.clear()
    render = tariff_inside_screen if field == 'preview_description' else tariff_screen
    await render(message, await shop.get_tariff(tid), notice='Изменения сохранены.')


PRODUCT_STEPS = (
    ('title', 'Название', 'Например «VIP-канал» или «Все каналы [ТОП]».'),
    ('description', 'Описание', 'Напишите, что получает покупатель. «-» оставит описание пустым.'),
    ('stars', 'Цена в Stars', 'Введите целое число, например 250.'),
    ('rub', 'База для расчёта промокодов', 'Укажите сумму в рублях. Она используется только для расчёта скидок.'),
    ('duration_days', 'Срок в днях', 'Для бессрочного доступа и готовых ссылок укажите 0.'),
    ('delivery', 'Выдача доступа', DELIVERY_HINT),
)


async def product_step(target: Message | CallbackQuery, position: int):
    _, label, hint = PRODUCT_STEPS[position]
    back = (cb('◀️ Предыдущий шаг', f'adm:newt:back:{position}'),) if position else ()
    await screen(target, f'<b>➕ Новый товар · {position + 1}/{len(PRODUCT_STEPS)}</b>\n\n<b>{label}</b>\n{hint}',
                 reply_markup=kb(back, (cb('Отменить создание', 'adm:tariffs'),)))


@router.callback_query(F.data == 'adm:newt')
async def new_tariff_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.tariff_new)
    await state.update_data(values={})
    await call.answer()
    await product_step(call, 0)


@router.callback_query(F.data.regexp(r'^adm:newt:back:[1-5]$'), AdminState.tariff_new)
async def new_tariff_back(call: CallbackQuery, state: FSMContext):
    position = int(call.data.split(':')[-1])
    values = (await state.get_data())['values']
    if len(values) != position:
        return await call.answer('Используйте кнопки текущего шага.', show_alert=True)
    values.pop(PRODUCT_STEPS[position - 1][0])
    await state.update_data(values=values)
    await call.answer()
    await product_step(call, position - 1)


@router.message(AdminState.tariff_new, ~F.text.startswith('/'))
@router.message(AdminState.tariff_new, F.forward_origin)
async def new_tariff_save(message: Message, state: FSMContext, db: Database, bot: Bot, shop: Shop):
    values = dict((await state.get_data())['values'])
    text = (message.text or '').strip()
    position = len(values)
    origin = getattr(message, 'forward_origin', None)
    if not text and not (origin and PRODUCT_STEPS[position][0] == 'delivery'):
        return await message.answer('Пришлите значение текстом.')
    try:
        if position == 0 and text.count('|') == 5:
            title, description, rub, stars, days, text = (part.strip() for part in text.split('|'))
            raw = (title, description, stars, rub, days)
            values = {step[0]: tariff_value(step[0], value) for step, value in zip(PRODUCT_STEPS, raw)}
            position = len(values)
            await state.update_data(values=values)
        field = PRODUCT_STEPS[position][0]
        if field != 'delivery':
            values[field] = tariff_value(field, text)
            await state.update_data(values=values)
            return await product_step(message, position + 1)
        ids, urls, shared = await delivery_values(bot, text, values['duration_days'], origin=origin)
        def create(con):
            row = con.execute("""INSERT INTO tariffs(title,description,rub,stars,duration_days,channel_ids,delivery_urls,
                                                  allow_no_channel,active)
                VALUES(?,?,?,?,?,?,?,?,0)""", (values['title'], values['description'], values['rub'], values['stars'],
                                             values['duration_days'], ids, urls, int(text == '-' and not origin)))
            save_product_channel_links(con, shared)
            return row.lastrowid
        async with shop.delivery_lock, shop.entitlement_lock:
            tid = await db.atomic(create)
    except ValueError as exc:
        return await message.answer(esc(exc))
    await db.audit(message.from_user.id, 'tariff_new', str(tid))
    await state.clear()
    await tariff_screen(message, await shop.get_tariff(tid),
                        notice='Черновик сохранён. Загрузите фото или видео, проверьте карточку и опубликуйте товар.')


@router.callback_query(F.data.regexp(r'^adm:tp?m:[0-9]{1,16}$'))
async def tariff_media_start(call: CallbackQuery, state: FSMContext, shop: Shop):
    _, route, tid_raw = call.data.split(':')
    tid = int(tid_raw)
    tariff = await shop.get_tariff(tid)
    if not tariff:
        return await call.answer('Товар не найден.', show_alert=True)
    target = 'preview' if route == 'tpm' else 'cover'
    await state.set_state(AdminState.tariff_media)
    await state.update_data(tid=tid, media_target=target)
    await call.answer()
    if target == 'preview':
        prompt = (
            '<b>🎬 Медиа «Что внутри?» (превью)</b>\n\n'
            'Пришлите фото, видео или GIF в Telegram для экрана «👀 Что внутри?».\n'
            'Если отдельное медиа не задано, используется обложка из карточки товара.\n'
            'Для удаления отдельного медиа превью отправьте «удалить».'
        )
    else:
        prompt = '<b>🖼 Медиа карточки товара</b>\n\nПришлите фото, видео или GIF в Telegram.'
        if not tariff['active']:
            prompt += '\nДля удаления обложки отправьте «удалить».'
    back = f'adm:tinside:{tid}' if target == 'preview' else f'adm:t:{tid}'
    await screen(call, prompt, reply_markup=form_nav('◀️ Отменить', back))


@router.message(AdminState.tariff_media, ~F.text.startswith('/'))
async def tariff_media_save(message: Message, state: FSMContext, db: Database, shop: Shop):
    data = await state.get_data()
    tid = data['tid']
    target = data.get('media_target', 'cover')
    tariff = await shop.get_tariff(tid)
    if not tariff:
        await state.clear()
        return await message.answer('Товар не найден.', reply_markup=form_nav('◀️ К товарам', 'adm:tariffs'))

    extracted = _extract_media_upload(message)
    if extracted is None:
        allow_clear = target == 'preview' or not tariff['active']
        hint = ('Пришлите фото, видео или GIF в Telegram (или слово «удалить»).'
                if allow_clear else 'Пришлите фото, видео или GIF в Telegram.')
        return await message.answer(hint)
    mid, kind = extracted
    remove = not mid
    saved_label = {'photo': 'Фото', 'video': 'Видео', 'animation': 'GIF'}.get(kind, 'Медиа')

    if target == 'preview':
        changed = await db.execute(
            'UPDATE tariffs SET preview_media_id=?,preview_media_type=? WHERE id=?',
            (mid, kind, tid),
        )
        audit_action = 'tariff_preview_media'
        notice = 'Отдельное медиа превью удалено.' if remove else f'{saved_label} превью сохранено.'
    else:
        if remove:
            changed = await db.execute(
                "UPDATE tariffs SET media_id='',media_type='' WHERE id=? AND active=0",
                (tid,),
            )
            notice = 'Обложка удалена.'
        else:
            changed = await db.execute(
                'UPDATE tariffs SET media_id=?,media_type=? WHERE id=?',
                (mid, kind, tid),
            )
            notice = f'{saved_label} карточки сохранено.'
        audit_action = 'tariff_media'

    tariff = await shop.get_tariff(tid)
    if not tariff:
        await state.clear()
        return await message.answer('Товар не найден.', reply_markup=form_nav('◀️ К товарам', 'adm:tariffs'))
    if not changed:
        return await message.answer('Чтобы удалить обложку, сначала скройте товар с витрины.',
                                    reply_markup=form_nav('◀️ К товару', f'adm:t:{tid}'))
    await db.audit(message.from_user.id, audit_action, f"{tid}:{kind or 'clear'}")
    await state.clear()
    render = tariff_inside_screen if target == 'preview' else tariff_screen
    await render(message, tariff, notice=notice)


@router.callback_query(F.data == 'adm:sections')
async def sections(call: CallbackQuery, db: Database):
    placeholders = ','.join('?' for _ in SECTION_NAMES)
    stored = {
        row['key']: row
        for row in await db.read(
            f'SELECT key, value, media_id, media_type FROM settings WHERE key IN ({placeholders})',
            tuple(SECTION_NAMES),
            many=True,
        )
    }
    show_help = await help_visible(db)
    rows = []
    for key in SECTION_NAMES:
        row = stored.get(key) or {}
        has_media = _section_has_media(row)
        val = row.get('value') or ''
        badge = ' · 🖼' if has_media else ''
        if key not in MEDIA_ONLY and has_media and _utf16_len(val) > 950:
            badge += ' ⚠️'
        if key == 'help' and not show_help:
            badge += ' · 🙈 скрыт'
        rows.append([cb(f"{SECTION_TITLES.get(key, f'🖼 {key}')}{badge}", f'adm:section:{key}')])
    rows.append([cb('◀️ Меню', 'adm:home')])
    await call.answer()
    await screen(
        call,
        '<b>🖼 Тексты и баннеры</b>\n\n'
        'Выберите экран, чтобы посмотреть, скопировать или изменить его текст:\n'
        '• <b>Текстовые экраны</b> — можно писать с обычным форматированием Telegram или HTML-тегами (до 950 симв. для единого поста с баннером).\n'
        '• <b>Экраны (баннер)</b> — Профиль, Оплата и Статистика: здесь задаётся только медиа-баннер.\n'
        '• <b>🖼</b> — баннер уже загружен.',
        reply_markup=kb(*rows),
    )


def _bounded_section_copy_block(val: str, used_chars: int, used_utf16: int) -> str:
    """Build a `<pre>` preview block that keeps the full card safely under Telegram's 4096-char limit."""
    note = '\n<i>…показана часть шаблона (полный текст откроется при нажатии «✏️ Изменить текст»).</i>'
    prefix = '📋 <b>Текущий текст (нажми, чтобы скопировать):</b>\n<pre>'
    max_total = 3950
    limit = min(len(val), 2600)
    while limit > 0:
        copy_body = esc(val[:limit])
        suffix = note if limit < len(val) else ''
        block = f'{prefix}{copy_body}</pre>{suffix}'
        if used_chars + len(block) <= max_total and used_utf16 + _utf16_len(block) <= max_total:
            return block
        if limit == 1:
            break
        limit = max(1, (limit * 3) // 4) if limit > 16 else limit - 1
    return '📋 <b>Текущий текст:</b>\n<i>(Скрыт в превью из-за большого объёма спецсимволов — нажми «✏️ Изменить текст».)</i>'


async def _render_section_card(target: CallbackQuery | Message, db: Database, section: str, *, notice: str = ''):
    row = await db.setting(section)
    val = (row or {}).get('value', '')
    has_media = _section_has_media(row)
    title = SECTION_TITLES.get(section, section)
    hint = SECTION_HINTS.get(section, '')
    media = {'photo': 'фото ✅', 'video': 'видео ✅', 'animation': 'GIF ✅'}.get((row or {}).get('media_type'), 'нет')

    rows = [
        (cb('🖼 Изменить баннер', f'adm:sf:{section}:media'),
         cb('👁 Как видит юзер', f'adm:spreview:{section}')),
    ]
    if section not in MEDIA_ONLY:
        rows.insert(0, (cb('✏️ Изменить текст', f'adm:sf:{section}:text', style='primary'),))
        default_val = DEFAULT_SETTINGS.get(section)
        if default_val is not None and val != default_val:
            rows.append((cb('↩️ Сбросить к стандарту', f'adm:sreset:{section}'),))
    show_help = await help_visible(db) if section == 'help' else None
    if show_help is not None:
        rows.append((cb(
            '🙈 Скрыть раздел «Помощь»' if show_help else '👁 Открыть раздел «Помощь»',
            f'adm:section_help:{int(not show_help)}',
        ),))
    rows.append((cb('◀️ Все разделы', 'adm:sections'), cb('🏠 Главное меню', 'adm:home')))

    parts = [f"<b>{esc(title)}</b>"]
    if notice:
        parts.append(notice)
    if hint:
        parts.append(f"<i>{esc(hint)}</i>")
    if show_help is not None:
        parts.append(f"👁 Раздел в меню: <b>{'открыт' if show_help else 'скрыт'}</b>")
    parts.append(f"🖼 Баннер: <b>{media}</b>")
    if section in MEDIA_ONLY:
        parts.append('Для этого экрана настраивается только фото или видео-баннер.')
    elif val:
        parts.append(_section_length_line(val, has_media))
        joined = '\n\n'.join(parts)
        parts.append(_bounded_section_copy_block(val, len(joined) + 2, _utf16_len(joined) + 2))
    else:
        parts.append('<i>Текст раздела пока не задан.</i>')
    await screen(target, '\n\n'.join(parts), reply_markup=kb(*rows))


@router.callback_query(F.data.startswith('adm:section:'))
async def section_card(call: CallbackQuery, db: Database):
    section = call.data.split(':')[2]
    if section not in SECTION_NAMES:
        return await call.answer('Неверный раздел')
    await call.answer()
    await _render_section_card(call, db, section)


@router.callback_query(F.data.regexp(r'^adm:section_help:[01]$'))
async def section_help_toggle(call: CallbackQuery, db: Database):
    value = call.data.rsplit(':', 1)[1]
    await _set_visibility_setting(db, call.from_user.id, 'help_visible', value)
    notice = ('✅ <b>Раздел «Помощь» открыт в меню бота.</b>'
              if value == '1' else '🙈 <b>Раздел «Помощь» скрыт из меню бота.</b>')
    await call.answer('Раздел открыт' if value == '1' else 'Раздел скрыт')
    await _render_section_card(call, db, 'help', notice=notice)


@router.callback_query(F.data.startswith('adm:sreset:'))
async def section_reset(call: CallbackQuery, db: Database):
    parts = call.data.split(':')
    section = parts[2] if len(parts) >= 3 else ''
    confirmed = len(parts) == 4 and parts[3] == 'yes'
    if section not in SECTION_NAMES or section in MEDIA_ONLY or section not in DEFAULT_SETTINGS:
        return await call.answer('Для этого раздела нет стандартного текста.', show_alert=True)
    if not confirmed:
        await call.answer()
        title = SECTION_TITLES.get(section, section)
        return await screen(
            call,
            f"<b>↩️ Сбросить текст раздела «{esc(title)}»?</b>\n\n"
            "Текущий текст заменится на стандартный шаблон. Загруженный баннер останется на месте.",
            reply_markup=kb(
                (cb('✅ Да, сбросить текст', f'adm:sreset:{section}:yes', style='danger'),),
                (cb('◀️ Отменить', f'adm:section:{section}'),),
            ),
        )
    await db.set_setting(section, DEFAULT_SETTINGS[section])
    await db.audit(call.from_user.id, 'section_edit', f'{section}:reset')
    await call.answer('Стандартный текст восстановлен')
    await _render_section_card(call, db, section, notice='✅ <b>Восстановлен стандартный текст раздела.</b>')


@router.callback_query(F.data.startswith('adm:spreview:'))
async def section_preview(call: CallbackQuery, db: Database, bot: Bot):
    section = call.data.split(':')[2]
    if section not in SECTION_NAMES:
        return await call.answer('Раздел не найден.', show_alert=True)
    await call.answer()
    row = await rendered_section(db, section, name=call.from_user.full_name, channel='Тестовый канал',
                                 bot_username=(await bot.me()).username or '')
    text = row['value'] or SECTION_TITLES[section]
    await send_section(call.message, {**row, 'value': text},
                       markup=form_nav('◀️ К разделу', f'adm:section:{section}'))


@router.callback_query(F.data.startswith('adm:sf:'))
async def section_edit_start(call: CallbackQuery, state: FSMContext, db: Database):
    parts = call.data.split(':')
    if len(parts) != 4:
        return await call.answer('Неверное поле')
    _, _, section, field = parts
    if section not in SECTION_NAMES or field not in ('text', 'media') or (
        field == 'text' and section in MEDIA_ONLY
    ):
        return await call.answer('Неверное поле')
    await state.set_state(AdminState.section_field)
    await state.update_data(section=section, field=field)
    title = SECTION_TITLES.get(section, section)
    if field == 'text':
        row = await db.setting(section) or {}
        current = row.get('value') or ''
        has_media = _section_has_media(row)
        copy_block = (f"📋 <b>Текущий текст (тапните, чтобы скопировать и отредактировать):</b>\n"
                      f"<pre>{esc(current[:2600])}</pre>\n\n"
                      if current else '')
        prompt = (
            f"<b>✏️ {esc(title)}</b>\n"
            f"<i>{esc(SECTION_HINTS.get(section, ''))}</i>\n\n"
            f"{copy_block}"
            "<b>Отправьте новый текст одним сообщением</b> (до 4000 символов).\n"
            "Можно оформлять прямо в Telegram (жирный, курсив, ссылки, цитата) <b>или</b> писать HTML-тегами.\n\n"
            f"<b>Авто-подстановки:</b>\n{_section_variables_hint(section)}\n\n"
            f"{_section_length_line(current, has_media) if current else '📏 Для единого поста с баннером укладывайтесь в 950 символов.'}\n"
            "Отмена: кнопка ниже или /admin."
        )
        await send_text(call.message.answer, prompt, markup=form_nav('◀️ Отменить', f'adm:section:{section}'))
    else:
        await call.message.answer(
            f"<b>🖼 Баннер · {esc(title)}</b>\n\n"
            "Отправьте новое фото или короткое видео (до 5–8 сек) для баннера раздела.\n"
            "Чтобы убрать текущий баннер, отправьте слово <code>удалить</code>.\n"
            "Отмена: кнопка ниже или /admin.",
            reply_markup=form_nav('◀️ Отменить', f'adm:section:{section}'),
        )
    await call.answer()


@router.message(AdminState.section_field, ~F.text.startswith('/'))
async def section_edit_save(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    section = data['section']
    title = SECTION_TITLES.get(section, section)
    if data['field'] == 'text':
        raw_html = _extract_section_html(message)
        if not raw_html or len(raw_html) > 4000:
            return await message.answer(
                '⚠️ Требуется текст до 4000 символов.',
                reply_markup=form_nav('◀️ Отменить', f'adm:section:{section}'),
            )
        try:
            validate_section_html(raw_html)
        except ValueError as exc:
            return await message.answer(
                f'⚠️ <b>Текст не сохранён:</b> {esc(exc)}\n\n'
                'Исправьте сообщение и отправьте заново, или нажмите «Отменить».',
                reply_markup=form_nav('◀️ Отменить', f'adm:section:{section}'),
            )
        await db.set_setting(section, raw_html)
        row = await db.setting(section) or {}
        extra_info = _section_length_line(raw_html, _section_has_media(row))
    else:
        extracted = _extract_media_upload(message)
        if extracted is None:
            return await message.answer(
                '⚠️ Отправьте фото, видео, GIF или слово <code>удалить</code>.',
                reply_markup=form_nav('◀️ Отменить', f'adm:section:{section}'),
            )
        mid, kind = extracted
        await db.set_setting(section, media_id=mid, media_type=kind)
        row = await db.setting(section) or {}
        if mid and section not in MEDIA_ONLY:
            extra_info = _section_length_line(row.get('value') or '', True)
        else:
            extra_info = 'Баннер удалён.' if not mid else 'Баннер обновлён.'
    await db.audit(message.from_user.id, 'section_edit', f"{section}:{data['field']}")
    await state.clear()
    await message.answer(
        f'✅ <b>Раздел «{esc(title)}» сохранён.</b>\n{extra_info}',
        reply_markup=kb(
            (cb('👁 Посмотреть как у юзера', f'adm:spreview:{section}', style='primary'),),
            (cb('✏️ Изменить ещё раз', f'adm:sf:{section}:text'), cb('◀️ К разделу', f'adm:section:{section}'))
            if section not in MEDIA_ONLY else (cb('◀️ К разделу', f'adm:section:{section}'),),
            (cb('🏠 Главное меню', 'adm:home'),),
        ),
    )


def _parse_promo_fields(raw_text: str, existing_code: str | None) -> tuple[str, int, int, int]:
    """Parse 'CODE 200 100' or 'CODE 20% 100' into (code, discount_rub, discount_percent, max_uses)."""
    tokens: list[str] = []
    for tok in (raw_text or '').upper().split():
        if tok == '%' and tokens:
            tokens[-1] = tokens[-1] + '%'
        else:
            tokens.append(tok)
    if existing_code:
        tokens.insert(0, existing_code)
    if len(tokens) != 3:
        raise ValueError('Укажите параметры в формате: КОД СКИДКА ЛИМИТ')
    code, raw_disc, raw_uses = tokens
    if not re.fullmatch(r'[A-Z0-9_]{1,28}', code):
        raise ValueError('Код должен содержать 1-28 латинских букв, цифр или _.')
    max_uses = int(raw_uses)
    if not 1 <= max_uses <= 1_000_000:
        raise ValueError('Лимит использований должен быть от 1 до 1000000.')
    if raw_disc.endswith('%'):
        pct = int(raw_disc[:-1])
        if not 1 <= pct <= 99:
            raise ValueError('Процент скидки должен быть от 1% до 99%.')
        return code, 0, pct, max_uses
    discount_rub = int(raw_disc)
    if not 1 <= discount_rub <= 1_000_000:
        raise ValueError('Скидка в ₽ должна быть от 1 до 1000000.')
    return code, discount_rub, 0, max_uses


@router.callback_query(F.data == 'adm:promos')
@router.callback_query(F.data.regexp(r'^adm:promos:[0-9]{1,6}$'))
async def promos(call: CallbackQuery, db: Database):
    await call.answer()
    page = int(call.data.split(':')[-1]) if call.data.count(':') == 2 else 0
    items = await db.read('SELECT * FROM promos WHERE is_personal=0 ORDER BY enabled DESC,code LIMIT ? OFFSET ?',
                          (PAGE_SIZE + 1, page * PAGE_SIZE), many=True)
    rows = [(cb(f"{'🟢' if p['enabled'] else '⚪'} {p['code']} · -{promo_discount_label(p)} · {p['used']}/{p['max_uses']}",
                f"adm:promo:card:{p['code']}"),) for p in items[:PAGE_SIZE]]
    rows.extend((pager('adm:promos', page, len(items) > PAGE_SIZE),
                 (cb('➕ Создать / изменить', 'adm:promo:new', style='success'), cb('🎁 Авто-пуш', 'adm:ppush')),
                 (cb('🏠 Главное меню', 'adm:home'),)))
    await screen(call, f"<b>🎫 Промокоды</b>\nСтраница {page + 1}"
                 + ('' if items else '\n\nПока нет промокодов.'), reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:promo:(card|toggle):[A-Z0-9_]{1,28}$'))
@router.callback_query(F.data.regexp(r'^adm:promo:set:[A-Z0-9_]{1,28}:[01]$'))
async def promo_card(call: CallbackQuery, db: Database):
    parts = call.data.split(':')
    code = parts[3]
    if len(parts) == 5:
        value = int(parts[4])
        if value == 1:
            target_promo = await db.read('SELECT * FROM promos WHERE code=?', (code,))
            if not target_promo:
                return await call.answer('Промокод не найден.', show_alert=True)
            if not promo_has_discount(target_promo):
                return await call.answer('Нельзя включить промокод с нулевой скидкой.', show_alert=True)
        changed = await db.execute('UPDATE promos SET enabled=? WHERE code=? AND enabled!=?', (value, code, value))
        if changed:
            await db.audit(call.from_user.id, 'promo_toggle', f'{code}={value}')
    promo = await db.read('SELECT * FROM promos WHERE code=?', (code,))
    if not promo:
        return await call.answer('Промокод не найден.', show_alert=True)
    await call.answer()
    status = 'Выключен' if not promo['enabled'] else 'Лимит исчерпан' if promo['used'] >= promo['max_uses'] else 'Активен'
    text = (f"<b>🎫 Промокод {esc(code)}</b>\n\n"
            f"Статус · {status}\nСкидка <b>{promo_discount_label(promo)}</b>\n"
            f"Использовано <b>{promo['used']}</b> из {promo['max_uses']}")
    rows = [(cb('✏️ Изменить скидку и лимит', f'adm:promo:edit:{code}'),)]
    if not promo.get('is_personal'):
        rows.append((cb('🎁 Назначить в авто-пуш', f'adm:ppush:setcode:{code}'),))
    rows.extend((
        (cb('Выключить' if promo['enabled'] else 'Включить',
            f"adm:promo:set:{code}:{int(not promo['enabled'])}",
            style='danger' if promo['enabled'] else 'success'),),
        (cb('◀️ Промокоды', 'adm:promos'), cb('🏠 Главное меню', 'adm:home')),
    ))
    await screen(call, text, reply_markup=kb(*rows))


@router.callback_query(F.data == 'adm:promo:new')
@router.callback_query(F.data.regexp(r'^adm:promo:edit:[A-Z0-9_]{1,28}$'))
async def promo_new_start(call: CallbackQuery, state: FSMContext, db: Database):
    parts = call.data.split(':')
    code = parts[-1] if len(parts) == 4 else None
    if code and not await db.read('SELECT 1 FROM promos WHERE code=?', (code,)):
        return await call.answer('Промокод не найден.', show_alert=True)
    await state.set_state(AdminState.promo_new)
    await state.update_data(code=code)
    await call.answer()
    text = (
        f'<b>✏️ {esc(code)}</b>\n\nВведите скидку (в ₽ или %) и лимит.\n'
        'Например <code>200 100</code> (200 ₽) или <code>20% 100</code> (20%).'
        if code else
        '<b>➕ Промокод</b>\n\nВведите код, скидку (в ₽ или %) и лимит.\n'
        'Например <code>VIP200 200 100</code> (200 ₽) или <code>VIP20 20% 100</code> (20%).'
    )
    await screen(call, text, reply_markup=form_nav('◀️ Отменить', f'adm:promo:card:{code}' if code else 'adm:promos'))


@router.message(AdminState.promo_new, ~F.text.startswith('/'))
async def promo_new_save(message: Message, state: FSMContext, db: Database):
    try:
        existing_code = (await state.get_data()).get('code')
        code, discount, discount_percent, max_uses = _parse_promo_fields(message.text or '', existing_code)
        await db.execute(
            '''INSERT INTO promos(code,discount,discount_percent,max_uses) VALUES(?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET discount=excluded.discount,
               discount_percent=excluded.discount_percent,
               max_uses=excluded.max_uses''',
            (code, discount, discount_percent, max_uses),
        )
        await db.audit(message.from_user.id, 'promo_upsert', code)
        await state.clear()
        await message.answer('✅ Промокод сохранён.', reply_markup=form_nav('🎫 К промокоду', f'adm:promo:card:{code}'))
    except Exception as exc:
        await message.answer(f'Ошибка формата: {esc(exc)}\nПопробуйте ещё раз или /admin.')


SETTING_LABELS = {
    'project_name': '🏷 Название проекта',
    'support_url': '💬 Поддержка (HTTPS-ссылка)',
    'reviews_url': '⭐ Отзывы (HTTPS-ссылка)',
    'partnership_contact': '🤝 Сотрудничество (контакт/текст)',
}


async def _set_visibility_setting(db: Database, admin_id: int, key: str, value: str):
    await db.set_setting(key, value)
    await db.audit(admin_id, 'setting_edit', key)


@router.callback_query(F.data == 'adm:settings')
@router.callback_query(F.data.regexp(r'^adm:(contacts|reviews|help):[01]$'))
async def settings(call: CallbackQuery, db: Database):
    if call.data != 'adm:settings':
        _, section, value = call.data.split(':')
        key = {'contacts': 'contacts_visible', 'reviews': 'reviews_visible', 'help': 'help_visible'}[section]
        await _set_visibility_setting(db, call.from_user.id, key, value)
    visible = await contacts_visible(db)
    show_reviews = await reviews_visible(db)
    show_help = await help_visible(db)
    rows = [[cb(name, f'adm:setting:{field}')] for field, name in SETTING_LABELS.items()]
    rows.append([cb('👁 Скрыть контакты' if visible else '👁 Показать контакты',
                    f'adm:contacts:{int(not visible)}')])
    rows.append([cb('👁 Скрыть отзывы' if show_reviews else '👁 Показать отзывы',
                    f'adm:reviews:{int(not show_reviews)}')])
    rows.append([cb('👁 Скрыть раздел «Помощь»' if show_help else '👁 Открыть раздел «Помощь»',
                    f'adm:help:{int(not show_help)}')])
    rows.append([cb('◀️ Меню', 'adm:home')])
    await call.answer()
    await screen(call, '<b>⚙️ Настройки и контакты</b>\n\nВыберите параметр для изменения.', reply_markup=kb(*rows))


@router.callback_query(F.data.startswith('adm:setting:'))
async def settings_start(call: CallbackQuery, state: FSMContext, db: Database):
    key = call.data.split(':', 2)[2]
    if key not in SETTING_LABELS:
        return await call.answer('Нет такого параметра')
    old = await db.project_name() if key == 'project_name' else (await db.setting(key) or {}).get('value', '')
    await state.set_state(AdminState.settings_field)
    await state.update_data(key=key)
    prompt = f"<b>{SETTING_LABELS[key]}</b>\nТекущее: <code>{esc(old[:350]) or 'не задано'}</code>\n\n"
    if key == 'partnership_contact':
        prompt += 'Отправьте контакт менеджера (например, @manager, https://t.me/manager или пояснительный текст).\nДля удаления отправьте «-».\nОтмена: /admin'
    elif key in ('support_url', 'reviews_url'):
        prompt += 'Отправьте полную HTTPS-ссылку (например, https://t.me/my_support).\nДля удаления отправьте «-».\nОтмена: /admin'
    else:
        prompt += (f'Пришлите новое название одной строкой, до 80 символов. «-» восстановит {DEFAULT_PROJECT_NAME}.\n'
                   'Тексты и нижнее меню будут использовать новое название.\nОтмена: /admin')
    await call.answer()
    await screen(call, prompt, reply_markup=form_nav('◀️ Отменить', 'adm:settings'))


@router.message(AdminState.settings_field, ~F.text.startswith('/'))
async def settings_save(message: Message, state: FSMContext, db: Database):
    key = (await state.get_data())['key']
    value = (message.text or '').strip()
    if not value:
        return await message.answer('Пришлите значение текстом.')
    if value == '-':
        value = DEFAULT_PROJECT_NAME if key == 'project_name' else ''
    if key == 'project_name' and (len(value.splitlines()) != 1 or any(ord(char) < 32 for char in value)):
        return await message.answer('Пришлите название одной строкой.')
    if key in ('support_url', 'reviews_url') and value and not is_https_url(value):
        return await message.answer('Нужна полная HTTPS-ссылка, например https://t.me/my_support')
    limit = 80 if key == 'project_name' else 2000
    if len(value) > limit:
        return await message.answer(f'Не более {limit} символов.')
    await db.set_setting(key, value)
    await db.audit(message.from_user.id, 'setting_edit', key)
    await state.clear()
    text = (f'✅ Название проекта сохранено — <b>{esc(value)}</b>.'
            if key == 'project_name' else '✅ Настройка сохранена.')
    await message.answer(text, reply_markup=form_nav('◀️ Настройки', 'adm:settings'))


ORDER_FILTERS = {
    'all': 'Все', 'review': 'На проверке', 'delivery': 'Выдача',
    'paid': 'Оплачены', 'pending': 'Ожидают оплаты', 'refunded': 'Возвраты',
}


async def orders_page(target: Message | CallbackQuery, db: Database, mode: str = 'all', page: int = 0,
                      user_id: int | None = None):
    conditions, args = [], []
    if mode == 'delivery':
        conditions.append(DELIVERY_CONDITION)
        args.append(now())
    elif mode != 'all':
        conditions.append('o.status=?')
        args.append(mode)
    if user_id is not None:
        conditions.append('o.user_id=?')
        args.append(user_id)
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    items = await db.read(f"""SELECT o.* FROM orders o {where}
        ORDER BY o.created_at DESC,o.rowid DESC LIMIT ? OFFSET ?""",
        (*args, PAGE_SIZE + 1, page * PAGE_SIZE), many=True)
    rows = []
    if user_id is None:
        filters = [cb(label, f'adm:orders:{key}:0', style='primary' if mode == key else None)
                   for key, label in ORDER_FILTERS.items()]
        rows.extend((filters[:3], filters[3:]))
        route, back = f'adm:orders:{mode}', cb('🏠 Главное меню', 'adm:home')
        heading = ORDER_FILTERS[mode]
    else:
        route, back = f'adm:uorders:{user_id}', cb('◀️ Пользователь', f'adm:user:{user_id}')
        heading = f'Пользователь {user_id}'
    for order in items[:PAGE_SIZE]:
        status = ORDER_STATUS.get(order['status'], order['status'])
        label = f"{order['id'][:8]} · {order_amount(order)} · {status}"
        rows.append((cb(label, f"adm:order:{order['id']}"),))
    rows.extend((pager(route, page, len(items) > PAGE_SIZE), (cb('🔍 Найти заказ', 'adm:lookuporder'), back)))
    text = f"<b>💳 Заказы · {heading}</b>\nСтраница {page + 1}"
    if mode == 'delivery':
        text += '\nАктивные покупки, по которым ещё не доставлено уведомление.'
    if not items:
        text += '\n\nВ этом разделе пока пусто.'
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data == 'adm:orders')
@router.callback_query(F.data.regexp(r'^adm:orders:(all|review|delivery|paid|pending|refunded):[0-9]{1,6}$'))
async def orders(call: CallbackQuery, db: Database):
    await call.answer()
    parts = call.data.split(':')
    mode, page = (parts[2], int(parts[3])) if len(parts) == 4 else ('all', 0)
    await orders_page(call, db, mode, page)


@router.callback_query(F.data.regexp(r'^adm:uorders:[0-9]{1,16}:[0-9]{1,6}$'))
async def orders_for_user(call: CallbackQuery, db: Database):
    await call.answer()
    _, _, uid, page = call.data.split(':')
    await orders_page(call, db, page=int(page), user_id=int(uid))


async def order_details(target: Message | CallbackQuery, db: Database, order: dict):
    order_id = order['id']
    purchase = await db.read("""SELECT p.*, (SELECT COUNT(*) FROM grants WHERE purchase_id=p.id AND state='error') AS errors,
        (SELECT COUNT(*) FROM grants WHERE purchase_id=p.id AND revoke_retry_at IS NOT NULL) AS revoke_errors
        FROM purchases p WHERE p.order_id=?""", (order_id,))
    rows = []
    if order['status'] == 'review' and order['provider'] == 'manual':
        rows.extend(((cb('✅ Подтвердить платёж', f'adm:approve:{order_id}', style='success'),),
                     (cb('❌ Отклонить', f'adm:reject:{order_id}', style='danger'),)))
    if order['provider'] == 'stars' and order['charge_id'] and order['status'] == 'paid':
        rows.append((cb('↩️ Возврат Stars', f'adm:refund:{order_id}'),))
    status_label = ORDER_STATUS.get(order['status'], 'Неизвестный статус')
    text = (f"<b>💳 Заказ · {status_label}</b>\n"
            f"<code>{esc(order_id)}</code>\n\n"
            f"📦 {esc(order['title'][:120])}\n"
            f"💰 <b>{order_amount(order)}</b> · {PROVIDERS.get(order['provider'], 'Другой способ')}\n"
            f"👤 Покупатель <code>{order['user_id']}</code>\n"
            f"🕓 Создан {short_date(order['created_at'])}\n")
    if order['paid_at']:
        text += f"Оплата {short_date(order['paid_at'])}\n"
    if order['refund_requested_at']:
        text += '\n⏳ Возврат ожидает подтверждения платёжной системы. Выдача приостановлена.\n'
    if order['promo_code'] and order['checkout_id'] and order['status'] in ('pending', 'expired'):
        text += '\nПромокод зарезервирован до сверки платежа.\n'
    payment_id = order['charge_id'] or order['external_id']
    if payment_id:
        text += f"ID платежа <code>{esc(payment_id[:200])}</code>\n"
    if order['promo_code']:
        text += f"Промокод <code>{esc(order['promo_code'])}</code>\n"
    if order['greeting_event_id']:
        source = await db.read("""SELECT e.chat_id,c.title FROM greeting_events e
            LEFT JOIN welcome_channels c ON c.chat_id=e.chat_id WHERE e.id=?""", (order['greeting_event_id'],))
        if source:
            text += f"Источник · {esc((source['title'] or str(source['chat_id']))[:70])}\n"
    links = [cb('👤 Покупатель', f"adm:user:{order['user_id']}")]
    if purchase:
        if order['notice_sent']:
            status = 'уведомление отправлено'
        elif order['status'] == 'refunded':
            status = 'закрыта после возврата'
        elif order['refund_requested_at']:
            status = 'приостановлена на время возврата'
        elif purchase['expires_at'] and purchase['expires_at'] <= now():
            status = 'доступ завершён, уведомление не доставлено'
        else:
            status = 'уведомление в очереди'
        text += f"\n<b>Выдача</b> · {status}"
        if purchase['errors']:
            text += f" · ошибок приглашений {purchase['errors']}"
        if purchase['expires_at'] and purchase['expires_at'] <= now() and purchase['revoke_errors']:
            text += f" · ошибок исключения из каналов {purchase['revoke_errors']}"
        links.append(cb('🔑 Открыть доступ', f"adm:purchase:{purchase['id']}", style='primary'))
    rows.extend((links, (cb('◀️ Заказы', 'adm:orders'), cb('🏠 Главное меню', 'adm:home'))))
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:order:[a-f0-9]{32}$'))
async def order_card(call: CallbackQuery, shop: Shop, db: Database):
    order = await shop.order(call.data.split(':')[2])
    if not order:
        return await call.answer('Заказ не найден.', show_alert=True)
    await call.answer()
    await order_details(call, db, order)


@router.callback_query(F.data.startswith('adm:refund:'))
async def refund_confirmation(call: CallbackQuery, shop: Shop):
    order = await shop.order(call.data.split(':', 2)[2])
    if not order or order['provider'] != 'stars' or not order['charge_id'] or order['status'] != 'paid':
        return await call.answer('Заказ недоступен для возврата.', show_alert=True)
    await call.answer()
    warning = '\n\nГотовая ссылка может остаться рабочей после возврата.' if order['delivery_urls'] != '[]' else ''
    await screen(call, f"<b>↩️ Подтверждение возврата</b>\n\nВернуть <b>{order['amount_stars']} ⭐</b> покупателю и закрыть доступ по заказу?{warning}",
        reply_markup=kb((cb('⚠️ Подтвердить возврат', f"adm:refundok:{order['id']}", style='danger'),),
                        (cb('◀️ Отмена', f"adm:order:{order['id']}"),)))


@router.callback_query(F.data.startswith('adm:refundok:'))
async def refund_confirmed(call: CallbackQuery, shop: Shop, db: Database, bot: Bot):
    await call.answer()
    order_id = call.data.split(':', 2)[2]
    order = await shop.order(order_id)
    if not order or order['provider'] != 'stars' or not order['charge_id'] or order['status'] != 'paid':
        return await call.message.answer('Заказ недоступен для возврата.')

    async def request_refund():
        return await bot.refund_star_payment(order['user_id'], order['charge_id'], request_timeout=10)

    try:
        await db.audit(call.from_user.id, 'stars_refund_requested', order_id)
        changed = await shop.refund_stars(order['charge_id'], order['amount_stars'], user_id=order['user_id'],
                                          order_id=order_id, request_refund=request_refund)
    except (TelegramAPIError, TimeoutError, OSError):
        log.warning('Refund API did not confirm order %s', order_id, exc_info=True)
        current = await shop.order(order_id)
        text = ('Возврат ожидает сверки с Telegram. Выдача приостановлена.' if current['refund_requested_at']
                else 'Telegram отклонил возврат. Доступ сохранён. Проверьте данные и повторите попытку.')
        return await screen(call, text,
                            reply_markup=form_nav('◀️ К заказу', f'adm:order:{order_id}'))
    if changed:
        await db.audit(call.from_user.id, 'stars_refunded', order_id)
    await screen(call, '✅ Stars возвращены. Доступ по заказу закрыт.' if changed else
                 'Статус возврата смотрите в карточке заказа.',
                 reply_markup=form_nav('◀️ К заказу', f'adm:order:{order_id}'))


@router.callback_query(F.data == 'adm:lookuporder')
async def lookup_order_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.lookup_order)
    await call.answer()
    await screen(call, '<b>🔍 Найти заказ</b>\n\nВведите полный номер, первые 8 символов или ID платежа.',
                 reply_markup=form_nav('◀️ Отменить', 'adm:orders'))


@router.message(AdminState.lookup_order, ~F.text.startswith('/'))
async def lookup_order_finish(message: Message, state: FSMContext, db: Database):
    value = (message.text or '').strip().lstrip('#')
    if not value:
        return await message.answer('Введите номер заказа или платежа.')
    prefix = value.lower() + '%' if re.fullmatch(r'[0-9a-fA-F]{8}', value) else ''
    items = await db.read("""SELECT * FROM orders WHERE id=? OR charge_id=? OR external_id=? OR id LIKE ?
        ORDER BY created_at DESC LIMIT 2""", (value.lower(), value, value, prefix), many=True)
    if not items:
        return await message.answer('Заказ не найден. Проверьте номер или нажмите «Отменить».')
    if len(items) > 1:
        return await message.answer('Нашлось несколько заказов. Введите полный номер заказа.')
    await state.clear()
    await order_details(message, db, items[0])


@router.callback_query(F.data.startswith('adm:approve:'))
async def approve_manual(call: CallbackQuery, shop: Shop, db: Database,
                         bot: Bot | None = None, config: Config | None = None):
    order_id = call.data.split(':', 2)[2]
    try:
        changed = await shop.settle(order_id, 'manual')
    except ShopError as exc:
        return await call.answer(str(exc)[:180], show_alert=True)
    if changed:
        await db.audit(call.from_user.id, 'manual_payment_approved', order_id)
        bot_obj = bot or getattr(call, 'bot', None)
        if bot_obj is not None:
            await dispatch_whale_alerts(bot_obj, db, config)
    current = await shop.order(order_id)
    status = ('Уже оплачено.' if current and current['status'] == 'paid'
              else 'Заказ уже закрыт. Проверьте актуальный статус.')
    await call.answer('Подтверждено. Выдача доступа запущена.' if changed else status, show_alert=True)
    if call.message and current:
        await order_details(call, db, current)


@router.callback_query(F.data.startswith('adm:reject:'))
async def reject_manual(call: CallbackQuery, db: Database, bot: Bot):
    order_id = call.data.split(':', 2)[2]
    def txn(con):
        order = con.execute(
            "SELECT id, user_id FROM orders WHERE id=? AND provider='manual' AND status='review'",
            (order_id,),
        ).fetchone()
        if not order:
            return None
        changed = con.execute(
            "UPDATE orders SET status='failed' WHERE id=? AND provider='manual' AND status='review'",
            (order_id,),
        ).rowcount
        return dict(order) if changed else None
    rejected = await db.atomic(txn)
    if rejected:
        await db.audit(call.from_user.id, 'manual_payment_rejected', order_id)
        try:
            await bot.send_message(rejected['user_id'], f'❌ Ручной платёж по заказу <code>{order_id}</code> отклонён. Если средства списаны, обратись в поддержку с чеком.')
        except Exception:
            pass
    await call.answer('Отклонено.' if rejected else 'Статус уже изменился.', show_alert=True)
    if call.message:
        current = await db.read('SELECT * FROM orders WHERE id=?', (order_id,))
        if current:
            await order_details(call, db, current)


@router.callback_query(F.data == 'adm:users')
@router.callback_query(F.data.regexp(r'^adm:users:[0-9]{1,6}$'))
@router.callback_query(F.data.regexp(r'^adm:users:(recent|whales):[0-9]{1,6}$'))
async def user_list(call: CallbackQuery, db: Database):
    await call.answer()
    parts = call.data.split(':')
    if len(parts) == 4:
        mode, page = parts[2], int(parts[3])
    elif len(parts) == 3:
        mode, page = 'recent', int(parts[2])
    else:
        mode, page = 'recent', 0

    if mode == 'whales':
        users = await db.read(
            """SELECT u.*, COALESCE(SUM(o.amount_rub), 0) AS ltv
               FROM users u
               JOIN orders o ON o.user_id=u.user_id
               WHERE o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL
               GROUP BY u.user_id
               HAVING ltv > 0
               ORDER BY ltv DESC, u.last_seen DESC, u.user_id DESC
               LIMIT ? OFFSET ?""",
            (PAGE_SIZE + 1, page * PAGE_SIZE),
            many=True,
        )
        rows = [(
            cb(f"🐋 {u['ltv']} ₽ · {(u['name'] or u['username'] or 'Пользователь')[:22]} · {u['user_id']}",
               f"adm:user:{u['user_id']}"),
        ) for u in users[:PAGE_SIZE]]
        subtitle = 'Топ покупателей по LTV'
    else:
        users = await db.read('SELECT * FROM users ORDER BY last_seen DESC,user_id DESC LIMIT ? OFFSET ?',
                              (PAGE_SIZE + 1, page * PAGE_SIZE), many=True)
        rows = [(cb(f"{(u['name'] or u['username'] or 'Пользователь')[:30]} · {u['user_id']}",
                    f"adm:user:{u['user_id']}"),) for u in users[:PAGE_SIZE]]
        subtitle = 'По последней активности'

    nav_modes = (
        cb('🕒 Активные', 'adm:users:recent:0', style='primary' if mode == 'recent' else None),
        cb('🐋 Киты (Топ LTV)', 'adm:users:whales:0', style='primary' if mode == 'whales' else None),
    )
    all_rows = [nav_modes, *rows]
    all_rows.extend((pager(f'adm:users:{mode}', page, len(users) > PAGE_SIZE),
                     (cb('🔍 Найти', 'adm:lookupuser'), cb('🏠 Главное меню', 'adm:home'))))
    text = f'<b>👥 Пользователи</b>\n{subtitle} · стр. {page + 1}'
    await screen(call, text + ('' if users else '\n\nЗдесь пока пусто.'), reply_markup=kb(*all_rows))


async def user_details(target: Message | CallbackQuery, db: Database, user: dict):
    uid = user['user_id']
    counts = await db.read("""SELECT
        (SELECT COUNT(*) FROM orders WHERE user_id=?) AS orders,
        (SELECT COALESCE(SUM(amount_rub), 0) FROM orders
            WHERE user_id=? AND status='paid' AND provider!='admin' AND refund_requested_at IS NULL) AS ltv,
        (SELECT COUNT(*) FROM purchases p JOIN orders o ON o.id=p.order_id
            WHERE p.user_id=? AND o.status='paid' AND o.refund_requested_at IS NULL
              AND (p.expires_at IS NULL OR p.expires_at>?)) AS active""",
        (uid, uid, uid, now()))
    purchases = await db.read("""SELECT p.* FROM purchases p JOIN orders o ON o.id=p.order_id WHERE p.user_id=?
        ORDER BY CASE WHEN o.status='paid' AND o.refund_requested_at IS NULL
            AND (p.expires_at IS NULL OR p.expires_at>?) THEN 0 ELSE 1 END,p.id DESC LIMIT 4""",
        (uid, now()), many=True)
    username = '@' + esc(user['username']) if user['username'] else '—'
    text = (f"👤 <b>{esc(user['name'][:100]) or 'Пользователь'}</b>\n"
            f"ID <code>{uid}</code> · {username}\n"
            f"Регистрация {short_date(user['created_at'])}\n"
            f"Активность {short_date(user['last_seen'])}\n"
            f"Промокод <code>{esc(user['active_promo']) or '—'}</code>\n\n"
            f"Заказов <b>{counts['orders']}</b> · активных доступов <b>{counts['active']}</b>\n"
            f"💰 Оплачено (LTV) <b>{counts['ltv']} ₽</b>")
    rows = [(cb(f"#{purchase['id']} · {purchase['title'][:38]}", f"adm:purchase:{purchase['id']}"),)
            for purchase in purchases]
    rows.extend(((cb('💳 Все заказы', f'adm:uorders:{uid}:0'),
                  cb('➕ Выдать доступ', f'adm:give:{uid}', style='success')),
                 (cb('◀️ Пользователи', 'adm:users'), cb('🏠 Главное меню', 'adm:home'))))
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:user:[0-9]{1,16}$'))
async def user_card(call: CallbackQuery, db: Database):
    uid = int(call.data.split(':')[2])
    user = await db.read('SELECT * FROM users WHERE user_id=?', (uid,))
    if not user:
        return await call.answer('Пользователь не найден.', show_alert=True)
    await call.answer()
    await user_details(call, db, user)


@router.callback_query(F.data == 'adm:lookupuser')
async def lookup_user_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.lookup_user)
    await call.answer()
    await screen(call, '<b>🔍 Найти пользователя</b>\n\nВведите Telegram ID или @username.',
                 reply_markup=form_nav('◀️ Отменить', 'adm:users'))


@router.message(AdminState.lookup_user, ~F.text.startswith('/'))
async def lookup_user_finish(message: Message, state: FSMContext, db: Database):
    value = (message.text or '').strip().lstrip('@')
    if re.fullmatch(r'[0-9]{1,16}', value):
        users = await db.read('SELECT * FROM users WHERE user_id=?', (int(value),), many=True)
    elif re.fullmatch(r'[A-Za-z0-9_]{1,32}', value):
        users = await db.read('SELECT * FROM users WHERE username=? COLLATE NOCASE LIMIT 2', (value,), many=True)
    else:
        return await message.answer('Введите Telegram ID или @username.')
    if not users:
        return await message.answer('Пользователь не найден.')
    if len(users) > 1:
        return await message.answer('Этот username встречается в нескольких записях. Введите Telegram ID.')
    await state.clear()
    await user_details(message, db, users[0])


ACCESS_FILTERS = {'active': 'Активные', 'errors': 'С ошибками', 'all': 'Все'}


@router.callback_query(F.data == 'adm:grants')
@router.callback_query(F.data.regexp(r'^adm:grants:(active|errors|all):[0-9]{1,6}$'))
async def grant_menu(call: CallbackQuery, db: Database):
    await call.answer()
    parts = call.data.split(':')
    mode, page = (parts[2], int(parts[3])) if len(parts) == 4 else ('active', 0)
    active = "o.status='paid' AND o.refund_requested_at IS NULL AND (p.expires_at IS NULL OR p.expires_at>?)"
    predicate = {'active': active,
                 'errors': '(' + active + """ AND EXISTS (
                            SELECT 1 FROM grants g WHERE g.purchase_id=p.id AND g.state='error'
                              AND NOT EXISTS (SELECT 1 FROM product_channel_links l WHERE l.chat_id=g.chat_id)))
                            OR (p.expires_at<=? AND EXISTS (SELECT 1 FROM grants g
                                WHERE g.purchase_id=p.id AND g.revoke_retry_at IS NOT NULL
                                  AND g.state NOT IN ('expired','revoked')))""",
                 'all': '1=1'}[mode]
    args = () if mode == 'all' else (now(), now()) if mode == 'errors' else (now(),)
    items = await db.read(f"""SELECT p.* FROM purchases p JOIN orders o ON o.id=p.order_id WHERE {predicate}
        ORDER BY p.id DESC LIMIT ? OFFSET ?""", (*args, PAGE_SIZE + 1, page * PAGE_SIZE), many=True)
    rows = [tuple(cb(label, f'adm:grants:{key}:0', style='primary' if mode == key else None)
                  for key, label in ACCESS_FILTERS.items())]
    rows.extend((cb(f"#{p['id']} · {p['title'][:28]} · {p['user_id']}", f"adm:purchase:{p['id']}"),)
                for p in items[:PAGE_SIZE])
    rows.extend((pager(f'adm:grants:{mode}', page, len(items) > PAGE_SIZE),
                 (cb('➕ Выдать доступ', 'adm:grant:new', style='success'), cb('🏠 Главное меню', 'adm:home'))))
    text = f'<b>🔑 Доступы · {ACCESS_FILTERS[mode]}</b>\nСтраница {page + 1}'
    await screen(call, text + ('' if items else '\n\nВ этом разделе пока пусто.'), reply_markup=kb(*rows))


async def purchase_record(db: Database, purchase_id: int):
    return await db.read("""SELECT p.*,o.status AS order_status,o.notice_sent,o.notice_retry_at,o.delivery_urls,
        o.refund_requested_at
        FROM purchases p JOIN orders o ON o.id=p.order_id WHERE p.id=?""", (purchase_id,))


async def purchase_details(target: Message | CallbackQuery, db: Database, purchase: dict):
    pid = purchase['id']
    grants = await db.read("""SELECT g.*,l.url AS shared_url FROM grants g
        LEFT JOIN product_channel_links l ON l.chat_id=g.chat_id
        WHERE g.purchase_id=? ORDER BY g.id""", (pid,), many=True)
    current = not purchase['expires_at'] or purchase['expires_at'] > now()
    refunded = purchase['order_status'] == 'refunded'
    paused = bool(purchase['refund_requested_at']) and not refunded
    active = current and purchase['order_status'] == 'paid' and not paused
    removal_errors = [grant for grant in grants if not current and grant['revoke_retry_at']
                      and grant['state'] not in ('expired', 'revoked')]
    status = 'Возврат' if refunded else 'Возврат проверяется' if paused else 'Активен' if active else 'Завершён'
    term_line = f"Действует до {short_date(purchase['expires_at'])}" if purchase['expires_at'] else 'Срок · <b>НАВСЕГДА</b>'
    text = (f"<b>🔑 Доступ #{pid}</b>\n{esc(purchase['title'][:110])}\n\n"
            f"Покупатель <code>{purchase['user_id']}</code>\n"
            f"Состояние <b>{status}</b>\n"
            f"{term_line}\n"
            f"Уведомление {'доставлено' if purchase['notice_sent'] else 'приостановлено' if paused else 'ожидает отправки' if active else 'не доставлено'}")
    if purchase['delivery_urls'] != '[]':
        text += '\nГотовая ссылка. Отзыв уберёт кнопку из кабинета. Уже выданная ссылка может остаться рабочей.'
    invite_errors = [g for g in grants if g['state'] == 'error' and not g['shared_url']]
    if grants:
        states = ['ready' if g['shared_url'] and g['state'] in ('need_link','creating','error') else g['state']
                  for g in grants]
        summary = ' · '.join(f"{GRANT_STATUS.get(state, state)} {states.count(state)}" for state in dict.fromkeys(states))
        text += f'\n\n<b>Каналы</b> · {summary}'
        errors = [f"<code>{g['chat_id']}</code> · {esc(g['last_error'][:130])}"
                  for g in grants if (g in invite_errors or g in removal_errors) and g['last_error']]
        if errors:
            text += '\n' + '\n'.join(errors[:5])
    if removal_errors:
        text += '\n\n⚠️ Не удалось исключить участника из каналов. Проверьте права бота и повторите отзыв.'
    rows = []
    if removal_errors:
        rows.append((cb('🔄 Повторить отзыв', f'adm:retryrevoke:{pid}', style='primary'),))
    if active and not purchase['refund_requested_at'] and (not purchase['notice_sent'] or invite_errors):
        rows.append((cb('🔄 Повторить выдачу', f'adm:retry:{pid}', style='primary'),))
    if active and not purchase['refund_requested_at'] and purchase['delivery_urls'] != '[]':
        rows.append((cb('🔗 Заменить ссылки в покупке', f'adm:fixlinks:{pid}'),))
    actions = []
    if purchase['expires_at'] and not refunded and not purchase['refund_requested_at']:
        actions.append(cb('↗️ Продлить', f'adm:extend:{pid}'))
    if active:
        actions.append(cb('Отозвать доступ', f'adm:revoke:{pid}', style='danger'))
    rows.extend((actions, (cb('💳 Заказ', f"adm:order:{purchase['order_id']}"),
                           cb('👤 Пользователь', f"adm:user:{purchase['user_id']}")),
                 (cb('◀️ Доступы', 'adm:grants'), cb('🏠 Главное меню', 'adm:home'))))
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:retryrevoke:[0-9]{1,16}$'))
async def retry_revocation(call: CallbackQuery, db: Database, access: AccessManager):
    pid = int(call.data.split(':')[2])
    await call.answer()
    async with access.lock:
        changed = await db.execute("""UPDATE grants SET revoke_retry_at=NULL
            WHERE purchase_id=? AND revoke_retry_at IS NOT NULL AND state NOT IN ('expired','revoked')
              AND EXISTS (SELECT 1 FROM purchases p WHERE p.id=grants.purchase_id AND p.expires_at<=?)""",
            (pid, now()))
    if changed:
        await db.audit(call.from_user.id, 'retry_revocation', str(pid))
    await screen(call, '✅ Повторный отзыв поставлен в ближайший цикл.' if changed else
                 'Повтор отзыва сейчас не требуется.',
                 reply_markup=form_nav('◀️ К доступу', f'adm:purchase:{pid}'))


@router.callback_query(F.data.regexp(r'^adm:purchase:[0-9]{1,16}$'))
async def purchase_card(call: CallbackQuery, db: Database):
    purchase = await purchase_record(db, int(call.data.split(':')[2]))
    if not purchase:
        return await call.answer('Доступ не найден.', show_alert=True)
    await call.answer()
    await purchase_details(call, db, purchase)


@router.callback_query(F.data.regexp(r'^adm:fixlinks:[0-9]{1,16}$'))
async def replace_links_start(call: CallbackQuery, db: Database, state: FSMContext):
    pid = int(call.data.split(':')[2])
    purchase = await purchase_record(db, pid)
    if (not purchase or purchase['order_status'] != 'paid' or purchase['refund_requested_at']
        or purchase['delivery_urls'] == '[]' or (purchase['expires_at'] and purchase['expires_at'] <= now())):
        return await call.answer('Эту покупку сейчас нельзя изменить.', show_alert=True)
    await state.set_state(AdminState.purchase_links)
    await state.update_data(pid=pid)
    await call.answer()
    await screen(call, '<b>Замена ссылок</b>\n\nПришлите полный набор новых ссылок Telegram, по одной на строку.',
                 reply_markup=form_nav('◀️ Отменить', f'adm:purchase:{pid}'))


@router.message(AdminState.purchase_links, ~F.text.startswith('/'))
async def replace_links_finish(message: Message, db: Database, shop: Shop, state: FSMContext):
    pid = (await state.get_data())['pid']
    try:
        changed = await shop.replace_purchase_links(pid, message.text or '')
    except ValueError as exc:
        return await message.answer(esc(exc))
    await state.clear()
    if changed:
        await db.audit(message.from_user.id, 'replace_purchase_links', str(pid))
    await screen(message, 'Ссылки заменены. Покупатель получит обновление.' if changed else
                 'Покупка больше недоступна для изменения.',
                 reply_markup=form_nav('◀️ К доступу', f'adm:purchase:{pid}'))


@router.callback_query(F.data.regexp(r'^adm:retry:[0-9]{1,16}$'))
async def retry_purchase(call: CallbackQuery, db: Database, access: AccessManager):
    pid = int(call.data.split(':')[2])
    await call.answer('Проверяю состояние доступа…')
    async with access.lock:
        def enqueue(con):
            timestamp = now()
            order = con.execute("""SELECT o.id FROM orders o JOIN purchases p ON p.order_id=o.id
                WHERE p.id=? AND o.status='paid' AND o.refund_requested_at IS NULL
                  AND (p.expires_at IS NULL OR p.expires_at>?)""",
                (pid, timestamp)).fetchone()
            if not order:
                return False
            grants = con.execute("""UPDATE grants SET state='need_link',processing_at=NULL
                WHERE purchase_id=? AND state='error'""", (pid,)).rowcount
            notice = con.execute("""UPDATE orders SET notice_retry_at=NULL
                WHERE id=? AND notice_sent=0 AND notice_retry_at IS NOT NULL""", (order['id'],)).rowcount
            return bool(grants or notice)
        changed = await db.atomic(enqueue)
    if changed:
        await db.audit(call.from_user.id, 'retry_access', str(pid))
    purchase = await purchase_record(db, pid)
    text = '✅ Выдача поставлена в ближайший цикл.' if changed else 'Для этого доступа повтор сейчас не требуется.'
    await screen(call, text, reply_markup=form_nav('◀️ К доступу', f"adm:purchase:{pid}" if purchase else 'adm:grants'))


@router.callback_query(F.data.regexp(r'^adm:give:[0-9]{1,16}$'))
async def grant_for_user(call: CallbackQuery, state: FSMContext, db: Database):
    uid = int(call.data.split(':')[2])
    if not await db.read('SELECT 1 FROM users WHERE user_id=?', (uid,)):
        return await call.answer('Пользователь не найден.', show_alert=True)
    await state.set_state(AdminState.grant)
    await state.update_data(uid=uid)
    await call.answer()
    await screen(call, f'<b>➕ Выдать доступ пользователю {uid}</b>\n\n'
                 'Введите ID товара и срок в днях. Например <code>1 30</code> (<code>0</code> — навсегда).',
                 reply_markup=form_nav('◀️ Отменить', f'adm:user:{uid}'))


@router.callback_query(F.data.regexp(r'^adm:extend:[0-9]{1,16}$'))
async def extend_purchase(call: CallbackQuery, state: FSMContext, db: Database):
    pid = int(call.data.split(':')[2])
    purchase = await purchase_record(db, pid)
    if not purchase or not purchase['expires_at'] or purchase['order_status'] == 'refunded' or purchase['refund_requested_at']:
        return await call.answer('Этот доступ нельзя продлить.', show_alert=True)
    await state.set_state(AdminState.extension)
    await state.update_data(pid=pid)
    await call.answer()
    await screen(call, f'<b>↗️ Продлить доступ #{pid}</b>\n\n'
                 'Введите количество дней. Для действующего доступа срок добавится к текущей дате окончания.',
                 reply_markup=form_nav('◀️ Отменить', f'adm:purchase:{pid}'))


@router.callback_query(F.data.regexp(r'^adm:revoke:[0-9]{1,16}$'))
async def revoke_purchase_prompt(call: CallbackQuery, db: Database):
    pid = int(call.data.split(':')[2])
    purchase = await purchase_record(db, pid)
    if not purchase:
        return await call.answer('Доступ не найден.', show_alert=True)
    await call.answer()
    await revoke_prompt(call, purchase)


async def revoke_prompt(target: Message | CallbackQuery, purchase: dict):
    pid = purchase['id']
    await screen(target, f"<b>⛔ Отозвать доступ #{pid}?</b>\n\n"
                 f"{esc(purchase['title'][:100])} · <code>{purchase['user_id']}</code>\n"
                 + ('Готовая ссылка исчезнет из кабинета. Уже выданная ссылка может остаться рабочей.'
                    if purchase['delivery_urls'] != '[]' else
                    'Бот исключит покупателя из каналов, к которым у него больше нет действующего доступа.'),
                 reply_markup=kb((cb('⚠️ Подтвердить отзыв', f'adm:revokeok:{pid}', style='danger'),),
                                 (cb('◀️ Отмена', f'adm:purchase:{pid}'),)))


@router.callback_query(F.data.regexp(r'^adm:revokeok:[0-9]{1,16}$'))
async def revoke_purchase(call: CallbackQuery, db: Database, access: AccessManager):
    pid = int(call.data.split(':')[2])
    await call.answer()
    async with access.shop.delivery_lock, access.lock:
        timestamp = now()
        changed = await db.execute("""UPDATE purchases SET expires_at=?, revoked_by_admin=1 WHERE id=?
            AND (expires_at IS NULL OR expires_at>?)""", (timestamp, pid, timestamp))
    if changed:
        await db.audit(call.from_user.id, 'revoke', str(pid))
    await screen(call, '✅ Доступ закрыт. Бот обработает выход из каналов.' if changed else
                 'Этот доступ уже завершён.',
                 reply_markup=form_nav('◀️ К доступу', f'adm:purchase:{pid}'))


@router.callback_query(F.data.startswith('adm:grant:'))
async def grant_start(call: CallbackQuery, state: FSMContext):
    what = call.data.split(':', 2)[2]
    if what == 'new':
        await state.set_state(AdminState.grant)
        text = ('<b>➕ Выдача доступа</b>\n\n'
                'Введите: <code>ID_ПОЛЬЗОВАТЕЛЯ ID_ТОВАРА ДНЕЙ</code>\n'
                'Например: <code>123456789 1 0</code> (<code>0</code> = навсегда).')
    elif what == 'extend':
        await state.set_state(AdminState.extension)
        text = ('<b>⏳ Продление доступа</b>\n\n'
                'Введите: <code>ID_ДОСТУПА ДНЕЙ</code>\n'
                'Продление считается от текущей даты окончания или от сегодняшнего дня.')
    elif what == 'revoke':
        await state.set_state(AdminState.revoke)
        text = ('<b>⛔ Отзыв доступа</b>\n\n'
                'Введите: <code>ID_ДОСТУПА</code>\n'
                'Доступ будет закрыт сразу после подтверждения.')
    else:
        return await call.answer('Неизвестная команда')
    await call.answer()
    await screen(call, text, reply_markup=form_nav('◀️ Отменить', 'adm:grants'))


@router.message(AdminState.grant, ~F.text.startswith('/'))
async def manual_grant(message: Message, state: FSMContext, db: Database,
                        shop: Shop, bot: Bot):
    try:
        data = await state.get_data()
        fields = (message.text or '').split()
        if data.get('uid'):
            fields.insert(0, str(data['uid']))
        uid, tariff_id, days = map(int, fields)
        if not (0 < uid < 2 ** 52 and 0 < tariff_id < 2 ** 52 and 0 <= days <= MAX_DURATION_DAYS):
            raise ValueError('Проверьте ID и срок доступа.')
        await db.register(uid)
        tariff = await shop.get_tariff(tariff_id)
        if not tariff:
            raise ShopError('Товар не найден.')
        ids, _ = tariff_delivery(tariff)
        if ids:
            await validate_sale_channels(bot, ids)
        order = await shop.create_order(uid, tariff_id, 'admin', duration_days=days)
        await shop.settle(order['id'], 'admin')
        await db.audit(message.from_user.id, 'grant_access', f'{order["id"]}:{days}')
        await state.clear()
        purchase = await db.read('SELECT id FROM purchases WHERE order_id=?', (order['id'],))
        term = 'навсегда' if days == 0 else f'на {duration(days)}'
        await message.answer(f"✅ Доступ оформлен ({term}). Бот готовит приглашения.",
                             reply_markup=form_nav('🔑 Открыть доступ', f"adm:purchase:{purchase['id']}"))
    except (ValueError, ShopError) as exc:
        await message.answer(f'Ошибка: {esc(exc)}\nПовторите ввод или /admin.')
    except Exception:
        log.exception('Manual grant failed')
        await message.answer('Ошибка при выдаче. Проверьте логи.')


@router.message(AdminState.extension, ~F.text.startswith('/'))
async def extend(message: Message, state: FSMContext, db: Database, access: AccessManager):
    try:
        data = await state.get_data()
        fields = (message.text or '').split()
        if data.get('pid'):
            fields.insert(0, str(data['pid']))
        purchase_id, days = map(int, fields)
        if not (0 < purchase_id < 2 ** 52 and 1 <= days <= MAX_DURATION_DAYS):
            raise ValueError('Проверьте номер доступа и срок от 1 до 3650 дней.')
        def txn(con):
            row = con.execute("""SELECT p.*,o.status,o.refund_requested_at FROM purchases p JOIN orders o ON o.id=p.order_id
                WHERE p.id=?""", (purchase_id,)).fetchone()
            if not row: raise ShopError('Покупка отсутствует.')
            if row['status'] == 'refunded' or row['refund_requested_at']:
                raise ShopError('Покупку в процессе возврата или после него нельзя продлить.')
            if not row['expires_at']: raise ShopError('Доступ бессрочный — его не нужно продлевать.')
            current_dt = datetime.now(timezone.utc)
            was_closed = bool(row['revoked_by_admin']) or parse_date(row['expires_at']) <= current_dt
            base = max(parse_date(row['expires_at']), current_dt)
            end = (base + timedelta(days=days)).isoformat(timespec='seconds')
            con.execute(
                'UPDATE purchases SET expires_at=?, revoked_by_admin=0, expiry_notice_sent=0, pre_expiry_upsell_sent=0 WHERE id=?',
                (end, purchase_id),
            )
            con.execute(
                """UPDATE grants
                      SET revoke_retry_at=NULL,
                          last_error=''
                    WHERE purchase_id=? AND revoke_retry_at IS NOT NULL""",
                (purchase_id,),
            )
            reset_grants = con.execute(
                """UPDATE grants
                      SET state='need_link', last_error='', processing_at=NULL
                    WHERE purchase_id=? AND state IN ('expired', 'error')""",
                (purchase_id,),
            ).rowcount
            reopened = was_closed or bool(reset_grants)
            if reopened:
                con.execute(
                    'UPDATE orders SET notice_sent=0, notice_retry_at=NULL WHERE id=?',
                    (row['order_id'],),
                )
            return end, reopened
        async with access.lock:
            end, reopened = await db.atomic(txn)
        await db.audit(message.from_user.id, 'extend', f'{purchase_id}:{days}')
        await state.clear()
        note = ' Бот перевыпустит ссылки и уведомит покупателя.' if reopened else ''
        await message.answer(f'✅ Продлено до {short_date(end)}.{note}',
                             reply_markup=form_nav('🔑 Открыть доступ', f'adm:purchase:{purchase_id}'))
    except (ShopError, ValueError, TypeError) as exc:
        await message.answer(f'Ошибка: {esc(exc)}\nПовторите ввод или /admin.')


@router.message(AdminState.revoke, ~F.text.startswith('/'))
async def revoke(message: Message, state: FSMContext, db: Database):
    value = (message.text or '').strip()
    if not re.fullmatch(r'[0-9]{1,16}', value):
        return await message.answer('Введите номер доступа.')
    purchase = await purchase_record(db, int(value))
    if not purchase:
        return await message.answer('Доступ не найден.')
    await state.clear()
    await revoke_prompt(message, purchase)


_VIP_ORDER_SQL = (
    "(CASE COALESCE((SELECT t.tier FROM tariffs t WHERE t.id=o.tariff_id), '') "
    "WHEN 'ultra' THEN 1 WHEN 'diamond' THEN 1 WHEN 'regular' THEN 0 "
    "ELSE (UPPER(o.title) LIKE '%ULTRA%' OR UPPER(o.title) LIKE '%DIAMOND%') END = 1)"
)

_OPT_IN_SQL = 'COALESCE(u.broadcast_opt_out, 0)=0'

BROADCAST_SEGMENTS = {
    'cold': (
        '❄️ Некупившие (0 покупок)',
        f"{_OPT_IN_SQL} AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.status='paid' "
        "AND o.provider!='admin' AND o.refund_requested_at IS NULL)",
    ),
    'buyers': (
        '💳 Купившие (все с оплатой)',
        f"{_OPT_IN_SQL} AND EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.status='paid' "
        "AND o.provider!='admin' AND o.refund_requested_at IS NULL)",
    ),
    'all': ('🌍 Все пользователи', _OPT_IN_SQL),
    'entry': (
        '🎯 Обычные покупатели (без ULTRA/DIAMOND)',
        f"{_OPT_IN_SQL} AND EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.status='paid' "
        "AND o.provider!='admin' AND o.refund_requested_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.status='paid' "
        f"AND o.provider!='admin' AND o.refund_requested_at IS NULL AND {_VIP_ORDER_SQL})",
    ),
    'whales': (
        '🐋 VIP и Киты (ULTRA / DIAMOND / LTV ≥ 1000 ₽)',
        f"{_OPT_IN_SQL} AND (EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.status='paid' "
        "AND o.provider!='admin' AND o.refund_requested_at IS NULL "
        f"AND {_VIP_ORDER_SQL}) "
        "OR COALESCE((SELECT SUM(o.amount_rub) FROM orders o WHERE o.user_id=u.user_id "
        "AND o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL), 0) >= 1000)",
    ),
}

_ACTIVE_BROADCASTS: dict[int, tuple[asyncio.Task, asyncio.Event]] = {}
_BROADCAST_ALBUMS: dict[tuple[int, str], dict] = {}
_BROADCAST_ALBUM_IDLE_SEC = 0.65


class IncompleteBroadcastAlbum(Exception):
    pass


def _clear_broadcast_albums(user_id: int) -> None:
    """Drop any buffered album preview tasks owned by user_id."""
    for key in [k for k in _BROADCAST_ALBUMS if k[0] == user_id]:
        entry = _BROADCAST_ALBUMS.pop(key, None)
        task = entry.get('task') if entry else None
        if task and not task.done():
            task.cancel()


async def _copy_broadcast_post(bot: Bot, target_chat_id: int, from_chat_id: int, msg_ids: list[int]) -> list[int]:
    """Copy a single post or a multi-media album to target_chat_id."""
    if len(msg_ids) == 1:
        copied = await bot.copy_message(target_chat_id, from_chat_id, msg_ids[0])
        return [copied.message_id]
    copied = await bot.copy_messages(target_chat_id, from_chat_id, msg_ids)
    if len(copied) != len(msg_ids):
        raise IncompleteBroadcastAlbum
    return [item.message_id for item in copied]


async def _user_opted_out(db: Database, user_id: int) -> bool:
    row = await db.read('SELECT broadcast_opt_out FROM users WHERE user_id=?', (user_id,))
    return bool(row and row['broadcast_opt_out'])


async def _run_broadcast(bot: Bot, db: Database, admin_id: int, from_chat_id: int, msg_ids: int | list[int] | tuple[int, ...],
                         seg: str, users: list[dict], progress: Message, stop_event: asyncio.Event):
    post_ids = [msg_ids] if isinstance(msg_ids, int) else [int(mid) for mid in msg_ids]
    success = fail = skipped = 0
    total = len(users)
    try:
        for index, row in enumerate(users, 1):
            if stop_event.is_set():
                break
            uid = row['user_id']
            if await _user_opted_out(db, uid):
                skipped += 1
                continue
            try:
                try:
                    await _copy_broadcast_post(bot, uid, from_chat_id, post_ids)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after)
                    if stop_event.is_set():
                        break
                    if await _user_opted_out(db, uid):
                        skipped += 1
                        continue
                    await _copy_broadcast_post(bot, uid, from_chat_id, post_ids)
                success += 1
            except Exception:
                fail += 1
            if index % 25 == 0 and index < total and not stop_event.is_set():
                try:
                    await progress.edit_text(
                        f'⏳ Рассылка выполняется: <b>{index}/{total}</b>\n'
                        f'Доставлено <b>{success}</b> · Ошибок <b>{fail}</b>',
                        reply_markup=kb((cb('🛑 Остановить рассылку', 'adm:bcast:stop', style='danger'),)),
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.07)
        stopped = stop_event.is_set()
        await db.audit(admin_id, 'broadcast_stopped' if stopped else 'broadcast', f'{seg}:{success}/{total}')
        title = '🛑 <b>Рассылка остановлена</b>' if stopped else '✅ <b>Рассылка завершена</b>'
        summary = f'{title}\n\nДоставлено <b>{success}</b>\nНе доставлено <b>{fail}</b>'
        if skipped:
            summary += f'\nОтписались <b>{skipped}</b>'
        done_kb = form_nav('📢 Новая рассылка', 'adm:broadcast')
        try:
            await progress.edit_text(summary, reply_markup=done_kb)
        except Exception:
            await safe_send(bot, admin_id, summary, done_kb)
    except Exception:
        log.exception('Background broadcast task failed for admin %s', admin_id)
    finally:
        current = _ACTIVE_BROADCASTS.get(admin_id)
        if current and current[1] is stop_event:
            _ACTIVE_BROADCASTS.pop(admin_id, None)


@router.callback_query(F.data == 'adm:broadcast')
async def broadcast_start(call: CallbackQuery, state: FSMContext, db: Database):
    _clear_broadcast_albums(call.from_user.id)
    await state.clear()
    await call.answer()
    rows = []
    for key, (label, where) in BROADCAST_SEGMENTS.items():
        count = (await db.read(f'SELECT COUNT(*) AS n FROM users u WHERE {where}'))['n']
        rows.append((cb(f'{label} · {count}', f'adm:bcast:seg:{key}', style='primary' if key in ('cold', 'buyers') else None),))
    rows.append((cb('◀️ Назад в меню', 'adm:home'),))
    await screen(
        call,
        '<b>📢 Новая рассылка · выбор аудитории</b>\n\nВыберите сегмент получателей:',
        reply_markup=kb(*rows),
    )


@router.callback_query(F.data.regexp(r'^adm:bcast:seg:(all|cold|buyers|entry|whales)$'))
async def broadcast_choose_segment(call: CallbackQuery, state: FSMContext, db: Database):
    _clear_broadcast_albums(call.from_user.id)
    seg = call.data.split(':')[3]
    label, where = BROADCAST_SEGMENTS[seg]
    count = (await db.read(f'SELECT COUNT(*) AS n FROM users u WHERE {where}'))['n']
    await state.set_state(AdminState.broadcast)
    await state.update_data(broadcast_segment=seg)
    await call.answer()
    await screen(
        call,
        f'<b>📢 Рассылка · {label}</b>\nПолучателей в сегменте: <b>{count}</b>\n\n'
        'Пришлите готовый пост: текст, фото, видео, документ или альбом из нескольких медиафайлов с подписью. Сначала вы увидите предпросмотр.',
        reply_markup=form_nav('◀️ Выбрать другой сегмент', 'adm:broadcast'),
    )


async def _flush_broadcast_album(
    key: tuple[int, str], chat_id: int, seg: str, label: str, count: int,
    state: FSMContext, bot: Bot, anchor_message: Message,
) -> None:
    """Wait briefly for all album items to arrive, then render preview & confirmation."""
    entry = _BROADCAST_ALBUMS[key]
    try:
        loop = asyncio.get_running_loop()
        while True:
            remaining = _BROADCAST_ALBUM_IDLE_SEC - (loop.time() - entry['last_seen'])
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            if await state.get_state() != AdminState.broadcast.state:
                return
            msg_ids = sorted(entry['msg_ids'])
            try:
                preview_ids = await _copy_broadcast_post(bot, chat_id, chat_id, msg_ids)
            except (TelegramAPIError, IncompleteBroadcastAlbum):
                await anchor_message.answer('Telegram не может скопировать этот альбом или тип медиа.')
                return
            if await state.get_state() != AdminState.broadcast.state:
                return
            if len(msg_ids) != len(entry['msg_ids']):
                continue
            kind = f'альбом ({len(preview_ids)} медиа)' if len(preview_ids) > 1 else 'сообщение'
            confirmation = await anchor_message.answer(
                f'📢 Сегмент: <b>{label}</b> (получателей: <b>{count}</b>). Отправить {kind} выше?',
                reply_markup=kb((cb('✅ Подтвердить и отправить', 'adm:sendbroadcast', style='success'),),
                                (cb('❌ Отмена', 'adm:home'),)),
            )
            if await state.get_state() != AdminState.broadcast.state:
                return
            if len(msg_ids) != len(entry['msg_ids']):
                continue
            await state.update_data(
                broadcast_msg_id=preview_ids[0],
                broadcast_msg_ids=preview_ids,
                broadcast_chat_id=chat_id,
                broadcast_confirmation_id=confirmation.message_id,
                broadcast_segment=seg,
                broadcast_album_id=key[1],
                broadcast_album_sources=msg_ids,
            )
            await state.set_state(AdminState.broadcast_confirm)
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception('Failed to flush broadcast album %s', key)
    finally:
        if _BROADCAST_ALBUMS.get(key) is entry:
            _BROADCAST_ALBUMS.pop(key)


@router.message(AdminState.broadcast_confirm, F.media_group_id)
@router.message(AdminState.broadcast, ~F.text.startswith('/'))
async def broadcast_preview(message: Message, state: FSMContext, db: Database, bot: Bot):
    if message.text and message.text.startswith('/'):
        return await message.answer('Для отмены введите /admin.')
    data = await state.get_data()
    mgid = getattr(message, 'media_group_id', None)
    album_sources = []
    if await state.get_state() == AdminState.broadcast_confirm.state:
        if not mgid or str(mgid) != data.get('broadcast_album_id'):
            return
        album_sources = data['broadcast_album_sources']
        if message.message_id in album_sources:
            return
        await state.update_data(broadcast_confirmation_id=None)
        await state.set_state(AdminState.broadcast)
    seg = data.get('broadcast_segment') or 'all'
    label, where = BROADCAST_SEGMENTS.get(seg, BROADCAST_SEGMENTS['all'])
    count = (await db.read(f'SELECT COUNT(*) AS n FROM users u WHERE {where}'))['n']
    if not count:
        await state.clear()
        return await message.answer('В выбранном сегменте пока нет пользователей.')
    user_id = getattr(getattr(message, 'from_user', None), 'id', message.chat.id)
    if mgid:
        key = (user_id, str(mgid))
        for other in [k for k in _BROADCAST_ALBUMS if k[0] == user_id and k != key]:
            stale = _BROADCAST_ALBUMS.pop(other, None)
            if stale and stale.get('task') and not stale['task'].done():
                stale['task'].cancel()
        entry = _BROADCAST_ALBUMS.get(key)
        now_t = asyncio.get_running_loop().time()
        if entry is None:
            entry = {'msg_ids': list(album_sources), 'last_seen': now_t, 'task': None}
            _BROADCAST_ALBUMS[key] = entry
        if message.message_id not in entry['msg_ids']:
            entry['msg_ids'].append(message.message_id)
        entry['last_seen'] = now_t
        if entry['task'] is None or entry['task'].done():
            entry['task'] = asyncio.create_task(
                _flush_broadcast_album(key, message.chat.id, seg, label, count, state, bot, message)
            )
        return
    _clear_broadcast_albums(user_id)
    try:
        preview = await bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    except TelegramAPIError:
        return await message.answer('Telegram не может скопировать этот тип сообщения.')
    confirmation = await message.answer(
        f'📢 Сегмент: <b>{label}</b> (получателей: <b>{count}</b>). Отправить сообщение выше?',
        reply_markup=kb((cb('✅ Подтвердить и отправить', 'adm:sendbroadcast', style='success'),),
                        (cb('❌ Отмена', 'adm:home'),)),
    )
    await state.update_data(
        broadcast_msg_id=preview.message_id,
        broadcast_msg_ids=[preview.message_id],
        broadcast_chat_id=message.chat.id,
        broadcast_confirmation_id=confirmation.message_id,
        broadcast_segment=seg,
    )
    await state.set_state(AdminState.broadcast_confirm)


@router.callback_query(F.data == 'adm:bcast:stop')
async def broadcast_stop(call: CallbackQuery):
    active = _ACTIVE_BROADCASTS.get(call.from_user.id)
    if not active or active[0].done():
        return await call.answer('Активной рассылки сейчас нет.', show_alert=True)
    active[1].set()
    await call.answer('Останавливаем рассылку после текущего сообщения…')


@router.callback_query(F.data == 'adm:sendbroadcast', AdminState.broadcast_confirm)
async def broadcast_send(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    data = await state.get_data()
    if call.message.message_id != data.get('broadcast_confirmation_id'):
        return await call.answer('Откройте последнее подтверждение.', show_alert=True)
    existing = _ACTIVE_BROADCASTS.get(call.from_user.id)
    if existing and not existing[0].done():
        return await call.answer('Дождитесь окончания текущей рассылки или остановите её.', show_alert=True)
    seg = data.get('broadcast_segment') or 'all'
    _, where = BROADCAST_SEGMENTS.get(seg, BROADCAST_SEGMENTS['all'])
    await state.clear()
    await call.answer('Рассылка запущена в фоне.')
    users = await db.read(f'SELECT u.user_id FROM users u WHERE {where} ORDER BY u.user_id', many=True)
    progress = await call.message.answer(
        f'⏳ Рассылка выполняется (получателей: <b>{len(users)}</b>)…',
        reply_markup=kb((cb('🛑 Остановить рассылку', 'adm:bcast:stop', style='danger'),)),
    )
    stop_event = asyncio.Event()
    msg_ids = data.get('broadcast_msg_ids') or data['broadcast_msg_id']
    task = asyncio.create_task(_run_broadcast(
        bot, db, call.from_user.id, data['broadcast_chat_id'], msg_ids,
        seg, users, progress, stop_event,
    ))
    _ACTIVE_BROADCASTS[call.from_user.id] = (task, stop_event)


# --- Automated & Manual Promo / Review Push Campaigns ---

_PROMO_DELAY_PRESETS = (5, 15, 30, 60, 120, 360, 1440)
_REVIEW_DELAY_PRESETS = (15, 30, 60, 120, 360, 1440)
_REVIEW_INTERVAL_PRESETS = (0, 6, 12, 24, 48, 72)
_REVIEW_MAX_PRESETS = (1, 2, 3, 5, 10, 0)


def _fmt_delay_minutes(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        return f'{minutes // 60} ч.'
    return f'{minutes} мин.'


def _short_preview(text: str, limit: int = 160) -> str:
    flat = ' '.join((text or '').split())
    return flat if len(flat) <= limit else flat[:limit - 1] + '…'


async def _render_promo_push_screen(target: Message | CallbackQuery, db: Database):
    cfg = await push_settings(db)
    enabled = cfg.get('promo_push_enabled') == '1'
    mode = cfg.get('promo_push_mode') or 'shared'
    code = (cfg.get('promo_push_code') or '').strip().upper()
    prefix = (cfg.get('promo_push_prefix') or 'VIP').strip().upper()
    disc_rub, disc_pct = parse_push_discount(cfg.get('promo_push_discount'))
    disc_label = promo_discount_label({'discount': disc_rub, 'discount_percent': disc_pct})
    ttl_hours = int_setting(cfg, 'promo_push_ttl_hours', 0, minimum=0)
    delay_min = int_setting(cfg, 'promo_push_delay_min', 30, minimum=1)

    stats = await db.read(
        f"""SELECT
            (SELECT COUNT(*) FROM users WHERE promo_push_sent=1 AND promo_push_code!='') AS sent_cnt,
            (SELECT COUNT(DISTINCT u.user_id) FROM users u
             JOIN orders o ON o.user_id=u.user_id
             WHERE u.promo_push_sent=1 AND u.promo_push_code!='' AND o.status='paid' AND o.provider!='admin'
               AND o.refund_requested_at IS NULL
               AND o.promo_code=u.promo_push_code AND o.paid_at>=u.promo_push_sent_at) AS conv_cnt,
            (SELECT COUNT(*) FROM users u WHERE {PROMO_PUSH_ELIGIBLE_SQL}) AS cold_cnt""",
        {'timestamp': now()},
    )
    sent_cnt = stats['sent_cnt']
    conv_cnt = stats['conv_cnt']
    conv_pct = f'{(conv_cnt * 100 / sent_cnt):.1f}%' if sent_cnt else '0%'

    warning = ''
    promo_row = None
    if mode == 'shared':
        promo_row = await db.read('SELECT * FROM promos WHERE code=? AND is_personal=0', (code,)) if code else None
        if not promo_row:
            code_line = f'Код: <b>{esc(code) or "не выбран"}</b> · ❌ не найден'
            warning = '\n⚠️ <b>Выберите или создайте активный промокод, иначе пуши не уйдут.</b>'
        elif not promo_has_discount(promo_row):
            code_line = f"Код: <code>{esc(promo_row['code'])}</code> · скидка <b>0 ₽</b>"
            warning = '\n⚠️ <b>Выбранный промокод не даёт скидку. Выберите другой код.</b>'
        else:
            p_status = '🟢 Активен' if (promo_row['enabled'] and promo_row['used'] < promo_row['max_uses']) else '⚪ Выключен / лимит'
            code_line = (
                f"Код: <code>{esc(promo_row['code'])}</code> · скидка <b>{promo_discount_label(promo_row)}</b> "
                f"({p_status}, {promo_row['used']}/{promo_row['max_uses']})"
            )
            if not promo_row['enabled'] or promo_row['used'] >= promo_row['max_uses']:
                warning = '\n⚠️ <b>Выбранный промокод сейчас выключен или исчерпан.</b>'
        mode_block = f"Режим: <b>🏷 Общий код из админки</b>\n{code_line}"
    else:
        ttl_label = f'{ttl_hours} ч.' if ttl_hours > 0 else 'бессрочно'
        mode_block = (
            f"Режим: <b>🔐 Персональный код (генерируется под юзера)</b>\n"
            f"Префикс: <code>{esc(prefix)}_XXXXXX</code> · Скидка: <b>{disc_label}</b> · Срок: <b>{ttl_label}</b>"
        )

    media_row = await db.setting('promo_push_text')
    has_media = bool(media_row and (media_row.get('media_id') or '').strip())
    media_label = f"🟢 Есть ({media_row.get('media_type', 'photo')})" if has_media else "⚪ Нет"

    text = (
        f"<b>🎁 Авто-пуш промокодом (дожим некупивших)</b>\n\n"
        f"Статус: <b>{'🟢 Включён' if enabled else '⚪ Выключен'}</b>\n"
        f"{mode_block}\n"
        f"Баннер (Luxury Hub): <b>{media_label}</b>\n"
        f"Таймер после /start: <b>{_fmt_delay_minutes(delay_min)}</b> (только если 0 покупок)\n"
        f"Отправлено пушей: <b>{sent_cnt}</b> · Купили с кодом: <b>{conv_cnt}</b> ({conv_pct})\n"
        f"Некупивших без пуша: <b>{stats['cold_cnt']}</b>"
        f"{warning}\n\n"
        f"<i>Текст:</i> <code>{esc(_short_preview(cfg.get('promo_push_text', '')))}</code>"
    )
    rows: list[tuple] = [
        (cb('⏸ Выключить авто-пуш' if enabled else '🟢 Включить авто-пуш',
            'adm:ppush:toggle', style='danger' if enabled else 'success'),),
        (cb(f"Режим: {'🏷 Общий код' if mode == 'shared' else '🔐 Персональный'}", 'adm:ppush:mode'),),
    ]
    if mode == 'shared':
        rows.append((cb('🎫 Выбрать промокод', 'adm:ppush:pick:0'), cb('➕ Новый промокод', 'adm:promo:new')))
        if promo_row and not promo_row['enabled'] and promo_has_discount(promo_row):
            rows.append((cb(f"🟢 Включить код {promo_row['code']}", f"adm:ppush:enablecode:{promo_row['code']}", style='success'),))
    else:
        rows.append((cb(f'🏷 Префикс: {prefix}', 'adm:ppush:edit:prefix'), cb(f'💰 Скидка: {disc_label}', 'adm:ppush:edit:discount')))
        rows.append((cb(f"⏳ Срок: {ttl_hours} ч." if ttl_hours else '⏳ Срок: бессрочно', 'adm:ppush:edit:ttl'),))
    rows.extend((
        (cb(f'⏱ Таймер: {_fmt_delay_minutes(delay_min)}', 'adm:ppush:delaymenu'), cb('✏️ Текст пуша', 'adm:ppush:edit:text')),
        (cb(f"🖼 Баннер ({'есть' if has_media else 'нет'})", 'adm:ppush:media'), cb('💜 Шаблон Luxury Hub', 'adm:ppush:tpl:luxury')),
        (cb('🔄 Шаблон 2026', 'adm:ppush:tpl:2026'), cb('🧪 Тест себе', 'adm:ppush:test')),
        (cb('🚀 Разослать сейчас', 'adm:ppush:blast'), cb('🏠 Главное меню', 'adm:home')),
    ))
    await screen(target, text, reply_markup=kb(*rows))


@router.callback_query(F.data == 'adm:ppush')
async def promo_push_home(call: CallbackQuery, db: Database):
    await call.answer()
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data == 'adm:ppush:toggle')
async def promo_push_toggle(call: CallbackQuery, db: Database):
    cfg = await push_settings(db)
    new_val = '0' if cfg.get('promo_push_enabled') == '1' else '1'
    if new_val == '1' and (cfg.get('promo_push_mode') or 'shared') == 'shared':
        if not await resolve_shared_promo(db, cfg.get('promo_push_code', '')):
            await call.answer('Сначала выберите или включите активный промокод!', show_alert=True)
            return await _render_promo_push_screen(call, db)
    await db.set_setting('promo_push_enabled', new_val)
    await db.audit(call.from_user.id, 'promo_push_setting', f'enabled={new_val}')
    await call.answer('Авто-пуш промокодом включён.' if new_val == '1' else 'Авто-пуш промокодом выключен.')
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data == 'adm:ppush:mode')
async def promo_push_mode_switch(call: CallbackQuery, db: Database):
    cfg = await push_settings(db)
    new_mode = 'personal' if (cfg.get('promo_push_mode') or 'shared') == 'shared' else 'shared'
    await db.set_setting('promo_push_mode', new_mode)
    await db.audit(call.from_user.id, 'promo_push_setting', f'mode={new_mode}')
    await call.answer('Режим переключён.')
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data.regexp(r'^adm:ppush:enablecode:[A-Z0-9_]{1,28}$'))
async def promo_push_enable_code(call: CallbackQuery, db: Database):
    code = call.data.rsplit(':', 1)[1]
    promo = await db.read('SELECT * FROM promos WHERE code=?', (code,))
    if not promo:
        return await call.answer('Промокод не найден.', show_alert=True)
    if not promo_has_discount(promo):
        return await call.answer('Нельзя включить промокод без скидки.', show_alert=True)
    await db.execute('UPDATE promos SET enabled=1 WHERE code=?', (code,))
    await db.audit(call.from_user.id, 'promo_toggle', f'{code}=1')
    await call.answer(f'Промокод {code} включён.')
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data.regexp(r'^adm:ppush:pick:[0-9]{1,6}$'))
async def promo_push_pick_list(call: CallbackQuery, db: Database):
    await call.answer()
    page = int(call.data.rsplit(':', 1)[1])
    items = await db.read(
        """SELECT * FROM promos
           WHERE is_personal=0 AND (discount>0 OR COALESCE(discount_percent,0)>0)
           ORDER BY enabled DESC, code LIMIT ? OFFSET ?""",
        (PAGE_SIZE + 1, page * PAGE_SIZE),
        many=True,
    )
    rows = [
        (cb(f"{'🟢' if p['enabled'] else '⚪'} {p['code']} · -{promo_discount_label(p)} ({p['used']}/{p['max_uses']})",
            f"adm:ppush:setcode:{p['code']}"),)
        for p in items[:PAGE_SIZE]
    ]
    rows.extend((
        pager('adm:ppush:pick', page, len(items) > PAGE_SIZE),
        (cb('◀️ Назад', 'adm:ppush'), cb('🏠 Меню', 'adm:home')),
    ))
    await screen(
        call,
        '<b>🎫 Выбор промокода для авто-пуша</b>\n\nНажмите на код, чтобы привязать его к авто-пушу:',
        reply_markup=kb(*rows),
    )


@router.callback_query(F.data.regexp(r'^adm:ppush:setcode:[A-Z0-9_]{1,28}$'))
async def promo_push_set_code(call: CallbackQuery, db: Database):
    code = call.data.rsplit(':', 1)[1]
    if not await db.read(
        'SELECT 1 FROM promos WHERE code=? AND is_personal=0 AND (discount>0 OR COALESCE(discount_percent,0)>0)',
        (code,),
    ):
        return await call.answer('Промокод не найден или имеет нулевую скидку.', show_alert=True)
    await db.set_setting('promo_push_code', code)
    await db.audit(call.from_user.id, 'promo_push_setting', f'code={code}')
    await call.answer(f'Выбран промокод {code}.')
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data == 'adm:ppush:delaymenu')
async def promo_push_delay_menu(call: CallbackQuery, db: Database):
    await call.answer()
    current = int_setting(await push_settings(db), 'promo_push_delay_min', 30, minimum=1)
    buttons = [
        cb(_fmt_delay_minutes(m), f'adm:ppush:delay:{m}', style='primary' if m == current else None)
        for m in _PROMO_DELAY_PRESETS
    ]
    rows = [tuple(buttons[i:i + 3]) for i in range(0, len(buttons), 3)]
    rows.append((cb('✏️ Своё значение (мин)', 'adm:ppush:edit:delay'),))
    rows.append((cb('◀️ Назад', 'adm:ppush'),))
    await screen(
        call,
        f'<b>⏱ Таймер пуша с промокодом</b>\nТекущее: <b>{_fmt_delay_minutes(current)}</b> после /start\n\nВыберите задержку:',
        reply_markup=kb(*rows),
    )


@router.callback_query(F.data.regexp(r'^adm:ppush:delay:[0-9]{1,6}$'))
async def promo_push_set_delay(call: CallbackQuery, db: Database):
    minutes = max(1, min(43200, int(call.data.rsplit(':', 1)[1])))
    await db.set_setting('promo_push_delay_min', str(minutes))
    await db.audit(call.from_user.id, 'promo_push_setting', f'delay_min={minutes}')
    await call.answer(f'Таймер: {_fmt_delay_minutes(minutes)}.')
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data == 'adm:ppush:resettext')
async def promo_push_reset_text(call: CallbackQuery, db: Database):
    await db.set_setting('promo_push_text', DEFAULT_SETTINGS['promo_push_text'])
    await db.audit(call.from_user.id, 'promo_push_setting', 'reset_text')
    await call.answer('Текст сброшен к стандартному.')
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data.regexp(r'^adm:ppush:edit:(prefix|discount|ttl|delay|text)$'))
async def promo_push_edit_start(call: CallbackQuery, state: FSMContext, db: Database):
    field = call.data.rsplit(':', 1)[1]
    await state.set_state(AdminState.promo_push_field)
    await state.update_data(ppush_field=field)
    await call.answer()
    if field == 'prefix':
        cur = (await db.setting('promo_push_prefix') or {}).get('value') or 'VIP'
        prompt = f'<b>🏷 Префикс персональных кодов</b>\nТекущий: <code>{esc(cur)}</code>\n\nОтправьте латинский префикс (1-12 символов, например <code>VIP</code> или <code>SALE</code>).'
    elif field == 'discount':
        disc_rub, disc_pct = parse_push_discount((await db.setting('promo_push_discount') or {}).get('value'))
        cur_label = promo_discount_label({'discount': disc_rub, 'discount_percent': disc_pct})
        prompt = (
            f'<b>💰 Скидка персонального кода (₽ или %)</b>\nТекущая: <b>{cur_label}</b>\n\n'
            'Введите сумму в ₽ (<code>1</code>–<code>100000</code>, например <code>200</code>) '
            'или процент (<code>1%</code>–<code>99%</code>, например <code>20%</code>).'
        )
    elif field == 'ttl':
        cur = (await db.setting('promo_push_ttl_hours') or {}).get('value') or '0'
        prompt = f'<b>⏳ Срок действия персонального кода (часов)</b>\nТекущий: <b>{esc(cur)}</b> (0 = бессрочно)\n\nВведите число часов от 0 до 720.'
    elif field == 'delay':
        cur = (await db.setting('promo_push_delay_min') or {}).get('value') or '30'
        prompt = f'<b>⏱ Задержка пуша (минуты)</b>\nТекущая: <b>{esc(cur)} мин.</b>\n\nВведите целое число минут от 1 до 43200.'
    else:
        cur = (await db.setting('promo_push_text') or {}).get('value') or DEFAULT_SETTINGS['promo_push_text']
        prompt = (
            '<b>✏️ Текст пуша с промокодом</b>\n\n'
            'Переменные:\n'
            '• <code>{name}</code> — имя пользователя\n'
            '• <code>{project_name}</code> — название проекта\n'
            '• <code>{code}</code> — промокод\n'
            '• <code>{discount}</code> — скидка (например 150 или 20%)\n'
            '• <code>{ttl_note}</code> — срок действия (если задан)\n\n'
            f'Текущий шаблон:\n<pre>{esc(cur[:1500])}</pre>\n\nОтправьте новый текст.'
        )
        return await screen(
            call, prompt,
            reply_markup=kb((cb('🔄 Сбросить текст', 'adm:ppush:resettext'),), (cb('◀️ Отменить', 'adm:ppush'),)),
        )
    await screen(call, prompt, reply_markup=form_nav('◀️ Отменить', 'adm:ppush'))


@router.callback_query(F.data == 'adm:ppush:tpl:luxury')
async def promo_push_tpl_luxury(call: CallbackQuery, db: Database):
    await db.set_setting('promo_push_text', LUXURY_HUB_PROMO_TEXT)
    await db.audit(call.from_user.id, 'promo_push_setting', 'tpl=luxury_hub')
    await call.answer('💜 Шаблон Luxury Hub установлен!', show_alert=True)
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data == 'adm:ppush:tpl:2026')
async def promo_push_tpl_2026(call: CallbackQuery, db: Database):
    await db.set_setting('promo_push_text', DEFAULT_SETTINGS['promo_push_text'])
    await db.audit(call.from_user.id, 'promo_push_setting', 'tpl=2026')
    await call.answer('🔄 Шаблон 2026 установлен!', show_alert=True)
    await _render_promo_push_screen(call, db)


@router.callback_query(F.data == 'adm:ppush:media')
async def promo_push_media_start(call: CallbackQuery, state: FSMContext, db: Database):
    await state.set_state(AdminState.promo_push_field)
    await state.update_data(ppush_field='media')
    cur = await db.setting('promo_push_text')
    has_m = bool(cur and (cur.get('media_id') or '').strip())
    status = f"Текущий баннер: <b>{cur.get('media_type', 'photo')}</b>" if has_m else "Сейчас баннер не прикреплён."
    prompt = (
        f"<b>🖼 Баннер для пуша со скидкой (Luxury Hub стиль)</b>\n\n"
        f"{status}\n\n"
        "Отправьте фото, видео или GIF для баннера.\n"
        "Чтобы убрать баннер и отправлять только текст, отправьте слово <code>удалить</code>."
    )
    await call.answer()
    await screen(call, prompt, reply_markup=form_nav('◀️ Отменить', 'adm:ppush'))


@router.message(AdminState.promo_push_field, ~F.text.startswith('/'))
async def promo_push_edit_save(message: Message, state: FSMContext, db: Database):
    field = (await state.get_data()).get('ppush_field')
    raw = (message.text or '').strip()
    try:
        if field == 'media':
            if raw.lower() in ('удалить', 'delete', 'нет', 'none'):
                await db.set_setting('promo_push_text', media_id='', media_type='')
                await db.audit(message.from_user.id, 'promo_push_setting', 'media=deleted')
            else:
                extracted = _extract_media_upload(message)
                if extracted is None:
                    return await message.answer(
                        '⚠️ Отправьте фото, видео, GIF или слово <code>удалить</code>.',
                        reply_markup=form_nav('◀️ Отменить', 'adm:ppush'),
                    )
                mid, kind = extracted
                await db.set_setting('promo_push_text', media_id=mid, media_type=kind)
                await db.audit(message.from_user.id, 'promo_push_setting', f'media={kind}')
        elif field == 'prefix':
            clean = raw.upper()
            if not re.fullmatch(r'[A-Z0-9_]{1,12}', clean):
                raise ValueError('Используйте 1-12 символов A-Z, 0-9 или _.')
            await db.set_setting('promo_push_prefix', clean)
            await db.audit(message.from_user.id, 'promo_push_setting', f'prefix={clean}')
        elif field == 'discount':
            disc_rub, disc_pct = parse_push_discount(raw, strict=True)
            stored = f'{disc_pct}%' if disc_pct > 0 else str(disc_rub)
            await db.set_setting('promo_push_discount', stored)
            await db.audit(message.from_user.id, 'promo_push_setting', f'discount={stored}')
        elif field == 'ttl':
            val = int(raw)
            if not 0 <= val <= 720:
                raise ValueError('Укажите от 0 до 720 часов.')
            await db.set_setting('promo_push_ttl_hours', str(val))
            await db.audit(message.from_user.id, 'promo_push_setting', f'ttl_hours={val}')
        elif field == 'delay':
            val = int(raw)
            if not 1 <= val <= 43_200:
                raise ValueError('Укажите от 1 до 43200 минут.')
            await db.set_setting('promo_push_delay_min', str(val))
            await db.audit(message.from_user.id, 'promo_push_setting', f'delay_min={val}')
        elif field == 'text':
            html_body = _extract_section_html(message)
            validate_section_html(html_body)
            await db.set_setting('promo_push_text', html_body)
            await db.audit(message.from_user.id, 'promo_push_setting', 'text')
        else:
            await state.clear()
            return
    except ValueError as exc:
        return await message.answer(f'Ошибка: {esc(exc)}\nПопробуйте ещё раз или /admin.')
    await state.clear()
    await message.answer('✅ Настройка пуша сохранена.', reply_markup=form_nav('🎁 К авто-пушу', 'adm:ppush'))


@router.callback_query(F.data == 'adm:ppush:test')
async def promo_push_test(call: CallbackQuery, db: Database):
    user = {'user_id': call.from_user.id, 'name': call.from_user.full_name}
    payload = await prepare_promo_push(db, user, dry_run=True)
    if not payload:
        return await call.answer('Выбранный промокод неактивен или исчерпан. Включите его или выберите другой.', show_alert=True)
    text, markup, _, _ = payload
    media_row = await db.setting('promo_push_text')
    mid = (media_row or {}).get('media_id') or ''
    kind = (media_row or {}).get('media_type') or 'photo'
    if mid:
        sent = await safe_send_media(call.bot, call.from_user.id, mid, kind, text, markup)
    else:
        sent = await safe_send_push_text(call.bot, call.from_user.id, text, markup)
    if sent:
        return await call.answer('Тестовый пуш отправлен.')
    await call.answer('Не удалось отправить ЛС клиенту (бот заблокирован или недоступен).', show_alert=True)


async def _run_promo_blast(bot: Bot, db: Database, admin_id: int, users: list[dict],
                           progress: Message, stop_event: asyncio.Event):
    success = fail = skipped = 0
    total = len(users)
    try:
        cfg = await push_settings(db)
        for index, user in enumerate(users, 1):
            if stop_event.is_set():
                break
            result = await send_promo_push_to_user(bot, db, user, cfg, retry_flood=True)
            if result == 'sent':
                success += 1
            elif result == 'skipped':
                skipped += 1
            else:
                fail += 1
                if result == 'flooded':
                    skipped += total - index
                    stop_event.set()
                    break
            if index % 25 == 0 and index < total and not stop_event.is_set():
                try:
                    await progress.edit_text(
                        f'⏳ Промо-пуш: <b>{index}/{total}</b>\n'
                        f'Доставлено <b>{success}</b> · Ошибок <b>{fail}</b>',
                        reply_markup=kb((cb('🛑 Остановить', 'adm:bcast:stop', style='danger'),)),
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.07)
        stopped = stop_event.is_set()
        await db.audit(admin_id, 'promo_push_blast', f'{success}/{total}')
        title = '🛑 <b>Промо-рассылка остановлена</b>' if stopped else '✅ <b>Промо-рассылка завершена</b>'
        summary = f'{title}\n\nДоставлено <b>{success}</b>\nНе доставлено <b>{fail}</b>\nПропущено <b>{skipped}</b>'
        done_kb = form_nav('🎁 К авто-пушу', 'adm:ppush')
        try:
            await progress.edit_text(summary, reply_markup=done_kb)
        except Exception:
            await safe_send(bot, admin_id, summary, done_kb)
    except Exception:
        log.exception('Promo blast failed for admin %s', admin_id)
    finally:
        current = _ACTIVE_BROADCASTS.get(admin_id)
        if current and current[1] is stop_event:
            _ACTIVE_BROADCASTS.pop(admin_id, None)


@router.callback_query(F.data == 'adm:ppush:blast')
async def promo_push_blast_confirm(call: CallbackQuery, db: Database):
    cfg = await push_settings(db)
    if (cfg.get('promo_push_mode') or 'shared') == 'shared':
        if not await resolve_shared_promo(db, cfg.get('promo_push_code', '')):
            return await call.answer('Сначала выберите и включите активный промокод!', show_alert=True)
    count = (await db.read(
        f'SELECT COUNT(*) AS n FROM users u WHERE {PROMO_PUSH_ELIGIBLE_SQL}', {'timestamp': now()},
    ))['n']
    if not count:
        return await call.answer('Нет некупивших пользователей, которые ещё не получали промо-пуш.', show_alert=True)
    await call.answer()
    await screen(
        call,
        f'<b>🚀 Ручная отправка промо-пуша</b>\n\n'
        f'Будет отправлено некупившим пользователям без пуша: <b>{count}</b> чел.\n'
        'Подтвердить запуск?',
        reply_markup=kb(
            (cb('✅ Запустить сейчас', 'adm:ppush:blastgo', style='success'),),
            (cb('◀️ Назад', 'adm:ppush'),),
        ),
    )


@router.callback_query(F.data == 'adm:ppush:blastgo')
async def promo_push_blast_go(call: CallbackQuery, db: Database, bot: Bot):
    existing = _ACTIVE_BROADCASTS.get(call.from_user.id)
    if existing and not existing[0].done():
        return await call.answer('Дождитесь окончания текущей рассылки или остановите её.', show_alert=True)
    users = await db.read(
        f'SELECT u.* FROM users u WHERE {PROMO_PUSH_ELIGIBLE_SQL} ORDER BY u.user_id',
        {'timestamp': now()}, many=True,
    )
    if not users:
        return await call.answer('Список получателей пуст.', show_alert=True)
    await call.answer('Запущено в фоне.')
    progress = await call.message.answer(
        f'⏳ Промо-пуш выполняется (получателей: <b>{len(users)}</b>)…',
        reply_markup=kb((cb('🛑 Остановить', 'adm:bcast:stop', style='danger'),)),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(_run_promo_blast(bot, db, call.from_user.id, users, progress, stop_event))
    _ACTIVE_BROADCASTS[call.from_user.id] = (task, stop_event)


# --- Review Screenshots & Review Push Campaigns ---

def _rpush_interval_label(hours: int) -> str:
    return 'только 1 раз' if hours == 0 else f'через {hours} ч.'


def _rpush_max_label(count: int) -> str:
    return 'все скрины' if count == 0 else f'до {count} шт.'


async def _render_review_push_screen(target: Message | CallbackQuery, db: Database):
    cfg = await push_settings(db)
    enabled = cfg.get('review_push_enabled') == '1'
    audience = cfg.get('review_push_audience') or 'cold'
    if audience not in AUDIENCE_LABELS:
        audience = 'cold'
    delay_min = int_setting(cfg, 'review_push_delay_min', 120, minimum=1)
    interval_hours = int_setting(cfg, 'review_push_interval_hours', 24, minimum=0)
    max_per_user = int_setting(cfg, 'review_push_max_per_user', 3, minimum=0)
    order_mode = cfg.get('review_push_order') or 'seq'
    reviews_url = (cfg.get('reviews_url') or '').strip()

    counts = await db.read(
        """SELECT
            (SELECT COUNT(*) FROM reviews WHERE active=1) AS active_cnt,
            (SELECT COUNT(*) FROM reviews) AS total_cnt,
            (SELECT COUNT(*) FROM user_review_pushes) AS sent_total,
            (SELECT COUNT(DISTINCT user_id) FROM user_review_pushes) AS sent_users"""
    )
    warnings = []
    if cfg.get('reviews_visible') != '1' and enabled:
        warnings.append('⚠️ <b>Показ отзывов скрыт в настройках — авто-пуши сейчас на паузе.</b>')
    if not counts['active_cnt']:
        warnings.append('⚠️ <b>Загрузите хотя бы 1 скриншот отзыва, чтобы пуши уходили.</b>')
    warning = ('\n' + '\n'.join(warnings)) if warnings else ''
    text = (
        f"<b>⭐ Отзывы и пуши со скриншотами</b>\n\n"
        f"Показ покупателям: <b>{'включён' if cfg.get('reviews_visible') == '1' else 'скрыт в настройках'}</b>\n"
        f"Авто-пуши: <b>{'🟢 Включены' if enabled else '⚪ Выключены'}</b>\n"
        f"Скриншотов в базе: <b>{counts['active_cnt']}</b> активных (всего {counts['total_cnt']})\n"
        f"Аудитория: <b>{AUDIENCE_LABELS[audience]}</b>\n"
        f"Первый пуш: <b>{_fmt_delay_minutes(delay_min)}</b> · Повтор: <b>{_rpush_interval_label(interval_hours)}</b>\n"
        f"Лимит на юзера: <b>{_rpush_max_label(max_per_user)}</b> · Порядок: <b>{'По очереди' if order_mode == 'seq' else 'Случайный'}</b>\n"
        f"Канал отзывов: <b>{'настроен' if is_https_url(reviews_url) else 'не указан (показ в боте)'}</b>\n"
        f"Отправлено пушей: <b>{counts['sent_total']}</b> (юзеров: <b>{counts['sent_users']}</b>)"
        f"{warning}\n\n"
        f"<i>Текст по умолч.:</i> <code>{esc(_short_preview(cfg.get('review_push_text', '')))}</code>"
    )
    await screen(target, text, reply_markup=kb(
        (cb('⏸ Выключить авто-пуши' if enabled else '🟢 Включить авто-пуши',
            'adm:rpush:toggle', style='danger' if enabled else 'success'),),
        (cb(f"🖼 Скриншоты ({counts['active_cnt']}/{counts['total_cnt']})", 'adm:rpush:list:0', style='primary'),
         cb('➕ Загрузить скрины', 'adm:rpush:add')),
        (cb(f'👥 {AUDIENCE_LABELS[audience]}', 'adm:rpush:aud'),
         cb(f"🔀 {'По очереди' if order_mode == 'seq' else 'Случайно'}", 'adm:rpush:order')),
        (cb(f'⏱ Первый: {_fmt_delay_minutes(delay_min)}', 'adm:rpush:delaymenu'),
         cb(f'🔁 {_rpush_interval_label(interval_hours)}', 'adm:rpush:intmenu')),
        (cb(f'📊 Лимит: {_rpush_max_label(max_per_user)}', 'adm:rpush:maxmenu'),
         cb('✏️ Общий текст', 'adm:rpush:edit:text')),
        (cb('⭐ Ссылка на канал отзывов', 'adm:setting:reviews_url'),),
        (cb('🧪 Тест себе', 'adm:rpush:test'), cb('🚀 Разослать сейчас', 'adm:rpush:blast:0')),
        (cb('🏠 Главное меню', 'adm:home'),),
    ))


@router.callback_query(F.data == 'adm:rpush')
async def review_push_home(call: CallbackQuery, db: Database):
    await call.answer()
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:toggle')
async def review_push_toggle(call: CallbackQuery, db: Database):
    cur = (await db.setting('review_push_enabled') or {}).get('value') == '1'
    new_val = '0' if cur else '1'
    if new_val == '1' and not await db.read('SELECT 1 FROM reviews WHERE active=1 LIMIT 1'):
        await call.answer('Сначала загрузите хотя бы один активный скриншот отзыва!', show_alert=True)
        return await _render_review_push_screen(call, db)
    await db.set_setting('review_push_enabled', new_val)
    await db.audit(call.from_user.id, 'review_push_setting', f'enabled={new_val}')
    await call.answer('Авто-пуши отзывов включены.' if new_val == '1' else 'Авто-пуши отзывов выключены.')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:aud')
async def review_push_cycle_audience(call: CallbackQuery, db: Database):
    order = ('cold', 'buyers', 'all')
    cur = (await db.setting('review_push_audience') or {}).get('value') or 'cold'
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else 'cold'
    await db.set_setting('review_push_audience', nxt)
    await db.audit(call.from_user.id, 'review_push_setting', f'audience={nxt}')
    await call.answer(f'Аудитория: {AUDIENCE_LABELS[nxt]}')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:order')
async def review_push_cycle_order(call: CallbackQuery, db: Database):
    cur = (await db.setting('review_push_order') or {}).get('value') or 'seq'
    nxt = 'random' if cur == 'seq' else 'seq'
    await db.set_setting('review_push_order', nxt)
    await db.audit(call.from_user.id, 'review_push_setting', f'order={nxt}')
    await call.answer('Порядок: Случайный' if nxt == 'random' else 'Порядок: По очереди')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:delaymenu')
async def review_push_delay_menu(call: CallbackQuery, db: Database):
    await call.answer()
    current = int_setting(await push_settings(db), 'review_push_delay_min', 120, minimum=1)
    buttons = [
        cb(_fmt_delay_minutes(m), f'adm:rpush:delay:{m}', style='primary' if m == current else None)
        for m in _REVIEW_DELAY_PRESETS
    ]
    rows = [tuple(buttons[i:i + 3]) for i in range(0, len(buttons), 3)]
    rows.append((cb('✏️ Своё значение (мин)', 'adm:rpush:edit:delay'),))
    rows.append((cb('◀️ Назад', 'adm:rpush'),))
    await screen(call, f'<b>⏱ Задержка первого отзыва</b>\nТекущая: <b>{_fmt_delay_minutes(current)}</b>', reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:rpush:delay:[0-9]{1,6}$'))
async def review_push_set_delay(call: CallbackQuery, db: Database):
    minutes = max(1, min(43200, int(call.data.rsplit(':', 1)[1])))
    await db.set_setting('review_push_delay_min', str(minutes))
    await db.audit(call.from_user.id, 'review_push_setting', f'delay_min={minutes}')
    await call.answer(f'Первый пуш через {_fmt_delay_minutes(minutes)}.')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:intmenu')
async def review_push_interval_menu(call: CallbackQuery, db: Database):
    await call.answer()
    current = int_setting(await push_settings(db), 'review_push_interval_hours', 24)
    buttons = [
        cb(_rpush_interval_label(h), f'adm:rpush:int:{h}', style='primary' if h == current else None)
        for h in _REVIEW_INTERVAL_PRESETS
    ]
    rows = [tuple(buttons[i:i + 3]) for i in range(0, len(buttons), 3)]
    rows.append((cb('✏️ Своё значение (часы)', 'adm:rpush:edit:interval'),))
    rows.append((cb('◀️ Назад', 'adm:rpush'),))
    await screen(call, f'<b>🔁 Интервал следующих отзывов</b>\nТекущий: <b>{_rpush_interval_label(current)}</b>', reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:rpush:int:[0-9]{1,5}$'))
async def review_push_set_interval(call: CallbackQuery, db: Database):
    hours = max(0, min(720, int(call.data.rsplit(':', 1)[1])))
    await db.set_setting('review_push_interval_hours', str(hours))
    await db.audit(call.from_user.id, 'review_push_setting', f'interval_hours={hours}')
    await call.answer(f'Повтор: {_rpush_interval_label(hours)}.')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:maxmenu')
async def review_push_max_menu(call: CallbackQuery, db: Database):
    await call.answer()
    current = int_setting(await push_settings(db), 'review_push_max_per_user', 3)
    buttons = [
        cb(_rpush_max_label(c), f'adm:rpush:max:{c}', style='primary' if c == current else None)
        for c in _REVIEW_MAX_PRESETS
    ]
    rows = [tuple(buttons[i:i + 3]) for i in range(0, len(buttons), 3)]
    rows.append((cb('✏️ Свой лимит', 'adm:rpush:edit:max'),))
    rows.append((cb('◀️ Назад', 'adm:rpush'),))
    await screen(call, f'<b>📊 Лимит пушей отзывов на 1 пользователя</b>\nТекущий: <b>{_rpush_max_label(current)}</b>', reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^adm:rpush:max:[0-9]{1,5}$'))
async def review_push_set_max(call: CallbackQuery, db: Database):
    count = max(0, min(100, int(call.data.rsplit(':', 1)[1])))
    await db.set_setting('review_push_max_per_user', str(count))
    await db.audit(call.from_user.id, 'review_push_setting', f'max_per_user={count}')
    await call.answer(f'Лимит: {_rpush_max_label(count)}.')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data == 'adm:rpush:resettext')
async def review_push_reset_text(call: CallbackQuery, db: Database):
    await db.set_setting('review_push_text', DEFAULT_SETTINGS['review_push_text'])
    await db.audit(call.from_user.id, 'review_push_setting', 'reset_text')
    await call.answer('Текст сброшен к стандартному.')
    await _render_review_push_screen(call, db)


@router.callback_query(F.data.regexp(r'^adm:rpush:edit:(delay|interval|max|text)$'))
async def review_push_edit_start(call: CallbackQuery, state: FSMContext, db: Database):
    field = call.data.rsplit(':', 1)[1]
    await state.set_state(AdminState.review_push_field)
    await state.update_data(rpush_field=field)
    await call.answer()
    if field == 'delay':
        prompt = '<b>⏱ Задержка первого отзыва (минуты)</b>\n\nВведите целое число минут от 1 до 43200.'
    elif field == 'interval':
        prompt = '<b>🔁 Интервал повторных отзывов (часы)</b>\n\nВведите число часов от 0 до 720 (0 = только 1 отзыв).'
    elif field == 'max':
        prompt = '<b>📊 Лимит отзывов на 1 пользователя</b>\n\nВведите число от 0 до 100 (0 = пока есть новые скриншоты).'
    else:
        cur = (await db.setting('review_push_text') or {}).get('value') or DEFAULT_SETTINGS['review_push_text']
        return await screen(
            call,
            '<b>✏️ Общий текст для пушей с отзывами</b>\n'
            'Используется, если у скриншота нет своей подписи.\n'
            'Переменные: <code>{name}</code>, <code>{project_name}</code>\n\n'
            f'Текущий:\n<pre>{esc(cur[:1500])}</pre>\n\nОтправьте новый текст.',
            reply_markup=kb((cb('🔄 Сбросить текст', 'adm:rpush:resettext'),), (cb('◀️ Отменить', 'adm:rpush'),)),
        )
    await screen(call, prompt, reply_markup=form_nav('◀️ Отменить', 'adm:rpush'))


@router.message(AdminState.review_push_field, ~F.text.startswith('/'))
async def review_push_edit_save(message: Message, state: FSMContext, db: Database):
    field = (await state.get_data()).get('rpush_field')
    raw = (message.text or '').strip()
    try:
        if field == 'delay':
            val = int(raw)
            if not 1 <= val <= 43_200:
                raise ValueError('Укажите от 1 до 43200 минут.')
            await db.set_setting('review_push_delay_min', str(val))
            await db.audit(message.from_user.id, 'review_push_setting', f'delay_min={val}')
        elif field == 'interval':
            val = int(raw)
            if not 0 <= val <= 720:
                raise ValueError('Укажите от 0 до 720 часов.')
            await db.set_setting('review_push_interval_hours', str(val))
            await db.audit(message.from_user.id, 'review_push_setting', f'interval_hours={val}')
        elif field == 'max':
            val = int(raw)
            if not 0 <= val <= 100:
                raise ValueError('Укажите от 0 до 100.')
            await db.set_setting('review_push_max_per_user', str(val))
            await db.audit(message.from_user.id, 'review_push_setting', f'max_per_user={val}')
        elif field == 'text':
            html_body = _extract_section_html(message)
            validate_section_html(html_body)
            await db.set_setting('review_push_text', html_body)
            await db.audit(message.from_user.id, 'review_push_setting', 'text')
        else:
            await state.clear()
            return
    except ValueError as exc:
        return await message.answer(f'Ошибка: {esc(exc)}\nПопробуйте ещё раз или /admin.')
    await state.clear()
    await message.answer('✅ Настройка пушей отзывов сохранена.', reply_markup=form_nav('⭐ К отзывам', 'adm:rpush'))


@router.callback_query(F.data == 'adm:rpush:add')
async def review_add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.review_add)
    await call.answer()
    await screen(
        call,
        '<b>📸 Загрузка скриншотов отзывов</b>\n\n'
        'Пришлите одно или несколько фото (скриншоты), видео или GIF — можно с подписью или без неё.\n'
        'Когда закончите загрузку, нажмите «✅ Готово».',
        reply_markup=kb(
            (cb('✅ Готово (к списку)', 'adm:rpush:list:0', style='success'),),
            (cb('◀️ Назад', 'adm:rpush'),),
        ),
    )


@router.message(AdminState.review_add, ~F.text.startswith('/'))
async def review_add_receive(message: Message, db: Database):
    media = _extract_media_upload(message)
    if not media or not media[0]:
        return await message.answer('Пришлите фото, видео или GIF (либо нажмите «✅ Готово» или /admin).')
    media_id, media_type = media
    caption = _extract_caption_html(message) if message.caption else ''
    if caption:
        try:
            validate_section_html(caption)
        except ValueError as exc:
            return await message.answer(f'Ошибка в подписи: {esc(exc)}')
    def txn(con):
        cur = con.execute(
            'INSERT INTO reviews(media_id, media_type, caption, active, created_at) VALUES(?,?,?,1,?)',
            (media_id, media_type, caption, now()),
        )
        total = con.execute('SELECT COUNT(*) FROM reviews WHERE active=1').fetchone()[0]
        return cur.lastrowid, total
    review_id, total_active = await db.atomic(txn)
    await db.audit(message.from_user.id, 'review_add', str(review_id))
    len_note = ('\n⚠️ Подпись длиннее 950 символов — отправится отдельным сообщением под скриншотом.'
                if _utf16_len(caption) > 950 else '')
    await message.answer(
        f'✅ Скриншот <b>#{review_id}</b> добавлен (активных в базе: <b>{total_active}</b>).{len_note}\n'
        'Отправьте следующий скриншот или нажмите «✅ Готово».',
        reply_markup=kb((cb('✅ Готово (к списку)', 'adm:rpush:list:0', style='success'),)),
    )


async def _render_review_list(target: Message | CallbackQuery, db: Database, page: int = 0):
    items = await db.read(
        'SELECT * FROM reviews ORDER BY active DESC, sort_order ASC, id ASC LIMIT ? OFFSET ?',
        (PAGE_SIZE + 1, page * PAGE_SIZE),
        many=True,
    )
    rows = [
        (cb(
            f"{'🟢' if r['active'] else '⚪'} #{r['id']} · {'свой текст' if r['caption'].strip() else 'общий текст'}",
            f"adm:rpush:card:{r['id']}",
        ),)
        for r in items[:PAGE_SIZE]
    ]
    rows.extend((
        pager('adm:rpush:list', page, len(items) > PAGE_SIZE),
        (cb('➕ Загрузить скрины', 'adm:rpush:add', style='success'), cb('◀️ Настройки пушей', 'adm:rpush')),
        (cb('🏠 Главное меню', 'adm:home'),),
    ))
    await screen(
        target,
        f'<b>🖼 Скриншоты отзывов · стр. {page + 1}</b>\n'
        + ('Выберите скриншот для предпросмотра или настройки:' if items else '\nПока не загружено ни одного скриншота.'),
        reply_markup=kb(*rows),
    )


@router.callback_query(F.data.regexp(r'^adm:rpush:list:[0-9]{1,6}$'))
async def review_list(call: CallbackQuery, db: Database):
    await call.answer()
    await _render_review_list(call, db, int(call.data.rsplit(':', 1)[1]))


async def _render_review_card(target: Message | CallbackQuery, db: Database, review_id: int, *, send_preview: bool = True):
    review = await db.read('SELECT * FROM reviews WHERE id=?', (review_id,))
    if not review:
        if isinstance(target, CallbackQuery):
            return await target.answer('Скриншот не найден.', show_alert=True)
        return await target.answer('Скриншот не найден.')
    sent_cnt = (await db.read('SELECT COUNT(*) AS n FROM user_review_pushes WHERE review_id=?', (review_id,)))['n']
    status = '🟢 Активен' if review['active'] else '⚪ На паузе'
    cap_raw = review['caption'].strip()
    if cap_raw:
        preview_cap = cap_raw if len(cap_raw) <= 600 else cap_raw[:600] + '…'
        cap_desc = f'<pre>{esc(preview_cap)}</pre>'
    else:
        cap_desc = '<i>используется общий текст пушей</i>'
    text = (
        f"<b>⭐ Скриншот отзыва #{review['id']}</b>\n\n"
        f"Статус: <b>{status}</b> · Тип: <b>{review['media_type']}</b>\n"
        f"Отправок по базе: <b>{sent_cnt}</b>\n"
        f"Подпись:\n{cap_desc}"
    )
    markup = kb(
        (cb('✏️ Изменить подпись', f"adm:rpush:rcap:{review['id']}"),
         cb('⏸ На паузу' if review['active'] else '▶️ Включить', f"adm:rpush:rtog:{review['id']}")),
        (cb('🚀 Разослать этот скрин', f"adm:rpush:blast:{review['id']}", style='primary'),),
        (cb('🗑 Удалить', f"adm:rpush:rdel:{review['id']}", style='danger'), cb('◀️ К списку', 'adm:rpush:list:0')),
    )
    if send_preview:
        bot = target.bot
        chat_id = target.from_user.id
        await safe_send_media(bot, chat_id, review['media_id'], review['media_type'], '')
        msg = target if isinstance(target, Message) else target.message
        return await msg.answer(text, reply_markup=markup)
    return await screen(target, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r'^adm:rpush:card:[0-9]{1,12}$'))
async def review_card(call: CallbackQuery, db: Database):
    await call.answer()
    await _render_review_card(call, db, int(call.data.rsplit(':', 1)[1]), send_preview=True)


@router.callback_query(F.data.regexp(r'^adm:rpush:rtog:[0-9]{1,12}$'))
async def review_toggle_one(call: CallbackQuery, db: Database):
    rid = int(call.data.rsplit(':', 1)[1])
    review = await db.read('SELECT active FROM reviews WHERE id=?', (rid,))
    if not review:
        return await call.answer('Скриншот не найден.', show_alert=True)
    new_active = int(not review['active'])
    await db.execute('UPDATE reviews SET active=? WHERE id=?', (new_active, rid))
    await db.audit(call.from_user.id, 'review_edit', f'{rid}:active={new_active}')
    await call.answer('Включён.' if new_active else 'Поставлен на паузу.')
    await _render_review_card(call, db, rid, send_preview=False)


@router.callback_query(F.data.regexp(r'^adm:rpush:rcap:[0-9]{1,12}$'))
async def review_caption_start(call: CallbackQuery, state: FSMContext, db: Database):
    rid = int(call.data.rsplit(':', 1)[1])
    if not await db.read('SELECT 1 FROM reviews WHERE id=?', (rid,)):
        return await call.answer('Скриншот не найден.', show_alert=True)
    await state.set_state(AdminState.review_caption)
    await state.update_data(review_id=rid)
    await call.answer()
    await screen(
        call,
        f'<b>✏️ Подпись к отзыву #{rid}</b>\n\n'
        'Отправьте текст подписи (можно с переменными <code>{name}</code>, <code>{project_name}</code>).\n'
        'Чтобы вернуть общий текст пушей, отправьте «-» или «удалить».',
        reply_markup=form_nav('◀️ Отменить', f'adm:rpush:card:{rid}'),
    )


@router.message(AdminState.review_caption, ~F.text.startswith('/'))
async def review_caption_save(message: Message, state: FSMContext, db: Database):
    rid = (await state.get_data()).get('review_id')
    raw = (message.text or '').strip()
    if raw.lower() in ('-', 'удалить'):
        caption = ''
    else:
        caption = _extract_section_html(message)
        try:
            validate_section_html(caption)
        except ValueError as exc:
            return await message.answer(f'Ошибка формата: {esc(exc)}\nПопробуйте ещё раз или /admin.')
    await db.execute('UPDATE reviews SET caption=? WHERE id=?', (caption, rid))
    await db.audit(message.from_user.id, 'review_edit', f'{rid}:caption')
    await state.clear()
    len_note = ('\n⚠️ Подпись длиннее 950 символов — при отправке пойдёт отдельным сообщением под скриншотом.'
                if _utf16_len(caption) > 950 else '')
    await message.answer(f'✅ Подпись сохранена.{len_note}', reply_markup=form_nav('⭐ К отзыву', f'adm:rpush:card:{rid}'))


@router.callback_query(F.data.regexp(r'^adm:rpush:rdel:[0-9]{1,12}$'))
async def review_delete_confirm(call: CallbackQuery, db: Database):
    rid = int(call.data.rsplit(':', 1)[1])
    if not await db.read('SELECT 1 FROM reviews WHERE id=?', (rid,)):
        return await call.answer('Скриншот не найден.', show_alert=True)
    await call.answer()
    await screen(
        call,
        f'<b>🗑 Удалить скриншот отзыва #{rid}?</b>\nЭто действие необратимо.',
        reply_markup=kb(
            (cb('🗑 Да, удалить', f'adm:rpush:rdelok:{rid}', style='danger'),),
            (cb('◀️ Отмена', f'adm:rpush:card:{rid}'),),
        ),
    )


@router.callback_query(F.data.regexp(r'^adm:rpush:rdelok:[0-9]{1,12}$'))
async def review_delete_execute(call: CallbackQuery, db: Database):
    rid = int(call.data.rsplit(':', 1)[1])
    await db.execute('DELETE FROM user_review_pushes WHERE review_id=?', (rid,))
    await db.execute('DELETE FROM reviews WHERE id=?', (rid,))
    await db.audit(call.from_user.id, 'review_delete', str(rid))
    await call.answer('Скриншот удалён.')
    await _render_review_list(call, db, 0)


@router.callback_query(F.data == 'adm:rpush:test')
async def review_push_test(call: CallbackQuery, db: Database, shop: Shop):
    review = await db.read('SELECT * FROM reviews WHERE active=1 ORDER BY sort_order ASC, id ASC LIMIT 1')
    if not review:
        review = await db.read('SELECT * FROM reviews ORDER BY id ASC LIMIT 1')
    if not review:
        return await call.answer('Сначала загрузите хотя бы один скриншот отзыва.', show_alert=True)
    user = {'user_id': call.from_user.id, 'name': call.from_user.full_name}
    caption, markup = await build_review_push(db, shop, user, review)
    if await safe_send_media(call.bot, call.from_user.id, review['media_id'], review['media_type'], caption, markup):
        return await call.answer('Тестовый пуш с отзывом отправлен.')
    await call.answer('Не удалось отправить ЛС клиенту (бот заблокирован или недоступен).', show_alert=True)


def _review_blast_where(seg: str, review_id: int) -> str:
    base = BROADCAST_SEGMENTS.get(seg, BROADCAST_SEGMENTS['cold'])[1]
    if review_id > 0:
        return f'({base}) AND NOT EXISTS (SELECT 1 FROM user_review_pushes urp WHERE urp.user_id=u.user_id AND urp.review_id={int(review_id)})'
    return (
        f'({base}) AND EXISTS (SELECT 1 FROM reviews r WHERE r.active=1 '
        'AND NOT EXISTS (SELECT 1 FROM user_review_pushes urp WHERE urp.user_id=u.user_id AND urp.review_id=r.id))'
    )


async def _run_review_blast(bot: Bot, db: Database, shop: Shop, admin_id: int,
                            review_id: int, seg: str, users: list[dict],
                            progress: Message, stop_event: asyncio.Event):
    success = fail = skipped = 0
    total = len(users)
    try:
        cfg = await push_settings(db)
        order_mode = cfg.get('review_push_order') or 'seq'
        fixed_review = await db.read('SELECT * FROM reviews WHERE id=?', (review_id,)) if review_id > 0 else None
        for index, user in enumerate(users, 1):
            if stop_event.is_set():
                break
            if review_id > 0 and not fixed_review:
                skipped += total - index + 1
                break
            result = await send_review_push_to_user(
                bot, db, shop, user, cfg,
                review=fixed_review, order_mode=order_mode, retry_flood=True,
            )
            if result == 'sent':
                success += 1
            elif result == 'skipped':
                skipped += 1
            else:
                fail += 1
                if result == 'flooded':
                    skipped += total - index
                    stop_event.set()
                    break
            if index % 25 == 0 and index < total and not stop_event.is_set():
                try:
                    await progress.edit_text(
                        f'⏳ Рассылка отзывов: <b>{index}/{total}</b>\n'
                        f'Доставлено <b>{success}</b> · Ошибок <b>{fail}</b>',
                        reply_markup=kb((cb('🛑 Остановить', 'adm:bcast:stop', style='danger'),)),
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.07)
        stopped = stop_event.is_set()
        await db.audit(admin_id, 'review_push_blast', f'{seg}:r{review_id}:{success}/{total}')
        title = '🛑 <b>Рассылка отзывов остановлена</b>' if stopped else '✅ <b>Рассылка отзывов завершена</b>'
        summary = f'{title}\n\nДоставлено <b>{success}</b>\nНе доставлено <b>{fail}</b>\nПропущено <b>{skipped}</b>'
        done_kb = form_nav('⭐ К отзывам', 'adm:rpush')
        try:
            await progress.edit_text(summary, reply_markup=done_kb)
        except Exception:
            await safe_send(bot, admin_id, summary, done_kb)
    except Exception:
        log.exception('Review blast failed for admin %s', admin_id)
    finally:
        current = _ACTIVE_BROADCASTS.get(admin_id)
        if current and current[1] is stop_event:
            _ACTIVE_BROADCASTS.pop(admin_id, None)


@router.callback_query(F.data.regexp(r'^adm:rpush:blast:[0-9]{1,12}$'))
async def review_blast_choose_segment(call: CallbackQuery, db: Database):
    if not await reviews_visible(db):
        return await call.answer('Включите показ отзывов в настройках перед рассылкой.', show_alert=True)
    rid = int(call.data.rsplit(':', 1)[1])
    if rid == 0 and not await db.read('SELECT 1 FROM reviews WHERE active=1 LIMIT 1'):
        return await call.answer('Сначала загрузите хотя бы 1 активный скриншот.', show_alert=True)
    if rid > 0 and not await db.read('SELECT 1 FROM reviews WHERE id=?', (rid,)):
        return await call.answer('Скриншот не найден.', show_alert=True)
    await call.answer()
    rows = []
    for seg in ('cold', 'buyers', 'entry', 'whales', 'all'):
        label = BROADCAST_SEGMENTS[seg][0]
        where = _review_blast_where(seg, rid)
        count = (await db.read(f'SELECT COUNT(*) AS n FROM users u WHERE {where}'))['n']
        rows.append((cb(f'{label} · {count}', f'adm:rpush:blastgo:{rid}:{seg}', style='primary' if seg in ('cold', 'buyers') else None),))
    rows.append((cb('◀️ Назад', f'adm:rpush:card:{rid}' if rid > 0 else 'adm:rpush'),))
    title = f'скриншот #{rid}' if rid > 0 else 'следующий непоказанный скриншот'
    await screen(
        call,
        f'<b>🚀 Ручная рассылка отзыва ({title})</b>\n\n'
        'Пользователи, которые уже получали этот скриншот, пропускаются автоматически.\n'
        'Выберите аудиторию для отправки:',
        reply_markup=kb(*rows),
    )


@router.callback_query(F.data.regexp(r'^adm:rpush:blastgo:[0-9]{1,12}:(cold|buyers|entry|whales|all)$'))
async def review_blast_go(call: CallbackQuery, db: Database, shop: Shop, bot: Bot):
    if not await reviews_visible(db):
        return await call.answer('Включите показ отзывов в настройках перед рассылкой.', show_alert=True)
    existing = _ACTIVE_BROADCASTS.get(call.from_user.id)
    if existing and not existing[0].done():
        return await call.answer('Дождитесь окончания текущей рассылки или остановите её.', show_alert=True)
    _, _, _, rid_s, seg = call.data.split(':')
    rid = int(rid_s)
    where = _review_blast_where(seg, rid)
    users = await db.read(f'SELECT u.* FROM users u WHERE {where} ORDER BY u.user_id', many=True)
    if not users:
        return await call.answer('В выбранном сегменте нет получателей для этого отзыва.', show_alert=True)
    await call.answer('Рассылка отзывов запущена в фоне.')
    progress = await call.message.answer(
        f'⏳ Рассылка отзывов выполняется (получателей: <b>{len(users)}</b>)…',
        reply_markup=kb((cb('🛑 Остановить', 'adm:bcast:stop', style='danger'),)),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(_run_review_blast(bot, db, shop, call.from_user.id, rid, seg, users, progress, stop_event))
    _ACTIVE_BROADCASTS[call.from_user.id] = (task, stop_event)
