import requests
import time
import psycopg2
from odoo import fields, http, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.tools import groupby

from odoo.addons.website_sale.controllers.main import WebsiteSale

import logging
_logger = logging.getLogger(__name__)


class Pricing(http.Controller):

    def _get_pricelist_context(self):
        pricelist_context = dict(request.env.context)
        if not pricelist_context.get('pricelist'):
            pricelist = request.website._get_current_pricelist()
            pricelist_context['pricelist'] = pricelist.id
        else:
            pricelist = request.env['product.pricelist'].browse(pricelist_context['pricelist'])
        if not pricelist:
            pricelist = request.env['product.pricelist'].search([('company_id', '=', request.website.company_id.id)], limit=1)

        return pricelist_context, pricelist, request.env['product.pricelist'].search([])

    def _format_price(self, val):
        if val is None:
            return '0'
        val = float(val)
        if val.is_integer():
            return f"{int(val):,}"
        return f"{val:,.2f}"

    def _get_company(self):
        """Company that drives the SaaS prices (the website company when available)."""
        website = getattr(request, 'website', None)
        return (website and website.company_id) or request.env.company

    def _convert_price(self, amount, from_currency=None):
        """Convert an amount entered in the SaaS price currency into the company currency."""
        return self._get_company()._saas_convert_price(amount, from_currency=from_currency)

    def _get_display_currency(self):
        """Currency the customer must see / pay in (the company currency)."""
        return self._get_company().currency_id

    # The entry plan was renamed "Standard" (it used to be "Essential"). Both names
    # must keep resolving so pre-migration databases, already-created orders and any
    # cached form posting the old label keep working.
    PLAN_PRODUCT_ALIASES = {
        'standard': ('Standard', 'Essential'),
        'essential': ('Standard', 'Essential'),
        'growth': ('Growth',),
    }

    def _get_plan_product(self, plan_name):
        Product = request.env['product.product'].sudo()
        candidates = self.PLAN_PRODUCT_ALIASES.get((plan_name or '').lower(), (plan_name,))
        for candidate in candidates:
            prod = Product.search([('default_code', '=ilike', candidate), ('active', '=', True)], limit=1)
            if not prod:
                prod = Product.search([('name', '=ilike', candidate), ('active', '=', True)], limit=1)
            if prod:
                return prod
        # Legacy XML id fallback (the ``Standard`` product still lives under the
        # ``product_saas_plan_essential`` XML id).
        try:
            return request.env.ref(
                f's_odoo_saas_master.product_saas_plan_{plan_name.lower()}').sudo()
        except Exception:
            return Product

    def _get_user_product(self):
        """Return the paid "Workers" seat product (auto-created if missing)."""
        return request.env['product.product'].sudo()._get_saas_worker_product()

    def _get_extra_storage_product(self):
        Product = request.env['product.product'].sudo()
        prod = Product.search([('default_code', '=ilike', 'saas_extra_storage'), ('active', '=', True)], limit=1)
        if not prod:
            try:
                prod = request.env.ref('s_odoo_saas_master.product_saas_extra_storage').sudo()
            except Exception:
                pass
        if not prod:
            prod = Product.search([('name', 'ilike', 'Extra Storage'), ('active', '=', True)], limit=1)
        return prod

    def _get_storage_price_per_gb(self, pricelist=False):
        """Monthly price per extra GB, expressed in the company currency.

        The ``saas_extra_storage`` price is entered in the company's SaaS price
        currency (XPF by default) and converted automatically. When the product
        carries no price we fall back to the historic $2 USD / GB base.
        """
        company = self._get_company()
        prod = self._get_extra_storage_product()

        if prod and prod.list_price:
            return company._saas_convert_price(float(prod.list_price))

        # Fallback: $2 USD / GB / month converted into the company currency.
        display_currency = company.currency_id
        usd_currency = request.env['res.currency'].sudo().search([('name', '=', 'USD')], limit=1)
        if usd_currency and display_currency and usd_currency != display_currency:
            try:
                price = usd_currency._convert(2.0, display_currency, company, fields.Date.today())
                if price and price > 0:
                    return round(price, 2)
            except Exception:
                pass

        return 2.0


    @http.route([
        '''/pricing''',
        '''/saas/pricing''',
        '''/my/saas/pricing'''
    ], type='http', auth="public", website=True)
    def pricing(self, **post):
        pricelist_context, pricelist, pricelists = self._get_pricelist_context()
        partner = request.env.user.partner_id
        request.update_context(pricelist=pricelist.id, partner=partner)

        domains = request.env['saas.based.domain'].sudo().search([])
        ProductObj = request.env['product.product'].sudo()

        # Product prices are entered in the company's SaaS price currency (XPF) and
        # converted here, so the cards follow the company currency (EUR).
        company = self._get_company()

        # Dynamic Plan products lookup (Standard / Growth)
        essential_product = self._get_plan_product('Standard')
        essential_base_price = float(essential_product.list_price) if (essential_product and essential_product.list_price) else 14900.0
        essential_monthly_price = company._saas_convert_price(essential_base_price)
        essential_annual_price = round(essential_monthly_price * 12 * 0.85, 2)

        growth_product = self._get_plan_product('Growth')
        growth_base_price = float(growth_product.list_price) if (growth_product and growth_product.list_price) else 39900.0
        growth_monthly_price = company._saas_convert_price(growth_base_price)
        growth_annual_price = round(growth_monthly_price * 12 * 0.85, 2)

        # Dynamic User product lookup
        user_product = self._get_user_product()
        user_base_price = float(user_product.list_price) if (user_product and user_product.list_price) else 100.0
        user_monthly_price = company._saas_convert_price(user_base_price)
        # Workers have ONE price that never depends on the billing cycle: 1 worker costs
        # the same whether the customer is on monthly or on annual billing (no x12, no
        # annual discount). The plan keeps its own annual discount.
        user_annual_price = user_monthly_price
        user_annual_per_month = user_monthly_price

        essential_monthly_fmt = self._format_price(essential_monthly_price)
        essential_annual_fmt = self._format_price(essential_annual_price)
        growth_monthly_fmt = self._format_price(growth_monthly_price)
        growth_annual_fmt = self._format_price(growth_annual_price)
        user_monthly_fmt = self._format_price(user_monthly_price)
        user_annual_fmt = self._format_price(user_annual_price)
        user_annual_per_month_fmt = self._format_price(user_annual_per_month)

        # Dynamic Extra Storage product lookup (price per GB / month)
        extra_storage_product = self._get_extra_storage_product()
        extra_storage_monthly_price = self._get_storage_price_per_gb(pricelist)
        # Extra storage follows the same rule as the workers: ONE price, identical on
        # monthly and annual billing (no x12, no annual discount).
        extra_storage_annual_price = extra_storage_monthly_price
        extra_storage_annual_per_month = extra_storage_monthly_price

        extra_storage_monthly_fmt = self._format_price(extra_storage_monthly_price)
        extra_storage_annual_fmt = self._format_price(extra_storage_annual_price)
        extra_storage_annual_per_month_fmt = self._format_price(extra_storage_annual_per_month)

        data = {
            'user': {},
            'categs': []
        }
        
        data['user'].update({
            'id': user_product.id if user_product else False,
            'monthly_price': user_monthly_price,
            'yearly_price': user_annual_price,
        })
        all_products = ProductObj.search([
            ('is_published', '=', True),
            ('can_be_user_app', '=', True),
            ('is_saas_user', '=', False),
        ], order='website_sequence, id')
        for cate, products in groupby(all_products, key=lambda p: p.ecom_category_id):
            if not products or len(products) == 0:
                continue
            app_list = []
            for product in products:
                app_monthly_price = company._saas_convert_price(
                    pricelist.with_context(subscription_type='monthly')._get_product_price(product, 1),
                    from_currency=pricelist.currency_id,
                )
                app_list.append({
                    'id': product.id,
                    'name': product.name,
                    'tech_name': product.technical_name,
                    'image': request.website.image_url(product, 'image_256'),
                    'monthly_price': app_monthly_price,
                    'yearly_price': app_monthly_price * 12,
                })
            data['categs'].append({
                'id': cate.id,
                'name': cate.name,
                'apps': app_list
            })

        # Versions + editions the customer can pick, derived from the configured servers.
        # Only a version that has a server can be ordered; the edition list follows suit.
        servers = request.env['saas.odoo.server'].sudo().search([('active', '=', True)])
        version_options = []
        for version in servers.mapped('odoo_version_id').sorted(lambda v: v.name or ''):
            types = servers.filtered(lambda s: s.odoo_version_id == version).mapped('version_type')
            version_options.append({
                'id': version.id,
                'name': version.name,
                'community': 'community' in types or not types,
                'enterprise': 'enterprise' in types,
            })

        values = {
            'page_name': 'saas_pricing',
            'partner': partner,
            'domains': domains,
            'pricelist': pricelist,
            'pricelists': pricelists,
            'data': data,
            'currency_symbol': company.currency_id.symbol or '',
            'max_workers': request.env['saas.odoo.instance'].MAX_WORKERS_PER_INSTANCE,
            'backup_limit': request.env.company.instance_backup_limit or 5,
            'version_options': version_options,
            'default_odoo_version_id': version_options[0]['id'] if version_options else False,
            # Plans
            'essential_product': essential_product,
            'essential_monthly_price': essential_monthly_price,
            'essential_annual_price': essential_annual_price,
            'essential_monthly_price_formatted': essential_monthly_fmt,
            'essential_annual_price_formatted': essential_annual_fmt,
            'growth_product': growth_product,
            'growth_monthly_price': growth_monthly_price,
            'growth_annual_price': growth_annual_price,
            'growth_monthly_price_formatted': growth_monthly_fmt,
            'growth_annual_price_formatted': growth_annual_fmt,
            # Users
            'user_product': user_product,
            'user_product_id': user_product.id if user_product else False,
            'user_monthly_price': user_monthly_price,
            'user_annual_price': user_annual_price,
            'user_annual_per_month': user_annual_per_month,
            'user_monthly_price_formatted': user_monthly_fmt,
            'user_annual_price_formatted': user_annual_fmt,
            'user_annual_per_month_formatted': user_annual_per_month_fmt,
            # Extra Storage ($2 USD / GB / mo converted)
            'extra_storage_product': extra_storage_product,
            'extra_storage_product_id': extra_storage_product.id if extra_storage_product else False,
            'extra_storage_monthly_price': extra_storage_monthly_price,
            'extra_storage_annual_price': extra_storage_annual_price,
            'extra_storage_annual_per_month': extra_storage_annual_per_month,
            'extra_storage_monthly_price_formatted': extra_storage_monthly_fmt,
            'extra_storage_annual_price_formatted': extra_storage_annual_fmt,
            'extra_storage_annual_per_month_formatted': extra_storage_annual_per_month_fmt,
        }
        return request.render("s_odoo_saas_master.portal_pricing_page", values)

    @http.route(['/pricing/get-saas-pricelist'], type='json', auth='public')
    def get_saas_pricelist(self, pricelist_id):
        products = request.env['product.product'].sudo().search([('is_published', '=', True)])
        worker_product = request.env['product.product'].sudo()._get_saas_worker_product()
        if worker_product:
            products |= worker_product
        pricelist = request.env['product.pricelist'].sudo().browse(pricelist_id)
        qty = [1] * len(products)

        monthly_raw = pricelist.with_context(subscription_type='monthly')._get_products_price(products, qty)

        company = self._get_company()
        display_currency = company.currency_id
        # The pricelist returns amounts in its own currency (XPF): convert them so
        # the JS always receives the company currency (EUR).
        monthly_pricelist = {
            k: company._saas_convert_price(float(v or 0), from_currency=pricelist.currency_id)
            for k, v in monthly_raw.items()
        }

        # Recalculate yearly as 12x monthly
        yearly_pricelist_calculated = {k: v * 12 for k, v in monthly_pricelist.items()}

        return {
            'monthly_pricelist': monthly_pricelist,
            'yearly_pricelist': yearly_pricelist_calculated,
            'currency': {
                'id': display_currency.id,
                'symbol': display_currency.symbol or '',
                'decimal_places': display_currency.decimal_places or 2,
                'position': display_currency.position or 'after',
            },
        }

    @http.route(['/pricing/get-required-apps'], type='json', auth='public')
    def get_required_apps(self, app_id):
        product = request.env['product.product'].sudo().browse(app_id)
        return product.get_required_products()

    @http.route(['/pricing/get-dependent-apps'], type='json', auth='public')
    def get_dependent_apps(self, app_id):
        product = request.env['product.product'].sudo().browse(app_id)
        return product.get_dependent_products()

    @http.route(['/pricing/check-domain', '/saas/check-domain', '/saas/check-subdomain-available'], type='json', auth='public', website=True)
    def check_saas_domain(self, sub_domain=None, domain_id=None, **kw):
        sub_domain = sub_domain or kw.get('sub_domain') or kw.get('domain_name') or kw.get('subdomain')
        domain_id = domain_id or kw.get('domain_id') or kw.get('base_domain_id')
        res = request.env['saas.odoo.instance'].sudo().check_subdomain_availability(sub_domain, domain_id)
        res['success'] = res.get('available', False)
        return res

    @http.route(['/saas/auth-status'], type='json', auth='public', website=True)
    def saas_auth_status(self, **kw):
        """Authoritative "am I logged in?" endpoint for the frontend auth guard.

        ``auth='public'`` so a visitor is never bounced to a login page, and the
        answer always comes from the current server-side session. The page's own
        ``odoo.__session_info__`` snapshot can be stale (page rendered while the
        visitor was anonymous, restored from the back/forward cache, or the login
        happened in another browser tab): in those cases the guard used to keep
        answering "Account Required" even though the visitor was authenticated.
        """
        uid = request.session.uid
        return {
            'uid': uid or False,
            'is_public': request.env.user._is_public(),
            'partner_id': request.env.user.partner_id.id if uid else False,
        }

    @http.route(['/pricing/check-trial'], type='json', auth='user', website=True)
    def check_trial(self):
        if request.env.user.partner_id.trial_instance_count >= request.website.company_id.limit_trial:
            return False
        return True

    @http.route(['/saas/instance/buy-storage'], type='json', auth='user', website=True)
    def instance_buy_storage(self, instance_id, additional_gb, subscription_type='monthly', **kwargs):
        """Create a storage-only sale order for an existing instance.
        Only the extra storage product line is added; the base plan is NOT charged again."""
        instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
        if not instance.exists():
            return {'error': 'Instance not found'}
        # Security: only the owner can buy storage for their instance
        partner = request.env.user.partner_id
        if instance.partner_id != partner:
            return {'error': 'Access denied'}

        try:
            additional_gb = int(additional_gb)
        except (TypeError, ValueError):
            return {'error': 'Invalid storage quantity'}
        # 1 GB minimum, 500 GB maximum per purchase.
        additional_gb = max(1, min(additional_gb, 500))
        is_annual = (subscription_type == 'yearly')
        # Extra storage has ONE price: the same amount is charged on a monthly order and
        # on an annual order (no x12, no annual discount).
        multiplier = 1.0

        # Lookup extra storage product
        storage_product = False
        try:
            storage_product = request.env.ref('s_odoo_saas_master.product_saas_extra_storage').sudo()
        except Exception:
            pass
        if not storage_product or not storage_product.exists():
            storage_product = request.env['product.product'].sudo().search(
                [('default_code', '=ilike', 'saas_extra_storage'), ('active', '=', True)], limit=1)
        if not storage_product or not storage_product.exists():
            return {'error': 'Storage product not found'}

        company = self._get_company()
        storage_price_per_gb = company._saas_convert_price(float(storage_product.list_price or 220.0))
        storage_unit_price = round(storage_price_per_gb * multiplier, 2)

        target_currency = company.currency_id

        order_vals = request.website._prepare_sale_order_values(partner_sudo=partner)
        order_vals.update({
            'currency_id': target_currency.id,
            'pricelist_id': False,
            'is_saas_order': True,
            'saas_order_type': 'buy_storage',
            'instance_id': instance.id,
            'subscription_type': subscription_type,
            'storage_limit_gb': additional_gb,
            'order_line': [(0, 0, {
                'product_id': storage_product.id,
                'name': '%s (+%d GB storage) (%s)' % (
                    storage_product.name, additional_gb,
                    'Annual' if is_annual else 'Monthly'
                ),
                'product_uom_qty': additional_gb,
                'product_uom': storage_product.uom_id.id,
                'price_unit': storage_unit_price,
                'tax_id': [(6, 0, storage_product.taxes_id.ids)],
            })],
        })

        order = request.env['sale.order'].sudo().create(order_vals)
        request.session['sale_order_id'] = order.id
        return {'success': True, 'order_id': order.id, 'redirect': '/shop/checkout?express=1'}

    @http.route(['/saas/instance/buy-workers'], type='json', auth='user', website=True)
    def instance_buy_workers(self, instance_id, additional_workers, subscription_type='monthly', **kwargs):
        """Create a workers-only sale order for an existing instance.

        Only the Workers seat line is added; the base plan is NOT charged again.
        On payment the extra workers are added to the instance and it is redeployed.
        """
        instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
        if not instance.exists():
            return {'error': 'Instance not found'}
        # Security: only the owner can buy workers for their instance
        partner = request.env.user.partner_id
        if instance.partner_id != partner:
            return {'error': 'Access denied'}

        max_workers = instance.MAX_WORKERS_PER_INSTANCE
        current_workers = max(int(instance.workers_count or 1), 1)
        if current_workers >= max_workers:
            return {'error': 'This instance already runs the maximum of %s workers.' % max_workers}

        try:
            additional_workers = int(additional_workers)
        except (TypeError, ValueError):
            return {'error': 'Invalid workers quantity'}
        # At least 1, and never more than the remaining slots up to the per-instance cap.
        additional_workers = max(1, min(additional_workers, max_workers - current_workers))
        is_annual = (subscription_type == 'yearly')
        # Workers have ONE price: the same amount is charged on a monthly order and on an
        # annual order (no x12, no annual discount).
        multiplier = 1.0

        worker_product = request.env['product.product'].sudo()._get_saas_worker_product()
        if not worker_product or not worker_product.exists():
            return {'error': 'Workers product not found'}

        company = self._get_company()
        worker_unit_price = round(company._saas_convert_price(float(worker_product.list_price or 100.0)) * multiplier, 2)

        target_currency = company.currency_id

        order_vals = request.website._prepare_sale_order_values(partner_sudo=partner)
        order_vals.update({
            'currency_id': target_currency.id,
            'pricelist_id': False,
            'is_saas_order': True,
            'saas_order_type': 'buy_workers',
            'instance_id': instance.id,
            'subscription_type': subscription_type,
            'workers_count': additional_workers,
            'order_line': [(0, 0, {
                'product_id': worker_product.id,
                'name': '%s (+%d worker%s) (%s)' % (
                    worker_product.name, additional_workers,
                    's' if additional_workers > 1 else '',
                    'Annual' if is_annual else 'Monthly'
                ),
                'product_uom_qty': additional_workers,
                'product_uom': worker_product.uom_id.id,
                'price_unit': worker_unit_price,
                'tax_id': [(6, 0, worker_product.taxes_id.ids)],
            })],
        })

        order = request.env['sale.order'].sudo().create(order_vals)
        request.session['sale_order_id'] = order.id
        return {'success': True, 'order_id': order.id, 'redirect': '/shop/checkout?express=1'}

    @http.route(['/pricing/checkout'], type='http', methods=['POST'], auth="public", website=True)
    def checkout(self, **post):
        # A public visitor cannot own an instance/subscription. The frontend
        # already shows the "Account Required" dialog; this is a safety net for
        # direct POSTs / expired sessions: send them to login and back to the
        # page they were configuring.
        if request.env.user._is_public():
            from urllib.parse import urlencode, urlparse
            referer = request.httprequest.referrer
            back = urlparse(referer).path if referer else '/saas/pricing'
            return request.redirect('/web/login?' + urlencode({'redirect': back}))

        sub_domain = (post.get('sub_domain') or '').strip().lower()
        domain_id = post.get('domain')
        instance_id = post.get('instance_id')

        # Subdomain availability validation only for new instances
        if instance_id:
            inst = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
            if inst.exists():
                post['instance_id'] = inst.id
                post['saas_order_type'] = 'renew'
                if not sub_domain:
                    sub_domain = inst.name
                if not domain_id and inst.based_domain_id:
                    domain_id = inst.based_domain_id.id
        else:
            check_res = request.env['saas.odoo.instance'].sudo().check_subdomain_availability(sub_domain, domain_id)
            if not check_res.get('available'):
                from urllib.parse import urlencode
                error_msg = check_res.get('error') or f"{sub_domain} domain already taken"
                params = urlencode({'domain_error': error_msg, 'subdomain': sub_domain})
                return request.redirect(f'/saas/pricing?{params}')

        pricelist = request.website._get_current_pricelist()
        num_users = int(post.pop('num_users', 1))
        storage_gb = int(post.pop('storage_gb', 0))
        subscription_type = post.pop('price_by', 'yearly')
        plan_name = post.pop('plan', 'Standard')
        plan_product_id = post.pop('plan_product_id', False)

        if not plan_product_id:
            plan_product = self._get_plan_product(plan_name)
            if plan_product:
                plan_product_id = plan_product.id
        else:
            try:
                plan_product_id = int(plan_product_id)
            except (ValueError, TypeError):
                plan_product = self._get_plan_product(plan_name)
                plan_product_id = plan_product.id if plan_product else False

        app_ids = []
        for key, val in post.items():
            if key.startswith('app_') and val == 'on':
                app_id = int(key[4:])
                app_ids.append(app_id)
        
        post['partner'] = request.env.user.partner_id
        post['domain_id'] = domain_id
        post['sub_domain'] = sub_domain
        post['users_count'] = num_users
        post['storage_gb'] = storage_gb
        post['plan_name'] = plan_name
        post['plan_product_id'] = plan_product_id
        post['app_ids'] = app_ids
        post['subscription_type'] = subscription_type
        post['pricelist'] = pricelist
        
        # Create order (Plan line, User line, and Extra Storage line are created with exact discounted prices)
        order = request.website.create_saas_order(post)
                
        request.session['sale_order_id'] = order.id
        return request.redirect('/shop/checkout?express=1')

    @http.route('/saas/instance/create-trial', type='json', auth='user')
    def instance_create(self, instance_vals, **kwargs):
        sub_domain = (instance_vals.get('sub_domain') or '').strip().lower()
        base_domain_id = instance_vals.get('base_domain_id')

        # Subdomain availability validation
        check_res = request.env['saas.odoo.instance'].sudo().check_subdomain_availability(sub_domain, base_domain_id)
        if not check_res.get('available'):
            return {'error': check_res.get('error') or f"{sub_domain} domain already taken"}

        base_domain = request.env['saas.based.domain'].sudo().browse(base_domain_id) if base_domain_id else False
        if not base_domain or not base_domain.exists():
            base_domain = request.env['saas.based.domain'].sudo().search([], limit=1)

        default_app_ids = instance_vals.get('default_app_ids', [])        
        app_ids = []
        for app_id in default_app_ids:
            if isinstance(app_id, str) and app_id.startswith('app_'):
                app_ids.append(int(app_id[4:]))
            elif isinstance(app_id, int):
                app_ids.append(app_id)
            elif isinstance(app_id, str) and app_id.isdigit():
                app_ids.append(int(app_id))
                
        apps = request.env['product.product'].sudo().browse(app_ids)
        default_modules = apps.mapped('technical_name')

        instance_vals['sub_domain'] = sub_domain
        instance_vals['base_domain'] = base_domain
        instance_vals['based_domain'] = base_domain
        instance_vals['base_domain_id'] = base_domain.id if base_domain else False
        instance_vals['based_domain_id'] = base_domain.id if base_domain else False
        instance_vals['default_modules'] = default_modules
        instance_vals['partner'] = request.env.user.partner_id
        instance_vals['trial'] = True
        
        # Storage configuration for trial
        storage_gb = instance_vals.get('storage_gb')
        if storage_gb:
            instance_vals['storage_limit_gb'] = float(storage_gb)

        try:
            instance_vals = request.env['saas.odoo.instance'].sudo()._prepare_instance_val_to_create(instance_vals)
            instance = request.env['saas.odoo.instance'].sudo().create(instance_vals)
            instance.action_deploy()
            return {
                'id': instance.id,
                'name': instance.name,
                'url': instance.url,
                'domain_name': instance.domain_name,
                'expiration_date': instance.expiration_date.strftime('%d %B %Y') if instance.expiration_date else '',
            }
        except psycopg2.errors.UniqueViolation:
            # Another request created the same subdomain first (e.g. a double click).
            request.env.cr.rollback()
            return {
                'error': _("Subdomain '%s' is already taken. Please choose another one.") % sub_domain
            }
        except (ValidationError, UserError) as e:
            # Business rule rejected the request (trial quota reached, bad plan, ...). The
            # message is meant for the customer, so it must not be logged as a traceback.
            request.env.cr.rollback()
            return {'error': str(e)}
        except Exception as e:
            _logger.exception("Failed to create trial instance: %s", e)
            return {'error': str(e)}


