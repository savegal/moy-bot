"""VIP/whale notifications, custom-order notices, and buyer-only upsell helpers."""
from __future__ import annotations

import asyncio
import json
import logging

from .config import Config
from .analytics import campaign_delivered, prepare_campaign
from .db import Database, in_seconds, now
from .service import (
    Shop, available_quick_promo, channels_of, price_with_promo, promo_discount_label, tariff_tier_kind,
)
from .ui import (
    button_tariff_name, cb, duration_phrase, esc, fmt_rub, kb, plural_channels, tariff_channel_count,
)

log = logging.getLogger(__name__)


def _lock_for(db: Database, key: str = '_alerts_lock') -> asyncio.Lock:
    lock = getattr(db, key, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(db, key, lock)
    return lock


async def safe_send(bot, chat_id: int, text: str, markup=None) -> bool:
    """Send a Telegram HTML message, falling back cleanly for minimal test doubles."""
    try:
        if markup is not None:
            try:
                await asyncio.wait_for(bot.send_message(chat_id, text, reply_markup=markup), timeout=10)
            except TypeError:
                await asyncio.wait_for(bot.send_message(chat_id, text), timeout=10)
        else:
            await asyncio.wait_for(bot.send_message(chat_id, text), timeout=10)
        return True
    except Exception as exc:
        log.debug('Notification delivery to %s skipped/failed: %s', chat_id, type(exc).__name__)
        return False


async def send_campaign(bot, db: Database, user_id: int, campaign: str, text: str, markup) -> bool:
    touch_id, tracked_markup = await prepare_campaign(db, user_id, campaign, markup)
    if not await safe_send(bot, user_id, text, tracked_markup):
        return False
    await campaign_delivered(db, touch_id)
    return True


async def _is_broadcast_opted_out(db: Database, user_id: int) -> bool:
    row = await db.read('SELECT broadcast_opt_out FROM users WHERE user_id=?', (user_id,))
    return bool(row and row['broadcast_opt_out'])


def _json_items(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (TypeError, ValueError):
            pass
    return []


def _tariff_targets(tariff: dict) -> set[tuple[str, int | str]]:
    """Extract comparable channel ID and delivery URL tokens for a tariff or order snapshot."""
    targets: set[tuple[str, int | str]] = set()
    raw_ids = tariff.get('channel_ids')
    if isinstance(raw_ids, list):
        for cid in raw_ids:
            try:
                targets.add(('ch', int(cid)))
            except (TypeError, ValueError):
                pass
    elif isinstance(raw_ids, str) and raw_ids.strip():
        stripped = raw_ids.strip()
        if stripped.startswith('['):
            for cid in _json_items(stripped):
                try:
                    targets.add(('ch', int(cid)))
                except (TypeError, ValueError):
                    pass
        else:
            try:
                for cid in channels_of(stripped):
                    targets.add(('ch', int(cid)))
            except ValueError:
                for part in stripped.replace('\n', ',').split(','):
                    clean_part = part.strip()
                    if clean_part:
                        targets.add(('ch', clean_part))
    for cid in _json_items(tariff.get('channels')):
        try:
            targets.add(('ch', int(cid)))
        except (TypeError, ValueError):
            if str(cid).strip():
                targets.add(('ch', str(cid).strip()))
    for url in _json_items(tariff.get('delivery_urls')):
        clean_url = str(url or '').strip().rstrip('/')
        if clean_url:
            targets.add(('url', clean_url))
    return targets


async def _user_active_access_targets(db: Database, user_id: int) -> set[tuple[str, int | str]]:
    """Return channels and delivery links the user currently holds active access to."""
    purchases = await db.read(
        """SELECT p.id AS purchase_id, o.channels, o.delivery_urls
           FROM purchases p
           JOIN orders o ON o.id=p.order_id
           WHERE p.user_id=?
             AND p.revoked_by_admin=0
             AND o.status='paid'
             AND o.refund_requested_at IS NULL
             AND (p.expires_at IS NULL OR p.expires_at>?)""",
        (user_id, now()),
        many=True,
    )
    active: set[tuple[str, int | str]] = set()
    for purchase in purchases:
        grants = await db.read(
            'SELECT chat_id, state FROM grants WHERE purchase_id=?',
            (purchase['purchase_id'],),
            many=True,
        )
        if grants:
            for grant in grants:
                if grant['state'] not in ('revoked', 'expired'):
                    active.add(('ch', int(grant['chat_id'])))
        else:
            for cid in _json_items(purchase['channels']):
                try:
                    active.add(('ch', int(cid)))
                except (TypeError, ValueError):
                    pass
        for url in _json_items(purchase['delivery_urls']):
            clean_url = str(url or '').strip().rstrip('/')
            if clean_url:
                active.add(('url', clean_url))
    return active


def order_amount_label(order: dict) -> str:
    if order['provider'] == 'stars':
        return f"{order['amount_stars']} ⭐ (~{order['amount_rub']} ₽)"
    return f"{order['amount_rub']} ₽"


async def paying_perk_rows(shop: Shop, db: Database, user_id: int) -> list[tuple]:
    """Return buyer-only upgrade inline rows."""
    if not await shop.is_paying_user(user_id):
        return []
    rows: list[tuple] = []
    offers = await shop.upgrade_offers(user_id)
    for offer in offers:
        short_name = button_tariff_name(offer['tariff']['title'])
        promo_rub, _, promo_code = await shop.user_price(user_id, offer['tariff'])
        if promo_code and promo_rub < offer['rub']:
            rows.append((
                cb(
                    f"🎁 {short_name} по промокоду — {fmt_rub(promo_rub)} ₽ ({promo_code})",
                    f"pay_options:{offer['tariff']['id']}",
                    style='primary' if offer['kind'] == 'DIAMOND' else 'success',
                ),
            ))
            continue
        applied_credit = offer.get('saved_rub', offer['credit_rub'])
        rows.append((
            cb(
                f"⚡ Апгрейд до {short_name} — {fmt_rub(offer['rub'])} ₽ (зачёт {fmt_rub(applied_credit)} ₽)",
                f"up_pay_options:{offer['tariff']['id']}",
                style='primary' if offer['kind'] == 'DIAMOND' else 'success',
            ),
        ))
    return rows


async def dispatch_whale_alerts(bot, db: Database, config: Config | None) -> int:
    """Notify admins on VIP/high-ticket payments."""
    alerts_sent = 0
    async with _lock_for(db, '_whale_alerts_lock'):
        rows = await db.read(
            """SELECT o.*, o.rowid AS ord_rowid, COALESCE(o.paid_at, o.created_at) AS ord_ts,
                      COALESCE(t.tier, '') AS tariff_tier, u.username, u.name AS user_name
               FROM orders o
               LEFT JOIN users u ON u.user_id = o.user_id
               LEFT JOIN tariffs t ON t.id = o.tariff_id
               WHERE o.provider!='admin'
                 AND o.status='paid'
                 AND o.refund_requested_at IS NULL
                 AND o.whale_alert_sent=0
               ORDER BY COALESCE(o.paid_at, o.created_at), o.rowid
               LIMIT 25""",
            (),
            many=True,
        )
        for order in rows:
            ltv_row = await db.read(
                """SELECT COALESCE(SUM(amount_rub), 0) AS total
                   FROM orders
                   WHERE user_id=? AND status='paid' AND provider!='admin' AND refund_requested_at IS NULL
                     AND (
                         COALESCE(paid_at, created_at) < ?
                         OR (COALESCE(paid_at, created_at) = ? AND rowid <= ?)
                     )""",
                (order['user_id'], order['ord_ts'], order['ord_ts'], order['ord_rowid']),
            )
            ltv = int((ltv_row or {}).get('total') or 0)
            prev_ltv = ltv - int(order['amount_rub'])
            tier_kind = tariff_tier_kind(order['title'], order.get('tariff_tier', ''))
            reasons = []
            if tier_kind in ('ultra', 'diamond'):
                reasons.append('VIP-тариф')
            if order['amount_rub'] >= 1000:
                reasons.append('Чек ≥ 1 000 ₽')
            if prev_ltv < 1500 <= ltv:
                reasons.append('LTV ≥ 1 500 ₽')

            if not reasons:
                await db.execute('UPDATE orders SET whale_alert_sent=1 WHERE id=?', (order['id'],))
                continue
            if not config or not config.admin_ids:
                continue

            uname = '@' + esc(order['username']) if order.get('username') else 'без username'
            alert_text = (
                "🐋 <b>РАДАР КИТОВ · НОВЫЙ VIP-ПЛАТЁЖ</b>\n\n"
                f"🏷 Триггер: <b>{esc(' · '.join(reasons))}</b>\n"
                f"👤 Клиент: <b>{esc((order.get('user_name') or 'Пользователь')[:80])}</b> ({uname})\n"
                f"🆔 ID: <code>{order['user_id']}</code>\n"
                f"📦 Покупка: <b>{esc(order['title'][:120])}</b>\n"
                f"💳 Сумма платежа: <b>{order_amount_label(order)}</b>\n"
                f"💰 Суммарный LTV: <b>{ltv} ₽</b>"
            )
            markup = kb(
                (cb('👤 Профиль клиента', f"adm:user:{order['user_id']}"),
                 cb('💳 Открыть заказ', f"adm:order:{order['id']}")),
            )
            delivered_any = False
            for admin_id in config.admin_ids:
                if await safe_send(bot, admin_id, alert_text, markup):
                    delivered_any = True
            if delivered_any:
                await db.execute('UPDATE orders SET whale_alert_sent=1 WHERE id=?', (order['id'],))
                alerts_sent += 1
    return alerts_sent


async def send_expiry_upsell_if_needed(bot, db: Database, shop: Shop, purchase_id: int) -> bool:
    """Push a single upgrade DM when all grants of a temporary paid purchase finish expiring."""
    purchase = await db.read(
        """SELECT p.*, o.provider, o.status AS order_status, o.refund_requested_at, o.duration_days
           FROM purchases p JOIN orders o ON o.id=p.order_id
           WHERE p.id=?""",
        (purchase_id,),
    )
    if (not purchase or purchase['expiry_notice_sent'] or purchase['provider'] == 'admin'
            or purchase['order_status'] != 'paid' or purchase['refund_requested_at']
            or purchase.get('revoked_by_admin')):
        return False

    remaining = await db.read(
        "SELECT 1 FROM grants WHERE purchase_id=? AND state NOT IN ('expired','revoked') LIMIT 1",
        (purchase_id,),
    )
    if remaining:
        return False

    has_active_cover = await db.read(
        """SELECT 1 FROM purchases p2
           JOIN orders o2 ON o2.id=p2.order_id
           LEFT JOIN tariffs t2 ON t2.id=p2.tariff_id
           WHERE p2.user_id=? AND p2.id!=?
             AND p2.revoked_by_admin=0
             AND o2.status='paid' AND o2.refund_requested_at IS NULL
             AND (p2.expires_at IS NULL OR p2.expires_at>?)
             AND (
                 CASE LOWER(COALESCE(t2.tier, ''))
                     WHEN 'ultra' THEN 1
                     WHEN 'diamond' THEN 1
                     WHEN 'regular' THEN (
                         CASE WHEN p2.expires_at IS NULL AND (
                             UPPER(p2.title) LIKE '%BLACK%'
                             OR COALESCE(t2.rub, o2.amount_rub, 0) >= 2490
                         ) THEN 1 ELSE 0 END
                     )
                     ELSE (
                         UNICODE_UPPER(p2.title) LIKE '%ULTRA%'
                         OR UNICODE_UPPER(p2.title) LIKE '%DIAMOND%'
                         OR UNICODE_UPPER(p2.title) LIKE '%VIP MAX%'
                         OR REPLACE(UNICODE_UPPER(p2.title), 'Ё', 'Е') LIKE '%ВСЕ СРАЗУ%'
                         OR (p2.expires_at IS NULL AND (
                             UPPER(p2.title) LIKE '%BLACK%'
                             OR COALESCE(t2.rub, o2.amount_rub, 0) >= 2490
                         ))
                     )
                 END = 1
                 OR EXISTS (
                     SELECT 1 FROM grants g1 JOIN grants g2 ON g2.chat_id=g1.chat_id
                     WHERE g1.purchase_id=? AND g2.purchase_id=p2.id
                       AND g2.state NOT IN ('expired','revoked')
                 )
             )
           LIMIT 1""",
        (purchase['user_id'], purchase_id, now(), purchase_id),
    )
    if has_active_cover:
        await db.execute('UPDATE purchases SET expiry_notice_sent=1 WHERE id=? AND expiry_notice_sent=0', (purchase_id,))
        return False

    claimed = await db.execute(
        'UPDATE purchases SET expiry_notice_sent=1 WHERE id=? AND expiry_notice_sent=0',
        (purchase_id,),
    )
    if not claimed:
        return False

    offers = await shop.upgrade_offers(purchase['user_id'])
    offer = next((item for item in offers if item['kind'] == 'ULTRA'), offers[0] if offers else None)
    if offer:
        applied_credit = offer.get('saved_rub', offer['credit_rub'])
        expired_heading = (
            '⏳ <b>Твой 24ч доступ закрыт:</b>'
            if purchase.get('duration_days') == 1
            else '⏳ <b>Срок твоего доступа завершён:</b>'
        )
        text = (
            f"{expired_heading}\n{esc(purchase['title'])}\n\n"
            f"🔥 Забирай <b>{esc(offer['tariff']['title'])}</b> <b>НАВСЕГДА</b> — "
            f"мы уже зачли оплаченные тобой <b>{fmt_rub(applied_credit)} ₽</b>.\n"
            f"💳 Персональная доплата по Trade-In: <b>{fmt_rub(offer['rub'])} ₽</b> "
            f"(вместо {fmt_rub(offer['tariff']['rub'])} ₽)."
        )
        markup = kb((cb(f"⚡ Забрать навсегда за {fmt_rub(offer['rub'])} ₽", f"up_pay_options:{offer['tariff']['id']}", style='success'),))
    else:
        text = (
            f"⏳ <b>Срок твоего доступа завершён:</b>\n{esc(purchase['title'])}\n\n"
            "🔓 Хочешь вернуть доступ без ограничений по времени? Открой каталог и выбери постоянный тариф 👇"
        )
        markup = kb((cb('🔐 Открыть каталог', 'catalog', style='success'),))
    return await send_campaign(bot, db, purchase['user_id'], 'expired', text, markup)


async def _find_entry_tariff(shop: Shop) -> dict | None:
    """Return the cheapest deliverable short trial, falling back to the cheapest deliverable tariff."""
    deliverable = [
        t for t in await shop.catalogs()
        if t['channel_ids'] or t['delivery_urls'] != '[]'
    ]
    if not deliverable:
        return None
    trial = next((t for t in deliverable if 1 <= t['duration_days'] <= 3), None)
    return trial or min(deliverable, key=lambda t: (t['rub'], t['stars'], t['id']))


async def _find_trial_tariff(shop: Shop) -> dict | None:
    return await _find_entry_tariff(shop)


def _find_ultra_tariff(tariffs: list[dict]) -> dict | None:
    return next(
        (t for t in tariffs
         if tariff_tier_kind(t['title'], t.get('tier', '')) == 'ultra'
         and (t['channel_ids'] or t['delivery_urls'] != '[]')),
        None,
    )


def _upgrade_offer_buttons(offers: list[dict]) -> list[tuple]:
    rows = []
    for item in offers:
        title = button_tariff_name(item['tariff']['title'], item['tariff']['title'], max_len=26)
        if item['kind'] == 'ULTRA':
            label = f"🔥 Апгрейд до «{title}» · Доплатить {fmt_rub(item['rub'])} ₽"
        else:
            emoji = '👑' if item['kind'] == 'DIAMOND' else '⚡'
            label = f"{emoji} {title} · Доплатить {fmt_rub(item['rub'])} ₽"
        rows.append((cb(label, f"up_pay_options:{item['tariff']['id']}",
                        style='success' if item['kind'] == 'ULTRA' else 'primary'),))
    return rows


async def dispatch_abandoned_cart_reminders(
    bot, db: Database, shop: Shop, *, min_age_seconds: int = 240, limit: int = 10
) -> int:
    """Remind first-time users after 4 minutes if their checkout stayed unpaid."""
    min_age = in_seconds(-min_age_seconds)
    max_age = in_seconds(-4 * 3600)
    day_cutoff = in_seconds(-86400)
    sent = 0
    async with _lock_for(db):
        rows = await db.read(
            """SELECT o.*
               FROM orders o
               JOIN users u ON u.user_id=o.user_id
               WHERE COALESCE(u.broadcast_opt_out, 0)=0
                 AND o.id = (
                   SELECT o2.id FROM orders o2
                   WHERE o2.user_id=o.user_id
                     AND o2.status IN ('pending', 'expired')
                     AND o2.provider IN ('stars', 'cryptopay', '2328')
                     AND o2.order_kind IN ('tariff', 'upgrade')
                     AND o2.cart_reminder_sent=0
                     AND o2.created_at BETWEEN ? AND ?
                   ORDER BY o2.created_at DESC, o2.rowid DESC
                   LIMIT 1
               )
               AND NOT EXISTS (
                   SELECT 1 FROM orders p
                   WHERE p.user_id=o.user_id AND p.status='paid'
                     AND p.provider!='admin' AND p.refund_requested_at IS NULL
               )
               AND NOT EXISTS (
                   SELECT 1 FROM orders r
                   WHERE r.user_id=o.user_id AND r.cart_reminder_sent=1
                     AND r.created_at>=?
               )
               ORDER BY o.created_at ASC
               LIMIT ?""",
            (max_age, min_age, day_cutoff, limit),
            many=True,
        )
        if not rows:
            return 0
        catalogs = await shop.catalogs()
        entry = await _find_entry_tariff(shop)
        ultra = _find_ultra_tariff(catalogs)
        for order in rows:
            if await _is_broadcast_opted_out(db, order['user_id']):
                continue
            claimed = await db.execute(
                "UPDATE orders SET cart_reminder_sent=1 WHERE user_id=? AND status IN ('pending', 'expired') AND cart_reminder_sent=0",
                (order['user_id'],),
            )
            if not claimed:
                continue
            buttons: list[tuple] = []
            promo_note = ''
            quick_promo = None
            if order['tariff_id']:
                pay_cb = (
                    f"up_pay_options:{order['tariff_id']}"
                    if order.get('order_kind') == 'upgrade' else f"pay_options:{order['tariff_id']}"
                )
                disc_rub = order['amount_rub']
                if (not order['promo_code'] and order.get('order_kind') != 'upgrade'
                        and not await shop.user_promo(order['user_id'])):
                    quick_promo, _, _ = await available_quick_promo(db, order['user_id'])
                    if quick_promo:
                        disc_rub, _ = price_with_promo(
                            order['amount_rub'], order['amount_stars'],
                            quick_promo['discount'], int(quick_promo.get('discount_percent') or 0),
                        )
                if quick_promo and disc_rub < order['amount_rub']:
                    d_label = promo_discount_label(quick_promo)
                    promo_note = (
                        f"\n\n🎁 <b>Скидка {d_label} по коду <code>{esc(quick_promo['code'])}</code>:</b> "
                        f"итого <b>{fmt_rub(disc_rub)} ₽</b> вместо {fmt_rub(order['amount_rub'])} ₽."
                    )
                    buttons.append((cb(
                        f"🎁 Завершить оплату со скидкой · {fmt_rub(disc_rub)} ₽",
                        f"claim_pay:{order['tariff_id']}:{quick_promo['code']}",
                        style='success',
                    ),))
                else:
                    buttons.append((cb(f"💳 Завершить оплату · {fmt_rub(order['amount_rub'])} ₽", pay_cb, style='success'),))
            entry_rub = (await shop.user_price(order['user_id'], entry))[0] if entry else 0
            entry_cb = f"pay_options:{entry['id']}" if entry else ''
            if entry and quick_promo:
                entry_disc_rub, _ = price_with_promo(
                    entry_rub, entry['stars'],
                    quick_promo['discount'], int(quick_promo.get('discount_percent') or 0),
                )
                if entry_disc_rub < entry_rub:
                    entry_rub = entry_disc_rub
                    entry_cb = f"claim_pay:{entry['id']}:{quick_promo['code']}"
            has_entry = bool(entry and entry['id'] != order['tariff_id'] and entry_rub < order['amount_rub'])
            if has_entry:
                entry_name = button_tariff_name(entry['title'], 'пропуск')
                buttons.append((cb(f"⚡ Или взять «{entry_name}» · {fmt_rub(entry_rub)} ₽", entry_cb, style='primary'),))
            buttons.append((cb('📂 Вернуться в каталог', 'catalog'),))
            text = (
                f"👀 <b>Ссылка в «{esc(order['title'])}» уже сформирована</b>\n"
                "Осталось подтвердить вход. Оплата строго разовая — <b>никаких автосписаний и скрытых подписок</b>.\n"
                "⚡ Персональное приглашение в закрытый канал появится в этом чате сразу после оплаты."
                f"{promo_note}"
            )
            if await send_campaign(bot, db, order['user_id'], 'cart', text, kb(*buttons)):
                sent += 1
    return sent


def _ultra_upsell_button(ultra: dict, ultra_rub: int, diff: int | None = None,
                         *, callback_data: str | None = None) -> tuple:
    """Format an upsell button dynamically from the Ultra tariff's title, duration, and channel count."""
    u_name = button_tariff_name(ultra.get('title'), 'VIP')
    u_term = duration_phrase(ultra.get('duration_days'))
    u_ch = tariff_channel_count(ultra)
    scope = plural_channels(u_ch) if u_ch > 1 else u_term
    if diff is not None and diff <= 550:
        label = f"⭐️ Или {u_name} ({scope}, +{fmt_rub(diff)} ₽)" if u_ch > 1 else f"⭐️ Или {u_name} (+{fmt_rub(diff)} ₽)"
    elif diff is not None:
        label = f"⭐️ {u_name} · {u_term} · {fmt_rub(ultra_rub)} ₽"
    else:
        label = f"🔥 Забрать {u_name} ({scope}) · {fmt_rub(ultra_rub)} ₽"
    return (cb(label, callback_data or f"tariff:{ultra['id']}", style='primary'),)


async def dispatch_tariff_view_reminders(
    bot,
    db: Database,
    shop: Shop,
    *,
    min_age_seconds: int = 720,
    limit: int = 20,
) -> int:
    """Follow up with users who viewed a tariff card 12m+ ago without starting checkout."""
    sent = 0
    async with _lock_for(db):
        min_age = in_seconds(-min_age_seconds)
        max_age = in_seconds(-24 * 3600)
        rows = await db.read(
            """SELECT u.user_id, u.last_viewed_tariff_id, u.last_viewed_at,
                      t.title AS tariff_title, t.rub AS tariff_rub,
                      t.stars AS tariff_stars, t.tier AS tariff_tier
               FROM users u
               JOIN tariffs t ON t.id=u.last_viewed_tariff_id AND t.active=1
               WHERE u.view_reminder_sent=0
                 AND COALESCE(u.broadcast_opt_out, 0)=0
                 AND u.last_viewed_tariff_id IS NOT NULL
                 AND u.last_viewed_at BETWEEN ? AND ?
                 AND NOT EXISTS (
                     SELECT 1 FROM orders p
                     WHERE p.user_id=u.user_id
                       AND (p.status IN ('paid', 'review')
                            OR p.created_at>=u.last_viewed_at
                            OR (p.status='pending' AND p.order_kind IN ('tariff', 'upgrade') AND p.created_at>=?)
                            OR (p.cart_reminder_sent=1 AND p.created_at>=?))
                 )
               ORDER BY u.last_viewed_at ASC
               LIMIT ?""",
            (max_age, min_age, in_seconds(-4 * 3600), max_age, limit),
            many=True,
        )
        if not rows:
            return 0
        catalogs = await shop.catalogs()
        entry = await _find_entry_tariff(shop)
        ultra = _find_ultra_tariff(catalogs)
        for row in rows:
            if await _is_broadcast_opted_out(db, row['user_id']):
                continue
            claimed = await db.execute(
                'UPDATE users SET view_reminder_sent=1, view_reminder_sent_at=? WHERE user_id=? AND view_reminder_sent=0 AND last_viewed_at<=?',
                (now(), row['user_id'], min_age),
            )
            if not claimed:
                continue
            tid = row['last_viewed_tariff_id']
            viewed_rub, viewed_stars, active_code = await shop.user_price(
                row['user_id'],
                {'id': tid, 'rub': row['tariff_rub'], 'stars': row['tariff_stars'],
                 'title': row['tariff_title'], 'tier': row['tariff_tier']},
            )
            entry_rub = (await shop.user_price(row['user_id'], entry))[0] if entry else 0
            viewed_name = button_tariff_name(row['tariff_title'], 'тариф', max_len=26)
            buttons: list[tuple] = []
            promo_note = ''
            quick_promo = None
            disc_rub = viewed_rub
            if not active_code and viewed_rub:
                quick_promo, _, _ = await available_quick_promo(db, row['user_id'])
                if quick_promo:
                    disc_rub, _ = price_with_promo(
                        viewed_rub, viewed_stars,
                        quick_promo['discount'], int(quick_promo.get('discount_percent') or 0),
                    )
            if quick_promo and disc_rub < viewed_rub:
                d_label = promo_discount_label(quick_promo)
                promo_note = (
                    f"\n\n🎁 <b>Скидка {d_label} по коду <code>{esc(quick_promo['code'])}</code>:</b> "
                    f"доступ выйдет <b>{fmt_rub(disc_rub)} ₽</b> вместо {fmt_rub(viewed_rub)} ₽."
                )
                buttons.append((cb(
                    f"🎁 Забрать со скидкой {d_label} · {fmt_rub(disc_rub)} ₽",
                    f"claim_pay:{tid}:{quick_promo['code']}",
                    style='success',
                ),))
            else:
                buttons.append((cb(
                    f"💳 Забрать «{viewed_name}» · {fmt_rub(viewed_rub)} ₽",
                    f'pay_options:{tid}',
                    style='success',
                ),))
            if ultra and ultra['id'] != tid:
                ultra_rub = (await shop.user_price(row['user_id'], ultra))[0]
                buttons.append((cb(
                    f"🔥 Или «{button_tariff_name(ultra['title'])}» · {fmt_rub(ultra_rub)} ₽",
                    f"pay_options:{ultra['id']}",
                    style='primary',
                ),))
            buttons.append((cb('📂 Открыть каталог', 'catalog'),))
            text = (
                f"🔥 <b>В «{esc(row['tariff_title'])}» уже выложены сегодняшние новинки</b>\n"
                "Все ролики в HD и со звуком — без цензуры, смайлов и переходов по сторонним сайтам.\n"
                "• Ссылка в канал приходит сразу после оплаты\n"
                "• Разовый доступ — без скрытых списаний"
                f"{promo_note}"
            )
            if await send_campaign(bot, db, row['user_id'], 'view', text, kb(*buttons)):
                sent += 1
    return sent


async def dispatch_trial_upsell_touchpoints(
    bot,
    db: Database,
    shop: Shop,
    *,
    post_join_delay_seconds: int = 1200,
    pre_expiry_window_seconds: int = 10800,
    limit: int = 20,
) -> int:
    """Send 20m post-join upgrade push for entry/trial buyers and 3h pre-expiry alert."""
    sent = 0
    async with _lock_for(db):
        current = now()
        joined_cutoff = in_seconds(-post_join_delay_seconds)
        pre_expiry_cutoff = in_seconds(pre_expiry_window_seconds)

        # 1) ~20 minutes after joining/receiving an entry or trial tariff without flagship tier yet
        post_join_rows = await db.read(
            """SELECT p.id, p.user_id, p.title, p.expires_at,
                      o.channels AS order_channels, o.delivery_urls AS order_urls
               FROM purchases p
               JOIN users u ON u.user_id=p.user_id
               JOIN orders o ON o.id=p.order_id
               LEFT JOIN tariffs t ON t.id=p.tariff_id
               WHERE COALESCE(u.broadcast_opt_out, 0)=0
                 AND o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL
                 AND o.order_kind='tariff'
                 AND p.revoked_by_admin=0
                 AND p.post_join_upsell_sent=0
                 AND LOWER(COALESCE(t.tier, '')) NOT IN ('ultra', 'diamond')
                 AND UPPER(p.title) NOT LIKE '%ULTRA%'
                 AND UPPER(p.title) NOT LIKE '%DIAMOND%'
                 AND UPPER(p.title) NOT LIKE '%BLACK%'
                 AND UNICODE_UPPER(p.title) NOT LIKE '%VIP MAX%'
                 AND REPLACE(UNICODE_UPPER(p.title), 'Ё', 'Е') NOT LIKE '%ВСЕ СРАЗУ%'
                 AND (
                     (o.duration_days BETWEEN 1 AND 3 AND p.expires_at IS NOT NULL AND p.expires_at>?)
                     OR (COALESCE(t.rub, o.amount_rub) < 1000 AND (p.expires_at IS NULL OR p.expires_at>?))
                 )
                 AND (
                     EXISTS (
                         SELECT 1 FROM grants g
                         WHERE g.purchase_id=p.id AND g.state='joined'
                           AND g.joined_at IS NOT NULL AND g.joined_at<=?
                     )
                     OR (o.delivery_urls!='[]' AND p.started_at<=?)
                 )
               ORDER BY p.id ASC
               LIMIT ?""",
            (pre_expiry_cutoff, pre_expiry_cutoff, joined_cutoff, joined_cutoff, limit),
            many=True,
        )
        for row in post_join_rows:
            if await _is_broadcast_opted_out(db, row['user_id']):
                continue
            claimed = await db.execute(
                'UPDATE purchases SET post_join_upsell_sent=1 WHERE id=? AND post_join_upsell_sent=0',
                (row['id'],),
            )
            if not claimed:
                continue
            offers = await shop.upgrade_offers(row['user_id'])
            if not offers:
                continue
            top = next((item for item in offers if item['kind'] == 'ULTRA'), offers[0])
            credit = top.get('saved_rub', top['credit_rub'])
            top_title = esc(top['tariff']['title'])
            top_base_rub = top['tariff'].get('rub', top['rub'] + credit)
            text = (
                f"⚡ <b>Все {fmt_rub(credit)} ₽ сохранены на твоём балансе</b>\n"
                f"Если решишь забрать <b>«{top_title}» навсегда</b>, эта сумма зачтётся в полном объёме — "
                f"доплатишь всего <b>{fmt_rub(top['rub'])} ₽</b> вместо {fmt_rub(top_base_rub)} ₽.\n"
                "Все каналы резерва останутся у тебя навсегда без каких-либо доплат.\n"
                "👇 <b>Забрать доступ навсегда:</b>"
            )
            if await send_campaign(bot, db, row['user_id'], 'post_join', text, kb(*_upgrade_offer_buttons(offers))):
                sent += 1

        # 2) ~3 hours BEFORE pass/trial expiry (Loss Aversion)
        pre_expiry_rows = await db.read(
            """SELECT p.id, p.user_id, p.title
               FROM purchases p
               JOIN users u ON u.user_id=p.user_id
               JOIN orders o ON o.id=p.order_id
               LEFT JOIN tariffs t ON t.id=p.tariff_id
               WHERE COALESCE(u.broadcast_opt_out, 0)=0
                 AND o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL
                 AND o.order_kind='tariff'
                 AND o.duration_days BETWEEN 1 AND 3
                 AND p.revoked_by_admin=0
                 AND p.pre_expiry_upsell_sent=0
                 AND p.expires_at IS NOT NULL AND p.expires_at>? AND p.expires_at<=?
               ORDER BY p.expires_at ASC
               LIMIT ?""",
            (current, pre_expiry_cutoff, limit),
            many=True,
        )
        for row in pre_expiry_rows:
            if await _is_broadcast_opted_out(db, row['user_id']):
                continue
            claimed = await db.execute(
                'UPDATE purchases SET pre_expiry_upsell_sent=1 WHERE id=? AND pre_expiry_upsell_sent=0',
                (row['id'],),
            )
            if not claimed:
                continue
            offers = await shop.upgrade_offers(row['user_id'])
            if not offers:
                continue
            top = next((item for item in offers if item['kind'] == 'ULTRA'), offers[0])
            text = (
                f"⏳ <b>Твой пропуск в {esc(row['title'])} истекает через 3 часа</b>\n"
                "После этого ссылка будет деактивирована.\n"
                f"💡 Чтобы не терять доступ и открыть остальные 3 канала со скидкой за прошлую оплату — "
                f"обновись до <b>«{esc(top['tariff']['title'])}» навсегда</b>:"
            )
            if await send_campaign(bot, db, row['user_id'], 'pre_expiry', text, kb(*_upgrade_offer_buttons(offers))):
                sent += 1
    return sent


async def dispatch_cold_start_nudges(
    bot,
    db: Database,
    shop: Shop,
    *,
    min_age_seconds: int = 1200,
    max_age_seconds: int = 3 * 3600,
    limit: int = 15,
) -> int:
    """Nudge cold /start leads who opened zero tariff cards after 20 minutes while impulse is still warm."""
    min_age = in_seconds(-min_age_seconds)
    max_age = in_seconds(-max_age_seconds)
    sent = 0
    async with _lock_for(db):
        rows = await db.read(
            """SELECT u.user_id, u.name
               FROM users u
               WHERE COALESCE(u.broadcast_opt_out, 0)=0
                 AND COALESCE(u.cold_nudge_sent, 0)=0
                 AND COALESCE(u.promo_push_sent, 0)=0
                 AND u.last_viewed_tariff_id IS NULL
                 AND u.first_start_at IS NOT NULL
                 AND u.first_start_at BETWEEN ? AND ?
                 AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id)
               ORDER BY u.first_start_at ASC, u.user_id
               LIMIT ?""",
            (max_age, min_age, limit),
            many=True,
        )
        if not rows:
            return 0
        catalogs = await shop.catalogs()
        entry = await _find_entry_tariff(shop)
        ultra = _find_ultra_tariff(catalogs)
        if not entry and not ultra:
            return 0
        for row in rows:
            if await _is_broadcast_opted_out(db, row['user_id']):
                continue
            claimed = await db.execute(
                """UPDATE users SET cold_nudge_sent=1
                   WHERE user_id=? AND COALESCE(cold_nudge_sent, 0)=0 AND last_viewed_tariff_id IS NULL""",
                (row['user_id'],),
            )
            if not claimed:
                continue
            buttons: list[tuple] = []
            lines = ['👋 <b>Не определился с разделом?</b>', 'Вот варианты доступа:']
            featured = {tariff['id']: tariff for tariff in (entry, ultra) if tariff}
            for tariff in featured.values():
                rub, _, _ = await shop.user_price(row['user_id'], tariff)
                name = button_tariff_name(tariff['title'])
                label = f"{name} · {fmt_rub(rub)} ₽"
                style = 'success' if tariff is ultra else None
                buttons.append((cb(label, f"pay_options:{tariff['id']}", style=style),))
                lines.append(f"• <b>{esc(tariff['title'])}</b> — {fmt_rub(rub)} ₽")
            buttons.append((cb('📂 Открыть полный каталог', 'catalog'),))
            text = '\n'.join(lines)
            if await send_campaign(bot, db, row['user_id'], 'cold_nudge', text, kb(*buttons)):
                sent += 1
    return sent
