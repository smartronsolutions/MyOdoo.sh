# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
import hashlib
import hmac

from odoo import fields
from odoo.tests import tagged

from odoo.addons.payment.tests.common import PaymentCommon
from odoo.addons.payment_epaync.models.payment_transaction import (
    epaync_numeric_code,
)


@tagged('-at_install', 'post_install')
class TestPaymentEpaync(PaymentCommon):
    """Test the Odoo 18 EpayNC payment provider implementation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.provider = cls._prepare_provider(
            code='epaync',
            update_values={
                'epaync_merchant_id': '14282108',
                'epaync_test_key': 'test-secret-key',
            },
        )
        cls.payment_method = cls.provider.payment_method_ids.filtered(
            lambda pm: pm.code == 'epaync'
        )
        cls.payment_method_id = cls.payment_method.id
        cls.payment_method_code = 'epaync'
        cls.xpf_currency = cls._prepare_currency('XPF')

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _expected_amount(self, tx):
        return str(int(round(tx.amount * (10 ** tx.currency_id.decimal_places))))

    def _expected_gateway_amount(self, tx):
        """Return the expected ``vads_amount`` for the tx's gateway currency."""
        provider = self.provider
        gateway_currency = provider.epaync_gateway_currency_id or tx.currency_id
        date = fields.Date.to_date(tx.create_date) if tx.create_date else fields.Date.today()
        converted = provider._epaync_convert_amount(
            tx.amount, tx.currency_id, gateway_currency, date
        )
        return str(int(round(converted * (10 ** gateway_currency.decimal_places))))

    def _build_notification(self, tx, **overrides):
        """Return a validly-signed EpayNC notification payload for the tx."""
        gateway_currency = tx.epaync_gateway_currency_id or tx.currency_id
        data = {
            'vads_trans_id': tx.epaync_vads_trans_id or '000001',
            'vads_amount': tx.epaync_gateway_amount or self._expected_amount(tx),
            'vads_currency': epaync_numeric_code(gateway_currency),
            'vads_site_id': self.provider.epaync_merchant_id,
            'vads_trans_status': 'AUTHORISED',
            'vads_auth_result': '00',
            'vads_trans_uuid': 'gateway-uuid-123',
        }
        data.update(overrides)
        signature, _ = self.provider._epaync_compute_signature(data)
        data['signature'] = signature
        return data

    # ── Provider configuration ────────────────────────────────────────────────
    def test_provider_is_registered(self):
        self.assertEqual(self.provider.code, 'epaync')
        self.assertEqual(self.provider.state, 'test')

    def test_default_payment_method_codes(self):
        self.assertEqual(self.provider._get_default_payment_method_codes(), {'epaync'})
        self.assertTrue(self.payment_method.active)

    def test_redirect_form_and_inline_form(self):
        self.assertEqual(
            self.provider._get_redirect_form_view(),
            self.env.ref('payment_epaync.redirect_form'),
        )
        self.assertFalse(self.provider._should_build_inline_form())

    def test_endpoint_urls_are_computed(self):
        self.assertTrue(self.provider.epaync_url_return.endswith('/payment/epaync/return'))
        self.assertTrue(self.provider.epaync_url_ipn.endswith('/payment/epaync/ipn'))

    # ── Signature engine ──────────────────────────────────────────────────────
    def test_signature_matches_lyra_specification(self):
        """HMAC-SHA256 over alphabetically-sorted vads_* values + '+' + key."""
        data = {'vads_amount': '100', 'vads_site_id': '14282108'}
        signature, message = self.provider._epaync_compute_signature(data)

        expected_values = '+'.join(
            value for _, value in sorted(
                (key, value) for key, value in data.items() if key.startswith('vads_')
            )
        )
        expected_message = expected_values + '+' + 'test-secret-key'
        expected_signature = base64.b64encode(
            hmac.new(
                b'test-secret-key', expected_message.encode('utf-8'), hashlib.sha256
            ).digest()
        ).decode('utf-8')

        self.assertEqual(message, expected_message)
        self.assertEqual(signature, expected_signature)

    def test_signature_verification(self):
        data = {'vads_amount': '100', 'vads_site_id': '14282108'}
        signature, _ = self.provider._epaync_compute_signature(data)

        valid, _, _, _ = self.provider._epaync_verify_signature(
            {**data, 'signature': signature}
        )
        self.assertTrue(valid)

        valid, _, _, _ = self.provider._epaync_verify_signature(
            {**data, 'signature': 'tampered'}
        )
        self.assertFalse(valid)

    # ── Currencies ────────────────────────────────────────────────────────────
    def test_gateway_currency_defaults_to_xpf(self):
        self.assertEqual(self.provider.epaync_gateway_currency_id, self.xpf_currency)

    def test_currency_numeric_code_helper(self):
        self.assertEqual(epaync_numeric_code(self.currency_euro), '978')
        self.assertEqual(epaync_numeric_code(self.xpf_currency), '953')
        self.assertEqual(epaync_numeric_code(self.env['res.currency']), '')

    def test_gateway_currency_uses_order_currency_when_configured(self):
        """With the gateway currency set to the order currency, nothing is converted."""
        self.provider.epaync_gateway_currency_id = self.currency_euro
        tx = self._create_transaction(flow='redirect')
        processing_values = tx._get_processing_values()
        inputs = self._extract_values_from_html_form(
            processing_values['redirect_form_html']
        )['inputs']

        self.assertEqual(inputs['vads_currency'], '978')  # EUR ISO 4217 numeric code
        self.assertEqual(inputs['vads_amount'], self._expected_amount(tx))
        self.assertEqual(tx.epaync_gateway_currency_id, self.currency_euro)
        self.assertEqual(tx.epaync_gateway_amount, self._expected_amount(tx))

    def test_amount_is_converted_to_xpf_with_fixed_peg(self):
        """Without any configured rate, EUR -> XPF uses the fixed peg (953)."""
        self.env['res.currency.rate'].sudo().search([
            ('currency_id', 'in', (self.currency_euro | self.xpf_currency).ids),
        ]).unlink()

        tx = self._create_transaction(flow='redirect')
        processing_values = tx._get_processing_values()
        inputs = self._extract_values_from_html_form(
            processing_values['redirect_form_html']
        )['inputs']

        self.assertEqual(inputs['vads_currency'], '953')  # XPF ISO 4217 numeric code
        self.assertEqual(inputs['vads_amount'], str(int(round(self.amount * 119.3317))))
        self.assertEqual(tx.epaync_gateway_currency_id, self.xpf_currency)
        self.assertEqual(tx.epaync_gateway_amount, self._expected_gateway_amount(tx))

    def test_conversion_uses_configured_rate_when_available(self):
        """A rate configured in Odoo takes precedence over the fixed peg."""
        company = self.env.company
        self.env['res.currency.rate'].sudo().search([
            ('currency_id', 'in', (self.currency_euro | self.xpf_currency).ids),
        ]).unlink()
        for currency in (self.currency_euro, self.xpf_currency):
            if currency == company.currency_id:
                continue
            self.env['res.currency.rate'].sudo().create({
                'currency_id': currency.id,
                'rate': 1.0,
                'name': '2020-01-01',
                'company_id': company.id,
            })

        converted = self.provider._epaync_convert_amount(
            self.amount, self.currency_euro, self.xpf_currency, fields.Date.today()
        )
        expected = self.currency_euro._convert(
            self.amount, self.xpf_currency, company, fields.Date.today()
        )
        self.assertAlmostEqual(converted, expected, places=2)

    # ── Redirect form rendering ───────────────────────────────────────────────
    def test_redirect_form_rendering(self):
        # Charge in the order currency (no conversion) to keep this test focused on
        # the form itself; conversion is covered by the currency tests above.
        self.provider.epaync_gateway_currency_id = self.currency_euro
        tx = self._create_transaction(flow='redirect')
        processing_values = tx._get_processing_values()

        self.assertIn('redirect_form_html', processing_values)
        form = self._extract_values_from_html_form(processing_values['redirect_form_html'])

        self.assertEqual(form['action'], self.provider.epaync_payment_url)
        self.assertEqual(form['method'], 'post')

        inputs = form['inputs']
        self.assertEqual(inputs['vads_site_id'], '14282108')
        self.assertEqual(inputs['vads_currency'], '978')  # EUR ISO 4217 numeric code
        self.assertEqual(inputs['vads_amount'], self._expected_amount(tx))
        self.assertEqual(inputs['vads_ctx_mode'], 'TEST')
        self.assertEqual(inputs['vads_version'], 'V2')
        self.assertEqual(inputs['vads_page_action'], 'PAYMENT')
        self.assertTrue(inputs['signature'])

    # ── Notification lookup ───────────────────────────────────────────────────
    def test_find_transaction_by_vads_trans_id(self):
        tx = self._create_transaction(flow='redirect')
        tx.epaync_vads_trans_id = '000456'
        data = self._build_notification(tx, vads_trans_id='000456')

        found = self.env['payment.transaction']._get_tx_from_notification_data(
            'epaync', data
        )
        self.assertEqual(found, tx)

    # ── Notification processing ───────────────────────────────────────────────
    def test_notification_authorised_sets_done(self):
        tx = self._create_transaction(flow='redirect')
        tx.epaync_vads_trans_id = '000123'
        data = self._build_notification(tx, vads_trans_id='000123')

        tx._process_notification_data(data)

        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.epaync_gateway_status, 'AUTHORISED')
        self.assertEqual(tx.epaync_auth_result, '00')
        self.assertEqual(tx.epaync_gateway_trans_id, 'gateway-uuid-123')
        self.assertEqual(tx.provider_reference, 'gateway-uuid-123')

    def test_notification_refused_sets_error(self):
        tx = self._create_transaction(flow='redirect')
        tx.epaync_vads_trans_id = '000789'
        data = self._build_notification(
            tx, vads_trans_id='000789', vads_trans_status='REFUSED', vads_auth_result='51'
        )

        tx._process_notification_data(data)

        self.assertEqual(tx.state, 'error')
        self.assertEqual(tx.epaync_gateway_status, 'REFUSED')

    def test_notification_cancelled_sets_cancel(self):
        tx = self._create_transaction(flow='redirect')
        tx.epaync_vads_trans_id = '000321'
        data = self._build_notification(
            tx, vads_trans_id='000321', vads_trans_status='CANCELLED'
        )

        tx._process_notification_data(data)

        self.assertEqual(tx.state, 'cancel')

    def test_notification_invalid_signature_sets_error(self):
        tx = self._create_transaction(flow='redirect')
        tx.epaync_vads_trans_id = '000999'
        data = self._build_notification(tx, vads_trans_id='000999')
        data['signature'] = 'invalid-signature'

        tx._process_notification_data(data)

        self.assertEqual(tx.state, 'error')
        self.assertIn('ecurity', tx.state_message)  # "Security verification failed..."

    def test_notification_log_is_created(self):
        tx = self._create_transaction(flow='redirect')
        tx.epaync_vads_trans_id = '000654'
        data = self._build_notification(tx, vads_trans_id='000654')

        tx._process_notification_data(data)

        log = self.env['payment.epaync.log'].search([
            ('transaction_id', '=', tx.id),
            ('log_type', '=', 'webhook'),
        ])
        self.assertEqual(len(log), 1)
        self.assertTrue(log.signature_valid)
