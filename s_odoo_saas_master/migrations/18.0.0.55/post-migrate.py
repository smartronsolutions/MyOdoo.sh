# Prices are now taken from the product sale price as-is, in the company currency.
#
# An earlier revision auto-flagged XPF as the "SaaS Price Currency" and divided the
# sale prices by 119.3317 to display them in EUR. That is wrong for databases whose
# product prices are entered directly in the company currency (e.g. Essential = 100
# EUR must show 100 €, not 0.84 €). The conversion is now opt-in: clear the flag so
# prices are used as-is. It can be re-enabled per company in Settings > Odoo SaaS.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    companies = env['res.company'].search([('saas_price_currency_id', '!=', False)])
    if companies:
        companies.write({'saas_price_currency_id': False})
        _logger.info(
            "SaaS price conversion disabled on %s company(ies): prices are used as-is "
            "in the company currency.", len(companies))
