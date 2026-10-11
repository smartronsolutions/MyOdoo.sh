# -*- coding: utf-8 -*-
"""On-demand Python libraries for a SaaS instance.

The customer installs a pip package from the instance dashboard. The package is installed
*inside* the instance container, as the ``odoo`` user and with ``--user``: this puts it in
``$HOME/.local`` (``/var/lib/odoo/.local``), which is the bind-mounted
``<instance>/odoo-web-data`` folder. That is what makes the library survive a container
stop / start / recreate (redeploy), unlike a plain ``docker exec pip install`` that only
lives in the container's writable layer.

Every change is mirrored in ``/home/<technical_name>/requirements.txt``: the file is both
the record of what the customer asked for and the manifest that can be replayed to rebuild
the container image.
"""

import logging
import re
import shlex

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Manifest written in the instance folder (/home/<technical_name>).
REQUIREMENTS_FILE = 'requirements.txt'

# pip must never be able to hang a portal request: --timeout / --retries are handed to pip
# itself and the whole docker exec is wrapped in `timeout` as the last resort.
INSTALL_TIMEOUT_SECONDS = 300
UNINSTALL_TIMEOUT_SECONDS = 120
LIST_TIMEOUT_SECONDS = 60

# How much of the pip output is kept and sent back to the portal.
LOG_MAX_CHARS = 4000

# A customer may only ask for a plain distribution name with optional extras and/or version
# specifiers: 'pandas', 'python-escpos==3.1', 'opencv-python>=4.8,<5', 'pandas[excel]'.
# Anything that could become a second shell word (spaces, ';', '|', '&', '$', '`', a VCS or
# a local path, another requirements file, ...) is refused. The accepted value can therefore
# be handed to ``docker exec`` without ever going through a shell.
_NAME = r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?'
_EXTRAS = r'(?:\[[A-Za-z0-9._,-]+\])?'
_OPERATOR = r'(?:~=|==|!=|<=|>=|<|>|===)'
_VERSION = r'[A-Za-z0-9][A-Za-z0-9._*+!-]*'
_SPECIFIER = r'(?:%s%s)' % (_OPERATOR, _VERSION)
_SPECIFIERS = r'(?:%s(?:,%s)*)?' % (_SPECIFIER, _SPECIFIER)
REQUIREMENT_RE = re.compile(
    r'^(?P<name>%s)(?P<extras>%s)(?P<specs>%s)$' % (_NAME, _EXTRAS, _SPECIFIERS))

# Packages that must never be replaced inside the container: shadowing one of them with a
# ``--user`` copy breaks Odoo itself, or the tooling that installs the next package.
BLOCKED_PACKAGES = ('odoo', 'pip', 'setuptools', 'wheel')


def _normalize(name):
    """PEP 503 normalized distribution name (lowercase, ``-`` separated)."""
    return re.sub(r'[-_.]+', '-', (name or '').strip()).strip('-').lower()


