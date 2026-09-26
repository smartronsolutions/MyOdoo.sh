from odoo import fields, models, api


class OdooServerExtraAddons(models.Model):
    _name = 'saas.odoo.server.extra.addon'
    _description = "SaaS Odoo Server Extra Addons"
    _rec_name = 'source_path'

    odoo_server_id = fields.Many2one('saas.odoo.server', string='Odoo Server', required=True, ondelete='cascade')
    source_path = fields.Char(string='Source Path', required=True)
    docker_container_path = fields.Char(string="Docker Container Path", compute='_compute_docker_container_path', store=True)
    description = fields.Char(string='Description', compute='_compute_description', store=True)

    @api.depends('source_path', 'odoo_server_id.extra_addon_ids')
    def _compute_docker_container_path(self):
        for r in self:
            r.docker_container_path = ''
            if r.source_path:
                # Get the index of this addon in the server's extra addons
                addon_index = 0
                for i, addon in enumerate(r.odoo_server_id.extra_addon_ids):
                    if addon.id == r.id:
                        addon_index = i
                        break
                
                if addon_index == 0:
                    r.docker_container_path = '/mnt/standard-extra-addons/'
                else:
                    r.docker_container_path = '/mnt/standard-extra-addons-%d/' % (addon_index + 1)

    @api.depends('source_path')
    def _compute_description(self):
        for r in self:
            r.description = ''
            if r.source_path:
                r.description = 'copy %s -> /home/instance_technical_name/custom-addons' % r.source_path
