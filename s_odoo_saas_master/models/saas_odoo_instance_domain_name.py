from odoo import fields, models, api, _
from odoo.exceptions import UserError


class OdooInstanceDomainName(models.Model):
    _name = 'saas.odoo.instance.domain.name'
    _description = "SaaS Odoo Instance Domain Name"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    name = fields.Char(string='Domain Name', required=True)
    is_instance_domain_name = fields.Boolean(string='Is Instance Domain Name?', default=False, readonly=True)
    noindex = fields.Boolean(string='No Index')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('deploy', 'Deployed'),
        ('cancel', 'Cancelled')
    ], string='Status', required=True, readonly=True, index=True, default='draft')
    instance_state = fields.Selection(related='instance_id.state', string='Odoo Instance Status')

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'Domain Name must be unique!')
    ]

    @api.constrains('name')
    def _check_name(self):
        for r in self:
            if not r.is_instance_domain_name:
                existed_instance = self.env['saas.odoo.instance'].search([('domain_name', '=', r.name)], limit=1)
                if existed_instance:
                    raise UserError(_("Domain name %s already belongs to Odoo Instance %s") % (r.name, existed_instance.name))

    def action_deploy(self):
        self.instance_id.pserver_id._deploy_nginx(self)
        self.write({'state': 'deploy'})

    def action_cancel(self):
        for r in self:
            if r.is_instance_domain_name:
                raise UserError(_("You cannot cancel domain name %s. Because it is own Odoo Instance, you should cancel Odoo Instance if you want.") % r.name)

        self.instance_id.pserver_id._cancel_nginx(self)
        self.write({'state': 'cancel'})

    def action_draft(self):
        self.write({'state': 'draft'})

    @api.ondelete(at_uninstall=False)
    def unlink_exception(self):
        for r in self:
            if r.is_instance_domain_name:
                raise UserError(_("You can not delete domain name %s. Because it is own Odoo Instance") % r.name)
            if r.state not in ('draft', 'cancel'):
                raise UserError(_("You can not delete domain name %s. You must first cancel or set it to draft") % r.name)

    def _get_nginx_file_content(self):
        # declaration
        file_content = ''
        domain_name = self.name
        domain_nodot = domain_name.replace('.', '_')
        nginx_server = self.instance_id.odoo_server_id.nginx_server_id
        working_ip = nginx_server.working_ip_id.name
        xmlrpc_port = self.instance_id.docker_xmlrpc_expose_port
        longpolling_port = self.instance_id.docker_longpolling_expose_port
        odoo_version = self.instance_id.odoo_version_id.version
        # end declaration

        file_content += 'upstream ' + domain_nodot + '{\n'
        file_content += '\tserver %s:%s weight=1 max_fails=5 fail_timeout=10s;\n' % (working_ip, xmlrpc_port)
        file_content += '\tkeepalive 32;\n'
        file_content += '}\n'
        file_content += '\n'

        if odoo_version >= 16:
            file_content += 'map $http_upgrade $conn_upgrade_' + domain_nodot + ' {\n'
            file_content += '\tdefault upgrade;\n'
            file_content += "\t''      close;\n"
            file_content += '}\n'

        file_content += 'server {\n'
        file_content += '\tlisten 80;\n'
        file_content += '\tserver_name www.' + domain_name + ';\n'
        file_content += '\treturn 301 $scheme://' + domain_name + '$request_uri;\n'
        file_content += '}\n'

        file_content += 'server {\n'
        file_content += '\tlisten 80;\n'
        file_content += '\tserver_name ' + domain_name + ';\n'
        file_content += '\taccess_log ' + nginx_server.access_log_path + '/access_' + domain_name + '.log combined;\n'
        file_content += '\terror_log ' + nginx_server.error_log_path + '/error_' + domain_name + '.log;\n'
        file_content += '\tclient_max_body_size 200m;\n'
        file_content += '\tkeepalive_timeout 600;\n'
        file_content += '\t# increase proxy buffer to handle some Odoo web requests\n'
        file_content += '\tproxy_buffers 16 64k;\n'
        file_content += '\tproxy_buffer_size 128k;\n'
        file_content += "\tadd_header 'Content-Security-Policy' 'upgrade-insecure-requests';\n"
        if self.noindex:
            file_content += "\tadd_header 'X-Robots-Tag' 'noindex, nofollow' always;\n"
        # Keep these at server level: locations that do not declare their own
        # proxy_set_header (e.g. /web/static) inherit them.
        file_content += "\tproxy_set_header X-Forwarded-Host $host;\n"
        file_content += "\tproxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        file_content += "\tproxy_set_header X-Forwarded-Proto $scheme;\n"
        file_content += "\tproxy_set_header X-Real-IP $remote_addr;\n"
        file_content += '\n'
        file_content += '\tlocation / {\n'
        file_content += '\t\tproxy_pass http://' + domain_nodot + ';\n'
        file_content += '\t\tproxy_http_version 1.1;\n'
        file_content += '\t\tproxy_connect_timeout 600s;\n'
        file_content += '\t\tproxy_send_timeout 600s;\n'
        file_content += '\t\tproxy_read_timeout 600s;\n'
        file_content += '\t\tsend_timeout  600s;\n'
        file_content += '\t\tproxy_next_upstream error timeout invalid_header http_500 http_502 http_503;\n'
        file_content += '\t\t# nginx stops inheriting the server-level proxy_set_header directives\n'
        file_content += '\t\t# as soon as a location declares its own, so every forwarded header has to\n'
        file_content += '\t\t# be repeated here. Without X-Forwarded-Proto Odoo sees the request as plain\n'
        file_content += '\t\t# HTTP (non-Secure session cookie, http:// editor URLs) and the website\n'
        file_content += '\t\t# editor hangs on an HTTPS domain.\n'
        file_content += '\t\tproxy_set_header Host $host;\n'
        file_content += '\t\tproxy_set_header X-Real-IP $remote_addr;\n'
        file_content += '\t\tproxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
        file_content += '\t\tproxy_set_header X-Forwarded-Host $host;\n'
        file_content += '\t\tproxy_set_header X-Forwarded-Proto $scheme;\n'
        file_content += '\t\tproxy_set_header Connection "";\n'
        file_content += '\t\t# by default, do not forward anything\n'
        file_content += '\t\tproxy_redirect off;\n'
        file_content += '\t}\n'
        file_content += '\n'

        if odoo_version >= 16:
            # Odoo >= 16 keeps the realtime bus on /websocket. nginx proxies with
            # HTTP/1.0 by default, which makes the WebSocket Upgrade impossible
            # (Odoo answers 400 and the website editor / bus hang). Force HTTP/1.1,
            # forward the real Host and disable buffering on this long-lived stream.
            file_content += "\tlocation /websocket {\n"
            file_content += "\t\tproxy_pass http://127.0.0.1:%s;\n" % (longpolling_port)
            file_content += "\t\tproxy_http_version 1.1;\n"
            file_content += "\t\tproxy_set_header Host $host;\n"
            file_content += "\t\tproxy_set_header X-Forwarded-Host $host;\n"
            file_content += "\t\tproxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            file_content += "\t\tproxy_set_header X-Forwarded-Proto $scheme;\n"
            file_content += "\t\tproxy_set_header X-Real-IP $remote_addr;\n"
            file_content += "\t\tproxy_set_header Upgrade $http_upgrade;\n"
            file_content += "\t\tproxy_set_header Connection $conn_upgrade_" + domain_nodot + ";\n"
            file_content += "\t\tproxy_buffering off;\n"
            file_content += "\t\tproxy_read_timeout 3600s;\n"
            file_content += "\t\tproxy_send_timeout 3600s;\n"
            file_content += "\t}\n"
            file_content += '\n'
        else:
            file_content += "\tlocation /longpolling {\n"
            file_content += "\t\tproxy_pass http://127.0.0.1:%s;\n" % (longpolling_port)
            file_content += "\t\tproxy_http_version 1.1;\n"
            file_content += "\t\tproxy_set_header Host $host;\n"
            file_content += "\t\tproxy_set_header X-Forwarded-Host $host;\n"
            file_content += "\t\tproxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            file_content += "\t\tproxy_set_header X-Forwarded-Proto $scheme;\n"
            file_content += "\t\tproxy_set_header X-Real-IP $remote_addr;\n"
            file_content += "\t\tproxy_redirect off;\n"
            file_content += "\t}\n"
            file_content += '\n'

        file_content += '\tlocation ~* /web/static {\n'
        file_content += '\t\tproxy_cache_valid 200 120m;\n'
        file_content += '\t\tproxy_buffering on;\n'
        file_content += '\t\texpires 14d;\n'
        file_content += '\t\tproxy_pass http://' + domain_nodot + ';\n'
        file_content += '\t}\n'

        file_content += '}'

        return file_content

    def _get_nginx_file_path(self):
        file_path = self.instance_id.odoo_server_id.nginx_server_id.sites_available_path + '/' + self.name + '.conf'
        return file_path

    def _get_nginx_symlink_file_path(self):
        symlink_path = self.instance_id.odoo_server_id.nginx_server_id.sites_enabled_path + '/' + self.name + '.conf'
        return symlink_path
