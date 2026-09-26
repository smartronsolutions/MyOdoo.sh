from odoo import fields, models


class ServiceMixin(models.AbstractModel):
    _name = 'saas.service.mixin'
    _inherit = ['saas.ssh.mixin']
    _description = "SaaS Service Mixin"

    pserver_id = fields.Many2one('saas.pserver', string="Physical Server", required=True, copy=False)
    working_ip_id = fields.Many2one('saas.pserver.ip', string="Working IP", required=True, copy=False)

    def _get_service_name(self):
        raise "Cannot find service name. You must implement _get_service_name method"

    def _get_service_status(self):
        cmd = ''
        if self.pserver_id.version_16_plus:
            cmd = "systemctl status %s" % self._get_service_name()
        else:
            cmd = "service %s status" % self._get_service_name()
        ssh = self.pserver_id._connect()
        output = self._exec_cmd(cmd, ssh, without_return=False)
        ssh.close()
        return output
