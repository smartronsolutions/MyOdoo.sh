from odoo import fields, models, api, _
from odoo.exceptions import UserError


class OdooInstanceCustomAddon(models.Model):
    _name = 'saas.odoo.instance.custom.addon'
    _description = "SaaS Odoo Instance Custom Addon"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete="cascade")
    url = fields.Char(string='Instance URL', related='instance_id.url', store=True)
    name = fields.Char(string='Folder Name', required=True)
    addon_path = fields.Char(string='Addon Path', compute='_compute_addon_path', store=True)
    container_path = fields.Char(string='Container Path', compute='_compute_container_path', store=True)
    clone_uri = fields.Char(string='Clone URI', required=True)
    branch = fields.Char(string='Branch', required=True, default='main')
    cloned = fields.Boolean(string='Cloned', readonly=True)

    @api.depends('instance_id.technical_name', 'name')
    def _compute_addon_path(self):
        for r in self:
            addon_path = '/home/'
            if r.instance_id and r.name:
                addon_path += r.instance_id.technical_name + '/custom-addons/' + r.name
            r.addon_path = addon_path

    @api.depends('name')
    def _compute_container_path(self):
        for r in self:
            r.container_path = ''
            if r.name:
                r.container_path = '/mnt/extra-addons/%s' % r.name

    @api.ondelete(at_uninstall=False)
    def unlink_exception(self):
        for r in self:
            if r.cloned:
                raise UserError(_("You can not delete custom addon cloned. You must first remote it"))

    def action_clone(self):
        # Allow syncing custom addons while the instance is deployed OR suspended
        # (its containers may still be running), so "Sync & Deploy" never silently
        # skips the clone.
        if self.instance_id.state not in ('deploy', 'suspend'):
            raise UserError(_("You can only clone custom addon of a deployed or suspended instance"))

        config = self.instance_id.config_ids.filtered(lambda c: c.name == 'addons_path')
        if config:
            current_paths = [p.strip() for p in (config.value or '').split(',') if p.strip()]
            if self.container_path not in current_paths:
                current_paths.append(self.container_path)
                config.write({'value': ','.join(current_paths)})
        self.instance_id.pserver_id._clone_customer_addons(self)
        self.instance_id.action_redeploy_config()
        self.write({'cloned': True})

    def action_pull(self, trigger='manual'):
        if self.instance_id.need_to_compose_up:
            self.instance_id.pserver_id._docker_compose_up(self.instance_id)
        self.instance_id.pserver_id._pull_customer_addons(self, trigger=trigger)

    def action_remove(self):
        config = self.instance_id.config_ids.filtered(lambda c: c.name == 'addons_path')
        if config:
            clone_path_index = config.value.find(self.container_path)
            value = False
            if clone_path_index == 0:
                if len(config.value.split(',')) > 1:
                    value = config.value.replace('%s,' % (self.container_path), '')
                else:
                    value = config.value.replace('%s' % (self.container_path), '/mnt/extra-addons')
            elif clone_path_index == -1:
                value = config.value
            else:
                value = config.value.replace(',%s' % (self.container_path), '')
            config.write({'value': value})

        self.instance_id.pserver_id._remove_customer_addons(self)
        self.instance_id.action_redeploy_config()
        self.write({'cloned': False})
