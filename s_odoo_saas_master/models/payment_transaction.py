from odoo import models

import logging

_logger = logging.getLogger(__name__)


def _is_concurrency_error(exc):
    """PostgreSQL concurrency errors must bubble up so Odoo can retry the request."""
    return (
        type(exc).__name__ in ('SerializationFailure', 'DeadlockDetected', 'LockNotAvailable')
        or 'could not serialize' in str(exc).lower()
    )


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _post_process(self):
        """Apply SaaS orders (invoice + storage upgrade) once the payment is done.

        ``/payment/status/poll`` calls ``_post_process`` as soon as the provider
        confirms the payment. Doing the work here guarantees the purchased storage is
        credited even if the customer never reaches the ``/shop/confirmation`` page.
        """
        res = super()._post_process()
        for tx in self:
            if tx.state != 'done':
                continue
            orders = tx.sale_order_ids.filtered(lambda o: o.is_saas_order and o.instance_id)
            if not orders:
                continue
            try:
                orders.sudo()._finalize_saas_payment()
            except Exception as e:
                if _is_concurrency_error(e):
                    raise
                # A storage/invoice error must never break the payment confirmation:
                # the portal page retries the upgrade through the status endpoint.
                _logger.exception(
                    "Could not finalize SaaS payment for transaction %s", tx.id
                )
        return res
