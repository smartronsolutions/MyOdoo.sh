from odoo import fields, models, api


class OdooInstanceInstalledApp(models.Model):
    _name = 'saas.odoo.instance.installed.app'
    _description = "Odoo Instance Installed App"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    name = fields.Char(string='Name', required=True)
    technical_name = fields.Char(string='Technical Name', required=True)
    installed_date = fields.Datetime(string='Installed Date')
    product_id = fields.Many2one('product.product', string='Product')
    image_1920 = fields.Image(related='product_id.image_1920', store=True)
    image_1024 = fields.Image(related='product_id.image_1024', store=True)
    image_512 = fields.Image(related='product_id.image_512', store=True)
    image_256 = fields.Image(related='product_id.image_256', store=True)
    image_128 = fields.Image(related='product_id.image_128', store=True)
    not_paid = fields.Boolean(string='Not Paid', compute='_compute_not_paid', store=True)

    @api.depends('instance_id', 'instance_id.sale_order_ids', 'instance_id.sale_order_ids.state', 'product_id')
    def _compute_not_paid(self):
        for r in self:
            r.not_paid = False
            domain = r._get_installed_apps_domain()
            order_lines = r.instance_id.sale_order_ids.order_line.filtered_domain(domain)
            paid_products = r._get_paid_apps(order_lines)
            if r.product_id.list_price > 0 and r.product_id.id not in paid_products.ids:
                r.not_paid = True

    def _get_installed_apps_domain(self):
        domain = [
            ('order_id.state', '=', 'sale'),
            ('product_id.is_saas_user', '=', False),
            ('price_subtotal', '>', 0)
        ]
        return domain

    def _get_paid_apps(self, order_lines):
        return order_lines.product_id
