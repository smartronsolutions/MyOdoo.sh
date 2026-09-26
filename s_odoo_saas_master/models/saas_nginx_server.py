from odoo import fields, models, _
from odoo.exceptions import UserError


class Nginx(models.Model):
    _name = 'saas.nginx.server'
    _inherit = ['saas.service.mixin']
    _description = "SaaS Nginx"

    name = fields.Char(string='Name', required=True)
    sites_available_path = fields.Char(string="Sites Available Path", required=True, default='/etc/nginx/sites-available')
    sites_enabled_path = fields.Char(string="Sites Enabled Path", required=True, default='/etc/nginx/sites-enabled')
    ssl_path = fields.Char(string="SSL Path", required=True, default='/etc/nginx/ssl')
    can_edit_ssl_path = fields.Boolean(string="Can Edit SSL Path", compute="_compute_can_edit_ssl_path")
    access_log_path = fields.Char(string="Access Log Path", required=True, default='/var/log/nginx')
    error_log_path = fields.Char(string="Error Log Path", required=True, default='/var/log/nginx')
    pagespeed_cache_path = fields.Char(string="Pagespeed Cache Path", required=True, default='/var/pagespeed')
    active = fields.Boolean(string="Active", default=True)

    _sql_constraints = [
        ('pserver_id_unique',
         'UNIQUE(pserver_id)',
         "Nginx must be unique per physical server."),
    ]

    def _compute_can_edit_ssl_path(self):
        """Check if the current user can edit SSL path."""
        is_saas_master = self.env.user.has_group('s_odoo_saas_master.group_odoo_saas_master')
        for record in self:
            record.can_edit_ssl_path = is_saas_master

    def _get_service_name(self):
        return 'nginx'

    def action_check_status(self):
        self.ensure_one()
        status = self._get_service_status()
        if not status:
            raise UserError(_("Error in check nginx status. Please verify server and nginx information"))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': status,
                'type': 'success',
                'sticky': False,
            }
        }
