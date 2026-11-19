"""Retry payment reconciliation and access jobs independently."""
from __future__ import annotations

import asyncio
import logging
from time import time

from aiogram import Bot
from aiogram.types import TransactionPartnerUser

from .alerts import (
    dispatch_abandoned_cart_reminders,
    dispatch_cold_start_nudges,
    dispatch_tariff_view_reminders,
    dispatch_trial_upsell_touchpoints,
    dispatch_whale_alerts,
)
from .pushes import (
    dispatch_promo_expiry_reminders,
    dispatch_promo_pushes,
    dispatch_review_pushes,
    dispatch_winback_pushes,
)
from .config import Config
from .db import Database, in_seconds, now
from .service import Shop, ShopError
from .access import AccessManager
from .payments import CryptoPay, GatewayError, Payments, invoice_identifier

logger = logging.getLogger(__name__)


async def reconcile_star_page(bot: Bot, shop: Shop, offset: int, limit: int = 100) -> int:
    ledger = await bot.get_star_transactions(offset=offset, limit=limit, request_timeout=15)
    # Close refunds before settling receipts from the same batch, so an
    # already-refunded order cannot reach the access worker in between.
    for entry in ledger.transactions:
        receiver = entry.receiver
        if (not entry.nanostar_amount and entry.amount and isinstance(receiver, TransactionPartnerUser)
                and receiver.transaction_type == 'invoice_payment'):
            await shop.refund_stars(entry.id, abs(entry.amount), user_id=receiver.user.id,
                                    order_id=receiver.invoice_payload)
    for entry in reversed(ledger.transactions):
        if entry.nanostar_amount:
            continue
        source = entry.source
        if (entry.amount > 0 and isinstance(source, TransactionPartnerUser)
                and source.transaction_type == 'invoice_payment' and source.invoice_payload):
            try:
                await shop.settle_stars(source.invoice_payload, source.user.id, entry.amount, entry.id,
                                        paid_at=entry.date)
            except ShopError as exc:
                logger.warning('Unmatched Stars invoice %r: %s', source.invoice_payload, exc)
    return len(ledger.transactions)


async def _probe_star_entry_id(bot: Bot, offset: int) -> tuple[bool, str | None]:
    """Fetch a single entry at offset to test whether the chronological ledger extends that far."""
    ledger = await bot.get_star_transactions(offset=offset, limit=1, request_timeout=15)
    txs = getattr(ledger, 'transactions', None) or []
    if not txs:
        return False, None
    return True, getattr(txs[0], 'id', None)


async def _find_star_ledger_end(bot: Bot, known_offset: int, limit: int) -> int | None:
    """Locate total transaction count in Telegram's oldest-first ledger via bounded binary search."""
    has_next, next_id = await _probe_star_entry_id(bot, known_offset + limit)
    if not has_next:
        return known_offset + limit
    _, ref_id = await _probe_star_entry_id(bot, known_offset)
    if next_id is not None and next_id == ref_id:
        # Mock ignores offset and returns a fixed page.
        return None
    prev_id = next_id
    lo = known_offset + limit + 1
    probe_step = limit
    hi = lo + probe_step
    max_probe = lo + 1_000_000
    while hi <= max_probe:
        exists, entry_id = await _probe_star_entry_id(bot, hi)
        if not exists:
            break
        if entry_id is not None and entry_id == prev_id:
            return None
        prev_id = entry_id
        lo = hi + 1
        probe_step *= 2
        hi = lo + probe_step
    else:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        exists, _ = await _probe_star_entry_id(bot, mid)
        if exists:
            lo = mid + 1
        else:
            hi = mid
    return lo


