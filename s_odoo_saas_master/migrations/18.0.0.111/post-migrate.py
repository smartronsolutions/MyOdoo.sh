"""Align the Growth plan with the Standard plan.

The plan products live in ``product_product_data.xml`` with ``noupdate="1"``, so the
new ``list_price`` written in that file only applies to fresh databases. Growth was
still carrying its legacy XPF price (39900) and the pricing page displayed it as
39,900 in EUR.

Both plans now ship the same base container — 36 / month, 2 workers, 5 GB — so the
stored price is aligned here.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

STANDARD_PRICE = 36.0


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    growth = env.ref(
        's_odoo_saas_master.product_saas_plan_growth', raise_if_not_found=False)
    if not growth:
        growth = env['product.product'].search(
            [('default_code', '=', 'Growth'), ('type', '=', 'service')], limit=1)
    if growth and round(growth.list_price or 0.0, 2) != STANDARD_PRICE:
        _logger.info(
            "Growth plan: %s -> %s (product %s).",
            growth.list_price, STANDARD_PRICE, growth.id)
        growth.list_price = STANDARD_PRICE
