"""Seed the SaaS subscription data introduced in 18.0.0.82.

* The base container subscription (Standard plan) becomes 36 / month, billed
  annually (36 × 12 = 432) and now includes 1 worker + 5 GB of storage.
* Every existing container gets its subscription metadata: base plan, base
  price, included resources and subscription start date.
"""


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID, fields

    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Base plan price: legacy 14900 (XPF-era) / 26 (EUR-era) -> 36.
    standard = env['product.product'].search(
        [('default_code', 'ilike', 'Standard'), ('active', '=', True)], limit=1)
    if standard and standard.list_price in (0.0, 26.0, 14900.0):
        standard.list_price = 36.0

    # 2. Seed container subscription metadata.
    Instance = env['saas.odoo.instance']
    instances = Instance.search([('base_plan_product_id', '=', False)])
    for instance in instances:
        plan_product = Instance.browse()
        start_date = instance.create_date.date() if instance.create_date else fields.Date.today()
        for order in instance.sale_order_ids.filtered(lambda o: o.saas_order_type == 'buy_new'):
            if order.date_order and order.date_order.date() < start_date:
                start_date = order.date_order.date()
            for line in order.order_line:
                product = line.product_id
                if not product or product.is_saas_user:
                    continue
                code = (product.default_code or '').lower()
                if code in ('saas_extra_storage',) or 'storage' in code:
                    continue
                plan_product = product
                break
            if plan_product:
                break
        if not plan_product and standard:
            plan_product = standard
        if not plan_product:
            continue
        is_growth = 'growth' in (plan_product.default_code or '').lower() \
            or 'growth' in (plan_product.name or '').lower()
        company = instance.company_id or env.company
        instance.write({
            'base_plan_product_id': plan_product.id,
            'base_price_monthly': company._saas_convert_price(float(plan_product.list_price or 0.0)),
            'base_workers': 1,
            'base_storage_gb': 20.0 if is_growth else 5.0,
            'subscription_start_date': start_date,
        })
