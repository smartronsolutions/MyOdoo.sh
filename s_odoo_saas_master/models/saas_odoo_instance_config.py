from odoo import fields, models, api, _
from odoo.exceptions import ValidationError

from .saas_odoo_instance import WORKER_MEMORY_SOFT, WORKER_MEMORY_HARD


class OdooInstanceConfig(models.Model):
    _name = 'saas.odoo.instance.config'
    _description = "SaaS Odoo Instance Config"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    odoo_version_id = fields.Many2one(related='instance_id.odoo_version_id')
    name = fields.Char(string='Key', required=True)
    value = fields.Char(string='Value')
    section_id = fields.Many2one('saas.odoo.version.config.section', string="Section", required=True)

    def _sync_workers_to_instance(self):
        """Keep the instance in sync when the ``workers`` config row is edited.

        An admin can change the deployed worker count right from the backend
        ("Configs" of the instance). Without this, the portal would keep showing the
        old ``workers_count``. We also refresh the per-worker memory rows and clamp
        the value to the maximum allowed per instance.
        """
        if self.env.context.get('skip_worker_config_sync'):
            return
        for record in self.filtered(lambda r: r.name == 'workers' and r.instance_id):
            instance = record.instance_id
            try:
                workers = int(record.value or 1)
            except (TypeError, ValueError):
                continue
            workers = max(1, min(workers, instance.MAX_WORKERS_PER_INSTANCE))
            if record.value != str(workers):
                record.with_context(skip_worker_config_sync=True).value = str(workers)
            if instance.workers_count != workers:
                instance.workers_count = workers
            soft = instance.config_ids.filtered(lambda c: c.name == 'limit_memory_soft')
            hard = instance.config_ids.filtered(lambda c: c.name == 'limit_memory_hard')
            for rows, per_worker in ((soft, WORKER_MEMORY_SOFT), (hard, WORKER_MEMORY_HARD)):
                ctx_rows = rows.with_context(skip_worker_config_sync=True)
                new_value = str(workers * per_worker)
                if ctx_rows and ctx_rows[0].value != new_value:
                    ctx_rows.write({'value': new_value})

    def _sync_admin_pass_to_instance(self):
        """Mirror an ``admin_passwd`` row back onto the instance's Master Password field.

        The instance field is the one that knows how to rewrite odoo.conf and restart
        the container, so keep it in sync when an admin edits the raw config row.
        """
        if self.env.context.get('skip_admin_pass_sync'):
            return
        for record in self.filtered(lambda r: r.name == 'admin_passwd' and r.instance_id):
            instance = record.instance_id
            if instance.admin_pass != record.value:
                instance.with_context(skip_admin_pass_sync=True).admin_pass = record.value

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_workers_to_instance()
        records._sync_admin_pass_to_instance()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'value' in vals or 'name' in vals:
            self._sync_workers_to_instance()
            self._sync_admin_pass_to_instance()
        return res

    @api.model
    def _get_config_file_content(self, instance):
        file_content = ''
        sections = instance.config_ids.mapped('section_id')
        for section in sections:
            file_content += '[' + section.name + ']' + '\n'
            configs = instance.config_ids.filtered(lambda c: c.section_id == section)
            for config in configs:
                file_content += config.name + '=' + config.value + '\n'
        return file_content

    @api.model
    def _get_config_file_path(self, instance):
        file_paths = instance.docker_compose_volume_ids.filtered(lambda v: v.volume_type == 'odoo_config')
        if not file_paths:
            raise ValidationError(_("Cannot find path to config file."))
        return file_paths[0].storage_path + '/odoo.conf'
