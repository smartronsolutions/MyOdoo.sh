from odoo import fields, models


class InstanceUpgradeModuleWizard(models.TransientModel):
    _name = 'saas.odoo.instance.upgrade.module.wizard'
    _description = "SaaS Odoo Instance Upgrade Module Wizard"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    module = fields.Char(string='Module to Upgrade', required=True, default='all', help="Modules are separated by commas")
    type = fields.Selection([
        ('upgrade', 'Upgrade Module'),
        ('install', 'Install Module')
    ], string='Type', required=True, default='upgrade')

    def action_apply(self):
        if self.type == 'upgrade':
            self.instance_id.action_upgrade_modules(self.module)
        elif self.type == 'install':
            self.instance_id.action_install_modules(self.module)
