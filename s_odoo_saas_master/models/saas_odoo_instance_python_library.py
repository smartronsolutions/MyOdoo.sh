# -*- coding: utf-8 -*-
"""On-demand Python libraries and system packages for a SaaS instance.

The customer installs a pip package and/or an APT package from the instance dashboard.
Both are **baked into a Docker image**, not installed into the running container:

* on the first install a private ``saas.docker.image`` record is created for the instance
  (``odoo:<version>-custom-<technical_name>``), built FROM the image the instance already
  uses and carrying its Odoo version and business typology;
* every install appends its requirement (pip) and/or its APT package (system) to that
  image, regenerates the Dockerfile in the instance folder
  (``/home/<technical_name>/Dockerfile``) on the server and rebuilds the same tag;
* the container is then recreated from the new image.

Because the library lives in an image layer, it survives a restart, a redeploy and even a
``docker compose down/up`` — a plain ``docker exec pip install`` would only live in the
container's writable layer and be lost on the next recreate.

``/home/<technical_name>/requirements.txt`` stays the human-readable manifest of what the
customer asked for and is kept in sync with the image requirements.
"""

import logging
import re
import shlex

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Manifest written in the instance folder (/home/<technical_name>).
REQUIREMENTS_FILE = 'requirements.txt'
SYSTEM_PACKAGES_FILE = 'system-packages.txt'

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

