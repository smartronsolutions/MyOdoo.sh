import os

from odoo import fields, models


class OdooInstanceContainerBackup(models.Model):
    _name = 'saas.odoo.instance.container.backup'
    _description = 'SaaS Odoo Instance Container Backup'
    _order = 'datetime desc, id desc'

    instance_id = fields.Many2one(
        'saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade'
    )
    name = fields.Char(required=True)
    datetime = fields.Datetime(required=True)
    file_path = fields.Char(required=True)
    file_size = fields.Float(string='File Size (MB)')

    def action_download(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'target': '_blank',
            'url': '/saas_container_backup/download/%s' % self.id,
        }

    def unlink(self):
        files_to_delete = [
            record.file_path for record in self
            if record.file_path and os.path.isfile(record.file_path)
        ]
        result = super().unlink()
        for file_path in files_to_delete:
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass
        return result
