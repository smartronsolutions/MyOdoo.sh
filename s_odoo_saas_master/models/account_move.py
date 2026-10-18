from odoo import fields, models, api


class AccountMove(models.Model):
    _inherit = 'account.move'

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', compute='_compute_instance_id', store=True,
        readonly=False, compute_sudo=True, groups="s_odoo_saas_master.group_odoo_saas_user")
    saas_container_ref = fields.Char(
        string='SaaS Container', related='instance_id.name', store=True,
        groups="s_odoo_saas_master.group_odoo_saas_user")
    saas_order_type = fields.Selection([
        ('buy_new', 'Container Subscription'),
        ('renew', 'Subscription Renewal'),
        ('buy_extra', 'Extra Apps'),
        ('buy_storage', 'Storage Upgrade'),
        ('buy_workers', 'Workers Upgrade'),
        ('buy_upgrade', 'Resource Upgrade'),
    ], string='SaaS Invoice Type', compute='_compute_saas_invoice_info', store=True,
        compute_sudo=True)
    saas_period_start = fields.Date(string='Subscription Period Start',
        compute='_compute_saas_invoice_info', store=True, compute_sudo=True)
    saas_period_end = fields.Date(string='Subscription Period End',
        compute='_compute_saas_invoice_info', store=True, compute_sudo=True)

    @api.depends('line_ids', 'line_ids.sale_line_ids', 'line_ids.sale_line_ids.order_id')
    def _compute_instance_id(self):
        for r in self:
            r.instance_id = False
            if r.line_ids.sale_line_ids:
                sale_order = r.line_ids.sale_line_ids[0].order_id
                if sale_order and sale_order.instance_id:
                    r.instance_id = sale_order.instance_id

    @api.depends('line_ids', 'line_ids.sale_line_ids', 'line_ids.sale_line_ids.order_id',
                 'line_ids.sale_line_ids.order_id.saas_order_type',
                 'line_ids.sale_line_ids.order_id.saas_period_start',
                 'line_ids.sale_line_ids.order_id.saas_period_end')
    def _compute_saas_invoice_info(self):
        for r in self:
            orders = r.line_ids.sale_line_ids.order_id.filtered('is_saas_order')
            order = orders[:1]
            r.saas_order_type = order.saas_order_type or False
            r.saas_period_start = order.saas_period_start or False
            r.saas_period_end = order.saas_period_end or False

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