async def reconcile_stars(bot: Bot, db: Database, shop: Shop):
    """Reconcile recent Telegram Stars transactions without re-scanning old pages from 0."""
    limit = 100
    overlap = 20
    step = limit - overlap
    cursor = await db.setting('stars_ledger_offset')
    try:
        saved_offset = max(0, int((cursor or {}).get('value') or 0))
    except (TypeError, ValueError):
        saved_offset = 0

    offset = saved_offset
    count = await reconcile_star_page(bot, shop, offset, limit)
    if offset > 0 and count == 0:
        offset = max(0, offset - step)
        count = await reconcile_star_page(bot, shop, offset, limit)
        if count == 0 and offset > 0:
            offset = 0
            count = await reconcile_star_page(bot, shop, 0, limit)

    steps = 0
    while count == limit and steps < 2:
        steps += 1
        offset += step
        count = await reconcile_star_page(bot, shop, offset, limit)

    # Read the newest tail page separately when there is a backlog so new payments
    # settle right away while the cursor only advances over contiguously processed pages.
    if count == limit:
        total = await _find_star_ledger_end(bot, offset, limit)
        if total is not None:
            tail_offset = max(0, total - (limit - 10))
            if tail_offset > offset:
                await reconcile_star_page(bot, shop, tail_offset, limit)

    if offset == 0 and count < step:
        next_offset = 0
    else:
        next_offset = offset + max(0, count - overlap)
    if next_offset != saved_offset:
        await db.set_setting('stars_ledger_offset', str(next_offset))


async def recover_crypto_invoices(db: Database, payments: Payments, crypto: CryptoPay):
    missing = """provider='cryptopay' AND external_id='' AND refund_requested_at IS NULL
        AND status IN ('pending','expired')"""
    recent_cutoff = in_seconds(-3 * 86400)
    current_ts = now()
    if not await db.read(
        f"""SELECT 1 FROM orders
            WHERE {missing}
              AND (created_at>=? OR ((payment_url!='' OR invoice_sending_at IS NOT NULL)
                                     AND (reconcile_at IS NULL OR reconcile_at<=?)))
            LIMIT 1""",
        (recent_cutoff, current_ts),
    ):
        return

    async def process_page(items: list, expected_status: str) -> None:
        for invoice in items:
            if not isinstance(invoice, dict) or not isinstance(invoice.get('payload'), str):
                continue
            order = await db.read(f'SELECT * FROM orders WHERE id=? AND {missing}', (invoice['payload'],))
            if not order or invoice.get('status') != expected_status or not crypto.matches(order, invoice):
                continue
            try:
                await payments.sync_crypto_invoice(order, invoice, invoice_identifier(invoice.get('invoice_id')))
            except (GatewayError, ShopError):
                logger.warning('Could not bind Crypto Pay invoice to order %s', order['id'], exc_info=True)

    cursor = await db.setting('crypto_paid_invoice_offset')
    try:
        offset = max(0, int(cursor['value'])) if cursor else 0
    except (TypeError, ValueError):
        offset = 0
    scan_flag = await db.setting('crypto_paid_scan_from_zero')
    from_zero = offset == 0 or (scan_flag is not None and scan_flag['value'] == '1')
    limit = 100
    invoices = await crypto.invoices({'status': 'paid', 'offset': offset, 'count': limit})
    await process_page(invoices, 'paid')

    if len(invoices) == limit:
        await db.set_setting('crypto_paid_invoice_offset', str(offset + limit))
        await db.set_setting('crypto_paid_scan_from_zero', '1' if from_zero else '0')
    else:
        await db.set_setting('crypto_paid_invoice_offset', '0')
        await db.set_setting('crypto_paid_scan_from_zero', '0')
        if from_zero:
            await db.execute(
                f"""UPDATE orders SET reconcile_at=?
                    WHERE {missing} AND created_at<?
                      AND (payment_url!='' OR invoice_sending_at IS NOT NULL)
                      AND (reconcile_at IS NULL OR reconcile_at<=?)""",
                (in_seconds(86400), recent_cutoff, current_ts),
            )


