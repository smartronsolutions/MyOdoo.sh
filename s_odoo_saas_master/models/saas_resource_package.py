from odoo import fields, models


class SaaSResourcePackage(models.Model):
    _name = 'saas.resource.package'
    _description = "SaaS Resource Package"

    name = fields.Char(string='Name', required=True)
    line_ids = fields.One2many('saas.resource.package.line', 'package_id', string='Package Lines')
    active = fields.Boolean(string='Active', default=True)
