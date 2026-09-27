import logging
import re
import time
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# ISO 4217 alpha → numeric code mapping (required by EpayNC/Lyra gateway)
ISO4217_NUMERIC = {
    'AED': '784', 'AFN': '971', 'ALL': '008', 'AMD': '051', 'ANG': '532',
    'AOA': '973', 'ARS': '032', 'AUD': '036', 'AWG': '533', 'AZN': '944',
    'BAM': '977', 'BBD': '052', 'BDT': '050', 'BGN': '975', 'BHD': '048',
    'BIF': '108', 'BMD': '060', 'BND': '096', 'BOB': '068', 'BRL': '986',
    'BSD': '044', 'BTN': '064', 'BWP': '072', 'BYN': '933', 'BZD': '084',
    'CAD': '124', 'CDF': '976', 'CHF': '756', 'CLP': '152', 'CNY': '156',
    'COP': '170', 'CRC': '188', 'CUP': '192', 'CVE': '132', 'CZK': '203',
    'DJF': '262', 'DKK': '208', 'DOP': '214', 'DZD': '012', 'EGP': '818',
    'ETB': '230', 'EUR': '978', 'FJD': '242', 'FKP': '238', 'GBP': '826',
    'GEL': '981', 'GHS': '936', 'GIP': '292', 'GMD': '270', 'GNF': '324',
    'GTQ': '320', 'GYD': '328', 'HKD': '344', 'HNL': '340', 'HTG': '332',
    'HUF': '348', 'IDR': '360', 'ILS': '376', 'INR': '356', 'IQD': '368',
    'ISK': '352', 'JMD': '388', 'JOD': '400', 'JPY': '392', 'KES': '404',
    'KGS': '417', 'KHR': '116', 'KMF': '174', 'KRW': '410', 'KWD': '414',
    'KYD': '136', 'KZT': '398', 'LAK': '418', 'LBP': '422', 'LKR': '144',
    'LRD': '430', 'LYD': '434', 'MAD': '504', 'MDL': '498', 'MGA': '969',
    'MKD': '807', 'MMK': '104', 'MNT': '496', 'MOP': '446', 'MRU': '929',
    'MUR': '480', 'MVR': '462', 'MWK': '454', 'MXN': '484', 'MYR': '458',
    'MZN': '943', 'NAD': '516', 'NGN': '566', 'NIO': '558', 'NOK': '578',
    'NPR': '524', 'NZD': '554', 'OMR': '512', 'PAB': '590', 'PEN': '604',
    'PGK': '598', 'PHP': '608', 'PKR': '586', 'PLN': '985', 'PYG': '600',
    'QAR': '634', 'RON': '946', 'RSD': '941', 'RUB': '643', 'RWF': '646',
    'SAR': '682', 'SBD': '090', 'SCR': '690', 'SEK': '752', 'SGD': '702',
    'SHP': '654', 'SOS': '706', 'SRD': '968', 'STN': '930', 'SYP': '760',
    'SZL': '748', 'THB': '764', 'TJS': '972', 'TMT': '934', 'TND': '788',
    'TOP': '776', 'TRY': '949', 'TTD': '780', 'TWD': '901', 'TZS': '834',
    'UAH': '980', 'UGX': '800', 'USD': '840', 'UYU': '858', 'UZS': '860',
    'VES': '928', 'VND': '704', 'VUV': '548', 'WST': '882', 'XAF': '950',
    'XCD': '951', 'XOF': '952', 'XPF': '953', 'YER': '886', 'ZAR': '710',
    'ZMW': '967', 'ZWL': '932',
}

# EpayNC vads_trans_status → Odoo payment state
# EpayNC SINGLE mode (immediate capture, capture_delay=0): AUTHORISED means money
# is taken — treat as done. _set_authorized() requires support_manual_capture on
# the provider which is only needed for MULTI/deferred capture workflows.
EPAYNC_STATUS_MAP = {
    'AUTHORISED': 'done',
    'AUTHORISED_TO_VALIDATE': 'pending',
    'CAPTURED': 'done',
    'UNDER_VERIFICATION': 'pending',
    'REFUSED': 'error',
    'CANCELLED': 'cancel',
    'ABANDONED': 'cancel',
    'EXPIRED': 'cancel',
    'NOT_CREATED': 'error',
    'CAPTURE_FAILED': 'error',
    'WAITING_AUTHORISATION': 'pending',
    'WAITING_AUTHORISATION_TO_VALIDATE': 'pending',
    'INITIAL': 'pending',
}

