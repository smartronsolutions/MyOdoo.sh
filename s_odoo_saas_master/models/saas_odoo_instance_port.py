from odoo import fields, models


class OdooInstancePort(models.Model):
    _name = 'saas.odoo.instance.port'
    _description = "SaaS Odoo Instance Port"

    name = fields.Char(string='Name', required=True)
    port = fields.Integer(string='Port', required=True)
    pserver_id = fields.Many2one('saas.pserver', string='Physical Server', required=True)
    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance')
    based_domain_id = fields.Many2one(related='instance_id.based_domain_id', store=True)
