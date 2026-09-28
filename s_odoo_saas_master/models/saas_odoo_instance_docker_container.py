from odoo import fields, models, api, _
from odoo.exceptions import UserError


class OdooIstanceDockerContainer(models.Model):
    _name = 'saas.odoo.instance.docker.container'
    _description = "SaaS Odoo Instance Docker Container"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    name = fields.Char(string='Name', required=True)
    image = fields.Char(string='Image', required=True)
    container_type = fields.Selection([
        ('odoo', 'Odoo'),
        ('psql', 'PSQL')
    ], string='Container Type', required=True)
    ports = fields.Char(string='Ports', compute='_compute_ports', store=True)
    docker_compose_volume_ids = fields.One2many('saas.odoo.instance.docker.compose.volume', 'container_id', string='Volumes')
    state = fields.Selection([
        ('unknown', 'Unknown'),
        ('run', 'Running'),
        ('stop', 'Stopped')
    ], string="Status", compute='_compute_state', compute_sudo=True)

    @api.depends(
        'container_type', 'instance_id',
        'instance_id.docker_xmlrpc_expose_port', 'instance_id.docker_xmlrpcs_expose_port', 'instance_id.docker_longpolling_expose_port',
        'instance_id.docker_xmlrpc_container_port', 'instance_id.docker_xmlrpcs_container_port', 'instance_id.docker_longpolling_container_port')
    def _compute_ports(self):
        for r in self:
            ports = 'Unknown'
            if r.instance_id:
                if r.container_type == 'odoo' and r.instance_id.docker_xmlrpc_expose_port:
                    ports = '%s->%s' % (r.instance_id.docker_xmlrpc_expose_port, r.instance_id.docker_xmlrpc_container_port)
                    ports += ', %s->%s' % (r.instance_id.docker_xmlrpcs_expose_port, r.instance_id.docker_xmlrpcs_container_port)
                    ports += ', %s->%s' % (r.instance_id.docker_longpolling_expose_port, r.instance_id.docker_longpolling_container_port)
                elif r.container_type == 'psql':
                    ports = '5432'
                r.ports = ports

    def _compute_state(self):
        results = {}
        for pserver in self.instance_id.pserver_id:
            containers = self.instance_id.filtered(lambda i: i.pserver_id == pserver and i.state in ('deploy', 'suspend')).docker_container_ids
            results.update(pserver._get_container_status(containers))
        for r in self:
            status = results.get(r.name, False)
            if status == 'running':
                r.state = 'run'
            elif status == 'exited':
                r.state = 'stop'
            else:
                r.state = 'unknown'

    def _connect_instance(self, instance):
        ssh = instance.pserver_id._connect()
        if not ssh:
            raise UserError(
                _("Cannot connect to server %s. Please check server information and SSH Key Pair.")
                % instance.pserver_id.display_name
            )
        return ssh

    def action_restart(self):
        for instance in self.instance_id:
            ssh = self._connect_instance(instance)
            try:
                instance.pserver_id._compose_exec(instance, "restart", ssh)
            finally:
                if ssh:
                    ssh.close()

    def action_stop(self):
        for instance in self.instance_id:
            ssh = self._connect_instance(instance)
            try:
                # Plain ``docker stop`` on this instance's own containers (see
                # PServer._docker_stop_instance). It stops them and nothing else: no
                # ``compose down``, so the containers, their network and their data are all
                # left in place and starting again brings the same containers back.
                instance.pserver_id._docker_stop_instance(instance, ssh)
            finally:
                if ssh:
                    ssh.close()

        for instance in self.instance_id:
            if all(c.state == 'stop' for c in instance.docker_container_ids):
                instance.write({'operation_state': 'stop'})

    def action_start(self):
        for instance in self.instance_id:
            ssh = self._connect_instance(instance)
            try:
                # Use compose start to ensure all services start consistently
                instance.pserver_id._compose_exec(instance, "start", ssh)
            finally:
                if ssh:
                    ssh.close()

        for instance in self.instance_id:
            if all(c.state == 'run' for c in instance.docker_container_ids):
                instance.write({'state': 'deploy', 'operation_state': 'run'})
