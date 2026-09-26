from odoo import fields, models, api


class Partner(models.Model):
    _inherit = 'res.partner'

    instance_ids = fields.One2many('saas.odoo.instance', 'partner_id', string='Odoo Instances', groups="s_odoo_saas_master.group_odoo_saas_user")
    instance_count = fields.Integer(string='Instance Count', compute='_compute_instance_count', compute_sudo=True)
    trial_instance_count = fields.Integer(string='Trial Instance Count', compute='_compute_trial_instance_count', compute_sudo=True)
    github_login = fields.Char(string='GitHub Username')
    github_oauth_token = fields.Char(string='GitHub OAuth Token')
    github_avatar_url = fields.Char(string='GitHub Avatar URL')

    @api.depends('instance_ids')
    def _compute_instance_count(self):
        instance_data = self.env['saas.odoo.instance']._read_group([('partner_id', 'in', self.ids)], ['partner_id'], ['__count'])
        result = {p.id: count for p, count in instance_data}
        for r in self:
            r.instance_count = result.get(r.id, 0)

    @api.depends('instance_ids', 'instance_ids.trial')
    def _compute_trial_instance_count(self):
        instance_data = self.env['saas.odoo.instance']._read_group([('partner_id', 'in', self.ids), ('trial', '=', True)], ['partner_id'], ['__count'])
        result = {p.id: count for p, count in instance_data}
        for r in self:
            r.trial_instance_count = result.get(r.id, 0)

    def action_view_instance(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_action')
        action['domain'] = [('partner_id', '=', self.id)]
        action['context'] = {'default_partner_id': self.id}
        return action
