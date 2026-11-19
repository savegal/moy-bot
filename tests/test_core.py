from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from time import monotonic
import unittest

from aurora_bot.db import Database, in_seconds, now
from aurora_bot.service import Shop, ShopError, price_with_promo, channels_of, parse_date
from aurora_bot.payments import Gateway2328, rub_match
from aurora_bot.config import Config


class PriceTests(unittest.TestCase):
    def test_discount(self):
        self.assertEqual(price_with_promo(900,350,300),(600,233))
        self.assertEqual(price_with_promo(900,350,0),(900,350))
        self.assertEqual(price_with_promo(900,350,50000),(1,1))

    def test_channel_parser(self):
        self.assertEqual(channels_of('-1001,-1002\n-1003'),[-1001,-1002,-1003])
        with self.assertRaises(ShopError): channels_of('-1001,-1001')
        with self.assertRaises(ShopError): channels_of('123456')

    def test_signature_validity(self):
        config=Config('123:TOKEN',frozenset({1}),project_2328='test-project',key_2328='SECRET',
                      public_base_url='https://bot.example.com',allow_external_methods=True)
        gateway=Gateway2328(config)
        payload={'uuid':'xyz','order_id':'abc','amount':'600.00','currency':'RUB','payment_status':'paid'}
        payload['sign']=gateway.signature(payload,config.key_2328)
        self.assertTrue(gateway.verify_webhook(payload))
        self.assertTrue(gateway.valid_payment_webhook({'provider':'2328','id':'abc','external_id':'xyz','amount_rub':600},payload))
        self.assertFalse(gateway.valid_payment_webhook({'provider':'2328','id':'abc','external_id':'xyz','amount_rub':601},payload))
        payload['amount']='601.00'
        self.assertFalse(gateway.verify_webhook(payload))
        self.assertFalse(gateway.verify_webhook({**payload, 'sign': 'é' * 64}))

    def test_decimal_matches(self):
        self.assertTrue(rub_match('600.00',600))
        self.assertFalse(rub_match('600.01',600))
        self.assertFalse(rub_match('garbage',600))

    def test_config_external_default_off(self):
        config=Config('x:y',frozenset({1}),cryptopay_token='token')
        self.assertFalse(config.crypto_enabled)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'data.sqlite3')
        self.db=Database(self.path)
        await self.db.init()
        self.shop=Shop(self.db)
        await self.db.register(101,'alice','Alice')
        await self.db.register(202,'bob','Bob')
        await self.db.execute("UPDATE tariffs SET channel_ids='-100111,-100222' WHERE id=1")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_seeded_data_no_false_sales(self):
        self.assertEqual(len(await self.shop.catalogs()),7)
        self.assertEqual((await self.shop.statistics())['paid'],0)
        self.assertEqual((await self.shop.statistics())['users'],2)

    async def test_upgrade_preserves_custom_storefront_and_products(self):
        await self.db.set_setting('welcome', '<b>Custom welcome</b>')
        await self.db.set_setting('info', 'About {project_name}')
        await self.db.execute("""UPDATE tariffs SET title='Custom product',description='Custom description',
            rub=777,stars=333,duration_days=30,preview_description='Custom preview' WHERE id=1""")
        before = await self.db.read('SELECT * FROM tariffs ORDER BY id', many=True)
        for version in range(4):
            with self.subTest(version=version):
                await self.db.execute(f'PRAGMA user_version={version}')
                await self.db.init()
                self.assertEqual(await self.db.read('SELECT * FROM tariffs ORDER BY id', many=True), before)
                self.assertEqual((await self.db.setting('welcome'))['value'], '<b>Custom welcome</b>')
                self.assertEqual((await self.db.setting('info'))['value'], 'About {project_name}')

    async def test_hidden_tariff_can_be_granted_but_cannot_be_bought(self):
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id=1')
        with self.assertRaises(ShopError):
            await self.shop.create_order(101, 1, 'stars')
        order = await self.shop.create_order(101, 1, 'admin', duration_days=5)
        self.assertTrue(await self.shop.settle(order['id'], 'admin'))
        purchase = (await self.shop.purchases(101))[0]
        self.assertIsNotNone(purchase['expires_at'])
        self.assertEqual(len(await self.shop.purchase_grants(purchase['id'])), 2)

    async def test_order_snapshot_and_idempotency(self):
        order=await self.shop.create_order(101,1,'stars')
        self.assertEqual(order['amount_stars'],125)
        self.assertEqual(len(order['id']),32)
        self.assertEqual((await self.shop.order(order['id']))['status'],'pending')
        self.assertTrue(await self.shop.settle(order['id'],'stars',charge_id='charge-123'))
        self.assertFalse(await self.shop.settle(order['id'],'stars',charge_id='charge-123'))
        self.assertEqual((await self.shop.statistics())['paid'],1)
        purchases=await self.shop.purchases(101)
        self.assertEqual(len(purchases),1)
        grants=await self.shop.purchase_grants(purchases[0]['id'])
        self.assertEqual({g['chat_id'] for g in grants},{-100111,-100222})

    async def test_charge_unique_across_orders(self):
        a=await self.shop.create_order(101,1,'stars')
        b=await self.shop.create_order(202,1,'stars')
        await self.shop.settle(a['id'],'stars',charge_id='charge-unique')
        with self.assertRaises(ShopError):
            await self.shop.settle(b['id'],'stars',charge_id='charge-unique')
        self.assertEqual((await self.shop.order(b['id']))['status'],'pending')
        self.assertEqual((await self.shop.statistics())['paid'],1)

    async def test_parallel_settlement_is_atomic(self):
        o=await self.shop.create_order(101,1,'stars')
        done=await asyncio.gather(*[self.shop.settle(o['id'],'stars',charge_id='same') for _ in range(5)])
        self.assertEqual(done.count(True),1)
        self.assertEqual(done.count(False),4)
        self.assertEqual(len(await self.shop.purchases(101)),1)

    async def test_manual_payment_requires_review(self):
        o=await self.shop.create_order(101,1,'manual')
        with self.assertRaises(ShopError):
            await self.shop.settle(o['id'],'manual')
        self.assertTrue(await self.shop.manual_review(o['id'],101))
        self.assertFalse(await self.shop.manual_review(o['id'],101))
        self.assertTrue(await self.shop.settle(o['id'],'manual'))

    async def test_external_identity_required(self):
        o=await self.shop.create_order(101,1,'2328')
        with self.assertRaises(ShopError):
            await self.shop.settle(o['id'],'2328',external_id='xyz')
        await self.shop.set_external(o['id'],'2328','invoiceid','https://pay.example.com/checkout')
        with self.assertRaises(ShopError):
            await self.shop.settle(o['id'],'2328',external_id='another')
        self.assertTrue(await self.shop.settle(o['id'],'2328',external_id='invoiceid'))

    async def test_promo_redeemed_once_and_applied_to_stars(self):
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('WELCOME',100,1)")
        await self.shop.activate_promo(101,'welcome')
        o=await self.shop.create_order(101,1,'stars')
        self.assertEqual((o['amount_rub'],o['amount_stars'],o['promo_code']),(140,73,'WELCOME'))
        await self.shop.settle(o['id'],'stars',charge_id='promocharge')
        again=await self.shop.create_order(101,1,'stars')
        self.assertEqual(again['amount_stars'],125)
        with self.assertRaises(ShopError):
            await self.shop.activate_promo(101,'WELCOME')
        self.assertEqual((await self.db.read("SELECT used FROM promos WHERE code='WELCOME'"))['used'],1)

    async def test_no_channels_no_sale(self):
        with self.assertRaises(ShopError):
            await self.shop.create_order(101,2,'stars')

    async def test_promo_capacity_reserved_at_checkout(self):
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('ONE',300,1)")
        for uid in (101, 202):
            await self.shop.activate_promo(uid, 'ONE')
        invoices = [await self.shop.create_order(uid, 1, 'stars') for uid in (101, 202)]
        results = await asyncio.gather(*(self.shop.authorize_stars(
            order['id'], order['user_id'], order['amount_stars'], order['id']
        ) for order in invoices))
        self.assertEqual(results.count(True), 1)
        order = invoices[results.index(True)]
        args = order['id'], order['user_id'], order['amount_stars']
        self.assertTrue(await self.shop.authorize_stars(*args, order['id']))
        self.assertFalse(await self.shop.authorize_stars(*args, 'different-query'))

    async def test_refund_before_receipt_does_not_reopen_access(self):
        order = await self.shop.create_order(101, 1, 'stars')
        self.assertTrue(await self.shop.refund_stars('charge', 125, order_id=order['id'], user_id=101))
        self.assertFalse(await self.shop.settle_stars(order['id'], 101, 125, 'charge'))
        self.assertEqual(await self.shop.purchases(101), [])

    async def test_hidden_and_changed_tariff_rejects_old_invoice_but_honors_accepted_payment(self):
        order = await self.shop.create_order(101, 1, 'stars')
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id=1')
        self.assertFalse(await self.shop.authorize_stars(order['id'], 101, 125, 'hidden'))
        await self.db.execute("UPDATE tariffs SET active=1,channel_ids='-100333' WHERE id=1")
        self.assertFalse(await self.shop.authorize_stars(order['id'], 101, 125, 'changed'))
        fresh = await self.shop.create_order(101, 1, 'stars')
        self.assertNotEqual(fresh['id'], order['id'])
        self.assertTrue(await self.shop.authorize_stars(fresh['id'], 101, 125, 'accepted'))
        await self.db.execute('UPDATE tariffs SET active=0 WHERE id=1')
        self.assertTrue(await self.shop.settle_stars(fresh['id'], 101, 125, 'charge'))

    async def test_repeated_purchase_reuses_order_and_only_claims_one_send(self):
        orders = await asyncio.gather(*(self.shop.create_order(101, 1, 'stars') for _ in range(4)))
        self.assertEqual(len({order['id'] for order in orders}), 1)
        attempts = await asyncio.gather(*(self.shop.claim_invoice(order['id']) for order in orders))
        self.assertEqual(attempts.count(True), 1)

    async def test_expired_invoice_keeps_promo_reserved_until_receipt(self):
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('LAST',200,1)")
        for uid in (101, 202):
            await self.shop.activate_promo(uid, 'LAST')
        first = await self.shop.create_order(101, 1, 'stars')
        self.assertTrue(await self.shop.authorize_stars(first['id'], 101, first['amount_stars'], 'first'))
        await self.db.execute("""UPDATE orders SET status='expired',checkout_until='2000-01-01T00:00:00+00:00'
            WHERE id=?""", (first['id'],))
        second = await self.shop.create_order(202, 1, 'stars')
        self.assertFalse(await self.shop.authorize_stars(second['id'], 202, second['amount_stars'], 'second'))
        self.assertTrue(await self.shop.settle_stars(first['id'], 101, first['amount_stars'], 'late'))
        self.assertEqual((await self.db.read("SELECT used FROM promos WHERE code='LAST'"))['used'], 1)

    async def test_stars_checkout_respects_issued_crypto_promo_until_expiry(self):
        await self.db.execute("INSERT INTO promos(code,discount,max_uses) VALUES('SWITCH',150,1)")
        ext_nopromo = await self.shop.create_order(101, 1, '2328')
        await self.shop.activate_promo(101, 'SWITCH')
        await self.shop.activate_promo(202, 'SWITCH')
        ext_promo = await self.shop.create_order(101, 1, 'cryptopay')
        await self.shop.set_external(ext_promo['id'], 'cryptopay', 'gw-switch', 'https://pay.example/invoice')
        stars_101 = await self.shop.create_order(101, 1, 'stars')
        stars_202 = await self.shop.create_order(202, 1, 'stars')

        self.assertFalse(await self.shop.authorize_stars(stars_202['id'], 202, stars_202['amount_stars'], 'other-blocked'))
        self.assertFalse(await self.shop.authorize_stars(
            stars_101['id'], 101, stars_101['amount_stars'], 'expired-deadline', deadline=monotonic() - 1,
        ))
        self.assertEqual((await self.shop.order(ext_promo['id']))['status'], 'pending')

        self.assertFalse(await self.shop.authorize_stars(stars_101['id'], 101, stars_101['amount_stars'], 'own-blocked'))
        await self.db.execute('UPDATE orders SET expires_at=? WHERE id=?', (in_seconds(-1), ext_promo['id']))
        await self.shop.expire_external_invoice(ext_promo['id'], 'cryptopay', 'gw-switch')
        self.assertEqual((await self.shop.order(ext_promo['id']))['status'], 'expired')
        self.assertTrue(await self.shop.authorize_stars(stars_101['id'], 101, stars_101['amount_stars'], 'stars-ok'))
        self.assertEqual((await self.shop.order(ext_nopromo['id']))['status'], 'pending')
        self.assertFalse(await self.shop.authorize_stars(stars_202['id'], 202, stars_202['amount_stars'], 'still-blocked'))

        self.assertTrue(await self.shop.settle_stars(stars_101['id'], 101, stars_101['amount_stars'], 'charge-switch'))
        self.assertEqual((await self.db.read("SELECT used FROM promos WHERE code='SWITCH'"))['used'], 1)

    async def test_expiry(self):
        await self.db.execute('UPDATE tariffs SET duration_days=2 WHERE id=1')
        o=await self.shop.create_order(101,1,'admin')
        await self.shop.settle(o['id'],'admin')
        p=(await self.shop.purchases(101))[0]
        self.assertIsNotNone(p['expires_at'])
        self.assertGreater(parse_date(p['expires_at']),parse_date(p['started_at']))

    async def test_register_does_not_overwrite_existing_identity(self):
        await self.db.register(101)
        user=await self.shop.profile(101)
        self.assertEqual(user['username'],'alice')
        self.assertEqual(user['name'],'Alice')


if __name__ == '__main__':
    unittest.main()
