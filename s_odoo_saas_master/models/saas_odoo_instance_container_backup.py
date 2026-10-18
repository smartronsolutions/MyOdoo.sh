import os

from odoo import api, fields, models


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

    def _refresh_storage_live(self):
        """Recompute the storage usage of the instances behind these backups, at once."""
        for instance in self.mapped('instance_id'):
            refresh = getattr(instance, '_refresh_storage_usage', None)
            if refresh:
                try:
                    refresh()
                except Exception:  # never break a backup because of a size refresh
                    pass

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Live storage: a new container backup counts in the usage immediately.
        records._refresh_storage_live()
        return records

    def unlink(self):
        instances = self.mapped('instance_id')
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
        for instance in instances:
            refresh = getattr(instance, '_refresh_storage_usage', None)
            if refresh:
                try:
                    refresh()
                except Exception:
                    pass
        return result
