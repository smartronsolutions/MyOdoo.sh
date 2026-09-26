from odoo import fields, models


class OdooInstanceExtraAddon(models.Model):
    _name = 'saas.odoo.instance.extra.addon'
    _description = "SaaS Odoo Instance Extra Addon"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    name = fields.Char(string='Name', required=True)
    addon_path = fields.Char(string='Source Path', required=True)
    copy_to = fields.Char(string='Copy To', required=True)
    container_path = fields.Char(string='Container Path', required=True)