async def reconcile_external(config: Config, db: Database, payments: Payments):
    # Disabling checkout must not interrupt reconciliation of already issued invoices.
    crypto = payments.crypto if payments.crypto.token else None
    enabled = [name for name, active in (('cryptopay', crypto is not None), ('2328', config.gateway_configured))
               if active]
    if not enabled:
        return
    placeholders = ','.join('?' for _ in enabled)
    recent = in_seconds(-3 * 86400)
    rows = await db.read(f"""SELECT * FROM orders WHERE provider IN ({placeholders})
        AND status IN ('pending','expired')
        AND (created_at>? OR (provider='2328' AND refund_requested_at IS NOT NULL))
        AND NOT (provider='cryptopay' AND refund_requested_at IS NOT NULL)
        AND (external_id!='' OR provider='2328')
        AND (reconcile_at IS NULL OR reconcile_at<=?)
        ORDER BY COALESCE(reconcile_at, created_at), id LIMIT 10""",
        (*enabled, recent, now()), many=True)
    # A payment made before a long outage must still be recoverable. Keep a
    # separate, small archive batch so unpaid old invoices cannot starve new ones.
    rows += await db.read(f"""SELECT * FROM orders WHERE provider IN ({placeholders})
        AND status IN ('pending','expired') AND created_at<=? AND refund_requested_at IS NULL
        AND (external_id!='' OR (provider='2328' AND (payment_url!='' OR invoice_sending_at IS NOT NULL)))
        AND (reconcile_at IS NULL OR reconcile_at<=?)
        ORDER BY COALESCE(reconcile_at,created_at),id LIMIT 2""", (*enabled, recent, now()), many=True)
    if config.gateway_configured:
        rows += await db.read("""SELECT * FROM orders WHERE provider='2328' AND status='paid' AND external_id!=''
            AND (reconcile_at IS NULL OR reconcile_at<=?)
            ORDER BY refund_requested_at IS NULL, COALESCE(reconcile_at,paid_at),id LIMIT 5""", (now(),), many=True)

    async def check(order):
        paid = order['status'] == 'paid'
        awaiting_refund = bool(order['refund_requested_at'])
        try:
            await payments.reconcile(order, refund_only=paid or awaiting_refund,
                                     crypto=crypto if order['provider'] == 'cryptopay' else None)
        except Exception:
            logger.warning('External reconciliation failed for order %s', order['id'], exc_info=True)
        finally:
            current = await db.read('SELECT * FROM orders WHERE id=?', (order['id'],))
            if not current or current['status'] == 'failed' or (
                current['provider'] == 'cryptopay' and current['refund_requested_at']
            ):
                await db.execute('UPDATE orders SET reconcile_at=NULL WHERE id=?', (order['id'],))
            else:
                wait = (60 if current['refund_requested_at'] else 3600 if current['status'] == 'paid'
                        else 86400 if current['created_at'] <= recent
                        else 300 if current['expires_at'] <= now() else config.poll_seconds)
                await db.execute("""UPDATE orders SET reconcile_at=? WHERE id=? AND status=?
                    AND refund_requested_at IS ? AND reconcile_at IS ?""",
                    (in_seconds(wait), order['id'], current['status'],
                     current['refund_requested_at'], current['reconcile_at']))

    async def safe_recover():
        try:
            await recover_crypto_invoices(db, payments, crypto)
        except Exception:
            logger.warning('Crypto Pay batch recovery failed', exc_info=True)

    checks = [check(order) for order in rows]
    if crypto:
        checks.append(safe_recover())
    await asyncio.gather(*checks)


async def maintain_access(bot: Bot, db: Database, access: AccessManager, config: Config | None = None):
    await db.execute("UPDATE orders SET status='expired' WHERE status='pending' AND expires_at<=?", (now(),))
    await db.execute('DELETE FROM welcome_requests WHERE requested_at<?', (int(time()) - 86400,))
    await access.revoke_expired(bot)
    await access.provision(bot)
    await dispatch_whale_alerts(bot, db, config)
    await dispatch_cold_start_nudges(bot, db, access.shop)
    await dispatch_abandoned_cart_reminders(bot, db, access.shop)
    await dispatch_tariff_view_reminders(bot, db, access.shop)
    await dispatch_trial_upsell_touchpoints(bot, db, access.shop)
    await dispatch_promo_pushes(bot, db, access.shop)
    await dispatch_promo_expiry_reminders(bot, db, access.shop)
    await dispatch_winback_pushes(bot, db, access.shop)
    await dispatch_review_pushes(bot, db, access.shop)


async def run_periodically(name, interval, operation, *args):
    while True:
        try:
            await operation(*args)
        except Exception:
            logger.exception('%s failed, will retry', name)
        await asyncio.sleep(interval)
