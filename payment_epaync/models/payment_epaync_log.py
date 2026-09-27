import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PaymentEpayncLog(models.Model):
    _name = 'payment.epaync.log'
    _description = 'EpayNC Payment Log'
    _order = 'create_date desc'
    _rec_name = 'create_date'

    transaction_id = fields.Many2one(
        'payment.transaction',
        string="Transaction",
        ondelete='set null',
        index=True,
    )
    provider_id = fields.Many2one(
        'payment.provider',
        string="Provider",
        ondelete='set null',
    )
    log_type = fields.Selection([
        ('request', 'Request'),
        ('response', 'Response'),
        ('webhook', 'Webhook'),
        ('error', 'Error'),
        ('signature', 'Signature'),
    ], string="Type", default='request', required=True)
    request_data = fields.Text(string="Request Data")
    response_data = fields.Text(string="Response Data")
    headers = fields.Text(string="Headers")
    signature_string = fields.Text(string="Signature String")
    generated_signature = fields.Char(string="Generated Signature")
    received_signature = fields.Char(string="Received Signature")
    signature_valid = fields.Boolean(string="Signature Valid")
    webhook_payload = fields.Text(string="Webhook Payload")
    error_message = fields.Text(string="Error Message")
    retry_count = fields.Integer(string="Retry Count", default=0)
    timestamp = fields.Datetime(string="Timestamp", default=fields.Datetime.now)

    @api.model
    def _log(self, provider, transaction=None, log_type='request', **kwargs):
        if not provider.epaync_enable_logs:
            return self.env['payment.epaync.log']
        vals = {
            'provider_id': provider.id,
            'log_type': log_type,
            'timestamp': fields.Datetime.now(),
        }
        if transaction:
            vals['transaction_id'] = transaction.id
        vals.update(kwargs)
        try:
            return self.create(vals)
        except Exception:
            _logger.exception("EpayNC: Failed to write log entry")
            return self.env['payment.epaync.log']