AUTH_RESULT_MESSAGES = {
    '03': "Invalid merchant / acceptor",
    '05': "Do not honor",
    '12': "Invalid transaction",
    '13': "Invalid amount",
    '14': "Invalid card number",
    '33': "Card expired",
    '34': "Fraud suspicion",
    '41': "Lost card",
    '43': "Stolen card",
    '51': "Insufficient funds",
    '54': "Card expired",
    '56': "Card not found",
    '57': "Transaction not permitted to cardholder",
    '59': "Suspicion of fraud",
    '60': "Contact card acceptor",
    '81': "Cryptographic error",
}


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # ── EpayNC-specific fields ────────────────────────────────────────────────
    epaync_merchant_ref = fields.Char(string="Merchant Reference")
    epaync_gateway_trans_id = fields.Char(string="Gateway Transaction ID")
    epaync_auth_number = fields.Char(string="Authorization Number")
    epaync_vads_trans_id = fields.Char(string="vads_trans_id", readonly=True)
    epaync_shop_id = fields.Char(string="Shop ID")
    epaync_gateway_status = fields.Char(string="Gateway Status")
    epaync_card_brand = fields.Char(string="Card Brand")
    epaync_card_type = fields.Char(string="Card Type")
    epaync_auth_result = fields.Char(string="Authorization Result Code")
    epaync_auth_result_msg = fields.Char(string="Authorization Result Message")
    epaync_risk_result = fields.Char(string="Risk Result")
    epaync_signature = fields.Char(string="Received Signature")
    epaync_payment_date = fields.Datetime(string="Payment Date")
    epaync_ip_address = fields.Char(string="Customer IP")
    epaync_request_data = fields.Text(string="Request Data")
    epaync_response_data = fields.Text(string="Response Data")
    epaync_webhook_payload = fields.Text(string="Webhook Payload")
    epaync_headers = fields.Text(string="Request Headers")
    epaync_processing_time = fields.Float(string="Processing Time (s)", digits=(10, 4))

    # ── Transaction ID generation ─────────────────────────────────────────────
    def _epaync_generate_trans_id(self):
        """6-digit numeric ID unique per day per shop (uses DB id modulo)."""
        self.ensure_one()
        return str(self.id % 1000000).zfill(6)

    # ── Rendering values (form POST to gateway) ───────────────────────────────
    def _get_specific_rendering_values(self, processing_values):
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'epaync':
            return res

        provider = self.provider_id
        start_time = time.time()

        vads_trans_id = self._epaync_generate_trans_id()
        trans_date = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')

        # Use actual transaction currency — convert to ISO 4217 numeric code
        currency = self.currency_id
        currency_alpha = currency.name if currency else 'XPF'
        currency_numeric = ISO4217_NUMERIC.get(currency_alpha, '953')

        # Amount in smallest currency unit (cents for USD/EUR, whole units for XPF/JPY)
        decimal_places = currency.decimal_places if currency else 0
        raw_amount = processing_values.get('amount', self.amount)
        amount = int(round(raw_amount * (10 ** decimal_places)))

        base_url = provider._epaync_get_base_url()

        # Use processing_values['reference'] — self.reference may be stale if Odoo
        # applied the uniqueness suffix after we read the field (e.g. '-6').
        order_ref = processing_values.get('reference') or self.reference
        # EpayNC: vads_order_id must be alphanumeric (no / or special chars)
        order_ref_clean = re.sub(r'[^A-Za-z0-9_-]', '-', order_ref or '')[:32]

        vads_data = {
            'vads_action_mode': 'INTERACTIVE',
            'vads_amount': str(amount),
            'vads_ctx_mode': 'TEST' if provider.state == 'test' else 'PRODUCTION',
            'vads_currency': currency_numeric,
            'vads_page_action': 'PAYMENT',
            'vads_payment_config': provider.epaync_payment_action or 'SINGLE',
            'vads_return_mode': 'POST',
            'vads_site_id': provider.epaync_merchant_id or '',
            'vads_trans_date': trans_date,
            'vads_trans_id': vads_trans_id,
            'vads_version': 'V2',
            'vads_order_id': order_ref_clean or self.reference[:32],
            'vads_url_return': f"{base_url}/payment/epaync/return",
            'vads_url_success': f"{base_url}/payment/epaync/success",
            'vads_url_refused': f"{base_url}/payment/epaync/refused",
            'vads_url_cancel': f"{base_url}/payment/epaync/cancel",
            'vads_url_error': f"{base_url}/payment/epaync/error",
            'vads_url_check': f"{base_url}/payment/epaync/ipn",
        }

        if self.partner_email:
            vads_data['vads_cust_email'] = self.partner_email
        if self.partner_name:
            vads_data['vads_cust_name'] = self.partner_name[:127]
        if self.partner_phone:
            vads_data['vads_cust_phone'] = self.partner_phone
        if self.partner_city:
            vads_data['vads_cust_city'] = self.partner_city
        if self.partner_zip:
            vads_data['vads_cust_zip'] = self.partner_zip

        computed_sig, sig_string = provider._epaync_compute_signature(vads_data)
        vads_data['signature'] = computed_sig

        # Build the return dict expected by ir.qweb._render():
        #   - api_url    : top-level QWeb variable — used as the <form action>
        #   - form_fields: top-level QWeb variable — iterated to render hidden inputs
        #   All other keys are kept for backwards compatibility.
        api_url = provider.epaync_payment_url or 'https://epaync.nc/vads-payment/'
        form_fields = dict(vads_data)  # all vads_* + signature (iterated in QWeb template)

        processing_time = time.time() - start_time
        self.sudo().write({
            'epaync_vads_trans_id': vads_trans_id,
            'epaync_shop_id': provider.epaync_merchant_id,
            'epaync_request_data': str(form_fields),
            'epaync_processing_time': processing_time,
        })

        self.env['payment.epaync.log'].sudo()._log(
            provider=provider,
            transaction=self,
            log_type='request',
            request_data=str(vads_data),
            signature_string=sig_string,
            generated_signature=computed_sig,
        )

        return {
            'api_url': api_url,        # QWeb: <form t-att-action="api_url">
            'form_fields': form_fields, # QWeb: t-foreach="form_fields.items()"
            **vads_data,               # keep flat keys for IPN/return processing
        }

    # ── Accounting reconciliation ─────────────────────────────────────────────
    # Note: Odoo 18's `account_payment` module automatically creates the
    # provider-specific `account.payment.method.line` when the provider is
    # enabled (see `payment.provider._ensure_payment_method_line`), so the
    # former `_create_payment` override that injected `payment_method_line_id`
    # is no longer needed and has been removed.

    # ── Notification lookup ───────────────────────────────────────────────────
    @api.model
    def _get_tx_from_notification_data(self, provider_code, notification_data):
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'epaync' or len(tx) == 1:
            return tx

        # Primary: match by stored vads_trans_id — set at request time so it is
        # always consistent with what the gateway echoes back in the IPN.
        vads_trans_id = notification_data.get('vads_trans_id')
        if vads_trans_id:
            tx = self.search([
                ('epaync_vads_trans_id', '=', vads_trans_id),
                ('provider_code', '=', 'epaync'),
            ], limit=1)
            if tx:
                return tx

        # Fallback: match by the order reference we sent as vads_order_id.
        reference = notification_data.get('vads_order_id')
        if not reference:
            raise ValidationError(_("EpayNC: No reference found in notification data."))

        tx = self.search([
            ('reference', '=', reference),
            ('provider_code', '=', 'epaync'),
        ])
        if not tx:
            raise ValidationError(_(
                "EpayNC: No transaction found for reference %s.", reference
            ))
        return tx

    # ── Notification processing ───────────────────────────────────────────────
    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != 'epaync':
            return

        provider = self.provider_id
        start_time = time.time()

        is_valid, computed_sig, received_sig, sig_string = provider._epaync_verify_signature(
            notification_data
        )

        self.env['payment.epaync.log'].sudo()._log(
            provider=provider,
            transaction=self,
            log_type='webhook',
            webhook_payload=str(notification_data),
            signature_string=sig_string,
            generated_signature=computed_sig,
            received_signature=received_sig,
            signature_valid=is_valid,
        )

        if not is_valid:
            _logger.warning(
                "EpayNC: Invalid signature for tx %s | received=%s computed=%s",
                self.reference, received_sig, computed_sig,
            )
            self._set_error(provider.epaync_msg_invalid_signature or "Invalid signature")
            return

        trans_status = notification_data.get('vads_trans_status', '')
        auth_result = notification_data.get('vads_auth_result', '')
        auth_msg = AUTH_RESULT_MESSAGES.get(auth_result, auth_result)

        payment_date_str = notification_data.get('vads_payment_date') or notification_data.get('vads_trans_date')
        payment_date = None
        if payment_date_str:
            try:
                # Odoo Datetime fields require naive datetimes (UTC by convention, no tzinfo)
                payment_date = datetime.strptime(payment_date_str, '%Y%m%d%H%M%S')
            except Exception:
                payment_date = None

        processing_time = time.time() - start_time
        gateway_trans_id = (
            notification_data.get('vads_trans_uuid')
            or notification_data.get('vads_trans_id')
            or ''
        )
        self.sudo().write({
            'provider_reference': gateway_trans_id,
            'epaync_gateway_trans_id': gateway_trans_id,
            'epaync_auth_number': notification_data.get('vads_auth_number', ''),
            'epaync_gateway_status': trans_status,
            'epaync_card_brand': notification_data.get('vads_card_brand', ''),
            'epaync_card_type': notification_data.get('vads_card_type', ''),
            'epaync_auth_result': auth_result,
            'epaync_auth_result_msg': auth_msg,
            'epaync_risk_result': notification_data.get('vads_risk_control', ''),
            'epaync_signature': received_sig,
            'epaync_payment_date': payment_date,
            'epaync_ip_address': notification_data.get('vads_cust_ip', ''),
            'epaync_response_data': str(notification_data),
            'epaync_processing_time': processing_time,
        })

        odoo_state = EPAYNC_STATUS_MAP.get(trans_status)

        if odoo_state == 'done':
            self._set_done()
        elif odoo_state == 'pending':
            self._set_pending()
        elif odoo_state == 'cancel':
            self._set_canceled()
        elif odoo_state == 'error':
            error_detail = f"EpayNC: status={trans_status}"
            if auth_result and auth_result != '00':
                error_detail += f" | auth_result={auth_result} ({auth_msg})"
            self._set_error(error_detail)
        else:
            _logger.warning("EpayNC: Unknown trans_status %s for tx %s", trans_status, self.reference)
            self._set_error(f"EpayNC: Unknown status: {trans_status}")
