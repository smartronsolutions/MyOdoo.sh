import base64
import hashlib
import hmac
import logging
import math
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

EPAYNC_PAYMENT_URL = 'https://epaync.nc/vads-payment/'

# XPF is pegged to the euro at a fixed rate (1 EUR = 119.3317 XPF). It is used as a
# fallback when no exchange rate is configured in Odoo for a EUR <-> XPF conversion.
XPF_PER_EUR = 119.3317


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
    epaync_gateway_currency_id = fields.Many2one(
        string="Gateway Currency",
        comodel_name='res.currency',
        default=lambda self: self.env.ref('base.XPF', raise_if_not_found=False),
        context={'active_test': False},
        help="Currency actually sent to EpayNC as 'vads_currency' (ISO 4217 numeric code). "
             "The order total is automatically converted from the customer currency to this "
             "currency using Odoo's exchange rates (the fixed 1 EUR = 119.3317 XPF peg is used "
             "as a fallback if no rate is configured).\n"
             "EpayNC New Caledonia contracts usually only accept XPF (953): keep this set to XPF "
             "to allow EUR/USD/any other currency at checkout. If EpayNC enabled another currency "
             "on your shop/contract, select it here. Leave empty to send the order currency "
             "unchanged (only do this if that currency is enabled at EpayNC, otherwise the gateway "
             "returns error 10 'vads_currency Currency not found').",
    )
    epaync_min_amount = fields.Monetary(
        string="Minimum Order Amount",
        currency_field='epaync_gateway_currency_id',
        default=1000.0,
        help="Minimum amount accepted by your EpayNC contract (MID), expressed in the "
             "Gateway Currency. Example: most New Caledonian CB MIDs reject amounts below "
             "1 000.00 XPF and answer the error '10118 - MID_MIN_AMOUNT_NOT_REACHED'. "
             "The checkout blocks the payment below this amount and shows the limit "
             "converted into the customer's currency (1 000 XPF ≈ 9 €). "
             "Set 0 to disable the check.",
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
    epaync_msg_min_amount = fields.Text(
        string="Order Below Minimum Amount",
        translate=True,
        help="Optional. Message shown on the checkout page when the order total is below "
             "the Minimum Order Amount. Leave empty to use the built-in message. "
             "Available placeholders: %(minimum)s (minimum in the customer currency), "
             "%(gateway_minimum)s (minimum in the gateway currency), %(total)s (order "
             "total), %(missing)s (amount missing to reach the minimum) and %(currency)s.",
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

    # ── Currency conversion ───────────────────────────────────────────────────
    def _epaync_has_conversion_rate(self, from_currency, to_currency):
        """Return whether Odoo has the rates required to convert between two currencies.

        :param res.currency from_currency: The currency to convert from.
        :param res.currency to_currency: The currency to convert to.
        :return: Whether a conversion rate is available.
        :rtype: bool
        """
        self.ensure_one()
        company = self.company_id
        company_currency = company.currency_id
        for currency in (from_currency | to_currency):
            if currency == company_currency:
                continue
            has_rate = self.env['res.currency.rate'].sudo().search_count([
                ('currency_id', '=', currency.id),
                ('company_id', 'in', (False, company.root_id.id)),
            ], limit=1)
            if not has_rate:
                return False
        return True

    def _epaync_convert_amount(self, amount, from_currency, to_currency, date=None):
        """Convert ``amount`` from one currency to another for the gateway.

        Odoo's configured exchange rates are used when available. As a fallback, the
        fixed EUR <-> XPF peg (1 EUR = 119.3317 XPF) is applied so that EpayNC NC
        contracts (XPF) work out of the box with euro-priced catalogs.

        :param float amount: The amount to convert.
        :param res.currency from_currency: The currency of ``amount``.
        :param res.currency to_currency: The currency to send to the gateway.
        :param date date: The date used to pick the exchange rate.
        :return: The converted amount, rounded on ``to_currency``.
        :rtype: float
        """
        self.ensure_one()
        if not amount or from_currency == to_currency:
            return amount
        if self._epaync_has_conversion_rate(from_currency, to_currency):
            return from_currency._convert(amount, to_currency, self.company_id, date)
        if {from_currency.name, to_currency.name} == {'EUR', 'XPF'}:
            if from_currency.name == 'EUR':
                return to_currency.round(amount * XPF_PER_EUR)
            return to_currency.round(amount / XPF_PER_EUR)
        raise ValidationError(_(
            "EpayNC: no exchange rate is configured to convert %(from)s into %(to)s on "
            "company %(company)s. Please define a rate in Accounting ▸ Configuration ▸ "
            "Currencies, or set the Gateway Currency to the order currency.",
            from_currency=from_currency.name,
            to_currency=to_currency.name,
            company=self.company_id.display_name,
        ))

    # ── Minimum order amount ──────────────────────────────────────────────────
    def _epaync_get_min_amount(self):
        """Return the minimum amount accepted by the gateway.

        :return: The minimum amount, in the gateway currency, or ``0`` when disabled.
        :rtype: float
        """
        self.ensure_one()
        return float(self.epaync_min_amount or 0.0)

    def _epaync_get_min_amount_in_currency(self, currency, date=None):
        """Return the gateway minimum converted into ``currency``.

        The minimum is a limit of the EpayNC contract, expressed in the gateway
        currency (1 000 XPF for most New Caledonian CB MIDs). Since the shop may bill
        in another currency (e.g. EUR), the limit is converted back into that currency
        and rounded *up* to the next whole unit. The customer then sees a friendly
        floor (1 000 XPF -> 9 €) which is guaranteed to clear the gateway limit.

        :param res.currency currency: The currency to convert the minimum into.
        :param date date: The date used to pick the exchange rate.
        :return: The minimum amount, in ``currency`` (``0`` when disabled).
        :rtype: float
        """
        self.ensure_one()
        min_amount = self._epaync_get_min_amount()
        if not min_amount or not currency:
            return 0.0
        gateway_currency = self.epaync_gateway_currency_id or currency
        if gateway_currency == currency:
            min_in_currency = min_amount
        else:
            min_in_currency = self._epaync_convert_amount(
                min_amount, gateway_currency, currency, date
            )
        return currency.round(math.ceil(min_in_currency))

    def _epaync_is_below_min_amount(self, amount, currency, date=None):
        """Return whether ``amount`` is below the minimum accepted by the gateway.

        :param float amount: The amount to check, in ``currency``.
        :param res.currency currency: The currency of ``amount``.
        :param date date: The date used to pick the exchange rate.
        :return: Whether the amount is below the minimum.
        :rtype: bool
        """
        self.ensure_one()
        if not currency:
            return False
        min_amount = self._epaync_get_min_amount_in_currency(currency, date=date)
        return bool(min_amount) and currency.compare_amounts(amount or 0.0, min_amount) < 0

    def _epaync_build_min_amount_message(self, amount, currency):
        """Build the detailed (title, message) shown when the amount is below the minimum.

        :param float amount: The amount of the payment, in ``currency``.
        :param res.currency currency: The currency of ``amount``.
        :return: The title and the detailed message.
        :rtype: tuple[str, str]
        """
        self.ensure_one()
        # Format the amounts with the current language (the currency record may carry
        # another environment/language, e.g. when it was browsed by another recordset).
        display_currency = currency.with_env(self.env)
        min_amount = self._epaync_get_min_amount_in_currency(currency)
        total = currency.round(amount or 0.0)
        missing = currency.round(max(0.0, min_amount - total))
        gateway_currency = self.epaync_gateway_currency_id.with_env(self.env)
        template = self.epaync_msg_min_amount or _(
            "The minimum order amount for this payment method is %(minimum)s "
            "(gateway limit: %(gateway_minimum)s). Your order total is %(total)s, "
            "so %(missing)s is missing. Please add products to your cart to continue."
        )
        placeholders = {
            'minimum': display_currency.format(min_amount),
            'gateway_minimum': (
                gateway_currency.format(self._epaync_get_min_amount())
                if gateway_currency else ''
            ),
            'total': display_currency.format(total),
            'missing': display_currency.format(missing),
            'currency': currency.name or '',
        }
        # Only substitute the known %(name)s placeholders so a stray '%' typed by the
        # admin in the custom message can never break the checkout page.
        message = re.sub(
            r'%\((\w+)\)s',
            lambda match: str(placeholders.get(match.group(1), match.group(0))),
            str(template),
        )
        return _("Minimum order amount not reached"), message

    def _epaync_get_min_amount_error(self, amount, currency, date=None):
        """Return the (title, message) when ``amount`` is below the minimum, else ``None``.

        Used by the checkout page to display a friendly blocking message instead of
        letting the payment reach the gateway (which would answer the error
        ``10118 - MID_MIN_AMOUNT_NOT_REACHED``).

        :param float amount: The amount of the payment, in ``currency``.
        :param res.currency currency: The currency of ``amount``.
        :param date date: The date used to pick the exchange rate.
        :return: The title and message, or ``None`` when the amount is accepted.
        :rtype: tuple[str, str] | None
        """
        self.ensure_one()
        if self._epaync_is_below_min_amount(amount, currency, date=date):
            return self._epaync_build_min_amount_message(amount, currency)
        return None

    def _epaync_raise_if_below_min_amount(self, amount, currency, date=None):
        """Raise a ``ValidationError`` when ``amount`` is below the minimum.

        Called right before the payment form is rendered so that a too-small payment
        never reaches the gateway (whatever the payment flow: checkout, portal, payment
        link, ...).

        :raise ValidationError: When the amount is below the minimum.
        """
        self.ensure_one()
        error = self._epaync_get_min_amount_error(amount, currency, date=date)
        if error:
            title, message = error
            raise ValidationError(f"{title}\n{message}")

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
    def _get_supported_currencies(self):
        """Allow every currency: amounts are converted to the gateway currency.

        Returning all currencies (including inactive ones) keeps EpayNC available
        at checkout whatever the customer/company currency is, since the amount is
        converted to ``epaync_gateway_currency_id`` before being sent to the gateway.
        """
        self.ensure_one()
        if self.code == 'epaync':
            return self.env['res.currency'].with_context(active_test=False).search([])
        return super()._get_supported_currencies()

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
