from odoo import fields, models


class InstanceRedeployWizard(models.TransientModel):
    _name = 'saas.odoo.instance.redeploy.wizard'
    _description = "SaaS Odoo Instance Redeploy Wizard"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    redeploy_type = fields.Selection([
        ('config', 'Redeploy Config'),
        ('nginx', 'Redeploy Nginx'),
    ], string='Redeploy Type', required=True)

    def action_redeploy(self):
        if self.redeploy_type == 'config':
            return self.instance_id.action_redeploy_config()
        elif self.redeploy_type == 'nginx':
            return self.instance_id.action_redeploy_nginx()
