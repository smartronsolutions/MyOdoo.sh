from odoo import fields, models


class OdooVersionConfig(models.Model):
    _name = 'saas.odoo.version.config'
    _description = "SaaS Odoo Version Config"

    name = fields.Char(string="Key", required=True)
    value = fields.Char(string="Value")
    section_id = fields.Many2one('saas.odoo.version.config.section', string="Section", required=True)
    odoo_version_id = fields.Many2one('saas.odoo.version', string="Odoo Version", required=True)
