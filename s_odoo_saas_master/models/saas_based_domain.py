from odoo import fields, models


class BasedDomain(models.Model):
    _name = 'saas.based.domain'
    _description = "SaaS Based Domain"
    _order = 'sequence'

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(string="Active", default=True)
    descripsion = fields.Char(string="Description")
    forward_ip = fields.Char(string="Forward IP", required=True)
    reverse_ip = fields.Char(string="Reverse IP")
    sequence = fields.Integer('Sequence', default=10, required=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'The selected Based Domain Name must be uniqued!')
    ]
