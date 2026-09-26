from odoo import fields, models, api


class OdooVersion(models.Model):
    _name = 'saas.odoo.version'
    _description = "SaaS Odoo Version"
    _order = 'sequence'

    name = fields.Char(string="Odoo Version", required=True)
    version = fields.Integer(string='Odoo Version (Integer)', compute='_compute_version', store=True)
    sequence = fields.Integer('Sequence', default=10, required=True)
    docker_image_tag = fields.Char(string='Docker Image Tag', required=True)
    config_ids = fields.One2many('saas.odoo.version.config', 'odoo_version_id', string='Configs')
    active = fields.Boolean(string="Active", default=True)

    @api.depends('docker_image_tag')
    def _compute_version(self):
        for r in self:
            r.version = 0
            if r.docker_image_tag:
                r.version = int(r.docker_image_tag.split(".")[0])

    def action_view_odoo_version_config(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_version_config_action')
        action['context'] = {'default_odoo_version_id': self.id}
        action['domain'] = [('odoo_version_id', '=', self.id)]
        return action
