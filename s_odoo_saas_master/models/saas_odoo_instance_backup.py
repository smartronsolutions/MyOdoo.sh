import os
import threading
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.modules.registry import Registry


class OdooInstanceBackup(models.Model):
    _name = 'saas.odoo.instance.backup'
    _description = "SaaS Odoo Instance Backup"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    odoo_version_id = fields.Many2one('saas.odoo.version', string='Odoo Version',
        help="Odoo version of the instance at the time this backup was taken. Restoring is only allowed to instances of the same version.")
    name = fields.Char(string="Name", required=True)
    datetime = fields.Datetime(string="Datetime")
    file_path = fields.Char(string="File Path")
    file_size = fields.Float(string="File Size (MB)")
    active = fields.Boolean(string="Active", default=True)
    description = fields.Text(string="Description")
    container_backup_id = fields.Many2one(
        'saas.odoo.instance.container.backup', string='Container Backup', readonly=True,
        ondelete='set null'
    )
    format = fields.Selection([
        ('zip', 'zip (includes filestore)'),
        ('dump', 'pg_dump (without filestore)')
    ], string='Backup Format', default='zip')
    backup_type = fields.Selection([
        ('manual', 'Manual'),
        ('auto', 'Automatic'),
    ], string='Type', default='manual', required=True)
    restore_state = fields.Selection([
        ('none', 'Not Restored'),
        ('running', 'Restoring'),
        ('done', 'Restored'),
        ('failed', 'Failed'),
    ], string='Restore Status', default='none', copy=False)
    restore_instance_id = fields.Many2one('saas.odoo.instance', string='Restored To', copy=False)
    restore_start_datetime = fields.Datetime(string='Restore Started On', copy=False)
    restore_end_datetime = fields.Datetime(string='Restore Ended On', copy=False)
    restore_error_message = fields.Text(string='Restore Error', copy=False)
    restore_status_message = fields.Char(string='Restore Status Message', compute='_compute_restore_status_message')

    def _compute_restore_status_message(self):
        now = fields.Datetime.now()
        for rec in self:
            if rec.restore_state == 'running' and rec.restore_start_datetime:
                minutes = max(int((now - rec.restore_start_datetime).total_seconds() // 60), 0)
                if minutes <= 0:
                    rec.restore_status_message = _(
                        "You have started restore to %s just now, please wait, it's on the way..."
                    ) % (rec.restore_instance_id.display_name or '')
                else:
                    rec.restore_status_message = _(
                        "You have started restore to %s before %s minute(s), it's on the way. Please wait..."
                    ) % (rec.restore_instance_id.display_name or '', minutes)
            elif rec.restore_state == 'done':
                message = _(
                    "Your backup was restored to %s. It's done."
                ) % (rec.restore_instance_id.display_name or '')
                if rec.restore_error_message:
                    message = "%s %s" % (message, rec.restore_error_message)
                rec.restore_status_message = message
            elif rec.restore_state == 'failed':
                rec.restore_status_message = _("Restore failed: %s") % (rec.restore_error_message or '')
            else:
                rec.restore_status_message = ''

    def action_open_restore_wizard(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            's_odoo_saas_master.saas_odoo_instance_backup_restore_wizard_action'
        )
        action['context'] = {
            'default_backup_id': self.id,
            'default_instance_id': self.instance_id.id,
        }
        return action

    def action_restore_to(self, instance):
        self.ensure_one()
        if self.restore_state == 'running':
            raise UserError(_("A restore for this backup is already in progress."))
        if not self.file_path or not os.path.exists(self.file_path):
            raise UserError(_("Backup file not found on disk: %s") % (self.file_path or ''))
        if instance.state != 'deploy' or not instance.pserver_id:
            raise UserError(_("Target instance must be deployed before restoring a backup."))
        if self.odoo_version_id and instance.odoo_version_id != self.odoo_version_id:
            raise UserError(_(
                "This backup was taken from a %s instance and can only be restored to an instance of the same version."
            ) % self.odoo_version_id.name)

        self.sudo().write({
            'restore_state': 'running',
            'restore_instance_id': instance.id,
            'restore_start_datetime': fields.Datetime.now(),
            'restore_end_datetime': False,
            'restore_error_message': False,
        })
        self.env.cr.commit()

        backup_id = self.id
        instance_id = instance.id
        dbname = self.env.cr.dbname
        uid = self.env.uid

        def run_restore():
            registry = Registry(dbname)
            with registry.cursor() as cr:
                env = api.Environment(cr, uid, {})
                backup = env['saas.odoo.instance.backup'].sudo().browse(backup_id)
                target = env['saas.odoo.instance'].sudo().browse(instance_id)
                try:
                    warning = target.pserver_id._restore_odoo_instance_backup(target, backup)
                    backup.write({
                        'restore_state': 'done',
                        'restore_end_datetime': fields.Datetime.now(),
                        'restore_error_message': warning or False,
                    })
                except Exception as e:
                    backup.write({
                        'restore_state': 'failed',
                        'restore_end_datetime': fields.Datetime.now(),
                        'restore_error_message': str(e) or repr(e),
                    })
                cr.commit()

        threading.Thread(target=run_restore, daemon=True).start()

    def unlink(self):
        files_to_delete = []
        for rec in self:
            if rec.file_path and os.path.exists(rec.file_path):
                files_to_delete.append(rec.file_path)

        result = super(OdooInstanceBackup, self).unlink()
        for file in files_to_delete:
            os.remove(file)
        return result

    def action_download(self):
        return {
            'type': 'ir.actions.act_url',
            'target': '_blank',
            'url': '/saas_backup/download/%s' % self.id,
        }

    def action_download_container_backup(self):
        self.ensure_one()
        if not self.container_backup_id:
            raise UserError(_("No container backup is linked to this database backup."))
        return self.container_backup_id.action_download()
