import base64
import hashlib
import hmac
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

EPAYNC_PAYMENT_URL = 'https://epaync.nc/vads-payment/'


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('epaync', 'EpayNC')],
        ondelete={'epaync': 'set default'},
    )

    # ── Credentials ──────────────────────────────────────────────────────────
    epaync_merchant_id = fields.Char(
        string="Merchant Shop ID",
        help="Your EpayNC shop ID (vads_site_id), e.g. 14282108",
    )
    epaync_test_key = fields.Char(
        string="Test Key",
        groups='base.group_system',
    )
    epaync_production_key = fields.Char(
        string="Production Key",
        groups='base.group_system',
    )
    epaync_payment_url = fields.Char(
        string="Payment URL",
        default=EPAYNC_PAYMENT_URL,
    )
    epaync_currency = fields.Char(
        string="Default Currency",
        default='All currencies supported',
        readonly=True,
        help="EpayNC accepts all currencies. The ISO 4217 numeric code is sent automatically per transaction.",
    )
    epaync_api_version = fields.Char(
        string="API Version",
        default='V2',
        readonly=True,
    )
    epaync_signature_algorithm = fields.Char(
        string="Signature Algorithm",
        default='HMAC SHA-256',
        readonly=True,
    )

    # ── Configuration ─────────────────────────────────────────────────────────
    epaync_payment_action = fields.Selection([
        ('SINGLE', 'Immediate Capture'),
        ('MULTI', 'Manual Capture (future)'),
    ], string="Payment Action", default='SINGLE')
    epaync_trans_prefix = fields.Char(
        string="Transaction Prefix",
        default='ODOO',
    )
    epaync_trans_length = fields.Integer(
        string="Transaction ID Length",
        default=6,
    )
    epaync_timeout = fields.Integer(
        string="Timeout (seconds)",
        default=10,
    )
    epaync_auto_confirm_so = fields.Boolean(
        string="Auto Confirm Sales Order",
        default=True,
    )
    epaync_auto_validate_invoice = fields.Boolean(
        string="Auto Validate Invoice",
    )
    epaync_auto_register_payment = fields.Boolean(
        string="Auto Register Payment",
    )
    epaync_allow_customer_cancel = fields.Boolean(
        string="Allow Customer Cancel",
        default=True,
    )
    epaync_debug_mode_enabled = fields.Boolean(
        string="Enable Debug Mode",
    )
    epaync_enable_logs = fields.Boolean(
        string="Enable Logs",
        default=True,
    )
    epaync_retry_failed_ipn = fields.Boolean(
        string="Retry Failed IPN",
    )

    # ── Messages ──────────────────────────────────────────────────────────────
    epaync_msg_success = fields.Text(
        string="Payment Successful",
        default="Your payment was processed successfully. Thank you for your order.",
        translate=True,
    )
    epaync_msg_failed = fields.Text(
        string="Payment Failed",
        default="Your payment could not be processed. Please try again.",
        translate=True,
    )
    epaync_msg_cancelled = fields.Text(
        string="Payment Cancelled",
        default="Your payment has been cancelled.",
        translate=True,
    )
    epaync_msg_pending = fields.Text(
        string="Payment Pending",
        default="Your payment is pending verification. We will notify you once confirmed.",
        translate=True,
    )
    epaync_msg_refused = fields.Text(
        string="Payment Refused",
        default="Your payment has been refused. Please contact your bank or try a different card.",
        translate=True,
    )
    epaync_msg_expired = fields.Text(
        string="Payment Expired",
        default="Your payment session has expired. Please try again.",
        translate=True,
    )
    epaync_msg_invalid_signature = fields.Text(
        string="Invalid Signature",
        default="Security verification failed. The payment cannot be processed.",
        translate=True,
    )
    epaync_msg_unknown_error = fields.Text(
        string="Unknown Error",
        default="An unknown error occurred. Please contact support.",
        translate=True,
    )
    epaync_msg_webhook_received = fields.Text(
        string="Webhook Received",
        default="Payment notification received from EpayNC gateway.",
        translate=True,
    )
    epaync_msg_authorized = fields.Text(
        string="Payment Authorized",
        default="Your payment has been authorized and is awaiting capture.",
        translate=True,
    )
    epaync_msg_captured = fields.Text(
        string="Payment Captured",
        default="Your payment has been captured successfully.",
        translate=True,
    )

    # ── Computed URLs ─────────────────────────────────────────────────────────
    epaync_url_return = fields.Char(
        string="Return URL",
        compute='_compute_epaync_urls',
    )
    epaync_url_success = fields.Char(
        string="Success URL",
        compute='_compute_epaync_urls',
    )
    epaync_url_cancel = fields.Char(
        string="Cancel URL",
        compute='_compute_epaync_urls',
    )
    epaync_url_refused = fields.Char(
        string="Refused URL",
        compute='_compute_epaync_urls',
    )
    epaync_url_error = fields.Char(
        string="Error URL",
        compute='_compute_epaync_urls',
    )
    epaync_url_ipn = fields.Char(
        string="Webhook / IPN URL",
        compute='_compute_epaync_urls',
    )

    # ── Computed helpers ──────────────────────────────────────────────────────
    @api.depends('website_id', 'company_id.website_id')
    def _compute_epaync_urls(self):
        for provider in self:
            base = provider._epaync_get_base_url()
            provider.epaync_url_return = f"{base}/payment/epaync/return"
            provider.epaync_url_success = f"{base}/payment/epaync/success"
            provider.epaync_url_cancel = f"{base}/payment/epaync/cancel"
            provider.epaync_url_refused = f"{base}/payment/epaync/refused"
            provider.epaync_url_error = f"{base}/payment/epaync/error"
            provider.epaync_url_ipn = f"{base}/payment/epaync/ipn"

    def _epaync_get_base_url(self):
        """Return the EpayNC-facing base URL of the current provider.

        Odoo 18 resolves the base URL from the current website/company (see
        ``base`` / ``website_payment`` overrides of ``get_base_url``). EpayNC
        requires HTTPS for all callback URLs, so public ``http://`` hosts are
        upgraded to ``https://`` (localhost/127.0.0.1 are kept as-is for dev).

        :return: The base URL, without trailing slash.
        :rtype: str
        """
        self.ensure_one()
        base_url = (self.get_base_url() or '').rstrip('/')
        if (
            base_url.startswith('http://')
            and 'localhost' not in base_url
            and '127.0.0.1' not in base_url
        ):
            base_url = 'https://' + base_url[len('http://'):]
        return base_url

    # ── Signature engine ──────────────────────────────────────────────────────
    def _epaync_get_secret_key(self):
        self.ensure_one()
        if self.state == 'test':
            return self.epaync_test_key or ''
        return self.epaync_production_key or ''

    def _epaync_compute_signature(self, data):
        """
        HMAC SHA-256 signature per EpayNC / Lyra specification:
          1. Collect all vads_* fields, sort alphabetically by key
          2. Join values with '+'
          3. Append '+' + secret_key
          4. HMAC-SHA256 using secret_key as the HMAC key
          5. Base64-encode the digest
        """
        self.ensure_one()
        key = self._epaync_get_secret_key()
        vads_items = sorted(
            ((k, str(v)) for k, v in data.items() if k.startswith('vads_')),
            key=lambda x: x[0],
        )
        message = '+'.join(v for _, v in vads_items) + '+' + key
        digest = hmac.new(
            key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode('utf-8'), message

    def _epaync_verify_signature(self, data):
        """Return (is_valid, computed_sig, received_sig, sig_string)."""
        self.ensure_one()
        received_sig = data.get('signature', '')
        computed_sig, sig_string = self._epaync_compute_signature(data)
        return received_sig == computed_sig, computed_sig, received_sig, sig_string

    # ── Payment framework overrides ───────────────────────────────────────────
    def _get_default_payment_method_codes(self):
        default_codes = super()._get_default_payment_method_codes()
        if self.code == 'epaync':
            return {'epaync'}
        return default_codes

    def _get_redirect_form_view(self, is_validation=False):
        self.ensure_one()
        if self.code == 'epaync':
            return self.env.ref('payment_epaync.redirect_form')
        return super()._get_redirect_form_view(is_validation=is_validation)

    def _should_build_inline_form(self, is_validation=False):
        self.ensure_one()
        if self.code == 'epaync':
            return False
        return super()._should_build_inline_form(is_validation=is_validation)
