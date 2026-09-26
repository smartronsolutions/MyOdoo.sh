# The entry plan was renamed "Essential" -> "Standard" (product name + internal
# reference). The product XML id deliberately stays ``product_saas_plan_essential``
# so every existing order line, subscription and portal reference keeps working.
#
# ``data/product_product_data.xml`` is declared with ``noupdate="1"``, which means
# the record is only written on a fresh install. Databases that already exist (and
# therefore already carry the old "Essential" name, often with a manually adjusted
# sale price) are migrated here instead, so a module upgrade never resets the price.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    product = env.ref(
        's_odoo_saas_master.product_saas_plan_essential', raise_if_not_found=False)
    if not product:
        _logger.warning("SaaS entry plan product not found, nothing to rename.")
        return

    if product.product_tmpl_id.name != 'Standard':
        product.product_tmpl_id.write({'name': 'Standard'})
    if product.default_code != 'Standard':
        product.write({'default_code': 'Standard'})

    _logger.info(
        "SaaS entry plan renamed Essential -> Standard (product id %s).", product.id)
