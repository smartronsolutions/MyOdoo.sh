from odoo import models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _compute_pricelist_item_id(self):
        default_pricelist = self.env['product.pricelist'].search([('company_id', '=', self.env.user.company_id.id)], limit=1)
        for line in self:
            if not line.order_id.is_saas_order:
                if not line.product_id or line.display_type or not line.order_id.pricelist_id:
                    line.pricelist_item_id = False
                else:
                    line.pricelist_item_id = line.order_id.pricelist_id._get_product_rule(
                        line.product_id,
                        quantity=line.product_uom_qty or 1.0,
                        uom=line.product_uom,
                        date=line.order_id.date_order,
                    )                    
            else:
                if not line.product_id or line.display_type:
                    line.pricelist_item_id = False
                else:                    
                    subscription_type = False
                    if line.order_id.is_saas_order:
                        subscription_type = line.order_id.subscription_type
                    if not line.order_id.pricelist_id:
                        item = default_pricelist.with_context(subscription_type=subscription_type)._get_product_rule(
                            line.product_id,
                            quantity=line.product_uom_qty or 1.0,
                            uom=line.product_uom,
                            date=line.order_id.date_order,
                        )
                        line.pricelist_item_id = item
                    else:
                        item = line.order_id.pricelist_id.with_context(subscription_type=subscription_type)._get_product_rule(
                            line.product_id,
                            quantity=line.product_uom_qty or 1.0,
                            uom=line.product_uom,
                            date=line.order_id.date_order,
                        )
                        line.pricelist_item_id = item

    def _compute_price_unit(self):
        # SaaS lines have explicit subscription unit prices set at creation time.
        # Preserve them when price_unit is already established.
        saas_lines = self.filtered(lambda l: l.order_id.is_saas_order and l.price_unit > 0)
        super(SaleOrderLine, self - saas_lines)._compute_price_unit()

    def _get_display_price(self):
        self.ensure_one()
        if self.order_id.is_saas_order and self.price_unit > 0:
            return self.price_unit
        return super()._get_display_price()

