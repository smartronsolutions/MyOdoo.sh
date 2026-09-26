import paramiko
import json
import logging
import os
import shlex
from odoo import fields, models, _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class PServer(models.Model):
    _name = 'saas.pserver'
    _inherit = ['saas.ssh.mixin']
    _description = "SaaS Physical Server"

    name = fields.Char(string='Name', required=True)
    ssh_port = fields.Integer(string="SSH Port", required=True, default=22)
    ssh_username = fields.Char(
        string="SSH Username",
        default="root",
        required=True,
        help="SSH username used to connect. Default root. Use svc-odoobck (docker group + sudo) for production."
    )
    ssh_keypair_id = fields.Many2one('saas.ssh.keypair', string="SSH Key Pair", required=True)
    ssh_keypair_name = fields.Char(related="ssh_keypair_id.name", string="SSH Key", readonly=True)
    can_edit_ssh_key = fields.Boolean(string="Can Edit SSH Key", compute="_compute_can_edit_ssh_key")
    ip_ids = fields.One2many('saas.pserver.ip', 'pserver_id', string="IPs")
    version_16_plus = fields.Boolean(string='Ubuntu Version 16+', default=True)
    active = fields.Boolean(string="Active", default=True)

    def _compute_can_edit_ssh_key(self):
        """Check if the current user can edit SSH key fields."""
        is_saas_master = self.env.user.has_group('s_odoo_saas_master.group_odoo_saas_master')
        for record in self:
            record.can_edit_ssh_key = is_saas_master

    def action_test_connection(self):
        ssh = self._connect_or_raise()
        if ssh:
            ssh.close()

        message = _("Connection Successful!")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }

    def _get_managing_ip(self):
        managing_ips = self.ip_ids.filtered(lambda ip: ip.type == 'managing_ip')
        if not managing_ips:
            raise UserError(_("Cannot find managing IP of %s server") % self.name)
        return managing_ips[0].name

    def _validate_remote_account(self, ssh):
        """Validate remote account: whoami, docker group, sudo."""
        username = self.ssh_username or "root"
        stdin, stdout, stderr = ssh.exec_command("whoami")
        stdout.channel.recv_exit_status()
        if stdout.read().decode().strip() != username:
            raise UserError(_("SSH user mismatch on %s. Expected: %s") % (self.display_name, username))
        if username == "root":
            return True
        stdin, stdout, stderr = ssh.exec_command("groups")
        stdout.channel.recv_exit_status()
        if "docker" not in stdout.read().decode().strip():
            raise UserError(_("User %s on %s is NOT in docker group. Run: sudo usermod -aG docker %s") % (username, self.display_name, username))
        stdin, stdout, stderr = ssh.exec_command("sudo -n systemctl --version 2>&1")
        stdout.channel.recv_exit_status()
        err = stderr.read().decode().strip()
        if "not allowed" in err.lower() or "password" in err.lower():
            raise UserError(_("User %s on %s lacks passwordless sudo for systemctl/certbot.\nAdd: %s ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/bin/certbot") % (username, self.display_name, username))
        return True

    def _connect(self):
        managing_ip = self._get_managing_ip()
        privatekey_file_full_path = self.ssh_keypair_id.private_key_id._full_path(self.ssh_keypair_id.private_key_id.store_fname)
        if not privatekey_file_full_path:
            raise UserError(_("Cannot find attachment path of private key of %s server.") % self.name)

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            username = self.ssh_username or "root"
            ssh.connect(
                managing_ip,
                username=username,
                port=self.ssh_port,
                key_filename=privatekey_file_full_path,
            )
            if not ssh:
                raise UserError(_("Cannot connect to server %s. Please check server information and SSH Key Pair") % self.name)
            return ssh
        except paramiko.AuthenticationException:
            raise UserError(_("Auth failed for user %s on %s. Check: SSH key, user exists, AllowUsers") % (username, self.display_name))
        except Exception as e:
            _logger.exception("SSH connection error for %s", self.name)
            return False

    def _connect_or_raise(self):
        ssh = self._connect()
        if not ssh:
            raise UserError(
                _("Cannot connect to server %s. Please check server information and SSH Key Pair.")
                % self.display_name
            )
        return ssh

    def _deploy_odoo_instance(self, instance):
        ssh = self._connect()
        try:
            self._create_instance_folder(instance, ssh)
            self._create_odoo_instance_config_file(instance, ssh)
            # self._create_standard_extra_addons(instance, ssh)
            self._create_custom_addons(instance.custom_addon_ids, ssh)
            # Modules installed at database creation ("-i"). Enterprise instances always
            # include web_enterprise here, which is what makes the created database an
            # Enterprise one instead of a plain Community one.
            modules_to_install = instance._get_modules_to_install()
            if modules_to_install:
                odoo_command = 'odoo -i %s -d %s' % (','.join(modules_to_install), instance.technical_name)
                self._create_docker_compose_file(instance, ssh, odoo_command)
                self._docker_compose_up(instance, ssh)
                file_path = instance._get_docker_compose_file_path()
                self._exec_cmd('rm -f %s' % file_path, ssh)
                self._create_docker_compose_file(instance, ssh)
                instance.write({'need_to_compose_up': True})
            else:
                self._create_docker_compose_file(instance, ssh)
                self._docker_compose_up(instance, ssh)
            self._create_nginx_file(instance.domain_name_ids, ssh)
            ssh.close()
        except Exception as ex:
            self._revoke_odoo_instance(instance, ssh)
            raise UserError(ex)

    def _deploy_odoo_instance_from_template(self, instance):
        ssh = self._connect()
        try:
            self._create_instance_folder(instance, ssh)
            self._prepare_instance_folder_from_template(instance, ssh)
            self._create_docker_compose_file(instance, ssh)
            self._docker_compose_up(instance, ssh)
            self._create_nginx_file(instance.domain_name_ids, ssh)            
            ssh.close()
        except Exception as ex:
            self._revoke_odoo_instance(instance, ssh)
            raise UserError(ex)

    def _prepare_instance_folder_from_template(self, instance, ssh):
        self._exec_cmd("cp -r -a /home/%s/* /home/%s" % (instance.template_instance_id.technical_name, instance.technical_name), ssh)
        self._exec_cmd("rm -rf /home/%s/odoo-web-data/sessions" % instance.technical_name, ssh)
        self._exec_cmd("rm -rf /home/%s/docker-compose.yml" % instance.technical_name, ssh)

    def _create_instance_folder(self, instance, ssh):
        self._exec_cmd('mkdir /home/%s' % instance.technical_name, ssh)
        for volume in instance.docker_compose_volume_ids:
            self._exec_cmd('mkdir %s' % volume.storage_path, ssh)

    def _create_odoo_instance_config_file(self, instance, ssh):
        file_content = self.env['saas.odoo.instance.config']._get_config_file_content(instance)
        file_path = self.env['saas.odoo.instance.config']._get_config_file_path(instance)
        self._create_file(ssh, file_path, file_content)

    def _create_standard_extra_addons(self, instance, ssh):
        for extra_addon in instance.odoo_server_id.extra_addon_ids:
            self._exec_cmd('cp -r %s /home/%s/custom-addons' % (extra_addon.source_path, instance.technical_name), ssh)

    def _create_custom_addons(self, custom_addons, ssh):
        for custom_addon in custom_addons.filtered(lambda c: not c.cloned):
            path = shlex.quote(custom_addon.addon_path)
            uri = shlex.quote(custom_addon.clone_uri)
            branch = shlex.quote(custom_addon.branch or 'main')
            # Idempotent: clone the first time, otherwise fetch + hard-reset the selected
            # branch. This makes "Sync & Deploy" safe to run again (and repairs a half
            # finished clone) instead of failing with "destination path already exists".
            cmd = (
                "if [ -d %s/.git ]; then "
                "cd %s && git remote set-url origin %s && git fetch origin %s && "
                "git checkout -f %s && git reset --hard FETCH_HEAD; "
                "else git clone %s --branch %s --depth 1 --single-branch %s; fi"
            ) % (path, path, uri, branch, branch, uri, branch, path)
            self._exec_cmd(cmd, ssh, raise_on_error=True)

    def _create_docker_compose_file(self, instance, ssh, odoo_command=False):
        file_content = instance._get_docker_compose_file_content(odoo_command=odoo_command)
        file_path = instance._get_docker_compose_file_path()
        self._create_file(ssh, file_path, file_content)

    def _docker_compose_up(self, instance, ssh=False):
        if not ssh:
            ssh = self._connect()
        project_dir = '/home/%s' % instance.technical_name
        web_container = 'odoo_%s' % instance.technical_name
        # docker-compose v1 (1.29) is incompatible with Docker Engine >= 25: when it has
        # to *recreate* a container it crashes with `KeyError: 'ContainerConfig'` (the
        # Engine no longer returns ContainerConfig in `docker inspect`). That leaves the
        # instance stopped. Prefer the Compose v2 plugin; when only v1 is available,
        # remove the web container first so compose creates it fresh instead of
        # recreating it (a create does not hit the broken code path).
        cmd = (
            'cd %(dir)s ; '
            'if docker compose version >/dev/null 2>&1 ; then '
            '  docker compose up -d ; '
            'else '
            '  docker rm -f %(web)s >/dev/null 2>&1 || true ; '
            '  docker-compose up -d ; '
            'fi'
        ) % {'dir': project_dir, 'web': web_container}
        self._exec_cmd(cmd, ssh)

    def _compose_exec(self, instance, args, ssh=False):
        """Run a compose sub-command on the server.

        Prefers the Compose v2 plugin (``docker compose``) and falls back to the
        legacy ``docker-compose`` v1 binary, so start/stop/restart/config work on
        hosts where only one of the two is installed.
        """
        if not ssh:
            ssh = self._connect()
        project_dir = '/home/%s' % instance.technical_name
        cmd = (
            'cd %(dir)s ; '
            'if docker compose version >/dev/null 2>&1 ; then docker compose %(args)s ; '
            'else docker-compose %(args)s ; fi'
        ) % {'dir': project_dir, 'args': args}
        self._exec_cmd(cmd, ssh)

    def _create_nginx_file(self, domain_name_ids, ssh):
        for domain_name in domain_name_ids:
            file_content = domain_name._get_nginx_file_content()
            file_path = domain_name._get_nginx_file_path()
            symlink_path = domain_name._get_nginx_symlink_file_path()
            self._create_file(ssh, file_path, file_content)
            self._create_symlink(ssh, file_path, symlink_path)

        self._exec_cmd('sudo systemctl reload nginx', ssh)
        domain_names = ' -d '.join(domain_name_ids.mapped('name'))
        self._exec_cmd('sudo certbot --non-interactive --nginx --agree-tos -d %s --redirect' % domain_names, ssh)

    def _revoke_odoo_instance(self, instance, ssh=None):
        if not ssh:
            ssh = self._connect_or_raise()
        try:
            self._remove_docker_containers(instance, ssh)
            self._remove_instance_folder(instance, ssh)
            self._remove_nginx_file(instance.domain_name_ids, ssh)
            self._remove_network(instance, ssh)
        finally:
            if ssh:
                ssh.close()

    def _remove_docker_containers(self, instance, ssh):
        if not instance.docker_container_ids:
            return
        container_names = ' '.join(instance.docker_container_ids.mapped('name'))
        self._exec_cmd('docker stop %s' % container_names, ssh)
        self._exec_cmd('docker rm -v %s' % container_names, ssh)

    def _remove_instance_folder(self, instance, ssh):
        self._exec_cmd('rm -rf /home/%s' % instance.technical_name, ssh)

    def _remove_nginx_file(self, domain_name_ids, ssh):
        need_to_remove = []
        for domain_name in domain_name_ids:
            need_to_remove.append(domain_name._get_nginx_file_path())
            need_to_remove.append(domain_name._get_nginx_symlink_file_path())

        self._exec_cmd('rm -rf %s' % ' '.join(need_to_remove), ssh)
        self._exec_cmd('sudo systemctl reload nginx', ssh)
        # Drop the Let's Encrypt certificate as well: without this the certificate stays on
        # the server and the domain can never be issued again cleanly.
        for domain_name in domain_name_ids:
            name = (domain_name.name or '').strip()
            if not name:
                continue
            self._exec_cmd(
                'sudo certbot delete --non-interactive --cert-name %s 2>/dev/null || true' % name,
                ssh)

    def _remove_network(self, instance, ssh):
        network_name = "%s_default" % instance.technical_name
        self._exec_cmd('docker network rm %s' % network_name, ssh)

    def _get_container_status(self, containers):
        res = {}
        if not containers:
            return res
        container_names = ' '.join(containers.mapped('name'))
        ssh = self._connect()
        if not ssh:
            _logger.warning(
                "Cannot connect to server %s while reading Docker container status.",
                self.display_name,
            )
            return {container.name: 'unknown' for container in containers}

        statuses = {}
        try:
            # Ask docker for "<name> <status>" pairs instead of bare statuses: docker only
            # prints a line for the containers that still exist, so matching the output by
            # position shifted every later status as soon as one container was gone
            # (already removed, renamed, or not created yet).
            raw = self._exec_cmd(
                "docker inspect -f '{{.Name}} {{.State.Status}}' " + container_names,
                ssh, without_return=False,
            )
            for line in raw:
                parts = line.split()
                if len(parts) >= 2:
                    statuses[parts[0].lstrip('/')] = parts[1].rstrip()
        except Exception:
            _logger.exception(
                "Cannot read Docker container status from server %s.",
                self.display_name,
            )
            return {container.name: 'unknown' for container in containers}
        finally:
            ssh.close()

        for container in containers:
            if container.name in statuses:
                res[container.name] = statuses[container.name]
            else:
                # A container that is not there yet (deployment in progress) or already
                # removed is a normal transient state, so warn instead of logging an error.
                res[container.name] = 'unknown'
                _logger.warning("Container status missing for %s", container.name)
        return res

    def _container_operation(self, instance, operation, container_names, ssh=False):
        if not container_names:
            return

        if not ssh:
            ssh = self._connect_or_raise()
        if instance.need_to_compose_up:
            self._docker_compose_up(instance, ssh)
            instance.write({'need_to_compose_up': False})
        self._exec_cmd("docker %s %s" % (operation, container_names), ssh)
        if ssh:
            ssh.close()

    def _redeploy_odoo_instance_config(self, instance):
        ssh = self._connect()
        try:
            self._remove_odoo_instance_config_file(instance, ssh)
            self._create_odoo_instance_config_file(instance, ssh)
            self._container_operation(instance, 'restart', 'odoo_%s' % instance.technical_name, ssh=ssh)
        except Exception as ex:
            ssh.close()
            raise UserError(ex)

    def _redeploy_odoo_instance_nginx(self, domain_name_ids):
        ssh = self._connect()
        try:
            self._remove_nginx_file(domain_name_ids, ssh)
            self._create_nginx_file(domain_name_ids, ssh)
            ssh.close()
        except Exception as ex:
            ssh.close()
            raise UserError(ex)

    def _remove_odoo_instance_config_file(self, instance, ssh):
        config_path = instance.docker_compose_volume_ids.filtered(lambda v: v.volume_type == 'odoo_config')[0].storage_path
        self._exec_cmd('rm -f %s' % config_path, ssh)

    def _cancel_nginx(self, domain_name_ids):
        ssh = self._connect()
        self._remove_nginx_file(domain_name_ids, ssh)
        ssh.close()

    def _deploy_nginx(self, domain_name_ids):
        ssh = self._connect()
        self._create_nginx_file(domain_name_ids, ssh)
        ssh.close()

    def _clone_customer_addons(self, custom_addons):
        ssh = self._connect()
        self._create_custom_addons(custom_addons, ssh)
        ssh.close()

    def _git_command(self, path, args):
        return 'cd %s && git %s 2>&1' % (shlex.quote(path), args)

    def _git_rev(self, ssh, path, ref='HEAD'):
        """Current commit of the addon folder, or '' when it is not a working clone."""
        code, output = self._exec_capture(self._git_command(path, 'rev-parse %s' % ref), ssh)
        lines = [line.strip() for line in (output or '').splitlines() if line.strip()]
        return '' if code or not lines else lines[-1]

    def _pull_customer_addons(self, custom_addons, trigger='manual'):
        """git pull each addon, record exactly what changed, then restart the container.

        Every sync is stored in ``saas.odoo.instance.github.log`` with the commits, the file
        list with their ``+``/``-`` counts and the raw git output, so the GitHub Logs tab can
        show what really happened on the server.
        """
        Log = self.env['saas.odoo.instance.github.log'].sudo()
        for custom_addon in custom_addons:
            ssh = self._connect()
            try:
                path = custom_addon.addon_path
                before = self._git_rev(ssh, path)
                code, output = self._exec_capture(self._git_command(path, 'pull'), ssh)
                after = self._git_rev(ssh, path)

                commits = files = ''
                commit_count = file_count = insertions = deletions = 0
                if not code and before and after and before != after:
                    _rc, commits = self._exec_capture(self._git_command(
                        path, 'log --oneline --no-decorate %s..%s' % (before, after)), ssh)
                    _rc, numstat = self._exec_capture(self._git_command(
                        path, 'diff --numstat %s..%s' % (before, after)), ssh)
                    rows = []
                    for line in (numstat or '').splitlines():
                        parts = line.split('\t')
                        if len(parts) != 3:
                            continue
                        added, removed, name = parts
                        insertions += int(added) if added.isdigit() else 0
                        deletions += int(removed) if removed.isdigit() else 0
                        rows.append('%s | +%s -%s' % (name, added, removed))
                    files = '\n'.join(rows)
                    file_count = len(rows)
                    commit_count = len([l for l in (commits or '').splitlines() if l.strip()])

                if code:
                    status = 'failed'
                    summary = _("git pull failed (exit %s)") % code
                elif before != after:
                    status = 'success'
                    summary = _("%(files)s file(s) changed, +%(plus)s -%(minus)s") % {
                        'files': file_count, 'plus': insertions, 'minus': deletions}
                else:
                    status, summary = 'nochange', _("Already up to date")

                Log.create({
                    'instance_id': custom_addon.instance_id.id,
                    'trigger': trigger,
                    'status': status,
                    'repo': custom_addon.clone_uri or custom_addon.instance_id.github_repo_url or '',
                    'addon_name': custom_addon.name,
                    'branch': (custom_addon.branch
                               or custom_addon.instance_id.github_branch or 'main'),
                    'ref_before': before,
                    'ref_after': after,
                    'commits': commits,
                    'commit_count': commit_count,
                    'files': files,
                    'file_count': file_count,
                    'insertions': insertions,
                    'deletions': deletions,
                    'output': output,
                    'message': summary,
                })

                if code:
                    # Commit the failure first: the caller rolls back, and a rolled back log
                    # would hide the very error the customer needs to see.
                    self.env.cr.commit()
                    raise UserError(_("git pull failed (exit %s):\n%s")
                                    % (code, (output or '')[-800:]))

                self._container_operation(custom_addon.instance_id, 'restart', 'odoo_%s' % custom_addon.instance_id.technical_name, ssh=ssh)
            finally:
                ssh.close()

    def _remove_customer_addons(self, custom_addons):
        ssh = self._connect()
        for custom_addon in custom_addons:
            self._exec_cmd('rm -rf %s' % custom_addon.addon_path, ssh)
            self._container_operation(custom_addon.instance_id, 'restart', 'odoo_%s' % custom_addon.instance_id.technical_name, ssh=ssh)
        ssh.close()

    def _recreate_docker_compose_file(self, instance, odoo_command=False, update=False):
        # TODO: check upgrade finish to run docker-compose up -d
        ssh = self._connect()
        # 1. Remove origin docker compose file
        file_path = instance._get_docker_compose_file_path()
        self._exec_cmd('rm -f %s' % file_path, ssh)
        # 2. Create new docker compose file
        self._create_docker_compose_file(instance, ssh, odoo_command=odoo_command)
        # 3. docker compose up
        self._docker_compose_up(instance, ssh)
        if not update:
            # 4. Remove new docker compose file
            self._exec_cmd('rm -f %s' % file_path, ssh)
            # 5. Recreate origin docker compose file
            self._create_docker_compose_file(instance, ssh)
        ssh.close()

    def _get_active_user(self, instances):
        res = {}
        if not instances:
            return res
        ssh = self._connect()
        for instance in instances:
            psql_containers = instance.docker_container_ids.filtered(lambda c: c.container_type == 'psql')
            for container in psql_containers:
                query = 'select count(*) from res_users where share=False and active=True'
                cmd = 'docker exec -i %s psql -U odoo -W -d %s -c "%s"' % (container.name, instance.db_name, query)
                output = self._exec_cmd(cmd, ssh, arguments=['odoo'], without_return=False)
                if output:
                    output = output[2]
                    output = int(output.replace('\n', '').strip())
                    res.update({instance.id: output})
        ssh.close()
        return res

    def _get_installed_apps(self, instances):
        res = {}
        if not instances:
            return res
        ssh = self._connect()
        for instance in instances:
            psql_containers = instance.docker_container_ids.filtered(lambda c: c.container_type == 'psql')
            for container in psql_containers:
                query = "select shortdesc,name,write_date from ir_module_module where application=true and state='installed'"
                cmd = 'docker exec -i %s psql -U odoo -W -d %s -c "%s"' % (container.name, instance.db_name, query)
                output = self._exec_cmd(cmd, ssh, arguments=['odoo'], without_return=False)
                output = output[:-2][2:]
                apps = []
                for item in output:
                    item = item.split('|')
                    app_name = item[0].strip()
                    if instance.odoo_version_id.version >= 16:
                        app_name = json.loads(app_name)
                        app_name = list(app_name.values())[0]

                    technical_name = item[1].strip()
                    write_date = item[2].replace('\n', '').strip().split('.')[0]
                    apps.append({
                        'name': app_name,
                        'technical_name': technical_name,
                        'installed_date': fields.Datetime.to_datetime(write_date),
                    })
                res.update({instance.id: apps})
        ssh.close()
        return res
    
    def _create_backup_folder(self, backup_dir):
        ssh = self._connect()
        self._exec_cmd('mkdir %s' % backup_dir, ssh)
        self._exec_cmd('chmod 755 %s' % backup_dir, ssh)
        ssh.close()

    def _get_odoo_instance_database_name(self, instance, ssh):
        container_name = 'psql_%s' % instance.technical_name
        cmd = (
            'docker exec -e PGPASSWORD=odoo {container} '
            'psql -U odoo -d postgres -At -c '
            '"SELECT datname FROM pg_database WHERE datistemplate = false AND datname != \'postgres\' ORDER BY datname"'
        ).format(container=shlex.quote(container_name))
        stdin, stdout, stderr = ssh.exec_command("sudo sh -c " + shlex.quote(cmd))
        exit_status = stdout.channel.recv_exit_status()
        error = stderr.read().decode().strip()
        if exit_status:
            raise UserError(
                _("Database backup error: cannot read database list for %s. %s")
                % (instance.display_name, error or _("Unknown Error."))
            )

        db_names = [line.strip() for line in stdout.readlines() if line.strip()]
        candidates = [instance.db_name, instance.technical_name]
        for candidate in candidates:
            if candidate and candidate in db_names:
                return candidate

        if len(db_names) == 1:
            return db_names[0]

        raise UserError(
            _("Database backup error: database '%s' was not found for %s. Available databases: %s")
            % (
                instance.db_name or instance.technical_name,
                instance.display_name,
                ', '.join(db_names) or _("none"),
            )
        )

    def _create_odoo_instance_zip_backup(self, instance, local_filepath):
        ssh = self._connect_or_raise()
        remote_dir = '/tmp/saas_odoo_backups'
        remote_name = os.path.splitext(os.path.basename(local_filepath))[0]
        remote_workdir = '%s/%s' % (remote_dir, remote_name)
        remote_filepath = '%s/%s' % (remote_dir, os.path.basename(local_filepath))
        container_name = 'psql_%s' % instance.technical_name
        db_name = self._get_odoo_instance_database_name(instance, ssh)
        filestore_path = '/home/%s/odoo-web-data/filestore/%s' % (instance.technical_name, db_name)
        cmd = (
            'set -e; '
            'rm -rf {remote_workdir} {remote_filepath}; '
            'mkdir -p {remote_workdir}; '
            'docker exec -e PGPASSWORD=odoo {container} '
            'pg_dump -U odoo -d {db_name} > {remote_workdir}/dump.sql; '
            'mkdir -p {remote_workdir}/filestore; '
            'if [ -d {filestore_path} ]; then '
            'cp -a {filestore_contents} {remote_workdir}/filestore/; '
            'fi; '
            'cd {remote_workdir}; '
            'python3 -m zipfile -c {remote_filepath} dump.sql filestore 2>/dev/null || '
            'python -m zipfile -c {remote_filepath} dump.sql filestore'
        ).format(
            remote_workdir=shlex.quote(remote_workdir),
            remote_filepath=shlex.quote(remote_filepath),
            container=shlex.quote(container_name),
            db_name=shlex.quote(db_name),
            filestore_path=shlex.quote(filestore_path),
            filestore_contents=shlex.quote(filestore_path + '/.'),
        )
        try:
            stdin, stdout, stderr = ssh.exec_command("sudo sh -c " + shlex.quote(cmd))
            exit_status = stdout.channel.recv_exit_status()
            error = stderr.read().decode().strip()
            if exit_status:
                raise UserError(
                    _("Database backup error: backup packaging failed for %s. %s")
                    % (instance.display_name, error or _("Unknown Error."))
                )

            count_stdin, count_stdout, count_stderr = ssh.exec_command(
                'find %s -type f 2>/dev/null | wc -l' % shlex.quote(remote_workdir + '/filestore')
            )
            count_stdout.channel.recv_exit_status()
            count_lines = count_stdout.readlines()
            filestore_file_count = int(count_lines[0].strip()) if count_lines and count_lines[0].strip().isdigit() else 0

            sftp = ssh.open_sftp()
            try:
                sftp.get(remote_filepath, local_filepath)
            finally:
                sftp.close()

            return filestore_file_count
        finally:
            if ssh:
                try:
                    self._exec_cmd(
                        'rm -rf %s %s' % (
                            shlex.quote(remote_workdir),
                            shlex.quote(remote_filepath),
                        ),
                        ssh,
                    )
                except Exception:
                    _logger.warning("Could not remove temporary backup files for %s", remote_filepath)
            ssh.close()

    def _create_odoo_instance_container_backup(self, instance, local_filepath):
        """Zip the full instance directory remotely and download it to Odoo."""
        ssh = self._connect_or_raise()
        remote_filepath = '/tmp/%s' % os.path.basename(local_filepath)
        instance_dir = '/home/%s' % instance.technical_name
        cmd = (
            'set -e; rm -f {archive}; '
            'test -d {instance_dir}; '
            'cd /home; '
            '(zip -rq {archive} {instance_name} || '
            'python3 -m zipfile -c {archive} {instance_name})'
        ).format(
            archive=shlex.quote(remote_filepath),
            instance_dir=shlex.quote(instance_dir),
            instance_name=shlex.quote(instance.technical_name),
        )
        try:
            stdin, stdout, stderr = ssh.exec_command("sudo sh -c " + shlex.quote(cmd))
            exit_status = stdout.channel.recv_exit_status()
            error = stderr.read().decode().strip()
            if exit_status:
                raise UserError(
                    _("Container backup packaging failed for %s. %s")
                    % (instance.display_name, error or _("Unknown Error."))
                )
            sftp = ssh.open_sftp()
            try:
                sftp.get(remote_filepath, local_filepath)
            finally:
                sftp.close()
        finally:
            try:
                self._exec_cmd('rm -f %s' % shlex.quote(remote_filepath), ssh)
            except Exception:
                _logger.warning("Could not remove container backup temp file %s", remote_filepath)
            ssh.close()

    def _restore_odoo_instance_backup(self, instance, backup):
        ssh = self._connect_or_raise()
        remote_dir = '/tmp/saas_odoo_restores'
        remote_name = os.path.splitext(os.path.basename(backup.file_path))[0]
        remote_workdir = '%s/%s' % (remote_dir, remote_name)
        remote_zip_path = '%s/%s' % (remote_dir, os.path.basename(backup.file_path))
        remote_filestore_dir = '%s/filestore' % remote_workdir
        psql_container = 'psql_%s' % instance.technical_name
        odoo_container = 'odoo_%s' % instance.technical_name
        # Use the instance's configured database name rather than looking it up on the
        # server: the target database may have been deleted before restoring, in which
        # case there is nothing yet for a lookup to find.
        db_name = instance.db_name or instance.technical_name
        filestore_path = '/home/%s/odoo-web-data/filestore/%s' % (instance.technical_name, db_name)

        def run(command):
            if not command.startswith("sudo "):
                command = "sudo sh -c " + shlex.quote(command)
            stdin, stdout, stderr = ssh.exec_command(command)
            status = stdout.channel.recv_exit_status()
            out = stdout.readlines()
            err = stderr.read().decode().strip()
            return status, out, err

        try:
            tmp_upload = '/tmp/saas_upload_%s_%d_%d' % (
                instance.technical_name, os.getpid(), hash(backup.file_path) % 1000000)
            sftp = ssh.open_sftp()
            try:
                sftp.put(backup.file_path, tmp_upload)
            finally:
                sftp.close()
            self._exec_cmd(
                'mkdir -p %s && mv %s %s' % (
                    shlex.quote(remote_dir),
                    shlex.quote(tmp_upload),
                    shlex.quote(remote_zip_path),
                ),
                ssh,
                raise_on_error=True,
            )

            extract_cmd = (
                'set -e; rm -rf {remote_workdir}; mkdir -p {remote_workdir}; cd {remote_workdir}; '
                '(python3 -m zipfile -e {remote_zip_path} . 2>/dev/null || python -m zipfile -e {remote_zip_path} .)'
            ).format(
                remote_workdir=shlex.quote(remote_workdir),
                remote_zip_path=shlex.quote(remote_zip_path),
            )
            status, _out, error = run(extract_cmd)
            if status:
                raise UserError(
                    _("Database restore error for %s: backup extraction failed. %s")
                    % (instance.display_name, error or _("Unknown Error."))
                )

            status, out, _err = run('test -s %s/dump.sql && echo OK || echo MISSING' % shlex.quote(remote_workdir))
            if not out or out[0].strip() != 'OK':
                raise UserError(
                    _("Database restore error for %s: backup does not contain a valid database dump.")
                    % instance.display_name
                )

            status, out, _err = run('find %s -type f 2>/dev/null | wc -l' % shlex.quote(remote_filestore_dir))
            filestore_file_count = int(out[0].strip()) if out and out[0].strip().isdigit() else 0

            restore_cmd = (
                'set -e; '
                'docker stop {odoo_container} || true; '
                'docker exec -e PGPASSWORD=odoo {psql_container} psql -U odoo -d postgres -c '
                '"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = \'{db_name_sql}\'"; '
                'docker exec -e PGPASSWORD=odoo {psql_container} dropdb -U odoo --if-exists {db_name_shell}; '
                'docker exec -e PGPASSWORD=odoo {psql_container} createdb -U odoo {db_name_shell}; '
                'cat {remote_workdir}/dump.sql | docker exec -i -e PGPASSWORD=odoo {psql_container} psql -U odoo -d {db_name_shell} -q; '
                'rm -rf {filestore_path}; mkdir -p {filestore_path}; '
                'if [ -d {remote_filestore_dir} ]; then cp -a {remote_filestore_dir}/. {filestore_path}/; fi; '
                'chmod -R 777 {filestore_path}; '
                'docker start {odoo_container}'
            ).format(
                remote_workdir=shlex.quote(remote_workdir),
                remote_filestore_dir=shlex.quote(remote_filestore_dir),
                odoo_container=shlex.quote(odoo_container),
                psql_container=shlex.quote(psql_container),
                db_name_sql=db_name,
                db_name_shell=shlex.quote(db_name),
                filestore_path=shlex.quote(filestore_path),
            )
            status, _out, error = run(restore_cmd)
            if status:
                try:
                    self._exec_cmd('docker start %s' % shlex.quote(odoo_container), ssh)
                except Exception:
                    _logger.warning("Could not restart odoo container for %s after failed restore", instance.display_name)
                raise UserError(
                    _("Database restore error for %s: %s")
                    % (instance.display_name, error or _("Unknown Error."))
                )

            if filestore_file_count == 0:
                return _(
                    "This backup did not contain any filestore files, so attachments/images "
                    "(including the company logo) were not restored. The database was restored normally."
                )
            return False
        finally:
            if ssh:
                try:
                    self._exec_cmd(
                        'rm -rf %s %s' % (
                            shlex.quote(remote_workdir),
                            shlex.quote(remote_zip_path),
                        ),
                        ssh,
                    )
                except Exception:
                    _logger.warning("Could not remove temporary restore files for %s", remote_zip_path)
                ssh.close()
