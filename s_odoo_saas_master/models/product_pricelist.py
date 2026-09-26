from odoo import models
from odoo.osv import expression


class ProductPricelist(models.Model):
    _inherit = 'product.pricelist'

    def _get_applicable_rules_domain(self, products, date, **kwargs):
        domain = super(ProductPricelist, self)._get_applicable_rules_domain(products, date, kwargs=kwargs)
        subscription_type = self._context.get('subscription_type', 'none')
        if subscription_type and subscription_type != 'none':
            domain = expression.AND([
                domain,
                [('subscription_type', '=', subscription_type)],
            ])
        return domain
