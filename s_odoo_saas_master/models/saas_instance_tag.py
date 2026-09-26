from odoo import fields, models


class InstanceTag(models.Model):
    _name = "saas.instance.tag"
    _description = "SaaS Instance Tag"
    _order = "name"

    name = fields.Char(string="Tag Name", required=True, translate=True)
    color = fields.Integer(string="Color Index", default=0)
