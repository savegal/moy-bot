"""Acquisition funnel and product reports backed by recorded Telegram events."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from time import time

from aiogram.types import InlineKeyboardMarkup

from .db import Database, in_seconds, now

TOUCH_WINDOW_DAYS = 30
START_TOKEN = re.compile(r'g_([a-f0-9]{32})\Z')


def latest_greeting_touch(con, user_id: int):
    return con.execute("""SELECT id,engaged_at FROM greeting_events WHERE user_id=? AND sent_at IS NOT NULL
        AND COALESCE(touched_at,engaged_at,sent_at)>=?
        ORDER BY (engaged_at IS NOT NULL) DESC,COALESCE(touched_at,engaged_at,sent_at) DESC,
            requested_at DESC,rowid DESC LIMIT 1""",
        (user_id, in_seconds(-TOUCH_WINDOW_DAYS * 86400))).fetchone()


async def record_engagement(db: Database, user_id: int, payload: str = ''):
    match = START_TOKEN.fullmatch(payload)
    timestamp = now()
    touched_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    if match:
        # Receiving the private link also proves delivery if a click races the send acknowledgement.
        await db.execute("""UPDATE greeting_events SET sent_at=COALESCE(sent_at,?),
            engaged_at=COALESCE(engaged_at,?),touched_at=? WHERE id=? AND user_id=?
            AND (sent_at IS NOT NULL OR requested_at>=?)""",
            (timestamp, timestamp, touched_at, match[1], user_id, int(time()) - TOUCH_WINDOW_DAYS * 86400))
        return

    def record(con):
        candidate = latest_greeting_touch(con, user_id)
        if candidate and not candidate['engaged_at']:
            con.execute('UPDATE greeting_events SET engaged_at=?,touched_at=? WHERE id=?',
                        (timestamp, touched_at, candidate['id']))
    await db.atomic(record)


async def funnel_summary(db: Database, days: int, chat_id: int | None = None):
    cutoff = int(time()) - days * 86400
    return await db.read("""WITH cohort AS (
        SELECT e.user_id,MAX(e.sent_at IS NOT NULL) AS delivered,MAX(e.engaged_at IS NOT NULL) AS engaged,
            MAX(e.sent_at IS NOT NULL AND EXISTS (
                SELECT 1 FROM orders o WHERE o.greeting_event_id=e.id AND o.status='paid'
                    AND o.provider!='admin' AND o.refund_requested_at IS NULL)) AS bought,
            MAX(e.engaged_at IS NOT NULL AND EXISTS (
                SELECT 1 FROM orders o WHERE o.greeting_event_id=e.id AND o.status='paid'
                    AND o.provider!='admin' AND o.refund_requested_at IS NULL AND o.created_at>=e.engaged_at)) AS engaged_bought
        FROM greeting_events e WHERE requested_at>=? AND (? IS NULL OR chat_id=?)
        GROUP BY e.user_id
    )
    SELECT COUNT(*) AS requested,COALESCE(SUM(delivered),0) AS delivered,
        COALESCE(SUM(engaged),0) AS engaged,COALESCE(SUM(bought),0) AS bought,
        COALESCE(SUM(engaged_bought),0) AS engaged_bought FROM cohort""", (cutoff, chat_id, chat_id))


async def product_sales(db: Database, days: int, limit: int, offset: int):
    return await db.read("""SELECT t.id,t.title,COUNT(o.id) AS sales,COUNT(DISTINCT o.user_id) AS buyers,
        SUM(CASE WHEN o.provider='stars' THEN o.amount_stars ELSE 0 END) AS stars,
        SUM(CASE WHEN o.provider!='stars' THEN o.amount_rub ELSE 0 END) AS rub
        FROM orders o JOIN tariffs t ON t.id=o.tariff_id
        WHERE o.status='paid' AND o.provider!='admin' AND o.refund_requested_at IS NULL AND o.paid_at>=?
        GROUP BY t.id,t.title ORDER BY sales DESC,t.id LIMIT ? OFFSET ?""",
        (in_seconds(-days * 86400), limit, offset), many=True)

CAMPAIGNS = {
    'promo': '🎁 Пуш с промокодом',
    'promo_exp': '⏳ Сгорание промокода',
    'winback': '🔥 Вторая волна (Winback)',
    'cold_nudge': '⚡ Старт без выбора',
    'review': '⭐ Пуш с отзывом',
    'cart': '🛒 Брошенная оплата',
    'view': '👀 Просмотр товара',
    'post_join': '⬆️ Апгрейд после входа',
    'pre_expiry': '⏳ Дожим перед окончанием',
    'expired': '🔄 Дожим после окончания',
}
CAMPAIGN_WINDOW_DAYS = 7
CAMPAIGN_TARGET = (
    r'(?:catalog|tariff:[0-9]{1,16}|pay_options:[0-9]{1,16}|up_pay_options:[0-9]{1,16}'
    r'|claim_promo:[A-Z0-9_]{1,28}|claim_tariff:[0-9]{1,16}:[A-Z0-9_]{1,28}'
    r'|claim_pay:[0-9]{1,16}:[A-Z0-9_]{1,28})'
)
CAMPAIGN_CALLBACK = re.compile(rf'd:([0-9]{{1,19}}):({CAMPAIGN_TARGET})\Z')


async def prepare_campaign(db: Database, user_id: int, campaign: str, markup: InlineKeyboardMarkup):
    def create(con):
        return con.execute("""INSERT INTO campaign_touches (user_id,campaign,prepared_at)
            VALUES(?,?,?)""", (user_id, campaign, now())).lastrowid

    touch_id = await db.atomic(create)
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for button in row:
            if button.callback_data and re.fullmatch(CAMPAIGN_TARGET, button.callback_data):
                callback = f'd:{touch_id}:{button.callback_data}'
                if len(callback.encode('utf-8')) <= 64:
                    button = button.model_copy(update={'callback_data': callback})
            buttons.append(button)
        rows.append(buttons)
    return touch_id, markup.model_copy(update={'inline_keyboard': rows})


async def campaign_delivered(db: Database, touch_id: int):
    await db.execute('UPDATE campaign_touches SET sent_at=COALESCE(sent_at,prepared_at) WHERE id=?', (touch_id,))


async def campaign_click(db: Database, user_id: int, touch_id: int):
    timestamp = now()
    return await db.execute("""UPDATE campaign_touches SET sent_at=COALESCE(sent_at,prepared_at),
        clicked_at=CASE WHEN prepared_at>=? THEN ? ELSE clicked_at END WHERE id=? AND user_id=?""",
        (in_seconds(-CAMPAIGN_WINDOW_DAYS * 86400), timestamp, touch_id, user_id))


async def attribute_campaign_order(db: Database, order_id: str):
    cutoff = in_seconds(-CAMPAIGN_WINDOW_DAYS * 86400)
    await db.execute("""INSERT INTO campaign_orders(order_id,touch_id,clicked_at)
        SELECT o.id,c.id,c.clicked_at FROM orders o JOIN campaign_touches c ON c.user_id=o.user_id
        WHERE o.id=? AND o.status='pending' AND o.refund_requested_at IS NULL
          AND c.clicked_at>=? AND c.prepared_at>=?
        ORDER BY c.clicked_at DESC,c.id DESC LIMIT 1
        ON CONFLICT(order_id) DO UPDATE SET touch_id=excluded.touch_id,clicked_at=excluded.clicked_at""",
        (order_id, cutoff, cutoff))


async def campaign_summary(db: Database, days: int):
    return await db.read("""SELECT c.campaign,
        COUNT(DISTINCT c.user_id) AS sent,
        COUNT(DISTINCT CASE WHEN c.clicked_at IS NOT NULL THEN c.user_id END) AS clicked,
        COUNT(DISTINCT o.user_id) AS buyers, COUNT(DISTINCT o.id) AS orders,
        COALESCE(SUM(CASE WHEN o.provider='stars' THEN o.amount_stars END),0) AS stars,
        COALESCE(SUM(CASE WHEN o.provider='cryptopay' THEN o.amount_rub END),0) AS rub
        FROM campaign_touches c
        LEFT JOIN campaign_orders a ON a.touch_id=c.id
        LEFT JOIN orders o ON o.id=a.order_id AND o.status='paid' AND o.refund_requested_at IS NULL
          AND o.provider IN ('stars','cryptopay') AND o.paid_at>=a.clicked_at
          AND julianday(o.paid_at)-julianday(c.sent_at)<=?
        WHERE c.sent_at>=? GROUP BY c.campaign""",
        (CAMPAIGN_WINDOW_DAYS, in_seconds(-days * 86400)), many=True)


async def checkout_summary(db: Database, days: int):
    cutoff = in_seconds(-days * 86400)
    return await db.read("""WITH started AS (
        SELECT user_id, first_start_at FROM users WHERE first_start_at>=?
    ), checkout AS (
        SELECT o.user_id, MAX(o.status='paid' AND o.refund_requested_at IS NULL) AS bought
        FROM orders o JOIN started u ON u.user_id=o.user_id
        WHERE o.created_at>=u.first_start_at AND o.provider IN ('stars','cryptopay')
          AND (o.payment_url!='' OR o.external_id!='' OR o.checkout_id!='' OR o.paid_at IS NOT NULL)
        GROUP BY o.user_id
    ) SELECT (SELECT COUNT(*) FROM started) AS started, COUNT(*) AS checkout,
        COALESCE(SUM(bought),0) AS bought FROM checkout""", (cutoff,))
