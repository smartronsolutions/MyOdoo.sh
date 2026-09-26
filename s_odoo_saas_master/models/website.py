from odoo import models


class Website(models.Model):
    _inherit = 'website'

    def create_saas_order(self, data):
        vals = self.sudo()._prepare_saas_order_vals(data)
        order = self.env['sale.order'].sudo().create(vals)
        # order.action_quotation_send()
        return order

    def _resolve_odoo_version_id(self, data):
        """Resolve the Odoo version picked on the pricing card.

        Accepts a version id, a name/number (e.g. ``"18"``/``"Odoo 18"``) or nothing (then
        the default version applies), so the pricing form stays free of internal ids.
        """
        Version = self.env['saas.odoo.version'].sudo()
        raw = data.get('odoo_version_id') or data.get('odoo_version') or data.get('version')
        if not raw:
            return False
        try:
            version = Version.browse(int(raw)).exists()
        except (TypeError, ValueError):
            version = Version.browse()
        if not version:
            version = Version.search([('name', '=ilike', str(raw).strip())], limit=1)
        if not version:
            version = Version.search([('name', 'ilike', str(raw).strip())], limit=1)
        return version.id if version else False

    def _prepare_saas_order_vals(self, data):
        subscription_type = data.get('subscription_type', 'yearly')
        partner = data.get('partner')
        sub_domain = data.get('sub_domain')
        domain_id = int(data.get('domain_id')) if data.get('domain_id') else False
        users_count = int(data.get('users_count', 1))
        plan_product_id = data.get('plan_product_id')
        plan_name = data.get('plan_name') or data.get('plan')
        app_ids = data.get('app_ids', [])
        buy_now_from_pricing = data.get('buy_now_from_pricing', False)
        self.ensure_one()

        order_vals = self._prepare_sale_order_values(partner_sudo=partner)
        # SaaS prices are entered in the company's SaaS price currency (XPF) and
        # converted into the company currency (EUR). The order is billed in that
        # currency, so it must not carry a foreign-currency pricelist.
        company = self.company_id or self.env.company
        target_currency = company.currency_id

        order_vals.update({
            'currency_id': target_currency.id if target_currency else self.company_id.currency_id.id,
            'pricelist_id': False,
            'subscription_type': subscription_type,
            'is_saas_order': True,
            'subdomain': sub_domain,
            'based_domain_id': domain_id,
            'odoo_version_id': self._resolve_odoo_version_id(data),
            'version_type': data.get('version_type') or 'community',
            'storage_limit_gb': float(data.get('storage_gb') or 5.0),
            'buy_now_from_pricing': True if buy_now_from_pricing == 'on' else False,
        })
        if data.get('instance_id'):
            order_vals['instance_id'] = int(data.get('instance_id'))
            order_vals['saas_order_type'] = data.get('saas_order_type') or 'renew'
        order_line_vals = []

        is_annual = (subscription_type == 'yearly')
        # The plan (and the apps) keep the 15% annual discount...
        multiplier = (12 * 0.85) if is_annual else 1.0
        # ...but extra workers and extra storage have ONE price: an annual order is
        # charged the exact same amount as a monthly one (no x12, no discount).
        addon_multiplier = 1.0

        Product = self.env['product.product'].sudo()

        # 1. Plan product line (Standard / Growth)
        plan_product = False
        if plan_product_id:
            plan_product = Product.browse(int(plan_product_id))
        if (not plan_product or not plan_product.exists()) and plan_name:
            plan_product = Product.search([('default_code', '=ilike', plan_name), ('active', '=', True)], limit=1)
            if not plan_product:
                plan_product = Product.search([('name', '=ilike', plan_name), ('active', '=', True)], limit=1)
            if not plan_product:
                try:
                    plan_product = self.sudo().env.ref(f's_odoo_saas_master.product_saas_plan_{plan_name.lower()}')
                except Exception:
                    pass

        if plan_product and plan_product.exists():
            plan_base_price = company._saas_convert_price(float(plan_product.list_price or 0.0))
            plan_unit_price = round(plan_base_price * multiplier, 2)
            order_line_vals.append((0, 0, {
                'product_id': plan_product.id,
                'name': f"{plan_product.name} ({'Annual - 15% OFF' if is_annual else 'Monthly'})",
                'product_uom_qty': 1,
                'product_uom': plan_product.uom_id.id,
                'price_unit': plan_unit_price,
                'technical_price_unit': plan_unit_price,
                'currency_id': target_currency.id if target_currency else False,
                'tax_id': [(6, 0, plan_product.taxes_id.ids)],
            }))

        # 2. Workers line (paid seat licence, auto-created if missing)
        user_product = Product._get_saas_worker_product()

        if user_product and user_product.exists():
            user_base_price = company._saas_convert_price(float(user_product.list_price or 100.0))
            # Same unit price as monthly, whatever the billing cycle.
            user_unit_price = round(user_base_price * addon_multiplier, 2)
            order_line_vals.append((0, 0, {
                'product_id': user_product.id,
                'name': f"{user_product.name} ({'Annual' if is_annual else 'Monthly'})",
                'product_uom_qty': users_count,
                'product_uom': user_product.uom_id.id,
                'price_unit': user_unit_price,
                'technical_price_unit': user_unit_price,
                'currency_id': target_currency.id if target_currency else False,
                'tax_id': [(6, 0, user_product.taxes_id.ids)],
            }))

        # 3. Extra Storage line (if extra_storage_gb > 0)
        storage_gb = int(data.get('storage_gb', 0))
        base_storage = 20 if (plan_name and 'growth' in str(plan_name).lower()) else 5
        if storage_gb < base_storage:
            storage_gb = base_storage
        extra_storage_gb = max(0, storage_gb - base_storage)

        if extra_storage_gb > 0:
            storage_product = False
            try:
                storage_product = self.sudo().env.ref('s_odoo_saas_master.product_saas_extra_storage')
            except Exception:
                pass
            if not storage_product or not storage_product.exists():
                storage_product = Product.search([('default_code', '=ilike', 'saas_extra_storage'), ('active', '=', True)], limit=1)
            if not storage_product or not storage_product.exists():
                storage_product = Product.search([('name', 'ilike', 'Extra Storage'), ('active', '=', True)], limit=1)

            storage_monthly_price = company._saas_convert_price(220.0)
            if storage_product and storage_product.exists() and storage_product.list_price:
                storage_monthly_price = company._saas_convert_price(float(storage_product.list_price))
            else:
                usd_currency = self.env['res.currency'].sudo().search([('name', '=', 'USD')], limit=1)
                t_curr = target_currency or company.currency_id
                if usd_currency and t_curr and usd_currency != t_curr:
                    try:
                        from odoo import fields
                        conv = usd_currency._convert(2.0, t_curr, company, fields.Date.today())
                        if conv and conv > 0:
                            storage_monthly_price = float(conv)
                    except Exception:
                        pass
                elif t_curr and t_curr.name == 'USD':
                    storage_monthly_price = 2.0

            # Same unit price as monthly, whatever the billing cycle.
            storage_unit_price = round(storage_monthly_price * addon_multiplier, 2)

            if storage_product and storage_product.exists():
                order_line_vals.append((0, 0, {
                    'product_id': storage_product.id,
                    'name': f"{storage_product.name} ({extra_storage_gb} GB) ({'Annual' if is_annual else 'Monthly'})",
                    'product_uom_qty': extra_storage_gb,
                    'product_uom': storage_product.uom_id.id,
                    'price_unit': storage_unit_price,
                    'technical_price_unit': storage_unit_price,
                    'currency_id': target_currency.id if target_currency else False,
                    'tax_id': [(6, 0, storage_product.taxes_id.ids)],
                }))

        order_vals['storage_limit_gb'] = storage_gb

        # 4. Apps lines
        for app_id in app_ids:
            app_product = Product.browse(app_id)
            if app_product and app_product.exists():
                app_base_price = company._saas_convert_price(float(app_product.list_price or 0.0))
                app_unit_price = round(app_base_price * multiplier, 2)
                order_line_vals.append((0, 0, {
                    'product_id': app_id,
                    'product_uom_qty': 1,
                    'product_uom': app_product.uom_id.id,
                    'price_unit': app_unit_price,
                    'technical_price_unit': app_unit_price,
                    'currency_id': target_currency.id if target_currency else False,
                    'tax_id': [(6, 0, app_product.taxes_id.ids)],
                }))


        order_vals['order_line'] = order_line_vals
        return order_vals
