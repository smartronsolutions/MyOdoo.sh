from odoo import fields, models, api, _
from odoo.exceptions import ValidationError

import logging

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    @api.model
    def _default_based_domain(self):
        based_domain = self.env['saas.based.domain'].sudo().search([], limit=1)
        return based_domain or False

    is_saas_order = fields.Boolean(string='Is SaaS Order?')
    subscription_type = fields.Selection([
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
    ], string='Subscription Type', default='yearly', tracking=True)
    subdomain = fields.Char(string="Sub domain", tracking=True, copy=False)
    based_domain_id = fields.Many2one('saas.based.domain', string="Based Domain", tracking=True, copy=False,
        default=_default_based_domain, groups="s_odoo_saas_master.group_odoo_saas_user")
    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance')
    is_saas_trial = fields.Boolean(related='instance_id.trial', store=True)
    saas_order_type = fields.Selection([
        ('buy_new', 'Buy New'),
        ('renew', 'Renew'),
        ('buy_extra', 'Buy Extra'),
        ('buy_storage', 'Buy Storage'),
        ('buy_workers', 'Upgrade Workers'),
    ], default='buy_new', readonly=True, copy=False)
    buy_now_from_pricing = fields.Boolean(help="Technical field")
    storage_limit_gb = fields.Float(string="Storage Limit (GB)", default=5.0)
    storage_applied = fields.Boolean(string="Storage Applied", default=False, copy=False,
                                     help="Idempotency guard: True once additional storage has been applied to the instance.")
    workers_count = fields.Integer(string="Workers", default=1,
                                   help="Number of additional workers purchased on this order.")
    workers_applied = fields.Boolean(string="Workers Applied", default=False, copy=False,
                                     help="Idempotency guard: True once the extra workers have been applied to the instance.")
    # Chosen on the pricing card / order: the deployed instance picks the matching server.
    odoo_version_id = fields.Many2one('saas.odoo.version', string='Odoo Version', copy=False)
    version_type = fields.Selection([
        ('community', 'Community'),
        ('enterprise', 'Enterprise'),
    ], string='Version Type', default='community', copy=False)


    @api.depends('partner_id', 'company_id', 'is_saas_order')
    def _compute_pricelist_id(self):
        super(SaleOrder, self)._compute_pricelist_id()
        # SaaS orders are billed in the company currency: their unit prices are
        # already converted from the SaaS price currency (XPF), so the partner's
        # foreign-currency pricelist must not be attached to them (it would switch
        # the order currency back to that pricelist currency).
        for order in self.filtered(lambda o: o.is_saas_order and o.state == 'draft'):
            order.pricelist_id = False

    @api.constrains('is_saas_order', 'order_line')
    def _check_saas_order_line(self):
        for r in self:
            if r.is_saas_order and r.order_line:
                if r.saas_order_type not in ('buy_extra', 'buy_plan_extra', 'buy_storage', 'buy_workers') and not any(line.product_id.is_saas_user for line in r.order_line):
                    raise ValidationError(_("Order lines must include SaaS User product"))

    def _get_update_prices_lines(self):
        """ Hook to exclude SaaS subscription lines from generic pricelist recomputations. """
        lines = super()._get_update_prices_lines()
        if self.is_saas_order:
            return self.env['sale.order.line']
        return lines

    def _action_confirm(self):
        res = super(SaleOrder, self)._action_confirm()
        InstanceObj = self.env['saas.odoo.instance']
        for r in self:
            if r.is_saas_order:
                if not r.subscription_type:
                    raise ValidationError(_("You must selection Subscription Type."))
                if not r.instance_id and r.saas_order_type == 'buy_new':
                    instance = r._create_odoo_instance()
                    if instance:
                        if not instance.buy_now_from_pricing:
                            instance.action_deploy()
                        r.instance_id = instance.id
                elif r.instance_id and r.saas_order_type == 'renew':
                    expiration_date = InstanceObj._get_expiration_date(r.subscription_type, expiration_date=r.instance_id.expiration_date)
                    renew_vals = {
                        'expiration_date': expiration_date,
                        'trial': False,
                        'subscription_type': r.subscription_type,
                    }
                    if r.storage_limit_gb and r.storage_limit_gb > (r.instance_id.storage_limit_gb or 0):
                        renew_vals['storage_limit_gb'] = r.storage_limit_gb
                    r.instance_id.write(renew_vals)
                    if r.instance_id.operation_state == 'stop' and r.instance_id._can_auto_resume():
                        try:
                            r.instance_id.action_start()
                        except Exception:
                            pass
        return res

    def _apply_storage_upgrade(self):
        """Idempotently apply the additional storage purchased on this order.

        Safe to call from the payment confirmation page and from the portal refresh
        endpoint: concurrent calls are serialized with a row-level lock and the
        ``storage_applied`` flag guarantees the storage is never credited twice.

        Returns a status dict consumed by the portal loader.
        """
        self.ensure_one()
        status = {
            'success': True,
            'applied': False,
            'already_applied': False,
            'additional_gb': 0.0,
            'storage_limit_gb': 0.0,
            'storage_used_gb': 0.0,
            'storage_percentage': 0.0,
            'resumed': False,
            'instance_id': self.instance_id.id if self.instance_id else False,
        }
        if not self.is_saas_order or self.saas_order_type != 'buy_storage':
            status.update({'success': False, 'error': 'not_a_storage_order'})
            return status
        if not self.instance_id:
            status.update({'success': False, 'error': 'no_instance'})
            return status

        additional_gb = float(self.storage_limit_gb or 0.0)
        if additional_gb <= 0:
            status.update({'success': False, 'error': 'invalid_storage_quantity'})
            return status

        # Make sure any pending ORM change (e.g. a previous call in the same
        # transaction that already set storage_applied) is flushed, otherwise the
        # raw SELECT below could read a stale value and apply the storage twice.
        self.env.flush_all()
        # Serialize concurrent payment callbacks / page refreshes on the same order.
        self.env.cr.execute(
            "SELECT storage_applied FROM sale_order WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        already = bool(row and row[0])

        instance = self.instance_id
        if already:
            status.update({
                'already_applied': True,
                'additional_gb': additional_gb,
                'storage_limit_gb': instance.storage_limit_gb or 0.0,
                'storage_used_gb': instance.storage_used_gb or 0.0,
                'storage_percentage': instance.storage_percentage or 0.0,
            })
            return status

        new_limit = round((instance.storage_limit_gb or 0.0) + additional_gb, 2)
        was_storage_full = instance.suspension_reason == 'storage_full'
        instance.write({'storage_limit_gb': new_limit})
        resumed = instance._resume_after_storage_upgrade() if was_storage_full else False
        self.sudo().write({'storage_applied': True})
        instance._log_history(
            _("Storage upgraded"),
            category='storage',
            level='success',
            icon='fa-hdd-o',
            summary=_("+%.2f GB → %.2f GB") % (additional_gb, new_limit),
            description=_(
                "Purchased %.2f GB of extra storage on order %s. New storage limit: %.2f GB."
            ) % (additional_gb, self.name, new_limit),
            source='system',
        )
        instance.partner_id._saas_notify(
            'storage_upgrade',
            instance=instance,
            title=_("Extra storage added to %s") % instance.name,
            intro=_("Your extra storage purchase is active. The instance keeps running "
                    "without interruption."),
            rows=[('Added', '+{:g} GB'.format(additional_gb)),
                  ('New limit', '{:g} GB'.format(new_limit)),
                  ('Order', self.name or '')],
            cta_label=_("See storage usage"),
        )

        status.update({
            'applied': True,
            'additional_gb': additional_gb,
            'storage_limit_gb': new_limit,
            'storage_used_gb': instance.storage_used_gb or 0.0,
            'storage_percentage': instance.storage_percentage or 0.0,
            'resumed': resumed,
        })
        return status

    def _apply_workers_upgrade(self):
        """Idempotently add the workers purchased on this order to the instance.

        Mirrors :meth:`_apply_storage_upgrade`: a row-level lock + the
        ``workers_applied`` flag guarantee the workers are never credited twice.
        Once applied, the instance is redeployed (odoo.conf + docker-compose) so
        the new worker processes and memory limits take effect.
        """
        self.ensure_one()
        status = {
            'success': True,
            'applied': False,
            'already_applied': False,
            'additional_workers': 0,
            'workers_count': 0,
            'instance_id': self.instance_id.id if self.instance_id else False,
        }
        if not self.is_saas_order or self.saas_order_type != 'buy_workers':
            status.update({'success': False, 'error': 'not_a_workers_order'})
            return status
        if not self.instance_id:
            status.update({'success': False, 'error': 'no_instance'})
            return status

        additional_workers = int(self.workers_count or 0)
        if additional_workers <= 0:
            status.update({'success': False, 'error': 'invalid_workers_quantity'})
            return status

        self.env.flush_all()
        self.env.cr.execute(
            "SELECT workers_applied FROM sale_order WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        if row and row[0]:
            status.update({
                'already_applied': True,
                'additional_workers': additional_workers,
                'workers_count': self.instance_id.workers_count or 0,
            })
            return status

        instance = self.instance_id
        new_count = min(
            max(int(instance.workers_count or 1), 1) + additional_workers,
            instance.MAX_WORKERS_PER_INSTANCE,
        )
        instance.write({'workers_count': new_count})
        self.sudo().write({'workers_applied': True})
        instance._log_history(
            _("Workers upgraded"),
            category='workers',
            level='success',
            icon='fa-server',
            summary=_("+%s → %s workers") % (additional_workers, new_count),
            description=_(
                "Purchased %s extra worker(s) on order %s. The instance now runs %s worker(s)."
            ) % (additional_workers, self.name, new_count),
            source='system',
        )
        # Deploy the new worker count (regenerates odoo.conf + docker-compose and
        # restarts the instance).
        try:
            instance.action_apply_workers()
        except Exception:
            _logger.exception("Could not redeploy instance %s after workers upgrade", instance.name)

        instance.partner_id._saas_notify(
            'workers_upgrade',
            instance=instance,
            title=_("Extra workers added to %s") % instance.name,
            intro=_("Your workers upgrade is active and the instance was restarted with the "
                    "new capacity."),
            rows=[('Added', '+{} worker(s)'.format(additional_workers)),
                  ('Workers now', str(new_count)),
                  ('Memory per worker', '2 GB'),
                  ('Order', self.name or '')],
            cta_label=_("Open your instance"),
        )

        status.update({
            'applied': True,
            'additional_workers': additional_workers,
            'workers_count': new_count,
        })
        return status

    def _finalize_saas_payment(self):
        """Make a SaaS payment final: create+pay the invoice once, and credit the
        purchased storage (idempotent).

        Called from the payment transaction post-processing, the payment validation
        route and the confirmation page, so a successful payment is always applied
        even if the customer never lands on the confirmation page.
        """
        for order in self:
            if not order.is_saas_order or not order.instance_id:
                continue
            try:
                if order.invoice_status == 'to invoice':
                    invoice = order._create_saas_invoice()
                    if invoice:
                        invoice._post()
                        invoice._auto_paid_saas_invoice()
            except Exception:
                _logger.exception("Could not create/pay the SaaS invoice for order %s", order.id)
            if order.saas_order_type == 'buy_storage' and not order.storage_applied:
                order._apply_storage_upgrade()
            if order.saas_order_type == 'buy_workers' and not order.workers_applied:
                order._apply_workers_upgrade()
            if order.saas_order_type == 'buy_new' and order.instance_id.state == 'draft':
                # Brand new instance: deploy it (in the background) so the portal can
                # show the deployment preloader until it is ready.
                order.instance_id._schedule_deployment()
        return True

    def _create_odoo_instance(self):
        if not self.is_saas_order:
            return False

        if not self.subdomain:
            raise ValidationError(_("Cannot find Sub domain to create Odoo instance"))
        if not self.based_domain_id:
            raise ValidationError(_("Cannot find Based domain to create Odoo instance"))

        default_modules = [line.product_id.technical_name for line in self.order_line if not line.product_id.is_saas_user and line.product_id.technical_name]
        
        plan = 'Standard'
        for line in self.order_line:
            code = (line.product_id.default_code or '').lower()
            name = (line.product_id.name or '').lower()
            if 'growth' in code or 'growth' in name:
                plan = 'Growth'
                break
            elif 'standard' in code or 'standard' in name or 'essential' in code or 'essential' in name:
                plan = 'Standard'

        base_storage = 20.0 if plan == 'Growth' else 5.0
        extra_storage = 0.0
        for line in self.order_line:
            code = (line.product_id.default_code or '').lower()
            name = (line.product_id.name or '').lower()
            if code == 'saas_extra_storage' or 'extra storage' in name:
                extra_storage += float(line.product_uom_qty or 0.0)

        total_storage = self.storage_limit_gb if (self.storage_limit_gb and self.storage_limit_gb > 0) else (base_storage + extra_storage)

        # Deployed workers = quantity of paid Workers seats on the order.
        workers_count = sum(
            float(line.product_uom_qty or 0.0)
            for line in self.order_line if line.product_id.is_saas_user
        )
        workers_count = max(int(workers_count), 1)

        data = {
            'sub_domain': self.subdomain,
            'partner': self.partner_id,
            'based_domain': self.based_domain_id,
            'subscription_type': self.subscription_type,
            'default_modules': default_modules,
            'plan': plan,
            'buy_now_from_pricing': self.buy_now_from_pricing,
            'storage_limit_gb': total_storage,
            'storage_gb': total_storage,
            'extra_storage_gb': extra_storage,
            'workers_count': workers_count,
            # Version + edition chosen by the customer; the matching Odoo server (and its
            # physical server / docker images) is resolved automatically.
            'odoo_version_id': self.odoo_version_id.id if self.odoo_version_id else False,
            'version_type': self.version_type or 'community',
        }
        instance_vals = self.env['saas.odoo.instance']._prepare_instance_val_to_create(data)
        instance = self.env['saas.odoo.instance'].sudo().create(instance_vals)        
        return instance

    def _create_saas_invoice(self):
        make_invoice = self.env['sale.advance.payment.inv'].create({
            'advance_payment_method': 'delivered',
            'sale_order_ids': [(6, 0, self.ids)]
        })
        invoice = make_invoice._create_invoices(make_invoice.sale_order_ids)
        for inv in invoice:
            partner = inv.partner_id or self.partner_id
            if partner and inv.amount_total:
                partner._saas_notify(
                    'invoice',
                    instance=self.instance_id or None,
                    title=_("Invoice %s from your hosting portal") % (inv.name or ''),
                    intro=_("A new invoice was issued for your hosting subscription. You can "
                            "pay it from the billing section of your portal."),
                    rows=[('Invoice', inv.name or ''),
                          ('Amount', '{:.2f} {}'.format(
                              inv.amount_total, inv.currency_id.name or '')),
                          ('Due date', str(inv.invoice_date_due or '')),
                          ('Status', inv.state or '')],
                    cta_label=_("View the invoice"),
                )
        return invoice
