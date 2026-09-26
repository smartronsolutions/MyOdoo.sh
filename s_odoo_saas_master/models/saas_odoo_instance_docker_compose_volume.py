from odoo import fields, models, api


class OdooInstanceDockerComposeVolume(models.Model):
    _name = 'saas.odoo.instance.docker.compose.volume'
    _description = "SaaS Odoo Instance Docker Compose Volume"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    container_id = fields.Many2one('saas.odoo.instance.docker.container', string='Docker Container')
    name = fields.Char(string='Volume Name', required=True)
    volume_type = fields.Selection([
        ('odoo_config', 'Odoo Config'),
        ('odoo_custom_addons', 'Odoo Custom Addons'),
        ('odoo_extra_addons', 'Odoo Standard Extra Addons'),
        ('odoo_log', 'Odoo Log File'),
        ('odoo_filestore', 'Odoo Filestore'),
        ('pgdata', 'PGDATA'),
    ], string='Volume Type', required=True, default='normal')
    container_path = fields.Char(string='Container Path', required=True)
    storage_path = fields.Char(string='Storage Path', compute='_compute_storage_path', store=True)

    @api.depends('instance_id', 'instance_id.technical_name', 'name')
    def _compute_storage_path(self):
        for r in self:
            if r.instance_id and r.instance_id.technical_name and r.name:
                r.storage_path = '/home/%s/%s' % (r.instance_id.technical_name, r.name.strip().replace(" ", "-").replace(".", "-"))
            else:
                r.storage_path = ''
