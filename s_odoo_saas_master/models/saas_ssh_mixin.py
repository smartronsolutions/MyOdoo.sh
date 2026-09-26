import logging
import shlex
from odoo import models, _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class SSHMixin(models.AbstractModel):
    _name = 'saas.ssh.mixin'
    _description = "SaaS SSH Mixin"

    def _check_ssh_connection(self, ssh):
        if not ssh or not hasattr(ssh, 'exec_command'):
            raise UserError(_("Cannot connect to server. Please check server information and SSH Key Pair."))

    def _exec_cmd(self, command, ssh, arguments=False, without_return=True, raise_on_error=False):
        """Execute a command on the remote server via SSH.

        :param command: shell command to run
        :param ssh: opened paramiko SSHClient
        :param arguments: optional list of arguments piped to stdin
        :param without_return: if False, return stdout lines
        :param raise_on_error: if True, raise a UserError when the remote
            command exits with a non-zero status (instead of silently
            ignoring the failure).
        """
        assert isinstance(command, str) and command
        self._check_ssh_connection(ssh)
        command = command.strip()
        # Auto-sudo for non-root service accounts (wrap in sh -c for cd, &&, etc.)
        if not command.startswith("sudo "):
            command = "sudo sh -c " + shlex.quote(command)
        stdin, stdout, stderr = ssh.exec_command(command)
        if arguments:
            for arg in arguments:
                stdin.write('%s\n' % arg)
            stdin.flush()
        channel = stdout.channel
        exit_status = channel.recv_exit_status()
        if raise_on_error and exit_status:
            error = stderr.read().decode().strip()
            raise UserError(
                _("Command failed on server (exit code %s): %s\n%s")
                % (exit_status, command, error or _("Unknown error."))
            )
        if not without_return:
            return stdout.readlines()

    def _exec_capture(self, command, ssh, arguments=False):
        """Run a command and return ``(exit_code, stdout + stderr)``.

        Unlike :meth:`_exec_cmd` this keeps what the command printed, which is what lets the
        portal show the exact text git produced (files changed, ``+``/``-``, ...).
        """
        self._check_ssh_connection(ssh)
        command = command.strip()
        if not command.startswith("sudo "):
            command = "sudo sh -c " + shlex.quote(command)
        stdin, stdout, stderr = ssh.exec_command(command)
        if arguments:
            for arg in arguments:
                stdin.write('%s\n' % arg)
            stdin.flush()
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        return exit_status, (out + err)

    def _create_file(self, ssh, file_path, file_content):
        """Create a file on the remote server. Uses /tmp as intermediate to work with non-root SSH users."""
        self._check_ssh_connection(ssh)
        import os
        tmp_path = "/tmp/saas_tmp_%d_%d" % (os.getpid(), hash(file_path) % 1000000)
        sftp = ssh.open_sftp()
        f = sftp.open(tmp_path, 'w')
        f.write(file_content)
        f.close()
        sftp.close()
        self._exec_cmd("sudo mv %s %s" % (tmp_path, file_path), ssh)
        return True

    def _create_symlink(self, ssh, from_path, to_path, overwrite=False):
        cmd = "ln -s"
        if overwrite:
            cmd += 'f'
        cmd += ' ' + from_path + ' ' + to_path

        return self._exec_cmd(cmd, ssh)
