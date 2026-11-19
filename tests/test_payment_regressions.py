from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerPreCheckoutQuery
from aiogram.types import StarTransactions, Update

from aurora_bot.config import Config
from aurora_bot.db import Database, in_seconds
from aurora_bot.handlers_user import before_checkout, check_payment
from aurora_bot.payments import CryptoPay, Gateway2328, GatewayError, Payments, invoice_destination
from aurora_bot.server import make_app, start_server
from aurora_bot.service import Shop, ShopError
from aurora_bot.workers import reconcile_external, reconcile_star_page
from main import build_dispatcher


class InvoiceValidationTests(TestCase):
    def test_invalid_invoice_destinations_are_rejected(self):
        for external_id, url in ((None, 'https://pay.example'), (False, 'https://pay.example'),
                                 ('', 'https://pay.example'), ('123', None),
                                 ('123', 'https://'), ('123', 'http://pay.example'),
                                 ('123', 'https://pay. example'), ('123', 'https://[broken'),
                                 ('123', 'https://pay.example:invalid'), ('123', 'https://pay.example:99999')):
            with self.subTest(external_id=external_id, url=url), self.assertRaises(GatewayError):
                invoice_destination(external_id, url)


class PaymentRegressionTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'payments.db'))
        await self.db.init()
        await self.db.register(101)
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id=1")
        self.shop = Shop(self.db)
        self.config = Config('123:mock', frozenset({99}), allow_external_methods=True,
                             cryptopay_token='test-token', project_2328='project', key_2328='secret',
                             public_base_url='https://shop.example')
        self.payments = Payments(self.config, self.shop)

    async def external_order(self, provider='2328', *, paid=False):
        order = await self.shop.create_order(101, 1, provider)
        await self.shop.set_external(order['id'], provider, order['id'], 'https://pay.example/invoice')
        if paid:
            await self.shop.settle(order['id'], provider, external_id=order['id'])
        return await self.shop.order(order['id'])

    def event(self, order, status):
        payload = {'order_id': order['id'], 'uuid': order['external_id'], 'amount': str(order['amount_rub']),
                   'currency': 'RUB', 'payment_status': status}
        return {**payload, 'sign': self.payments.gateway.signature(payload, self.config.key_2328)}

    async def test_precheckout_does_not_wait_for_the_buyers_fsm_lock(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='', duration_days=0, delivery_urls=? WHERE id=1",
                              ('["https://t.me/+test"]',))
        order = await self.shop.create_order(101, 1, 'stars')
        started, release = asyncio.Event(), asyncio.Event()
        router = Router()

        @router.message()
        async def blocked_menu(message):
            started.set()
            await release.wait()

        router.pre_checkout_query.register(before_checkout)
        with patch('main.admin_router', Router()), patch('main.user_router', router):
            dispatcher = build_dispatcher(shop=self.shop)
        self.addAsyncCleanup(dispatcher.fsm.close)
        user = {'id': 101, 'first_name': 'Buyer', 'is_bot': False}
        menu = Update.model_validate({'update_id': 1, 'message': {'message_id': 1,
            'date': datetime.now(timezone.utc), 'from': user, 'chat': {'id': 101, 'type': 'private'}}})
        checkout = Update.model_validate({'update_id': 2, 'pre_checkout_query': {
            'id': 'query', 'from': user, 'currency': 'XTR', 'total_amount': order['amount_stars'],
            'invoice_payload': order['id']}})
        async with AiohttpSession() as session:
            bot = Bot(self.config.token, session=session)
            with patch.object(session, 'make_request', new_callable=AsyncMock, return_value=True) as request:
                menu_task = asyncio.create_task(dispatcher.feed_update(bot, menu))
                try:
                    await asyncio.wait_for(started.wait(), timeout=3)
                    await asyncio.wait_for(dispatcher.feed_update(bot, checkout), timeout=3)
                    self.assertFalse(menu_task.done())
                    self.assertTrue(request.await_args.args[1].ok)
                    self.assertEqual((await self.shop.order(order['id']))['checkout_id'], 'query')
                finally:
                    release.set()
                    await menu_task

    async def test_rejected_stars_answer_releases_the_promo_reservation(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='', delivery_urls=? WHERE id=1",
                              ('["https://t.me/+test"]',))
        await self.db.register(202)
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('ONE',100,1)")
        await self.shop.activate_promo(101, 'ONE')
        await self.shop.activate_promo(202, 'ONE')
        order = await self.shop.create_order(101, 1, 'stars')
        second = await self.shop.create_order(202, 1, 'stars')
        failure = TelegramBadRequest(method=AnswerPreCheckoutQuery(pre_checkout_query_id='query', ok=True),
                                     message='query is too old')
        query = SimpleNamespace(currency='XTR', invoice_payload=order['id'], from_user=SimpleNamespace(id=101),
                                total_amount=order['amount_stars'], id='query',
                                answer=AsyncMock(side_effect=failure))
        await before_checkout(query, self.shop, None)

        self.assertTrue(query.answer.await_args.kwargs['ok'])
        self.assertEqual((await self.shop.order(order['id']))['checkout_id'], '')
        self.assertTrue(await self.shop.authorize_stars(second['id'], 202, second['amount_stars'], 'second'))

    async def test_ambiguous_stars_answer_keeps_reservation_and_is_not_repeated(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='', delivery_urls=? WHERE id=1",
                              ('["https://t.me/+test"]',))
        order = await self.shop.create_order(101, 1, 'stars')
        query = SimpleNamespace(currency='XTR', invoice_payload=order['id'], from_user=SimpleNamespace(id=101),
                                total_amount=order['amount_stars'], id='query',
                                answer=AsyncMock(side_effect=TimeoutError))
        with self.assertRaises(TimeoutError):
            await before_checkout(query, self.shop, None)
        await before_checkout(query, self.shop, None)

        query.answer.assert_awaited_once()
        self.assertEqual((await self.shop.order(order['id']))['checkout_id'], 'query')

    async def test_stars_upgrade_rechecks_price_after_credit_decreases(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id IN (2, 5)")
        first = await self.shop.create_order(101, 1, 'stars')
        second = await self.shop.create_order(101, 2, 'stars')
        await self.shop.settle_stars(first['id'], 101, first['amount_stars'], 'first')
        await self.shop.settle_stars(second['id'], 101, second['amount_stars'], 'second')
        upgrade = await self.shop.create_upgrade_order(101, 5, 'stars')
        await self.shop.refund_stars('second', second['amount_stars'], order_id=second['id'], user_id=101)

        self.assertFalse(await self.shop.authorize_stars(upgrade['id'], 101, upgrade['amount_stars'], 'old'))
        updated = await self.shop.create_upgrade_order(101, 5, 'stars')
        self.assertGreater(updated['amount_stars'], upgrade['amount_stars'])
        self.assertTrue(await self.shop.authorize_stars(updated['id'], 101, updated['amount_stars'], 'updated'))

    async def test_crypto_webhook_recovers_missing_invoice_id_and_checks_raw_signature(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        await self.db.execute("UPDATE orders SET status='expired' WHERE id=?", (order['id'],))
        invoice = {'invoice_id': 123, 'bot_invoice_url': 'https://pay.example/invoice',
                   'currency_type': 'fiat', 'fiat': 'RUB', 'payload': order['id'],
                   'status': 'paid', 'amount': str(order['amount_rub']), 'paid_at': in_seconds(-300)}
        body = json.dumps({'update_type': 'invoice_paid', 'payload': invoice}, indent=2).encode()
        key = hashlib.sha256(self.config.cryptopay_token.encode()).digest()
        headers = {'crypto-pay-api-signature': hmac.new(key, body, hashlib.sha256).hexdigest()}
        self.payments.crypto.api = AsyncMock(side_effect=[TimeoutError, {'items': [invoice]}])
        async with TestClient(TestServer(make_app(self.config, self.shop, self.payments))) as client:
            invalid = await client.post('/webhooks/cryptopay', data=body + b' ', headers=headers)
            self.assertEqual(invalid.status, 401)
            self.payments.crypto.api.assert_not_awaited()
            with self.assertLogs('aurora_bot.server', level='WARNING'):
                retry = await client.post('/webhooks/cryptopay', data=body, headers=headers)
            self.assertEqual(retry.status, 503)
            self.assertEqual((await self.shop.order(order['id']))['external_id'], '')
            for _ in range(2):
                response = await client.post('/webhooks/cryptopay', data=body, headers=headers)
                self.assertEqual(response.status, 200)
        current = await self.shop.order(order['id'])
        self.assertEqual((current['status'], current['external_id'], current['paid_at']),
                         ('paid', '123', invoice['paid_at']))
        self.assertEqual(len(await self.shop.purchases(101)), 1)
        self.assertEqual(self.payments.crypto.api.await_count, 2)
        self.payments.crypto.api.assert_awaited_with('getInvoices', {'invoice_ids': '123'})

    async def test_2328_polling_recovers_missing_invoice_id(self):
        order = await self.shop.create_order(101, 1, '2328')
        await self.db.execute("UPDATE orders SET status='expired' WHERE id=?", (order['id'],))
        self.payments.gateway.api = AsyncMock(return_value={
            'uuid': 'recovered-id', 'order_id': order['id'], 'amount': str(order['amount_rub']),
            'currency': 'RUB', 'url': 'https://pay.example/invoice', 'payment_status': 'overpaid'})
        await reconcile_external(self.config, self.db, self.payments)
        self.payments.gateway.api.assert_awaited_once_with('v1/payment/info', {'order_id': order['id']})
        current = await self.shop.order(order['id'])
        self.assertEqual((current['status'], current['external_id']), ('paid', 'recovered-id'))
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_crypto_polling_recovers_missing_id_without_webhook(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        await self.db.execute("UPDATE orders SET status='expired' WHERE id=?", (order['id'],))
        invoice = {'invoice_id': 123, 'bot_invoice_url': 'https://pay.example/invoice',
                   'currency_type': 'fiat', 'fiat': 'RUB', 'payload': order['id'],
                   'status': 'paid', 'amount': str(order['amount_rub']), 'paid_at': in_seconds(-300)}
        self.payments.crypto.api = AsyncMock(return_value={'items': [
            {**invoice, 'invoice_id': 456, 'amount': '1'}, invoice,
        ]})
        await reconcile_external(self.config, self.db, self.payments)
        await reconcile_external(self.config, self.db, self.payments)
        self.payments.crypto.api.assert_awaited_once_with('getInvoices', {'status': 'paid', 'offset': 0, 'count': 100})
        current = await self.shop.order(order['id'])
        self.assertEqual((current['status'], current['external_id'], current['paid_at']),
                         ('paid', '123', invoice['paid_at']))
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_admin_enable_applies_to_worker_and_webhook_without_restart(self):
        config = Config('123:mock', frozenset({99}))
        payments = Payments(config, self.shop)
        order = await self.external_order('cryptopay')
        async with TestClient(TestServer(make_app(config, self.shop, payments))) as client:
            response = await client.post('/webhooks/cryptopay')
            self.assertEqual(response.status, 404)
            with patch.object(CryptoPay, 'check_connection', new_callable=AsyncMock):
                await payments.configure_crypto(token='verified-token', enabled=True)
            response = await client.post('/webhooks/cryptopay')
            self.assertEqual(response.status, 401)
            with patch.object(payments.crypto, 'paid_invoice', new_callable=AsyncMock,
                              return_value={'paid_at': in_seconds(-10)}):
                await reconcile_external(config, self.db, payments)
            self.assertEqual((await self.shop.order(order['id']))['status'], 'paid')
            old_order = await self.external_order('cryptopay')
            await payments.configure_crypto(enabled=False)
            self.assertFalse(payments.crypto_enabled)
            response = await client.post('/webhooks/cryptopay')
            self.assertEqual(response.status, 401)
            invoice = {
                'invoice_id': old_order['external_id'], 'status': 'paid', 'payload': old_order['id'],
                'currency_type': 'fiat', 'fiat': 'RUB', 'amount': str(old_order['amount_rub']),
            }
            body = json.dumps({'update_type': 'invoice_paid', 'payload': invoice}).encode()
            key = hashlib.sha256(payments.crypto.token.encode()).digest()
            headers = {'crypto-pay-api-signature': hmac.new(key, body, hashlib.sha256).hexdigest()}
            payments.crypto.paid_invoice = AsyncMock(return_value=invoice)
            response = await client.post('/webhooks/cryptopay', data=body, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await self.shop.order(old_order['id']))['status'], 'paid')

            pending = await self.external_order('cryptopay')
            await reconcile_external(config, self.db, payments)
            self.assertEqual((await self.shop.order(pending['id']))['status'], 'paid')

    async def test_stars_ledger_refund_prevents_even_temporary_access(self):
        order = await self.shop.create_order(101, 1, 'stars')
        partner = {'type': 'user', 'transaction_type': 'invoice_payment',
                   'user': {'id': 101, 'is_bot': False, 'first_name': 'Tester'},
                   'invoice_payload': order['id']}
        entry = {'id': 'refunded-charge', 'amount': order['amount_stars'], 'date': datetime.now(timezone.utc)}
        ledger = StarTransactions.model_validate({'transactions': [
            {**entry, 'receiver': partner}, {**entry, 'source': partner},
        ]})
        bot = SimpleNamespace(get_star_transactions=AsyncMock(return_value=ledger))
        await reconcile_star_page(bot, self.shop, 0)
        self.assertEqual((await self.shop.order(order['id']))['status'], 'refunded')
        self.assertEqual(await self.shop.purchases(101), [])

    async def test_one_external_invoice_cannot_settle_two_orders(self):
        first = await self.external_order(paid=True)
        second = await self.shop.create_order(101, 1, '2328')
        with self.assertRaises(ShopError):
            await self.shop.set_external(second['id'], '2328', first['external_id'], first['payment_url'])
        await self.db.execute('UPDATE orders SET external_id=? WHERE id=?', (first['external_id'], second['id']))
        with self.assertRaises(ShopError):
            await self.shop.settle(second['id'], '2328', external_id=first['external_id'])
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_2328_reconciliation_serializes_status_reads_for_one_invoice(self):
        order = await self.external_order(paid=True)
        entered, release, second_lookup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def old_status(_):
            entered.set()
            await release.wait()
            return {'payment_status': 'refund_fail'}

        async def new_status(_):
            second_lookup.set()
            return {'payment_status': 'refund_process'}

        responses = iter((old_status, new_status))

        async def info(order):
            return await next(responses)(order)

        self.payments.gateway.info = AsyncMock(side_effect=info)
        poll = asyncio.create_task(self.payments.reconcile(order, refund_only=True))
        webhook = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            webhook = asyncio.create_task(self.payments.reconcile(
                order, refund_only=True, expected_status='refund_process',
                webhook_invoice_id=order['external_id']))
            done, _ = await asyncio.wait((webhook,), timeout=0.1)
            self.assertFalse(done)
            self.assertFalse(second_lookup.is_set())
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(poll, *([webhook] if webhook else [])), timeout=3)
        self.assertTrue(second_lookup.is_set())
        self.assertIsNotNone((await self.shop.order(order['id']))['refund_requested_at'])

    async def test_manual_payment_check_acknowledges_before_external_request(self):
        order = await self.external_order()
        call = SimpleNamespace(data=f"check:{order['id']}", from_user=SimpleNamespace(id=101), answer=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock())

        async def reconcile(_):
            call.answer.assert_awaited_once_with()
            await self.shop.refund_gateway(order['id'], order['external_id'])

        await check_payment(call, bot, self.shop, SimpleNamespace(reconcile=reconcile))
        bot.send_message.assert_awaited_once_with(101, 'Возврат подтверждён.', request_timeout=5)

    async def test_2328_webhook_requires_api_to_confirm_missing_invoice_id(self):
        order = await self.shop.create_order(101, 1, '2328')
        webhook = self.event({**order, 'external_id': 'confirmed-id'}, 'paid')
        response = {**webhook, 'url': 'https://pay.example/invoice'}
        self.payments.gateway.api = AsyncMock(side_effect=[{**response, 'uuid': 'other-id'}, response])
        async with TestClient(TestServer(make_app(self.config, self.shop, self.payments))) as client:
            with self.assertLogs('aurora_bot.server', level='WARNING'):
                mismatch = await client.post('/webhooks/2328', json=webhook)
            self.assertEqual(mismatch.status, 503)
            self.assertEqual((await self.shop.order(order['id']))['external_id'], '')
            confirmed = await client.post('/webhooks/2328', json=webhook)
            self.assertEqual(confirmed.status, 200)
        self.assertEqual((await self.shop.order(order['id']))['external_id'], 'confirmed-id')
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_payments_before_long_outage_are_recovered(self):
        order = await self.external_order('cryptopay')
        await self.db.execute("UPDATE orders SET status='expired',created_at=?,expires_at=? WHERE id=?",
                              (in_seconds(-10 * 86400), in_seconds(-9 * 86400), order['id']))
        payment_date = in_seconds(-9 * 86400)
        self.payments.crypto.paid_invoice = AsyncMock(return_value={'paid_at': payment_date})
        await reconcile_external(self.config, self.db, self.payments)
        self.assertEqual((await self.shop.order(order['id']))['status'], 'paid')
        self.assertEqual((await self.shop.order(order['id']))['paid_at'], payment_date)
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_ledger_recovery_keeps_payment_date_and_full_access_period(self):
        await self.db.execute('UPDATE tariffs SET duration_days=30 WHERE id=1')
        order = await self.shop.create_order(101, 1, 'stars')
        payment_date = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=5)
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?',
                              ((payment_date - timedelta(minutes=10)).isoformat(), order['id']))
        ledger = StarTransactions.model_validate({'transactions': [{
            'id': 'ledger-charge', 'amount': order['amount_stars'], 'date': payment_date,
            'source': {'type': 'user', 'transaction_type': 'invoice_payment',
                       'user': {'id': 101, 'is_bot': False, 'first_name': 'Tester'},
                       'invoice_payload': order['id']},
        }]})
        bot = SimpleNamespace(get_star_transactions=AsyncMock(return_value=ledger))
        await reconcile_star_page(bot, self.shop, 0)
        self.assertEqual((await self.shop.order(order['id']))['paid_at'], payment_date.isoformat())
        purchase = (await self.shop.purchases(101))[0]
        self.assertGreaterEqual(purchase['started_at'], in_seconds(-2))
        self.assertGreaterEqual(purchase['expires_at'], in_seconds(30 * 86400 - 2))

    async def test_archive_batch_does_not_starve_new_invoices(self):
        for _ in range(12):
            old = await self.external_order()
            await self.db.execute("UPDATE orders SET status='expired',created_at=?,expires_at=? WHERE id=?",
                                  (in_seconds(-10 * 86400), in_seconds(-9 * 86400), old['id']))
        recent = await self.external_order()
        self.payments.reconcile = AsyncMock(return_value=False)
        await reconcile_external(self.config, self.db, self.payments)
        ids = [call.args[0]['id'] for call in self.payments.reconcile.await_args_list]
        self.assertIn(recent['id'], ids)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(ids), len(set(ids)))

    async def test_processing_and_failed_refund_transitions(self):
        order = await self.external_order(paid=True)
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'refund_process'})
        await reconcile_external(self.config, self.db, self.payments)
        paused = await self.shop.order(order['id'])
        self.assertTrue(paused['refund_requested_at'])
        self.assertLessEqual(paused['reconcile_at'], in_seconds(61))
        await self.db.execute('UPDATE orders SET reconcile_at=NULL WHERE id=?', (order['id'],))
        self.payments.gateway.info.return_value = {'payment_status': 'refund_fail'}
        await reconcile_external(self.config, self.db, self.payments)
        resumed = await self.shop.order(order['id'])
        self.assertEqual(resumed['status'], 'paid')
        self.assertIsNone(resumed['refund_requested_at'])
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_failed_refund_is_not_proof_of_full_payment(self):
        order = await self.external_order()
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'refund_fail'})
        self.assertFalse(await self.payments.reconcile(order))
        self.assertEqual((await self.shop.order(order['id']))['status'], 'pending')
        self.assertEqual(await self.shop.purchases(101), [])

    async def test_refund_webhooks_are_confirmed_and_monotonic(self):
        order = await self.external_order(paid=True)
        self.payments.gateway.info = AsyncMock()
        async with TestClient(TestServer(make_app(self.config, self.shop, self.payments))) as client:
            for status in ('refund_process', 'refund_fail', 'refund_process', 'refund_paid'):
                self.payments.gateway.info.return_value = {'payment_status': status}
                response = await client.post('/webhooks/2328', json=self.event(order, status))
                self.assertEqual(response.status, 200)
                current = await self.shop.order(order['id'])
                self.assertEqual(bool(current['refund_requested_at']), status == 'refund_process')
            self.payments.gateway.info.reset_mock()
            for status in ('refund_process', 'refund_fail', 'paid'):
                response = await client.post('/webhooks/2328', json=self.event(order, status))
                self.assertEqual(response.status, 200)
            self.payments.gateway.info.assert_not_awaited()
        self.assertEqual((await self.shop.order(order['id']))['status'], 'refunded')

    async def test_refund_webhook_does_not_acknowledge_stale_merchant_state(self):
        order = await self.external_order(paid=True)
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'paid'})
        async with TestClient(TestServer(make_app(self.config, self.shop, self.payments))) as client:
            with self.assertLogs('aurora_bot.server', level='WARNING'):
                response = await client.post('/webhooks/2328', json=self.event(order, 'refund_process'))
            self.assertEqual(response.status, 503)
            self.assertTrue((await self.shop.order(order['id']))['refund_requested_at'])

    async def test_delayed_processing_webhook_accepts_authoritative_failed_refund(self):
        order = await self.external_order(paid=True)
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'refund_fail'})
        async with TestClient(TestServer(make_app(self.config, self.shop, self.payments))) as client:
            response = await client.post('/webhooks/2328', json=self.event(order, 'refund_process'))
            self.assertEqual(response.status, 200)
        current = await self.shop.order(order['id'])
        self.assertEqual(current['status'], 'paid')
        self.assertIsNone(current['refund_requested_at'])

    async def test_parallel_creation_reuses_persisted_invoice(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        self.payments.crypto.create = AsyncMock(return_value=('123', 'https://pay.example/invoice'))
        results = await asyncio.gather(*(self.payments.create_external(order) for _ in range(3)))
        self.assertEqual(results, [('123', 'https://pay.example/invoice')] * 3)
        self.payments.crypto.create.assert_awaited_once()

    async def test_expired_order_cannot_create_provider_invoice(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        await self.db.execute('UPDATE orders SET expires_at=? WHERE id=?', (in_seconds(-1), order['id']))
        self.payments.crypto.create = AsyncMock()
        with self.assertRaises(ShopError):
            await self.payments.create_external(order)
        self.payments.crypto.create.assert_not_awaited()

    async def test_crypto_creation_checks_returned_price_and_payload(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        invoice = {'invoice_id': 123, 'bot_invoice_url': 'https://pay.example/invoice',
                   'currency_type': 'fiat', 'fiat': 'RUB', 'payload': order['id'],
                   'amount': str(order['amount_rub'])}
        for changes in ({'amount': '1'}, {'payload': 'another-order'}, {'invoice_id': None}):
            self.payments.crypto.api = AsyncMock(return_value={**invoice, **changes})
            with self.subTest(changes=changes), self.assertRaises(GatewayError):
                await self.payments.crypto.create(order)
        self.payments.crypto.api.return_value = invoice
        self.assertEqual(await self.payments.crypto.create(order), ('123', 'https://pay.example/invoice'))

    async def test_provider_success_flags_have_strict_types(self):
        crypto, gateway = CryptoPay(self.config), Gateway2328(self.config)
        with patch('aurora_bot.payments._json_post', new_callable=AsyncMock) as post:
            post.return_value = {'ok': 'false', 'result': {}}
            with self.assertRaises(GatewayError):
                await crypto.api('getInvoices', {})
            post.return_value = {'state': False, 'result': {}}
            with self.assertRaises(GatewayError):
                await gateway.api('v1/payment/info', {})

    async def test_failed_server_start_cleans_up_runner(self):
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock(side_effect=OSError('port busy')))
        with patch('aurora_bot.server.web.AppRunner', return_value=runner), \
             patch('aurora_bot.server.web.TCPSite', return_value=site):
            with self.assertRaises(OSError):
                await start_server(self.config, self.shop, self.payments)
        runner.cleanup.assert_awaited_once()

    async def test_2328_settles_and_reconciles_refunds_after_checkout_is_disabled(self):
        pending = await self.external_order()
        config = replace(self.config, allow_external_methods=False, public_base_url='')
        payments = Payments(config, self.shop)
        self.assertFalse(config.gateway_enabled)
        self.assertTrue(config.gateway_configured)
        payments.gateway.info = AsyncMock(return_value={'payment_status': 'paid'})

        async with TestClient(TestServer(make_app(config, self.shop, payments))) as client:
            response = await client.post('/webhooks/2328', json=self.event(pending, 'paid'))
            self.assertEqual(response.status, 200)
        self.assertEqual((await self.shop.order(pending['id']))['status'], 'paid')
        payments.gateway.info.return_value = {'payment_status': 'refund_paid'}
        await reconcile_external(config, self.db, payments)
        self.assertEqual((await self.shop.order(pending['id']))['status'], 'refunded')

    async def test_2328_retry_after_timeout_uses_the_same_merchant_order_id(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='', delivery_urls=? WHERE id=1",
                              ('["https://t.me/+test"]',))
        await self.db.set_setting('support_url', 'https://t.me/support')

        async def create(method, payload):
            if self.payments.gateway.api.await_count == 1:
                raise TimeoutError
            self.assertEqual(method, 'v1/payment')
            return {**payload, 'uuid': 'provider-1', 'url': 'https://pay.example/invoice'}

        self.payments.gateway.api = AsyncMock(side_effect=create)
        order = await self.shop.create_order(101, 1, '2328')
        with self.assertRaises(TimeoutError):
            await self.payments.create_external(order)
        self.assertEqual(await self.payments.create_external(order), ('provider-1', 'https://pay.example/invoice'))

        self.assertEqual(self.payments.gateway.api.await_count, 2)
        first, second = [item.args[1] for item in self.payments.gateway.api.await_args_list]
        self.assertEqual(first, second)
        self.assertEqual((await self.shop.order(first['order_id']))['external_id'], 'provider-1')
        self.assertEqual((await self.db.read("SELECT COUNT(*) AS n FROM orders WHERE provider='2328'"))['n'], 1)

    async def test_nearly_expired_2328_invoice_recovered_after_timeout_is_reused(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='', delivery_urls=? WHERE id=1",
                              ('["https://t.me/+test"]',))
        await self.db.set_setting('support_url', 'https://t.me/support')
        order = await self.shop.create_order(101, 1, '2328')
        self.payments.gateway.create = AsyncMock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            await self.payments.create_external(order)
        self.payments.gateway.info = AsyncMock(return_value={
            'uuid': 'provider-1', 'url': 'https://pay.example/original',
            'expires_at': in_seconds(120), 'payment_status': 'check',
        })
        await reconcile_external(self.config, self.db, self.payments)
        self.payments.gateway.create.reset_mock()

        self.assertEqual(await self.payments.create_external(order), ('provider-1', 'https://pay.example/original'))
        self.payments.gateway.create.assert_not_awaited()
        self.assertEqual((await self.db.read("SELECT COUNT(*) AS n FROM orders WHERE provider='2328'"))['n'], 1)

    async def test_creating_external_invoice_reserves_shared_promo_against_stars(self):
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('ONE',100,1)")
        await self.shop.activate_promo(101, 'ONE')
        external = await self.shop.create_order(101, 1, '2328')
        stars = await self.shop.create_order(101, 1, 'stars')
        started, release = asyncio.Event(), asyncio.Event()

        async def create(_):
            started.set()
            await release.wait()
            return 'provider-1', 'https://pay.example/original'

        self.payments.gateway.create = AsyncMock(side_effect=create)
        creation = asyncio.create_task(self.payments.create_external(external))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            self.assertFalse(await self.shop.authorize_stars(stars['id'], 101, stars['amount_stars'], 'stars'))
        finally:
            release.set()
            await creation
        saved = await self.shop.order(external['id'])
        self.assertEqual((saved['status'], saved['external_id']), ('pending', 'provider-1'))
        self.assertFalse(await self.shop.authorize_stars(stars['id'], 101, stars['amount_stars'], 'stars'))

        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'paid'})
        self.assertTrue(await self.payments.reconcile(saved))
        self.assertTrue(await self.payments.reconcile(saved))
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_2328_refund_failure_releases_pending_order_for_later_confirmed_payment(self):
        pending = await self.external_order()
        await self.shop.pause_gateway_delivery(pending['id'], pending['external_id'])
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'refund_fail'})
        await self.payments.reconcile(pending, refund_only=True)
        self.assertIsNone((await self.shop.order(pending['id']))['refund_requested_at'])
        self.assertEqual(await self.shop.purchases(101), [])

        self.payments.gateway.info.return_value = {'payment_status': 'paid'}
        self.assertTrue(await self.payments.reconcile(pending))
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_concurrent_external_creations_reserve_only_one_remaining_promo_use(self):
        await self.db.register(202)
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('ONE',100,1)")
        await self.shop.activate_promo(101, 'ONE')
        await self.shop.activate_promo(202, 'ONE')
        orders = (await self.shop.create_order(101, 1, '2328'),
                  await self.shop.create_order(202, 1, 'cryptopay'))
        self.payments.gateway.create = AsyncMock(return_value=('gw-1', 'https://pay.example/gw'))
        self.payments.crypto.create = AsyncMock(return_value=('123', 'https://pay.example/cp'))

        results = await asyncio.gather(*(self.payments.create_external(order) for order in orders),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ShopError) for result in results), 1)
        self.assertEqual(self.payments.gateway.create.await_count + self.payments.crypto.create.await_count, 1)
        saved = [await self.shop.order(order['id']) for order in orders]
        self.assertEqual({order['status'] for order in saved}, {'pending', 'expired'})

    async def test_external_invoice_uses_the_actual_provider_deadline(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        deadline = in_seconds(120)
        self.payments.crypto.api = AsyncMock(return_value={
            'invoice_id': 123, 'bot_invoice_url': 'https://pay.example/invoice',
            'currency_type': 'fiat', 'fiat': 'RUB', 'payload': order['id'],
            'amount': str(order['amount_rub']), 'expiration_date': deadline,
        })
        await self.payments.create_external(order)
        self.assertEqual((await self.shop.order(order['id']))['expires_at'], deadline)

    async def test_cancelled_2328_invoice_is_removed_from_reuse_but_can_settle_late(self):
        order = await self.external_order()
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'cancel'})
        self.assertFalse(await self.payments.reconcile(order))
        self.assertEqual((await self.shop.order(order['id']))['status'], 'expired')
        with self.assertRaises(ShopError):
            await self.payments.create_external(order)

        self.payments.gateway.info.return_value = {'payment_status': 'paid'}
        self.assertTrue(await self.payments.reconcile(order))
        self.assertEqual(len(await self.shop.purchases(101)), 1)

    async def test_crypto_creation_timeout_remains_recoverable_after_a_long_outage(self):
        order = await self.shop.create_order(101, 1, 'cryptopay')
        self.payments.crypto.api = AsyncMock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            await self.payments.create_external(order)
        await self.db.execute('UPDATE orders SET created_at=? WHERE id=?', (in_seconds(-5 * 86400), order['id']))
        self.payments.crypto.api = AsyncMock(return_value={'items': [{
            'invoice_id': 123, 'bot_invoice_url': 'https://pay.example/invoice',
            'currency_type': 'fiat', 'fiat': 'RUB', 'payload': order['id'],
            'amount': str(order['amount_rub']), 'status': 'paid',
        }]})
        await reconcile_external(self.config, self.db, self.payments)
        self.assertEqual((await self.shop.order(order['id']))['status'], 'paid')

