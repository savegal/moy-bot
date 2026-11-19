"""Signed callbacks for optional external payment providers."""
from __future__ import annotations

import json
import logging
from aiohttp import web
from .payments import GatewayError, Payments, invoice_identifier
from .service import Shop, ShopError
from .config import Config

log = logging.getLogger(__name__)


def make_app(config: Config, shop: Shop, payments: Payments):
    routes=web.RouteTableDef()

    @routes.get('/health')
    async def health(request):
        return web.json_response({'status':'ok'})

    @routes.post('/webhooks/cryptopay')
    async def crypto_webhook(request):
        crypto = payments.crypto
        if not crypto.token:
            raise web.HTTPNotFound()
        body = await request.read()
        if not crypto.verify_webhook(body, request.headers.get('crypto-pay-api-signature', '')):
            raise web.HTTPUnauthorized()
        try:
            event = json.loads(body)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(event, dict):
            raise web.HTTPBadRequest()
        if event.get('update_type') != 'invoice_paid':
            return web.json_response({'ok': True})
        invoice = event.get('payload')
        if not isinstance(invoice, dict) or invoice.get('status') != 'paid':
            raise web.HTTPBadRequest()
        try:
            invoice_id = invoice_identifier(invoice.get('invoice_id'))
        except GatewayError:
            raise web.HTTPBadRequest() from None
        order_id = invoice.get('payload')
        if not isinstance(order_id, str):
            raise web.HTTPBadRequest()
        order = await shop.order(order_id)
        if (order and order['provider'] == 'cryptopay' and order['status'] in ('pending', 'expired')
                and order['external_id'] in ('', invoice_id) and crypto.matches(order, invoice)):
            try:
                settled = await payments.reconcile(order, webhook_invoice_id=invoice_id, crypto=crypto)
            except ShopError as exc:
                current = await shop.order(order['id'])
                if current and current['status'] == 'failed':
                    log.error('Crypto Pay webhook %s closed: %s', order_id, exc)
                    return web.json_response({'ok': True})
                log.warning('Crypto Pay webhook %s rejected: %s', order_id, exc)
                raise web.HTTPServiceUnavailable() from None
            except Exception:
                log.warning('Crypto Pay webhook %s reconciliation will retry', order_id, exc_info=True)
                raise web.HTTPServiceUnavailable() from None
            if not settled:
                current = await shop.order(order['id'])
                if current and current['status'] in ('paid', 'refunded', 'failed'):
                    return web.json_response({'ok': True})
                raise web.HTTPServiceUnavailable()
        return web.json_response({'ok': True})

    @routes.post('/webhooks/2328')
    async def payment_webhook(request):
        if not config.gateway_configured:
            raise web.HTTPNotFound()
        if request.content_length is not None and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384,actual_size=request.content_length)
        try:
            payload=await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest()
        if not isinstance(payload,dict) or not payments.gateway.verify_webhook(payload):
            raise web.HTTPUnauthorized()
        order=await shop.order(str(payload.get('order_id','')))
        if order and payments.gateway.valid_payment_webhook(order,payload):
            try:
                # Confirm using authenticated merchant query as additional protection.
                if order['status'] != 'refunded':
                    status = payload['payment_status']
                    refund = status.startswith('refund_')
                    if refund or order['status'] in ('pending', 'expired'):
                        try:
                            await payments.reconcile(order, refund_only=refund, expected_status=status,
                                                     webhook_invoice_id=invoice_identifier(payload['uuid']))
                        except ShopError as exc:
                            current = await shop.order(order['id'])
                            if current and current['status'] == 'failed':
                                log.error('Signed webhook %s closed: %s', order['id'], exc)
                                return web.json_response({'ok': True})
                            raise
                        current = await shop.order(order['id'])
                        if status == 'refund_paid':
                            accepted = current['status'] == 'refunded'
                        elif status == 'refund_process':
                            # Reconciliation also accepts a newer authoritative
                            # refund_fail and correctly resumes delivery for it.
                            accepted = True
                        elif status == 'refund_fail':
                            accepted = (
                                current['status'] == 'refunded'
                                or not current['refund_requested_at']
                            )
                        else:
                            accepted = current['status'] in ('paid', 'refunded')
                        if not accepted:
                            raise web.HTTPServiceUnavailable()
            except web.HTTPException:
                raise
            except Exception:
                log.warning('Signed webhook %s reconciliation will retry', order['id'], exc_info=True)
                raise web.HTTPServiceUnavailable()
        return web.json_response({'ok':True})

    app=web.Application(client_max_size=16384)
    app.add_routes(routes)
    return app


async def start_server(config: Config, shop: Shop, payments: Payments):
    app=make_app(config,shop,payments)
    runner=web.AppRunner(app,access_log=None)
    await runner.setup()
    # For production: reverse proxy Caddy/Nginx on HTTPS to localhost; never bind to all interfaces.
    try:
        site=web.TCPSite(runner,'127.0.0.1',config.http_port)
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
