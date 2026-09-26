from odoo import fields, models, _
from odoo.exceptions import ValidationError


class OdooInstanceBackupRestoreWizard(models.TransientModel):
    _name = 'saas.odoo.instance.backup.restore.wizard'
    _description = "SaaS Odoo Instance Backup Restore Wizard"

    backup_id = fields.Many2one('saas.odoo.instance.backup', string='Backup', required=True, ondelete='cascade')
    odoo_version_id = fields.Many2one(related='backup_id.odoo_version_id', string='Backup Odoo Version')
    restore_state = fields.Selection(related='backup_id.restore_state', string='Restore Status')
    restore_status_message = fields.Char(related='backup_id.restore_status_message', string='Restore Status Message')
    instance_id = fields.Many2one('saas.odoo.instance', string='Restore To Instance', required=True,
        domain=[('state', '=', 'deploy'), ('is_template', '=', False)])
    confirmation = fields.Char(string="Type 'yes' to confirm")
    # Tracks which panel THIS wizard dialog should show. Kept separate from restore_state
    # (which reflects the backup's own history) so that reopening the wizard for a backup
    # that was already restored before still starts at the confirmation form instead of
    # jumping straight to the previous attempt's result.
    wizard_step = fields.Selection([
        ('confirm', 'Confirm'),
        ('progress', 'Progress'),
    ], string='Wizard Step', default='confirm')

    def action_restore(self):
        self.ensure_one()
        if self.confirmation != 'yes':
            raise ValidationError(_("Please enter 'yes' in the confirmation box before restoring the backup."))
        self.backup_id.action_restore_to(self.instance_id)
        self.write({'wizard_step': 'progress'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