# Debian/Ubuntu package name, e.g. 'python3-xmlsec', 'libxml2-dev' or 'libssl3:amd64'.
# The optional '=version' lets the customer pin a release.
_SYSTEM_NAME = r'[a-z0-9][a-z0-9+.-]+'
SYSTEM_PACKAGE_RE = re.compile(
    r'^(?P<name>%s)(?::(?P<arch>[a-z0-9-]+))?(?:=(?P<version>[A-Za-z0-9.+:~-]+))?$'
    % _SYSTEM_NAME)

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
    # system (APT) package manifest (system-packages.txt in the instance folder)
    # ------------------------------------------------------------------
    def _system_packages_path(self):
        """Absolute path of the system package manifest on the physical server."""
        self.ensure_one()
        return '/home/%s/%s' % (self.technical_name, SYSTEM_PACKAGES_FILE)

    @staticmethod
    def _system_package_base(name):
        """Base package name, without the optional ``:arch`` / ``=version`` suffix."""
        match = SYSTEM_PACKAGE_RE.match((name or '').strip())
        return match.group('name') if match else ''

    def _system_packages_read(self, ssh):
        """Read the system package manifest. A missing file is an empty manifest."""
        _, output = self.pserver_id._exec_capture(
            'cat %s 2>/dev/null' % shlex.quote(self._system_packages_path()), ssh)
        lines = []
        for raw in (output or '').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and line not in lines:
                lines.append(line)
        return lines

    def _system_packages_write(self, ssh, lines):
        """Replace the system package manifest with ``lines``."""
        content = '\n'.join(lines) + '\n' if lines else ''
        self.pserver_id._create_file(ssh, self._system_packages_path(), content)

    def _system_packages_merge(self, packages, line):
        """Put ``line`` in the manifest, keeping a single entry per base package."""
        wanted = self._system_package_base(line)
        merged = []
        for existing in packages:
            if wanted and self._system_package_base(existing) == wanted:
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

    def _system_packages_installed(self, ssh):
        """Base package name -> version, for every APT package visible in the container."""
        installed = {}
        try:
            _, output = self.pserver_id._exec_capture(
                self._python_library_docker_command(
                    ['dpkg-query', '-W', '-f=${Package}\t${Version}\n'],
                    LIST_TIMEOUT_SECONDS), ssh)
        except Exception:
            # A base image without dpkg simply has no APT packages to report.
            _logger.exception(
                "Could not list the system packages of %s", self.display_name)
            return installed
        for raw in (output or '').splitlines():
            name, _, version = raw.strip().partition('\t')
            name = name.split(':', 1)[0].strip()
            if name:
                installed[name] = version.strip()
        return installed

    def _python_library_payload(self, ssh):
        """State the portal needs to render the wizard."""
        requirements = self._python_requirements_read(ssh)
        system_manifest = self._system_packages_read(ssh)
        container_running = bool(self.state == 'deploy' and self.operation_state == 'run')
        installed = self._python_libraries_installed(ssh) if container_running else {}
        installed_system = self._system_packages_installed(ssh) if container_running else {}
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
        system_packages = []
        for line in system_manifest:
            name = self._system_package_base(line) or line
            version = installed_system.get(name, '')
            system_packages.append({
                'name': name,
                'package': line,
                'version': version,
                'installed': bool(version),
            })
        # Keep the recorded image size in sync whenever the wizard is opened: images
        # built before this size existed get measured here, and the customer always
        # sees how much of the storage their package image takes.
        image_gb, image_name = 0.0, ''
        private_image = self.env['saas.docker.image'].search(
            [('instance_id', '=', self.id)], limit=1)
        if private_image:
            image_name = private_image.image_name
            try:
                image_gb = private_image.refresh_size_gb(self.pserver_id, ssh)
            except Exception:
                _logger.warning(
                    "Could not read the image size of %s", self.display_name)
                image_gb = private_image.size_gb or 0.0
        return {
            'requirements_path': self._python_requirements_path(),
            'system_packages_path': self._system_packages_path(),
            'container_running': container_running,
            'libraries': libraries,
            'system_packages': system_packages,
            'image_name': image_name,
            'image_size_gb': image_gb,
            'storage_used_gb': self.storage_used_gb or 0.0,
            'storage_limit_gb': self.storage_limit_gb or 0.0,
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

    def _system_packages_clean(self, text):
        """Validate the system packages field and return the list of APT packages.

        One package per line (commas are accepted too). Only a plain Debian package
        name is allowed, optionally with an architecture and/or a pinned version.
        """
        packages = []
        for raw in re.split(r'[\n,]+', text or ''):
            package = raw.strip()
            if not package or package.startswith('#'):
                continue
            if len(package) > 100:
                raise UserError(_(
                    "That system package name is too long (100 characters maximum)."))
            if not SYSTEM_PACKAGE_RE.match(package):
                raise UserError(_(
                    "\"%s\" is not a valid system package name. Use a plain APT package "
                    "name, for example \"python3-xmlsec\" or \"libxml2-dev\".") % package)
            if package not in packages:
                packages.append(package)
        return packages

    def _system_packages_clean_name(self, name):
        """Validate one APT package name (the Remove button only sends a name)."""
        name = (name or '').strip()
        if not name or not SYSTEM_PACKAGE_RE.match(name):
            raise UserError(_("Invalid system package name."))
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
    # ------------------------------------------------------------------
    # per-instance Docker image (libraries are baked in, never in a container layer)
    # ------------------------------------------------------------------
    def _python_library_base_image_name(self):
        """The image the private image of this instance is built FROM."""
        self.ensure_one()
        if self.docker_odoo_image:
            return self.docker_odoo_image
        version = self.odoo_server_id.odoo_version_id
        return 'odoo:%s' % (version.docker_image_tag or version.name)

    def _python_library_image(self, requirements=None, system_packages=None):
        """The private ``saas.docker.image`` of this instance, created on first use.

        There is exactly **one** image per instance, named after its Odoo version, so a
        library installed today and another one installed next month land in the *same*
        Dockerfile: the second install only appends its line and rebuilds the same tag.
        The image is built FROM the image the instance already uses, which keeps every
        system package it may carry (e.g. ``python3-xmlsec``).
        """
        self.ensure_one()
        Image = self.env['saas.docker.image']
        image = Image.search([('instance_id', '=', self.id)], limit=1)
        if image:
            return image
        base_name = self._python_library_base_image_name()
        base_image = Image.search(
            [('image_name', '=', base_name), ('instance_id', '=', False)], limit=1)
        version = (self.odoo_version_id
                   or self.odoo_server_id.odoo_version_id
                   or base_image.odoo_version_id)
        if not version:
            raise UserError(_(
                "The Odoo version of this instance is not set: the Python library "
                "image cannot be built."))
        tag = (base_image.tag_id
               or self.env.ref('s_odoo_saas_master.saas_instance_tag_standard',
                               raise_if_not_found=False)
               or self.env['saas.instance.tag'].search([], limit=1))
        if not tag:
            raise UserError(_(
                "No business typology is configured: the Python library image "
                "cannot be built."))
        image_name = '%s-custom-%s' % (base_name, self.technical_name)
        if Image.search_count([('image_name', '=', image_name)]):
            image_name = '%s-custom-%s-%s' % (base_name, self.technical_name, self.id)
        return Image.create({
            'name': _("Python libs - %s") % (self.name or self.technical_name),
            'odoo_version_id': version.id,
            'tag_id': tag.id,
            'edition': self.version_type or base_image.edition or 'community',
            'image_name': image_name,
            'base_image': base_name,
            'python_requirements': '\n'.join(requirements or []),
            'system_packages': '\n'.join(system_packages or []),
            'server_ids': [(6, 0, [self.pserver_id.id])],
            'instance_id': self.id,
            'build_status': 'not_built',
        })

    def _python_library_restore_dockerfile(self, image, ssh):
        """Put the instance Dockerfile back in sync with the image record.

        ``_python_library_build_image`` writes ``/home/<technical_name>/Dockerfile``
        *before* building it. When the build fails (typo in a package name, no matching
        wheel, unknown APT package, ...) the requirement has already been rolled back on
        the image record, so the file has to be regenerated from the restored record —
        otherwise the rejected package stays in the Dockerfile and breaks every later
        build, even though the customer only sees an error once.
        """
        self.ensure_one()
        try:
            image.invalidate_recordset(['dockerfile_content'])
            folder = '/home/%s' % self.technical_name
            self.pserver_id._create_file(
                ssh, '%s/Dockerfile' % folder, image.dockerfile_content)
        except Exception as error:
            _logger.warning(
                "Could not restore the Dockerfile of %s after a failed build: %s",
                self.display_name, error)

    @staticmethod
    def _python_library_clean_build_log(text):
        """Strip ANSI colours and the legacy-builder banner Docker prints on every build."""
        text = re.sub(r'\x1b\[[0-9;]*m', '', text or '')
        noise = (
            'DEPRECATED: The legacy builder',
            'Install the buildx component',
            'https://docs.docker.com/go/buildx',
        )
        lines = []
        for raw in text.splitlines():
            line = raw.rstrip()
            if any(marker in line for marker in noise):
                continue
            if line.startswith('+ pip install'):
                continue
            if not line.strip() and lines and not lines[-1].strip():
                continue
            lines.append(line)
        return '\n'.join(lines).strip()

    def _python_library_build_image(self, image, ssh):
        """Write the Dockerfile in the instance folder, build the tag, return the build log.

        The build context is the instance folder itself, so the Dockerfile lives next to
        the instance it belongs to (``/home/<technical_name>``) instead of a shared
        ``custom-images`` folder. A generated ``.dockerignore`` keeps the context tiny:
        without it Docker would tar ``pgdata/`` and ``odoo-web-data/`` (several GB) on
        every build.
        """
        self.ensure_one()
        image.write({'build_status': 'building', 'build_log': ''})
        folder = '/home/%s' % self.technical_name
        self.pserver_id._exec_cmd('mkdir -p %s' % shlex.quote(folder), ssh)
        self.pserver_id._create_file(ssh, '%s/Dockerfile' % folder, image.dockerfile_content)
        self.pserver_id._create_file(
            ssh, '%s/.dockerignore' % folder,
            '*\n!Dockerfile\n!requirements.txt\n')
        command = ('cd %s && docker build -t %s . > build.log 2>&1; echo BUILD_EXIT_CODE:$?'
                   % (shlex.quote(folder), shlex.quote(image.image_name)))
        output = self.pserver_id._exec_cmd(
            command, ssh, without_return=False, raise_on_error=False)
        exit_code = 0
        for output_line in output or []:
            if output_line.startswith('BUILD_EXIT_CODE:'):
                try:
                    exit_code = int(output_line.split(':', 1)[1].strip())
                except ValueError:
                    exit_code = 1
        _exit_code, build_log = self.pserver_id._exec_capture(
            'tail -n 200 %s' % shlex.quote('%s/build.log' % folder), ssh)
        build_log = self._python_library_clean_build_log(build_log)
        image.write({
            'build_status': 'built' if not exit_code else 'failed',
            'build_log': build_log[-2 * LOG_MAX_CHARS:],
        })
        # The rebuilt image is what the customer's storage is charged for: store the
        # real size docker reports so the storage usage can count it.
        if not exit_code:
            image.refresh_size_gb(self.pserver_id, ssh)
        if exit_code:
            # apt/pip could not resolve a package: that is a name/version problem, not a
            # Docker problem, so say it plainly instead of dumping the whole build log.
            missing_apt = re.search(
                r"Unable to locate package ([\w.+:-]+)", build_log)
            if not missing_apt:
                missing_apt = re.search(
                    r"Package ([\w.+:-]+) has no installation candidate", build_log)
            if missing_apt:
                raise UserError(_(
                    "The system package \"%s\" is not available in the APT repositories "
                    "of this Odoo image, so it was not installed. Check the exact "
                    "package name — for example \"python3-xmlsec\" or \"libxml2-dev\".")
                    % missing_apt.group(1))
            missing = re.search(
                r'No matching distribution found for ([^\s,;]+)', build_log)
            if not missing:
                missing = re.search(
                    r'Could not find a version that satisfies the requirement ([^\s,;]+)',
                    build_log)
            if missing:
                raise UserError(_(
                    "\"%s\" was not found on PyPI (or has no release for this Python "
                    "version), so it was not installed. Check the exact package name — "
                    "for example \"pandas\", \"psyquartz\" or \"python-escpos==3.1\". "
                    "A library hosted on GitHub or a private index cannot be installed "
                    "from this form.") % missing.group(1))
            raise UserError(_(
                "The Docker image %(image)s could not be built, so the packages were "
                "not installed.\n\n%(log)s") % {
                    'image': image.image_name,
                    'log': build_log[-LOG_MAX_CHARS:] or _("Docker returned no output.")})
        return build_log

    def _python_library_redeploy(self, image, ssh):
        """Use ``image`` for this instance and recreate its container from it."""
        self.ensure_one()
        if self.docker_image_id != image:
            self.write({'docker_image_id': image.id})
            self.invalidate_recordset(['docker_odoo_image'])
        self.pserver_id._create_docker_compose_file(self, ssh)
        self.pserver_id._docker_compose_up(self, ssh)

    def _python_library_revert_image(self, image, ssh):
        """Drop the private image once it has neither a requirement nor an APT package."""
        self.ensure_one()
        base = self.env['saas.docker.image'].search(
            [('image_name', '=', image.base_image), ('instance_id', '=', False)], limit=1)
        self.write({'docker_image_id': base.id if base else False})
        image.unlink()
        self.pserver_id._create_docker_compose_file(self, ssh)
        self.pserver_id._docker_compose_up(self, ssh)

    def python_library_install(self, requirement=None, system_packages=None):
        """Bake Python and/or APT packages into the instance image and redeploy it.

        The packages are **not** installed into the running container: they are added to
        the instance's Dockerfile (``pip install`` for the Python requirement,
        ``apt-get install`` for the system packages), the image is rebuilt and the
        container is recreated from it. That is what makes them survive a restart, a
        redeploy and even a ``docker compose down/up``. The Dockerfile lives on the server
        in the instance folder (``/home/<technical_name>/Dockerfile``) and is regenerated
        from the requirements of the image, so the next install simply appends its own
        line.

        Returns a portal payload: ``{'success': bool, ...}`` with either ``message`` or
        ``error``, plus the build ``log`` so the customer can see what happened.
        """
        self.ensure_one()
        line = (requirement or '').strip()
        if line:
            line = self._python_library_clean_requirement(line)
        apt_packages = self._system_packages_clean(system_packages)
        if not line and not apt_packages:
            raise UserError(_(
                "Enter a Python package, a system package, or both."))
        name = self._python_requirement_name(line) if line else ''
        # Fail fast (and without opening SSH) when the instance cannot be redeployed.
        self._python_library_container()
        ssh = self._python_library_ssh()
        image = None
        try:
            requirements = self._python_requirements_read(ssh)
            if line:
                requirements = self._python_requirements_merge(requirements, line)
            system_manifest = self._system_packages_read(ssh)
            for package in apt_packages:
                system_manifest = self._system_packages_merge(system_manifest, package)
            image = self._python_library_image(requirements, system_manifest)
            # Snapshot the Dockerfile content before adding the new lines: a failed build
            # (typo in the package name, no matching wheel, unknown APT package, ...)
            # must not leave the bad entry behind, otherwise every later install fails.
            previous_requirements = image.python_requirements or ''
            previous_system = image.system_packages or ''
            previous_status = image.build_status
            image.write({
                'python_requirements': '\n'.join(requirements),
                'system_packages': '\n'.join(system_manifest),
            })
            try:
                build_log = self._python_library_build_image(image, ssh)
            except UserError:
                # The requirement and the Dockerfile written on the server are both
                # rolled back: the customer only sees the error, and the instance keeps
                # the packages that really work.
                image.write({
                    'python_requirements': previous_requirements,
                    'system_packages': previous_system,
                    'build_status': previous_status or 'built',
                })
                self._python_library_restore_dockerfile(image, ssh)
                raise
            if name:
                # A copy left in the volume by an older ``pip --user`` install would
                # shadow the version baked into the image: drop it before recreating
                # the container.
                self.pserver_id._exec_capture(
                    self._python_library_docker_command(
                        ['pip', 'uninstall', '-y', '--break-system-packages',
                         '--disable-pip-version-check', name],
                        UNINSTALL_TIMEOUT_SECONDS), ssh)
            self._python_library_redeploy(image, ssh)
            stored = line
            if line and not REQUIREMENT_RE.match(line).group('specs'):
                version = self._python_libraries_installed(ssh).get(_normalize(name), '')
                if version:
                    stored = '%s==%s' % (line, version)
                    requirements = self._python_requirements_merge(
                        self._python_requirements_read(ssh), stored)
                    # Keep the Dockerfile reproducible: the pin is what the next build
                    # will install, and it matches what this build produced.
                    image.write({'python_requirements': '\n'.join(requirements)})
            self._python_requirements_write(ssh, requirements)
            self._system_packages_write(ssh, system_manifest)
            if stored:
                self._log_history(
                    _("Python library installed: %s") % stored,
                    category='python', level='success', icon='fa-cube',
                    summary=_("Baked into image %s") % image.image_name,
                    description=build_log[-1200:])
            if apt_packages:
                self._log_history(
                    _("System packages installed: %s") % ', '.join(apt_packages),
                    category='system', level='success', icon='fa-server',
                    summary=_("Baked into image %s") % image.image_name,
                    description=build_log[-1200:])
            if stored and apt_packages:
                message = _(
                    "%(python)s and %(count)s system package(s) are installed in "
                    "image %(image)s.") % {
                        'python': stored, 'count': len(apt_packages),
                        'image': image.image_name}
            elif stored:
                message = _("%s is installed in image %s.") % (stored, image.image_name)
            else:
                message = _("%s system package(s) are installed in image %s.") % (
                    ', '.join(apt_packages), image.image_name)
            return dict(self._python_library_payload(ssh), success=True,
                        message=message,
                        log=build_log[-LOG_MAX_CHARS:], image=image.image_name)
        except UserError as error:
            label = ', '.join(filter(None, [line, ', '.join(apt_packages)]))
            self._log_history(
                _("Package installation failed: %s") % label,
                category='system', level='danger', icon='fa-cube',
                summary=str(error)[:400])
            return {'success': False, 'error': str(error),
                    'image': image.image_name if image else ''}
        finally:
            ssh.close()

    def python_library_remove(self, name):
        """Remove a package from the instance Dockerfile and rebuild the image.

        The package is dropped from the image requirements and the image is rebuilt, so it
        really disappears from the instance (a container-layer uninstall would not survive
        the next redeploy). A possible ``--user`` copy left in the volume by the old flow is
        uninstalled too. When no requirement is left the instance simply goes back to its
        original base image.
        """
        self.ensure_one()
        name = self._python_library_clean_name(name)
        self._python_library_container()
        ssh = self._python_library_ssh()
        try:
            # Drop a possible ``--user`` copy left in the persisted volume by the old flow.
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
            image = self.env['saas.docker.image'].search(
                [('instance_id', '=', self.id)], limit=1)
            has_system = bool((image.system_packages or '').strip()) if image else False
            build_log = ''
            if image and (requirements or has_system):
                previous_requirements = image.python_requirements or ''
                previous_status = image.build_status
                image.write({'python_requirements': '\n'.join(requirements)})
                try:
                    build_log = self._python_library_build_image(image, ssh)
                except UserError:
                    # Nothing is removed when the rebuild fails: the record and the
                    # Dockerfile on the server go back to the previous requirements.
                    image.write({
                        'python_requirements': previous_requirements,
                        'build_status': previous_status or 'built',
                    })
                    self._python_library_restore_dockerfile(image, ssh)
                    raise
                self._python_library_redeploy(image, ssh)
            elif image:
                self._python_library_revert_image(image, ssh)
            self._python_requirements_write(ssh, requirements)
            message = _("%s is uninstalled and removed from the instance image.") % name
            self._log_history(
                _("Python library removed: %s") % name,
                category='python', level='success', icon='fa-cube',
                summary=self._python_library_error(log) if exit_code else '',
                description=(build_log or log)[-1200:])
            return dict(self._python_library_payload(ssh), success=True,
                        message=message, log=build_log or log)
        except UserError as error:
            self._log_history(
                _("Python library removal failed: %s") % name,
                category='python', level='danger', icon='fa-cube',
                summary=str(error)[:400])
            return {'success': False, 'error': str(error)}
        finally:
            ssh.close()

    def system_package_remove(self, name):
        """Remove an APT package from the instance Dockerfile and rebuild the image.

        The package is dropped from the image and the image is rebuilt, so it really
        disappears from the instance (a container-layer uninstall would not survive the
        next redeploy). When nothing is left — no Python requirement and no system
        package — the instance simply goes back to its original base image.
        """
        self.ensure_one()
        name = self._system_packages_clean_name(name)
        wanted = self._system_package_base(name)
        self._python_library_container()
        ssh = self._python_library_ssh()
        try:
            packages = [
                line for line in self._system_packages_read(ssh)
                if self._system_package_base(line) != wanted
            ]
            image = self.env['saas.docker.image'].search(
                [('instance_id', '=', self.id)], limit=1)
            has_python = bool((image.python_requirements or '').strip()) if image else False
            build_log = ''
            if image and (packages or has_python):
                previous_system = image.system_packages or ''
                previous_status = image.build_status
                image.write({'system_packages': '\n'.join(packages)})
                try:
                    build_log = self._python_library_build_image(image, ssh)
                except UserError:
                    # Same rollback as an install: a failed rebuild must leave the
                    # Dockerfile on the server exactly as it was.
                    image.write({
                        'system_packages': previous_system,
                        'build_status': previous_status or 'built',
                    })
                    self._python_library_restore_dockerfile(image, ssh)
                    raise
                self._python_library_redeploy(image, ssh)
            elif image:
                self._python_library_revert_image(image, ssh)
            self._system_packages_write(ssh, packages)
            message = _("%s is removed from the instance image.") % name
            self._log_history(
                _("System package removed: %s") % name,
                category='system', level='success', icon='fa-server',
                description=build_log[-1200:])
            return dict(self._python_library_payload(ssh), success=True,
                        message=message, log=build_log)
        except UserError as error:
            self._log_history(
                _("System package removal failed: %s") % name,
                category='system', level='danger', icon='fa-server',
                summary=str(error)[:400])
            return {'success': False, 'error': str(error)}
        finally:
            ssh.close()
