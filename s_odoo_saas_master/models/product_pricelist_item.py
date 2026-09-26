from odoo import fields, models


class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    subscription_type = fields.Selection([('none', 'None'),
                                          ('monthly', 'Monthly'),
                                          ('yearly', 'Yearly')], string='Subscription Type', default='none')