class OdooInstance(models.Model):
    _inherit = 'saas.odoo.instance'

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _python_requirements_path(self):
        """Absolute path of the manifest on the physical server."""
        self.ensure_one()
        return '/home/%s/%s' % (self.technical_name, REQUIREMENTS_FILE)

    def _python_library_ssh(self):
        """One SSH connection to the physical server hosting this instance."""
        self.ensure_one()
        if not self.pserver_id:
            raise UserError(_("This instance is not attached to a physical server yet."))
        return self.pserver_id._connect_or_raise()

    def _python_library_container(self):
        """Container the pip commands run in.

        The instance has to be deployed *and* running: pip lives inside the container, and
        the ``--user`` target directory only makes sense while the volume is mounted.
        """
        self.ensure_one()
        if self.state != 'deploy':
            raise UserError(_(
                "This instance is not deployed, Python libraries cannot be installed."))
        if self.operation_state != 'run':
            raise UserError(_(
                "Start the instance before installing a Python library."))
        return self._shell_container_name('odoo')

    def _python_library_docker_command(self, argv, timeout):
        """``docker exec`` inside the instance container, as the ``odoo`` user.

        ``HOME`` is pinned to ``/var/lib/odoo`` so ``pip --user`` always writes to the
        ``odoo-web-data`` volume: that folder is bind-mounted from the instance folder,
        which is what keeps the libraries across a container restart or recreate.

        Every element of ``argv`` is quoted, so the resulting string is safe even though it
        travels through ``sudo sh -c``.
        """
        parts = ['timeout', str(int(timeout)), 'docker', 'exec', '-u', 'odoo',
                 '-e', 'HOME=/var/lib/odoo', '-e', 'PYTHONUSERBASE=/var/lib/odoo/.local',
                 self._python_library_container()] + list(argv)
        return ' '.join(shlex.quote(str(part)) for part in parts)

    # ------------------------------------------------------------------
    # manifest (requirements.txt in the instance folder)
    # ------------------------------------------------------------------
    @staticmethod
    def _python_requirement_name(line):
        """Distribution name of a stored manifest line ('' when it is not a valid line)."""
        match = REQUIREMENT_RE.match((line or '').strip())
        return match.group('name') if match else ''

    def _python_requirements_read(self, ssh):
        """Read the manifest. A missing file is simply an empty manifest."""
        _, output = self.pserver_id._exec_capture(
            'cat %s 2>/dev/null' % shlex.quote(self._python_requirements_path()), ssh)
        lines = []
        for raw in (output or '').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and line not in lines:
                lines.append(line)
        return lines

    def _python_requirements_write(self, ssh, lines):
        """Replace the manifest with ``lines`` (empty list writes an empty file)."""
        content = '\n'.join(lines) + '\n' if lines else ''
        self.pserver_id._create_file(ssh, self._python_requirements_path(), content)

    def _python_requirements_merge(self, requirements, line):
        """Put ``line`` in the manifest, keeping a single entry per distribution."""
        wanted = _normalize(self._python_requirement_name(line))
        merged = []
        for existing in requirements:
            existing_name = _normalize(self._python_requirement_name(existing))
            if existing_name and existing_name == wanted:
                continue
            merged.append(existing)
        merged.append(line)
        return merged

    # ------------------------------------------------------------------
    # container state
    # ------------------------------------------------------------------
    def _python_libraries_installed(self, ssh):
        """Normalized distribution name -> version, for everything visible in the container."""
        installed = {}
        try:
            _, output = self.pserver_id._exec_capture(
                self._python_library_docker_command(
                    ['pip', 'list', '--format=freeze', '--disable-pip-version-check'],
                    LIST_TIMEOUT_SECONDS), ssh)
        except Exception:
            _logger.exception("Could not list the Python packages of %s", self.display_name)
            return installed
        for raw in (output or '').splitlines():
            line = raw.strip()
            # pip writes warnings (invalid metadata, ...) on the same stream: only real
            # "name==version" lines are kept.
            if '==' not in line or line.startswith(('WARNING', 'ERROR')):
                continue
            name, _, version = line.partition('==')
            installed[_normalize(name)] = version.strip()
        return installed

    def _python_library_payload(self, ssh):
        """State the portal needs to render the wizard."""
        requirements = self._python_requirements_read(ssh)
        container_running = bool(self.state == 'deploy' and self.operation_state == 'run')
        installed = self._python_libraries_installed(ssh) if container_running else {}
        libraries = []
        for line in requirements:
            name = self._python_requirement_name(line) or line
            version = installed.get(_normalize(name), '')
            libraries.append({
                'name': name,
                'requirement': line,
                'version': version,
                'installed': bool(version),
            })
        return {
            'requirements_path': self._python_requirements_path(),
            'container_running': container_running,
            'libraries': libraries,
        }

    def python_library_list(self):
        """Public entry point: manifest + live versions of this instance."""
        self.ensure_one()
        ssh = self._python_library_ssh()
        try:
            return self._python_library_payload(ssh)
        finally:
            ssh.close()

    # ------------------------------------------------------------------
    # input validation
    # ------------------------------------------------------------------
    def _python_library_clean_requirement(self, requirement):
        """Validate what the customer typed and return the line to install/store."""
        requirement = (requirement or '').strip()
        if not requirement:
            raise UserError(_("Enter the name of the Python library to install."))
        if len(requirement) > 100:
            raise UserError(_("That library name is too long (100 characters maximum)."))
        match = REQUIREMENT_RE.match(requirement)
        if not match:
            raise UserError(_(
                "Only a plain library name is accepted, optionally with a version — for "
                "example \"pandas\", \"pandas[excel]\" or \"python-escpos==3.1\"."))
        if _normalize(match.group('name')) in BLOCKED_PACKAGES:
            raise UserError(_(
                "\"%s\" cannot be installed or replaced inside your instance.")
                % match.group('name'))
        return requirement

    def _python_library_clean_name(self, name):
        """Validate a distribution name (the Remove button only sends a name)."""
        name = (name or '').strip()
        if not name or not re.match(r'^%s$' % _NAME, name):
            raise UserError(_("Invalid library name."))
        return name

    # ------------------------------------------------------------------
    # pip plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _python_library_log(output):
        """Last part of the pip output, as shown to the customer."""
        return (output or '').strip()[-LOG_MAX_CHARS:]

    @staticmethod
    def _python_library_error(log):
        """The most useful line(s) of a failed pip run."""
        lines = [line.strip() for line in (log or '').splitlines() if line.strip()]
        errors = [line for line in lines if line.upper().startswith('ERROR')]
        return ' '.join(errors or lines[-3:])[:400] or _(
            "pip did not report a reason.")

    @staticmethod
    def _python_library_version_for(name, log):
        """The version pip just installed for one distribution, from its own log.

        pip ends with ``Successfully installed <name>-<version> ...``; matching the token on
        our own (normalized) name avoids picking up a version of a dependency, and the
        leading-digit check keeps ``foo-bar-1.0`` from being read as a version of ``foo``.
        """
        wanted = _normalize(name) + '-'
        for line in (log or '').splitlines():
            if 'Successfully installed' not in line:
                continue
            for token in line.split('Successfully installed', 1)[1].split():
                token = token.strip().strip(',')
                if token.lower().startswith(wanted):
                    version = token[len(wanted):]
                    if re.match(r'^\d', version):
                        return version
        return ''

    # ------------------------------------------------------------------
    # install / remove
    # ------------------------------------------------------------------
    def python_library_install(self, requirement):
        """Install one Python package inside the instance container and record it.

        Returns a portal payload: ``{'success': bool, ...}`` with either ``message`` or
        ``error``, plus the pip ``log`` so the customer can see what happened.
        """
        self.ensure_one()
        line = self._python_library_clean_requirement(requirement)
        name = self._python_requirement_name(line)
        # Fail fast (and without opening SSH) when the instance cannot take a pip command.
        self._python_library_container()
        ssh = self._python_library_ssh()
        try:
            exit_code, output = self.pserver_id._exec_capture(
                self._python_library_docker_command(
                    ['pip', 'install', '--user', '--break-system-packages',
                     '--no-cache-dir', '--disable-pip-version-check',
                     '--timeout', '30', '--retries', '1', line],
                    INSTALL_TIMEOUT_SECONDS), ssh)
            log = self._python_library_log(output)
            # The exit code is what decides: pip also prints "ERROR: pip's dependency
            # resolver ..." on a perfectly successful install, and exits 0 both when it
            # installed the package and when the requirement was already satisfied.
            if exit_code:
                self._log_history(
                    _("Python library installation failed: %s") % line,
                    category='python', level='danger', icon='fa-cube',
                    summary=self._python_library_error(log))
                return {'success': False,
                        'error': self._python_library_error(log),
                        'log': log}
            stored = line
            if not REQUIREMENT_RE.match(line).group('specs'):
                pin = self._python_library_version_for(name, log)
                if not pin:
                    # Nothing was installed (pip answered "Requirement already satisfied",
                    # typically a package of the base image): take the version already
                    # present so the manifest stays reproducible all the same.
                    pin = self._python_libraries_installed(ssh).get(_normalize(name), '')
                if pin:
                    stored = '%s==%s' % (line, pin)
            requirements = self._python_requirements_merge(
                self._python_requirements_read(ssh), stored)
            self._python_requirements_write(ssh, requirements)
            self._log_history(
                _("Python library installed: %s") % stored,
                category='python', level='success', icon='fa-cube',
                summary=_("%s in requirements.txt") % self._python_requirements_path(),
                description=log[-1200:])
            return dict(self._python_library_payload(ssh), success=True,
                        message=_("%s is installed.") % stored, log=log)
        finally:
            ssh.close()

    def python_library_remove(self, name):
        """Uninstall a Python package and drop it from the manifest.

        A package that ships with the base image cannot be uninstalled as the ``odoo`` user;
        it is then only removed from ``requirements.txt`` and the customer is told so.
        """
        self.ensure_one()
        name = self._python_library_clean_name(name)
        self._python_library_container()
        ssh = self._python_library_ssh()
        try:
            exit_code, output = self.pserver_id._exec_capture(
                self._python_library_docker_command(
                    ['pip', 'uninstall', '-y', '--break-system-packages',
                     '--disable-pip-version-check', name],
                    UNINSTALL_TIMEOUT_SECONDS), ssh)
            log = self._python_library_log(output)
            wanted = _normalize(name)
            requirements = [
                line for line in self._python_requirements_read(ssh)
                if _normalize(self._python_requirement_name(line)) != wanted
            ]
            self._python_requirements_write(ssh, requirements)
            if exit_code:
                message = _(
                    "%s was removed from requirements.txt, but pip could not uninstall it: "
                    "it is provided by the base image and stays available to your Odoo."
                ) % name
                level = 'warning'
            else:
                message = _("%s is uninstalled and removed from requirements.txt.") % name
                level = 'success'
            self._log_history(
                _("Python library removed: %s") % name,
                category='python', level='success' if not exit_code else 'warning',
                icon='fa-cube', summary=self._python_library_error(log) if exit_code else '',
                description=log[-1200:])
            return dict(self._python_library_payload(ssh), success=True,
                        level=level, message=message, log=log)
        finally:
            ssh.close()
