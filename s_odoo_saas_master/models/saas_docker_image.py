# -*- coding: utf-8 -*-
import json
import logging
import re
import shlex

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DockerImage(models.Model):
    _name = 'saas.docker.image'
    _description = "SaaS Docker Image"
    _order = 'name'

    name = fields.Char(string="Name", required=True)
    odoo_version_id = fields.Many2one(
        'saas.odoo.version', string="Odoo Version", required=True,
        default=lambda self: self._default_odoo_version_id(),
        help="Odoo version this image is based on.")
    tag_id = fields.Many2one(
        'saas.instance.tag', string="Business Typology", required=True,
        default=lambda self: self._default_tag_id(),
        help="Business typology linked to this image (BTP, OCR, ...).")
    edition = fields.Selection([
        ('community', 'Community'),
        ('enterprise', 'Enterprise'),
    ], string="Edition", default=lambda self: self._default_edition(), required=True,
        help="Odoo edition baked into this image. Defaults to the edition (version type) "
             "of the instance the image is built for. Enterprise images embed "
             "the enterprise addons; community images are based on the "
             "official community image (enterprise addons mounted at runtime, if any).")
    image_name = fields.Char(
        string="Docker Image Name", required=True,
        help="Full image reference used in docker-compose. Convention: "
             "odoo:<version>-<edition>-<typology> where <edition> is "
             "'cmy' (Community) or 'ent' (Enterprise), e.g. "
             "'odoo:19.0-cmy-btp' or 'odoo:19.0-ent-btp'.")
    base_image = fields.Char(
        string="Base Image", required=True,
        default=lambda self: self._default_base_image(),
        help="Base image used as FROM, e.g. 'odoo:19.0'.")
    system_packages = fields.Text(
        string="System Packages",
        help="APT packages to install (one per line).")
    python_requirements = fields.Text(
        string="Python Requirements",
        help="Pip packages to install (one per line).")
    auto_requirements = fields.Text(
        string="Auto-Detected Requirements", readonly=True,
        help="Requirements detected in custom-addons (requirements*.txt) on the "
             "build servers during the last build / detection.")
    ignored_requirements_files = fields.Text(
        string="Ignored Requirements Files",
        help="Paths or glob patterns (fnmatch) of requirements*.txt to SKIP during "
             "auto-detection (one per line). Useful when a custom-addon pins a "
             "requirement incompatible with the image's Python version.")
    dockerfile_content = fields.Text(
        string="Dockerfile", compute='_compute_dockerfile_content',
        help="Dockerfile generated automatically from the fields above.")
    build_status = fields.Selection([
        ('not_built', 'Not Built'),
        ('building', 'Building'),
        ('built', 'Built'),
        ('failed', 'Failed'),
    ], string="Build Status", default='not_built', required=True)
    build_log = fields.Text(string="Build Log", readonly=True)
    size_gb = fields.Float(
        string="Image Size (GB)", readonly=True, copy=False,
        help="Size of the built image on the server, in GB. It is read from "
             "``docker image inspect`` after every build and counted in the storage "
             "quota of the instance this image belongs to (a rebuilt image with extra "
             "Python libraries or system packages takes space on the customer's storage).")
    server_ids = fields.Many2many(
        'saas.pserver', string="Build Servers",
        help="Servers where this image must be built/available (prod + DR).")
    instance_id = fields.Many2one(
        'saas.odoo.instance', string="Instance", ondelete='cascade', index=True,
        help="When set, this is the private image of that instance: every Python "
             "library the customer installs from the portal is baked into it "
             "(Dockerfile + rebuild), so it survives restarts and redeploys.")

    _sql_constraints = [
        ('image_name_uniq', 'unique(image_name)',
         'Docker image name must be unique!'),
    ]

    @api.model
    def _default_tag_id(self):
        """Default the Business Typology to the ``Standard`` typology."""
        tag = self.env.ref(
            's_odoo_saas_master.saas_instance_tag_standard',
            raise_if_not_found=False)
        if not tag:
            tag = self.env['saas.instance.tag'].search(
                [('name', '=ilike', 'Standard')], limit=1)
        return tag.id if tag else False

    @api.model
    def _default_instance(self):
        """Instance the record is prepared for (``default_instance_id`` in the context)."""
        instance_id = self.env.context.get('default_instance_id')
        if not instance_id:
            return self.env['saas.odoo.instance']
        return self.env['saas.odoo.instance'].browse(int(instance_id)).exists()

    @api.model
    def _default_odoo_version_id(self):
        """Odoo version of the instance the image is built for."""
        instance = self._default_instance()
        return instance.odoo_version_id.id if instance.odoo_version_id else False

    @api.model
    def _default_edition(self):
        """Edition (version type) of the instance the image is built for."""
        instance = self._default_instance()
        return instance.version_type or 'community'

    @api.model
    def _default_base_image(self):
        """``odoo:<tag>`` of the instance Odoo version, else the historical default."""
        version = self._default_instance().odoo_version_id
        if version:
            return 'odoo:%s' % (version.docker_image_tag or version.name)
        return 'odoo:19.0'

    def _apply_instance_values(self, vals):
        """Copy the instance Odoo version, edition and base image into ``vals``."""
        instance = self.env['saas.odoo.instance'].browse(
            vals.get('instance_id') or []).exists()
        if not instance:
            return vals
        version = instance.odoo_version_id
        if version:
            vals.setdefault('odoo_version_id', version.id)
            vals.setdefault(
                'base_image', 'odoo:%s' % (version.docker_image_tag or version.name))
        if instance.version_type:
            vals.setdefault('edition', instance.version_type)
        return vals

    @api.onchange('instance_id')
    def _onchange_instance_id(self):
        """A new record for an instance inherits its Odoo version and edition."""
        if not self.instance_id:
            return
        version = self.instance_id.odoo_version_id
        if version:
            self.odoo_version_id = version
            self.base_image = 'odoo:%s' % (version.docker_image_tag or version.name)
        if self.instance_id.version_type:
            self.edition = self.instance_id.version_type
        if not self.tag_id:
            self.tag_id = self._default_tag_id()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id'):
                self._apply_instance_values(vals)
        return super().create(vals_list)

    # docker reports sizes with decimal units (kB, MB, GB = 1000^n), which is exactly
    # the "DISK USAGE" column of ``docker image ls`` / ``docker system df``.
    _SIZE_UNITS = {
        'B': 1, 'KB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3, 'TB': 1000 ** 4,
    }

    @api.model
    def _human_size_to_bytes(self, value):
        """``'3.62GB'`` → ``3620000000`` (0 when the string is not a size)."""
        match = re.match(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?B)\s*$', value or '', re.I)
        if not match:
            return 0
        amount, unit = match.groups()
        return int(float(amount) * self._SIZE_UNITS.get(unit.upper(), 0))

    def refresh_size_gb(self, server, ssh):
        """Read the real on-disk size of this image and store it.

        ``docker image inspect .Size`` only returns the **content size** (839 MB for an
        image whose layers really take 3.62 GB on disk). The number the customer is
        charged for is the ``DISK USAGE`` shown by ``docker system df``, so the size is
        read from there; ``docker image inspect`` is only the fallback when that command
        is not available on the server.
        """
        self.ensure_one()
        command = "docker system df -v --format '{{json .}}' 2>/dev/null || true"
        _exit_code, output = server._exec_capture(command, ssh)
        size_gb = 0.0
        payload = (output or '').strip()
        if payload:
            try:
                data = json.loads(payload.splitlines()[0])
            except ValueError:
                data = {}
            for image in data.get('Images') or []:
                repository = image.get('Repository') or ''
                tag = image.get('Tag') or ''
                if tag in ('', '<none>'):
                    name = repository
                else:
                    name = '%s:%s' % (repository, tag)
                if name != self.image_name:
                    continue
                size_gb = round(
                    self._human_size_to_bytes(image.get('Size')) / (1000 ** 3), 2)
                break
        if not size_gb:
            # Fallback: the content size (still better than storing nothing).
            command = (
                "docker image inspect %s --format '{{.Size}}' 2>/dev/null || true"
                % shlex.quote(self.image_name))
            _exit_code, output = server._exec_capture(command, ssh)
            lines = [line.strip() for line in (output or '').splitlines() if line.strip()]
            if lines and lines[-1].isdigit():
                size_gb = round(int(lines[-1]) / (1000 ** 3), 2)
        self.write({'size_gb': size_gb})
        return size_gb

    @api.depends('base_image', 'system_packages', 'python_requirements')
    def _compute_dockerfile_content(self):
        for r in self:
            r.dockerfile_content = self._generate_dockerfile(
                r.base_image or 'odoo:19.0',
                self._split_lines(r.system_packages),
                self._split_lines(r.python_requirements),
                use_requirements_file=False,
            )

    @staticmethod
    def _generate_dockerfile(base_image, packages, requirements,
                             use_requirements_file=False):
        """Génère le Dockerfile.

        :param base_image: image de base (FROM)
        :param packages: liste des paquets apt
        :param requirements: liste des requirements pip
        :param use_requirements_file: si True, écrit le pip install via un
            requirements.txt copié dans le build (nécessite que le fichier soit
            écrit à côté du Dockerfile par l'appelant).
        """
        lines = ['FROM %s' % (base_image or 'odoo:19.0')]
        if packages or requirements:
            lines += ['', 'USER root']
        if packages:
            lines += [
                '',
                '# Paquets système',
                'RUN apt-get update && apt-get install -y --no-install-recommends \\',
            ]
            lines += ['    %s \\' % pkg for pkg in packages]
            lines.append('    && rm -rf /var/lib/apt/lists/*')
        if requirements:
            lines += [
                '',
                '# Dépendances Python (bypass PEP 668)',
            ]
            if use_requirements_file:
                lines += [
                    'COPY requirements.txt /tmp/requirements.txt',
                    '# setuptools/wheel pré-installés (compatibilité sdists)',
                    'RUN pip install --break-system-packages --no-cache-dir '
                    'setuptools wheel',
                    'RUN pip install --break-system-packages --no-cache-dir '
                    '-r /tmp/requirements.txt',
                ]
            else:
                lines += [
                    'RUN pip install --break-system-packages --no-cache-dir \\',
                ]
                lines += ['    %s \\' % pkg for pkg in requirements[:-1]]
                lines.append('    %s' % requirements[-1])
        if packages or requirements:
            lines += ['', 'USER odoo']
        return '\n'.join(lines) + '\n'

    @staticmethod
    def _split_lines(text):
        if not text:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    @staticmethod
    def _merge_requirements(*groups):
        """Fusionne plusieurs listes de requirements en dédupliquant (ordre conservé)."""
        seen = set()
        out = []
        for group in groups:
            for line in group or []:
                key = line.strip()
                if key and key not in seen:
                    seen.add(key)
                    out.append(line)
        return out

    @staticmethod
    def _is_safe_requirement_line(line):
        """Filtre les lignes de requirements dangereuses/non portables dans le
        contexte de build : chemins locaux (./, ../, /) et références à
        d'autres fichiers (-r/-c/--requirement/--constraint)."""
        line = line.strip()
        if not line or line.startswith('#'):
            return None
        low = line.lower()
        if low.startswith(('./', '../', '/')):
            return None
        if low.startswith(('-r ', '-c ', '--requirement', '--constraint')):
            return None
        return line

    def _detect_server_requirements(self, server, ssh):
        """Scanne les custom-addons du serveur physique (via ses odoo.servers
        + découverte des /home/*/custom-addons d'instances) et retourne
        (requirements_lines, fichiers_trouvés)."""
        odoo_servers = self.env['saas.odoo.server'].search(
            [('pserver_id', '=', server.id)])
        paths = []
        for osrv in odoo_servers:
            for addon in osrv.extra_addon_ids:
                if addon.source_path and addon.source_path not in paths:
                    paths.append(addon.source_path)
        # Découverte des custom-addons des instances (/home/<instance>/custom-addons)
        discover_out = server._exec_cmd(
            'find /home -maxdepth 3 -type d -name custom-addons -print 2>/dev/null',
            ssh, without_return=False, raise_on_error=False)
        for line in discover_out:
            p = line.strip()
            if p and p not in paths:
                paths.append(p)
        if not paths:
            return [], []
        quoted = ' '.join(shlex.quote(p) for p in paths)
        files_cmd = ('find %s -maxdepth 3 -type f '
                     '-iname "requirements*.txt" -print 2>/dev/null' % quoted)
        files_out = server._exec_cmd(files_cmd, ssh, without_return=False,
                                     raise_on_error=False)
        files = [line.strip() for line in files_out if line.strip()]
        # Exclusion des fichiers correspondant aux motifs ignorés
        files = [f for f in files if not self._is_ignored_requirements_file(f)]
        if not files:
            return [], []
        # Parsing fichier par fichier (évite la concaténation de fichiers
        # sans retour à la ligne final)
        lines = []
        for f in files:
            out = server._exec_cmd('cat %s' % shlex.quote(f), ssh,
                                   without_return=False, raise_on_error=False)
            for line in out:
                safe = self._is_safe_requirement_line(line)
                if safe:
                    lines.append(safe)
        return lines, files

    def _is_ignored_requirements_file(self, file_path):
        import fnmatch
        patterns = self._split_lines(self.ignored_requirements_files)
        if not patterns:
            return False
        base = file_path.split('/')[-1]
        for pattern in patterns:
            if fnmatch.fnmatch(file_path, pattern) \
                    or fnmatch.fnmatch(base, pattern):
                return True
        return False

    def action_detect_requirements(self):
        """Aperçu : détecte les requirements*.txt des custom-addons sur les
        serveurs sélectionnés et remplit auto_requirements (sans builder)."""
        self.ensure_one()
        if not self.server_ids:
            raise UserError(
                _("Please select at least one build server before detecting."))
        manual = self._split_lines(self.python_requirements)
        all_detected = []
        total_files = 0
        for server in self.server_ids:
            ssh = None
            try:
                ssh = server._connect_or_raise()
                detected, files = self._detect_server_requirements(server, ssh)
                all_detected.extend(detected)
                total_files += len(files)
            except Exception as e:
                raise UserError(
                    _("Detection failed on %s: %s") % (server.name, e))
            finally:
                if ssh:
                    ssh.close()
        merged = self._merge_requirements(manual, all_detected)
        self.write({'auto_requirements': ('\n'.join(merged) + '\n') if merged else ''})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("Détection terminée : %s fichier(s), %s requirement(s) "
                             "(%s auto-détectés).")
                            % (total_files, len(merged), len(all_detected)),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_build_image(self):
        self.ensure_one()
        if not self.server_ids:
            raise UserError(
                _("Please select at least one build server before building."))
        self.write({'build_status': 'building', 'build_log': ''})
        all_logs = []
        failed = []
        manual = self._split_lines(self.python_requirements)
        global_detected = []
        global_files = []
        for server in self.server_ids:
            ssh = None
            try:
                ssh = server._connect_or_raise()
                folder = '/opt/odoo18/custom-images/%s' % self._slug()
                server._exec_cmd('mkdir -p %s' % folder, ssh)
                detected, files = self._detect_server_requirements(server, ssh)
                global_detected.extend(detected)
                global_files.extend(files)
                merged = self._merge_requirements(manual, detected)
                all_logs.append('=== %s (%s) ==='
                                % (server.name, server._get_managing_ip()))
                if detected:
                    dockerfile = self._generate_dockerfile(
                        self.base_image or 'odoo:19.0',
                        self._split_lines(self.system_packages),
                        merged,
                        use_requirements_file=True,
                    )
                    server._create_file(ssh, '%s/Dockerfile' % folder, dockerfile)
                    server._create_file(
                        ssh, '%s/requirements.txt' % folder,
                        '\n'.join(merged) + '\n')
                    all_logs.append(
                        'AUTO-REQS: %d fichier(s) détecté(s) → %d requirement(s) '
                        '(dont %d auto-détectés)'
                        % (len(files), len(merged), len(detected)))
                    for f in files:
                        all_logs.append('  + %s' % f)
                else:
                    server._create_file(ssh, '%s/Dockerfile' % folder,
                                        self.dockerfile_content)
                    all_logs.append(
                        'AUTO-REQS: aucun requirements*.txt détecté')
                cmd = ('cd %s && docker build -t %s . > build.log 2>&1; '
                       'echo BUILD_EXIT_CODE:$?'
                       % (folder, self.image_name))
                out = server._exec_cmd(cmd, ssh, without_return=False,
                                       raise_on_error=False)
                all_logs.extend(line.rstrip() for line in out)
                exit_code = None
                for line in out:
                    if line.startswith('BUILD_EXIT_CODE:'):
                        exit_code = int(line.split(':', 1)[1].strip())
                log_lines = server._exec_cmd(
                    'cat %s/build.log' % folder, ssh, without_return=False,
                    raise_on_error=False)
                all_logs.extend(line.rstrip() for line in log_lines)
                if exit_code != 0:
                    failed.append('%s (exit %s)' % (server.name, exit_code))
            except Exception as e:
                failed.append('%s (%s)' % (server.name, e))
                all_logs.append('ERROR: %s' % e)
            finally:
                if ssh:
                    ssh.close()
        merged_all = self._merge_requirements(manual, global_detected)
        self.write({
            'build_log': '\n'.join(all_logs),
            'auto_requirements': ('\n'.join(merged_all) + '\n')
                                 if merged_all else '',
        })
        if failed:
            self.write({'build_status': 'failed'})
            raise UserError(
                _("Docker image build failed on: %s")
                % ', '.join(failed))
        self.write({'build_status': 'built'})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("Image %s built successfully.")
                            % self.image_name,
                'type': 'success',
                'sticky': False,
            }
        }

    def action_reset_build(self):
        self.write({'build_status': 'not_built', 'build_log': ''})

    def _slug(self):
        return (self.image_name or self.name).replace(':', '-').replace('/', '-')
