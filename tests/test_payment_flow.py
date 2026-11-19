from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import StarTransactions, Update

from aurora_bot.access import AccessManager
from aurora_bot.analytics import campaign_delivered, campaign_summary, funnel_summary, prepare_campaign, product_sales, record_engagement
from aurora_bot.config import Config
from aurora_bot.db import Database, in_seconds
from aurora_bot.handlers_admin import router as admin_router
from aurora_bot.handlers_user import router as user_router
from aurora_bot.payments import Payments
from aurora_bot.pushes import push_settings, send_promo_push_to_user, send_review_push_to_user
from aurora_bot.server import make_app
from aurora_bot.service import Shop
from aurora_bot.ui import MENU_HELP, MENU_HELP_SIMPLE, MENU_INFO, MENU_PROFILE, MENU_STATS_PREFIX, cb, kb
from aurora_bot.workers import maintain_access, reconcile_external, reconcile_stars


class OfflineTelegram(AiohttpSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.ledger = StarTransactions(transactions=[])

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        match method.__api_method__:
            case 'getMe':
                return SimpleNamespace(username='test_store_bot')
            case 'getChat':
                return SimpleNamespace(type='channel', title='Channel', username=None, active_usernames=None)
            case 'getChatMember':
                if method.user_id != bot.id:
                    return SimpleNamespace(status='member')
                return SimpleNamespace(status='administrator', can_invite_users=True, can_restrict_members=True)
            case 'createChatInviteLink':
                return SimpleNamespace(invite_link=f'https://t.me/+test-{method.chat_id}')
            case 'createInvoiceLink':
                return f'https://t.me/$invoice-{method.payload}'
            case 'getStarTransactions':
                return self.ledger
            case _:
                return True

    def for_method(self, method):
        return [call for call in self.calls if call.__api_method__ == method]


class PaymentFlowTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / 'shop.db'))
        await self.db.init()
        await self.db.set_setting('support_url', 'https://t.me/shop_support')
        await self.db.set_setting('join_welcome', media_id='test-image', media_type='photo')
        await self.db.execute("UPDATE tariffs SET channel_ids='-1001,-1002,-1003,-1004', duration_days=30 WHERE id=1")
        self.shop = Shop(self.db)
        self.access = AccessManager(self.db, self.shop)
        self.config = Config('123:mock', frozenset({99}), allow_external_methods=True,
                             project_2328='project', key_2328='secret', public_base_url='https://shop.example')
        self.payments = Payments(self.config, self.shop)
        self.session = OfflineTelegram()
        self.bot = Bot(self.config.token, session=self.session)
        self.addAsyncCleanup(self.bot.session.close)
        self.request_time = int(time())

    def user(self, uid):
        return {'id': uid, 'first_name': 'Tester', 'is_bot': False}

    def message(self, uid, **fields):
        return {'message_id': 1, 'date': 1794220000, 'from': self.user(uid),
                'chat': {'id': uid, 'type': 'private'}, **fields}

    async def feed(self, **data):
        await self.dp.feed_update(self.bot, Update.model_validate({'update_id': 1, **data}))

    async def click(self, uid, callback_data):
        await self.feed(callback_query={'id': 'callback', 'from': self.user(uid), 'chat_instance': 'test',
                                        'message': self.message(uid, text='menu'), 'data': callback_data})

    async def join(self, uid, chat_id, invite=None, *, creator_id=123):
        data = {'chat': {'id': chat_id, 'type': 'channel', 'title': 'Test'}, 'from': self.user(uid),
                'user_chat_id': uid, 'date': self.request_time}
        if invite:
            data['invite_link'] = {'invite_link': invite, 'creator': {**self.user(creator_id), 'is_bot': creator_id == 123},
                                   'creates_join_request': True, 'is_primary': False, 'is_revoked': False}
        await self.feed(chat_join_request=data)

    async def membership(self, chat_id, *, administrator):
        bot = {**self.user(123), 'is_bot': True}
        left = {'status': 'left', 'user': bot}
        admin = {'status': 'administrator', 'user': bot, 'can_invite_users': True,
                 **dict.fromkeys(('can_be_edited', 'is_anonymous', 'can_manage_chat', 'can_delete_messages',
                                  'can_manage_video_chats', 'can_restrict_members', 'can_promote_members',
                                  'can_change_info', 'can_post_stories', 'can_edit_stories',
                                  'can_delete_stories', 'can_send_welcome_messages'), False)}
        await self.feed(my_chat_member={'chat': {'id': chat_id, 'type': 'channel', 'title': 'New channel'},
                                        'from': self.user(99), 'date': self.request_time,
                                        'old_chat_member': left if administrator else admin,
                                        'new_chat_member': admin if administrator else left})

    def start_dispatcher(self):
        self.dp = Dispatcher(db=self.db, shop=self.shop, access=self.access, config=self.config,
                             payments=self.payments)
        self.dp.include_router(admin_router)
        self.dp.include_router(user_router)
        self.addAsyncCleanup(self.close_dispatcher)

    async def close_dispatcher(self):
        for router in (admin_router, user_router):
            self.dp.sub_routers.remove(router)
            router._parent_router = None
        await self.dp.fsm.close()

    async def forward_channel_photo(self, chat_id):
        origin = {'type': 'channel', 'date': self.request_time, 'message_id': 10,
                  'chat': {'id': chat_id, 'type': 'channel', 'title': 'Forwarded channel'}}
        photo = [{'file_id': 'file', 'file_unique_id': 'unique', 'height': 100, 'width': 100}]
        await self.feed(message=self.message(99, forward_origin=origin, photo=photo))

    async def test_forwarded_channel_binding_and_join_with_shared_link(self):
        self.start_dispatcher()
        shared = 'https://t.me/+sharedProduct'
        await self.db.execute("UPDATE tariffs SET delivery_urls=?,duration_days=0 WHERE id=3", (f'["{shared}"]',))
        await self.db.register(204)
        await self.db.register(206)
        old_paid = await self.shop.create_order(204, 3, 'stars')
        await self.shop.settle_stars(old_paid['id'], 204, old_paid['amount_stars'], 'old-shared-product')
        old_pending = await self.shop.create_order(206, 3, 'stars')

        await self.click(99, 'adm:greetadd')
        await self.forward_channel_photo(-100710)
        self.assertIsNotNone(await self.db.read('SELECT 1 FROM welcome_channels WHERE chat_id=-100710'))
        await self.click(99, 'adm:tf:2:delivery')
        await self.forward_channel_photo(-100710)
        self.assertEqual((await self.shop.get_tariff(2))['channel_ids'], '-100710')
        buyer_link_button = next(
            button for row in self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard
            for button in row if button.text == '🔗 Ссылка покупателю'
        )
        await self.click(99, buyer_link_button.callback_data)
        await self.feed(message=self.message(99, text=shared))
        await self.db.set_setting('greeting_enabled', '1')
        await self.db.register(202)
        order = await self.shop.create_order(202, 2, 'stars')
        await self.shop.settle_stars(order['id'], 202, order['amount_stars'], 'shared-product')
        await self.access.provision(self.bot)
        self.assertEqual((await self.shop.statistics())['grants'], 2)
        self.assertFalse(self.session.for_method('createChatInviteLink'))
        delivery = self.session.for_method('sendMessage')[-1]
        self.assertEqual(delivery.reply_markup.inline_keyboard[0][0].url, shared)
        await self.join(202, -100710, shared, creator_id=99)
        self.assertEqual(self.session.for_method('approveChatJoinRequest')[-1].user_id, 202)
        self.assertFalse(self.session.for_method('sendPhoto'))
        await self.join(203, -100710, shared, creator_id=99)
        self.assertEqual(self.session.for_method('sendPhoto')[-1].chat_id, 203)
        self.assertEqual(self.session.for_method('declineChatJoinRequest')[-1].user_id, 203)
        await self.shop.refund_stars('shared-product', order['amount_stars'])
        await self.join(202, -100710, shared)
        self.assertEqual(self.session.for_method('declineChatJoinRequest')[-1].user_id, 202)
        await self.join(204, -100710, shared)
        self.assertEqual(self.session.for_method('approveChatJoinRequest')[-1].user_id, 204)
        self.assertTrue(await self.shop.authorize_stars(
            old_pending['id'], 206, old_pending['amount_stars'], 'pending-shared-product',
        ))
        await self.shop.settle_stars(old_pending['id'], 206, old_pending['amount_stars'], 'new-shared-product')
        await self.join(206, -100710, shared)
        self.assertEqual(self.session.for_method('approveChatJoinRequest')[-1].user_id, 206)

        await self.click(99, 'adm:newt')
        for value in ('New forwarded product', 'Description', '250', '500', '5'):
            await self.feed(message=self.message(99, text=value))
        await self.forward_channel_photo(-100711)
        draft = await self.db.read("SELECT * FROM tariffs WHERE title='New forwarded product'")
        self.assertEqual((draft['channel_ids'], draft['active']), ('-100711', 0))

    async def test_buyer_link_on_unbound_product_waits_for_forwarded_channel(self):
        self.start_dispatcher()
        invite = 'https://t.me/+buyerLink'
        await self.click(99, 'adm:tf:4:buyer_link')
        await self.feed(message=self.message(99, text=invite))
        self.assertEqual((await self.shop.get_tariff(4))['delivery_urls'], '[]')
        self.assertIsNone(await self.db.read('SELECT 1 FROM product_channel_links WHERE url=?', (invite,)))
        await self.forward_channel_photo(-100714)
        tariff = await self.shop.get_tariff(4)
        self.assertEqual((tariff['channel_ids'], tariff['delivery_urls']), ('-100714', '[]'))
        shared = await self.db.read('SELECT url FROM product_channel_links WHERE chat_id=-100714')
        self.assertEqual(shared['url'], invite)

        await self.click(99, 'adm:tf:4:buyer_link')
        await self.feed(message=self.message(99, text='-'))
        self.assertEqual((await self.shop.get_tariff(4))['channel_ids'], '-100714')
        self.assertIsNone(await self.db.read('SELECT 1 FROM product_channel_links WHERE chat_id=-100714'))

    async def test_no_channel_checkout_with_hidden_contacts_and_campaign_conversion(self):
        self.start_dispatcher()
        await self.db.set_setting('support_url', 'https://t.me/hiddensupport')
        await self.db.set_setting('partnership_contact', '@hiddenmanager')
        await self.click(99, 'adm:reviews:1')
        await self.click(99, 'adm:contacts:0')
        await self.click(202, 'help_menu')
        help_markup = str(self.session.for_method('sendMessage')[-1].reply_markup)
        self.assertNotIn('hiddensupport', help_markup)
        self.assertNotIn('partnership', help_markup)
        self.assertIn('contact:reviews', help_markup)
        await self.click(202, 'partnership')
        self.assertTrue(self.session.for_method('answerCallbackQuery')[-1].show_alert)

        await self.click(99, 'adm:tf:1:delivery')
        await self.feed(message=self.message(99, text='-'))
        self.assertEqual((await self.shop.get_tariff(1))['allow_no_channel'], 1)
        await self.feed(message=self.message(202, text='/start'))
        touch_id, markup = await prepare_campaign(self.db, 202, 'cart', kb((cb('Open', 'tariff:1'),)))
        await campaign_delivered(self.db, touch_id)
        tracked_cb = markup.inline_keyboard[0][0].callback_data
        await self.click(203, tracked_cb)
        self.assertTrue(self.session.for_method('answerCallbackQuery')[-1].show_alert)
        await self.click(202, tracked_cb)
        self.assertIn('pay_options:1', str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.click(202, 'pay_options:1')
        invoice = self.session.for_method('createInvoiceLink')[-1]
        amount = invoice.prices[0].amount
        await self.feed(pre_checkout_query={'id': 'no-channel', 'from': self.user(202), 'currency': 'XTR',
                                           'total_amount': amount, 'invoice_payload': invoice.payload})
        self.assertTrue(self.session.for_method('answerPreCheckoutQuery')[-1].ok)
        await self.shop.settle_stars(invoice.payload, 202, amount, 'no-channel')
        await self.access.provision(self.bot)
        self.assertFalse(self.session.for_method('createChatInviteLink'))
        self.assertIn('Покупка подтверждена', self.session.for_method('sendMessage')[-1].text)
        self.assertEqual(await campaign_summary(self.db, 7), [
            {'campaign': 'cart', 'sent': 1, 'clicked': 1, 'buyers': 1, 'orders': 1, 'stars': amount, 'rub': 0},
        ])
        await self.click(99, 'adm:campaigns:7')
        self.assertIn('Купили 1 (100,0%', self.session.for_method('sendMessage')[-1].text)
        await self.click(99, 'adm:stats:7')
        self.assertIn('Запустили бота · <b>1</b>', self.session.for_method('sendMessage')[-1].text)
        await self.shop.refund_stars('no-channel', amount)
        self.assertEqual((await campaign_summary(self.db, 7))[0]['buyers'], 0)

    async def test_start_menu_promo_and_repeat_tariff_view(self):
        self.start_dispatcher()
        await self.db.set_setting('project_name', 'Test & Club')
        await self.db.set_setting('reviews_visible', '1')
        for uid, command in ((301, '/start'), (302, '/start g_test')):
            with self.subTest(command=command):
                await self.feed(message=self.message(uid, text=command))
                welcome, catalog = self.session.for_method('sendMessage')[-2:]
                self.assertEqual(welcome.reply_markup.keyboard[0][0].text, '📂 Каталог фуллов')
                self.assertEqual(catalog.reply_markup.inline_keyboard[4][0].callback_data, 'tariff:5')
                await self.db.execute(
                    'UPDATE users SET view_reminder_sent=1, view_reminder_sent_at=? WHERE user_id=?',
                    (in_seconds(-60), uid),
                )
                count = len(self.session.for_method('sendMessage'))
                await self.click(uid, 'tariff:5')
                messages = self.session.for_method('sendMessage')[count:]
                self.assertEqual(len(messages), 1)
                self.assertIn('Test &amp; Club', messages[0].text)
                self.assertNotIn('{project_name}', messages[0].text)
                self.assertEqual((await self.shop.profile(uid))['view_reminder_sent'], 1)

        await self.feed(message=self.message(301, text='🔞 Каталог фуллов'))
        self.assertEqual(self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard[0][0].callback_data,
                         'tariff:1')
        await self.feed(message=self.message(301, text='💬 Помощь / Отзывы'))
        markup = self.session.for_method('sendMessage')[-1].reply_markup
        self.assertEqual([row[0].callback_data for row in markup.inline_keyboard[:2]], ['promo', 'profile'])
        await self.click(301, 'promo')
        self.assertIn('Активация промокода', self.session.for_method('sendMessage')[-1].text)
        await self.feed(message=self.message(301, text='⭐ Отзывы и обновления'))
        self.assertEqual(self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard[0][0].callback_data,
                         'contact:reviews')

    async def test_paid_channel_greeting_explains_access_before_decline(self):
        self.start_dispatcher()
        await self.db.set_setting('greeting_enabled', '1')
        await self.join(301, -1001)
        greeting = self.session.for_method('sendPhoto')[-1]
        self.assertIn('выдаётся после оплаты', greeting.caption)
        self.assertNotIn('принята', greeting.caption)
        self.assertEqual(self.session.for_method('declineChatJoinRequest')[-1].user_id, 301)

    async def test_reviews_visibility_changes_menus_and_disables_old_callbacks(self):
        self.start_dispatcher()
        reviews_url = 'https://t.me/test_reviews'
        await self.db.set_setting('reviews_url', reviews_url)
        self.assertEqual((await self.db.setting('reviews_visible'))['value'], '0')
        await self.feed(message=self.message(202, text='/start'))
        self.assertEqual(self.session.for_method('sendMessage')[-2].reply_markup.keyboard[1][0].text, MENU_HELP_SIMPLE)
        await self.click(202, 'help_menu')
        self.assertNotIn(reviews_url, str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.feed(message=self.message(202, text=MENU_INFO))
        self.assertIsNone(self.session.for_method('sendMessage')[-1].reply_markup)
        await self.feed(message=self.message(202, text='/start terms'))
        self.assertNotIn('Отзывы', self.session.for_method('sendMessage')[-1].text)

        await self.click(202, 'adm:reviews:1')
        self.assertEqual((await self.db.setting('reviews_visible'))['value'], '0')
        await self.click(99, 'adm:reviews:1')
        await self.feed(message=self.message(202, text=MENU_HELP_SIMPLE))
        menu_update, help_page = self.session.for_method('sendMessage')[-2:]
        self.assertEqual(menu_update.reply_markup.keyboard[1][0].text, MENU_HELP)
        self.assertIn(reviews_url, str(help_page.reply_markup))
        await self.feed(message=self.message(202, text=MENU_INFO))
        self.assertIn(reviews_url, str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.click(202, 'contact:reviews')
        self.assertIn(reviews_url, str(self.session.for_method('sendMessage')[-1].reply_markup))

        await self.click(99, 'adm:reviews:0')
        await self.feed(message=self.message(202, text=MENU_HELP))
        menu_update, help_page = self.session.for_method('sendMessage')[-2:]
        self.assertEqual(menu_update.reply_markup.keyboard[1][0].text, MENU_HELP_SIMPLE)
        self.assertNotIn(reviews_url, str(help_page.reply_markup))
        for previous_button in ('contact:reviews', 'review_view:0'):
            await self.click(202, previous_button)
            self.assertEqual(self.session.for_method('answerCallbackQuery')[-1].text, 'Раздел временно недоступен.')
        await self.click(99, 'adm:rpush:blastgo:0:all')
        self.assertTrue(self.session.for_method('answerCallbackQuery')[-1].show_alert)

    async def test_help_visibility_toggle_from_admin_updates_menus_and_routes(self):
        self.start_dispatcher()
        await self.db.set_setting('partnership_contact', '@help_mgr')
        self.assertEqual((await self.db.setting('help_visible'))['value'], '1')

        # Initial /start shows Help button in row 1
        await self.feed(message=self.message(202, text='/start'))
        self.assertEqual(
            [btn.text for btn in self.session.for_method('sendMessage')[-2].reply_markup.keyboard[1]],
            [MENU_HELP_SIMPLE, MENU_INFO],
        )

        # Admin screens reflect open state and expose hide buttons in both settings and section card
        await self.click(99, 'adm:home')
        self.assertIn('Помощь · <b>открыта</b>', self.session.for_method('sendMessage')[-1].text)
        await self.click(99, 'adm:settings')
        self.assertIn('adm:help:0', str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.click(99, 'adm:section:help')
        help_card = self.session.for_method('sendMessage')[-1]
        self.assertIn('открыт', help_card.text)
        self.assertIn('adm:section_help:0', str(help_card.reply_markup))

        # Non-admin cannot toggle help visibility
        await self.click(202, 'adm:help:0')
        await self.click(202, 'adm:section_help:0')
        self.assertEqual((await self.db.setting('help_visible'))['value'], '1')

        # Admin hides Help from settings
        await self.click(99, 'adm:help:0')
        self.assertEqual((await self.db.setting('help_visible'))['value'], '0')
        settings_screen = self.session.for_method('sendMessage')[-1]
        self.assertIn('adm:help:1', str(settings_screen.reply_markup))

        # Admin sections list & home screen show hidden status; admin preview still works
        await self.click(99, 'adm:home')
        self.assertIn('Помощь · <b>скрыта</b>', self.session.for_method('sendMessage')[-1].text)
        await self.click(99, 'adm:sections')
        self.assertIn('скрыт', str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.click(99, 'adm:spreview:help')
        self.assertIn('Частые вопросы', self.session.for_method('sendMessage')[-1].text)

        # Cached user tapping old Help button receives updated menu (no Help) + hidden notice
        await self.feed(message=self.message(202, text=MENU_HELP_SIMPLE))
        menu_update, hidden_notice = self.session.for_method('sendMessage')[-2:]
        self.assertEqual([btn.text for btn in menu_update.reply_markup.keyboard[1]], [MENU_INFO])
        self.assertIn('временно скрыт', hidden_notice.text)

        # Old inline help_menu callback is rejected with alert
        await self.click(202, 'help_menu')
        cb_ans = self.session.for_method('answerCallbackQuery')[-1]
        self.assertEqual(cb_ans.text, 'Раздел временно недоступен.')
        self.assertTrue(cb_ans.show_alert)

        # Legal & partnership screens omit 'help_menu' back button and menu reference while Help is hidden
        await self.feed(message=self.message(202, text='/terms'))
        terms_msg = self.session.for_method('sendMessage')[-1]
        self.assertIn('/paysupport', terms_msg.text)
        self.assertNotIn('в нижнем меню бота', terms_msg.text)
        self.assertIsNone(terms_msg.reply_markup)

        await self.click(202, 'partnership')
        self.assertNotIn('help_menu', str(self.session.for_method('sendMessage')[-1].reply_markup))

        # New /start also omits Help button while hidden
        await self.feed(message=self.message(303, text='/start'))
        self.assertEqual(
            [btn.text for btn in self.session.for_method('sendMessage')[-2].reply_markup.keyboard[1]],
            [MENU_INFO],
        )

        # Admin re-opens Help from the help section card
        await self.click(99, 'adm:section_help:1')
        self.assertEqual((await self.db.setting('help_visible'))['value'], '1')
        reopened_card = self.session.for_method('sendMessage')[-1]
        self.assertIn('открыт', reopened_card.text)
        self.assertIn('adm:section_help:0', str(reopened_card.reply_markup))

        # User interaction refreshes menu with Help restored and Help screen works again
        await self.feed(message=self.message(202, text=MENU_HELP_SIMPLE))
        menu_restored, help_page = self.session.for_method('sendMessage')[-2:]
        self.assertEqual(
            [btn.text for btn in menu_restored.reply_markup.keyboard[1]],
            [MENU_HELP_SIMPLE, MENU_INFO],
        )
        self.assertIn('Частые вопросы', help_page.text)
        await self.feed(message=self.message(202, text='/terms'))
        self.assertIn('help_menu', str(self.session.for_method('sendMessage')[-1].reply_markup))

    async def test_one_tap_discount_and_upgrade_paths_across_funnel(self):
        from aurora_bot.alerts import dispatch_abandoned_cart_reminders, dispatch_tariff_view_reminders
        from aurora_bot.pushes import prepare_promo_push

        self.start_dispatcher()
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100555' WHERE id=5")
        await self.db.register(202, 'buyer', 'Buyer', via_start=True)
        await self.db.execute(
            "INSERT INTO promos(code, discount, discount_percent, max_uses, enabled, owner_user_id, is_personal) "
            "VALUES('PASS_TEST20', 0, 20, 1, 1, 202, 1)"
        )
        await self.db.execute(
            "UPDATE users SET promo_push_code='PASS_TEST20', last_viewed_tariff_id=1, last_viewed_at=? WHERE user_id=202",
            (in_seconds(-10 * 60),),
        )

        # 1) View reminder + promo push offer 1-tap discounted tariff jump with campaign tracking
        user_202 = await self.shop.profile(202)
        _, push_kb, _, _ = await prepare_promo_push(self.db, user_202)
        push_cbs = [b.callback_data for row in push_kb.inline_keyboard for b in row]
        self.assertIn('claim_promo:PASS_TEST20', push_cbs)
        self.assertIn('claim_tariff:1:PASS_TEST20', push_cbs)

        await dispatch_tariff_view_reminders(self.bot, self.db, self.shop)
        view_msg = self.session.for_method('sendMessage')[-1]
        self.assertIn('claim_tariff:1:PASS_TEST20', str(view_msg.reply_markup))

        # 2) Catalog, tariff card, and checkout surface 1-tap promo recovery while unapplied
        await self.click(202, 'catalog')
        self.assertIn('claim_promo:PASS_TEST20', str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.click(202, 'tariff:1')
        self.assertIn('claim_tariff:1:PASS_TEST20', str(self.session.for_method('sendMessage')[-1].reply_markup))
        await self.click(202, 'pay_options:1')
        checkout_markup = str(self.session.for_method('sendMessage')[-1].reply_markup)
        self.assertIn('claim_pay:1:PASS_TEST20', checkout_markup)

        # 3) Abandoned cart reminder prioritizes 1-tap promo checkout without competing full-price CTA
        await self.db.execute("UPDATE orders SET created_at=? WHERE user_id=202", (in_seconds(-15 * 60),))
        await dispatch_abandoned_cart_reminders(self.bot, self.db, self.shop)
        cart_markup = self.session.for_method('sendMessage')[-1].reply_markup
        cart_cbs = [b.callback_data for row in cart_markup.inline_keyboard for b in row if b.callback_data]
        self.assertFalse(any(':pay_options:1' in c for c in cart_cbs))
        tracked_claim_pay = next(c for c in cart_cbs if ':claim_pay:1:PASS_TEST20' in c)

        # Abandoned cart WITHOUT quick promo includes direct checkout CTA (:pay_options:1)
        await self.db.register(404, 'plain', 'Plain', via_start=True)
        await self.shop.create_order(404, 1, 'stars')
        await self.db.execute("UPDATE orders SET created_at=? WHERE user_id=404", (in_seconds(-15 * 60),))
        await dispatch_abandoned_cart_reminders(self.bot, self.db, self.shop)
        plain_cart_cbs = [
            b.callback_data
            for row in self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard
            for b in row if b.callback_data
        ]
        self.assertTrue(any(':pay_options:1' in c for c in plain_cart_cbs))

        # 4) Tapping tracked claim_pay activates promo and immediately renders discounted checkout (100 Stars / 192 RUB)
        await self.click(202, tracked_claim_pay)
        discounted_checkout = self.session.for_method('sendMessage')[-1]
        self.assertIn('PASS_TEST20', discounted_checkout.text)
        self.assertIn('100 Stars', discounted_checkout.text)
        self.assertEqual((await self.shop.profile(202))['active_promo'], 'PASS_TEST20')
        latest_order = await self.db.read(
            "SELECT amount_rub, amount_stars, promo_code FROM orders WHERE user_id=202 ORDER BY rowid DESC LIMIT 1"
        )
        self.assertEqual((latest_order['amount_rub'], latest_order['amount_stars'], latest_order['promo_code']),
                         (192, 100, 'PASS_TEST20'))

        # 4b) Pending check_payment returns retry + invoice link buttons instead of a dead end
        pending_ext = await self.shop.create_order(404, 1, 'cryptopay')
        await self.db.execute(
            "UPDATE orders SET payment_url='https://t.me/CryptoBot?start=inv-pending' WHERE id=?",
            (pending_ext['id'],),
        )
        with patch.object(self.payments, 'reconcile', AsyncMock(return_value=None)):
            await self.click(404, f"check:{pending_ext['id']}")
        pending_msg = self.session.for_method('sendMessage')[-1]
        pending_btns = [b for row in pending_msg.reply_markup.inline_keyboard for b in row]
        self.assertIn(f"check:{pending_ext['id']}", [b.callback_data for b in pending_btns])
        self.assertIn('https://t.me/CryptoBot?start=inv-pending', [b.url for b in pending_btns])

        # 5) After buying #1, opening catalog surfaces instant Trade-In upgrade CTA
        paid_order = await self.shop.create_order(202, 1, 'stars')
        await self.shop.settle_stars(paid_order['id'], 202, paid_order['amount_stars'], 'chg-conv-1')
        await self.click(202, 'catalog')
        cat_after = str(self.session.for_method('sendMessage')[-1].reply_markup)
        self.assertIn('up_pay_options:5', cat_after)

        # 6) Second user claiming straight on tariff card via claim_tariff refreshes card with reduced price
        await self.db.register(303, 'viewer', 'Viewer', via_start=True)
        await self.db.execute("UPDATE promos SET enabled=1 WHERE code='LUXURYNEW'")
        await self.click(303, 'claim_tariff:1:LUXURYNEW')
        card_after_claim = self.session.for_method('sendMessage')[-1]
        self.assertIn('LUXURYNEW', card_after_claim.text)
        self.assertIn('90 ₽', str(card_after_claim.reply_markup))

        # 7) Personal promo expiry reminder fires once inside the final 60 minutes under promo_exp campaign
        from aurora_bot.pushes import dispatch_promo_expiry_reminders

        await self.db.set_setting('promo_push_enabled', '1')
        await self.db.register(505, 'urgent', 'Urgent', via_start=True)
        await self.db.execute(
            "INSERT INTO promos(code, discount, discount_percent, max_uses, enabled, owner_user_id, is_personal, expires_at) "
            "VALUES('EXP505', 0, 20, 1, 1, 505, 1, ?)",
            (in_seconds(30 * 60),),
        )
        await self.db.execute(
            "UPDATE users SET promo_push_sent=1, promo_push_code='EXP505', promo_push_sent_at=?, "
            "last_viewed_tariff_id=1 WHERE user_id=505",
            (in_seconds(-2 * 3600),),
        )
        self.assertEqual(await dispatch_promo_expiry_reminders(self.bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_promo_expiry_reminders(self.bot, self.db, self.shop), 0)
        exp_msg = self.session.for_method('sendMessage')[-1]
        self.assertIn('EXP505', exp_msg.text)
        exp_cbs = [b.callback_data or '' for row in exp_msg.reply_markup.inline_keyboard for b in row]
        self.assertTrue(any('claim_pay:1:EXP505' in c for c in exp_cbs))
        self.assertTrue(any('claim_tariff:1:EXP505' in c for c in exp_cbs))
        exp_touch = await self.db.read("SELECT campaign, sent_at FROM campaign_touches WHERE user_id=505")
        self.assertEqual(exp_touch['campaign'], 'promo_exp')
        self.assertIsNotNone(exp_touch['sent_at'])

        # 7b) Cold /start lead who opened zero cards gets 5m express nudge under cold_nudge campaign
        from aurora_bot.alerts import dispatch_cold_start_nudges

        await self.db.register(606, 'silent', 'SilentLead', via_start=True)
        await self.db.execute("UPDATE users SET first_start_at=? WHERE user_id=606", (in_seconds(-10 * 60),))
        self.assertEqual(await dispatch_cold_start_nudges(self.bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_cold_start_nudges(self.bot, self.db, self.shop), 0)
        nudge_cbs = [
            b.callback_data or ''
            for row in self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard
            for b in row
        ]
        self.assertTrue(any(':pay_options:1' in c or ':claim_pay:1:' in c for c in nudge_cbs))
        nudge_touch = await self.db.read("SELECT campaign, sent_at FROM campaign_touches WHERE user_id=606")
        self.assertEqual(nudge_touch['campaign'], 'cold_nudge')
        self.assertIsNotNone(nudge_touch['sent_at'])

        # 7c) 24h second-wave winback fires once after initial personal promo expires, minting deeper LAST_ discount
        from aurora_bot.pushes import dispatch_winback_pushes

        await self.db.set_setting('promo_push_discount', '25%')
        await self.db.register(707, 'cold24', 'Cold24', via_start=True)
        await self.db.execute(
            "INSERT INTO promos(code, discount, discount_percent, max_uses, enabled, owner_user_id, is_personal, expires_at) "
            "VALUES('OLD707', 0, 25, 1, 1, 707, 1, ?)",
            (in_seconds(-20 * 3600),),
        )
        await self.db.execute(
            "UPDATE users SET promo_push_sent=1, promo_push_code='OLD707', promo_push_sent_at=?, "
            "last_viewed_tariff_id=5 WHERE user_id=707",
            (in_seconds(-26 * 3600),),
        )
        self.assertEqual(await dispatch_winback_pushes(self.bot, self.db, self.shop), 1)
        self.assertEqual(await dispatch_winback_pushes(self.bot, self.db, self.shop), 0)
        wb_msg = self.session.for_method('sendMessage')[-1]
        self.assertIn('40%', wb_msg.text)
        wb_user = await self.shop.profile(707)
        wb_code = wb_user['promo_push_code']
        self.assertTrue(wb_code.startswith('LAST_'))
        wb_cbs = [b.callback_data or '' for row in wb_msg.reply_markup.inline_keyboard for b in row]
        tracked_wb_entry = next(c for c in wb_cbs if f':claim_pay:1:{wb_code}' in c)
        self.assertTrue(any(f':claim_pay:5:{wb_code}' in c for c in wb_cbs))
        wb_touch = await self.db.read("SELECT campaign, sent_at FROM campaign_touches WHERE user_id=707")
        self.assertEqual(wb_touch['campaign'], 'winback')
        self.assertIsNotNone(wb_touch['sent_at'])

        # Clicking the winback 1-tap button immediately opens 40%-discounted checkout (144 RUB / 75 Stars)
        await self.click(707, tracked_wb_entry)
        wb_order = await self.db.read(
            "SELECT amount_rub, amount_stars, promo_code FROM orders WHERE user_id=707 ORDER BY rowid DESC LIMIT 1"
        )
        self.assertEqual((wb_order['amount_rub'], wb_order['amount_stars'], wb_order['promo_code']),
                         (144, 75, wb_code))

        # 8) Deactivated tariff on claim_tariff reports unavailable without raising double callback answer
        await self.db.execute("UPDATE tariffs SET active=0 WHERE id=1")
        ans_before = len(self.session.for_method('answerCallbackQuery'))
        await self.click(303, 'claim_tariff:1:LUXURYNEW')
        self.assertEqual(len(self.session.for_method('answerCallbackQuery')), ans_before + 1)
        self.assertIn('временно недоступен', self.session.for_method('sendMessage')[-1].text)

    async def test_project_rename_refreshes_existing_menu_and_brand_templates(self):
        self.start_dispatcher()
        await self.db.set_setting('project_name', 'Old & Club')
        await self.db.set_setting('catalog', '<a href="https://t.me/LuxuryHub">Old &amp; Club</a> · {name}')
        await self.db.set_setting('join_welcome', '<b>Old &amp; Club</b> · {name}')
        await self.db.set_setting('promo_push_text', 'LuxuryHub · код {code}')
        await self.db.set_setting('promo_push_mode', 'personal')
        await self.db.set_setting('reviews_visible', '1')
        await self.db.execute("""INSERT INTO reviews(media_id,media_type,caption,created_at)
            VALUES('feedback','photo','Old &amp; Club · @LuxuryHub',?)""", (in_seconds(-1),))
        old_push_settings = await push_settings(self.db)
        await self.feed(message=self.message(202, text='/start'))
        self.assertEqual(self.session.for_method('sendMessage')[-2].reply_markup.keyboard[2][0].text,
                         f'{MENU_STATS_PREFIX} Old & Club')

        await self.click(99, 'adm:setting:project_name')
        await self.feed(message=self.message(99, text='New <Club>'))
        self.assertEqual(await self.db.project_name(), 'New <Club>')
        await self.feed(message=self.message(202, text=f'{MENU_STATS_PREFIX} Old & Club'))
        keyboard, stats = self.session.for_method('sendMessage')[-2:]
        self.assertEqual(keyboard.reply_markup.keyboard[2][0].text, f'{MENU_STATS_PREFIX} New <Club>')
        self.assertIn('New &lt;Club&gt;', stats.text)

        count = len(self.session.for_method('sendMessage'))
        await self.feed(message=self.message(202, text=MENU_PROFILE))
        self.assertEqual(len(self.session.for_method('sendMessage')), count + 1)
        self.assertIn('New &lt;Club&gt;', self.session.for_method('sendMessage')[-1].text)
        await self.feed(message=self.message(202, text='/terms'))
        self.assertIn('New &lt;Club&gt;', self.session.for_method('sendMessage')[-1].text)
        await self.click(202, 'catalog')
        self.assertIn('<a href="https://t.me/LuxuryHub">New &lt;Club&gt;</a>', self.session.for_method('sendMessage')[-1].text)

        user = await self.shop.profile(202)
        self.assertEqual(await send_promo_push_to_user(self.bot, self.db, user, old_push_settings), 'sent')
        self.assertIn('New &lt;Club&gt;', self.session.for_method('sendMessage')[-1].text)
        review = await self.db.read("SELECT * FROM reviews WHERE media_id='feedback'")
        self.assertEqual(await send_review_push_to_user(
            self.bot, self.db, self.shop, user, old_push_settings, review=review,
        ), 'sent')
        self.assertEqual(self.session.for_method('sendPhoto')[-1].caption, 'New &lt;Club&gt; · @LuxuryHub')

        await self.click(99, 'adm:setting:project_name')
        await self.feed(message=self.message(99, text='Final Club'))
        await self.click(202, 'catalog')
        keyboard, catalog = self.session.for_method('sendMessage')[-2:]
        self.assertEqual(keyboard.reply_markup.keyboard[2][0].text, f'{MENU_STATS_PREFIX} Final Club')
        self.assertIn('<a href="https://t.me/LuxuryHub">Final Club</a>', catalog.text)

    async def test_dispatcher_stars_and_access_lifecycle(self):
        self.start_dispatcher()
        try:
            await self.membership(-100900, administrator=True)
            known = await self.db.read('SELECT * FROM welcome_channels WHERE chat_id=-100900')
            self.assertEqual((known['is_admin'], known['can_invite'], known['enabled']), (1, 1, 0))
            await self.join(202, -100900)
            self.assertEqual(self.session.for_method('sendPhoto'), [])

            await self.click(99, 'adm:greetswitch:main:1')
            panel = self.session.for_method('sendMessage')[-1].reply_markup
            self.assertEqual(panel.inline_keyboard[0][0].style, 'danger')
            await self.click(99, 'adm:greetset:-100900:enabled:1')
            await self.join(205, -1001)
            self.assertEqual(self.session.for_method('sendPhoto')[-1].chat_id, 205)
            self.assertEqual(self.session.for_method('declineChatJoinRequest')[-1].user_id, 205)
            await self.join(202, -100900)
            self.assertEqual(len(self.session.for_method('sendPhoto')), 2)
            photo = self.session.for_method('sendPhoto')[-1]
            start = photo.reply_markup.inline_keyboard[0][0].url.split('start=')[1]
            self.assertRegex(start, r'^g_[a-f0-9]{32}$')
            self.assertEqual(photo.reply_markup.inline_keyboard[0][0].text, '🔞 Открыть каталог фуллов')
            self.assertEqual(photo.reply_markup.inline_keyboard[0][0].style, 'success')
            self.assertIn('собраны все фуллы', photo.caption)
            self.assertIn('Приветствую, Tester!', photo.caption)
            self.assertNotIn('Доступные тарифы', photo.caption)
            self.assertEqual(self.session.for_method('approveChatJoinRequest'), [])
            await self.join(202, -100900)
            self.assertEqual(len(self.session.for_method('sendPhoto')), 2)

            await self.click(99, 'adm:greetset:-100900:auto_approve:1')
            await self.join(210, -100900)
            self.assertEqual(self.session.for_method('approveChatJoinRequest')[-1].user_id, 210)
            await self.click(99, 'adm:greetset:-100900:enabled:0')
            await self.click(99, 'adm:greetswitch:auto:1')
            await self.join(211, -100900)
            self.assertEqual(len(self.session.for_method('sendPhoto')), 3)
            await self.join(212, -100901)
            self.assertEqual(len(self.session.for_method('sendPhoto')), 4)
            self.assertEqual(self.session.for_method('approveChatJoinRequest')[-1].user_id, 210)
            await self.membership(-100901, administrator=False)
            await self.join(213, -100901)
            self.assertEqual(len(self.session.for_method('sendPhoto')), 4)
            await self.click(99, 'adm:greetswitch:main:0')
            await self.join(214, -100902)
            self.assertEqual(len(self.session.for_method('sendPhoto')), 4)

            await self.feed(message=self.message(99, chat={'id': -300, 'type': 'group'}, text='/admin'))
            self.assertFalse(any(call.chat_id == -300 for call in self.session.for_method('sendMessage')))
            await self.db.set_setting('sbp_details', 'legacy details')
            self.assertIsNone(await self.db.read('SELECT * FROM users WHERE user_id=202'))
            await self.feed(message=self.message(202, text=f'/start {start}'))
            self.assertIsNotNone(await self.db.read('SELECT * FROM users WHERE user_id=202'))
            sent_after_start = self.session.for_method('sendMessage')
            welcome_reply = sent_after_start[-2]
            self.assertEqual(welcome_reply.reply_markup.keyboard[0][0].text, '🔞 Каталог фуллов')
            auto_catalog_btns = [row[0] for row in sent_after_start[-1].reply_markup.inline_keyboard]
            self.assertIn('tariff:1', [b.callback_data for b in auto_catalog_btns])
            self.assertFalse(any('₽' in b.text for b in auto_catalog_btns))
            await self.feed(message=self.message(202, text='🔞 Каталог фуллов'))
            catalog = self.session.for_method('sendMessage')[-1].reply_markup
            buttons = [row[0] for row in catalog.inline_keyboard]
            self.assertIn('tariff:1', [b.callback_data for b in buttons])
            self.assertEqual([button.style for button in buttons], [None, None, None, None, 'primary', None, None])
            frozen_msk = datetime(2026, 11, 11, 15, 0, tzinfo=timezone(timedelta(hours=3)))
            def _parse_buyers(card_text: str) -> int:
                m = re.search(r'Уже приобрели: <b>([\d\s\u00a0]+)</b>', card_text)
                self.assertIsNotNone(m, card_text)
                return int(m.group(1).replace('\u00a0', '').replace(' ', ''))

            with patch('aurora_bot.live_stats._now_msk', return_value=frozen_msk):
                await self.click(202, 'tariff:1')
            old_card = self.session.for_method('sendMessage')[-1].text
            self.assertIn('<b>30 дн.</b>', old_card)
            buyers_1_before = _parse_buyers(old_card)
            self.assertGreater(buyers_1_before, 0)
            await self.click(202, 'pay_options:1')
            await self.click(202, 'pay_options:1')
            self.assertEqual(len(self.session.for_method('sendInvoice')), 0)
            self.assertEqual(len(self.session.for_method('createInvoiceLink')), 1)
            invoice = self.session.for_method('createInvoiceLink')[-1]
            checkout_msg = self.session.for_method('sendMessage')[-1]
            checkout = checkout_msg.reply_markup
            self.assertNotIn('Условия покупки', checkout_msg.text)
            self.assertNotIn('Условия покупки', str(checkout))
            self.assertNotIn('terms:', str(checkout))
            self.assertNotIn('pay:manual', str(checkout))
            self.assertEqual(checkout.inline_keyboard[0][0].url, f'https://t.me/$invoice-{invoice.payload}')
            self.assertEqual(checkout.inline_keyboard[0][0].style, 'success')
            await self.click(202, 'ext_pay:2328:1')
            self.assertTrue(self.session.for_method('answerCallbackQuery')[-1].show_alert)
            self.assertIsNone(await self.db.read("SELECT 1 FROM orders WHERE provider='2328'"))
            amount = invoice.prices[0].amount
            await self.feed(pre_checkout_query={'id': 'query', 'from': self.user(202), 'currency': 'XTR',
                                               'total_amount': amount, 'invoice_payload': invoice.payload})
            self.assertTrue(self.session.for_method('answerPreCheckoutQuery')[-1].ok)

            # The successful-payment update is deliberately missing. Restart using the same database.
            await self.db.init()
            self.shop = Shop(self.db)
            self.access = AccessManager(self.db, self.shop)
            self.dp['shop'], self.dp['access'] = self.shop, self.access
            payer = {'type': 'user', 'transaction_type': 'invoice_payment',
                     'user': self.user(202), 'invoice_payload': invoice.payload}
            self.session.ledger = StarTransactions.model_validate({'transactions': [
                {'id': 'charge', 'amount': amount, 'date': 1794220000, 'source': payer}
            ]})
            await reconcile_stars(self.bot, self.db, self.shop)
            await reconcile_stars(self.bot, self.db, self.shop)
            await maintain_access(self.bot, self.db, self.access)
            self.assertEqual(len(await self.shop.purchases(202)), 1)
            self.assertEqual(await self.shop.tariff_buyers_count(1), 1)
            with patch('aurora_bot.live_stats._now_msk', return_value=frozen_msk):
                await self.click(202, 'tariff:1')
            self.assertEqual(_parse_buyers(self.session.for_method('sendMessage')[-1].text), buyers_1_before + 1)
            self.assertEqual(await funnel_summary(self.db, 7, -100900), {
                'requested': 3, 'delivered': 2, 'engaged': 1, 'bought': 1, 'engaged_bought': 1,
            })
            self.assertEqual(len(self.session.for_method('createChatInviteLink')), 4)
            grant = await self.db.read('SELECT * FROM grants WHERE chat_id=-1001')
            await self.join(203, -1001, grant['invite_url'])
            await self.join(202, -1001, grant['invite_url'])
            self.assertEqual(self.session.for_method('declineChatJoinRequest')[-1].user_id, 203)
            self.assertEqual(self.session.for_method('approveChatJoinRequest')[-1].user_id, 202)

            await self.click(99, f'adm:refundok:{invoice.payload}')
            self.assertEqual(len(self.session.for_method('refundStarPayment')), 1)
            await self.click(99, f'adm:refundok:{invoice.payload}')
            self.assertEqual(len(self.session.for_method('refundStarPayment')), 1)
            await maintain_access(self.bot, self.db, self.access)
            self.assertEqual((await self.shop.order(invoice.payload))['status'], 'refunded')
            self.assertEqual(await self.shop.tariff_buyers_count(1), 0)
            with patch('aurora_bot.live_stats._now_msk', return_value=frozen_msk):
                await self.click(202, 'tariff:1')
            self.assertEqual(_parse_buyers(self.session.for_method('sendMessage')[-1].text), buyers_1_before)
            self.assertEqual(len(self.session.for_method('banChatMember')), 4)
            self.assertEqual((await funnel_summary(self.db, 7, -100900))['bought'], 0)

            # Create and publish a link-only product through the real admin wizard.
            await self.click(99, 'adm:newt')
            invite = 'https://t.me/+TestPrivateInvite'
            for value in ('Private link', 'Access by Telegram invitation', '250', '500', '0'):
                await self.feed(message=self.message(99, text=value))
            await self.feed(message=self.message(99, text='https://example.com/not-telegram'))
            self.assertIsNone(await self.db.read("SELECT * FROM tariffs WHERE title='Private link'"))
            await self.feed(message=self.message(99, text=invite))
            item = await self.db.read("SELECT * FROM tariffs WHERE title='Private link'")
            self.assertEqual(item['active'], 0)
            tid = item['id']
            await self.click(99, f'adm:tset:{tid}:1')
            self.assertEqual((await self.shop.get_tariff(tid))['active'], 0)
            self.assertIn('Загрузите фото', self.session.for_method('sendMessage')[-1].text)
            await self.click(99, f'adm:tf:{tid}:preview_description')
            await self.feed(message=self.message(99, text='Inside <the channel>'))
            await self.click(99, f'adm:tm:{tid}')
            await self.feed(message=self.message(99, photo=[
                {'file_id': 'product-photo', 'file_unique_id': 'product-unique', 'width': 1280, 'height': 720},
            ]))
            await self.click(99, f'adm:tpreview:{tid}:details')
            admin_preview = self.session.for_method('sendPhoto')[-1]
            self.assertEqual(admin_preview.photo, 'product-photo')
            self.assertIn('<blockquote>Inside &lt;the channel&gt;</blockquote>', admin_preview.caption)

            await self.click(99, f'adm:t:{tid}')
            inside_button = next(
                button for row in self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard
                for button in row if button.text == '👀 Что внутри?'
            )
            await self.click(99, inside_button.callback_data)
            inside = self.session.for_method('sendMessage')[-1]
            upload_button = inside.reply_markup.inline_keyboard[0][0]
            self.assertEqual(upload_button.text, '➕ Добавить медиа')
            await self.click(99, upload_button.callback_data)
            await self.feed(message=self.message(99, photo=[
                {'file_id': 'inside-photo', 'file_unique_id': 'inside-unique', 'width': 1280, 'height': 720},
            ]))
            inside = self.session.for_method('sendMessage')[-1]
            self.assertEqual(inside.reply_markup.inline_keyboard[0][0].text, '🎬 Заменить медиа')
            preview_button = inside.reply_markup.inline_keyboard[1][1]
            await self.click(99, preview_button.callback_data)
            self.assertEqual(self.session.for_method('sendPhoto')[-1].photo, 'inside-photo')

            await self.click(99, f'adm:tset:{tid}:1')
            await self.click(202, f'adm:tset:{tid}:0')
            self.assertEqual((await self.shop.get_tariff(tid))['active'], 1)
            await self.click(99, f'adm:tm:{tid}')
            await self.feed(message=self.message(99, text='удалить'))
            self.assertIn('сначала скройте товар', self.session.for_method('sendMessage')[-1].text)

            await self.feed(message=self.message(212, text='/start'))
            with patch('aurora_bot.live_stats._now_msk', return_value=frozen_msk):
                await self.click(212, f'tariff:{tid}')
            card = self.session.for_method('sendPhoto')[-1]
            self.assertEqual(card.photo, 'product-photo')
            self.assertIn('Access by Telegram invitation', card.caption)
            self.assertIn('<b>НАВСЕГДА</b>', card.caption)
            self.assertIn('<b>250 Stars</b>', card.caption)
            link_buyers_before = _parse_buyers(card.caption)
            self.assertGreater(link_buyers_before, 0)
            self.assertEqual(card.reply_markup.inline_keyboard[0][0].style, 'success')
            self.assertNotIn(invite, card.caption)

            await self.click(212, f'preview:{tid}')
            preview = self.session.for_method('sendPhoto')[-1]
            self.assertEqual(preview.photo, 'inside-photo')
            self.assertIn('<blockquote>Inside &lt;the channel&gt;</blockquote>', preview.caption)
            self.assertIn('<b>250 Stars</b>', preview.caption)
            self.assertNotIn(invite, preview.caption)

            await self.click(212, f'pay_options:{tid}')
            self.assertEqual(len(self.session.for_method('sendInvoice')), 0)
            invoice = self.session.for_method('createInvoiceLink')[-1]
            amount = invoice.prices[0].amount
            self.assertNotIn(invite, str(invoice))
            await self.feed(pre_checkout_query={'id': 'link-query', 'from': self.user(212), 'currency': 'XTR',
                                               'total_amount': amount, 'invoice_payload': invoice.payload})
            self.assertTrue(self.session.for_method('answerPreCheckoutQuery')[-1].ok)
            await self.feed(message=self.message(212, successful_payment={
                'currency': 'XTR', 'total_amount': amount, 'invoice_payload': invoice.payload,
                'telegram_payment_charge_id': 'link-charge', 'provider_payment_charge_id': '',
            }, date=int(time())))
            await maintain_access(self.bot, self.db, self.access)
            delivery = self.session.for_method('sendMessage')[-1]
            self.assertEqual(delivery.chat_id, 212)
            self.assertEqual(delivery.reply_markup.inline_keyboard[0][0].url, invite)
            self.assertEqual(len(self.session.for_method('createChatInviteLink')), 4)
            with patch('aurora_bot.live_stats._now_msk', return_value=frozen_msk):
                await self.click(212, f'tariff:{tid}')
            self.assertEqual(_parse_buyers(self.session.for_method('sendPhoto')[-1].caption), link_buyers_before + 1)
            purchase = (await self.shop.purchases(212))[0]
            replacement = 'https://t.me/+NewPrivateInvite'
            await self.click(99, f"adm:fixlinks:{purchase['id']}")
            await self.feed(message=self.message(99, text=replacement))
            await maintain_access(self.bot, self.db, self.access)
            self.assertEqual(self.session.for_method('sendMessage')[-1].reply_markup.inline_keyboard[0][0].url,
                             replacement)
            self.assertEqual(await funnel_summary(self.db, 7, -100901), {
                'requested': 2, 'delivered': 1, 'engaged': 1, 'bought': 1, 'engaged_bought': 1,
            })
            sales = await product_sales(self.db, 7, 10, 0)
            self.assertEqual([(row['id'], row['sales'], row['stars']) for row in sales], [(tid, 1, 250)])
            await self.click(99, 'adm:funnel:7:-100901')
            self.assertIn('100,0%', self.session.for_method('sendMessage')[-1].text)

            await self.click(99, 'adm:lookuporder')
            await self.feed(message=self.message(99, text=invoice.payload[:8]))
            self.assertIn(invoice.payload, self.session.for_method('sendMessage')[-1].text)
            admin_panel = self.message(99, text='Admin panel', **{
                'from': {**self.user(123), 'is_bot': True},
                'reply_markup': {'inline_keyboard': [[{'text': 'Orders', 'callback_data': 'adm:orders'}]]},
            })
            await self.feed(callback_query={'id': 'nav', 'from': self.user(99), 'chat_instance': 'test',
                                            'message': admin_panel, 'data': 'adm:orders:paid:0'})
            self.assertIn('Оплачены', self.session.for_method('editMessageText')[-1].text)
            purchase = (await self.shop.purchases(212))[0]
            await self.click(99, f"adm:revoke:{purchase['id']}")
            self.assertIsNone((await self.shop.purchases(212))[0]['expires_at'])
            await self.click(99, f"adm:revokeok:{purchase['id']}")
            await self.click(212, 'accesses')
            self.assertNotIn(replacement, str(self.session.for_method('sendMessage')[-1].reply_markup))
        finally:
            await self.dp.fsm.close()

    async def test_2328_retries_when_merchant_lookup_fails(self):
        await self.db.register(202)
        order = await self.shop.create_order(202, 1, '2328')
        await self.shop.set_external(order['id'], '2328', 'invoice-id', 'https://shop.example/pay')
        event = {'order_id': order['id'], 'uuid': 'invoice-id', 'amount': str(order['amount_rub']),
                 'currency': 'RUB', 'payment_status': 'paid'}
        event['sign'] = self.payments.gateway.signature(event, self.config.key_2328)
        self.payments.gateway.info = AsyncMock(side_effect=TimeoutError)
        async with TestClient(TestServer(make_app(self.config, self.shop, self.payments))) as client:
            invalid = await client.post('/webhooks/2328', json={**event, 'sign': chr(233) * 64})
            self.assertEqual(invalid.status, 401)
            failed = await client.post('/webhooks/2328', json=event)
            self.assertEqual(failed.status, 503)
            self.assertEqual((await self.shop.order(order['id']))['status'], 'pending')
            self.payments.gateway.info = AsyncMock(return_value=event)
            for _ in range(2):
                response = await client.post('/webhooks/2328', json=event)
                self.assertEqual(response.status, 200)
            refund = {**event, 'payment_status': 'refund_paid'}
            refund.pop('sign')
            refund['sign'] = self.payments.gateway.signature(refund, self.config.key_2328)
            response = await client.post('/webhooks/2328', json=refund)
            self.assertEqual(response.status, 503)
            self.assertEqual((await self.shop.order(order['id']))['status'], 'paid')
            self.assertTrue((await self.shop.order(order['id']))['refund_requested_at'])
            self.payments.gateway.info = AsyncMock(return_value=refund)
            for payload in (refund, refund, event):
                response = await client.post('/webhooks/2328', json=payload)
                self.assertEqual(response.status, 200)
            self.assertEqual((await self.shop.order(order['id']))['status'], 'refunded')
        self.assertEqual(len(await self.shop.purchases(202)), 1)
        self.assertLessEqual((await self.shop.purchases(202))[0]['expires_at'], in_seconds(0))

    async def test_2328_polling_reconciles_refund_without_webhook(self):
        await self.db.register(202)
        order = await self.shop.create_order(202, 1, '2328')
        await self.shop.set_external(order['id'], '2328', 'invoice-id', 'https://shop.example/pay')
        await self.shop.settle(order['id'], '2328', external_id='invoice-id')
        self.payments.gateway.info = AsyncMock(return_value={'payment_status': 'refund_paid'})
        await reconcile_external(self.config, self.db, self.payments)
        self.assertEqual((await self.shop.order(order['id']))['status'], 'refunded')

    async def test_explicit_greeting_source_is_not_overwritten_by_plain_click(self):
        await self.db.register(202)
        for event_id, chat_id, age in (('a' * 32, -100900, -80), ('b' * 32, -100901, -10)):
            await self.db.execute("""INSERT INTO greeting_events(id,chat_id,user_id,requested_at,sent_at)
                VALUES(?,?,?,?,?)""", (event_id, chat_id, 202, int(time()) + age, in_seconds(age)))
        await record_engagement(self.db, 202, 'g_' + 'a' * 32)
        await record_engagement(self.db, 202)
        order = await self.shop.create_order(202, 1, 'stars')
        self.assertEqual(order['greeting_event_id'], 'a' * 32)

    async def test_external_polling_moves_past_failed_invoices(self):
        await self.db.register(202)
        ids = []
        for index in range(11):
            order = await self.shop.create_order(202, 1, '2328')
            await self.shop.set_external(order['id'], '2328', str(index + 1), 'https://shop.example/pay')
            await self.db.execute('UPDATE orders SET expires_at=?,created_at=? WHERE id=?',
                                  (in_seconds(-1), in_seconds(-100 + index), order['id']))
            ids.append(order['id'])
        self.payments.reconcile = AsyncMock(return_value=False)
        await reconcile_external(self.config, self.db, self.payments)
        self.assertEqual(self.payments.reconcile.await_count, 10)
        await reconcile_external(self.config, self.db, self.payments)
        self.assertEqual(self.payments.reconcile.await_args.args[0]['id'], ids[-1])
