from collections import OrderedDict
from datetime import datetime, date
import logging
import werkzeug.exceptions

from odoo import http, fields, _
from odoo.osv import expression
from odoo.exceptions import MissingError, UserError, ValidationError
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.http import request, content_disposition

from ..models.saas_portal_team import SAAS_DEVELOPER_ALLOWED_PREFIXES

_logger = logging.getLogger(__name__)


class PortalInstance(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'instance_count' in counters:
            values['instance_count'] = request.env.user.partner_id.instance_count
        return values

    def _get_instance_searchbar_sortings(self):
        return {
            'date': {'label': _('Expiration Date'), 'order': 'expiration_date desc'},
            'state': {'label': _('Status'), 'order': 'state'},
        }

    def _get_instance_searchbar_filters(self):
        return {
            'all': {'label': _('All'), 'domain': []},
            'deploy': {'label': _('Deployed'), 'domain': [('state', '=', 'deploy')]},
            'suspend': {'label': _('Suspended'), 'domain': [('state', '=', 'suspend')]},
            'cancel': {'label': _('Cancelled'), 'domain': [('state', '=', 'cancel')]},
        }

    def _get_instances_domain(self):
        return [('partner_id', '=', request.env.user.partner_id.id)]

    def _browse_instance(self, instance_id):
        """Browse an instance from an id that may arrive as a string.

        JSON-RPC payloads and HTML ``data-*`` attributes send ids as strings; a recordset
        built from them does not compare equal to one built from integers.
        """
        try:
            instance_id = int(instance_id)
        except (TypeError, ValueError):
            raise werkzeug.exceptions.NotFound()
        return request.env['saas.odoo.instance'].sudo().browse(instance_id)

    def _saas_block_team_developer(self):
        """Redirect a developer away from the owner-only portal pages.

        Team members get their instances, logs, shell and GitHub - nothing else, so
        billing, plans and the account settings are refused server side (not merely
        hidden in the browser).
        """
        if request.env.user.sudo().saas_team_role == 'developer':
            return request.redirect('/my/saas/odoo-instances')
        return None

    def _validate_instance(self, instance):
        user = request.env.user
        if not instance or not instance.exists():
            raise werkzeug.exceptions.NotFound()
        if not user._is_admin() and instance.partner_id.id != user.partner_id.id:
            raise werkzeug.exceptions.Forbidden(_("Access Denied"))
        # Team members only get the instances that were assigned to them.
        if not user._is_admin() and not user._saas_can_open_instance(instance):
            raise werkzeug.exceptions.Forbidden(_("Access Denied"))
        # ...and only the tools that were agreed: logs, shell and GitHub.
        if not user._is_admin() and user.sudo().saas_team_role == 'developer':
            path = request.httprequest.path or ''
            if path.startswith('/saas/') and not any(
                    path.startswith(prefix)
                    for prefix in SAAS_DEVELOPER_ALLOWED_PREFIXES):
                _logger.warning("Blocked %s for developer %s", path, user.login)
                raise werkzeug.exceptions.Forbidden(_("Access Denied"))

    # -------------------------------------------------------------------------
    # My Account Portal Override
    # -------------------------------------------------------------------------
    @http.route(['/my', '/my/home'], type='http', auth="user", website=True)
    def home(self, **kw):
        # A team member only gets the instances they were given, so the full customer
        # dashboard (billing, plans, settings) is skipped entirely.
        if request.env.user.sudo().saas_team_role == 'developer':
            return request.redirect('/my/saas/odoo-instances')
        partner = request.env.user.partner_id
        instances = request.env['saas.odoo.instance'].sudo().search(
            request.env.user._saas_instance_domain(), order='id desc')

        total_instances = len(instances)
        online_instances = len(instances.filtered(lambda i: i.operation_state == 'run' and i.state == 'deploy'))
        stopped_instances = len(instances.filtered(lambda i: i.operation_state == 'stop' or i.state == 'suspend'))
        total_users = sum(instances.mapped('active_user'))
        total_workers = sum(instances.mapped('workers_count'))
        online_workers = sum(
            instances.filtered(lambda i: i.operation_state == 'run' and i.state == 'deploy').mapped('workers_count')
        )

        recent_backups = request.env['saas.odoo.instance.backup'].sudo().search([
            ('instance_id', 'in', instances.ids)
        ], order='datetime desc', limit=5)

        recent_messages = request.env['mail.message'].sudo().search([
            ('model', '=', 'saas.odoo.instance'),
            ('res_id', 'in', instances.ids),
        ], order='date desc', limit=6)

        earliest_expiry = False
        active_instances = instances.filtered(lambda i: i.expiration_date and i.state in ('deploy', 'suspend'))
        if active_instances:
            dates = active_instances.mapped('expiration_date')
            earliest_expiry = min(dates)
        
        days_left = None
        if earliest_expiry:
            days_left = (earliest_expiry - date.today()).days

        total_storage_used = round(sum(instances.mapped('storage_used_gb')) if 'storage_used_gb' in instances._fields else 0.0, 1)
        total_storage_limit = round(sum(instances.mapped('storage_limit_gb')) if 'storage_limit_gb' in instances._fields else (total_instances * 5.0), 1)

        values = {
            'page_name': 'saas_dashboard',
            'partner': partner,
            'instances': instances,
            'total_instances': total_instances,
            'online_instances': online_instances,
            'stopped_instances': stopped_instances,
            'total_users': total_users,
            'total_workers': total_workers,
            'online_workers': online_workers,
            'total_storage_used': total_storage_used,
            'total_storage_limit': total_storage_limit,
            'earliest_expiry': earliest_expiry,
            'days_left': days_left,
            'recent_backups': recent_backups,
            'recent_messages': recent_messages,
            'today': date.today(),
        }
        return request.render("s_odoo_saas_master.portal_customer_dashboard", values)

    # -------------------------------------------------------------------------
    # Billing (instance dashboard tab + account billing page)
    # -------------------------------------------------------------------------
    ORDER_STATE_LABELS = {
        'draft': 'Draft', 'sent': 'Quotation', 'sale': 'Confirmed',
        'done': 'Completed', 'cancel': 'Cancelled',
    }
    INVOICE_STATE_LABELS = {'draft': 'Draft', 'posted': 'Posted', 'cancel': 'Cancelled'}
    PAYMENT_LABELS = {
        'not_paid': 'Unpaid', 'partial': 'Partially paid', 'paid': 'Paid',
        'in_payment': 'In payment', 'reversed': 'Reversed', 'blocked': 'Blocked',
    }
    PAID_PAYMENT_STATES = ('paid', 'in_payment')

    @staticmethod
    def _billing_amount(amount):
        return '{:,.0f}'.format(round(amount or 0.0)).replace(',', '\u202f')

    def _billing_orders(self, partner, limit=50):
        return request.env['sale.order'].sudo().search([
            ('id', 'in', request.env.user._saas_visible_instance_ids()),
            ('is_saas_order', '=', True),
        ], order='date_order desc, id desc', limit=limit)

    def _billing_invoices(self, partner, limit=50):
        return request.env['account.move'].sudo().search([
            ('id', 'in', request.env.user._saas_visible_instance_ids()),
            ('move_type', '=', 'out_invoice'),
        ], order='invoice_date desc, id desc', limit=limit)

    def _billing_order_payment(self, order):
        """Payment badge for a sale order, derived from its payment transactions."""
        if order.state == 'cancel':
            return 'Cancelled', 'offline'
        if any(tx.state == 'done' for tx in order.transaction_ids):
            return 'Paid', 'ok'
        if order.state in ('draft', 'sent'):
            return 'Not paid', 'warning'
        return 'Unpaid', 'danger'

    def _billing_order_rows(self, orders):
        rows = []
        for order in orders:
            items = ', '.join(
                '%s × %s' % (int(line.product_uom_qty or 0), line.product_id.display_name)
                for line in order.order_line if line.product_id
            )
            payment_label, payment_class = self._billing_order_payment(order)
            rows.append({
                'id': order.id,
                'name': order.name,
                'date': order.date_order.strftime('%d %b %Y') if order.date_order else '',
                'items': items or order.name,
                'amount': self._billing_amount(order.amount_total),
                'currency': order.currency_id.symbol or '',
                'state': order.state,
                'state_label': self.ORDER_STATE_LABELS.get(order.state, order.state),
                'is_open': order.state in ('draft', 'sent'),
                'payment_label': payment_label,
                'payment_class': payment_class,
                'instance': order.instance_id.name or '',
                'instance_id': order.instance_id.id or 0,
                'url': '/my/orders/%s' % order.id,
            })
        return rows

    def _billing_invoice_rows(self, invoices):
        rows = []
        paid_total = 0.0
        due_total = 0.0
        for invoice in invoices:
            residual = invoice.amount_residual or 0.0
            is_paid = invoice.payment_state in self.PAID_PAYMENT_STATES
            if invoice.state == 'posted':
                if is_paid:
                    paid_total += invoice.amount_total or 0.0
                else:
                    due_total += residual
            rows.append({
                'id': invoice.id,
                'name': invoice.name,
                'date': invoice.invoice_date.strftime('%d %b %Y') if invoice.invoice_date else '',
                'due': invoice.invoice_date_due.strftime('%d %b %Y') if invoice.invoice_date_due else '',
                'origin': invoice.invoice_origin or '',
                'amount': self._billing_amount(invoice.amount_total),
                'currency': invoice.currency_id.symbol or '',
                'residual': self._billing_amount(residual),
                'state_label': self.INVOICE_STATE_LABELS.get(invoice.state, invoice.state),
                'payment_label': self.PAYMENT_LABELS.get(invoice.payment_state or '', 'Unpaid'),
                'is_paid': is_paid,
                'is_overdue': (not is_paid and invoice.state == 'posted'
                               and invoice.invoice_date_due and invoice.invoice_date_due < fields.Date.context_today(invoice)),
                'pdf_url': '/my/invoices/%s?report_type=pdf&download=true' % invoice.id,
                'url': '/my/invoices/%s' % invoice.id,
            })
        return rows, paid_total, due_total

    def _billing_plan_label(self, orders):
        for order in orders:
            for line in order.order_line:
                code = (line.product_id.default_code or '').lower()
                name = (line.product_id.name or '').lower()
                if 'growth' in code or 'growth' in name:
                    return 'Growth'
        return 'Standard'

    def _billing_client_values(self, partner):
        return {
            'name': partner.name or '',
            'company': partner.parent_name if partner.parent_id else '',
            'email': partner.email or '',
            'phone': partner.phone or partner.mobile or '',
            'vat': partner.vat or '',
            'address': ', '.join(p for p in [
                partner.street, partner.street2,
                ' '.join(p for p in [partner.zip, partner.city] if p),
                partner.state_id.name, partner.country_id.name,
            ] if p),
            'customer_since': partner.create_date.strftime('%d %b %Y') if partner.create_date else '',
            'lang': (partner.lang or 'en_US').split('_')[0].upper(),
        }

    def _billing_subscription_values(self, instance, orders):
        expiry = instance.expiration_date
        days_left = (expiry - fields.Date.context_today(instance)).days if expiry else False
        return {
            'plan': self._billing_plan_label(orders),
            'version': '%s · %s' % (
                instance.odoo_version_id.name or '',
                dict(instance._fields['version_type'].selection).get(instance.version_type, ''),
            ),
            'workers': instance.workers_count or 1,
            'storage': self._billing_amount(instance.storage_limit_gb or 0.0),
            'cycle': 'Yearly' if instance.subscription_type == 'yearly' else 'Monthly',
            'expiry': expiry.strftime('%d %b %Y') if expiry else '—',
            'days_left': days_left,
            'status': 'Trial' if instance.trial else (
                'Active' if instance.state == 'deploy' else instance.state.title()
            ),
            'is_trial': bool(instance.trial),
        }

    def _get_instance_billing_values(self, instance):
        """Everything the customer needs to know about money for this instance.

        Returns the client card, the subscription summary, every SaaS purchase (sale
        orders) and every invoice, plus the running totals.
        """
        partner = instance.partner_id
        currency = instance.company_id.currency_id or request.env.company.currency_id

        orders = self._billing_orders(partner)
        invoices = self._billing_invoices(partner)

        billing_orders = self._billing_order_rows(orders)
        billing_invoices, paid_total, due_total = self._billing_invoice_rows(invoices)

        # What the customer actually bought, grouped by kind.
        bought = {'plan': self._billing_plan_label(orders), 'workers': 0, 'storage': 0.0}
        for order in orders:
            if order.state not in ('sale', 'done'):
                continue
            if order.saas_order_type == 'buy_workers':
                bought['workers'] += int(order.workers_count or 0)
            elif order.saas_order_type == 'buy_storage' and order.storage_limit_gb:
                bought['storage'] += float(order.storage_limit_gb)
        bought['workers'] = max(bought['workers'], instance.workers_count or 1)
        bought['storage'] = bought['storage'] or float(instance.storage_limit_gb or 0.0)

        return {
            'billing_client': self._billing_client_values(partner),
            'billing_subscription': self._billing_subscription_values(instance, orders),
            'billing_orders': billing_orders,
            'billing_invoices': billing_invoices,
            'billing_bought': bought,
            'billing_totals': {
                'paid': self._billing_amount(paid_total),
                'due': self._billing_amount(due_total),
                'orders': len(billing_orders),
                'invoices': len(billing_invoices),
                'currency': currency.symbol or '',
            },
            'billing_has_due': due_total > 0.005,
        }

    def _get_customer_billing_values(self, filterby='all'):
        """Account-wide billing data for the dedicated Billing page.

        Aggregates every SaaS purchase and every invoice of the customer, whatever the
        instance they belong to, and computes the paid / unpaid totals. ``filterby``
        only narrows the displayed rows; the totals always cover the whole account.
        """
        partner = request.env.user.partner_id
        # No limit: the Billing page shows the whole history inside scrollable
        # boxes (~10 rows visible at a time, the rest reached by scrolling).
        orders = self._billing_orders(partner, limit=None)
        invoices = self._billing_invoices(partner, limit=None)

        all_order_rows = self._billing_order_rows(orders)
        all_invoice_rows, paid_total, due_total = self._billing_invoice_rows(invoices)

        order_rows = all_order_rows
        invoice_rows = all_invoice_rows
        if filterby == 'paid':
            order_rows = [r for r in all_order_rows if r['payment_label'] == 'Paid']
            invoice_rows = [r for r in all_invoice_rows if r['is_paid']]
        elif filterby == 'unpaid':
            order_rows = [r for r in all_order_rows if r['payment_label'] != 'Paid']
            invoice_rows = [r for r in all_invoice_rows if not r['is_paid']]

        instances = request.env['saas.odoo.instance'].sudo().search(
            request.env.user._saas_instance_domain(), order='id desc')
        current = instances.filtered(lambda i: i.state == 'deploy')[:1] or instances[:1]
        if current:
            subscription = self._billing_subscription_values(current, orders)
        else:
            subscription = {
                'plan': 'No active plan', 'version': '—', 'workers': 0, 'storage': '0',
                'cycle': '—', 'expiry': '—', 'days_left': False,
                'status': 'No instance', 'is_trial': False,
            }

        return {
            'billing_client': self._billing_client_values(partner),
            'billing_subscription': subscription,
            'billing_orders': order_rows,
            'billing_invoices': invoice_rows,
            'billing_instances_count': len(instances),
            'billing_filterby': filterby,
            'billing_counts': {
                'all': len(all_invoice_rows),
                'paid': len([r for r in all_invoice_rows if r['is_paid']]),
                'unpaid': len([r for r in all_invoice_rows if not r['is_paid']]),
            },
            'billing_totals': {
                'paid': self._billing_amount(paid_total),
                'due': self._billing_amount(due_total),
                'invoiced': self._billing_amount(paid_total + due_total),
                'orders': len(all_order_rows),
                'invoices': len(all_invoice_rows),
                'currency': request.env.company.currency_id.symbol or '',
            },
            'billing_has_due': due_total > 0.005,
        }

    @http.route(['/my/saas/billing'], type='http', auth="user", website=True)
    def portal_saas_billing(self, filterby=None, **kw):
        blocked = self._saas_block_team_developer()
        if blocked is not None:
            return blocked
        if filterby not in ('all', 'paid', 'unpaid'):
            filterby = 'all'
        values = self._prepare_portal_layout_values()
        values.update(self._get_customer_billing_values(filterby=filterby))
        values.update({
            'page_name': 'saas_billing',
            'partner': request.env.user.partner_id,
            'today': date.today(),
        })
        return request.render("s_odoo_saas_master.portal_billing_page", values)

    @http.route(['/my/saas/odoo-instances'], type='http', auth="user", website=True)
    def portal_my_instances(self, sortby=None, filterby=None, **kw):
        values = self._prepare_my_instances_values(sortby, filterby)
        return request.render("s_odoo_saas_master.portal_my_instances", values)

    @http.route(['/my/saas/odoo-instance/<int:instance_id>'], type='http', auth="user", website=True)
    def portal_my_instance_detail(self, instance_id, access_token=None, **kw):
        values = self._instance_get_page_view_values(instance_id, access_token, **kw)
        return request.render("s_odoo_saas_master.portal_instance_page", values)

    @http.route(['/my/saas/settings'], type='http', auth="user", website=True)
    def portal_saas_settings(self, **kw):
        # Deliberately NOT blocked for developers: they keep their own security tab.
        partner = request.env.user.partner_id
        instances = request.env['saas.odoo.instance'].sudo().search(
            request.env.user._saas_instance_domain(), order='id desc')
        has_github_oauth = bool(
            request.env.company.github_client_id
            or request.env['ir.config_parameter'].sudo().get_param('saas.github_client_id')
        )
        values = {
            'page_name': 'saas_settings',
            'partner': partner,
            'instances': instances,
            'user': request.env.user,
            'has_github_oauth': has_github_oauth,
            'user_github_connected': bool(partner.github_oauth_token),
            'github_login': partner.github_login or '',
            # General pane: real choices instead of the hardcoded lists it used to show.
            'timezones': request.env['saas.odoo.instance'].saas_available_timezones(),
            'user_timezone': request.env.user.tz or 'UTC',
            'countries': request.env['res.country'].sudo().search([], order='name'),
            # Team Members pane: who is on the account and with which role.
            'team_members': request.env.user.sudo()._saas_team_members(),
            'team_role': request.env.user.sudo().saas_team_role or 'owner',
            'team_roles': [('developer', _("Developer")), ('admin', _("Administrator"))],
        }
        return request.render("s_odoo_saas_master.portal_settings_page", values)

    @http.route(['/saas/pricing', '/my/saas/pricing'], type='http', auth="public", website=True)
    def portal_saas_pricing(self, **kw):
        blocked = self._saas_block_team_developer()
        if blocked is not None:
            return blocked
        from .pricing import Pricing
        return Pricing().pricing(**kw)


    def _prepare_my_instances_values(self, sortby, filterby, domain=None, url="/my/saas/odoo-instances"):
        values = self._prepare_portal_layout_values()
        domain = expression.AND([
            domain or [],
            self._get_instances_domain(),
        ])

        searchbar_sortings = self._get_instance_searchbar_sortings()
        if not sortby:
            sortby = 'date'
        order = searchbar_sortings[sortby]['order']

        searchbar_filters = self._get_instance_searchbar_filters()
        if not filterby:
            filterby = 'all'
        domain += searchbar_filters[filterby]['domain']

        instances = request.env['saas.odoo.instance'].sudo().search(domain, order=order)

        partner = request.env.user.partner_id
        values.update({
            'instances': instances,
            'page_name': 'instance',
            'default_url': url,
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
            'searchbar_filters': OrderedDict(sorted(searchbar_filters.items())),
            'filterby': filterby,
            'today': date.today(),
            'partner': partner,
            'user_github_connected': bool(partner.github_oauth_token),
            'github_login': partner.github_login or '',
        })

        return values

    def _instance_get_page_view_values(self, instance_id, access_token, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        managing_ip = ''
        if instance.pserver_id:
            try:
                managing_ip = instance.pserver_id._get_managing_ip()
            except Exception:
                managing_ip = '127.0.0.1'

        days_left = None
        if instance.expiration_date:
            days_left = (instance.expiration_date - date.today()).days

        recent_messages = request.env['mail.message'].sudo().search([
            ('model', '=', 'saas.odoo.instance'),
            ('res_id', '=', instance.id),
        ], order='date desc', limit=8)

        partner = request.env.user.partner_id
        has_github_oauth = bool(
            request.env.company.github_client_id
            or request.env['ir.config_parameter'].sudo().get_param('saas.github_client_id')
        )

        # Pricing values for Renew Modal
        from .pricing import Pricing
        pricing_ctrl = Pricing()
        pricelist_context, pricelist, _ = pricing_ctrl._get_pricelist_context()
        essential_prod = pricing_ctrl._get_plan_product('Standard')
        growth_prod = pricing_ctrl._get_plan_product('Growth')
        user_prod = pricing_ctrl._get_user_product()
        extra_storage_prod = pricing_ctrl._get_extra_storage_product()
        
        ess_monthly = pricing_ctrl._convert_price(float(essential_prod.list_price)) if (essential_prod and essential_prod.list_price) else pricing_ctrl._convert_price(14900.0)
        gro_monthly = pricing_ctrl._convert_price(float(growth_prod.list_price)) if (growth_prod and growth_prod.list_price) else pricing_ctrl._convert_price(39900.0)
        user_monthly = pricing_ctrl._convert_price(float(user_prod.list_price)) if (user_prod and user_prod.list_price) else pricing_ctrl._convert_price(100.0)
        storage_monthly = pricing_ctrl._get_storage_price_per_gb(pricelist)
        
        currency_sym = pricing_ctrl._get_display_currency().symbol or ''

        # Fallback for a paid storage order that was never applied because the customer
        # did not land on the confirmation page: the instance page then plays the
        # storage-extension animation and applies the upgrade (idempotent).
        pending_storage_order = request.env['sale.order'].sudo().search([
            ('instance_id', '=', instance.id),
            ('is_saas_order', '=', True),
            ('saas_order_type', '=', 'buy_storage'),
            ('storage_applied', '=', False),
        ], order='id desc', limit=1)
        if pending_storage_order and not any(
            tx.state == 'done' for tx in pending_storage_order.transaction_ids
        ):
            pending_storage_order = request.env['sale.order']

        # Same fallback for a paid workers upgrade the customer never confirmed.
        pending_workers_order = request.env['sale.order'].sudo().search([
            ('instance_id', '=', instance.id),
            ('is_saas_order', '=', True),
            ('saas_order_type', '=', 'buy_workers'),
            ('workers_applied', '=', False),
        ], order='id desc', limit=1)
        if pending_workers_order and not any(
            tx.state == 'done' for tx in pending_workers_order.transaction_ids
        ):
            pending_workers_order = request.env['sale.order']

        # A paid "buy new" instance that was never deployed (e.g. paid before the
        # automatic deployment existed): show the deployment preloader and let the
        # status endpoint queue the deployment.
        needs_deployment = False
        if instance.state == 'draft' and instance.deployment_state == 'idle':
            deploy_order = request.env['sale.order'].sudo().search([
                ('instance_id', '=', instance.id),
                ('is_saas_order', '=', True),
                ('saas_order_type', '=', 'buy_new'),
                ('state', 'in', ('sale', 'done')),
            ], order='id desc', limit=1)
            if deploy_order and any(tx.state == 'done' for tx in deploy_order.transaction_ids):
                needs_deployment = True

        values = {
            'page_name': 'instance_detail',
            'instance': instance,
            'installed_apps': instance.installed_app_ids,
            'managing_ip': managing_ip,
            'days_left': days_left,
            'recent_messages': recent_messages,
            'history_lines': instance.history_ids[:200],
            'today': date.today(),
            'partner': partner,
            'has_github_oauth': has_github_oauth,
            'user_github_connected': bool(partner.github_oauth_token),
            'github_login': partner.github_login or '',
            'essential_product': essential_prod,
            'growth_product': growth_prod,
            'essential_monthly_price': ess_monthly,
            'essential_annual_price': round(ess_monthly * 12 * 0.85, 2),
            'growth_monthly_price': gro_monthly,
            'growth_annual_price': round(gro_monthly * 12 * 0.85, 2),
            'user_monthly_price': user_monthly,
            # Workers and extra storage have ONE price that does not depend on the billing
            # cycle: the annual figure is the very same amount (no x12, no discount).
            'user_annual_price': user_monthly,
            'extra_storage_monthly_price': storage_monthly,
            'extra_storage_annual_price': storage_monthly,
            'essential_monthly_price_formatted': pricing_ctrl._format_price(ess_monthly),
            'essential_annual_price_formatted': pricing_ctrl._format_price(round(ess_monthly * 12 * 0.85, 2)),
            'growth_monthly_price_formatted': pricing_ctrl._format_price(gro_monthly),
            'growth_annual_price_formatted': pricing_ctrl._format_price(round(gro_monthly * 12 * 0.85, 2)),
            'user_monthly_price_formatted': pricing_ctrl._format_price(user_monthly),
            'user_annual_price_formatted': pricing_ctrl._format_price(user_monthly),
            'extra_storage_monthly_price_formatted': pricing_ctrl._format_price(storage_monthly),
            'extra_storage_annual_price_formatted': pricing_ctrl._format_price(storage_monthly),
            'currency_symbol': currency_sym,
            'workers_count': instance.workers_count or 1,
            'max_workers': instance.MAX_WORKERS_PER_INSTANCE,
            'pending_workers_order_id': int(kwargs.get('order_id') or 0)
                or (pending_workers_order.id if pending_workers_order else 0),
            'worker_monthly_price': user_monthly,
            'worker_monthly_price_formatted': pricing_ctrl._format_price(user_monthly),
            'is_workers_upgraded': bool(kwargs.get('workers_upgraded')) or bool(pending_workers_order),
            'workers_upgraded_count': int(kwargs.get('workers') or 0) or (
                int(pending_workers_order.workers_count or 0) if pending_workers_order else 0
            ),
            'is_renewed': bool(kwargs.get('renewed') or kwargs.get('payment_success')),
            'is_storage_extended': bool(kwargs.get('storage_extended')) or bool(pending_storage_order),
            'storage_extended_gb': int(kwargs.get('gb') or 0) or (int(pending_storage_order.storage_limit_gb or 0) if pending_storage_order else 0),
            'pending_storage_order_id': int(kwargs.get('order_id') or 0)
                or (pending_storage_order.id if pending_storage_order else 0),
            'is_deploying': bool(kwargs.get('deploying')) or needs_deployment or (
                instance.state == 'draft'
                and instance.deployment_state in ('pending', 'deploying', 'failed')
            ),
            'deployment_state': instance.deployment_state or 'idle',
            'deployment_error': instance.deployment_error or '',
        }
        values.update(self._get_instance_billing_values(instance))
        return self._get_page_view_values(instance, access_token, values, 'my_instances_history', False, **kwargs)

    @http.route('/saas/instance/stop', type='json', auth='user')
    def instance_stop(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        if instance.state not in ('deploy', 'suspend'):
            return {'success': False, 'error': _("This instance cannot be stopped in its current state.")}
        try:
            instance.action_stop()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'state': instance.state, 'operation_state': instance.operation_state}

    @http.route('/saas/instance/deploy', type='json', auth='user')
    def instance_deploy(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        if instance.state != 'draft' and not instance.buy_now_from_pricing:
            return {'success': False, 'error': _("This instance is already deployed.")}
        self._validate_instance(instance)
        try:
            instance.action_deploy()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'state': instance.state, 'operation_state': instance.operation_state}

    @http.route('/saas/instance/start', type='json', auth='user')
    def instance_start(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        locked = self._storage_lock_error(instance)
        if locked:
            return locked
        if instance.state in ('cancel', 'draft'):
            return {'success': False, 'error': _("This instance cannot be started in its current state.")}
        try:
            instance.action_start()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'state': instance.state, 'operation_state': instance.operation_state}

    @http.route('/saas/instance/restart', type='json', auth='user')
    def instance_restart(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        locked = self._storage_lock_error(instance)
        if locked:
            return locked
        if instance.state != 'deploy':
            return {'success': False, 'error': _("Please start the instance before restarting its services.")}
        try:
            instance.action_restart()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'state': instance.state, 'operation_state': instance.operation_state}

    def _storage_lock_error(self, instance):
        """Return an error payload when the instance is locked because its storage is full.

        The customer must not be able to start/restart/redeploy the instance and bypass
        the storage restriction; the only way out is to purchase additional storage.
        """
        if instance.suspension_reason == 'storage_full':
            return {
                'success': False,
                'storage_full': True,
                'error': _(
                    "Your instance has been suspended because its storage limit is full. "
                    "Please upgrade your storage to continue using the instance."
                ),
            }
        return None

    @http.route('/saas/instance/deployment-status', type='json', auth='user', website=True)
    def instance_deployment_status(self, instance_id=None, retry=False, **kwargs):
        """Return the progress of the automatic deployment of a purchased instance.

        Also acts as a safety net: if the instance was paid but no deployment was ever
        queued, it queues one now. Pass ``retry`` to re-queue a failed deployment.
        """
        instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id or 0)).exists()
        if not instance:
            return {'success': False, 'error': 'Instance not found'}
        self._validate_instance(instance)

        if retry and instance.state == 'draft':
            instance.write({'deployment_state': 'idle', 'deployment_error': False})

        if instance.state == 'draft' and instance.deployment_state in ('idle', 'pending'):
            if instance.deployment_state == 'idle':
                instance._schedule_deployment()
                request.env.cr.commit()

        deployment_state = instance.deployment_state or 'idle'
        if instance.state == 'deploy' and instance.operation_state == 'run':
            deployment_state = 'deployed'
        return {
            'success': True,
            'deployment_state': deployment_state,
            'state': instance.state,
            'operation_state': instance.operation_state,
            'url': instance.url,
            'instance_name': instance.name,
            'error': instance.deployment_error or False,
        }

    @http.route('/saas/instance/storage-upgrade-status', type='json', auth='user', website=True)
    def instance_storage_upgrade_status(self, instance_id=None, order_id=None, **kwargs):
        """Apply / confirm a storage upgrade and return the refreshed storage state.

        Idempotent: calling it again (page refresh, duplicate payment callback) never
        credits the purchased storage twice.
        """
        partner = request.env.user.partner_id
        instance = request.env['saas.odoo.instance']
        if instance_id:
            instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
            self._validate_instance(instance)

        order = request.env['sale.order']
        if order_id:
            candidate = request.env['sale.order'].sudo().browse(int(order_id))
            if candidate.exists() and (
                candidate.partner_id == partner or request.env.user._is_admin()
            ):
                order = candidate

        if not order and instance:
            order = request.env['sale.order'].sudo().search([
                ('instance_id', '=', instance.id),
                ('id', 'in', request.env.user._saas_visible_instance_ids()),
                ('is_saas_order', '=', True),
                ('saas_order_type', '=', 'buy_storage'),
            ], order='id desc', limit=1)

        if not order or not order.exists():
            return {'success': False, 'error': _("Storage upgrade order not found.")}

        if not instance:
            instance = order.instance_id
            self._validate_instance(instance)

        # Finalize the payment once (invoice + storage), then read the status. Both
        # operations are idempotent so a page refresh never credits storage twice.
        if order.invoice_status == 'to invoice' or not order.storage_applied:
            order._finalize_saas_payment()
        status = order._apply_storage_upgrade()
        return {
            'success': bool(status.get('success')) and not status.get('error'),
            'applied': status.get('applied', False),
            'already_applied': status.get('already_applied', False),
            'resumed': status.get('resumed', False),
            'error': status.get('error'),
            'instance_id': instance.id,
            'additional_gb': status.get('additional_gb', 0.0),
            'storage_limit_gb': instance.storage_limit_gb or 0.0,
            'storage_used_gb': instance.storage_used_gb or 0.0,
            'storage_percentage': instance.storage_percentage or 0.0,
            'suspension_reason': instance.suspension_reason or False,
            'state': instance.state,
            'operation_state': instance.operation_state,
        }

    @http.route('/saas/instance/workers-upgrade-status', type='json', auth='user', website=True)
    def instance_workers_upgrade_status(self, instance_id=None, order_id=None, **kwargs):
        """Apply / confirm a workers upgrade and return the refreshed worker state.

        Idempotent: calling it again (page refresh, duplicate payment callback) never
        credits the purchased workers twice.
        """
        partner = request.env.user.partner_id
        instance = request.env['saas.odoo.instance']
        if instance_id:
            instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
            self._validate_instance(instance)

        order = request.env['sale.order']
        if order_id:
            candidate = request.env['sale.order'].sudo().browse(int(order_id))
            if candidate.exists() and (
                candidate.partner_id == partner or request.env.user._is_admin()
            ):
                order = candidate

        if not order and instance:
            order = request.env['sale.order'].sudo().search([
                ('instance_id', '=', instance.id),
                ('id', 'in', request.env.user._saas_visible_instance_ids()),
                ('is_saas_order', '=', True),
                ('saas_order_type', '=', 'buy_workers'),
                ('workers_applied', '=', False),
            ], order='id desc', limit=1)

        if not order or not order.exists():
            return {'success': False, 'error': _("Workers upgrade order not found.")}

        if not instance:
            instance = order.instance_id
            self._validate_instance(instance)

        if order.invoice_status == 'to invoice' or not order.workers_applied:
            order._finalize_saas_payment()
        status = order._apply_workers_upgrade()
        return {
            'success': bool(status.get('success')) and not status.get('error'),
            'applied': status.get('applied', False),
            'already_applied': status.get('already_applied', False),
            'error': status.get('error'),
            'instance_id': instance.id,
            'additional_workers': status.get('additional_workers', 0),
            'workers_count': instance.workers_count or 1,
            'max_workers': instance.MAX_WORKERS_PER_INSTANCE,
            'state': instance.state,
            'operation_state': instance.operation_state,
        }

    @http.route('/saas/instance/create-backup', type='json', auth='user')
    def instance_create_backup(self, instance_id, **kwargs):
        """Start a backup in the background and return immediately.

        The portal then polls ``/saas/instance/backup-status`` so the progress bar keeps
        running even if the page is refreshed or closed.
        """
        instance = self._browse_instance(instance_id)
        self._validate_instance(instance)
        try:
            return instance.action_backup_async(backup_type='manual')
        except UserError as error:
            return {'success': False, 'error': str(error)}
        except Exception as error:  # never 500 the portal because of a backup failure
            _logger.exception("Could not start the manual backup for instance %s", instance.name)
            return {'success': False, 'error': str(error) or repr(error)}

    @http.route('/saas/instance/backup-status', type='json', auth='user')
    def instance_backup_status(self, instance_id, **kwargs):
        """Live backup state, used by the progress bar (also after a page refresh)."""
        instance = self._browse_instance(instance_id)
        self._validate_instance(instance)
        status = instance.get_backup_status()
        status['backups'] = [
            {
                'id': bk.id,
                'name': bk.name,
                'date': bk.datetime.strftime('%d %b %Y, %H:%M') if bk.datetime else '',
                'size': '%.2f MB' % (bk.file_size or 0.0) if bk.file_size else 'N/A',
                'type': 'auto' if bk.backup_type == 'auto' else 'manual',
                'type_label': _('Automatic') if bk.backup_type == 'auto' else _('Manual'),
                'download_url': '/my/instance/%s/download-backup/%s' % (instance.id, bk.id),
            }
            for bk in instance.backup_ids.sorted('datetime', reverse=True)
        ]
        return status

    @http.route('/saas/instance/delete-backup', type='json', auth='user')
    def instance_delete_backup(self, instance_id, backup_id, **kwargs):
        """Delete one backup (the archive file is removed together with the record)."""
        # The portal sends these as strings (they come from data-attributes), so normalise
        # them: comparing a recordset against a non-int id never matches.
        try:
            instance_id = int(instance_id)
            backup_id = int(backup_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': _("Backup does not exist.")}
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        backup = request.env['saas.odoo.instance.backup'].sudo().browse(backup_id).exists()
        if not backup or backup.instance_id.id != instance.id:
            return {'success': False, 'error': _("Backup does not exist.")}
        name = backup.name
        backup.unlink()
        instance._log_history(
            _("Backup deleted"),
            category='backup',
            level='warning',
            icon='fa-trash',
            summary=name,
            description=_("Backup %s was deleted from the portal.") % name,
        )
        return {'success': True}

    @http.route('/saas/instance/toggle-autobackup', type='json', auth='user')
    def instance_toggle_autobackup(self, instance_id, enabled=False, **kwargs):
        """Enable/disable the automatic daily backup for one instance."""
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        enabled = bool(enabled)
        instance.write({'enable_autobackup': enabled})
        instance._log_history(
            _("Automatic backups enabled") if enabled else _("Automatic backups disabled"),
            category='backup',
            level='info',
            icon='fa-clock-o',
            description=_("Automatic daily backups were %s from the portal.")
                % (_("enabled") if enabled else _("disabled")),
        )
        return {'success': True, 'enabled': enabled}

    @http.route([
        '/my/instance/<int:instance_id>/download-backup',
        '/my/instance/<int:instance_id>/download-backup/<int:backup_id>'
    ], type='http', auth='user')
    def instance_download_backup(self, instance_id, backup_id=None, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        backup = False
        if not backup_id:
            backup = instance.backup_ids.sorted('datetime', reverse=True)[:1]
        else:
            backup = request.env['saas.odoo.instance.backup'].sudo().browse(backup_id) & instance.backup_ids
        if not backup:
            raise MissingError(_("Backup does not exist."))
        try:
            headers = [
                ('Content-Type', 'application/octet-stream; charset=binary'),
                ('Content-Disposition', content_disposition(backup.name)),
            ]
            with open(backup.file_path, mode='rb') as f:
                stream = f.read()
            response = request.make_response(stream, headers=headers)
            return response
        except Exception as e:
            error = "Download backup error: %s" % (str(e) or repr(e))
            _logger.exception(error)
            return self.portal_my_instance_detail(instance_id)

    @http.route('/saas/instance/verify-domain', type='json', auth='user')
    def instance_verify_domain(self, instance_id, domain_name, **kwargs):
        """Check that the customer's domain really points to our server."""
        import socket
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        domain = (domain_name or '').strip().lower().replace('http://', '').replace('https://', '').strip('/')
        if not domain or '.' not in domain:
            return {'success': False, 'error': _(
                "Enter a domain like erp.mycompany.com or mycompany.com.")}
        server_ip = instance.pserver_id._get_managing_ip() or ''
        try:
            resolved = socket.gethostbyname(domain)
        except Exception:
            resolved = ''
        if not resolved:
            return {'success': False, 'server_ip': server_ip, 'resolved': '', 'error': _(
                "%(domain)s does not resolve yet. Add an A record in your domain DNS "
                "with the value %(ip)s, wait a few minutes and try again.") % {
                    'domain': domain, 'ip': server_ip or _('our server IP')}}
        if server_ip and resolved != server_ip:
            return {'success': False, 'server_ip': server_ip, 'resolved': resolved, 'error': _(
                "%(domain)s currently points to %(found)s. Set its A record to %(ip)s in "
                "your DNS, then add the domain again.") % {
                    'domain': domain, 'found': resolved, 'ip': server_ip}}
        return {'success': True, 'resolved': resolved, 'server_ip': server_ip}

    @http.route('/saas/instance/domain-status', type='json', auth='user')
    def instance_domain_status(self, instance_id, domain_name, **kwargs):
        """Tell the portal whether the certificate is really live for this domain."""
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        domain = (domain_name or '').strip().lower()
        record = instance.domain_name_ids.filtered(lambda d: d.name == domain)[:1]
        status = {'success': True, 'domain': domain, 'state': record.state or '',
                  'ssl': False, 'https': 0, 'http': 0, 'any': 0, 'nginx': False}
        server = instance.pserver_id
        if not server:
            return status
        ssh = server._connect()
        try:
            # No sudo needed: the enabled symlink is world readable and the certificate is
            # verified by talking to localhost over TLS (an invalid/absent certificate makes
            # curl fail, which is exactly the signal we want).
            out = server._exec_cmd(
                'ls /etc/nginx/sites-enabled/%s.conf >/dev/null 2>&1 && echo ENABLED || true; '
                'curl -s -o /dev/null -m 6 -w "PLAIN%%{http_code}" -H "Host: %s" '
                'http://127.0.0.1/ 2>/dev/null || true; echo; '
                'curl -s -o /dev/null -m 10 -w "TLS%%{http_code}" --resolve %s:443:127.0.0.1 '
                'https://%s/ 2>/dev/null || true; echo; '
                'curl -sk -o /dev/null -m 6 -w "ANY%%{http_code}" -H "Host: %s" '
                'https://127.0.0.1/ 2>/dev/null || true'
                % (domain, domain, domain, domain, domain),
                ssh, without_return=False)
            for line in out:
                line = (line or '').strip()
                if line == 'ENABLED':
                    status['nginx'] = True
                elif line.startswith('PLAIN') and line[5:].isdigit():
                    status['http'] = int(line[5:])
                elif line.startswith('TLS') and line[3:].isdigit():
                    status['https'] = int(line[3:])
                elif line.startswith('ANY') and line[3:].isdigit():
                    status['any'] = int(line[3:])
            # a working TLS handshake (any status but 000) means the certificate is live
            status['ssl'] = bool(status['https']) and status['nginx']
        except Exception as error:
            _logger.warning("Domain status check failed for %s: %s", domain, error)
        finally:
            ssh.close()
        return status

    @http.route(['/saas/instance/check-domain-name'], type='json', auth='user')
    def instance_check_domain_name(self, domain_name):
        domain_name = (domain_name or '').strip().lower()
        instance_domain_name = request.env['saas.odoo.instance.domain.name'].sudo().search([
            ('name', '=', domain_name),
        ], limit=1)
        primary_instance = request.env['saas.odoo.instance'].sudo().search([
            '|',
            ('domain_name', '=', domain_name),
            ('name', '=', domain_name),
        ], limit=1)
        if instance_domain_name or primary_instance:
            error = _("%s domain already taken") % domain_name
            return {
                'success': False,
                'error': error,
            }
        return {'success': True}

    @http.route('/saas/instance/remove-domain-name', type='json', auth='user')
    def instance_remove_domain_name(self, domain_name_id=None, **kwargs):
        if not domain_name_id:
            return {'success': False, 'error': _(
                "We could not tell which domain to remove. Reload the page and try again.")}
        domain_name = request.env['saas.odoo.instance.domain.name'].sudo().browse(
            int(domain_name_id))
        if not domain_name.exists():
            return {'success': False, 'error': _("This domain was already removed.")}
        self._validate_instance(domain_name.instance_id)
        domain_name.action_cancel()
        domain_name.unlink()
        return {'success': True, 'message': _(
            "Domain removed with its nginx configuration and SSL certificate.")}

    @http.route('/saas/instance/add-domain-name', type='json', auth='user')
    def instance_add_domain_name(self, instance_id, domain_name, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        instance_domain_name = request.env['saas.odoo.instance.domain.name'].sudo().create({
            'instance_id': instance_id,
            'name': domain_name,
            # A freshly added address should be indexable straight away.
            'noindex': False,
        })
        instance_domain_name.action_deploy()
        return True

    @http.route('/saas/instance/get-app-and-user', type='json', auth='user')
    def instance_get_app_and_user(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        if instance.state != 'deploy':
            return False
        self._validate_instance(instance)
        instance.action_get_active_users()
        instance.action_get_installed_apps()
        return True

    @http.route('/saas/instance/suspend', type='json', auth='user')
    def instance_suspend(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        if instance.state == 'suspend':
            return {'success': True, 'state': 'suspend', 'operation_state': 'stop'}
        if instance.state not in ('deploy', 'draft'):
            return {'success': False, 'error': _("This instance cannot be suspended in its current state.")}
        try:
            instance.action_suspend()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'state': instance.state, 'operation_state': instance.operation_state}

    @http.route('/saas/instance/remove', type='json', auth='user')
    def instance_remove(self, instance_id, **kwargs):
        """Customer-side "Remove Instance" (Instance Controls).

        Same outcome as the back-office Cancel: the containers, the database, the
        files, the nginx vhost, the domain and its SSL certificate are deleted and
        the ports are released. The record itself is **kept** in state ``cancel`` so
        the customer still gets the "Your instance is removed" page; an
        administrator can then delete the record from the backend and it
        disappears from the portal completely.

        Only the owner (or an allowed team member) can do this: ``_validate_instance``
        also blocks team members with the ``developer`` role.
        """
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        if instance.state == 'cancel':
            # Idempotent: a second click (or a stale tab) must not fail.
            return {'success': True, 'state': 'cancel', 'already_removed': True}
        try:
            if instance.state == 'deploy':
                # Stop the services first so the database is not dropped under a
                # running Odoo, then revoke everything.
                instance.action_suspend()
            instance._action_cancel()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'state': instance.state, 'operation_state': instance.operation_state}

    @http.route('/saas/instance/redeploy', type='json', auth='user')
    def instance_redeploy(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        locked = self._storage_lock_error(instance)
        if locked:
            return locked
        try:
            instance.action_redeploy_latest()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @http.route('/saas/instance/github-connect', type='json', auth='user')
    def instance_github_connect(self, instance_id, repo_url, branch='main', token=None, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        try:
            instance.action_connect_github(repo_url=repo_url, branch=branch, token=token)
            return {
                'success': True,
                'repo_url': instance.github_repo_url,
                'branch': instance.github_branch or 'main',
                'connected': instance.github_connected,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @http.route('/saas/instance/github-disconnect', type='json', auth='user')
    def instance_github_disconnect(self, instance_id, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        try:
            instance.action_disconnect_github()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @http.route('/saas/instance/live-logs', type='json', auth='user')
    def instance_live_logs(self, instance_id, lines=100, **kwargs):
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        logs = instance.action_get_live_logs(lines=lines)
        return {'success': True, 'logs': logs}

    # -------------------------------------------------------------------------
    # Live interactive shell (terminal in the customer container)
    # -------------------------------------------------------------------------

    def _instance_shell_call(self, instance_id, method, kind, **kwargs):
        """Single entry point so every shell route validates ownership the same way.

        The instance is fetched and ownership-checked here; the container name, the database
        and the credentials are resolved server side by the model, so the browser never has
        to send (nor know) any credential.
        """
        instance = request.env['saas.odoo.instance'].sudo().browse(instance_id)
        self._validate_instance(instance)
        try:
            return getattr(instance, method)(kind, **kwargs)
        except (UserError, ValidationError) as error:
            return {'success': False, 'error': str(error), 'alive': False}

    @http.route('/saas/instance/shell/open', type='json', auth='user')
    def instance_shell_open(self, instance_id, kind='odoo', **kwargs):
        return self._instance_shell_call(instance_id, '_shell_open', kind)

    @http.route('/saas/instance/shell/read', type='json', auth='user')
    def instance_shell_read(self, instance_id, kind='odoo', **kwargs):
        return self._instance_shell_call(instance_id, '_shell_read', kind)

    @http.route('/saas/instance/shell/write', type='json', auth='user')
    def instance_shell_write(self, instance_id, kind='odoo', text=None, keys=None, **kwargs):
        return self._instance_shell_call(instance_id, '_shell_write', kind,
                                         text=text, keys=keys)

    @http.route('/saas/instance/shell/resize', type='json', auth='user')
    def instance_shell_resize(self, instance_id, kind='odoo', cols=None, rows=None, **kwargs):
        """Fit / fullscreen: make the tmux window match the browser terminal."""
        return self._instance_shell_call(instance_id, '_shell_resize', kind,
                                         cols=cols, rows=rows)

    @http.route('/saas/instance/shell/close', type='json', auth='user')
    def instance_shell_close(self, instance_id, kind='odoo', **kwargs):
        return self._instance_shell_call(instance_id, '_shell_close', kind)

    @http.route('/saas/settings/notifications', type='json', auth='user')
    def saas_settings_notifications(self, events=None, email=None, **kwargs):
        """Save the notification switches / address for the current customer.

        ``events`` is a dict {event_key: bool}; unknown keys are ignored so a stale page
        can never write a field that no longer exists.
        """
        partner = request.env.user.partner_id.sudo()
        # Fields follow the "saas_notify_<event>" convention, so the event keys are enough
        # to validate whatever the (possibly cached) page posted.
        allowed = {item['key']: 'saas_notify_%s' % item['key']
                   for item in request.env['res.partner'].saas_event_map()}
        vals = {}
        for key, enabled in (events or {}).items():
            field_name = allowed.get(key)
            if field_name:
                vals[field_name] = bool(enabled)
        if email is not None:
            vals['saas_notify_email'] = (email or '').strip()
        if vals:
            partner.write(vals)
        return {
            'success': True,
            'email': partner._saas_notify_recipient(),
            'events': partner.env['res.partner'].saas_event_map(),
        }

    @http.route('/saas/settings/notifications/test', type='json', auth='user')
    def saas_settings_notifications_test(self, **kwargs):
        """Send a sample notification so the customer can confirm mail delivery."""
        partner = request.env.user.partner_id.sudo()
        if not partner._saas_notify_recipient():
            return {'success': False, 'error': _(
                "Add a notification email address first, then send the test again.")}
        mail = partner._saas_notify(
            'instance_created',
            title=_("Test email — your hosting notifications work"),
            intro=_("This is a test message from your hosting portal. If you can read this, "
                    "every notification you enabled will arrive at this address."),
            rows=[('Workers', '2'), ('Storage', '5 GB'), ('Backup copies', '5'),
                  ('Sent to', partner._saas_notify_recipient())],
            cta_label=_("Open the hosting portal"),
        )
        if not mail:
            return {'success': False, 'error': _(
                "The test was not sent: turn on “Instance created” notifications first or "
                "add a valid email address.")}
        state = mail.state
        if state == 'exception':
            return {'success': False, 'error': _(
                "Test email queued but not delivered. Check Odoo's outgoing mail server "
                "(SMTP) settings — the message is in the mail queue.")}
        return {'success': True, 'message': _("Test email sent to %s.")
                % partner._saas_notify_recipient()}

    # ------------------------------------------------------------------
    # Team Members tab
    # ------------------------------------------------------------------

    @http.route('/saas/team/add', type='json', auth='user')
    def saas_team_add_member(self, name=None, email=None, role=None, password=None,
                             instance_id=None, **kwargs):
        """Create a team member: with a password (manual) or by email invitation."""
        user = request.env.user
        if not user._saas_check_team_manager():
            return {'success': False, 'error': _(
                "Only the account owner can manage team members.")}
        return user.sudo().saas_team_add(name=name, email=email, role=role, password=password,
                                         instance_id=instance_id)

    @http.route('/saas/team/remove', type='json', auth='user')
    def saas_team_remove_member(self, member_id=None, instance_id=None, **kwargs):
        user = request.env.user
        if not user._saas_check_team_manager():
            return {'success': False, 'error': _(
                "Only the account owner can manage team members.")}
        return user.sudo().saas_team_remove(member_id, instance_id=instance_id)

    @http.route('/saas/team/list', type='json', auth='user')
    def saas_team_list(self, instance_id=None, **kwargs):
        user = request.env.user.sudo()
        if instance_id:
            return user.saas_team_members_for_instance(instance_id)
        members = user._saas_team_members()
        my_role = request.env.user.sudo().saas_team_role or 'owner'
        return {'success': True, 'my_role': my_role, 'members': [{
            'id': member.id,
            'name': member.name,
            'email': member.login,
            'role': member.saas_team_role or 'owner',
            'role_label': member.saas_team_role == 'developer' and _('Developer')
                          or member.saas_team_role == 'admin' and _('Administrator')
                          or _('Owner'),
            'invited': not bool(member.password),
        } for member in members]}

    @http.route('/saas/team/password', type='http', auth='public', website=True, csrf=False,
                sitemap=False, methods=['GET', 'POST'])
    def saas_team_password(self, token=None, password=None, confirm=None, **kwargs):
        """Set-password page for invited team members (no address to type)."""
        user = request.env['res.users'].sudo()._saas_user_from_password_token(token)
        values = {'token': token or '', 'email': user.login if user else '', 'error': False}
        if not user:
            values['error'] = _(
                "This link is invalid or has expired. Ask the account owner to send a "
                "new invitation.")
            return request.render('s_odoo_saas_master.saas_team_password_page', values)
        if request.httprequest.method == 'POST':
            new_password = (password or '').strip()
            if len(new_password) < 8:
                values['error'] = _("The password must be at least 8 characters long.")
            elif new_password != (confirm or '').strip():
                values['error'] = _("The two passwords do not match.")
            else:
                user.sudo().write({'password': new_password, 'active': True})
                request.env.cr.commit()
                return request.redirect('/web/login?saas_password=1')
        return request.render('s_odoo_saas_master.saas_team_password_page', values)

    @http.route('/saas/team/instances', type='json', auth='user')
    def saas_team_instances(self, member_id=None, **kwargs):
        user = request.env.user
        if not user._saas_check_team_manager():
            return {'success': False, 'error': _(
                "Only the account owner can manage team members.")}
        return user.sudo().saas_team_instances(member_id)

    @http.route('/saas/team/instances/set', type='json', auth='user')
    def saas_team_instances_set(self, member_id=None, instance_ids=None, **kwargs):
        user = request.env.user
        if not user._saas_check_team_manager():
            return {'success': False, 'error': _(
                "Only the account owner can manage team members.")}
        return user.sudo().saas_team_set_instances(member_id, instance_ids or [])

    @http.route('/saas/team/re-invite', type='json', auth='user')
    def saas_team_reinvite(self, member_id=None, **kwargs):
        """Mail the invitation again (new set-password link)."""
        user = request.env.user
        if not user._saas_check_team_manager():
            return {'success': False, 'error': _(
                "Only the account owner can manage team members.")}
        return user.sudo().saas_team_reinvite(member_id)

    @http.route('/saas/team/reset-password', type='json', auth='user')
    def saas_team_reset_password(self, member_id=None, password=None, **kwargs):
        """Set a member's password, or mail them a reset link when none is given."""
        user = request.env.user
        if not user._saas_check_team_manager():
            return {'success': False, 'error': _(
                "Only the account owner can manage team members.")}
        return user.sudo().saas_team_reset_password(member_id, password)

    # ------------------------------------------------------------------
    # Security tab: password, login alerts, sign out everywhere, 2FA
    # ------------------------------------------------------------------

    @http.route('/saas/settings/password', type='json', auth='user')
    def saas_settings_password(self, current=None, new_password=None, confirm=None, **kwargs):
        return request.env.user.sudo().saas_change_password(current, new_password, confirm)

    @http.route('/saas/settings/signout-all', type='json', auth='user')
    def saas_settings_signout_all(self, **kwargs):
        """End every stored session of this user (the client then logs this tab out too)."""
        user = request.env.user
        removed = request.env['res.users'].saas_signout_everywhere(user.id)
        return {'success': True, 'removed': removed, 'message': _(
            "Signed out of %(count)s session(s) on every device.") % {'count': removed}}

    @http.route('/saas/settings/login-alerts', type='json', auth='user')
    def saas_settings_login_alerts(self, enabled=None, **kwargs):
        user = request.env.user.sudo()
        user.write({'saas_login_alerts': bool(enabled)})
        return {'success': True, 'enabled': user.saas_login_alerts}

    @http.route('/saas/settings/totp/init', type='json', auth='user')
    def saas_settings_totp_init(self, **kwargs):
        """Give the portal the secret + QR for the authenticator app."""
        return request.env.user.sudo().saas_totp_start()

    @http.route('/saas/settings/totp/apply', type='json', auth='user')
    def saas_settings_totp_apply(self, secret=None, code=None, **kwargs):
        # No rollback here on purpose: the pending secret must survive a wrong code so the
        # customer can simply retype the code from the same QR.
        return request.env.user.sudo().saas_totp_apply(secret, code)

    @http.route('/saas/settings/totp/disable', type='json', auth='user')
    def saas_settings_totp_disable(self, password=None, **kwargs):
        return request.env.user.sudo().saas_totp_disable(password)

    @http.route('/saas/settings/login-email', type='json', auth='user')
    def saas_settings_login_email(self, new_email=None, **kwargs):
        """Change the sign-in email of the connected user."""
        return request.env.user.sudo().saas_change_login(new_email)

    @http.route('/saas/settings/save', type='json', auth='user')
    def saas_settings_save(self, **kwargs):
        partner = request.env.user.partner_id
        vals = {}
        # The workspace name and the region used to be ignored here, so those two fields
        # looked editable but silently kept their old value.
        if kwargs.get('workspaceName'):
            vals['name'] = kwargs['workspaceName'].strip()
        if 'companyName' in kwargs and kwargs['companyName']:
            vals['company_name'] = kwargs['companyName'].strip()
        if 'phone' in kwargs:
            vals['phone'] = kwargs['phone'].strip()
        if 'accountEmail' in kwargs and kwargs['accountEmail']:
            vals['email'] = kwargs['accountEmail'].strip()
        if kwargs.get('region'):
            country = request.env['res.country'].sudo().search(
                [('name', '=', kwargs['region'].strip())], limit=1)
            if country:
                vals['country_id'] = country.id
        if vals:
            partner.sudo().write(vals)

        # The timezone lives on the user: Odoo uses it to render every date of the portal
        # (backups, history, logs, logs of GitHub, expiry dates, ...).
        if kwargs.get('timezone'):
            request.env.user.sudo().write({'tz': kwargs['timezone'].strip()})

        if 'githubClientId' in kwargs or 'githubClientSecret' in kwargs:
            is_admin = request.env.user.has_group('s_odoo_saas_master.group_odoo_saas_manager') or request.env.user.has_group('base.group_system')
            if is_admin:
                comp_vals = {}
                ICP = request.env['ir.config_parameter'].sudo()
                if 'githubClientId' in kwargs:
                    client_id = kwargs['githubClientId'].strip()
                    comp_vals['github_client_id'] = client_id
                    ICP.set_param('saas.github_client_id', client_id)
                if 'githubClientSecret' in kwargs:
                    client_secret = kwargs['githubClientSecret'].strip()
                    comp_vals['github_client_secret'] = client_secret
                    ICP.set_param('saas.github_client_secret', client_secret)
                if comp_vals:
                    request.env.company.sudo().write(comp_vals)

        return {'success': True}