NON_REQUIRED_FIELDS = ['street', 'city']


class SaasPayment(WebsiteSale):

    @http.route(['/shop/payment/validate'], type='http', auth="public", website=True, sitemap=False)
    def shop_payment_validate(self, sale_order_id=None, **post):
        """Send a paid upgrade order straight to the instance page.

        The standard route redirects to ``/shop/confirmation`` (or ``/shop`` when the
        session cart is gone). For SaaS orders we go to the instance page with the
        matching flag (``storage_extended`` / ``workers_upgraded`` / ``deploying``) so
        the customer always sees the preloader animation.
        """
        order = request.env['sale.order']
        # 1. Explicit order id.
        if sale_order_id is not None:
            order = request.env['sale.order'].sudo().browse(int(sale_order_id)).exists()
        # 2. Resolve from the transaction id. The payment portal appends
        #    '?tx_id=<id>&access_token=<token>' to the landing route, and that is how the
        #    customer actually comes back from the provider. Relying on the session cart
        #    alone silently dropped the customer on /shop (no preloader).
        if not order and post.get('tx_id'):
            try:
                tx = request.env['payment.transaction'].sudo().browse(int(post['tx_id'])).exists()
            except (TypeError, ValueError):
                tx = request.env['payment.transaction']
            if tx:
                order = tx.sale_order_ids.filtered(lambda o: o.is_saas_order and o.instance_id)[:1]
        # 3. Session cart / last known order.
        if not order:
            order = request.website.sale_get_order()
        if not order and request.session.get('sale_last_order_id'):
            order = request.env['sale.order'].sudo().browse(request.session['sale_last_order_id']).exists()

        if order and order.is_saas_order and order.saas_order_type == 'buy_storage' and order.instance_id:
            tx_done = any(tx.state == 'done' for tx in order.transaction_ids)
            if tx_done or order.storage_applied:
                try:
                    order._finalize_saas_payment()
                except Exception as ex:
                    # Concurrency errors must bubble up so Odoo retries the request;
                    # other errors are logged and retried by the portal status endpoint.
                    if type(ex).__name__ in ('SerializationFailure', 'DeadlockDetected', 'LockNotAvailable') \
                            or 'could not serialize' in str(ex).lower():
                        raise
                    _logger.exception(ex)
                request.website.sale_reset()
                return request.redirect(
                    '/my/saas/odoo-instance/%s?storage_extended=1&gb=%d&order_id=%d' % (
                        order.instance_id.id, int(order.storage_limit_gb or 0), order.id
                    )
                )

        if order and order.is_saas_order and order.saas_order_type == 'buy_workers' and order.instance_id:
            tx_done = any(tx.state == 'done' for tx in order.transaction_ids)
            if tx_done or order.workers_applied:
                try:
                    order._finalize_saas_payment()
                except Exception as ex:
                    if type(ex).__name__ in ('SerializationFailure', 'DeadlockDetected', 'LockNotAvailable') \
                            or 'could not serialize' in str(ex).lower():
                        raise
                    _logger.exception(ex)
                request.website.sale_reset()
                return request.redirect(
                    '/my/saas/odoo-instance/%s?workers_upgraded=1&workers=%d&order_id=%d' % (
                        order.instance_id.id, int(order.workers_count or 0), order.id
                    )
                )

        if order and order.is_saas_order and order.saas_order_type == 'buy_new' and order.instance_id:
            try:
                order._finalize_saas_payment()
            except Exception as ex:
                if type(ex).__name__ in ('SerializationFailure', 'DeadlockDetected', 'LockNotAvailable') \
                        or 'could not serialize' in str(ex).lower():
                    raise
                _logger.exception(ex)
            request.website.sale_reset()
            return request.redirect('/my/saas/odoo-instance/%s?deploying=1' % order.instance_id.id)

        return super(SaasPayment, self).shop_payment_validate(sale_order_id=sale_order_id, **post)

    @http.route(['/shop/confirmation'], type='http', auth="public", website=True, sitemap=False)
    def shop_payment_confirmation(self, **post):
        sale_order_id = request.session.get('sale_last_order_id')
        if sale_order_id:
            order = request.env['sale.order'].sudo().browse(sale_order_id)
            if order.is_saas_order and order.instance_id:
                try:
                    # Create + pay the invoice once and credit the purchased storage
                    # (idempotent, so refreshing the page never applies it twice).
                    order._finalize_saas_payment()

                    # ── Storage upgrade order ──────────────────────────────
                    if order.saas_order_type == 'buy_storage':
                        instance = order.instance_id
                        gb = int(order.storage_limit_gb or 0)
                        return request.redirect(
                            '/my/saas/odoo-instance/%s?storage_extended=1&gb=%d&order_id=%d' % (
                                instance.id, gb, order.id
                            )
                        )

                    # ── Workers upgrade order ───────────────────────────────
                    if order.saas_order_type == 'buy_workers':
                        return request.redirect(
                            '/my/saas/odoo-instance/%s?workers_upgraded=1&workers=%d&order_id=%d' % (
                                order.instance_id.id, int(order.workers_count or 0), order.id
                            )
                        )

                    # ── New instance ───────────────────────────────────────
                    if order.saas_order_type == 'buy_new':
                        return request.redirect(
                            '/my/saas/odoo-instance/%s?deploying=1' % order.instance_id.id
                        )

                    # ── Regular / renew order ──────────────────────────────
                    is_renew = (order.saas_order_type == 'renew')
                    return request.redirect('/my/saas/odoo-instance/%s?payment_success=1%s' % (
                        order.instance_id.id,
                        '&renewed=1' if is_renew else ''
                    ))
                except Exception as ex:
                    if order.saas_order_type not in ('renew', 'buy_storage', 'buy_workers'):
                        try:
                            order.instance_id._action_cancel()
                            order.instance_id.unlink()
                        except Exception:
                            pass
                    _logger.exception(ex)

        return super(SaasPayment, self).shop_payment_confirmation(post=post)


    def _redirect_instance_url(self, instance):
        response = requests.get(instance.url)
        tried_count = 1
        while response.status_code != 200 and tried_count <= 15:
            time.sleep(2)
            response = requests.get(instance.url)
        return request.redirect(instance.url, local=False)

    def _get_country_related_render_values(self, kw, render_values):
        res = super(SaasPayment, self)._get_country_related_render_values(kw, render_values)
        order = render_values['website_sale_order']
        res['lang'] = order.partner_id.lang
        res['languages'] = request.env['res.lang'].get_installed()
        return res

    def _get_mandatory_fields_billing(self, country_id=False):
        req = super(SaasPayment, self)._get_mandatory_fields_billing(country_id)
        req = list(set(req) - set(NON_REQUIRED_FIELDS))
        return req

    def _get_mandatory_fields_shipping(self, country_id=False):
        req = super(SaasPayment, self)._get_mandatory_fields_shipping(country_id)
        req = list(set(req) - set(NON_REQUIRED_FIELDS))
        return req
