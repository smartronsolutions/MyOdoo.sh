from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    instance_starting_port = fields.Integer(related='company_id.instance_starting_port', readonly=False)
    instance_backup_limit = fields.Integer(related='company_id.instance_backup_limit', readonly=False, default_model='saas.odoo.instance')
    backup_directory = fields.Char(related='company_id.backup_directory', readonly=False)
    instance_trial_day = fields.Integer(related='company_id.instance_trial_day', readonly=False, default_model='saas.odoo.instance')
    notification_expiration_day = fields.Integer(related='company_id.notification_expiration_day', readonly=False)
    revoke_instance_day = fields.Integer(related='company_id.revoke_instance_day', readonly=False)
    limit_trial = fields.Integer(related='company_id.limit_trial', readonly=False)
    ssh_username = fields.Char(related='company_id.default_ssh_username', readonly=False)
    resource_package_id = fields.Many2one(related='company_id.resource_package_id', readonly=False)
    github_client_id = fields.Char(related='company_id.github_client_id', readonly=False)
    github_client_secret = fields.Char(related='company_id.github_client_secret', readonly=False)
    saas_price_currency_id = fields.Many2one(related='company_id.saas_price_currency_id', readonly=False)
    saas_price_rate = fields.Float(related='company_id.saas_price_rate', readonly=False)
