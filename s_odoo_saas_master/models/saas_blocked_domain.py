from odoo import fields, models


class BlockedDomain(models.Model):
    _name = 'saas.blocked.domain'
    _description = "SaaS Blocked Domain"

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(string="Active", default=True)
    based_domain_id = fields.Many2one('saas.based.domain', string='Based Domain')

    _sql_constraints = [
        ('name_uniq', 'unique(name,based_domain_id)', 'Blocked Domain must be uniqued per based domain!')
    ]
