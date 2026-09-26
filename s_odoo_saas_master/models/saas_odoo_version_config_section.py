from odoo import fields, models


class OdooVersionConfigSection(models.Model):
    _name = 'saas.odoo.version.config.section'
    _description = "SaaS Odoo Version Config Section"

    name = fields.Char(string="Name", required=True)
