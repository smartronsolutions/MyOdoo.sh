from odoo import api, fields, models, _

import logging

_logger = logging.getLogger(__name__)

# A container subscription is billed annually. Upgrades purchased in the middle of
# an active subscription are prorated over the remaining period.
MONTHS_PER_CYCLE = {
    'yearly': 12.0,
    'monthly': 1.0,
}
DAYS_PER_CYCLE = {
    'yearly': 365,
    'monthly': 30,
}


class SaasSubscriptionUpgrade(models.Model):
    """One purchased resource upgrade of a SaaS container.

    A container keeps one base subscription and any number of upgrades (workers
    and/or storage). Every upgrade is linked to the sale order that sold it and to
    the invoice that was issued for it, so the whole history of a container can be
    rebuilt.
    """

    _name = 'saas.subscription.upgrade'
    _description = 'SaaS Subscription Upgrade'
    _order = 'create_date desc, id desc'

    name = fields.Char(string='Reference', compute='_compute_name', store=True)
    instance_id = fields.Many2one(
        'saas.odoo.instance', string='SaaS Container', required=True,
        ondelete='cascade', index=True)
    container_ref = fields.Char(string='Container Reference', related='instance_id.name', store=True)
    partner_id = fields.Many2one('res.partner', string='Customer', related='instance_id.partner_id', store=True)
    company_id = fields.Many2one('res.company', string='Company', related='instance_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', string='Currency', related='company_id.currency_id')
    upgrade_type = fields.Selection(
        [('workers', 'Workers'), ('storage', 'Storage')],
        string='Upgrade Type', required=True, index=True)
    quantity = fields.Float(string='Quantity', required=True, default=1.0)
    uom_label = fields.Char(string='Unit', compute='_compute_name', store=True)
    monthly_unit_price = fields.Monetary(
        string='Monthly Unit Price', currency_field='currency_id',
        help="Recurring monthly price of one unit (worker or GB) before proration.")
    unit_price = fields.Monetary(
        string='Billed Unit Price', currency_field='currency_id',
        help="Unit price actually invoiced, prorated for the remaining period.")
    amount = fields.Monetary(string='Amount', currency_field='currency_id')
    months_billed = fields.Float(string='Months Billed')
    period_start = fields.Date(string='Subscription Period Start')
    period_end = fields.Date(string='Subscription Period End')
    prorated = fields.Boolean(string='Prorated')
    sale_order_id = fields.Many2one('sale.order', string='Upgrade Order', ondelete='set null')
    invoice_id = fields.Many2one('account.move', string='Upgrade Invoice', ondelete='set null')
    state = fields.Selection([
        ('pending', 'Pending Payment'),
        ('applied', 'Applied'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='pending', required=True, index=True)
    applied_date = fields.Date(string='Applied On')
    note = fields.Text(string='Note')
    upgrade_type_label = fields.Char(string='Type', compute='_compute_labels')
    state_label = fields.Char(string='Status Label', compute='_compute_labels')

    _sql_constraints = [
        ('upgrade_order_type_uniq', 'unique(sale_order_id, upgrade_type)',
         'An upgrade order can only be tracked once per resource type.'),
    ]

    @api.depends('upgrade_type', 'state')
    def _compute_labels(self):
        type_map = dict(self._fields['upgrade_type'].selection)
        state_map = dict(self._fields['state'].selection)
        for r in self:
            r.upgrade_type_label = type_map.get(r.upgrade_type, r.upgrade_type)
            r.state_label = state_map.get(r.state, r.state)

    @api.depends('upgrade_type', 'quantity', 'container_ref')
    def _compute_name(self):
        for r in self:
            if r.upgrade_type == 'storage':
                unit = _('GB')
                label = _('Storage')
            else:
                unit = _('worker(s)')
                label = _('Workers')
            qty = int(r.quantity) if float(r.quantity or 0).is_integer() else r.quantity
            r.uom_label = unit
            r.name = '%s %s +%s %s' % (
                r.container_ref or _('Container'), label, qty, unit)


class SaasOdooInstance(models.Model):
    _inherit = 'saas.odoo.instance'

    # ------------------------------------------------------------------
    # Subscription metadata
    # ------------------------------------------------------------------
    base_plan_product_id = fields.Many2one(
        'product.product', string='Base Plan', copy=False,
        help="Plan product this container's base subscription is billed from.")
    base_price_monthly = fields.Float(
        string='Base Subscription Price (Monthly)', default=36.0, copy=False,
        help="Monthly price of the base subscription. Billed annually (× 12).")
    base_workers = fields.Integer(
        string='Included Workers', default=2, required=True, copy=False,
        help="Number of workers included in the base subscription. The Standard plan "
             "includes 2 workers (Odoo does not run reliably on a single worker).")
    base_storage_gb = fields.Float(
        string='Included Storage (GB)', default=5.0, required=True, copy=False,
        help="Storage included in the base subscription.")
    subscription_start_date = fields.Date(string='Subscription Start Date', copy=False)
    last_renewal_date = fields.Date(string='Last Renewal Date', copy=False)

    upgrade_ids = fields.One2many(
        'saas.subscription.upgrade', 'instance_id', string='Upgrade History')
    worker_upgrade_ids = fields.One2many(
        'saas.subscription.upgrade', 'instance_id', string='Worker Upgrades History',
        domain=[('upgrade_type', '=', 'workers')])
    storage_upgrade_ids = fields.One2many(
        'saas.subscription.upgrade', 'instance_id', string='Storage Upgrades History',
        domain=[('upgrade_type', '=', 'storage')])

    upgrade_count = fields.Integer(string='Upgrades', compute='_compute_subscription_info')
    worker_upgrade_count = fields.Integer(string='Worker Upgrade Count', compute='_compute_subscription_info')
    storage_upgrade_count = fields.Integer(string='Storage Upgrade Count', compute='_compute_subscription_info')
    extra_workers_total = fields.Integer(string='Purchased Extra Workers', compute='_compute_subscription_info')
    extra_storage_total = fields.Float(string='Purchased Extra Storage (GB)', compute='_compute_subscription_info')

    subscription_monthly_price = fields.Float(
        string='Subscription Price (Monthly)', compute='_compute_subscription_info')
    subscription_annual_price = fields.Float(
        string='Subscription Price (Annual)', compute='_compute_subscription_info')
    next_renewal_date = fields.Date(
        string='Next Renewal Date', related='expiration_date', readonly=True)
    payment_status = fields.Selection([
        ('no_invoice', 'No Invoice'),
        ('unpaid', 'Unpaid'),
        ('partial', 'Partially Paid'),
        ('paid', 'Paid'),
    ], string='Payment Status', compute='_compute_subscription_info')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('upgrade_ids', 'upgrade_ids.upgrade_type', 'upgrade_ids.quantity',
                 'upgrade_ids.state', 'base_price_monthly', 'expiration_date',
                 'account_move_ids', 'account_move_ids.payment_state',
                 'account_move_ids.state')
    def _compute_subscription_info(self):
        for r in self:
            applied = r.upgrade_ids.filtered(lambda u: u.state == 'applied')
            workers = applied.filtered(lambda u: u.upgrade_type == 'workers')
            storage = applied.filtered(lambda u: u.upgrade_type == 'storage')
            r.upgrade_count = len(r.upgrade_ids)
            r.worker_upgrade_count = len(workers)
            r.storage_upgrade_count = len(storage)
            r.extra_workers_total = int(sum(workers.mapped('quantity')))
            r.extra_storage_total = sum(storage.mapped('quantity'))
            # Full recurring price: the base plan plus every worker / GB above the
            # seats included in the plan (what a renewal actually bills).
            monthly = r._get_base_monthly_price()
            monthly += max(0, int(r.workers_count or 0) - int(r.base_workers or 0)) * r._get_worker_monthly_price()
            monthly += max(0.0, float(r.storage_limit_gb or 0.0) - float(r.base_storage_gb or 0.0)) * r._get_storage_monthly_price_per_gb()
            r.subscription_monthly_price = monthly
            r.subscription_annual_price = round(monthly * 12.0, 2)

            invoices = r.account_move_ids.filtered(
                lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
            if not invoices:
                r.payment_status = 'no_invoice'
            elif all(m.payment_state in ('paid', 'in_payment') for m in invoices):
                r.payment_status = 'paid'
            elif any(m.payment_state in ('paid', 'in_payment', 'partial') for m in invoices):
                r.payment_status = 'partial'
            else:
                r.payment_status = 'unpaid'

    # ------------------------------------------------------------------
    # Base subscription helpers
    # ------------------------------------------------------------------
    def _get_base_plan_product(self):
        """Resolve the plan product backing the container's base subscription."""
        self.ensure_one()
        if self.base_plan_product_id:
            return self.base_plan_product_id
        Product = self.env['product.product'].sudo()
        for order in self.sale_order_ids.filtered(lambda o: o.saas_order_type == 'buy_new'):
            for line in order.order_line:
                product = line.product_id
                if not product or product.is_saas_user:
                    continue
                code = (product.default_code or '').lower()
                if code in ('saas_extra_storage',) or 'storage' in code:
                    continue
                if code in ('standard', 'essential', 'growth') or product.can_be_user_app is False:
                    return product
        product = self.env.ref(
            's_odoo_saas_master.product_saas_plan_essential', raise_if_not_found=False)
        if not product:
            product = Product.search(
                [('default_code', '=ilike', 'Standard'), ('active', '=', True)], limit=1)
        return product or Product.browse()

    def _get_base_monthly_price(self):
        """Monthly price of the base subscription, in the company currency."""
        self.ensure_one()
        if self.base_price_monthly:
            return float(self.base_price_monthly)
        product = self._get_base_plan_product()
        company = self.company_id or self.env.company
        if product:
            return float(company._saas_convert_price(float(product.list_price or 0.0)))
        return 0.0

    # ------------------------------------------------------------------
    # Upgrade pricing (monthly × 12 with proration)
    # ------------------------------------------------------------------
    def _get_subscription_cycle(self):
        self.ensure_one()
        return 'yearly' if (self.subscription_type or 'yearly') == 'yearly' else 'monthly'

    def _get_subscription_period_bounds(self, date=None):
        """Return ``(period_start, period_end, days_left, cycle_days)``."""
        self.ensure_one()
        today = date or fields.Date.context_today(self)
        cycle_days = DAYS_PER_CYCLE[self._get_subscription_cycle()]
        end = self.expiration_date
        days_left = (end - today).days if (end and end > today) else 0
        return today, end, days_left, cycle_days

    def _get_upgrade_billing_months(self, date=None):
        """Fraction of the subscription cycle still to be served, in months.

        A full annual period yields ``12`` and a mid-term upgrade yields the
        prorated number of months left. When the subscription has no remaining
        period (expired / not set) the full cycle is charged.
        """
        self.ensure_one()
        cycle = self._get_subscription_cycle()
        cycle_months = MONTHS_PER_CYCLE[cycle]
        _, _, days_left, cycle_days = self._get_subscription_period_bounds(date)
        if days_left <= 0:
            return cycle_months
        return min(cycle_months, cycle_months * days_left / float(cycle_days))

    def _get_worker_monthly_price(self):
        """Recurring monthly price of one worker, in the company currency."""
        self.ensure_one()
        company = self.company_id or self.env.company
        configured = company.saas_worker_price_monthly if 'saas_worker_price_monthly' in company._fields else 0.0
        if configured:
            return float(configured)
        product = self.env['product.product'].sudo()._get_saas_worker_product()
        return company._saas_convert_price(float(product.list_price or 0.0)) if product else 0.0

    def _get_storage_product(self):
        Product = self.env['product.product'].sudo()
        product = self.env.ref(
            's_odoo_saas_master.product_saas_extra_storage', raise_if_not_found=False)
        if not product:
            product = Product.search(
                [('default_code', '=ilike', 'saas_extra_storage'), ('active', '=', True)], limit=1)
        return product or Product.browse()

    def _get_storage_monthly_price_per_gb(self):
        """Recurring monthly price of one extra GB, in the company currency."""
        self.ensure_one()
        company = self.company_id or self.env.company
        configured = company.saas_storage_price_per_gb if 'saas_storage_price_per_gb' in company._fields else 0.0
        if configured:
            return float(configured)
        product = self._get_storage_product()
        return company._saas_convert_price(float(product.list_price or 0.0)) if product else 0.0

    def _get_upgrade_pricing(self, additional_workers=0, additional_gb=0, date=None,
                             prorate=True, storage_months=None):
        """Complete pricing breakdown of a worker/storage upgrade.

        Annual upgrades are billed ``monthly price × 12``. When ``prorate`` is set
        and the subscription is still active, only the remaining months are billed.

        ``storage_months`` overrides the billed months of the **storage** part only.
        Extra storage is a monthly rate per GB (0.10 €/GB/month), so the portal sells
        it as a plain multiplication — 10 GB = 1 € — instead of mixing it with the
        remaining days of the subscription.
        """
        self.ensure_one()
        cycle = self._get_subscription_cycle()
        cycle_months = MONTHS_PER_CYCLE[cycle]
        months = self._get_upgrade_billing_months(date) if prorate else cycle_months
        worker_monthly = self._get_worker_monthly_price()
        storage_monthly = self._get_storage_monthly_price_per_gb()
        worker_unit = round(worker_monthly * months, 2)
        storage_unit = round(
            storage_monthly * (months if storage_months is None else float(storage_months)), 2)
        start, end, days_left, _cycle_days = self._get_subscription_period_bounds(date)
        currency = self.company_id.currency_id or self.env.company.currency_id
        return {
            'subscription_type': cycle,
            'cycle_months': cycle_months,
            'months': months,
            'prorated': months < cycle_months - 1e-6,
            'period_start': start,
            'period_end': end,
            'days_left': days_left,
            'currency_id': currency.id,
            'currency_symbol': currency.symbol or '',
            'additional_workers': int(additional_workers or 0),
            'additional_gb': float(additional_gb or 0.0),
            'worker_monthly_price': worker_monthly,
            'worker_unit_price': worker_unit,
            'worker_amount': round(worker_unit * int(additional_workers or 0), 2),
            'storage_monthly_price': storage_monthly,
            'storage_unit_price': storage_unit,
            'storage_amount': round(storage_unit * float(additional_gb or 0.0), 2),
        }

    # ------------------------------------------------------------------
    # Upgrade history
    # ------------------------------------------------------------------
    def _create_upgrade_history(self, upgrade_type, quantity, monthly_unit_price,
                                unit_price, period_start, period_end, months,
                                prorated, sale_order=False, invoice=False, note=False):
        """Create (or update) the history row of one purchased upgrade.

        Idempotent per ``(sale order, upgrade type)`` so a retried payment callback
        never records the same upgrade twice.
        """
        self.ensure_one()
        if not quantity:
            return self.env['saas.subscription.upgrade']
        Upgrade = self.env['saas.subscription.upgrade'].sudo()
        existing = Upgrade
        if sale_order:
            existing = Upgrade.search([
                ('sale_order_id', '=', sale_order.id),
                ('upgrade_type', '=', upgrade_type),
            ], limit=1)
        amount = round(unit_price * quantity, 2)
        vals = {
            'instance_id': self.id,
            'upgrade_type': upgrade_type,
            'quantity': quantity,
            'monthly_unit_price': monthly_unit_price,
            'unit_price': unit_price,
            'amount': amount,
            'months_billed': months,
            'period_start': period_start,
            'period_end': period_end,
            'prorated': prorated,
            'sale_order_id': sale_order.id if sale_order else False,
            'invoice_id': invoice.id if invoice else False,
            'state': 'applied',
            'applied_date': fields.Date.context_today(self),
            'note': note,
        }
        if existing:
            existing.write(vals)
            return existing
        return Upgrade.create(vals)

    # ------------------------------------------------------------------
    # Renewal (base subscription + active upgrades)
    # ------------------------------------------------------------------
    def _prepare_renewal_order_vals(self, date=None):
        """Build the next annual renewal order: base plan + all active upgrades.

        The base plan is billed for one full annual period and every active worker /
        storage upgrade is re-billed for the new period, so the renewal invoice is
        the single invoice that keeps the container alive for another year.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        currency = company.currency_id
        order_lines = []

        plan_product = self._get_base_plan_product()
        if plan_product:
            plan_price = round(self._get_base_monthly_price() * 12.0, 2)
            order_lines.append((0, 0, {
                'product_id': plan_product.id,
                'name': _('%s — Annual subscription %s (%s → %s)') % (
                    plan_product.name, self.name or '',
                    date or fields.Date.context_today(self),
                    fields.Date.add(date or fields.Date.context_today(self), months=12)),
                'product_uom_qty': 1,
                'product_uom': plan_product.uom_id.id,
                'price_unit': plan_price,
                'tax_id': [(6, 0, plan_product.taxes_id.ids)],
            }))

        extra_workers = max(0, int(self.workers_count or self.base_workers or 2) - int(self.base_workers or 2))
        if extra_workers:
            worker_product = self.env['product.product'].sudo()._get_saas_worker_product()
            worker_price = round(self._get_worker_monthly_price() * 12.0, 2)
            order_lines.append((0, 0, {
                'product_id': worker_product.id,
                'name': _('%s (+%s workers) — Annual renewal %s') % (
                    worker_product.name, extra_workers, self.name or ''),
                'product_uom_qty': extra_workers,
                'product_uom': worker_product.uom_id.id,
                'price_unit': worker_price,
                'tax_id': [(6, 0, worker_product.taxes_id.ids)],
            }))

        extra_storage = max(0.0, float(self.storage_limit_gb or 0.0) - float(self.base_storage_gb or 0.0))
        if extra_storage:
            storage_product = self._get_storage_product()
            storage_price = round(self._get_storage_monthly_price_per_gb() * 12.0, 2)
            order_lines.append((0, 0, {
                'product_id': storage_product.id,
                'name': _('%s (+%g GB) — Annual renewal %s') % (
                    storage_product.name, extra_storage, self.name or ''),
                'product_uom_qty': extra_storage,
                'product_uom': storage_product.uom_id.id,
                'price_unit': storage_price,
                'tax_id': [(6, 0, storage_product.taxes_id.ids)],
            }))

        if not order_lines:
            return {}
        return {
            'partner_id': self.partner_id.id,
            'company_id': company.id,
            'currency_id': currency.id,
            'is_saas_order': True,
            'saas_order_type': 'renew',
            'subscription_type': self._get_subscription_cycle(),
            'instance_id': self.id,
            'subdomain': self.name,
            'based_domain_id': self.based_domain_id.id if self.based_domain_id else False,
            'odoo_version_id': self.odoo_version_id.id if self.odoo_version_id else False,
            'order_line': order_lines,
        }

    def _create_renewal_order(self, date=None):
        """Create the annual renewal order of this container."""
        self.ensure_one()
        vals = self._prepare_renewal_order_vals(date=date)
        if not vals:
            return self.env['sale.order']
        return self.env['sale.order'].sudo().create(vals)

    def action_create_renewal_order(self):
        order = self._create_renewal_order()
        if not order:
            return False
        return {
            'name': _('Renewal Quotation'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'view_mode': 'form',
            'res_id': order.id,
        }

    # ------------------------------------------------------------------
    # Backend navigation
    # ------------------------------------------------------------------
    def action_view_upgrades(self):
        self.ensure_one()
        return {
            'name': _('Upgrades — %s') % (self.name or ''),
            'type': 'ir.actions.act_window',
            'res_model': 'saas.subscription.upgrade',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id},
        }

    def action_view_upgrade_invoices(self):
        self.ensure_one()
        return {
            'name': _('Upgrade Invoices — %s') % (self.name or ''),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id),
                       ('saas_order_type', 'in', ('buy_storage', 'buy_workers', 'buy_upgrade'))],
            'context': {'default_instance_id': self.id},
        }
