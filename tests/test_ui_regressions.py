from __future__ import annotations

import asyncio

from pathlib import Path
from tempfile import TemporaryDirectory
from time import time, monotonic
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import ApproveChatJoinRequest, SendMessage
from aiogram.types import Message

from aurora_bot import handlers_admin as admin
from aurora_bot.access import AccessManager
from aurora_bot.config import Config
from aurora_bot.db import Database, in_seconds
from aurora_bot.greetings import greet_request, observe_channel, retry_welcome_approvals, send_welcome
from aurora_bot.handlers_user import (UserInput, before_checkout, check_payment, claim_promo_one_click,
                                      external_pay_invoice, my_accesses, my_orders,
                                      pay_options, payment_support, profile_callback, profile_message,
                                      promo_start, show_catalog, successful_stars, support_fallback,
                                      tariff_card, tariff_preview, track_shop_interaction, welcome)
from aurora_bot.payments import Payments
from aurora_bot.service import Shop, ShopError
from aurora_bot.ui import (MENU_STATS_PREFIX, is_https_url, kb, cb, menu, plain_html, send_section,
                           send_text, validate_section_html)


class HTMLRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_html_fallback_preserves_all_text_and_keyboard(self):
        markup = kb((cb('Каталог', 'catalog'),))
        content = '<b>' + 'Long &amp; complete ' * 150 + '</b>'
        method = SendMessage(chat_id=1, text=content)
        sender = AsyncMock(side_effect=[TelegramBadRequest(method=method, message='Invalid HTML'), True])
        await send_text(sender, content, markup)
        self.assertEqual(sender.await_args_list[1].args[0], plain_html(content))
        self.assertIsNone(sender.await_args_list[1].kwargs['parse_mode'])
        self.assertEqual(sender.await_args_list[1].kwargs['reply_markup'], markup)

    async def test_expanded_long_text_is_sent_completely_in_utf16_safe_chunks(self):
        sender = AsyncMock()
        text = '😀<&>' * 3000
        content = '<b>' + text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') + '</b>'
        markup = kb((cb('Каталог', 'catalog'),))
        await send_text(sender, content, markup)
        self.assertEqual(''.join(call.args[0] for call in sender.await_args_list), text)
        for call in sender.await_args_list:
            self.assertLessEqual(len(call.args[0].encode('utf-16-le')) // 2, 4000)
            self.assertIsNone(call.kwargs['parse_mode'])
        self.assertTrue(all(call.kwargs['reply_markup'] is None for call in sender.await_args_list[:-1]))
        self.assertEqual(sender.await_args_list[-1].kwargs['reply_markup'], markup)

    async def test_emoji_caption_does_not_overflow_and_lose_cover(self):
        message = NS(answer_photo=AsyncMock(), answer=AsyncMock())
        content = '😀' * 600
        await send_section(message, {'media_type': 'photo', 'media_id': 'cover', 'value': content})
        self.assertIsNone(message.answer_photo.await_args.kwargs['caption'])
        self.assertEqual(message.answer.await_args.args[0], content)

    def test_telegram_html_validator_accepts_supported_formatting(self):
        for text in ('<b>A <i>B</i></b> &amp; &#x1F600;',
                     '<a href="https://t.me/{bot_username}?start=privacy">Условия</a>',
                     '<a href="tg://user?id=123">Пользователь</a>',
                     '<span class="tg-spoiler">Скрыто</span>',
                     '<pre><code class="language-python">print(1)</code></pre>',
                     '<blockquote expandable>Цитата</blockquote>',
                     '<tg-time unix="1647531900" format="wDT">Завтра</tg-time>',
                     '<tg-time unix="1647531900" format="">Завтра</tg-time>'):
            with self.subTest(text=text):
                validate_section_html(text)

    def test_telegram_html_validator_rejects_invalid_saved_markup(self):
        for text in ('<b>broken', '<b><i>broken</b></i>', '<script>bad</script>',
                     '<a href="javascript:alert(1)">bad</a>', '<a>missing URL</a>',
                     '<b style="color:red">bad</b>', 'A & B', 'A &amp B', '&nbsp;', '<b></b>',
                     '<b>hello</b> <![CDATA[LOST]]>', '<tg-time unix="1" format="invalid">bad</tg-time>',
                     '<pre><b>bad</b></pre>', '<blockquote><blockquote>bad</blockquote></blockquote>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_section_html(text)

    def test_invalid_https_port_is_rejected_and_legacy_menu_name_is_bounded(self):
        self.assertFalse(is_https_url('https://example.com:invalid'))
        self.assertLess(len(menu('X' * 2000).keyboard[2][0].text), 100)


class UserFlowRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / 'ui.db'))
        await self.db.init()
        await self.db.register(101, 'alice', 'Alice')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111' WHERE id=1")
        self.shop = Shop(self.db)

    def call(self, data):
        return NS(data=data, from_user=NS(id=101, full_name='Alice'), answer=AsyncMock(),
                  message=NS(answer=AsyncMock()))

    async def test_catalog_pages_keep_large_store_accessible(self):
        shop = NS(catalogs=AsyncMock(return_value=[{'id': n, 'title': f'Product {n}'} for n in range(1, 122)]),
                  user_promo=AsyncMock(return_value=None))
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await show_catalog(self.call('catalog'), shop, self.db)
            keyboard = sender.await_args.args[2].inline_keyboard
            self.assertEqual(len(keyboard), 11)
            self.assertEqual(keyboard[-1][0].callback_data, 'catalog:1')
            await show_catalog(self.call('catalog:12'), shop, self.db, page=12)
            keyboard = sender.await_args.args[2].inline_keyboard
            self.assertEqual(keyboard[0][0].callback_data, 'tariff:121')
            self.assertEqual(keyboard[-1][0].callback_data, 'catalog:11')

    async def test_empty_catalog_says_it_is_empty(self):
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await show_catalog(self.call('catalog'), NS(catalogs=AsyncMock(return_value=[])), self.db)
            self.assertIn('Каталог временно пуст', sender.await_args.args[1]['value'])

    async def test_purchase_list_can_reach_older_products(self):
        for index in range(9):
            await self.db.execute('UPDATE tariffs SET title=? WHERE id=1', (f'Товар {index}',))
            order = await self.shop.create_order(101, 1, 'stars')
            await self.shop.settle_stars(order['id'], 101, order['amount_stars'], f'charge-{index}')
        first = self.call('my_orders')
        await my_orders(first, self.db)
        self.assertIn('Товар 8', first.message.answer.await_args.args[0])
        self.assertNotIn('Товар 0', first.message.answer.await_args.args[0])
        keyboard = first.message.answer.await_args.kwargs['reply_markup']
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'my_orders:1')
        second = self.call('my_orders:1')
        await my_orders(second, self.db)
        self.assertIn('Товар 0', second.message.answer.await_args.args[0])
        self.assertNotIn('Товар 8', second.message.answer.await_args.args[0])

    async def test_checkout_does_not_promise_anonymity_or_fixed_delivery_time(self):
        await self.db.set_setting('support_url', 'https://t.me/support_bot')
        bot = NS(
            id=123,
            get_chat=AsyncMock(return_value=NS(type='channel', username=None, active_usernames=None)),
            get_chat_member=AsyncMock(return_value=NS(status='administrator', can_invite_users=True, can_restrict_members=True)),
            create_invoice_link=AsyncMock(side_effect=[RuntimeError('transient'), 'https://t.me/$checkout']),
        )
        first_call = self.call('pay_options:1')
        await pay_options(first_call, bot, self.shop, self.db)
        self.assertIn('Сервис временно недоступен', first_call.answer.await_args.args[0])
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await pay_options(self.call('pay_options:1'), bot, self.shop, self.db)
            text = sender.await_args.args[1]['value']
            self.assertNotIn('аноним', text)
            self.assertNotIn('5 секунд', text)
            self.assertNotIn('Условия покупки', text)
            labels = str(sender.await_args.kwargs['markup'])
            self.assertNotIn('СБП', labels)
            self.assertNotIn('Условия покупки', labels)
            self.assertNotIn('terms:', labels)
            self.assertIn('https://t.me/$checkout', labels)

    async def test_menu_navigation_exits_promo_input(self):
        for menu_text in ('ℹ️ Информация', f'{MENU_STATS_PREFIX} LuxuryHub'):
            with self.subTest(menu_text=menu_text):
                event = Message.model_validate({'message_id': 1, 'date': int(time()),
                    'from': {'id': 101, 'is_bot': False, 'first_name': 'Alice'},
                    'chat': {'id': 101, 'type': 'private'}, 'text': menu_text})
                state = NS(get_state=AsyncMock(return_value=UserInput.promo.state), clear=AsyncMock())
                handler = AsyncMock()
                await track_shop_interaction(handler, event, {'db': self.db, 'state': state})
                state.clear.assert_awaited_once()
                handler.assert_awaited_once()

    async def test_long_support_url_does_not_exceed_callback_alert_limit(self):
        url = 'https://example.com/' + 'x' * 1000
        await self.db.set_setting('support_url', url)
        call = self.call('contact:support')
        await support_fallback(call, self.db)
        call.answer.assert_awaited_once_with()
        self.assertEqual(call.message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url, url)

    async def test_precheckout_passes_deadline_into_atomic_authorization(self):
        query = NS(currency='XTR', invoice_payload='order', from_user=NS(id=101), total_amount=10,
                   id='query', answer=AsyncMock())
        shop = NS(order=AsyncMock(return_value={'id': 'order', 'provider': 'stars', 'user_id': 101,
            'amount_stars': 10, 'status': 'pending', 'expires_at': in_seconds(60),
            'delivery_urls': '["https://t.me/+Test"]', 'channels': '[]'}), authorize_stars=AsyncMock(return_value=True))
        start = monotonic()
        await before_checkout(query, shop, NS())
        self.assertGreater(shop.authorize_stars.await_args.kwargs['deadline'], start)
        self.assertLessEqual(shop.authorize_stars.await_args.kwargs['deadline'], start + 6.6)
        self.assertTrue(query.answer.await_args.kwargs['ok'])

    async def test_pay_options_only_shows_stars_and_enabled_crypto(self):
        await self.db.set_setting('support_url', 'https://t.me/support_bot')
        bot = NS(
            id=123,
            get_chat=AsyncMock(return_value=NS(type='channel', username=None, active_usernames=None)),
            get_chat_member=AsyncMock(return_value=NS(status='administrator', can_invite_users=True, can_restrict_members=True)),
            create_invoice_link=AsyncMock(return_value='https://t.me/$checkout'),
        )
        config = Config(
            '123:mock', frozenset({99}), allow_external_methods=True,
            project_2328='proj', key_2328='sec', public_base_url='https://pay.example.com',
            cryptopay_token='123:crypto',
        )
        payments = Payments(config, self.shop)
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await pay_options(self.call('pay_options:1'), bot, self.shop, self.db, payments, config)
            rows = sender.await_args.kwargs['markup'].inline_keyboard
            self.assertEqual([row[0].callback_data for row in rows], ['ext_pay:cryptopay:1', None, 'tariff:1'])
            self.assertEqual(rows[0][0].style, 'primary')
            self.assertEqual(rows[1][0].url, 'https://t.me/$checkout')
            self.assertNotIn('СБП', sender.await_args.args[1]['value'])

        msg_on = NS(answer=AsyncMock())
        await payment_support(msg_on, self.db, payments)
        self.assertIn('CryptoBot', msg_on.answer.await_args.args[0])

        with patch('aurora_bot.handlers_admin.screen', new_callable=AsyncMock) as adm_screen:
            await admin.payment_settings_screen(NS(message=NS()), self.db, payments)
            btns_on = [b.text for row in adm_screen.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
            self.assertIn('🔴 Выключить CryptoBot', btns_on)

        await payments.configure_crypto(enabled=False)
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await pay_options(self.call('pay_options:1'), bot, self.shop, self.db, payments, config)
            rows_off = sender.await_args.kwargs['markup'].inline_keyboard
            cbs_off = [b.callback_data for row in rows_off for b in row if getattr(b, 'callback_data', None)]
            self.assertNotIn('ext_pay:cryptopay:1', cbs_off)
            self.assertEqual(cbs_off, ['tariff:1'])
            self.assertEqual(rows_off[0][0].style, 'success')
            self.assertNotIn('К оплате в крипте', sender.await_args.args[1]['value'])

        msg_off = NS(answer=AsyncMock())
        await payment_support(msg_off, self.db, payments)
        self.assertNotIn('CryptoBot', msg_off.answer.await_args.args[0])

        with patch('aurora_bot.handlers_admin.screen', new_callable=AsyncMock) as adm_screen:
            await admin.payment_settings_screen(NS(message=NS()), self.db, payments)
            btns_off = [b.text for row in adm_screen.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
            self.assertIn('🟢 Включить CryptoBot', btns_off)

    async def test_crypto_checkout_concurrently_reuses_invoice_and_validates_channels(self):
        bot = NS(
            id=123,
            get_chat=AsyncMock(return_value=NS(type='channel', username=None, active_usernames=None)),
            get_chat_member=AsyncMock(return_value=NS(status='administrator', can_invite_users=True, can_restrict_members=True)),
        )
        config = Config('123:mock', frozenset({99}), cryptopay_token='123:crypto', allow_external_methods=True)
        payments = Payments(config, self.shop)
        url = 'https://t.me/CryptoBot?start=IVcp1'
        with patch.object(payments.crypto, 'create', new=AsyncMock(return_value=('cp-1', url))) as create:
            calls = [self.call('ext_pay:cryptopay:1') for _ in range(2)]
            await asyncio.gather(*(
                external_pay_invoice(call, bot, self.shop, self.db, payments, config) for call in calls
            ))
            create.assert_awaited_once()
            for call in calls:
                rows = call.message.answer.await_args.kwargs['reply_markup'].inline_keyboard
                self.assertEqual(rows[0][0].url, url)
                self.assertTrue(rows[1][0].callback_data.startswith('check:'))
                self.assertEqual(rows[2][0].callback_data, 'pay_options:1')

        with patch('aurora_bot.handlers_user.validate_sale_channels', new=AsyncMock(side_effect=ShopError('Нет прав в канале'))), \
             patch.object(payments.crypto, 'create', new=AsyncMock()) as create:
            call = self.call('ext_pay:cryptopay:1')
            await external_pay_invoice(call, bot, self.shop, self.db, payments, config)
            create.assert_not_awaited()
            call.answer.assert_awaited_once_with('Нет прав в канале', show_alert=True)

    async def test_profile_and_one_click_promo_claim(self):
        cfg = Config('123:mock', frozenset({99}))
        msg = NS(from_user=NS(id=101, username='alice', full_name='Alice'))
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await profile_message(msg, self.shop, self.db, cfg)
            callbacks_default = [btn.callback_data for row in sender.await_args.kwargs['markup'].inline_keyboard for btn in row]
            self.assertNotIn('claim_promo', callbacks_default)
            self.assertIn('Не применён', sender.await_args.args[1]['value'])
            self.assertNotIn('LUXURYNEW', sender.await_args.args[1]['value'])

            acc_call = self.call('accesses')
            await my_accesses(acc_call, self.shop)
            self.assertNotIn('LUXURYNEW', acc_call.message.answer.await_args.args[0])

            promo_call = self.call('promo')
            await promo_start(promo_call, NS(set_state=AsyncMock()))
            self.assertNotIn('LUXURYNEW', promo_call.message.answer.await_args.args[0])

            await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
            await profile_message(msg, self.shop, self.db, cfg)
            callbacks = [btn.callback_data for row in sender.await_args.kwargs['markup'].inline_keyboard for btn in row]
            self.assertIn('claim_promo', callbacks)
            self.assertIn('LUXURYNEW', sender.await_args.args[1]['value'])

            claim_call = self.call('claim_promo')
            await claim_promo_one_click(claim_call, self.shop, self.db)
            self.assertTrue(claim_call.answer.await_args.kwargs['show_alert'])
            self.assertIn('LUXURYNEW активирована', claim_call.answer.await_args.args[0])
            self.assertEqual((await self.shop.profile(101))['active_promo'], 'LUXURYNEW')
            self.assertEqual(sender.await_count, 3)

            await profile_message(msg, self.shop, self.db, cfg)
            callbacks_active = [btn.callback_data for row in sender.await_args.kwargs['markup'].inline_keyboard for btn in row]
            self.assertNotIn('claim_promo', callbacks_active)

            order = await self.shop.create_order(101, 1, 'stars')
            self.assertEqual(order['promo_code'], 'LUXURYNEW')
            self.assertTrue(await self.shop.authorize_stars(order['id'], 101, order['amount_stars'], 'chk-luxury'))
            self.assertTrue(await self.shop.settle_stars(order['id'], 101, order['amount_stars'], 'chg-luxury'))
            self.assertEqual((await self.shop.profile(101))['active_promo'], '')
            await profile_message(msg, self.shop, self.db, cfg)
            callbacks_used = [btn.callback_data for row in sender.await_args.kwargs['markup'].inline_keyboard for btn in row]
            self.assertNotIn('claim_promo', callbacks_used)
            self.assertIn('уже использован', sender.await_args.args[1]['value'])

            stale_call = self.call('claim_promo')
            await claim_promo_one_click(stale_call, self.shop, self.db)
            self.assertTrue(stale_call.answer.await_args.kwargs['show_alert'])
            self.assertNotIn('активирована', stale_call.answer.await_args.args[0])
            self.assertEqual((await self.shop.profile(101))['active_promo'], '')

        await self.db.execute("UPDATE promos SET enabled=0 WHERE code='LUXURYNEW'")
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await profile_message(msg, self.shop, self.db, cfg)
            callbacks_off = [btn.callback_data for row in sender.await_args.kwargs['markup'].inline_keyboard for btn in row]
            self.assertNotIn('claim_promo', callbacks_off)
            self.assertEqual(sender.await_args.args[1]['value'].count('Не применён'), 1)
            self.assertNotIn('LUXURYNEW', sender.await_args.args[1]['value'])

    async def test_buyer_only_upgrade_cta_in_my_accesses(self):
        await self.db.set_setting('support_url', 'https://t.me/support_bot')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id IN (5, 6)")

        # Non-buyer does not see upgrade CTAs or removed order_desk in my_accesses
        non_buyer_call = self.call('accesses')
        await my_accesses(non_buyer_call, self.shop)
        non_buyer_kb = non_buyer_call.message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.url or b.callback_data for row in non_buyer_kb for b in row], ['catalog'])

        # Paying user sees upgrade CTA in my_accesses without order_desk
        paid_order = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(paid_order['id'], 101, paid_order['amount_stars'], 'chg-buyer-1')
        buyer_call = self.call('accesses')
        await my_accesses(buyer_call, self.shop)
        buyer_replies = buyer_call.message.answer.await_args_list
        vip_card_kb = buyer_replies[-1].kwargs['reply_markup'].inline_keyboard
        buttons = [b.url or b.callback_data for row in vip_card_kb for b in row]
        self.assertTrue(any(item.startswith('up_pay_options:') for item in buttons))
        self.assertNotIn('order_desk', buttons)

    async def queue_approval(self, resolver):
        timestamp = int(time())
        await self.db.set_setting('greeting_enabled', '1')
        await self.db.set_setting('greeting_auto_new', '1')
        await observe_channel(self.db, -100999, 'Public', is_admin=True, can_invite=True, timestamp=timestamp)
        await self.db.execute('UPDATE welcome_channels SET auto_approve=1 WHERE chat_id=-100999')
        request = NS(chat=NS(id=-100999, type='channel', title='Public'), from_user=NS(id=101),
                     date=NS(timestamp=lambda: timestamp), invite_link=None, query_id=None)
        with patch('aurora_bot.greetings.send_welcome', new_callable=AsyncMock), \
             patch('aurora_bot.greetings.resolve_join_request', new=resolver):
            await greet_request(NS(id=123), self.db, request)
        await self.db.execute('UPDATE welcome_approvals SET retry_at=?', (in_seconds(-1),))
        return await self.db.read('SELECT * FROM welcome_approvals')

    async def test_new_channels_never_autoapprove_without_manual_opt_in(self):
        timestamp = int(time())
        await self.db.set_setting('greeting_enabled', '1')
        await self.db.set_setting('greeting_auto_new', '1')
        channel = await observe_channel(self.db, -100999, 'Public', is_admin=True,
                                        can_invite=True, timestamp=timestamp)
        self.assertEqual(channel['enabled'], 1)
        self.assertEqual(channel['auto_approve'], 0)
        request = NS(chat=NS(id=-100999, type='channel', title='Public'), from_user=NS(id=101),
                     date=NS(timestamp=lambda: timestamp), invite_link=None, query_id=None)
        with patch('aurora_bot.greetings.send_welcome', new_callable=AsyncMock), \
             patch('aurora_bot.greetings.resolve_join_request', new_callable=AsyncMock) as resolver:
            await greet_request(NS(id=123), self.db, request)
        resolver.assert_awaited_once_with(request, 'queue')
        self.assertIsNone(await self.db.read('SELECT * FROM welcome_approvals'))

    async def test_autoapproval_recovers_without_update_redelivery_or_duplicate_greeting(self):
        timestamp = int(time())
        await self.db.set_setting('greeting_enabled', '1')
        await self.db.set_setting('greeting_auto_new', '1')
        await observe_channel(self.db, -100999, 'Public', is_admin=True, can_invite=True, timestamp=timestamp)
        await self.db.execute('UPDATE welcome_channels SET auto_approve=1 WHERE chat_id=-100999')
        request = NS(chat=NS(id=-100999, type='channel', title='Public'), from_user=NS(id=101),
                     date=NS(timestamp=lambda: timestamp), invite_link=None, query_id='fast-query')
        with patch('aurora_bot.greetings.send_welcome', new_callable=AsyncMock) as welcome, \
             patch('aurora_bot.greetings.resolve_join_request', new_callable=AsyncMock) as resolve:
            resolve.side_effect = TimeoutError
            await greet_request(NS(id=123), self.db, request)
            job = await self.db.read('SELECT * FROM welcome_approvals')
            self.assertEqual(job['attempts'], 1)
            # The first-DM time limit does not prevent approving an older request.
            await self.db.execute('UPDATE greeting_events SET requested_at=?', (timestamp - 86400,))
            await self.db.execute('UPDATE welcome_approvals SET retry_at=?', (in_seconds(-1),))
            bot = NS(approve_chat_join_request=AsyncMock(return_value=True))
            await retry_welcome_approvals(bot, self.db)
            bot.approve_chat_join_request.assert_awaited_once_with(-100999, 101, request_timeout=5)
            welcome.assert_awaited_once()
            resolve.assert_awaited_once()
            self.assertIsNone(await self.db.read('SELECT * FROM welcome_approvals'))

    async def test_pending_autoapproval_is_cancelled_when_disabled_or_channel_becomes_paid(self):
        for setting in ('global', 'channel', 'rights', 'auto_approve', 'paid'):
            with self.subTest(setting=setting):
                await self.db.execute('DELETE FROM welcome_approvals')
                await self.db.execute('DELETE FROM welcome_requests')
                await self.db.execute('DELETE FROM greeting_events')
                await self.db.execute('UPDATE welcome_channels SET enabled=1,can_invite=1,auto_approve=1')
                job = await self.queue_approval(AsyncMock(side_effect=TimeoutError))
                self.assertIsNotNone(job)
                if setting == 'global':
                    await self.db.set_setting('greeting_enabled', '0')
                elif setting == 'paid':
                    await self.db.execute("UPDATE tariffs SET channel_ids='-100999' WHERE id=1")
                else:
                    field = {'channel': 'enabled', 'rights': 'can_invite', 'auto_approve': 'auto_approve'}[setting]
                    await self.db.execute(f'UPDATE welcome_channels SET {field}=0')
                bot = NS(approve_chat_join_request=AsyncMock(return_value=True))
                await retry_welcome_approvals(bot, self.db)
                bot.approve_chat_join_request.assert_not_awaited()
                self.assertIsNone(await self.db.read('SELECT * FROM welcome_approvals'))

    async def test_delivery_ack_and_autoapproval_job_survive_failure_before_first_approval(self):
        with patch.object(self.db, 'paid_channel', new=AsyncMock(side_effect=[False, RuntimeError('restart')])):
            with self.assertRaises(RuntimeError):
                await self.queue_approval(AsyncMock())
        job = await self.db.read('SELECT * FROM welcome_approvals')
        self.assertIsNotNone(job)
        event = await self.db.read('SELECT * FROM greeting_events WHERE id=?', (job['event_id'],))
        self.assertIsNotNone(event['sent_at'])
        await self.db.execute('UPDATE welcome_approvals SET retry_at=?', (in_seconds(-1),))
        bot = NS(approve_chat_join_request=AsyncMock(return_value=True))
        await retry_welcome_approvals(bot, self.db)
        bot.approve_chat_join_request.assert_awaited_once()
        self.assertIsNone(await self.db.read('SELECT * FROM welcome_approvals'))

    async def test_missing_join_request_is_terminal_but_unknown_bad_request_retries(self):
        method = ApproveChatJoinRequest(chat_id=-100999, user_id=101)
        for message, terminal in (('Bad Request: HIDE_REQUESTER_MISSING', True),
                                  ('Bad Request: USER_ALREADY_PARTICIPANT', True),
                                  ('Bad Request: CHAT_ADMIN_REQUIRED', False)):
            with self.subTest(message=message):
                await self.db.execute('DELETE FROM welcome_approvals')
                await self.db.execute('DELETE FROM welcome_requests')
                await self.db.execute('DELETE FROM greeting_events')
                await self.queue_approval(AsyncMock(side_effect=TimeoutError))
                bot = NS(approve_chat_join_request=AsyncMock(side_effect=TelegramBadRequest(
                    method=method, message=message)))
                await retry_welcome_approvals(bot, self.db)
                job = await self.db.read('SELECT * FROM welcome_approvals')
                if terminal:
                    self.assertIsNone(job)
                else:
                    self.assertEqual(job['attempts'], 2)
                    self.assertGreater(job['retry_at'], in_seconds(0))

    async def test_greeting_button_does_not_offer_missing_gift(self):
        bot = NS(me=AsyncMock(return_value=NS(username='test_bot')), send_message=AsyncMock(),
                 send_photo=AsyncMock(), send_video=AsyncMock())
        request = NS(user_chat_id=101, from_user=NS(full_name='Alice'), chat=NS(title='Public'))
        await send_welcome(bot, self.db, request, 'a' * 32)
        message_text = bot.send_message.await_args.args[1]
        keyboard = bot.send_message.await_args.kwargs['reply_markup']
        self.assertIn('Приветствую, Alice!', message_text)
        self.assertIn('собраны все фуллы', message_text)
        self.assertIn('Жми кнопку ниже, чтобы посмотреть каталог', message_text)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, '🔞 Открыть каталог фуллов')
        self.assertEqual(keyboard.inline_keyboard[0][0].style, 'success')
        self.assertTrue(keyboard.inline_keyboard[0][0].url.startswith('https://t.me/test_bot?start='))

    async def test_greeting_fallbacks_for_blank_names_and_missing_bot_username(self):
        bot = NS(me=AsyncMock(return_value=NS(username='')), send_message=AsyncMock(),
                 send_photo=AsyncMock(), send_video=AsyncMock())
        request = NS(user_chat_id=101, from_user=NS(full_name='   '), chat=NS(title=''))
        await send_welcome(bot, self.db, request, 'b' * 32)
        message_text = bot.send_message.await_args.args[1]
        keyboard = bot.send_message.await_args.kwargs['reply_markup']
        self.assertIn('Приветствую, друг!', message_text)
        self.assertIn('«канал»', message_text)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, '🔞 Открыть каталог фуллов')
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'catalog')
        self.assertEqual(keyboard.inline_keyboard[0][0].style, 'success')

    async def test_upgrade_card_strikes_original_price_and_hides_duplicate_full_price_cta(self):
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id IN (1, 5)")
        paid = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(paid['id'], 101, paid['amount_stars'], 'chg-up-card')
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            for handler, cb_data in ((tariff_card, 'tariff:5'), (tariff_preview, 'preview:5')):
                await handler(self.call(cb_data), self.shop)
                rendered = sender.await_args.args[1]['value']
                markup = sender.await_args.kwargs['markup']
                cbs = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
                self.assertIn('<s>', rendered)
                self.assertIn('Цена апгрейда для тебя', rendered)
                self.assertIn('up_pay_options:5', cbs)
                self.assertNotIn('pay_options:5', cbs)

    async def test_my_purchases_only_lists_paid_products_and_profile_opens_it(self):
        cfg = Config('123:mock', frozenset({99}))
        await self.db.execute("UPDATE tariffs SET title='Купленный <товар>' WHERE id=1")
        paid = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(paid['id'], 101, paid['amount_stars'], 'paid-purchase')
        await self.db.execute('UPDATE purchases SET expires_at=? WHERE order_id=?', (in_seconds(-60), paid['id']))

        await self.db.execute("UPDATE tariffs SET title='Возвращённый товар' WHERE id=1")
        refunded = await self.shop.create_order(101, 1, 'stars')
        await self.shop.settle_stars(refunded['id'], 101, refunded['amount_stars'], 'refunded-purchase')
        await self.shop.refund_stars('refunded-purchase', refunded['amount_stars'])

        await self.db.execute("UPDATE tariffs SET title='Ручная выдача' WHERE id=1")
        grant = await self.shop.create_order(101, 1, 'admin')
        await self.shop.settle(grant['id'], 'admin')

        await self.db.register(202)
        await self.db.execute("UPDATE tariffs SET title='Чужая покупка' WHERE id=1")
        other = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle_stars(other['id'], 202, other['amount_stars'], 'other-purchase')

        await self.db.execute("UPDATE tariffs SET title='Неоплаченный товар' WHERE id=1")
        pending = await self.shop.create_order(101, 1, '2328')
        await self.shop.set_external(pending['id'], '2328', 'ext-ord-1', 'https://1fin.example/pay/pending')
        crypto = await self.shop.create_order(101, 1, 'cryptopay')
        await self.shop.set_external(crypto['id'], 'cryptopay', 'cp-ord-1', 'https://t.me/CryptoBot?start=pending')

        call = self.call('my_orders')
        await my_orders(call, self.db)
        self.assertEqual(call.message.answer.await_args.args[0], '🧾 <b>Мои покупки</b>\n\n• Купленный &lt;товар&gt;')
        rows = call.message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([button.callback_data for row in rows for button in row], ['profile'])

        prof_call = self.call('profile')
        with patch('aurora_bot.handlers_user.send_section', new_callable=AsyncMock) as sender:
            await profile_callback(prof_call, self.shop, self.db, cfg)
            sender.assert_awaited_once()
            self.assertIn('Личный кабинет', sender.await_args.args[1]['value'])
            self.assertIn('🧾 Мои покупки', str(sender.await_args.kwargs['markup']))

    async def test_immediate_access_provision_on_stars_and_check_payment_avoids_double_notice(self):
        cfg = Config('123:mock', frozenset())
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id=1")
        access = AccessManager(self.db, self.shop)

        def make_bot():
            return NS(
                create_chat_invite_link=AsyncMock(
                    side_effect=lambda **kw: NS(invite_link=f"https://t.me/+inv-{kw['chat_id']}")
                ),
                send_message=AsyncMock(),
            )

        # 1. Stars payment settles -> immediate provision sends ONLY notice with invite button (no duplicate message)
        s_bot = make_bot()
        s_ord = await self.shop.create_order(101, 1, 'stars')
        paid_msg = NS(
            from_user=NS(id=101), date=None, bot=s_bot, answer=AsyncMock(),
            successful_payment=NS(currency='XTR', invoice_payload=s_ord['id'],
                                  total_amount=s_ord['amount_stars'], telegram_payment_charge_id='chg-imm-1'),
        )
        await successful_stars(paid_msg, self.shop, cfg, access)
        s_bot.create_chat_invite_link.assert_awaited_once()
        s_bot.send_message.assert_awaited_once()
        paid_msg.answer.assert_not_awaited()
        urls = [b.url for row in s_bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard
                for b in row if getattr(b, 'url', None)]
        self.assertEqual(urls, ['https://t.me/+inv--100555'])

        # 2. Manual check_payment on freshly paid external order immediately sends ONLY notice (no second DM)
        c_bot = make_bot()
        await self.db.register(202)
        c_ord = await self.shop.create_order(202, 1, '2328')
        await self.shop.set_external(c_ord['id'], '2328', 'ext-imm-2', 'https://1fin.example/pay/2')
        chk_call = NS(data=f"check:{c_ord['id']}", from_user=NS(id=202), answer=AsyncMock())

        async def reconcile(_):
            await self.shop.settle(c_ord['id'], '2328', external_id='ext-imm-2')

        await check_payment(chk_call, c_bot, self.shop, NS(reconcile=reconcile), cfg, access)
        c_bot.create_chat_invite_link.assert_awaited_once()
        c_bot.send_message.assert_awaited_once()
        self.assertEqual(c_bot.send_message.await_args.args[0], 202)


if __name__ == '__main__':
    unittest.main()
