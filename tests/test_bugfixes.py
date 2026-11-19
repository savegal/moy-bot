"""Regression tests verifying all 7 audit bugfixes end-to-end."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import Chat, Message, StarTransaction, StarTransactions, TransactionPartnerUser, Update, User
from aiohttp.test_utils import TestClient, TestServer

from aurora_bot.alerts import (
    dispatch_abandoned_cart_reminders,
    dispatch_tariff_view_reminders,
    dispatch_trial_upsell_touchpoints,
)
from aurora_bot.config import Config
from aurora_bot.db import Database, in_seconds
from aurora_bot.handlers_admin import (
    BROADCAST_SEGMENTS,
    AdminState,
    _parse_promo_fields,
    _run_broadcast,
    promo_push_enable_code,
    promo_push_pick_list,
    promo_push_set_code,
    router as admin_router,
)
from aurora_bot.handlers_user import (
    _render_tariff_view,
    _send_profile,
    check_payment,
    notifications,
    router as user_router,
    successful_stars,
)
from aurora_bot.payments import GatewayError, Payments
from aurora_bot.pushes import (
    PERMANENT_BLOCK_UNTIL,
    dispatch_promo_pushes,
    dispatch_review_pushes,
    resolve_shared_promo,
)
from aurora_bot.server import make_app
from aurora_bot.service import Shop, ShopError
from aurora_bot.ui import MENU_CATALOG, MENU_HELP, MENU_INFO, MENU_PROFILE, MENU_STATS_PREFIX, product_text
from aurora_bot.workers import reconcile_external, reconcile_stars


def _star_tx(tx_id: str, user_id: int, amount: int, payload: str) -> StarTransaction:
    return StarTransaction(
        id=tx_id,
        amount=amount,
        date=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        source=TransactionPartnerUser(
            transaction_type='invoice_payment',
            user=User(id=user_id, is_bot=False, first_name='Buyer'),
            invoice_payload=payload,
        ),
    )


class AuditBugfixesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Database(f'{self.tmp.name}/test.sqlite3')
        await self.db.init()
        self.shop = Shop(self.db)
        await self.db.register(101, 'admin', 'Admin', via_start=True)
        await self.db.register(202, 'alice', 'Alice', via_start=True)
        await self.db.register(303, 'bob', 'Bob', via_start=True)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_1_2_reconcile_stars_prioritizes_tail_and_persists_cursor(self):
        await self.db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+t1\"]' WHERE id=1")
        mid_order = await self.shop.create_order(202, 1, 'stars')
        tail_order = await self.shop.create_order(303, 1, 'stars')
        entries = [_star_tx(f'old-{i}', 303, 100, f'unmatched-{i}') for i in range(600)]
        entries[290] = _star_tx('mid-290', 202, mid_order['amount_stars'], mid_order['id'])
        entries[599] = _star_tx('tail-599', 303, tail_order['amount_stars'], tail_order['id'])

        seen_offsets: list[tuple[int, int]] = []

        async def fake_get_star_transactions(*, offset: int = 0, limit: int = 100, **_kw):
            if limit > 1:
                seen_offsets.append((offset, limit))
            return StarTransactions(transactions=entries[offset: offset + limit])

        bot = NS(get_star_transactions=AsyncMock(side_effect=fake_get_star_transactions))

        # Tick 1 catches newest tail payment immediately while cursor advances only across contiguous pages (0..260 -> 240).
        await reconcile_stars(bot, self.db, self.shop)
        self.assertEqual((await self.shop.order(tail_order['id']))['status'], 'paid')
        self.assertEqual((await self.shop.order(mid_order['id']))['status'], 'pending')
        self.assertEqual(int((await self.db.setting('stars_ledger_offset'))['value']), 240)

        # Tick 2 resumes contiguously at 240 (without scanning offset 0 or skipping 240..500),
        # settles the middle entry at 290, and still fast-settles a new tail payment at 600.
        seen_offsets.clear()
        order2 = await self.shop.create_order(303, 1, 'stars')
        entries.append(_star_tx('tail-600', 303, order2['amount_stars'], order2['id']))
        await reconcile_stars(bot, self.db, self.shop)
        self.assertEqual((await self.shop.order(mid_order['id']))['status'], 'paid')
        self.assertEqual((await self.shop.order(order2['id']))['status'], 'paid')
        self.assertNotIn(0, [off for off, _ in seen_offsets])
        self.assertEqual(seen_offsets[:3], [(240, 100), (320, 100), (400, 100)])
        self.assertEqual(int((await self.db.setting('stars_ledger_offset'))['value']), 480)

        # Tick 3 catches up contiguously through index 600 and stops at tail cursor.
        seen_offsets.clear()
        await reconcile_stars(bot, self.db, self.shop)
        self.assertEqual(seen_offsets, [(480, 100), (560, 100)])
        self.assertGreaterEqual(int((await self.db.setting('stars_ledger_offset'))['value']), 580)

        # Ledger reset/shrink recovers gracefully without getting stuck at stale high offset.
        entries[:] = entries[-20:]
        order3 = await self.shop.create_order(202, 1, 'stars')
        entries.append(_star_tx('reset-21', 202, order3['amount_stars'], order3['id']))
        await reconcile_stars(bot, self.db, self.shop)
        self.assertEqual((await self.shop.order(order3['id']))['status'], 'paid')
        self.assertEqual((await self.db.setting('stars_ledger_offset'))['value'], '0')

    async def test_1_2_reconcile_stars_survives_constant_mock_without_looping(self):
        entries = [_star_tx(f'const-{i}', 303, 100, f'u-{i}') for i in range(100)]
        calls = 0

        async def broken_mock(**_kw):
            nonlocal calls
            calls += 1
            return StarTransactions(transactions=entries)

        bot = NS(get_star_transactions=AsyncMock(side_effect=broken_mock))
        await reconcile_stars(bot, self.db, self.shop)
        self.assertLessEqual(calls, 10)

    async def test_1_1_external_lock_extends_to_provider_invoice_expiry(self):
        await self.db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+tlock\"]' WHERE id=1")
        cfg = Config(
            '123:mock', frozenset({101}), project_2328='proj', key_2328='sec',
            public_base_url='https://pay.example', cryptopay_token='tok', allow_external_methods=True,
        )
        payments = Payments(cfg, self.shop)

        # Case A: order was created earlier and has only 60s left before DB expiry,
        # but Crypto Pay issues a fresh invoice expiring in +40m from creation time.
        c202 = await self.shop.create_order(202, 1, 'cryptopay')
        await self.db.execute('UPDATE orders SET expires_at=? WHERE id=?', (in_seconds(60), c202['id']))
        provider_exp = in_seconds(40 * 60)
        payments.crypto.api = AsyncMock(return_value={
            'invoice_id': 9001,
            'bot_invoice_url': 'https://t.me/CryptoBot?start=cp9001',
            'currency_type': 'fiat',
            'fiat': 'RUB',
            'payload': c202['id'],
            'amount': str(c202['amount_rub']),
            'expiration_date': provider_exp,
        })
        await payments.create_external(c202)
        c202_after = await self.shop.order(c202['id'])
        self.assertGreaterEqual(c202_after['expires_at'], in_seconds(39 * 60))

        # Case B: mocked provider client without expiration_date still extends order TTL to 35m (not remaining 30s).
        g202 = await self.shop.create_order(202, 1, '2328')
        await self.db.execute('UPDATE orders SET expires_at=? WHERE id=?', (in_seconds(30), g202['id']))
        payments.gateway.create = AsyncMock(return_value=('gw-short', 'https://pay.example/gwshort'))
        await payments.create_external(g202)
        g202_after = await self.shop.order(g202['id'])
        self.assertGreaterEqual(g202_after['expires_at'], in_seconds(34 * 60))

    async def test_1_2_upgrade_migration_resets_stars_cursor_and_recovers_failed_issued_invoices_once(self):
        legacy_path = f'{self.tmp.name}/legacy_v25.sqlite3'
        legacy_db = Database(legacy_path)
        await legacy_db.init()
        legacy_shop = Shop(legacy_db)
        await legacy_db.register(101, 'admin', 'Admin', via_start=True)
        await legacy_db.register(202, 'alice', 'Alice', via_start=True)
        await legacy_db.register(303, 'bob', 'Bob', via_start=True)
        await legacy_db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+tlegacy\"]' WHERE id=1")

        # Prepare orders that simulate pre-fix state in an existing DB:
        skipped_stars = await legacy_shop.create_order(202, 1, 'stars')
        failed_cp = await legacy_shop.create_order(303, 1, 'cryptopay')
        failed_gw = await legacy_shop.create_order(303, 1, '2328')
        rejected_manual = await legacy_shop.create_order(303, 1, 'manual')
        unissued_stars = await legacy_shop.create_order(303, 1, 'stars')

        await legacy_db.execute(
            "UPDATE orders SET external_id='cp-fail-1', payment_url='https://t.me/cpfail1', status='failed', reconcile_at=NULL WHERE id=?",
            (failed_cp['id'],),
        )
        await legacy_db.execute(
            "UPDATE orders SET external_id='gw-fail-1', payment_url='https://pay.example/gwfail1', status='failed', reconcile_at=NULL WHERE id=?",
            (failed_gw['id'],),
        )
        await legacy_db.execute("UPDATE orders SET status='failed' WHERE id=?", (rejected_manual['id'],))
        await legacy_db.execute("UPDATE orders SET status='failed' WHERE id=?", (unissued_stars['id'],))

        # Simulate old version state: skipped Stars cursor and absence of one-time recovery marker.
        await legacy_db.set_setting('stars_ledger_offset', '500')
        await legacy_db.execute("DELETE FROM settings WHERE key='payment_gap_recovery_v1'")

        # Upgrade startup runs one-time backfill and upgrades schema to v28.
        upgraded = Database(legacy_path)
        await upgraded.init()
        self.assertEqual((await upgraded.read('PRAGMA user_version'))['user_version'], 28)
        self.assertEqual((await upgraded.setting('stars_ledger_offset'))['value'], '0')
        self.assertEqual((await upgraded.setting('payment_gap_recovery_v1'))['value'], '1')

        self.assertEqual((await upgraded.read('SELECT status FROM orders WHERE id=?', (failed_cp['id'],)))['status'], 'expired')
        self.assertEqual((await upgraded.read('SELECT status FROM orders WHERE id=?', (failed_gw['id'],)))['status'], 'expired')
        self.assertEqual((await upgraded.read('SELECT status FROM orders WHERE id=?', (rejected_manual['id'],)))['status'], 'failed')
        self.assertEqual((await upgraded.read('SELECT status FROM orders WHERE id=?', (unissued_stars['id'],)))['status'], 'failed')

        # Reconcile passes recover both the previously skipped Stars tx at index 40 and paid cp-fail-1.
        upgraded_shop = Shop(upgraded)
        entries = [_star_tx(f'leg-{i}', 303, 100, f'leg-u-{i}') for i in range(150)]
        entries[40] = _star_tx('leg-40', 202, skipped_stars['amount_stars'], skipped_stars['id'])
        bot = NS(get_star_transactions=AsyncMock(
            side_effect=lambda *, offset=0, limit=100, **_kw: StarTransactions(transactions=entries[offset: offset + limit])
        ))
        await reconcile_stars(bot, upgraded, upgraded_shop)
        self.assertEqual((await upgraded_shop.order(skipped_stars['id']))['status'], 'paid')

        cfg = Config('123:mock', frozenset({101}), cryptopay_token='cp-sec', allow_external_methods=True)
        payments = Payments(cfg, upgraded_shop)
        payments.crypto = NS(
            token='cp-sec',
            paid_invoice=AsyncMock(side_effect=lambda o: {'invoice_id': 'cp-fail-1', 'status': 'paid'} if o['id'] == failed_cp['id'] else None),
        )
        await reconcile_external(cfg, upgraded, payments)
        cp_after = await upgraded_shop.order(failed_cp['id'])
        self.assertEqual(cp_after['status'], 'paid')
        self.assertIsNotNone(cp_after['paid_at'])

        # Calling reconcile_external again does not re-poll settled cp-fail-1.
        payments.crypto.paid_invoice.reset_mock()
        await reconcile_external(cfg, upgraded, payments)
        payments.crypto.paid_invoice.assert_not_awaited()

        # Subsequent restarts/init() must NOT reset an already-advancing Stars cursor again.
        await upgraded.set_setting('stars_ledger_offset', '240')
        await upgraded.init()
        self.assertEqual((await upgraded.setting('stars_ledger_offset'))['value'], '240')

    async def test_1_3_pre_upgrade_external_links_restore_reconcile_on_v2_backfill(self):
        legacy_path = f'{self.tmp.name}/legacy_leases.sqlite3'
        legacy_db = Database(legacy_path)
        await legacy_db.init()
        legacy_shop = Shop(legacy_db)
        await legacy_db.register(101, 'admin', 'Admin', via_start=True)
        await legacy_db.register(202, 'alice', 'Alice', via_start=True)
        await legacy_db.register(303, 'bob', 'Bob', via_start=True)
        await legacy_db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+tv2\"]' WHERE id=1")

        cp_paid = await legacy_shop.create_order(202, 1, 'cryptopay')
        cp_exp = await legacy_shop.create_order(202, 1, 'cryptopay')
        gw_paid = await legacy_shop.create_order(303, 1, '2328')

        old_ts = in_seconds(-4 * 86400)
        future_snooze = in_seconds(3600)
        await legacy_db.execute(
            """UPDATE orders SET created_at=?, external_id='5001', payment_url='https://t.me/CryptoBot?start=cp5001',
               status='expired', expires_at=?, reconcile_at=? WHERE id=?""",
            (old_ts, in_seconds(-300), future_snooze, cp_paid['id']),
        )
        await legacy_db.execute(
            """UPDATE orders SET created_at=?, external_id='5002', payment_url='https://t.me/CryptoBot?start=cp5002',
               status='expired', expires_at=?, reconcile_at=? WHERE id=?""",
            (old_ts, in_seconds(-300), future_snooze, cp_exp['id']),
        )
        await legacy_db.execute(
            """UPDATE orders SET created_at=?, external_id='gw-5003', payment_url='https://pay.example/gw5003',
               status='expired', expires_at=?, reconcile_at=? WHERE id=?""",
            (old_ts, in_seconds(-300), future_snooze, gw_paid['id']),
        )
        await legacy_db.set_setting('payment_gap_recovery_v1', '1')
        await legacy_db.execute("DELETE FROM settings WHERE key='payment_gap_recovery_v2'")

        upgraded = Database(legacy_path)
        await upgraded.init()
        self.assertEqual((await upgraded.read('PRAGMA user_version'))['user_version'], 28)
        self.assertEqual((await upgraded.setting('payment_gap_recovery_v2'))['value'], '1')
        for oid in (cp_paid['id'], cp_exp['id'], gw_paid['id']):
            self.assertIsNone((await upgraded.read('SELECT reconcile_at FROM orders WHERE id=?', (oid,)))['reconcile_at'])

        upgraded_shop = Shop(upgraded)
        cfg = Config(
            '123:mock', frozenset({101}), project_2328='proj', key_2328='sec',
            public_base_url='https://pay.example', cryptopay_token='cp-tok', allow_external_methods=True,
        )
        payments = Payments(cfg, upgraded_shop)

        async def fake_cp_api(method, payload):
            if payload.get('invoice_ids') == '5001':
                return {'items': [{
                    'invoice_id': 5001, 'status': 'paid', 'currency_type': 'fiat', 'fiat': 'RUB',
                    'amount': str(cp_paid['amount_rub']), 'payload': cp_paid['id'],
                    'bot_invoice_url': 'https://t.me/CryptoBot?start=cp5001',
                    'paid_at': in_seconds(-2 * 86400),
                }]}
            if payload.get('invoice_ids') == '5002':
                return {'items': [{
                    'invoice_id': 5002, 'status': 'expired', 'currency_type': 'fiat', 'fiat': 'RUB',
                    'amount': str(cp_exp['amount_rub']), 'payload': cp_exp['id'],
                    'bot_invoice_url': 'https://t.me/CryptoBot?start=cp5002',
                    'expiration_date': in_seconds(-60),
                }]}
            return {'items': []}

        payments.crypto.api = AsyncMock(side_effect=fake_cp_api)
        payments.gateway.api = AsyncMock(return_value={
            'uuid': 'gw-5003', 'order_id': gw_paid['id'], 'amount': str(gw_paid['amount_rub']),
            'currency': 'RUB', 'url': 'https://pay.example/gw5003', 'payment_status': 'paid',
        })

        await reconcile_external(cfg, upgraded, payments)
        await reconcile_external(cfg, upgraded, payments)

        self.assertEqual((await upgraded_shop.order(cp_paid['id']))['status'], 'paid')
        self.assertEqual((await upgraded_shop.order(cp_exp['id']))['status'], 'expired')
        self.assertEqual((await upgraded_shop.order(gw_paid['id']))['status'], 'paid')

    async def test_1_4_archival_issued_links_without_external_id_reconciled(self):
        legacy_path = f'{self.tmp.name}/legacy_archive.sqlite3'
        legacy_db = Database(legacy_path)
        await legacy_db.init()
        legacy_shop = Shop(legacy_db)
        await legacy_db.register(101, 'admin', 'Admin', via_start=True)
        await legacy_db.register(202, 'alice', 'Alice', via_start=True)
        await legacy_db.register(303, 'bob', 'Bob', via_start=True)
        await legacy_db.register(404, 'carol', 'Carol', via_start=True)
        await legacy_db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+tarchive\"]' WHERE id=1")

        arch_ts = in_seconds(-10 * 86400)
        arch_exp = in_seconds(-9 * 86400)

        # Archival 2328 order recovered by v1 ('expired'), has payment_url but no external_id.
        gw_arch = await legacy_shop.create_order(202, 1, '2328')
        await legacy_db.execute(
            """UPDATE orders SET created_at=?, expires_at=?, payment_url='https://pay.example/archgw',
               external_id='', status='expired', reconcile_at=? WHERE id=?""",
            (arch_ts, arch_exp, in_seconds(3600), gw_arch['id']),
        )

        # Archival Crypto Pay order with payment_url, no external_id, actually paid.
        cp_arch_paid = await legacy_shop.create_order(303, 1, 'cryptopay')
        await legacy_db.execute(
            """UPDATE orders SET created_at=?, expires_at=?, payment_url='https://t.me/CryptoBot?start=archpaid',
               external_id='', status='expired', reconcile_at=? WHERE id=?""",
            (arch_ts, arch_exp, in_seconds(3600), cp_arch_paid['id']),
        )

        # Archival Crypto Pay order with payment_url, no external_id, genuinely unpaid/expired -> snoozed after 1 scan.
        cp_arch_miss = await legacy_shop.create_order(404, 1, 'cryptopay')
        await legacy_db.execute(
            """UPDATE orders SET created_at=?, expires_at=?, payment_url='https://t.me/CryptoBot?start=archmiss',
               external_id='', status='expired', reconcile_at=? WHERE id=?""",
            (arch_ts, arch_exp, in_seconds(3600), cp_arch_miss['id']),
        )

        # Archival unissued Crypto Pay order without payment_url must NOT trigger polling.
        cp_arch_unissued = await legacy_shop.create_order(404, 1, 'cryptopay')
        await legacy_db.execute(
            "UPDATE orders SET created_at=?, expires_at=?, external_id='', payment_url='', status='expired' WHERE id=?",
            (arch_ts, arch_exp, cp_arch_unissued['id']),
        )

        await legacy_db.set_setting('payment_gap_recovery_v1', '1')
        await legacy_db.execute("DELETE FROM settings WHERE key='payment_gap_recovery_v2'")

        upgraded = Database(legacy_path)
        await upgraded.init()
        upgraded_shop = Shop(upgraded)

        cfg = Config(
            '123:mock', frozenset({101}), project_2328='proj', key_2328='sec',
            public_base_url='https://pay.example', cryptopay_token='cp-tok', allow_external_methods=True,
        )
        payments = Payments(cfg, upgraded_shop)

        async def fake_cp_api(method, payload):
            if payload.get('status') == 'paid':
                return {'items': [{
                    'invoice_id': 7701, 'status': 'paid', 'currency_type': 'fiat', 'fiat': 'RUB',
                    'amount': str(cp_arch_paid['amount_rub']), 'payload': cp_arch_paid['id'],
                    'bot_invoice_url': 'https://t.me/CryptoBot?start=archpaid',
                    'paid_at': in_seconds(-8 * 86400),
                }]}
            return {'items': []}

        payments.crypto.api = AsyncMock(side_effect=fake_cp_api)
        payments.gateway.api = AsyncMock(return_value={
            'uuid': 'gw-arch-99', 'order_id': gw_arch['id'], 'amount': str(gw_arch['amount_rub']),
            'currency': 'RUB', 'url': 'https://pay.example/archgw', 'payment_status': 'paid',
        })

        await reconcile_external(cfg, upgraded, payments)

        gw_after = await upgraded_shop.order(gw_arch['id'])
        self.assertEqual((gw_after['status'], gw_after['external_id']), ('paid', 'gw-arch-99'))
        self.assertEqual(len(await upgraded_shop.purchases(202)), 1)

        cp_paid_after = await upgraded_shop.order(cp_arch_paid['id'])
        self.assertEqual((cp_paid_after['status'], cp_paid_after['external_id']), ('paid', '7701'))
        self.assertEqual(len(await upgraded_shop.purchases(303)), 1)

        # Unmatched archival Crypto Pay link is snoozed for 24h, so next tick skips getInvoices altogether.
        miss_after = await upgraded_shop.order(cp_arch_miss['id'])
        self.assertGreater(miss_after['reconcile_at'], in_seconds(80000))
        payments.crypto.api.reset_mock()
        payments.gateway.api.reset_mock()
        await reconcile_external(cfg, upgraded, payments)
        paid_calls = [c for c in payments.crypto.api.await_args_list if c.args[1].get('status') in ('paid', 'active')]
        self.assertEqual(paid_calls, [])

        # Subsequent restarts/init() preserve snoozed archival timestamps once payment_gap_recovery_v2 is marked done.
        snoozed_before = (await upgraded_shop.order(cp_arch_miss['id']))['reconcile_at']
        await upgraded.init()
        self.assertEqual((await upgraded_shop.order(cp_arch_miss['id']))['reconcile_at'], snoozed_before)
        self.assertEqual((await upgraded.setting('payment_gap_recovery_v2'))['value'], '1')

    async def test_2_1_admin_fsm_clears_on_bottom_menu_and_falls_through_to_user_router(self):
        cfg = Config('123:mock', frozenset({101}))
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(admin_router)
        dp.include_router(user_router)
        try:
            def make_update(uid: int, text: str, update_id: int) -> Update:
                return Update(
                    update_id=update_id,
                    message=Message(
                        message_id=update_id,
                        date=datetime.now(timezone.utc),
                        chat=Chat(id=uid, type='private'),
                        from_user=User(id=uid, is_bot=False, first_name='Admin'),
                        text=text,
                    ),
                )

            bot = AsyncMock(spec=Bot)
            bot.id = 999
            bot.session = NS()
            skey = StorageKey(bot_id=999, chat_id=101, user_id=101)

            nav_labels = (MENU_CATALOG, MENU_PROFILE, MENU_HELP, MENU_INFO, f'{MENU_STATS_PREFIX} · 5', '/start')
            for idx, menu_label in enumerate(nav_labels, start=1):
                await dp.storage.set_state(skey, AdminState.tariff_field)
                await dp.storage.set_data(skey, {'field': 'title', 'tid': 1})
                with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as mock_sec, \
                     patch('aurora_bot.handlers_user._send_profile', new_callable=AsyncMock) as mock_prof, \
                     patch('aurora_bot.handlers_user.send_welcome_message', new_callable=AsyncMock) as mock_wel:
                    await dp.feed_update(
                        bot,
                        make_update(101, menu_label, idx),
                        db=self.db,
                        shop=self.shop,
                        config=cfg,
                        payments=NS(),
                        access=NS(),
                    )
                self.assertIsNone(await dp.storage.get_state(skey), f'FSM stayed stuck after tapping {menu_label!r}')
                self.assertTrue(
                    mock_sec.await_count or mock_prof.await_count or mock_wel.await_count or bot.send_message.await_count,
                    f'User router did not receive fallback navigation for {menu_label!r}',
                )
        finally:
            admin_router._parent_router = None
            user_router._parent_router = None
            dp.sub_routers.clear()

    async def test_2_2_permanent_errors_suppress_retries_clean_promos_and_unblock_on_start(self):
        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.set_setting('promo_push_delay_min', '5')
        await self.db.set_setting('review_push_enabled', '1')
        await self.db.set_setting('reviews_visible', '1')
        await self.db.set_setting('review_push_audience', 'cold')
        await self.db.set_setting('review_push_delay_min', '5')
        await self.db.set_setting('review_push_interval_hours', '1')
        await self.db.set_setting('review_push_max_per_user', '5')
        await self.db.execute(
            'UPDATE users SET first_start_at=? WHERE user_id IN (202, 303)',
            (in_seconds(-24 * 3600),),
        )
        await self.db.execute(
            "INSERT INTO reviews(media_id, media_type, active, sort_order, created_at) VALUES('r1', 'photo', 1, 1, ?)",
            (in_seconds(-3600),),
        )

        promos_before = (await self.db.read("SELECT COUNT(*) AS c FROM promos WHERE owner_user_id=202"))['c']
        blocked_err = TelegramForbiddenError(method=SendMessage(chat_id=202, text='hi'), message='Forbidden: bot was blocked by the user')
        deactivated_err = TelegramBadRequest(method=SendPhoto(chat_id=202, photo='r1'), message='Bad Request: user is deactivated')

        bot = NS(
            send_message=AsyncMock(side_effect=lambda uid, *a, **kw: (_ for _ in ()).throw(blocked_err) if uid == 202 else NS()),
            send_photo=AsyncMock(side_effect=lambda uid, *a, **kw: (_ for _ in ()).throw(deactivated_err) if uid == 202 else NS()),
        )

        # User 202 is blocked; user 303 is reachable. Promo dispatch sets permanent sentinel on 202,
        # deletes unused personal promo row cleanly, and delivers to 303 without starvation.
        promo_sent = await dispatch_promo_pushes(bot, self.db, self.shop)
        self.assertEqual(promo_sent, 1)
        u202 = await self.db.read('SELECT promo_push_sent, promo_push_retry_at FROM users WHERE user_id=202')
        self.assertEqual(u202['promo_push_sent'], 0)
        self.assertEqual(u202['promo_push_retry_at'], PERMANENT_BLOCK_UNTIL)
        promos_after = (await self.db.read("SELECT COUNT(*) AS c FROM promos WHERE owner_user_id=202"))['c']
        self.assertEqual(promos_after, promos_before)

        # Advance promo cooldown for 303 so review push eligibility is immediate, then test LIMIT=1 review starvation guard.
        await self.db.execute('UPDATE users SET promo_push_sent_at=? WHERE user_id=303', (in_seconds(-3600),))
        first_rev = await dispatch_review_pushes(bot, self.db, self.shop, limit=1)
        second_rev = await dispatch_review_pushes(bot, self.db, self.shop, limit=1)
        self.assertEqual(first_rev + second_rev, 1)
        u202_rev = await self.db.read('SELECT review_push_retry_at FROM users WHERE user_id=202')
        u303_rev = await self.db.read('SELECT last_review_push_at FROM users WHERE user_id=303')
        self.assertEqual(u202_rev['review_push_retry_at'], PERMANENT_BLOCK_UNTIL)
        self.assertIsNotNone(u303_rev['last_review_push_at'])

        # Subsequent promo/review ticks never attempt to contact 202 again.
        bot.send_message.reset_mock()
        bot.send_photo.reset_mock()
        await dispatch_promo_pushes(bot, self.db, self.shop)
        await dispatch_review_pushes(bot, self.db, self.shop)
        self.assertNotIn(202, [c.args[0] for c in bot.send_message.await_args_list])
        self.assertNotIn(202, [c.args[0] for c in bot.send_photo.await_args_list])

        # When user 202 unblocks bot and sends /start (via_start=True), permanent retry sentinels clear.
        await self.db.register(202, 'alice', 'Alice', via_start=True)
        u202_unblocked = await self.db.read('SELECT promo_push_retry_at, review_push_retry_at FROM users WHERE user_id=202')
        self.assertIsNone(u202_unblocked['promo_push_retry_at'])
        self.assertIsNone(u202_unblocked['review_push_retry_at'])

        # TimeoutError keeps generated personal promo active and reuses the exact same code on retry.
        await self.db.register(505, 'eva', 'Eva', via_start=True)
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id=505', (in_seconds(-24 * 3600),))
        await self.db.execute("UPDATE users SET promo_push_retry_at=? WHERE user_id=202", (PERMANENT_BLOCK_UNTIL,))
        bot_to = NS(send_message=AsyncMock(side_effect=[TimeoutError('tg timeout'), NS()]))
        self.assertEqual(await dispatch_promo_pushes(bot_to, self.db, self.shop), 0)
        u505_row = await self.db.read('SELECT promo_push_code, promo_push_sent FROM users WHERE user_id=505')
        saved_code = u505_row['promo_push_code']
        self.assertTrue(saved_code)
        self.assertEqual(u505_row['promo_push_sent'], 0)
        self.assertEqual((await self.db.read('SELECT enabled, used FROM promos WHERE code=?', (saved_code,)))['enabled'], 1)
        await self.db.execute('UPDATE users SET promo_push_retry_at=? WHERE user_id=505', (in_seconds(-10),))
        self.assertEqual(await dispatch_promo_pushes(bot_to, self.db, self.shop), 1)
        self.assertIn(saved_code, bot_to.send_message.await_args.args[1])
        self.assertEqual((await self.db.read('SELECT COUNT(*) AS c FROM promos WHERE owner_user_id=505'))['c'], 1)

    async def test_2_3_dynamic_tariff_titles_durations_and_channel_counts(self):
        await self.db.execute(
            "UPDATE tariffs SET title='⚡ Старт на неделю', description='Стартовый пакет на 7 дней', "
            "duration_days=7, rub=390, stars=195, channel_ids='-1001', delivery_urls='[]' WHERE id=1"
        )
        await self.db.execute(
            "UPDATE tariffs SET title='💎 Платиновый VIP', description='Главный пакет', duration_days=30, "
            "rub=1500, stars=750, tier='ultra', channel_ids='-1001,-1002,-1003', delivery_urls='[]' WHERE id=5"
        )
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id NOT IN (1, 5)')

        t5 = await self.shop.get_tariff(5)
        card = product_text(t5, t5['stars'], buyers=10, rub=t5['rub'])
        self.assertIn('3 канала', card)
        self.assertIn('на 30 дн.', card)
        self.assertNotIn('5 каналов навсегда', card)
        self.assertNotIn('3 550', card)

        t1 = await self.shop.get_tariff(1)
        t1_card = product_text(t1, t1['stars'], buyers=5, rub=t1['rub'])
        self.assertNotIn('Ultra VIP', t1_card)
        call = NS(
            from_user=NS(id=202),
            answer=AsyncMock(),
            message=NS(answer=AsyncMock(), answer_photo=AsyncMock(), answer_video=AsyncMock(), answer_animation=AsyncMock()),
        )
        await _render_tariff_view(call, self.shop, 1, is_preview=False)
        markup = call.message.answer.await_args.kwargs['reply_markup']
        btn_texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertTrue(any('Платиновый VIP' in txt for txt in btn_texts))
        self.assertFalse(any('Ultra VIP' in txt or '5 каналов' in txt for txt in btn_texts))

        bot = NS(send_message=AsyncMock())
        ord_big = await self.shop.create_order(202, 5, 'stars')
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-1800), ord_big['id']))
        await dispatch_abandoned_cart_reminders(bot, self.db, self.shop)
        cart_txt = bot.send_message.await_args.args[1]
        self.assertIn('Старт на неделю', cart_txt)
        self.assertIn('на 7 дн.', cart_txt)
        self.assertIn('Платиновый VIP', cart_txt)
        self.assertNotIn('Базовый', cart_txt)
        self.assertNotIn('Ultra VIP', cart_txt)

        bot.send_message.reset_mock()
        await self.db.execute(
            'UPDATE users SET last_viewed_tariff_id=5, last_viewed_at=?, view_reminder_sent=0 WHERE user_id=303',
            (in_seconds(-1800),),
        )
        await dispatch_tariff_view_reminders(bot, self.db, self.shop)
        view_txt = bot.send_message.await_args.args[1]
        self.assertIn('Старт на неделю', view_txt)
        self.assertIn('Платиновый VIP', view_txt)
        self.assertNotIn('Базового', view_txt)
        self.assertNotIn('Ultra VIP', view_txt)

        # Disjoint URL entry purchase (-/+start1) vs 3-channel Ultra {-1001,-1002,-1003} -> all 3 channels are new.
        await self.db.execute(
            "UPDATE tariffs SET duration_days=0, channel_ids='', delivery_urls='[\"https://t.me/+start1\"]' WHERE id=1"
        )
        await self.db.execute('UPDATE tariffs SET duration_days=0 WHERE id=5')
        p_entry = await self.shop.create_order(303, 1, 'stars')
        await self.shop.settle_stars(p_entry['id'], 303, p_entry['amount_stars'], 'ch-upsell-23')
        await self.db.execute('UPDATE purchases SET started_at=? WHERE order_id=?', (in_seconds(-1800), p_entry['id']))
        bot.send_message.reset_mock()
        await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop)
        upsell_txt = bot.send_message.await_args.args[1]
        self.assertIn('Старт на неделю', upsell_txt)
        self.assertIn('ещё 3 канала', upsell_txt)
        self.assertIn('Платиновый VIP', upsell_txt)
        self.assertNotIn('Базового', upsell_txt)

        # User holding two overlapping active channel purchases (-1001 & -1002) plus a revoked channel (-1004)
        # gets the exact set difference (3 - 2 = 1 new channel) against Ultra {-1001,-1002,-1003}.
        await self.db.register(505, 'dan', 'Dan', via_start=True)
        await self.db.execute(
            "UPDATE tariffs SET duration_days=0, channel_ids='-1001', delivery_urls='[]' WHERE id=1"
        )
        p_d1 = await self.shop.create_order(505, 1, 'stars')
        await self.shop.settle_stars(p_d1['id'], 505, p_d1['amount_stars'], 'ch-set-1')
        await self.db.execute(
            "UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=(SELECT id FROM purchases WHERE order_id=?)",
            (in_seconds(-1800), p_d1['id']),
        )
        await self.db.execute(
            "UPDATE tariffs SET title='Пакет 2', duration_days=0, rub=390, stars=195, channel_ids='-1002', delivery_urls='[]', active=1 WHERE id=2"
        )
        p_d2 = await self.shop.create_order(505, 2, 'stars')
        await self.shop.settle_stars(p_d2['id'], 505, p_d2['amount_stars'], 'ch-set-2')
        await self.db.execute(
            "UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=(SELECT id FROM purchases WHERE order_id=?)",
            (in_seconds(-1800), p_d2['id']),
        )
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id=2')
        bot.send_message.reset_mock()
        await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop)
        set_upsell = bot.send_message.await_args.args[1]
        self.assertIn('ещё 1 канал', set_upsell)
        self.assertNotIn('ещё 2 канала', set_upsell)

        # Also test timed 24h trial buyer -> dynamic 3-channel Ultra copy without hardcoded '5 каналах'
        await self.db.register(404, 'carl', 'Carl', via_start=True)
        await self.db.execute(
            "UPDATE tariffs SET duration_days=1, channel_ids='-1001', delivery_urls='[]' WHERE id=1"
        )
        p_trial = await self.shop.create_order(404, 1, 'stars')
        await self.shop.settle_stars(p_trial['id'], 404, p_trial['amount_stars'], 'ch-trial-24')
        await self.db.execute(
            "UPDATE grants SET state='joined', joined_at=? WHERE purchase_id=(SELECT id FROM purchases WHERE order_id=?)",
            (in_seconds(-1800), p_trial['id']),
        )
        bot.send_message.reset_mock()
        await dispatch_trial_upsell_touchpoints(bot, self.db, self.shop)
        trial_txt = bot.send_message.await_args.args[1]
        self.assertIn('во всех 3 каналах', trial_txt)
        self.assertIn('Платиновый VIP', trial_txt)
        self.assertNotIn('5 каналах', trial_txt)

    async def test_3_1_notifications_opt_out_honored_across_broadcasts_pushes_and_alerts(self):
        await self.db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+u5\"]' WHERE id=5")

        call_init = NS(data='notifications', from_user=NS(id=202, username='alice', full_name='Alice'),
                       answer=AsyncMock(), message=NS(answer=AsyncMock()))
        await notifications(call_init, self.db)
        self.assertEqual(call_init.message.answer.await_args.args[0], 'Рассылка включена.')

        call_off = NS(data='notifications:0', from_user=NS(id=202, username='alice', full_name='Alice'),
                      answer=AsyncMock(), message=NS(answer=AsyncMock()))
        await notifications(call_off, self.db)
        self.assertEqual(call_off.message.answer.await_args.args[0], 'Рассылка выключена.')

        for seg, (_label, where) in BROADCAST_SEGMENTS.items():
            ids = {r['user_id'] for r in await self.db.read(f'SELECT u.user_id FROM users u WHERE {where}', many=True)}
            self.assertNotIn(202, ids, f'Segment {seg} leaked opted-out user')
            if seg in ('cold', 'all'):
                self.assertIn(303, ids)

        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.set_setting('promo_push_delay_min', '5')
        await self.db.set_setting('review_push_enabled', '1')
        await self.db.set_setting('reviews_visible', '1')
        await self.db.set_setting('review_push_audience', 'cold')
        await self.db.set_setting('review_push_delay_min', '5')
        await self.db.execute('UPDATE users SET first_start_at=? WHERE user_id IN (202, 303)', (in_seconds(-24 * 3600),))
        await self.db.execute(
            "INSERT INTO reviews(media_id, media_type, active, sort_order, created_at) VALUES('r2', 'photo', 1, 1, ?)",
            (in_seconds(-3600),),
        )
        bot = NS(send_message=AsyncMock(), send_photo=AsyncMock())
        await dispatch_promo_pushes(bot, self.db, self.shop)
        promo_targets = [c.args[0] for c in bot.send_message.await_args_list]
        self.assertNotIn(202, promo_targets)
        self.assertIn(303, promo_targets)

        await dispatch_review_pushes(bot, self.db, self.shop)
        review_targets = [c.args[0] for c in bot.send_photo.await_args_list]
        self.assertNotIn(202, review_targets)

        bot.send_message.reset_mock()
        o202 = await self.shop.create_order(202, 5, 'stars')
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-1800), o202['id']))
        await self.db.execute(
            'UPDATE users SET last_viewed_tariff_id=5, last_viewed_at=? WHERE user_id=202',
            (in_seconds(-1800),),
        )
        self.assertEqual(await dispatch_abandoned_cart_reminders(bot, self.db, self.shop), 0)
        self.assertEqual(await dispatch_tariff_view_reminders(bot, self.db, self.shop), 0)

        call_on = NS(data='notifications:1', from_user=NS(id=202, username='alice', full_name='Alice'),
                     answer=AsyncMock(), message=NS(answer=AsyncMock()))
        await notifications(call_on, self.db)
        self.assertEqual(call_on.message.answer.await_args.args[0], 'Рассылка включена.')
        all_ids = {r['user_id'] for r in await self.db.read(
            f"SELECT u.user_id FROM users u WHERE {BROADCAST_SEGMENTS['all'][1]}", many=True)}
        self.assertIn(202, all_ids)

        # Profile renders the notifications entry button, and _run_broadcast re-checks opt-out mid-batch.
        cfg = Config('123:mock', frozenset({101}))
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as prof_sender:
            await _send_profile(NS(), NS(id=202, username='alice', full_name='Alice'), self.shop, self.db, cfg)
        prof_cbs = [btn.callback_data for row in prof_sender.await_args.kwargs['markup'].inline_keyboard for btn in row]
        self.assertIn('notifications', prof_cbs)

        await self.db.execute('UPDATE users SET broadcast_opt_out=1 WHERE user_id=202')
        bcast_bot = NS(copy_message=AsyncMock(return_value=NS(message_id=900)))
        progress = NS(edit_text=AsyncMock())
        await _run_broadcast(bcast_bot, self.db, 101, 101, 55, 'all', [{'user_id': 202}, {'user_id': 303}],
                             progress, asyncio.Event())
        bcast_bot.copy_message.assert_awaited_once_with(303, 101, 55)
        self.assertIn('Отписались <b>1</b>', progress.edit_text.await_args.args[0])

    async def test_3_2_promo_parser_rejects_zero_rub_discount(self):
        with self.assertRaises(ValueError):
            _parse_promo_fields('ZERO0 0 10', None)
        with self.assertRaises(ValueError):
            _parse_promo_fields('NEG -50 10', None)
        code, rub, pct, max_uses = _parse_promo_fields('rub100 100 10', None)
        self.assertEqual((code, rub, pct, max_uses), ('RUB100', 100, 0, 10))

        # Legacy 0 RUB promo cannot be activated, attached to orders, or picked for auto-pushes.
        await self.db.execute(
            "INSERT INTO promos(code, discount, discount_percent, max_uses, used, enabled, is_personal) VALUES('LEGACY0', 0, 0, 10, 0, 1, 0)"
        )
        with self.assertRaises(ShopError):
            await self.shop.activate_promo(202, 'LEGACY0')
        await self.db.execute("UPDATE users SET active_promo='LEGACY0' WHERE user_id=202")
        self.assertIsNone(await self.shop.user_promo(202))
        await self.db.execute("UPDATE tariffs SET delivery_urls='[\"https://t.me/+t32\"]' WHERE id=1")
        order = await self.shop.create_order(202, 1, 'stars')
        self.assertEqual(order['promo_code'], '')
        self.assertIsNone(await resolve_shared_promo(self.db, 'LEGACY0'))

        pick_call = NS(data='adm:ppush:pick:0', from_user=NS(id=101), answer=AsyncMock())
        with patch('aurora_bot.handlers_admin.screen', new_callable=AsyncMock) as scr:
            await promo_push_pick_list(pick_call, self.db)
        pick_cbs = [btn.callback_data for row in scr.await_args.kwargs['reply_markup'].inline_keyboard for btn in row]
        self.assertNotIn('adm:ppush:setcode:LEGACY0', pick_cbs)

        set_call = NS(data='adm:ppush:setcode:LEGACY0', from_user=NS(id=101), answer=AsyncMock())
        await promo_push_set_code(set_call, self.db)
        self.assertTrue(set_call.answer.await_args.kwargs.get('show_alert'))
        en_call = NS(data='adm:ppush:enablecode:LEGACY0', from_user=NS(id=101), answer=AsyncMock())
        await promo_push_enable_code(en_call, self.db)
        self.assertTrue(en_call.answer.await_args.kwargs.get('show_alert'))
