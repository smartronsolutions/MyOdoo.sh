from odoo import fields, models


class PServerIP(models.Model):
    _name = 'saas.pserver.ip'
    _description = "SaaS PServer IP"

    pserver_id = fields.Many2one('saas.pserver', string='Physical Server', ondelete='cascade')
    name = fields.Char(string="IP v4", required=True)
    ip_v6 = fields.Char(string="IP v6")
    is_public = fields.Boolean(string="Is Public")
    type = fields.Selection([('managing_ip', 'Managing IP'), ('working_ip', 'Working IP')], string="Type", required=True, default='working_ip')
