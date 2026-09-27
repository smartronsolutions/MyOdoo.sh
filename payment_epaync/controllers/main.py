import logging
import pprint

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)


class EpayncController(http.Controller):
    """EpayNC hosted-payment-page controller.

    Odoo 18 removed ``payment.transaction._execute_callback``. Notification data
    is now handled with ``_handle_notification_data`` (which matches the
    transaction and updates its state); the finalization of the transaction
    (creation of the ``account.payment``, invoice posting, ...) is performed by
    ``_post_process``. Browser flows are post-processed by the ``/payment/status``
    page, while the server-to-server IPN triggers ``_post_process`` directly.
    """

    _return_url = '/payment/epaync/return'
    _success_url = '/payment/epaync/success'
    _cancel_url = '/payment/epaync/cancel'
    _refused_url = '/payment/epaync/refused'
    _error_url = '/payment/epaync/error'
    _webhook_url = '/payment/epaync/ipn'

    # ── Browser return redirects from the gateway ─────────────────────────────
    @http.route(
        _return_url,
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def epaync_return(self, **data):
        """Process the data posted back by the gateway, then show the status."""
        _logger.info("EpayNC return | data:\n%s", pprint.pformat(data))
        if data:
            try:
                request.env['payment.transaction'].sudo()._handle_notification_data(
                    'epaync', data
                )
            except ValidationError as e:
                _logger.warning("EpayNC return: could not process notification: %s", e)
        return request.redirect('/payment/status')

    # ── IPN / Webhook (server-to-server) ─────────────────────────────────────
    @http.route(
        _webhook_url,
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def epaync_ipn(self, **data):
        _logger.info("EpayNC IPN | data:\n%s", pprint.pformat(data))

        provider = None
        site_id = data.get('vads_site_id')
        if site_id:
            provider = request.env['payment.provider'].sudo().search([
                ('code', '=', 'epaync'),
                ('epaync_merchant_id', '=', site_id),
            ], limit=1)

        try:
            tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                'epaync', data
            )

            # Verify shop ID
            if tx_sudo.provider_id.epaync_merchant_id and \
                    tx_sudo.provider_id.epaync_merchant_id != data.get('vads_site_id'):
                raise ValidationError("EpayNC IPN: Shop ID mismatch.")

            # Verify amount — vads_amount is in minor currency units
            currency = tx_sudo.currency_id
            decimal_places = currency.decimal_places if currency else 0
            expected_amount = int(round(tx_sudo.amount * (10 ** decimal_places)))
            received_amount = int(data.get('vads_amount', -1))
            if expected_amount != received_amount:
                raise ValidationError(
                    f"EpayNC IPN: Amount mismatch — expected {expected_amount}, got {received_amount}."
                )

            # Verify currency — compare ISO 4217 numeric codes
            from odoo.addons.payment_epaync.models.payment_transaction import ISO4217_NUMERIC
            expected_currency_numeric = ISO4217_NUMERIC.get(currency.name if currency else 'XPF', '953')
            received_currency = data.get('vads_currency', '')
            if received_currency and expected_currency_numeric != received_currency:
                raise ValidationError(
                    f"EpayNC IPN: Currency mismatch — expected {expected_currency_numeric}, got {received_currency}."
                )

            # Update the transaction state and finalize it server-side (no browser
            # will poll the status page for a server-to-server notification).
            tx_sudo._process_notification_data(data)
            tx_sudo._post_process()

        except ValidationError as e:
            _logger.warning("EpayNC IPN validation error: %s", str(e))
            if provider:
                request.env['payment.epaync.log'].sudo()._log(
                    provider=provider,
                    log_type='error',
                    error_message=str(e),
                    webhook_payload=str(data),
                )
            return request.make_response(
                f"ERROR: {e}",
                headers=[('Content-Type', 'text/plain')],
                status=400,
            )
        except Exception as e:
            _logger.exception("EpayNC IPN unexpected error: %s", str(e))
            return request.make_response(
                "ERROR",
                headers=[('Content-Type', 'text/plain')],
                status=500,
            )

        return request.make_response('OK', headers=[('Content-Type', 'text/plain')])

    # ── Status landing pages ──────────────────────────────────────────────────
    @http.route(
        _success_url,
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        website=True,
        save_session=False,
    )
    def epaync_success(self, **data):
        _logger.info("EpayNC success redirect")
        self._process_landing_data(data)
        return request.redirect('/payment/status')

    @http.route(
        _cancel_url,
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        website=True,
        save_session=False,
    )
    def epaync_cancel(self, **data):
        _logger.info("EpayNC cancel redirect")
        self._process_landing_data(data)
        return request.redirect('/payment/status')

    @http.route(
        _refused_url,
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        website=True,
        save_session=False,
    )
    def epaync_refused(self, **data):
        _logger.info("EpayNC refused redirect")
        self._process_landing_data(data)
        return request.redirect('/payment/status')

    @http.route(
        _error_url,
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        website=True,
        save_session=False,
    )
    def epaync_error(self, **data):
        _logger.info("EpayNC error redirect")
        self._process_landing_data(data)
        return request.redirect('/payment/status')

    @staticmethod
    def _process_landing_data(data):
        """Best-effort processing of the data attached to a browser redirect."""
        if not data:
            return
        try:
            request.env['payment.transaction'].sudo()._handle_notification_data(
                'epaync', data
            )
        except Exception:
            _logger.exception("EpayNC: could not process landing notification data")
