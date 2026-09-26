from odoo import fields, models, api


class AccountMove(models.Model):
    _inherit = 'account.move'

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', compute='_compute_instance_id', store=True,
        readonly=False, compute_sudo=True, groups="s_odoo_saas_master.group_odoo_saas_user")

    @api.depends('line_ids', 'line_ids.sale_line_ids')
    def _compute_instance_id(self):
        for r in self:
            r.instance_id = False
            if r.line_ids.sale_line_ids:
                sale_order = r.line_ids.sale_line_ids[0].order_id
                if sale_order and sale_order.instance_id:
                    r.instance_id = sale_order.instance_id

    def _auto_paid_saas_invoice(self):
        for r in self:
            if r.instance_id and r.payment_state != 'paid':
                reconcile_lines = self.env['account.move.line']
                payment_lines = self.env['account.move.line']
                for transaction in r.transaction_ids:
                    if transaction.state == 'done':
                        payment_lines |= transaction.payment_id.move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')

                receivable_lines = r.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
                if payment_lines and receivable_lines:
                    reconcile_lines = payment_lines + receivable_lines
                reconcile_lines.reconcile()
