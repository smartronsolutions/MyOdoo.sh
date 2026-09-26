from odoo import fields, models


class OdooInstanceResourcePackageLine(models.Model):
    _name = 'saas.odoo.instance.resource.package.line'
    _description = "SaaS Odoo Instance Resource Package Line"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', ondelete='cascade')
    name = fields.Char(string='Key', required=True)
    value = fields.Char(string='Value', required=True)
    type = fields.Selection([
        ('limits', 'Limits'),
        ('reservations', 'Reservations')
    ], string='Limit Type', required=True)
