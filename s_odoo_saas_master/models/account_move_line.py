from odoo import models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def reconcile(self):
        res = super(AccountMoveLine, self).reconcile()
        for instance in self.move_id.instance_id:
            # Never auto-start an instance that was explicitly suspended, or that is
            # suspended because its storage is full. A storage-full instance is only
            # resumed once a storage upgrade has been applied.
            if not all(inv.payment_state == 'paid' for inv in instance.account_move_ids):
                continue
            if instance.buy_now_from_pricing or instance.operation_state == 'run':
                continue
            if not instance._can_auto_resume():
                continue
            instance.action_start()
        return res
