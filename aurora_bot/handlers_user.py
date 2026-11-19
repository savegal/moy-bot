"""User-facing handlers: browse catalog, view tariffs, pay, manage profile and access."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from time import monotonic

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, PreCheckoutQuery, LabeledPrice

from .alerts import dispatch_whale_alerts, paying_perk_rows
from .pushes import build_review_push, safe_send_media
from .analytics import CAMPAIGN_CALLBACK, attribute_campaign_order, campaign_click, record_engagement
from .config import Config
from .db import DEFAULT_PROJECT_NAME, Database, contacts_visible, help_visible, now, reviews_visible
from .live_stats import get_live_recent_purchases, get_live_tariff_buyers
from .service import (
    Shop, ShopError, available_quick_promo, price_with_promo, promo_discount_label, tariff_delivery, tariff_tier_kind,
)
from .payments import Payments
from .access import AccessManager, validate_sale_channels
from .greetings import WELCOME_CHAT_TYPES, greet_request, membership_changed
from .legal import get_terms_of_service, get_privacy_policy
from .ui import (
    button_tariff_name, cb, kb, link, esc, duration, duration_phrase, fmt_rub, is_https_url, is_menu_navigation_text,
    menu, plural_channels, product_text, remaining_time_label, rendered_section, send_section, send_text,
    tariff_channel_count, tariff_view_media, MENU_CATALOG, MENU_PROFILE, MENU_HELP, MENU_HELP_SIMPLE, MENU_INFO,
    MENU_STATS_PREFIX,
)

router = Router(name='user')
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
logger = logging.getLogger(__name__)


@router.callback_query.outer_middleware()
async def track_campaign_callback(handler, event, data):
    match = CAMPAIGN_CALLBACK.fullmatch(event.data or '')
    if match:
        touch_id = int(match[1])
        if touch_id >= 2 ** 63 or not await campaign_click(data['db'], event.from_user.id, touch_id):
            return await event.answer('Предложение устарело. Открой магазин через меню.', show_alert=True)
        event = event.model_copy(update={'data': match[2]})
    return await handler(event, data)


@router.message.middleware()
@router.callback_query.middleware()
async def track_shop_interaction(handler, event, data):
    user = event.from_user
    if user and not user.is_bot:
        if isinstance(event, CallbackQuery):
            await record_engagement(data['db'], user.id)
            if isinstance(event.message, Message):
                await refresh_project_menu(event.message, data['db'], user.id)
        elif event.text:
            command = event.text.split(maxsplit=1)
            is_start = bool(command and command[0].split('@')[0] == '/start')
            payload = command[1].strip() if is_start and len(command) == 2 else ''
            await record_engagement(data['db'], user.id, payload)
            if isinstance(event, Message) and not is_start:
                await refresh_project_menu(event, data['db'], user.id)
    state = data.get('state')
    if state and await state.get_state() == UserInput.promo.state:
        navigating = (isinstance(event, CallbackQuery) and event.data != 'promo') or (
            isinstance(event, Message) and is_menu_navigation_text(event.text))
        if navigating:
            await state.clear()
    return await handler(event, data)


class UserInput(StatesGroup):
    promo = State()


async def terms_for_bot(db: Database, project_name: str) -> str:
    if not await help_visible(db):
        help_label = None
    else:
        help_label = MENU_HELP if await reviews_visible(db) else MENU_HELP_SIMPLE
    return get_terms_of_service(project_name, help_label=help_label)


async def _help_back_markup(db: Database):
    return kb((cb('◀️ Назад в Помощь', 'help_menu'),)) if await help_visible(db) else None


async def project_menu_settings(db: Database, user_id: int):
    return await db.read("""SELECT
        (SELECT value FROM settings WHERE key='project_name') AS project_name,
        (SELECT value='1' FROM settings WHERE key='reviews_visible') AS show_reviews,
        COALESCE((SELECT value!='0' FROM settings WHERE key='help_visible'), 1) AS show_help,
        (SELECT CAST(value AS INTEGER) FROM settings WHERE key='menu_revision') AS revision,
        (SELECT menu_revision FROM users WHERE user_id=?) AS shown_revision""", (user_id,))


async def refresh_project_menu(message: Message, db: Database, user_id: int):
    if message.bot is None:
        return
    settings = await project_menu_settings(db, user_id)
    if settings['shown_revision'] is None or settings['shown_revision'] == settings['revision']:
        return
    markup = menu(
        settings['project_name'] or DEFAULT_PROJECT_NAME,
        show_reviews=bool(settings['show_reviews']),
        show_help=bool(settings['show_help']),
    )
    try:
        await message.answer('Меню обновлено.', reply_markup=markup, request_timeout=3)
    except (TelegramAPIError, TimeoutError):
        return
    await db.execute('UPDATE users SET menu_revision=? WHERE user_id=?', (settings['revision'], user_id))


async def send_welcome_message(target: Message | CallbackQuery, db: Database, full_name: str, *, is_new: bool = False):
    user_id = target.from_user.id
    settings = await project_menu_settings(db, user_id)
    project_name = settings['project_name'] or DEFAULT_PROJECT_NAME
    markup = menu(
        project_name,
        show_reviews=bool(settings['show_reviews']),
        show_help=bool(settings['show_help']),
    )

    msg = target.message if hasattr(target, 'message') else target
    row = await rendered_section(db, 'welcome' if is_new else 'welcome_back', name=full_name, project_name=project_name)
    await send_section(msg, row, markup=markup)
    await db.execute('UPDATE users SET menu_revision=? WHERE user_id=?', (settings['revision'], user_id))


@router.message(CommandStart())
async def welcome(message: Message, db: Database, shop: Shop, config: Config, state: FSMContext,
                  payments: Payments | None = None):
    if not message.from_user:
        return

    await state.clear()

    # Check for deep-linking arguments (e.g. /start agreement or /start privacy)
    parts = (message.text or '').split(maxsplit=1)
    deep_arg = parts[1].strip().lower() if len(parts) > 1 else ''
    existing = await db.read('SELECT 1 FROM users WHERE user_id=?', (message.from_user.id,))
    await db.register(message.from_user.id, message.from_user.username or '', message.from_user.full_name,
                      via_start=True)
    if deep_arg:
        project_name = await db.project_name()
        if deep_arg in ('agreement', 'terms', 'tos'):
            await send_text(message.answer, await terms_for_bot(db, project_name), await _help_back_markup(db))
            return
        elif deep_arg in ('privacy', 'policy'):
            await send_text(message.answer, get_privacy_policy(project_name), await _help_back_markup(db))
            return

    await send_welcome_message(message, db, message.from_user.full_name, is_new=existing is None)
    await show_catalog(message, shop, db)

    if message.from_user.id in config.admin_ids:
        await message.answer(
            '🛡 <b>Вы вошли как администратор.</b> Для быстрого доступа:',
            reply_markup=kb((cb('🛡 Панель управления', 'adm:home'),))
        )


@router.my_chat_member(F.chat.type.in_(WELCOME_CHAT_TYPES))
async def discover_channel(event, db: Database):
    await membership_changed(event, db)


@router.chat_join_request()
async def join_request(request, bot: Bot, db: Database, access: AccessManager):
    async def greet_product():
        await greet_request(bot, db, request, paid_channel=True)

    if await access.on_join_request(request, greet=greet_product) == 'unhandled':
        await greet_request(bot, db, request)


@router.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Действие отменено.')


# --- Catalog ---

def _catalog_button_label(tariff: dict) -> str:
    """Return clean tariff title on catalog buttons so pricing appears only inside cards."""
    return tariff['title']


async def show_catalog(target: Message | CallbackQuery, shop: Shop, db: Database, page: int = 0):
    tariffs = await shop.catalogs()
    promo = await shop.user_promo(target.from_user.id) if tariffs else None
    page_size = 10
    page = min(page, max(0, (len(tariffs) - 1) // page_size))
    rows = []
    for t in tariffs[page * page_size:(page + 1) * page_size]:
        raw_title = t['title']
        tier_kind = tariff_tier_kind(raw_title, t.get('tier', ''))
        style = 'primary' if tier_kind == 'ultra' or raw_title.rstrip().upper().endswith(('[ТОП]', '[ХИТ]')) else None
        rows.append([cb(_catalog_button_label(t), f"tariff:{t['id']}", style=style)])
    navigation = []
    if page:
        navigation.append(cb('◀️ Предыдущие', f'catalog:{page - 1}'))
    if (page + 1) * page_size < len(tariffs):
        navigation.append(cb('Следующие ▶️', f'catalog:{page + 1}'))
    if navigation:
        rows.append(navigation)
    if tariffs and not promo:
        quick_promo, quick_cb, _ = await available_quick_promo(db, target.from_user.id)
        if quick_promo:
            rows.append([cb(
                f"🎁 Активировать скидку {promo_discount_label(quick_promo)} в 1 клик",
                quick_cb,
                style='primary',
            )])
    if tariffs and hasattr(shop, 'upgrade_offers'):
        rows.extend(list(r) for r in await paying_perk_rows(shop, db, target.from_user.id))
    row = await rendered_section(db, 'catalog', name=target.from_user.full_name)
    if not tariffs:
        row['value'] = 'Каталог временно пуст. Новые товары появятся здесь после публикации.'
    elif promo:
        rem = remaining_time_label(promo.get('expires_at'))
        rem_note = f' (действует ещё {rem})' if rem else ''
        promo_note = (
            f"🏷 <b>Промокод {esc(promo['code'])} ({promo_discount_label(promo)}) активен{rem_note}</b>"
            " — скидка уже учтена внутри каждой карточки 👇"
        )
        row['value'] = f"{row['value']}\n\n{promo_note}"
    msg = target.message if hasattr(target, 'message') else target
    await send_section(msg, row, kb(*rows) if rows else None, fallback='Каталог временно пуст.')


@router.message(F.text.in_({MENU_CATALOG, '◈ Каталог', 'Каталог', '🔐 Доступ к контенту'}))
async def catalog_message(message: Message, shop: Shop, db: Database):
    await db.register(message.from_user.id, message.from_user.username or '', message.from_user.full_name)
    await show_catalog(message, shop, db)


@router.callback_query(F.data == 'catalog')
@router.callback_query(F.data.regexp(r'^catalog:[0-9]{1,8}$'))
async def catalog_callback(call: CallbackQuery, shop: Shop, db: Database):
    await call.answer()
    await show_catalog(call, shop, db, int(call.data.partition(':')[2] or 0))


# --- Tariff Card & Payment Selection ---

async def _render_tariff_view(call: CallbackQuery, shop: Shop, tid: int, *, is_preview: bool,
                              answer_callback: bool = True):
    t = await shop.get_tariff(tid)
    if not t or not t['active']:
        if answer_callback:
            return await call.answer('Тариф временно недоступен' if not is_preview else 'Тариф недоступен', show_alert=True)
        return await call.message.answer('Тариф временно недоступен.')
    await shop.db.register(
        call.from_user.id,
        getattr(call.from_user, 'username', None) or '',
        getattr(call.from_user, 'full_name', None) or '',
    )
    await shop.db.execute(
        'UPDATE users SET last_viewed_tariff_id=?, last_viewed_at=?, view_reminder_sent=0 WHERE user_id=?',
        (tid, now(), call.from_user.id),
    )
    rub, stars, code = await shop.user_price(call.from_user.id, t)
    buyers = None if is_preview else await get_live_tariff_buyers(shop, shop.db, t)
    offers = await shop.upgrade_offers(call.from_user.id)
    is_paying = await shop.is_paying_user(call.from_user.id)
    upgrade = next((item for item in offers if item['tariff']['id'] == tid), None)
    effective_upgrade = upgrade if (upgrade and upgrade['rub'] < rub) else None
    ultra_offer = next((item for item in offers if item['kind'] == 'ULTRA'), None)
    already_has_ultra_or_above = is_paying and ultra_offer is None

    text = product_text(
        t, stars, buyers=buyers, promo=code, rub=rub, upgrade=effective_upgrade,
        allow_vip_nudge=not already_has_ultra_or_above,
    )

    rows = []
    if effective_upgrade:
        applied_credit = effective_upgrade.get('saved_rub', effective_upgrade['credit_rub'])
        rows.append((cb(
            f"⚡ Апгрейд (зачёт {fmt_rub(applied_credit)} ₽) — {fmt_rub(effective_upgrade['rub'])} ₽",
            f"up_pay_options:{t['id']}",
            style='success',
        ),))
    else:
        cta_label = f"🔥 Получить доступ · {fmt_rub(rub)} ₽" if rub else '🔥 Получить доступ'
        rows.append((cb(cta_label, f"pay_options:{t['id']}", style='success'),))
        if not is_preview and not code and rub:
            quick_promo, _, _ = await available_quick_promo(shop.db, call.from_user.id)
            if quick_promo:
                disc_rub, _ = price_with_promo(
                    rub, t['stars'], quick_promo['discount'], int(quick_promo.get('discount_percent') or 0),
                )
                if disc_rub < rub:
                    rows.append((cb(
                        f"🎁 Применить скидку {promo_discount_label(quick_promo)} → {fmt_rub(disc_rub)} ₽",
                        f"claim_tariff:{t['id']}:{quick_promo['code']}",
                        style='primary',
                    ),))
        tier_kind = tariff_tier_kind(t['title'], t.get('tier', ''))
        if not is_preview and tier_kind == '' and t['rub'] < 1000 and not already_has_ultra_or_above:
            ultra = None
            use_ultra_upgrade = False
            if ultra_offer and ultra_offer['tariff']['id'] != tid:
                ultra = ultra_offer['tariff']
                promo_ultra_rub, _, _ = await shop.user_price(call.from_user.id, ultra)
                use_ultra_upgrade = ultra_offer['rub'] < promo_ultra_rub
                eff_ultra_rub = ultra_offer['rub'] if use_ultra_upgrade else promo_ultra_rub
            else:
                ultra = next(
                    (u for u in await shop.catalogs()
                     if u['id'] != tid and tariff_tier_kind(u['title'], u.get('tier', '')) == 'ultra'
                     and (u['channel_ids'] or u['delivery_urls'] != '[]')),
                    None,
                )
                eff_ultra_rub = (await shop.user_price(call.from_user.id, ultra))[0] if ultra else 0
            if ultra:
                ultra_id = ultra['id']
                ultra_name = button_tariff_name(ultra['title'], 'Ultra VIP')
                u_channels = tariff_channel_count(ultra)
                u_term = duration_phrase(ultra.get('duration_days'))
                u_scope = plural_channels(u_channels) if u_channels > 1 else u_term
                diff = eff_ultra_rub - rub
                if use_ultra_upgrade and diff <= 0:
                    upsell_label = f"⚡ Апгрейд: {ultra_name} ({u_scope}) — {fmt_rub(eff_ultra_rub)} ₽"
                elif use_ultra_upgrade:
                    u_short = button_tariff_name(ultra['title'], 'Ultra VIP', max_len=20)
                    upsell_label = f"⚡ Апгрейд: {u_short} — {fmt_rub(eff_ultra_rub)} ₽ (+{fmt_rub(diff)} ₽)"
                elif diff <= 0:
                    upsell_label = f"🔥 Забрать {ultra_name} ({u_scope}) — {fmt_rub(eff_ultra_rub)} ₽"
                elif diff <= 550 and u_channels > 1:
                    upsell_label = f"⭐️ {ultra_name} · {u_term} (+{fmt_rub(diff)} ₽)"
                elif diff <= 550:
                    upsell_label = f"⭐️ {ultra_name} (+{fmt_rub(diff)} ₽)"
                else:
                    upsell_label = f"⭐️ Или {ultra_name} ({u_scope}) · {fmt_rub(eff_ultra_rub)} ₽"
                rows.append((cb(upsell_label, f"tariff:{ultra_id}", style='primary'),))
    if not is_preview:
        rows.append((cb('👀 Что внутри? (Смотреть превью)', f"preview:{t['id']}"),))
        rows.append((cb('◀️ Назад к тарифам', 'catalog'),))
    else:
        rows.append((cb('◀️ Назад к тарифу', f"tariff:{t['id']}"),))
    if answer_callback:
        await call.answer()
    media_id, media_type = tariff_view_media(t, details=is_preview)
    await send_section(call.message, {'value': text, 'media_id': media_id, 'media_type': media_type}, markup=kb(*rows))


@router.callback_query(F.data.regexp(r'^tariff:[0-9]{1,16}$'))
async def tariff_card(call: CallbackQuery, shop: Shop):
    tid = int(call.data.split(':')[1])
    await _render_tariff_view(call, shop, tid, is_preview=False)


@router.callback_query(F.data.regexp(r'^preview:[0-9]{1,16}$'))
async def tariff_preview(call: CallbackQuery, shop: Shop):
    tid = int(call.data.split(':')[1])
    await _render_tariff_view(call, shop, tid, is_preview=True)


async def _stars_invoice_for_order(bot: Bot, shop: Shop, db: Database, order: dict) -> tuple[str | None, dict]:
    """Reuse or create a Telegram Stars invoice link without sending a separate invoice post."""
    if order['payment_url']:
        return order['payment_url'], order
    try:
        if channels := json.loads(order['channels']):
            await validate_sale_channels(bot, channels)
    except ShopError:
        await db.execute(
            """UPDATE orders SET status='failed' WHERE id=? AND status='pending'
               AND checkout_id='' AND invoice_sending_at IS NULL AND invoice_sent_at IS NULL""",
            (order['id'],),
        )
        raise
    if not await shop.claim_invoice(order['id']):
        for _ in range(3):
            await asyncio.sleep(0.15)
            latest = await shop.order(order['id'])
            if latest and latest['payment_url']:
                return latest['payment_url'], latest
        return None, order
    term_label = duration(order['duration_days'])
    try:
        invoice_url = await bot.create_invoice_link(
            title=(order['title'][:32].strip() or 'Доступ'),
            description=f"{order['title']} · {term_label}. Разовая оплата."[:255],
            payload=order['id'],
            currency='XTR',
            provider_token='',
            prices=[LabeledPrice(label='Доступ', amount=order['amount_stars'])],
            request_timeout=10,
        )
    except Exception:
        await shop.release_invoice_claim(order['id'])
        raise
    await shop.mark_invoice_sent(order['id'], invoice_url)
    return invoice_url, order


async def _send_checkout_screen(target_msg: Message, db: Database, order: dict, invoice_url: str, *,
                                  title: str, crypto_cb: str, back_cb: str,
                                  credit_rub: int = 0, payments: Payments | None = None):
    stars = order['amount_stars']
    rub = order['amount_rub']
    code = order['promo_code']
    heading = f"💎 <b>Оформление доступа: {esc(title)}</b>"
    term_line = f"🕘 Срок доступа: <b>{duration(order['duration_days'])}</b>"
    extra_line = ''
    if credit_rub:
        extra_line = f"🎁 Зачтено из прошлых оплат: <b>{fmt_rub(credit_rub)} ₽</b>\n"
    elif code:
        promo_row = await db.read('SELECT expires_at FROM promos WHERE code=?', (code,))
        rem = remaining_time_label((promo_row or {}).get('expires_at'))
        rem_note = f' · ещё {rem}' if rem else ''
        extra_line = f"🏷 Промокод <b>{esc(code)}</b> активирован{rem_note} (скидка учтена!)\n"
    if order['channels'] != '[]' or order['delivery_urls'] != '[]':
        footer_note = '\n⚡️ <b>Моментальная автовыдача:</b> ссылка в закрытый канал придёт в этот чат сразу после оплаты.\n'
    else:
        footer_note = '\n🧾 Подтверждение покупки придёт в этот чат.\n'
    has_crypto = bool(payments and payments.crypto_enabled)
    rub_line = f"💎 Через @CryptoBot (USDT / TON): <b>{fmt_rub(rub)} ₽</b>\n" if has_crypto else ''
    crypto_tip = ''
    if has_crypto and rub:
        if stars * 1.9 > rub * 1.25:
            crypto_tip = '💡 <i>Через @CryptoBot заметно выгоднее — внутри доступны USDT, TON и P2P.</i>\n'
        else:
            crypto_tip = '💡 <i>В @CryptoBot можно оплатить балансом, TON или картой через P2P.</i>\n'
    text = (f"{heading}\n\n"
            f"{term_line}\n"
            f"{rub_line}"
            f"⭐ В Telegram Stars: <b>{fmt_rub(stars)} Stars</b>\n"
            f"{extra_line}"
            f"{footer_note}"
            f"{crypto_tip}"
            "🔒 <i>Платежи строго разовые — никаких подписок и автосписаний.</i>")
    row = await db.setting('payment') or {}
    rows = []
    if has_crypto:
        rows.append((cb(f'💎 CryptoBot (USDT / TON) — {fmt_rub(rub)} ₽', crypto_cb, style='primary'),))
    rows.append((link(f'⭐ Оплатить {fmt_rub(stars)} Telegram Stars', invoice_url, style='success'),))
    if not code and not credit_rub and order.get('order_kind') == 'tariff' and order.get('tariff_id') and rub:
        quick_promo, _, _ = await available_quick_promo(db, order['user_id'])
        if quick_promo:
            disc_rub, disc_stars = price_with_promo(
                rub, stars, quick_promo['discount'], int(quick_promo.get('discount_percent') or 0),
            )
            if disc_rub < rub or disc_stars < stars:
                disc_price = f"{fmt_rub(disc_rub)} ₽" if has_crypto else f"{fmt_rub(disc_stars)} ⭐"
                rows.append((cb(
                    f"🎁 Применить скидку {promo_discount_label(quick_promo)} → {disc_price}",
                    f"claim_pay:{order['tariff_id']}:{quick_promo['code']}",
                    style='primary',
                ),))
    rows.append((cb('◀️ Назад', back_cb),))
    await send_section(target_msg, {**row, 'value': text}, markup=kb(*rows))


async def _configured_support_url(db: Database) -> str:
    if not await contacts_visible(db):
        return ''
    support_url = (await db.setting('support_url') or {}).get('value', '').strip()
    return support_url if is_https_url(support_url) else ''


async def _open_pay_options(call: CallbackQuery, bot: Bot, shop: Shop, db: Database, tariff_id: int, *,
                            payments: Payments | None = None, config: Config | None = None,
                            toast: str | None = None):
    tariff = await shop.get_tariff(tariff_id)
    if not tariff or not tariff['active'] or not (tariff['channel_ids'] or tariff['delivery_urls'] != '[]' or tariff.get('allow_no_channel')):
        return await call.answer('Товар пока недоступен. Напиши в поддержку.', show_alert=True)
    await db.register(
        call.from_user.id,
        getattr(call.from_user, 'username', None) or '',
        getattr(call.from_user, 'full_name', '') or '',
    )
    try:
        order = await shop.create_order(call.from_user.id, tariff_id, 'stars')
        await attribute_campaign_order(db, order['id'])
        invoice_url, order = await _stars_invoice_for_order(bot, shop, db, order)
    except ShopError as exc:
        return await call.answer(str(exc)[:195], show_alert=True)
    except Exception:
        logger.exception('Invoice link generation failed')
        return await call.answer('Сервис временно недоступен. Попробуй позже или открой /paysupport.', show_alert=True)
    if not invoice_url:
        return await call.answer('Счёт уже формируется. Попробуй ещё раз через секунду.', show_alert=True)

    await call.answer(toast) if toast else await call.answer()
    await _send_checkout_screen(
        call.message, db, order, invoice_url, title=tariff['title'],
        crypto_cb=f'ext_pay:cryptopay:{tariff_id}', back_cb=f'tariff:{tariff_id}',
        payments=payments,
    )


@router.callback_query(F.data.regexp(r'^pay_options:[0-9]{1,16}$'))
async def pay_options(call: CallbackQuery, bot: Bot, shop: Shop, db: Database,
                      payments: Payments | None = None, config: Config | None = None):
    tariff_id = int(call.data.split(':')[1])
    await _open_pay_options(call, bot, shop, db, tariff_id, payments=payments, config=config)


@router.callback_query(F.data.regexp(r'^up_pay_options:[0-9]{1,16}$'))
async def upgrade_pay_options(call: CallbackQuery, bot: Bot, shop: Shop, db: Database,
                              payments: Payments | None = None, config: Config | None = None):
    tariff_id = int(call.data.split(':')[1])
    await db.register(
        call.from_user.id,
        getattr(call.from_user, 'username', None) or '',
        getattr(call.from_user, 'full_name', '') or '',
    )
    offers = await shop.upgrade_offers(call.from_user.id)
    offer = next((item for item in offers if item['tariff']['id'] == tariff_id), None)
    if not offer:
        return await call.answer('Персональное предложение апгрейда больше недоступно.', show_alert=True)
    try:
        order = await shop.create_upgrade_order(call.from_user.id, tariff_id, 'stars')
        await attribute_campaign_order(db, order['id'])
        invoice_url, order = await _stars_invoice_for_order(bot, shop, db, order)
    except ShopError as exc:
        return await call.answer(str(exc)[:195], show_alert=True)
    except Exception:
        logger.exception('Upgrade invoice generation failed')
        return await call.answer('Сервис временно недоступен. Попробуй позже или открой /paysupport.', show_alert=True)
    if not invoice_url:
        return await call.answer('Счёт уже формируется. Попробуй ещё раз через секунду.', show_alert=True)

    await call.answer()
    await _send_checkout_screen(
        call.message, db, order, invoice_url, title=order['title'],
        crypto_cb=f'up_ext_pay:cryptopay:{tariff_id}', back_cb='accesses',
        credit_rub=offer.get('saved_rub', offer['credit_rub']), payments=payments,
    )


async def _reusable_external_invoice(db: Database, user_id: int, *, provider: str, rub: int,
                                     channels: list[int], urls: list[str], tariff_id: int | None = None,
                                     duration_days: int = 0, promo_code: str = '',
                                     order_kind: str = 'tariff') -> dict | None:
    """Reuse an open invoice or a retryable 2328 request with unchanged terms."""
    rows = await db.read(
        """SELECT * FROM orders
           WHERE user_id=? AND tariff_id=? AND order_kind=? AND provider=? AND status='pending'
             AND amount_rub=? AND promo_code=? AND duration_days=?
             AND refund_requested_at IS NULL AND expires_at>?
             AND ((external_id!='' AND payment_url!='')
                  OR (provider='2328' AND external_id='' AND invoice_sending_at IS NOT NULL))
           ORDER BY created_at DESC, rowid DESC""",
        (user_id, tariff_id, order_kind, provider, rub, promo_code, duration_days, now()),
        many=True,
    )
    for row in rows:
        if (json.loads(row['channels']) == channels
                and json.loads(row['delivery_urls']) == urls):
            return row
    return None


async def _issue_external_order(payments: Payments, order: dict) -> tuple[dict, str]:
    await attribute_campaign_order(payments.shop.db, order['id'])
    _, pay_url = await payments.create_external(order)
    return order, pay_url


async def _send_external_invoice_card(call: CallbackQuery, db: Database, payments: Payments, *,
                                      provider: str, back_cb: str, resolve_order):
    if provider != 'cryptopay' or not payments.crypto_enabled:
        return await call.answer('Этот способ оплаты недоступен. Открой товар заново.', show_alert=True)

    await db.register(
        call.from_user.id,
        getattr(call.from_user, 'username', None) or '',
        getattr(call.from_user, 'full_name', '') or '',
    )
    try:
        lock = payments.checkout_locks.setdefault(call.from_user.id, asyncio.Lock())
        async with lock:
            order, pay_url = await resolve_order()
    except ShopError as exc:
        return await call.answer(str(exc)[:195], show_alert=True)
    except Exception as exc:
        logger.warning('External invoice creation failed (%s): %s', provider, exc)
        return await call.answer(
            'Не удалось создать счёт. Выбери другой способ оплаты или напиши в поддержку.',
            show_alert=True,
        )

    promo_line = f"🏷 Промокод <b>{esc(order['promo_code'])}</b> применён!\n" if order['promo_code'] else ''
    await call.answer()
    await call.message.answer(
        f"🧾 <b>Счёт на оплату сформирован!</b>\n\n"
        f"📦 Позиция: <b>{esc(order['title'])}</b>\n"
        f"💰 Сумма к оплате: <b>{fmt_rub(order['amount_rub'])} ₽</b>\n"
        f"🏦 Способ: <b>@CryptoBot (USDT / TON / BTC)</b>\n"
        f"{promo_line}\n"
        "1️⃣ Нажми <b>«Перейти к оплате»</b> ниже и заверши перевод.\n"
        "2️⃣ Бот проверит оплату автоматически (или нажми «Я оплатил»).\n\n"
        "⏳ <i>Счёт действует 35 минут · доступ придёт автоматически сразу после оплаты.</i>",
        reply_markup=kb(
            (link(f"🔥 Перейти к оплате ({fmt_rub(order['amount_rub'])} ₽)", pay_url, style='success'),),
            (cb('🔄 Я оплатил — проверить', f"check:{order['id']}", style='primary'),),
            (cb('◀️ Выбрать другой способ', back_cb),),
        ),
    )


@router.callback_query(F.data.regexp(r'^ext_pay:cryptopay:[0-9]{1,16}$'))
async def external_pay_invoice(call: CallbackQuery, bot: Bot, shop: Shop, db: Database,
                               payments: Payments, config: Config):
    _, provider, raw_tid = call.data.split(':')
    tariff_id = int(raw_tid)
    tariff = await shop.get_tariff(tariff_id)
    if not tariff or not tariff['active'] or not (tariff['channel_ids'] or tariff['delivery_urls'] != '[]' or tariff.get('allow_no_channel')):
        return await call.answer('Товар пока недоступен. Напиши в поддержку.', show_alert=True)

    async def resolve():
        channels, urls = tariff_delivery(tariff)
        if channels:
            await validate_sale_channels(bot, channels)
        rub, _, promo_code = await shop.user_price(call.from_user.id, tariff)
        reused = await _reusable_external_invoice(
            db, call.from_user.id, provider=provider, rub=rub, channels=channels, urls=urls,
            tariff_id=tariff_id, duration_days=tariff['duration_days'], promo_code=promo_code, order_kind='tariff',
        )
        order = reused or await shop.create_order(call.from_user.id, tariff_id, provider)
        return await _issue_external_order(payments, order)

    await _send_external_invoice_card(
        call, db, payments, provider=provider,
        back_cb=f'pay_options:{tariff_id}', resolve_order=resolve,
    )


@router.callback_query(F.data.regexp(r'^up_ext_pay:cryptopay:[0-9]{1,16}$'))
async def upgrade_external_pay_invoice(call: CallbackQuery, bot: Bot, shop: Shop, db: Database,
                                       payments: Payments, config: Config):
    _, provider, raw_tid = call.data.split(':')
    tariff_id = int(raw_tid)

    async def resolve():
        offers = await shop.upgrade_offers(call.from_user.id)
        offer = next((item for item in offers if item['tariff']['id'] == tariff_id), None)
        if not offer:
            raise ShopError('Предложение апгрейда больше недоступно.')
        tariff = offer['tariff']
        channels, urls = tariff_delivery(tariff)
        if channels:
            await validate_sale_channels(bot, channels)
        reused = await _reusable_external_invoice(
            db, call.from_user.id, provider=provider, rub=offer['rub'], channels=channels, urls=urls,
            tariff_id=tariff_id, duration_days=0, promo_code='', order_kind='upgrade',
        )
        order = reused or await shop.create_upgrade_order(call.from_user.id, tariff_id, provider)
        return await _issue_external_order(payments, order)

    await _send_external_invoice_card(
        call, db, payments, provider=provider,
        back_cb=f'up_pay_options:{tariff_id}', resolve_order=resolve,
    )


@router.callback_query(F.data.regexp(r'^(?:up_)?ext_pay:2328:[0-9]{1,16}$'))
@router.callback_query(F.data.startswith('review:'))
@router.callback_query(F.data.startswith('pay:'))
@router.callback_query(F.data.regexp(r'^buy:[0-9]+$'))
@router.callback_query(F.data.regexp(r'^terms:[0-9]+$'))
async def old_checkout(call: CallbackQuery):
    await call.answer('Открой товар заново, чтобы перейти к оплате.', show_alert=True)


@router.pre_checkout_query()
async def before_checkout(query: PreCheckoutQuery, shop: Shop, bot: Bot):
    deadline = monotonic() + 6.5
    async def authorize():
        order = await shop.order(query.invoice_payload)
        if (query.currency != 'XTR' or not order or order['provider'] != 'stars'
            or order['user_id'] != query.from_user.id or order['amount_stars'] != query.total_amount
            or order['status'] != 'pending' or order['expires_at'] <= now()):
            return False
        if channels := json.loads(order['channels']):
            await validate_sale_channels(bot, channels)
        return await shop.authorize_stars(order['id'], query.from_user.id, query.total_amount, query.id,
                                          deadline=deadline, ignore_repeat=True)

    try:
        ok = await asyncio.wait_for(authorize(), timeout=7)
    except (ValueError, TimeoutError, sqlite3.Error) as exc:
        logger.info('Pre-checkout rejected for order %r: %s', query.invoice_payload, type(exc).__name__)
        ok = False
    if ok is None:
        return
    try:
        acknowledged = await asyncio.wait_for(query.answer(ok=ok, error_message=None if ok else
            'Счёт, канал или скидка недоступны. Открой тариф заново или напиши в /paysupport.'), timeout=2.5)
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
        logger.info('Telegram rejected pre-checkout answer for order %r: %s',
                    query.invoice_payload, type(exc).__name__)
        acknowledged = False
    if ok and acknowledged is False:
        await shop.release_stars_checkout(query.invoice_payload, query.id)


async def _finalize_paid_tariff(bot: Bot | None, shop: Shop, config: Config | None,
                                 access: AccessManager | None, order_id: str) -> bool:
    """Emit whale alerts and immediately attempt access provisioning; return True if notice was sent."""
    if bot is not None:
        await dispatch_whale_alerts(bot, shop.db, config)
    if access is not None and bot is not None:
        try:
            await access.provision(bot)
        except Exception:
            logger.warning('Immediate access provisioning deferred for order %s', order_id, exc_info=True)
    latest = await shop.order(order_id)
    return bool(latest and latest['notice_sent'])


@router.message(F.successful_payment)
async def successful_stars(message: Message, shop: Shop, config: Config | None = None,
                           access: AccessManager | None = None):
    payment = message.successful_payment
    try:
        if payment.currency != 'XTR':
            raise ShopError('Неверная валюта Stars.')
        changed = await shop.settle_stars(payment.invoice_payload, message.from_user.id, payment.total_amount,
                                          payment.telegram_payment_charge_id, paid_at=message.date)
    except ShopError as exc:
        logger.error('Could not settle Stars order %r: %s', payment.invoice_payload, exc)
        await message.answer('Сохрани чек и напиши в /paysupport для сверки платежа.')
    else:
        if changed:
            bot = getattr(message, 'bot', None)
            delivered = await _finalize_paid_tariff(bot, shop, config, access, payment.invoice_payload)
            if delivered:
                return
            perks = await paying_perk_rows(shop, shop.db, message.from_user.id)
            rows = [(cb('🔓 Мои доступы', 'accesses', style='success'),), *perks]
            order = await shop.order(payment.invoice_payload)
            if order and order['channels'] == '[]' and order['delivery_urls'] == '[]':
                return await message.answer(
                    '✅ <b>Покупка подтверждена!</b> Товар добавлен в «🧾 Мои покупки».',
                    reply_markup=kb((cb('🧾 Мои покупки', 'my_orders', style='success'),), *perks),
                )
            await message.answer(
                '🔥 <b>Оплата успешно подтверждена!</b> 🎉\n\n'
                'Бот уже выпускает персональную ссылку для входа — через несколько секунд она придёт сюда автоматически.\n'
                'Также все ссылки всегда сохраняются в разделе <b>«👤 Профиль / Промокод» → «🔓 Мои доступы»</b>.\n\n'
                '🔞 <i>Приятного просмотра без цензуры!</i>',
                reply_markup=kb(*rows)
            )


@router.message(F.refunded_payment)
async def refunded_stars(message: Message, shop: Shop):
    payment = message.refunded_payment
    if payment.currency == 'XTR':
        await shop.refund_stars(payment.telegram_payment_charge_id, payment.total_amount,
                                order_id=payment.invoice_payload)


@router.callback_query(F.data.startswith('check:'))
async def check_payment(call: CallbackQuery, bot: Bot, shop: Shop, payments: Payments,
                        config: Config | None = None, access: AccessManager | None = None):
    order = await shop.order(call.data.split(':')[1])
    if not order or order['user_id'] != call.from_user.id:
        return await call.answer('Заказ не найден', show_alert=True)
    if order['refund_requested_at'] or order['status'] == 'refunded':
        status = 'Возврат подтверждён.' if order['status'] == 'refunded' else 'Возврат проверяется.'
        return await call.answer(status, show_alert=True)
    if order['status'] == 'failed':
        return await call.answer('Этот счёт уже закрыт или оплачен другим платежом.', show_alert=True)
    if order['status'] == 'paid':
        return await call.answer(
            'Оплата подтверждена! Доступ в разделе «👤 Профиль / Промокод» → «🔓 Мои доступы».',
            show_alert=True,
        )
    await call.answer()
    markup = None
    try:
        await payments.reconcile(order)
        current = await shop.order(order['id'])
    except ShopError as exc:
        text = str(exc)
    except Exception:
        logger.exception('Payment reconciliation failed')
        text = 'Касса сейчас не отвечает. Проверка повторится автоматически.'
    else:
        if current['status'] == 'failed':
            text = 'Этот счёт уже закрыт или оплачен другим платежом.'
        elif current['status'] == 'refunded':
            text = 'Возврат подтверждён.'
        elif current['refund_requested_at']:
            text = 'Возврат проверяется.'
        elif current['status'] == 'paid':
            if await _finalize_paid_tariff(bot, shop, config, access, current['id']):
                return
            perks = await paying_perk_rows(shop, shop.db, call.from_user.id)
            if current['channels'] == '[]' and current['delivery_urls'] == '[]':
                text = '✅ <b>Покупка подтверждена!</b> Товар добавлен в «🧾 Мои покупки».'
                markup = kb((cb('🧾 Мои покупки', 'my_orders', style='success'),), *perks)
            else:
                text = ('🔥 <b>Оплата подтверждена!</b>\n\n'
                        'Бот готовит ссылку — она придёт сюда автоматически. '
                        'Её также можно открыть в разделе «👤 Профиль / Промокод» → «🔓 Мои доступы».')
                markup = kb((cb('🔓 Мои доступы', 'accesses', style='success'),), *perks)
        else:
            text = (
                '⏳ <b>Платёж ещё обрабатывается кассой.</b>\n\n'
                'Обычно подтверждение занимает 5–15 секунд. Нажми «Проверить ещё раз» или дождись автоматической выдачи ссылки.'
            )
            pending_rows = [(cb('🔄 Проверить ещё раз', f"check:{current['id']}", style='primary'),)]
            if is_https_url(current.get('payment_url') or ''):
                pending_rows.append((link('🔥 Открыть счёт для оплаты', current['payment_url']),))
            markup = kb(*pending_rows)
    if markup:
        await bot.send_message(call.from_user.id, text, reply_markup=markup, request_timeout=5)
    else:
        await bot.send_message(call.from_user.id, text, request_timeout=5)


# --- Profile / Account ---

async def _send_profile(target_message: Message, user_obj, shop: Shop, db: Database, config: Config):
    uid = user_obj.id
    full_name = getattr(user_obj, 'full_name', '') or ''
    username = getattr(user_obj, 'username', None) or ''
    await db.register(uid, username, full_name)
    user = await shop.profile(uid)
    reg_date = user['created_at'][:10] if user and user.get('created_at') else now()[:10]
    active_promo = await shop.user_promo(uid)
    promo = active_promo['code'] if active_promo else ''
    quick_promo = None
    quick_promo_cb = 'claim_promo'
    redeemed_luxury = False
    if not promo:
        quick_promo, quick_promo_cb, redeemed_luxury = await available_quick_promo(db, uid, user=user)
    if promo:
        promo_line = esc(promo)
    elif quick_promo:
        q_code = quick_promo.get('code') or 'LUXURYNEW'
        promo_line = f"Не применён (активируй {esc(q_code)} для скидки {promo_discount_label(quick_promo)})"
    elif redeemed_luxury:
        promo_line = 'Не применён (промокод LUXURYNEW уже использован)'
    else:
        promo_line = 'Не применён'
    project_name = await db.project_name()
    text = (
        f"👤 <b>Личный кабинет участника {esc(project_name)}</b>\n\n"
        f"🆔 Твой ID: <code>{uid}</code>\n"
        f"👤 Профиль: <b>{esc(full_name)}</b>\n"
        f"🗓️ В клубе с: <b>{reg_date}</b>\n\n"
        "🎫 Применённый промокод:\n"
        f"└ <i>{promo_line}</i>\n\n"
        "🔐 <i>Все твои персональные ссылки и доступы хранятся в разделе «Мои доступы».</i>"
    )
    rows = [(cb('🔓 Мои доступы', 'accesses', style='primary'),)]
    if quick_promo:
        rows.append((cb(f"🎁 Активировать скидку {promo_discount_label(quick_promo)} в 1 клик", quick_promo_cb, style='success'),))
    rows.append((cb('🧾 Мои покупки', 'my_orders'), cb('🎫 Применить промокод', 'promo')))
    rows.extend(await paying_perk_rows(shop, db, uid))
    rows.append((cb('🔔 Уведомления и рассылки', 'notifications'),))
    if uid in config.admin_ids:
        rows.append((cb('🛡 Панель управления (Admin)', 'adm:home'),))
    row = await db.setting('profile') or {}
    await send_section(target_message, {**row, 'value': text}, markup=kb(*rows))


@router.message(F.text.in_({MENU_PROFILE, '◇ Мой аккаунт', 'Мой аккаунт', '👤 Мой аккаунт'}))
async def profile_message(message: Message, shop: Shop, db: Database, config: Config):
    await _send_profile(message, message.from_user, shop, db, config)


@router.callback_query(F.data == 'profile')
async def profile_callback(call: CallbackQuery, shop: Shop, db: Database, config: Config):
    await call.answer()
    await _send_profile(call.message, call.from_user, shop, db, config)


@router.callback_query(F.data == 'my_orders')
@router.callback_query(F.data.regexp(r'^my_orders:[0-9]{1,8}$'))
async def my_orders(call: CallbackQuery, db: Database):
    await call.answer()
    page_size = 8
    page = int(call.data.partition(':')[2] or 0)
    purchases = await db.read("""SELECT title FROM orders
        WHERE user_id=? AND status='paid' AND provider!='admin' AND refund_requested_at IS NULL
        ORDER BY COALESCE(paid_at,created_at) DESC, rowid DESC LIMIT ? OFFSET ?""",
        (call.from_user.id, page_size + 1, page * page_size), many=True)
    has_more = len(purchases) > page_size
    heading = '🧾 <b>Мои покупки</b>'
    if page or has_more:
        heading += f' · страница {page + 1}'
    body = '\n'.join(f"• {esc(purchase['title'])}" for purchase in purchases[:page_size])
    navigation = []
    if page:
        navigation.append(cb('◀️ Предыдущие', f'my_orders:{page - 1}'))
    if has_more:
        navigation.append(cb('Следующие ▶️', f'my_orders:{page + 1}'))
    await call.message.answer(
        f"{heading}\n\n{body or '<i>Покупок пока нет.</i>'}",
        reply_markup=kb(navigation, (cb('◀️ В профиль', 'profile'),)),
    )


@router.callback_query(F.data == 'notifications')
@router.callback_query(F.data.in_({'notifications:0', 'notifications:1'}))
async def notifications(call: CallbackQuery, db: Database):
    await call.answer()
    uid = call.from_user.id
    await db.register(uid, call.from_user.username or '', call.from_user.full_name)
    setting = call.data.partition(':')[2]
    if setting:
        enabled = 1 if int(setting) else 0
        await db.execute(
            'UPDATE users SET broadcast_opt_in=?, broadcast_opt_out=? WHERE user_id=?',
            (enabled, int(not enabled), uid),
        )
    user = await db.read('SELECT broadcast_opt_out FROM users WHERE user_id=?', (uid,))
    active = not bool(user and user['broadcast_opt_out'])
    await call.message.answer(
        'Рассылка включена.' if active else 'Рассылка выключена.',
        reply_markup=kb(
            (cb('Выключить' if active else 'Включить', f'notifications:{int(not active)}'),),
            (cb('◀️ В профиль', 'profile'),),
        ),
    )


@router.callback_query(F.data == 'accesses')
@router.callback_query(F.data.regexp(r'^accesses:[0-9]{1,8}$'))
async def my_accesses(call: CallbackQuery, shop: Shop):
    await call.answer()
    page_size = 5
    page = int(call.data.partition(':')[2] or 0)
    purchases = await shop.purchases(call.from_user.id, limit=page_size + 1, offset=page * page_size)
    perks = await paying_perk_rows(shop, shop.db, call.from_user.id) if page == 0 else []
    if not purchases and not page:
        return await call.message.answer(
            '🔒 <b>У тебя пока нет активных доступов к каналам.</b>\n\n'
            'В каталоге уже открыты все закрытые подборки и VIP-архивы — выбирай тариф и забирай доступ 🔥',
            reply_markup=kb((cb('🔐 Открыть каталог', 'catalog', style='success'),), *perks))
    for listed in purchases[:page_size]:
        async with shop.delivery_lock:
            purchase = await shop.purchase_for_user(call.from_user.id, listed['id'])
            grants = await shop.purchase_grants(purchase['id']) if purchase else []
        if not purchase:
            continue
        expired = bool(purchase['expires_at'] and purchase['expires_at'] <= now())
        if purchase['order_status'] == 'refunded':
            state = 'Возвращён'
        elif purchase['refund_requested_at']:
            state = 'Возврат проверяется'
        else:
            state = 'Отозван' if purchase.get('revoked_by_admin') else 'Истёк' if expired else 'Активен'
        expiry = purchase['expires_at'][:10] if purchase['expires_at'] else 'НАВСЕГДА'
        rows = []
        if (not expired and purchase['order_status'] == 'paid'
                and not purchase['refund_requested_at'] and not purchase.get('revoked_by_admin')):
            for index, url in enumerate(json.loads(purchase['delivery_urls']), 1):
                rows.append((link(f'🔗 Канал {index} · открыть', url, style='success'),))
            for index, grant in enumerate(grants, 1):
                if grant['state'] in ('expired', 'revoked'):
                    continue
                if grant['shared_url']:
                    rows.append((link(f'🔗 Канал {index} · войти', grant['shared_url'], style='success'),))
                elif grant['state'] in ('ready','joined') and grant['invite_url'] and (
                    grant['invite_expires_at'] and grant['invite_expires_at'] > now()
                ):
                    rows.append((link(f'🔗 Канал {index} · войти', grant['invite_url'], style='success'),
                                 cb('Обновить', f"refresh:{grant['id']}")))
                else:
                    rows.append((cb(f'🔄 Канал {index} · получить ссылку', f"refresh:{grant['id']}",
                                    style='primary'),))
        await asyncio.wait_for(call.message.answer(
            f"🔓 <b>{esc(purchase['title'])}</b>\n📌 Статус: <b>{state}</b>\n🕘 Срок: <b>{expiry}</b>"
            f"\n🧾 Покупка №{purchase['id']}",
            reply_markup=kb(*rows) if rows else None), timeout=10)
    buttons = []
    if page:
        buttons.append(cb('◀️ Предыдущие', f'accesses:{page - 1}'))
    if len(purchases) > page_size:
        buttons.append(cb('Следующие ▶️', f'accesses:{page + 1}'))
    if buttons:
        await call.message.answer(f'Мои доступы · страница {page + 1}', reply_markup=kb(buttons))
    if perks:
        upgrade_btns = [
            btn for row in perks for btn in row
            if getattr(btn, 'callback_data', '') and str(btn.callback_data).startswith('up_pay_options:')
        ]
        if upgrade_btns:
            has_u = any('ULTRA VIP' in (getattr(btn, 'text', '') or '') for btn in upgrade_btns)
            has_d = any('DIAMOND CLUB' in (getattr(btn, 'text', '') or '') for btn in upgrade_btns)
            tier_label = ('ULTRA VIP / DIAMOND CLUB' if (has_u and has_d)
                          else 'DIAMOND CLUB' if has_d
                          else 'ULTRA VIP')
            perk_lines = [
                '👑 <b>VIP-привилегии участника</b>\n',
                f'• Апгрейд до <b>{tier_label}</b> с полным зачётом прошлых покупок',
            ]
            await call.message.answer('\n'.join(perk_lines), reply_markup=kb(*perks))


@router.callback_query(F.data.regexp(r'^refresh:[0-9]{1,16}$'))
async def refresh_invite(call: CallbackQuery, access: AccessManager, bot: Bot):
    try:
        changed = await access.refresh_user(bot, call.from_user.id, int(call.data.split(':')[1]))
        if not changed:
            return await call.answer('Ссылка пока недоступна. Проверь срок доступа или повтори позже.', show_alert=True)
        grant = await access.db.read('SELECT * FROM grants WHERE id=? AND user_id=?',
                                   (int(call.data.split(':')[1]), call.from_user.id))
        await call.message.answer(
            '✅ <b>Новая персональная ссылка готова!</b>\n\n'
            'Жми кнопку ниже, чтобы перейти в закрытый канал:',
            reply_markup=kb((link('🔗 Войти в канал', grant['invite_url'], style='success'),)),
        )
        await call.answer('Ссылка успешно обновлена.')
    except Exception:
        logger.exception('Invite refresh failed')
        await call.answer('Не удалось обновить ссылку. Напиши в поддержку.', show_alert=True)


@router.callback_query(F.data == 'promo')
async def promo_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserInput.promo)
    await call.message.answer(
        '🎫 <b>Активация промокода</b>\n\n'
        'Отправь секретный промокод из рассылки или акции в ответном сообщении.\n\n'
        'Для отмены нажми: /cancel'
    )
    await call.answer()


@router.callback_query(F.data == 'claim_promo')
@router.callback_query(F.data.regexp(r'^claim_promo:[A-Z0-9_]{1,28}$'))
async def claim_promo_one_click(call: CallbackQuery, shop: Shop, db: Database):
    username = getattr(call.from_user, 'username', None) or ''
    await db.register(call.from_user.id, username, call.from_user.full_name)
    code = call.data.partition(':')[2] or 'LUXURYNEW'
    try:
        promo = await shop.activate_promo(call.from_user.id, code)
        await call.answer(
            f"🔥 Скидка {promo_discount_label(promo)} по промокоду {promo['code']} активирована! Открывай любой раздел — цена уже снижена.",
            show_alert=True,
        )
    except ShopError as exc:
        await call.answer(str(exc), show_alert=True)
    await show_catalog(call, shop, db)


@router.callback_query(F.data.regexp(r'^claim_tariff:[0-9]{1,16}:[A-Z0-9_]{1,28}$'))
async def claim_promo_on_tariff(call: CallbackQuery, shop: Shop, db: Database):
    _, raw_tid, code = call.data.split(':')
    tid = int(raw_tid)
    await db.register(
        call.from_user.id,
        getattr(call.from_user, 'username', None) or '',
        getattr(call.from_user, 'full_name', '') or '',
    )
    try:
        promo = await shop.activate_promo(call.from_user.id, code)
        await call.answer(f"🔥 Скидка {promo_discount_label(promo)} применена!")
    except ShopError as exc:
        await call.answer(str(exc), show_alert=True)
    await _render_tariff_view(call, shop, tid, is_preview=False, answer_callback=False)


@router.callback_query(F.data.regexp(r'^claim_pay:[0-9]{1,16}:[A-Z0-9_]{1,28}$'))
async def claim_promo_on_checkout(call: CallbackQuery, bot: Bot, shop: Shop, db: Database,
                                    payments: Payments | None = None, config: Config | None = None):
    _, raw_tid, code = call.data.split(':')
    await db.register(
        call.from_user.id,
        getattr(call.from_user, 'username', None) or '',
        getattr(call.from_user, 'full_name', '') or '',
    )
    try:
        promo = await shop.activate_promo(call.from_user.id, code)
    except ShopError as exc:
        return await call.answer(str(exc), show_alert=True)
    await _open_pay_options(
        call, bot, shop, db, int(raw_tid),
        payments=payments, config=config,
        toast=f"🔥 Скидка {promo_discount_label(promo)} применена!",
    )


# --- Help, Legal, and Cooperation ---

async def render_help_message(target: Message | CallbackQuery, db: Database, bot: Bot, config: Config | None = None):
    bot_info = await bot.get_me()
    bot_username = bot_info.username or ''
    row = await rendered_section(db, 'help', name=target.from_user.full_name, bot_username=bot_username)
    content = row['value']

    visible = await contacts_visible(db)
    support_url = (await db.setting('support_url') or {}).get('value', '').strip() if visible else ''
    show_reviews = await reviews_visible(db)

    rows = []
    if is_https_url(support_url):
        rows.append([link('💬 Поддержка ↗', support_url)])

    if show_reviews:
        reviews_url = (await db.setting('reviews_url') or {}).get('value', '').strip()
        if is_https_url(reviews_url):
            rows.append([link('⭐ Отзывы ↗', reviews_url)])
        else:
            rows.append([cb('⭐ Отзывы', 'contact:reviews')])

    if visible and (await db.setting('partnership_contact') or {}).get('value', '').strip():
        rows.append([cb('🤝 Сотрудничество', 'partnership')])

    user_id = target.from_user.id if target.from_user else 0
    if config and user_id in config.admin_ids:
        rows.append([cb('🛡 Панель управления (Admin)', 'adm:home')])

    msg = target.message if hasattr(target, 'message') else target
    markup = kb(*rows) if rows else None
    await send_section(msg, {'value': content, 'media_id': (row or {}).get('media_id', ''), 'media_type': (row or {}).get('media_type', '')},
                       markup=markup, fallback=content)


@router.message(F.text.in_({MENU_HELP, MENU_HELP_SIMPLE, '☏ Помощь', 'Помощь', '❓ Помощь/Отзывы'}))
async def help_message(message: Message, db: Database, bot: Bot, config: Config):
    if not await help_visible(db):
        return await message.answer('Раздел «Помощь» временно скрыт.')
    await render_help_message(message, db, bot, config)


@router.callback_query(F.data == 'help_menu')
async def help_callback(call: CallbackQuery, db: Database, bot: Bot, config: Config):
    if not await help_visible(db):
        return await call.answer('Раздел временно недоступен.', show_alert=True)
    await call.answer()
    await render_help_message(call, db, bot, config)


@router.callback_query(F.data == 'contact:support')
async def support_fallback(call: CallbackQuery, db: Database):
    support_url = await _configured_support_url(db)
    if is_https_url(support_url):
        await call.answer()
        await call.message.answer(
            '💬 <b>Служба поддержки</b>\n\nНажми кнопку ниже, чтобы написать администратору:',
            reply_markup=kb((link('💬 Написать в поддержку ↗', support_url),)),
        )
    else:
        await call.answer('Контакт поддержки недоступен.', show_alert=True)


async def _send_inbot_review(call: CallbackQuery, db: Database, shop: Shop, index: int = 0) -> bool:
    items = await db.read('SELECT * FROM reviews WHERE active=1 ORDER BY sort_order ASC, id ASC', many=True)
    if not items:
        return False
    pos = index % len(items)
    review = items[pos]
    user = {'user_id': call.from_user.id, 'name': call.from_user.full_name}
    caption, _ = await build_review_push(db, shop, user, review)
    rows: list[tuple] = []
    if len(items) > 1:
        rows.append((cb(f'➡️ Следующий отзыв ({pos + 1}/{len(items)})', f'review_view:{(pos + 1) % len(items)}'),))
    rows.append((cb('🔞 Открыть каталог', 'catalog', style='success'),))
    return await safe_send_media(call.bot, call.from_user.id, review['media_id'], review['media_type'], caption, kb(*rows))


@router.callback_query(F.data == 'contact:reviews')
async def reviews_fallback(call: CallbackQuery, db: Database, shop: Shop):
    if not await reviews_visible(db):
        return await call.answer('Раздел временно недоступен.', show_alert=True)
    reviews_url = (await db.setting('reviews_url') or {}).get('value', '').strip()
    if is_https_url(reviews_url):
        await call.answer()
        await call.message.answer(
            '⭐ <b>Отзывы наших участников</b>\n\nНажми кнопку ниже, чтобы перейти в канал с отзывами:',
            reply_markup=kb((link('⭐ Читать отзывы ↗', reviews_url),)),
        )
    elif await _send_inbot_review(call, db, shop, 0):
        await call.answer()
    else:
        await call.answer('Канал с отзывами временно настраивается администратором.', show_alert=True)


@router.callback_query(F.data.regexp(r'^review_view:[0-9]{1,6}$'))
async def reviews_viewer_next(call: CallbackQuery, db: Database, shop: Shop):
    if not await reviews_visible(db):
        return await call.answer('Раздел временно недоступен.', show_alert=True)
    idx = int(call.data.partition(':')[2])
    if not await _send_inbot_review(call, db, shop, idx):
        return await call.answer('Отзывы временно недоступны.', show_alert=True)
    await call.answer()


@router.callback_query(F.data == 'partnership')
async def partnership_handler(call: CallbackQuery, db: Database):
    contact = (await db.setting('partnership_contact') or {}).get('value', '').strip()
    if not contact or not await contacts_visible(db):
        return await call.answer('Контакт для сотрудничества недоступен.', show_alert=True)

    text = (
        "🤝 <b>Сотрудничество и реклама</b>\n\n"
        "Мы всегда открыты к предложениям по рекламе, взаимному пиару и покупке свежих сливов/архивов.\n\n"
        "Для связи с менеджером:\n"
        f"<blockquote>{esc(contact)}</blockquote>"
    )

    rows = []
    if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}', contact):
        rows.append([link('💬 Написать менеджеру ↗', f"https://t.me/{contact[1:]}")])
    elif contact.startswith('https://t.me/') and is_https_url(contact):
        rows.append([link('💬 Написать менеджеру ↗', contact)])
    if await help_visible(db):
        rows.append([cb('◀️ Назад в Помощь', 'help_menu')])

    await call.answer()
    await call.message.answer(text, reply_markup=kb(*rows) if rows else None)


# Legal Documents Handlers
@router.callback_query(F.data == 'doc:agreement')
async def agreement_callback(call: CallbackQuery, db: Database):
    project_name = await db.project_name()
    await call.answer()
    await send_text(call.message.answer, await terms_for_bot(db, project_name), await _help_back_markup(db))


@router.callback_query(F.data == 'doc:privacy')
async def privacy_callback(call: CallbackQuery, db: Database):
    project_name = await db.project_name()
    await call.answer()
    await send_text(call.message.answer, get_privacy_policy(project_name), await _help_back_markup(db))


@router.message(Command('agreement'))
@router.message(Command('terms'))
async def agreement_cmd(message: Message, db: Database):
    project_name = await db.project_name()
    await send_text(message.answer, await terms_for_bot(db, project_name), await _help_back_markup(db))


@router.message(Command('privacy'))
async def privacy_cmd(message: Message, db: Database):
    project_name = await db.project_name()
    await send_text(message.answer, get_privacy_policy(project_name), await _help_back_markup(db))


# --- Info ---

@router.message(F.text.in_({MENU_INFO, '≡ Информация', 'Информация', 'ℹ️ Информация'}))
async def info_message(message: Message, db: Database):
    reviews_url = ''
    if await reviews_visible(db):
        reviews_url = (await db.setting('reviews_url') or {}).get('value', '').strip()
    rows = []
    if is_https_url(reviews_url):
        rows.append([link('⭐ Отзывы ↗', reviews_url)])
    row = await rendered_section(db, 'info', name=message.from_user.full_name)
    await send_section(message, row, markup=kb(*rows) if rows else None)


# --- Statistics & Live Recent Purchases ---


async def render_stats(target: Message | CallbackQuery, shop: Shop, db: Database):
    stats = await shop.get_stats_data()
    live = await get_live_recent_purchases(shop, db)

    recent = live['recent_purchases'] if live['recent_purchases'] else stats['recent_purchases']
    lines = '\n'.join(f"✅ {esc(title)} — {ago}" for title, ago in recent)

    project_name = await db.project_name()
    stats_days_setting = (await db.setting('stats_days') or {}).get('value')
    base_days = int(stats_days_setting) if stats_days_setting and stats_days_setting.isdigit() else 450
    days_working = base_days + stats['days_working']
    days_word = ('день' if days_working % 10 == 1 and days_working % 100 != 11
                 else 'дня' if 2 <= days_working % 10 <= 4 and not 12 <= days_working % 100 <= 14
                 else 'дней')

    users_today = max(stats['new_users_today'], live['users_today'])
    sales_today = live['total_sales_today']
    popular_cat = live['popular_category'] or stats['popular_category']

    text = (
        f"📊 <b>Живая статистика {esc(project_name)}</b>\n\n"
        f"⚡️ Проект стабильно работает: <b>{days_working} {days_word}</b>\n"
        f"👥 Новых участников сегодня: <b>{users_today}</b>\n"
        f"🛒 Оформлено доступов сегодня: <b>{sales_today}</b>\n\n"
        f"<blockquote>🔥 Топ продаж сегодня — <b>{esc(popular_cat)}</b></blockquote>\n"
        "━━━━━━━━━━━━━━━\n"
        "🛒 <b>ПОСЛЕДНИЕ ПОКУПКИ В РЕАЛЬНОМ ВРЕМЕНИ:</b>\n"
        f"{lines}\n\n"
        "🔒 <i>Все доступы выдаются автоматически 24/7. Статистика обновляется онлайн.</i>"
    )
    markup = kb((cb('🔄 Обновить статистику', 'refresh_stats'),))
    stats_row = await db.setting('stats')

    msg = target.message if hasattr(target, 'message') else target
    await send_section(msg, {**(stats_row or {}), 'value': text}, markup=markup)


@router.message(F.text.startswith((MENU_STATS_PREFIX, '💜 Статистика', '▥ Статистика', 'Статистика')))
async def stats_message(message: Message, shop: Shop, db: Database):
    await render_stats(message, shop, db)


@router.callback_query(F.data == 'refresh_stats')
async def refresh_stats_callback(call: CallbackQuery, shop: Shop, db: Database):
    await call.answer('Статистика обновлена!')
    await render_stats(call, shop, db)


@router.message(Command('paysupport'))
async def payment_support(message: Message, db: Database, payments: Payments | None = None):
    url = await _configured_support_url(db)
    methods = 'Telegram Stars и CryptoBot' if payments and payments.crypto_enabled else 'Telegram Stars'
    text = (
        '🛟 <b>Поддержка по оплате и возвратам</b>\n\n'
        f'По вопросам оплаты {methods}, квитанций и возвратов обратись в службу поддержки.\n\n'
        '<i>💡 Сохрани чек об оплате. По нему проще найти платёж.</i>'
    )
    if is_https_url(url):
        await message.answer(text, reply_markup=kb((link('💬 Написать в поддержку ↗', url),)))
    else:
        await message.answer(text)


@router.message(UserInput.promo, ~F.text.startswith('/'))
async def promo_finish(message: Message, state: FSMContext, shop: Shop, db: Database):
    if not message.text:
        return await message.answer('Отправь код текстом.')
    await db.register(message.from_user.id, message.from_user.username or '', message.from_user.full_name)
    try:
        promo = await shop.activate_promo(message.from_user.id, message.text)
        await state.clear()
        await message.answer(
            f"✅ <b>Промокод</b> <code>{esc(promo['code'])}</code> <b>успешно активирован!</b> 🎉\n\n"
            f"Скидка <b>{promo_discount_label(promo)}</b> активирована — в карточках тарифов уже отображается сниженная цена.\n"
            "Открывай каталог и забирай доступ 👇",
            reply_markup=kb((cb('🔐 Открыть каталог со скидкой', 'catalog', style='success'),))
        )
    except ShopError as exc:
        await message.answer(esc(exc) + '\nПопробуй другой код или введи /cancel.')


@router.chat_member()
async def member_left(event, db: Database, access: AccessManager):
    try:
        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        uid = event.new_chat_member.user.id
        if new_status not in ('left', 'kicked') or old_status in ('left', 'kicked'):
            return
        async with access.lock:
            await db.execute("""UPDATE grants SET state='need_link'
                WHERE chat_id=? AND user_id=? AND state='joined' AND purchase_id IN
                (SELECT id FROM purchases WHERE expires_at IS NULL OR expires_at>?)""",
                (event.chat.id, uid, now()))
    except Exception:
        logger.exception('Failed to process member-left update')
