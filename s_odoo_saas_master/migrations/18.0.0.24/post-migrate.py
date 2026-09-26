# Rebrand the paid seat licence from "Users" to "Workers" (Odoo.sh style).
#
# The data files that define the seat product, its UoM and its public category
# are all loaded with noupdate="1", so editing them only affects fresh installs.
# This post-migration aligns existing databases with the new wording and archives
# the legacy "SaaS User" product.
#
# Note: ``product.product.active`` is its own field (it does NOT mirror
# ``product.template.active``), so both levels have to be toggled to really
# hide/show a product.
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Rename the seat UoM / category display names.
    uom_categ = env.ref('s_odoo_saas_master.product_uom_categ_saas_user', raise_if_not_found=False)
    if uom_categ:
        uom_categ.name = 'Workers'

    uom = env.ref('s_odoo_saas_master.product_uom_saas_user_month', raise_if_not_found=False)
    if uom:
        uom.name = 'Worker'

    categ = env.ref('s_odoo_saas_master.public_category_saas_user', raise_if_not_found=False)
    if categ:
        categ.name = 'Workers'

    # 2. Archive the legacy "SaaS User" product at template AND variant level
    #    (keep it in DB so historical sale orders keep their product link).
    legacy = env.ref('s_odoo_saas_master.product_saas_user', raise_if_not_found=False)
    if legacy:
        legacy.product_tmpl_id.with_context(active_test=False).write({
            'name': 'SaaS User (legacy)',
            'is_published': False,
            'active': False,
        })
        legacy.with_context(active_test=False).write({'active': False})

    # 3. Make sure the new "Workers" product exists, is active and published.
    worker = env.ref('s_odoo_saas_master.product_saas_worker', raise_if_not_found=False)
    if worker:
        worker.product_tmpl_id.with_context(active_test=False).write({
            'name': 'Workers',
            'is_published': True,
            'active': True,
        })
        worker.with_context(active_test=False).write({'active': True, 'is_saas_user': True})
