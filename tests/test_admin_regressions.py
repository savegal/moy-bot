from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
import sqlite3
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from aurora_bot import handlers_admin as admin
from aurora_bot import handlers_user as public
from aurora_bot.access import AccessManager
from aurora_bot.config import Config
from aurora_bot.db import Database, now
from aurora_bot.payments import CryptoPay, GatewayError, Payments
from aurora_bot.service import Shop


class OfflineAdminSession(AiohttpSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True


class AdminRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / 'review.sqlite3'))
        await self.db.init()
        await self.db.register(101, 'alice', 'Alice')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111',duration_days=0 WHERE id=1")
        self.shop = Shop(self.db)
        self.access = AccessManager(self.db, self.shop)
        self.storage = MemoryStorage()
        self.addAsyncCleanup(self.storage.close)
        self.state = FSMContext(self.storage, StorageKey(bot_id=123, chat_id=99, user_id=99))

    def message(self, text=None):
        return NS(text=text, from_user=NS(id=99, full_name='Admin'), chat=NS(id=99),
                  message_id=12, answer=AsyncMock())

    def call(self, data):
        return NS(data=data, from_user=NS(id=99), message=self.message(), answer=AsyncMock())

    async def paid(self):
        order = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(order['id'], 101, order['amount_stars'], order['id'])
        return order, (await self.shop.purchases(101))[0]

    async def test_nontext_settings_input_does_not_erase_value(self):
        for key in ('project_name', 'support_url', 'reviews_url', 'partnership_contact'):
            with self.subTest(key=key):
                await self.db.set_setting(key, 'original')
                await self.state.set_state(admin.AdminState.settings_field)
                await self.state.update_data(key=key)
                message = self.message()
                await admin.settings_save(message, self.state, self.db)
                self.assertEqual((await self.db.setting(key))['value'], 'original')
                self.assertIsNotNone(await self.state.get_state())
                self.assertIn('текстом', message.answer.call_args.args[0])

    async def test_dispatcher_enforces_admin_and_private_chat_guards(self):
        order, purchase = await self.paid()
        original_title = (await self.shop.get_tariff(1))['title']
        original_expiry = purchase['expires_at']
        session = OfflineAdminSession()
        bot = Bot('123:offline', session=session)
        config = Config('123:offline', frozenset({99}))
        payments = Payments(config, self.shop)
        dispatcher = Dispatcher(db=self.db, shop=self.shop, access=self.access,
                                config=config, payments=payments)
        routers = (admin.router, public.router)
        # aiogram has no public detach operation; restore global routers even when
        # another test previously attached them to its own Dispatcher.
        original_parents = [(router, router.parent_router,
                             router.parent_router.sub_routers.index(router) if router.parent_router else None)
                            for router in routers]
        for router, parent, _ in original_parents:
            if parent:
                parent.sub_routers.remove(router)
            router._parent_router = None
            dispatcher.include_router(router)

        def message(uid, chat_id, text='menu', *, from_bot=False):
            return {'message_id': 1, 'date': 1794220000,
                    'from': {'id': 123 if from_bot else uid, 'first_name': 'Tester', 'is_bot': from_bot},
                    'chat': {'id': chat_id, 'type': 'private' if chat_id > 0 else 'supergroup'}, 'text': text}

        async def feed(**fields):
            await dispatcher.feed_update(bot, Update.model_validate({'update_id': 1, **fields}))

        async def click(uid, chat_id, data):
            await feed(callback_query={'id': 'query', 'from': {'id': uid, 'first_name': 'Tester', 'is_bot': False},
                'chat_instance': 'test', 'message': message(uid, chat_id, from_bot=True), 'data': data})

        try:
            for uid, chat_id in ((101, 101), (99, -300)):
                with self.subTest(uid=uid, chat_id=chat_id):
                    context = dispatcher.fsm.get_context(bot=bot, chat_id=chat_id, user_id=uid)
                    await feed(message=message(uid, chat_id, '/admin'))
                    for data in ('adm:home', 'adm:tset:1:0', 'adm:tf:1:title', 'adm:grant:new',
                                 'adm:give:101', 'adm:payments', 'adm:crypto:token:0',
                                 'adm:crypto:enable:1', f"adm:refundok:{order['id']}",
                                 f"adm:revokeok:{purchase['id']}"):
                        await click(uid, chat_id, data)
                        self.assertIsNone(await context.get_state())
                    await feed(message=message(uid, chat_id, '101 1 30'))
                    self.assertEqual((await self.shop.get_tariff(1))['title'], original_title)
                    self.assertEqual((await self.shop.get_tariff(1))['active'], 1)
                    self.assertEqual((await self.shop.order(order['id']))['status'], 'paid')
                    self.assertEqual((await self.shop.purchases(101))[0]['expires_at'], original_expiry)
                    self.assertEqual(len(await self.shop.purchases(101)), 1)
            self.assertEqual(session.calls, [])

            # Ordinary users still reach the real public catalog handler.
            await feed(message=message(101, 101, public.MENU_CATALOG))
            self.assertTrue(any(call.__api_method__ == 'sendMessage' for call in session.calls))

            # Positive control: the same admin form works for an admin in private.
            await click(99, 99, 'adm:tf:1:title')
            context = dispatcher.fsm.get_context(bot=bot, chat_id=99, user_id=99)
            self.assertEqual(await context.get_state(), admin.AdminState.tariff_field.state)
            await feed(message=message(99, 99, 'Updated product'))
            self.assertEqual((await self.shop.get_tariff(1))['title'], 'Updated product')
            self.assertIsNone(await context.get_state())
            self.assertFalse(any(call.__api_method__ == 'refundStarPayment' for call in session.calls))

            await click(99, 99, 'adm:payments')
            panel = session.calls[-1]
            self.assertIn('Платежи', panel.text)
            self.assertIn('adm:crypto:token:0', [
                button.callback_data for row in panel.reply_markup.inline_keyboard for button in row])
            await click(99, 99, 'adm:payments:stars')
            self.assertTrue(any(call.__api_method__ == 'getStarTransactions' for call in session.calls))
            await click(99, 99, 'adm:crypto:token:0')
            self.assertEqual(await context.get_state(), admin.AdminState.crypto_token.state)
            token = '12345:merchant-secret'
            with patch.object(CryptoPay, 'check_connection', new_callable=AsyncMock) as verify:
                await feed(message=message(99, 99, token))
                self.assertEqual(payments.crypto.token, token)
                self.assertFalse(payments.crypto_enabled)
                self.assertIsNone(await context.get_state())
                await click(99, 99, 'adm:crypto:enable:1')
                self.assertTrue(payments.crypto_enabled)
                self.assertEqual(verify.await_count, 2)
            self.assertTrue(any(call.__api_method__ == 'deleteMessage' for call in session.calls))
            self.assertNotIn(token, repr(session.calls))
            reloaded = Payments(config, self.shop)
            self.assertTrue(reloaded.crypto_enabled)
            self.assertEqual(reloaded.crypto.token, token)
            await click(99, 99, 'adm:crypto:enable:0')
            self.assertFalse(payments.crypto_enabled)
            self.assertFalse(Payments(config, self.shop).crypto_enabled)
        finally:
            for router, _, _ in original_parents:
                if router in dispatcher.sub_routers:
                    dispatcher.sub_routers.remove(router)
                router._parent_router = None
            for router, parent, index in original_parents:
                if parent:
                    router._parent_router = parent
                    parent.sub_routers.insert(index, router)
            await dispatcher.fsm.close()
            await bot.session.close()

    async def test_failed_crypto_check_preserves_active_credentials(self):
        config = Config('123:offline', frozenset({99}), cryptopay_token='configured-token',
                        allow_external_methods=True)
        payments = Payments(config, self.shop)
        with patch.object(CryptoPay, 'check_connection', new_callable=AsyncMock,
                          side_effect=GatewayError('test token rejected')):
            with self.assertRaises(GatewayError):
                await payments.configure_crypto(token='invalid-token', testnet=True)
        self.assertEqual(payments.crypto.token, 'configured-token')
        self.assertFalse(payments.crypto.testnet)
        self.assertTrue(payments.crypto_enabled)
        self.assertFalse(payments.crypto_settings_path.exists())

    async def test_refund_audit_failure_cannot_pause_access_or_call_telegram(self):
        order, _ = await self.paid()
        bot = NS(refund_star_payment=AsyncMock())
        with patch.object(self.db, 'audit', new_callable=AsyncMock,
                          side_effect=sqlite3.OperationalError('Audit unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                await admin.refund_confirmed(self.call(f"adm:refundok:{order['id']}"), self.shop, self.db, bot)
        saved = await self.shop.order(order['id'])
        self.assertEqual(saved['status'], 'paid')
        self.assertIsNone(saved['refund_requested_at'])
        bot.refund_star_payment.assert_not_awaited()

    async def test_project_name_limit_prevents_oversized_documents(self):
        await self.state.set_state(admin.AdminState.settings_field)
        await self.state.update_data(key='project_name')
        previous = (await self.db.setting('project_name'))['value']
        message = self.message('A' * 81)
        await admin.settings_save(message, self.state, self.db)
        self.assertEqual((await self.db.setting('project_name'))['value'], previous)
        self.assertIn('80', message.answer.call_args.args[0])

    async def test_duration_edit_rejects_concurrent_switch_to_static_links(self):
        snapshot = await self.shop.get_tariff(1)
        await self.db.execute("UPDATE tariffs SET channel_ids='',delivery_urls='[\"https://t.me/+Saved\"]' WHERE id=1")
        await self.state.set_state(admin.AdminState.tariff_field)
        await self.state.update_data(tid=1, field='duration_days')
        message = self.message('30')
        with patch.object(self.shop, 'get_tariff', AsyncMock(return_value=snapshot)):
            await admin.edit_field_save(message, self.state, self.db, NS(), self.shop)
        tariff = await self.shop.get_tariff(1)
        self.assertEqual(tariff['duration_days'], 0)
        self.assertIn('другой сессии', message.answer.call_args.args[0])

    async def test_static_links_edit_rejects_concurrent_duration_change(self):
        snapshot = await self.shop.get_tariff(1)
        await self.db.execute('UPDATE tariffs SET duration_days=30 WHERE id=1')
        await self.state.set_state(admin.AdminState.tariff_field)
        await self.state.update_data(tid=1, field='delivery')
        message = self.message('https://t.me/+Saved')
        with patch.object(self.shop, 'get_tariff', AsyncMock(return_value=snapshot)):
            await admin.edit_field_save(message, self.state, self.db, NS(), self.shop)
        tariff = await self.shop.get_tariff(1)
        self.assertEqual(tariff['delivery_urls'], '[]')
        self.assertEqual(tariff['duration_days'], 30)
        self.assertIn('другой сессии', message.answer.call_args.args[0])

    async def test_delivery_cannot_be_cleared_during_concurrent_publication(self):
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id=1')
        snapshot = await self.shop.get_tariff(1)
        await self.db.execute('UPDATE tariffs SET active=1 WHERE id=1')
        await self.state.set_state(admin.AdminState.tariff_field)
        await self.state.update_data(tid=1, field='delivery')
        message = self.message('-')
        with patch.object(self.shop, 'get_tariff', AsyncMock(return_value=snapshot)):
            await admin.edit_field_save(message, self.state, self.db, NS(), self.shop)
        self.assertEqual((await self.shop.get_tariff(1))['channel_ids'], '-100111')
        self.assertIn('другой сессии', message.answer.call_args.args[0])

    async def test_pending_refund_is_not_shown_as_active_access(self):
        order, purchase = await self.paid()
        await self.db.execute('UPDATE orders SET refund_requested_at=? WHERE id=?', (now(), order['id']))
        record = await admin.purchase_record(self.db, purchase['id'])
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.purchase_details(self.call(''), self.db, record)
            self.assertIn('Возврат проверяется', screen.call_args.args[1])
            self.assertNotIn('Состояние <b>Активен</b>', screen.call_args.args[1])
            user = await self.db.read('SELECT * FROM users WHERE user_id=101')
            await admin.user_details(self.call(''), self.db, user)
            self.assertIn('активных доступов <b>0</b>', screen.call_args.args[1])
            await admin.grant_menu(self.call('adm:grants'), self.db)
            data = [button.callback_data for row in screen.call_args.kwargs['reply_markup'].inline_keyboard for button in row]
            self.assertNotIn(f"adm:purchase:{purchase['id']}", data)
            await admin.order_details(self.call(''), self.db, await self.shop.order(order['id']))
            self.assertIn('приостановлена на время возврата', screen.call_args.args[1])

    async def test_failed_member_removal_is_visible_and_can_be_retried(self):
        order, purchase = await self.paid()
        await self.db.execute("UPDATE purchases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (purchase['id'],))
        await self.db.execute("""UPDATE grants SET state='joined',revoke_retry_at='2999-01-01T00:00:00+00:00',
            last_error='Missing administrator rights' WHERE purchase_id=?""", (purchase['id'],))
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.grant_menu(self.call('adm:grants:errors:0'), self.db)
            buttons = [button.callback_data for row in screen.call_args.kwargs['reply_markup'].inline_keyboard for button in row]
            self.assertIn(f"adm:purchase:{purchase['id']}", buttons)
            record = await admin.purchase_record(self.db, purchase['id'])
            await admin.purchase_details(self.call(''), self.db, record)
            self.assertIn('Не удалось исключить', screen.call_args.args[1])
            self.assertIn('Missing administrator rights', screen.call_args.args[1])
            buttons = [button.callback_data for row in screen.call_args.kwargs['reply_markup'].inline_keyboard for button in row]
            self.assertIn(f"adm:retryrevoke:{purchase['id']}", buttons)
            await admin.retry_revocation(self.call(f"adm:retryrevoke:{purchase['id']}"), self.db, self.access)
        self.assertIsNone((await self.db.read('SELECT revoke_retry_at FROM grants WHERE purchase_id=?', (purchase['id'],)))['revoke_retry_at'])
        await self.db.execute("UPDATE purchases SET expires_at=NULL WHERE id=?", (purchase['id'],))
        await self.db.execute("UPDATE grants SET revoke_retry_at='2999-01-01T00:00:00+00:00' WHERE purchase_id=?", (purchase['id'],))
        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.retry_revocation(self.call(f"adm:retryrevoke:{purchase['id']}"), self.db, self.access)
        self.assertIsNotNone((await self.db.read('SELECT revoke_retry_at FROM grants WHERE purchase_id=?', (purchase['id'],)))['revoke_retry_at'])

    async def test_manual_payment_decision_keeps_updated_order_navigation(self):
        order = await self.shop.create_order(101, 1, 'manual')
        await self.shop.manual_review(order['id'], 101)
        with patch.object(admin, 'order_details', new_callable=AsyncMock) as details:
            await admin.approve_manual(self.call(f"adm:approve:{order['id']}"), self.shop, self.db)
            self.assertEqual(details.call_args.args[2]['status'], 'paid')
        second = await self.shop.create_order(101, 1, 'manual')
        await self.shop.manual_review(second['id'], 101)
        with patch.object(admin, 'order_details', new_callable=AsyncMock) as details:
            await admin.reject_manual(self.call(f"adm:reject:{second['id']}"), self.db, NS(send_message=AsyncMock()))
            self.assertEqual(details.call_args.args[2]['status'], 'failed')

    async def test_broadcast_sends_the_confirmed_copy_instead_of_mutable_original(self):
        await self.db.execute('UPDATE users SET broadcast_opt_in=1 WHERE user_id=101')
        message = self.message('Original announcement')
        message.answer.return_value = NS(message_id=333)
        bot = NS(copy_message=AsyncMock(return_value=NS(message_id=222)))
        await self.state.set_state(admin.AdminState.broadcast)
        await admin.broadcast_preview(message, self.state, self.db, bot)
        self.assertEqual((await self.state.get_data())['broadcast_msg_id'], 222)
        call = self.call('adm:sendbroadcast')
        call.message.message_id = 333
        progress = NS(edit_text=AsyncMock())
        call.message.answer.return_value = progress
        bot.copy_message.reset_mock()
        with patch.object(admin.asyncio, 'sleep', new_callable=AsyncMock):
            await admin.broadcast_send(call, self.state, self.db, bot)
            await asyncio.gather(*(t for t, _ in list(admin._ACTIVE_BROADCASTS.values())))
        bot.copy_message.assert_awaited_once_with(101, 99, 222)
        self.assertIsNone(await self.state.get_state())

    async def test_invalid_section_html_is_rejected_before_saving(self):
        await self.db.set_setting('info', '<b>Original</b>')
        await self.state.set_state(admin.AdminState.section_field)
        await self.state.update_data(section='info', field='text')
        message = self.message('<b>Unclosed text')
        await admin.section_edit_save(message, self.state, self.db)
        self.assertEqual((await self.db.setting('info'))['value'], '<b>Original</b>')
        self.assertIn('Текст не сохранён', message.answer.call_args.args[0])
        self.assertIsNotNone(await self.state.get_state())
        message.text = '<b>Valid {project_name}</b>'
        await admin.section_edit_save(message, self.state, self.db)
        self.assertEqual((await self.db.setting('info'))['value'], message.text)
        self.assertIsNone(await self.state.get_state())

    async def test_readiness_reports_missing_product_photo(self):
        await self.db.set_setting('support_url', 'https://t.me/support')
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id!=1')
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.readiness(self.call('adm:health'), self.shop, self.db, NS())
        self.assertIn('Загрузите фото или видео товара', screen.call_args.args[1])

    async def test_shortcut_creation_keeps_valid_fields_when_delivery_needs_correction(self):
        await self.state.set_state(admin.AdminState.tariff_new)
        await self.state.update_data(values={})
        message = self.message('VIP|Description|900|350|0|invalid-delivery')
        await admin.new_tariff_save(message, self.state, self.db, NS(), self.shop)
        self.assertEqual((await self.state.get_data())['values'], {
            'title': 'VIP', 'description': 'Description', 'stars': 350, 'rub': 900, 'duration_days': 0})
        message.text = 'https://t.me/+Saved'
        with patch.object(admin, 'tariff_screen', new_callable=AsyncMock):
            await admin.new_tariff_save(message, self.state, self.db, NS(), self.shop)
        tariff = await self.db.read("SELECT * FROM tariffs WHERE title='VIP'")
        self.assertEqual(tariff['active'], 0)
        self.assertEqual(tariff['delivery_urls'], '["https://t.me/+Saved"]')
        self.assertIsNone(await self.state.get_state())

    async def test_segmented_broadcasts_and_whales_tab(self):
        # Seed cold (202), entry buyer (101: 240 RUB), whale (303: ULTRA VIP 1190 RUB + LTV)
        await self.db.register(202, 'cold', 'Cold')
        await self.db.register(303, 'whale', 'Whale')
        await self.db.register(404, 'admingrant', 'Free')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100222',duration_days=0,active=1 WHERE id=5")

        o_entry = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(o_entry['id'], 101, o_entry['amount_stars'], 'bcast-entry')
        o_whale = await self.shop.create_order(303, 5, 'stars')
        await self.shop.settle_stars(o_whale['id'], 303, o_whale['amount_stars'], 'bcast-whale')
        o_free = await self.shop.create_order(404, 1, 'admin', duration_days=7)
        await self.shop.settle(o_free['id'], 'admin')

        # Top LTV whales tab shows 303 first (1190), then 101 (240), excluding cold (202) and free grant (404)
        with patch.object(admin, 'screen', new_callable=AsyncMock) as scr:
            await admin.user_list(self.call('adm:users:whales:0'), self.db)
            whale_btns = [btn.callback_data for row in scr.await_args.kwargs['reply_markup'].inline_keyboard
                          for btn in row if btn.callback_data and btn.callback_data.startswith('adm:user:')]
            self.assertEqual(whale_btns, ['adm:user:303', 'adm:user:101'])

        # Segmented broadcasts deliver strictly to each segment
        for seg, expected_ids in (('cold', [202, 404]), ('entry', [101]), ('whales', [303])):
            await self.state.clear()
            with patch.object(admin, 'screen', new_callable=AsyncMock):
                await admin.broadcast_choose_segment(self.call(f'adm:bcast:seg:{seg}'), self.state, self.db)
            bcast_bot = NS(copy_message=AsyncMock(side_effect=[NS(message_id=777)] + [True] * 10))
            msg = self.message(f'Promo for {seg}')
            msg.answer.return_value = NS(message_id=888)
            await admin.broadcast_preview(msg, self.state, self.db, bcast_bot)
            confirm = self.call('adm:sendbroadcast')
            confirm.message.message_id = 888
            confirm.message.answer.return_value = NS(edit_text=AsyncMock())
            with patch.object(admin.asyncio, 'sleep', new_callable=AsyncMock):
                await admin.broadcast_send(confirm, self.state, self.db, bcast_bot)
                await asyncio.gather(*(t for t, _ in list(admin._ACTIVE_BROADCASTS.values())))
            sent_to = [c.args[0] for c in bcast_bot.copy_message.await_args_list[1:]]
            self.assertEqual(sent_to, expected_ids, f'segment={seg}')

    async def test_admin_queues_tier_toggle_and_nonblocking_broadcast_stop(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id=1")
        o = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(o['id'], 101, o['amount_stars'], 'chg-adm-q')

        with patch.object(admin, 'screen', new_callable=AsyncMock) as scr:
            await admin.admin_home(self.call('adm:home'), self.db)
            home_btns = [b.text for row in scr.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
            self.assertFalse(any('Стол заказов' in t or 'Счета' in t for t in home_btns))

            # Tier toggle on tariff #1: '' -> 'ultra' -> 'diamond' -> 'regular'
            for expected in ('ultra', 'diamond', 'regular'):
                await admin.tariff_cycle_tier(self.call('adm:ttier:1'), self.shop, self.db)
                self.assertEqual((await self.shop.get_tariff(1))['tier'], expected)

            # Explicit 'regular' suppresses title-based upgrade matching on ULTRA-titled tariff #5
            await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id=5")
            await admin.tariff_cycle_tier(self.call('adm:ttier:5'), self.shop, self.db)  # ultra -> diamond
            await admin.tariff_cycle_tier(self.call('adm:ttier:5'), self.shop, self.db)  # diamond -> regular
            self.assertEqual((await self.shop.get_tariff(5))['tier'], 'regular')
            self.assertNotIn(5, {item['tariff']['id'] for item in await self.shop.upgrade_offers(101)})

        # 2. Non-blocking broadcast starts in background and stops cleanly on adm:bcast:stop
        for uid in (202, 303, 404):
            await self.db.register(uid)
        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.broadcast_choose_segment(self.call('adm:bcast:seg:all'), self.state, self.db)
        msg = self.message('Test campaign')
        msg.answer.return_value = NS(message_id=901)
        bcast_bot = NS(copy_message=AsyncMock(side_effect=[NS(message_id=777)] + [True] * 10))
        await admin.broadcast_preview(msg, self.state, self.db, bcast_bot)
        bcast_bot.copy_message.reset_mock()

        first_sent = asyncio.Event()
        release_sleep = asyncio.Event()

        async def slow_copy(*args):
            first_sent.set()
            return True

        async def gated_sleep(_):
            await release_sleep.wait()

        bcast_bot.copy_message.side_effect = slow_copy
        confirm = self.call('adm:sendbroadcast')
        confirm.message.message_id = 901
        progress = NS(edit_text=AsyncMock())
        confirm.message.answer.return_value = progress

        with patch.object(admin.asyncio, 'sleep', side_effect=gated_sleep):
            await admin.broadcast_send(confirm, self.state, self.db, bcast_bot)
            self.assertIn(99, admin._ACTIVE_BROADCASTS)
            await asyncio.wait_for(first_sent.wait(), timeout=2)
            stop_call = self.call('adm:bcast:stop')
            await admin.broadcast_stop(stop_call)
            release_sleep.set()
            await asyncio.gather(*(t for t, _ in list(admin._ACTIVE_BROADCASTS.values())))

        self.assertEqual(bcast_bot.copy_message.await_count, 1)
        self.assertIn('Рассылка остановлена', progress.edit_text.await_args.args[0])
        self.assertNotIn(99, admin._ACTIVE_BROADCASTS)

    async def test_easy_section_editing_copy_rich_entities_and_reset(self):
        from datetime import datetime, timezone
        from aiogram.types import Chat, Message, MessageEntity, User
        from aurora_bot.db import DEFAULT_SETTINGS

        # 1. List shows media & >950 warning badges
        await self.db.set_setting('welcome', 'W' * 960, media_id='wphoto', media_type='photo')
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.sections(self.call('adm:sections'), self.db)
        markup = screen.await_args.kwargs['reply_markup']
        btn_texts = [row[0].text for row in markup.inline_keyboard]
        self.assertTrue(any('Приветствие (новый пользователь)' in t and '🖼' in t and '⚠️' in t for t in btn_texts))

        # 2. Section card shows full template (>600 chars) in <pre>, length warning & reset button
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.section_card(self.call('adm:section:welcome'), self.db)
        card_text = screen.await_args.args[1]
        card_kb = screen.await_args.kwargs['reply_markup']
        self.assertIn(f"<pre>{'W' * 960}</pre>", card_text)
        self.assertIn('960 / 950', card_text)
        self.assertTrue(any(btn.callback_data == 'adm:sreset:welcome'
                            for row in card_kb.inline_keyboard for btn in row))

        # 3. Edit start shows current template for 1-tap copy and variable hints
        edit_call = self.call('adm:sf:join_welcome:text')
        await admin.section_edit_start(edit_call, self.state, self.db)
        prompt_text = edit_call.message.answer.await_args.args[0]
        self.assertIn('<pre>', prompt_text)
        self.assertIn('{channel}', prompt_text)
        self.assertIn('Можно оформлять прямо в Telegram', prompt_text)

        # 4. Rich Telegram entities (+ raw '&') convert cleanly to Telegram HTML on save
        real_msg = Message(
            message_id=50,
            date=datetime.now(timezone.utc),
            chat=Chat(id=99, type='private'),
            from_user=User(id=99, is_bot=False, first_name='Admin'),
            text='Привет {name} & канал {channel}',
            entities=[MessageEntity(type='bold', offset=0, length=6)],
        )
        expected_html = admin._extract_section_html(real_msg)
        self.assertEqual(expected_html, '<b>Привет</b> {name} &amp; канал {channel}')
        rich_msg = NS(
            text=real_msg.text,
            entities=real_msg.entities,
            html_text=real_msg.html_text,
            photo=None,
            video=None,
            from_user=NS(id=99),
            answer=AsyncMock(),
        )
        await admin.section_edit_save(rich_msg, self.state, self.db)
        self.assertEqual((await self.db.setting('join_welcome'))['value'], expected_html)
        saved_reply = rich_msg.answer.await_args.args[0]
        saved_kb = rich_msg.answer.await_args.kwargs['reply_markup']
        self.assertIn('Длина:', saved_reply)
        self.assertTrue(any(btn.callback_data == 'adm:spreview:join_welcome'
                            for row in saved_kb.inline_keyboard for btn in row))
        self.assertIsNone(await self.state.get_state())

        # 5. Raw HTML containing an auto-detected 'url' entity stays raw HTML (not double-escaped)
        await self.state.set_state(admin.AdminState.section_field)
        await self.state.update_data(section='help', field='text')
        raw_html = 'Помощь {project_name}: <a href="https://t.me/{bot_username}?start=privacy">Конфиденциальность</a>'
        url_msg = NS(
            text=raw_html,
            entities=[MessageEntity(type='url', offset=29, length=46)],
            html_text='double-escaped-should-not-be-used',
            photo=None,
            video=None,
            from_user=NS(id=99),
            answer=AsyncMock(),
        )
        await admin.section_edit_save(url_msg, self.state, self.db)
        self.assertEqual((await self.db.setting('help'))['value'], raw_html)

        # 6. Two-step reset restores DEFAULT_SETTINGS while keeping banner media intact
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.section_reset(self.call('adm:sreset:welcome'), self.db)
        self.assertIn('Сбросить текст раздела', screen.await_args.args[1])
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.section_reset(self.call('adm:sreset:welcome:yes'), self.db)
        restored = await self.db.setting('welcome')
        self.assertEqual(restored['value'], DEFAULT_SETTINGS['welcome'])
        self.assertEqual((restored['media_id'], restored['media_type']), ('wphoto', 'photo'))

    async def test_tariff_card_and_preview_support_photo_and_video(self):
        from aurora_bot.ui import has_product_photo, tariff_view_media

        tid = 1
        await self.db.execute("UPDATE tariffs SET active=0, channel_ids='-100111' WHERE id=?", (tid,))

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            # 1. Attach VIDEO as product card cover and publish
            await admin.tariff_media_start(self.call(f'adm:tm:{tid}'), self.state, self.shop)
            self.assertEqual((await self.state.get_data()).get('media_target'), 'cover')
            vid_msg = NS(
                from_user=NS(id=99),
                photo=None,
                video=NS(file_id='card-video-1'),
                text=None,
                answer=AsyncMock(),
            )
            await admin.tariff_media_save(vid_msg, self.state, self.db, self.shop)
        t = await self.shop.get_tariff(tid)
        self.assertEqual((t['media_id'], t['media_type']), ('card-video-1', 'video'))
        self.assertTrue(has_product_photo(t))

        bot = NS(
            get_chat=AsyncMock(return_value=NS(type='channel', title='C', username=None, active_usernames=None)),
            get_chat_member=AsyncMock(return_value=NS(status='administrator', can_invite_users=True, can_restrict_members=True)),
        )
        bot.id = 777
        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.tariff_publish(self.call(f'adm:tset:{tid}:1'), self.shop, self.db, bot)
        self.assertEqual((await self.shop.get_tariff(tid))['active'], 1)

        # 2. Attach separate PHOTO to "Что внутри?" (adm:tpm) and verify separate rendering vs fallback
        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.tariff_media_start(self.call(f'adm:tpm:{tid}'), self.state, self.shop)
            self.assertEqual((await self.state.get_data()).get('media_target'), 'preview')
            photo_msg = NS(
                from_user=NS(id=99),
                photo=[NS(file_id='inside-photo-1')],
                video=None,
                text=None,
                answer=AsyncMock(),
            )
            await admin.tariff_media_save(photo_msg, self.state, self.db, self.shop)
        t = await self.shop.get_tariff(tid)
        self.assertEqual(tariff_view_media(t, details=False), ('card-video-1', 'video'))
        self.assertEqual(tariff_view_media(t, details=True), ('inside-photo-1', 'photo'))

        def user_call(cb_data: str):
            return NS(
                data=cb_data,
                from_user=NS(id=202, username='u202', full_name='U202'),
                message=self.message(),
                answer=AsyncMock(),
            )

        with patch.object(public, 'send_section', new_callable=AsyncMock) as user_send:
            await public.tariff_card(user_call(f'tariff:{tid}'), self.shop)
            self.assertEqual(
                (user_send.await_args.args[1]['media_id'], user_send.await_args.args[1]['media_type']),
                ('card-video-1', 'video'),
            )
            await public.tariff_preview(user_call(f'preview:{tid}'), self.shop)
            self.assertEqual(
                (user_send.await_args.args[1]['media_id'], user_send.await_args.args[1]['media_type']),
                ('inside-photo-1', 'photo'),
            )

        with patch.object(admin, 'send_section', new_callable=AsyncMock) as adm_send:
            await admin.tariff_preview_admin(self.call(f'adm:tpreview:{tid}:details'), self.shop)
            self.assertEqual(
                (adm_send.await_args.args[1]['media_id'], adm_send.await_args.args[1]['media_type']),
                ('inside-photo-1', 'photo'),
            )

        # 3. Preview video can also be attached and cleared while product stays published
        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.tariff_media_start(self.call(f'adm:tpm:{tid}'), self.state, self.shop)
            prev_vid = NS(
                from_user=NS(id=99),
                photo=None,
                video=NS(file_id='inside-video-2'),
                text=None,
                answer=AsyncMock(),
            )
            await admin.tariff_media_save(prev_vid, self.state, self.db, self.shop)
        self.assertEqual(tariff_view_media(await self.shop.get_tariff(tid), details=True), ('inside-video-2', 'video'))

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.tariff_media_start(self.call(f'adm:tpm:{tid}'), self.state, self.shop)
            clear_msg = NS(from_user=NS(id=99), photo=None, video=None, text='удалить', answer=AsyncMock())
            await admin.tariff_media_save(clear_msg, self.state, self.db, self.shop)
        self.assertEqual(tariff_view_media(await self.shop.get_tariff(tid), details=True), ('card-video-1', 'video'))

        # 4. GIF/animation upload + send_section dispatch + active-cover hint + >950 caption warning
        from aurora_bot.ui import send_section

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.tariff_media_start(self.call(f'adm:tpm:{tid}'), self.state, self.shop)
            gif_msg = NS(
                from_user=NS(id=99),
                photo=None,
                video=None,
                animation=NS(file_id='inside-gif-3'),
                text=None,
                answer=AsyncMock(),
            )
            await admin.tariff_media_save(gif_msg, self.state, self.db, self.shop)
        t_gif = await self.shop.get_tariff(tid)
        self.assertEqual(tariff_view_media(t_gif, details=True), ('inside-gif-3', 'animation'))

        fake_msg = NS(answer_animation=AsyncMock())
        await send_section(fake_msg, {'value': 'Short preview caption', 'media_id': 'inside-gif-3', 'media_type': 'animation'})
        fake_msg.answer_animation.assert_awaited_once()

        with patch.object(admin, 'screen', new_callable=AsyncMock):
            await admin.tariff_media_start(self.call(f'adm:tm:{tid}'), self.state, self.shop)
            bad_msg = NS(from_user=NS(id=99), photo=None, video=None, animation=None, text='not media', answer=AsyncMock())
            await admin.tariff_media_save(bad_msg, self.state, self.db, self.shop)
            self.assertNotIn('удалить', bad_msg.answer.await_args.args[0])

        await self.db.execute('UPDATE tariffs SET description=? WHERE id=?', ('D' * 750, tid))
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.tariff_details(self.call(f'adm:t:{tid}'), self.shop)
        self.assertIn('Превышен лимит подписи с медиа', screen.await_args.args[1])

    async def test_admin_warnings_for_unlinked_active_tariff_and_hidden_review_pushes(self):
        await self.db.execute("UPDATE tariffs SET active=1, channel_ids='', delivery_urls='[]', allow_no_channel=0 WHERE id=3")
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.tariff_details(self.call('adm:t:3'), self.shop)
        self.assertIn('без привязанного канала', screen.await_args.args[1])

        await self.db.set_setting('review_push_enabled', '1')
        await self.db.set_setting('reviews_visible', '0')
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.review_push_home(self.call('adm:rpush'), self.db)
        self.assertIn('Показ отзывов скрыт в настройках — авто-пуши сейчас на паузе', screen.await_args.args[1])

    async def test_admin_tariff_rate_warning_for_stars_mismatch(self):
        await self.db.execute("UPDATE tariffs SET rub=240, stars=350 WHERE id=1")
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.tariff_details(self.call('adm:t:1'), self.shop)
        warn_text = screen.await_args.args[1]
        self.assertIn('Перекос цены в Stars', warn_text)
        self.assertIn('125 ⭐', warn_text)

        await self.db.execute("UPDATE tariffs SET rub=240, stars=125 WHERE id=1")
        with patch.object(admin, 'screen', new_callable=AsyncMock) as screen:
            await admin.tariff_details(self.call('adm:t:1'), self.shop)
        self.assertNotIn('Перекос цены в Stars', screen.await_args.args[1])


if __name__ == '__main__':
    unittest.main()
