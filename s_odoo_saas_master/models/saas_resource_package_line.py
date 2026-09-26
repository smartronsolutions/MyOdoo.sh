from odoo import fields, models


class SaaSResourcePackageLine(models.Model):
    _name = 'saas.resource.package.line'
    _description = "SaaS Resource Packcage Line"

    package_id = fields.Many2one('saas.resource.package', string='Package', ondelete='cascade')
    name = fields.Char(string='Key', required=True)
    value = fields.Char(string='Value', required=True)
    type = fields.Selection([
        ('limits', 'Limits'),
        ('reservations', 'Reservations')
    ], string='Limit Type', required=True)
