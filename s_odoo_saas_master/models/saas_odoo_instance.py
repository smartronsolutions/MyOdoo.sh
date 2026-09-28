import random
import string
import os
import re
import shlex
import subprocess
import threading
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
import logging
import psycopg2
import time
import base64
import secrets
from odoo import fields, models, api, _, SUPERUSER_ID
from odoo.exceptions import ValidationError, UserError
from odoo.modules.registry import Registry
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

# Live shell: one cached SSH connection per worker process. The tmux sessions themselves
# live on the physical server, which is what makes the terminal worker-independent.
_SHELL_SSH_CACHE = {}
_SHELL_SSH_LOCK = threading.Lock()

# Per-worker memory allocation (bytes), used for the generated odoo.conf limits
# and the docker-compose container memory limit.
WORKER_MEMORY_SOFT = 2 * 1024 * 1024 * 1024          # 2 GB per worker
WORKER_MEMORY_HARD = int(2.5 * 1024 * 1024 * 1024)   # 2.5 GB per worker

# A single instance can never run more than this many workers (buy cap + validation).
MAX_WORKERS_PER_INSTANCE = 4

# Installing this module turns a plain Community database into an Enterprise one. The
# Enterprise addons themselves are never baked into the image: they are mounted inside the
# container from the server's "standard extra addons" volume (/mnt/standard-extra-addons),
# which is why installing the module is the only thing left to do to get Enterprise.
ENTERPRISE_MODULE = 'web_enterprise'


def _is_db_concurrency_error(exc):
    """True for PostgreSQL concurrency errors Odoo retries at the request level.

    Odoo runs its cursors in REPEATABLE READ. When two requests touch the same record
    at the same time, PostgreSQL raises a serialization/deadlock/lock error which must
    bubble up so ``odoo.service.model.retrying`` can replay the whole request. Swallowing
    it would leave the transaction aborted and break the request.
    """
    if type(exc).__name__ in ('SerializationFailure', 'DeadlockDetected', 'LockNotAvailable'):
        return True
    return 'could not serialize' in str(exc).lower()


class OdooInstance(models.Model):
    _name = 'saas.odoo.instance'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _description = "SaaS Odoo Instance"

    # Expose the module constants on the model so a recordset can read them
    # (e.g. ``instance.MAX_WORKERS_PER_INSTANCE``).
    MAX_WORKERS_PER_INSTANCE = MAX_WORKERS_PER_INSTANCE
    WORKER_MEMORY_SOFT = WORKER_MEMORY_SOFT
    WORKER_MEMORY_HARD = WORKER_MEMORY_HARD
    ENTERPRISE_MODULE = ENTERPRISE_MODULE

    @api.model
    def _default_based_domain(self):
        based_domain = self.env['saas.based.domain'].search([], limit=1)
        return based_domain or False

    @api.model
    def _default_odoo_version(self):
        # Prefer Odoo version from active Odoo server
        server = self.env['saas.odoo.server'].search([('active', '=', True)], limit=1)
        if not server:
            server = self.env['saas.odoo.server'].search([], limit=1)
        if server and server.odoo_version_id:
            return server.odoo_version_id
        odoo_version = self.env['saas.odoo.version'].search([], order='id desc', limit=1)
        return odoo_version or False

    @api.model
    def _default_backup_limit(self):
        return self.env.user.company_id.instance_backup_limit or 5

    name = fields.Char(string="Subdomain", required=True)
    url = fields.Char(string="URL", compute='_compute_url', store=True)
    technical_name = fields.Char(string="Technical Name", compute='_compute_technical_name', store=True)
    noindex = fields.Boolean(string="No Index", default=True,
        help="If checked, search engines (Google, Bing, etc.) will not index this instance. Keep checked for client or test instances that should not appear in search results.")
    domain_name = fields.Char(string="Domain Name", compute='_compute_domain_name', store=True)
    based_domain_id = fields.Many2one('saas.based.domain', string="Based Domain", required=True, default=_default_based_domain)
    odoo_version_id = fields.Many2one('saas.odoo.version', string='Odoo Version', required=True, default=_default_odoo_version)
    version_type = fields.Selection([
        ('community', 'Community'),
        ('enterprise', 'Enterprise'),
    ], string='Version Type', default='community', required=True,
        help="Community or Enterprise edition. The Odoo server matching this version + type "
             "is selected automatically.")
    odoo_server_id = fields.Many2one('saas.odoo.server', string='Odoo Server', required=True,
        compute='_compute_odoo_server_id', store=True, readonly=False)
    pserver_id = fields.Many2one(related='odoo_server_id.pserver_id', store=True)
    port_ids = fields.One2many('saas.odoo.instance.port', 'instance_id', string='Odoo Instance Ports', readonly=True)
    user_demo_data = fields.Boolean(string='Use Demo Data')
    config_ids = fields.One2many('saas.odoo.instance.config', 'instance_id', string='Configs')
    admin_pass = fields.Char(
        string='Master Password',
        help="Odoo master password (``admin_passwd``) of this instance. Changing it rewrites "
             "``odoo.conf`` on the instance server and restarts the Odoo container so the new "
             "value takes effect.")
    db_name = fields.Char(string='Database Name', compute='_compute_db_name', store=True)
    domain_name_ids = fields.One2many('saas.odoo.instance.domain.name', 'instance_id', string='Domains Name')
    domain_name_count = fields.Integer(string="Domain Name Count", compute='_compute_domain_name_count')
    enable_autobackup = fields.Boolean(string="Enable Autobackup", default=True)
    installed_app_ids = fields.One2many('saas.odoo.instance.installed.app', 'instance_id', string='Installed Apps')
    installed_app_count = fields.Integer(string="Installed Apps Count", compute='_compute_installed_app_count')
    backup_limit = fields.Integer(string='Backup Limit', required=True, default=_default_backup_limit)
    backup_ids = fields.One2many('saas.odoo.instance.backup', 'instance_id', string='Backups')
    container_backup_ids = fields.One2many(
        'saas.odoo.instance.container.backup', 'instance_id', string='Container Backups'
    )
    backup_count = fields.Integer(string="Backup Count", compute='_compute_backup_count')
    # Live state of a (background) backup so the portal can show a progress bar that
    # survives a page refresh — the job itself runs in its own thread on the server.
    backup_state = fields.Selection([
        ('idle', 'Idle'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], string='Backup State', default='idle', required=True)
    backup_progress = fields.Integer(string='Backup Progress (%)', default=0)
    backup_message = fields.Char(string='Backup Message')
    backup_running_type = fields.Selection([
        ('manual', 'Manual'),
        ('auto', 'Automatic'),
    ], string='Running Backup Type')
    backup_started_at = fields.Datetime(string='Backup Started At')
    backup_finished_at = fields.Datetime(string='Backup Finished At')
    history_ids = fields.One2many(
        'saas.odoo.instance.history', 'instance_id', string='Activity History'
    )
    history_count = fields.Integer(string="History Count", compute='_compute_history_count')
    extra_addon_ids = fields.One2many('saas.odoo.instance.extra.addon', 'instance_id', string='Extra Addons')
    custom_addon_ids = fields.One2many('saas.odoo.instance.custom.addon', 'instance_id', string='Custom Addons',
        compute='_compute_custom_addon_ids', store=True, readonly=False)
    github_repo_url = fields.Char(string='GitHub Repository URL', help="e.g. https://github.com/organization/my-odoo-addons")
    github_repo_name = fields.Char(string='GitHub Repository Name', help="e.g. organization/my-odoo-addons")
    github_branch = fields.Char(string='GitHub Branch', default='main')
    github_token = fields.Char(string='GitHub Access Token', help="Personal Access Token for private repositories")
    github_connected = fields.Boolean(string='GitHub Connected', compute='_compute_github_connected', store=True)
    github_log_ids = fields.One2many(
        'saas.odoo.instance.github.log', 'instance_id', string='GitHub Sync Logs')
    last_redeploy_date = fields.Datetime(string='Last Redeploy Date', readonly=True)
    last_sync_message = fields.Char(string='Last Sync Message')
    default_module = fields.Char(string='Default Modules', help="Modules are separated by commas")
    trial = fields.Boolean(string='Trial')
    expiration_date = fields.Date(string='Expiration Date', compute='_compute_expiration_date', store=True, readonly=False)
    active_user = fields.Integer(string='Active Users', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer')
    company_id = fields.Many2one('res.company', string="Company", default=lambda self: self.env.user.company_id)
    resource_package_id = fields.Many2one('saas.resource.package', string='Resource Package', compute='_compute_resource_package_id', store=True, readonly=False)
    resource_package_line_ids = fields.One2many('saas.odoo.instance.resource.package.line', 'instance_id', string='Resource Package Lines',
        compute='_compute_resource_package_line_ids', store=True, readonly=False)
    storage_limit_gb = fields.Float(string='Storage Limit (GB)', default=5.0)
    storage_used_gb = fields.Float(string='Storage Used (GB)', compute='_compute_storage_usage', store=False)
    storage_odoo_data_gb = fields.Float(string='Odoo data — odoo-web-data (GB)',
        compute='_compute_storage_usage', store=False)
    storage_database_gb = fields.Float(string='Database — pgdata (GB)',
        compute='_compute_storage_usage', store=False)
    storage_percentage = fields.Float(string='Storage Usage %', compute='_compute_storage_usage', store=False)
    workers_count = fields.Integer(string='Workers', default=1, required=True,
        help="Number of Odoo worker processes the instance runs with. Also drives the "
             "container memory allocation (2 GB soft / 2.5 GB hard per worker).")
    suspension_reason = fields.Selection([
        ('manual', 'Manual'),
        ('storage_full', 'Storage Full'),
        ('expired', 'Expired'),
        ('non_payment', 'Non-Payment'),
    ], string='Suspension Reason', default=False, copy=False, index=True)
    deployment_state = fields.Selection([
        ('idle', 'Idle'),
        ('pending', 'Queued'),
        ('deploying', 'Deploying'),
        ('deployed', 'Deployed'),
        ('failed', 'Failed'),
    ], string='Deployment State', default='idle', copy=False, index=True,
        help="Progress of the automatic deployment triggered after a new instance is purchased.")
    deployment_error = fields.Text(string='Deployment Error', copy=False)
    deployment_started_at = fields.Datetime(string='Deployment Started At', copy=False)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('deploy', 'Deployed'),
        ('suspend', 'Suspended'),
        ('cancel', 'Cancelled'),
    ], string="Status", copy=False, index=True, readonly=True, tracking=True, default='draft')
    operation_state = fields.Selection([
        ('draft', 'Draft'),
        ('run', 'Running'),
        ('stop', 'Stopped')
    ], string='Operation Status', default='draft', readonly=True)
    is_template = fields.Boolean(string='Is Template?', help="This instance will be used as a template for other instances. "
                                 "Then the database, file store,... of this instance will be copied to the new instance as a template.")
    template_tag = fields.Many2many('saas.instance.tag', string='Tag',
        help="Identify the type of instance")
    deploy_mail_template_id = fields.Many2one('mail.template', string='Deploy Email Template',
        help="Email template used when deploying instances created from this instance template. "
        "It is useful when the login information of this template is different from the default information. "
        "In that case, you need a separate email template for this instance template. "
        "Keep empty to use default.")
    use_template = fields.Boolean(string='Use Template', help="Use another instance's database, file store,... as a template")
    template_instance_domain_ids = fields.Many2many('saas.odoo.instance', compute='_compute_template_instance_domain_ids',
        help="Technical field used to filter domain 'template_instance_id'")
    template_instance_id = fields.Many2one('saas.odoo.instance', string='Instance Template',
        domain="[('is_template', '=', True), ('state', '=', 'deploy'), ('odoo_version_id', '=', odoo_version_id)]")

    # docker
    docker_image_id = fields.Many2one('saas.docker.image', string='Docker Image',
        help="Custom Docker image for this instance. Leave empty to use the default image of the Odoo version.")
    docker_odoo_image = fields.Char(string='Odoo Image', compute='_compute_docker_compose', store=True)
    docker_psql_image = fields.Char(string='PSQL Image', compute='_compute_docker_compose', store=True)
    docker_xmlrpc_expose_port = fields.Char(string='Xmlrpc Expose Port', compute='_compute_docker_compose', store=True)
    docker_xmlrpcs_expose_port = fields.Char(string='Xmlrpcs Expose Port', compute='_compute_docker_compose', store=True)
    docker_longpolling_expose_port = fields.Char(string='Longpolling Expose Port', compute='_compute_docker_compose', store=True)
    docker_xmlrpc_container_port = fields.Char(string='Xmlrpc Container Port', compute='_compute_docker_compose', store=True)
    docker_xmlrpcs_container_port = fields.Char(string='Xmlrpcs Container Port', compute='_compute_docker_compose', store=True)
    docker_longpolling_container_port = fields.Char(string='Longpolling Container Port', compute='_compute_docker_compose', store=True)
    docker_container_ids = fields.One2many('saas.odoo.instance.docker.container', 'instance_id', string='Docker Containers',
        compute='_compute_docker_compose', store=True)
    docker_container_count = fields.Integer(string="Docker Container Count", compute='_compute_docker_container_count')
    docker_compose_volume_ids = fields.One2many('saas.odoo.instance.docker.compose.volume', 'instance_id', string='Docker Compose Volumes', readonly=True)
    docker_compose_volume_count = fields.Integer(string='Docker Compose Volume Count', compute='_compute_docker_compose_volume_count')
    need_to_compose_up = fields.Boolean(string='Need to run docker compose up -d', readonly=True)

    # sale
    subscription_type = fields.Selection([
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
    ], string='Subscription Type', readonly=True)
    sale_order_ids = fields.One2many('sale.order', 'instance_id', string='Sale Orders', readonly=True, groups="sales_team.group_sale_salesman")
    sale_order_count = fields.Integer(string='Sale Order Count', compute='_compute_sale_order_count', store=True, compute_sudo=True)
    paid_user = fields.Integer(string='Paid User', compute='_compute_paid_user', store=True)
    not_paid_app_count = fields.Integer(string="Not Paid Apps Count", compute='_compute_not_paid_app_count', store=True)
    account_move_ids = fields.One2many('account.move', 'instance_id', string='Invoices', readonly=True, groups="account.group_account_invoice")
    account_move_count = fields.Integer(string='Invoice Count', compute='_compute_account_move_count', store=True, compute_sudo=True)
    has_extra = fields.Boolean(string='Has extra addons or user', compute='_compute_has_extra', store=True)
    buy_now_from_pricing = fields.Boolean(help="Technical field")

    _sql_constraints = [
        ('name_uniq', 'unique(name,based_domain_id)', 'Subdomain must be unique per based domain!')
    ]

    @api.constrains('workers_count')
    def _check_workers_count(self):
        max_workers = self.env['saas.odoo.instance'].MAX_WORKERS_PER_INSTANCE
        for r in self:
            if r.workers_count is not False and (r.workers_count < 1 or r.workers_count > max_workers):
                raise ValidationError(
                    _("An instance can run between 1 and %s workers.") % max_workers
                )

    @api.constrains('trial', 'partner_id', 'company_id')
    def _check_trial_instance(self):
        for r in self:
            if r.trial and r.partner_id and r.company_id:
                if r.partner_id.trial_instance_count > r.company_id.limit_trial:
                    raise ValidationError(_("Partner %s has reached the maximum number of trials. Please use the paid Odoo instance") % r.partner_id.name)

    @api.constrains('name')
    def _check_subdomain(self):
        for r in self:
            if r.name:
                if not re.match(r'^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$', r.name):
                    raise ValidationError(_("Subdomain must only contain lowercase letters, numbers, and hyphens, and cannot start or end with a hyphen."))
                if r.name[0].isdigit():
                    raise ValidationError(_("Subdomain cannot start with a number."))
            existed_domain_name = self.env['saas.odoo.instance.domain.name'].search([('name', '=', r.domain_name)], limit=1)
            if existed_domain_name:
                raise UserError(_("Subdomain %s already belongs to Odoo Instance %s") % (r.name, existed_domain_name.instance_id.name))

    @api.model
    def check_subdomain_availability(self, subdomain, based_domain_id=False):
        """Checks if a subdomain is available, valid format, and not taken or reserved."""
        if not subdomain:
            return {'available': False, 'error': _("Please enter a subdomain.")}

        subdomain = str(subdomain).strip().lower()

        # Format validation
        if not re.match(r'^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$', subdomain):
            return {
                'available': False,
                'error': _("Subdomain must only contain lowercase letters, numbers, and hyphens (cannot start or end with a hyphen).")
            }

        if subdomain[0].isdigit():
            return {
                'available': False,
                'error': _("Subdomain cannot start with a number.")
            }

        if len(subdomain) < 2 or len(subdomain) > 50:
            return {
                'available': False,
                'error': _("Subdomain must be between 2 and 50 characters.")
            }

        reserved_names = {
            'www', 'mail', 'smtp', 'pop', 'imap', 'ftp', 'admin', 'administrator', 'root',
            'support', 'billing', 'api', 'dev', 'stage', 'test', 'demo', 'saas', 'odoo',
            'app', 'apps', 'portal', 'dashboard', 'login', 'web', 'auth', 'account', 'status',
            'help', 'blog', 'shop', 'cart', 'checkout', 'payment'
        }
        if subdomain in reserved_names:
            return {
                'available': False,
                'error': _("'%s' is a reserved subdomain. Please choose another one.") % subdomain
            }

        # Resolve based domain
        based_domain = False
        if based_domain_id:
            try:
                based_domain = self.env['saas.based.domain'].sudo().browse(int(based_domain_id))
            except Exception:
                pass
        if not based_domain or not based_domain.exists():
            based_domain = self.env['saas.based.domain'].sudo().search([], limit=1)

        base_domain_name = based_domain.name if based_domain else 'edc.nc'
        full_domain = f"{subdomain}.{base_domain_name}"

        # 1. Check saas.blocked.domain
        blocked = self.env['saas.blocked.domain'].sudo().search([
            ('name', '=', subdomain),
            '|',
            ('based_domain_id', '=', False),
            ('based_domain_id', '=', based_domain.id if based_domain else False)
        ], limit=1)
        if blocked:
            return {
                'available': False,
                'error': _("%s domain already taken") % subdomain
            }

        # 2. Check saas.odoo.instance (primary name or domain_name)
        domain_args = [
            '|',
            '&', ('name', '=', subdomain), ('based_domain_id', '=', based_domain.id if based_domain else False),
            ('domain_name', '=', full_domain)
        ]
        existing_inst = self.sudo().search(domain_args, limit=1)
        if existing_inst:
            return {
                'available': False,
                'error': _("%s domain already taken") % subdomain
            }

        # 3. Check saas.odoo.instance.domain.name (custom domain aliases)
        existing_alias = self.env['saas.odoo.instance.domain.name'].sudo().search([
            '|',
            ('name', '=', full_domain),
            ('name', '=', subdomain)
        ], limit=1)
        if existing_alias:
            return {
                'available': False,
                'error': _("%s domain already taken") % subdomain
            }

        return {
            'available': True,
            'message': _("Subdomain '%s' is available!") % full_domain,
            'full_domain': full_domain
        }

    def copy(self, default=None):
        self.ensure_one()
        if default is None:
            default = {}

        # Generate a unique subdomain name only if no name was provided
        original_name = self.name
        new_name = original_name + "-copy"

        counter = 1
        while self.search_count([
            ("name", "=", new_name),
            ("based_domain_id", "=", self.based_domain_id.id)
        ]) > 0:
            counter += 1
            new_name = "%s-copy%s" % (original_name, counter)

        default.setdefault("name", new_name)
        default["state"] = "draft"
        default["operation_state"] = "draft"
        default["use_template"] = True
        default["template_instance_id"] = self.id

        return super(OdooInstance, self).copy(default=default)

    @api.depends('config_ids', 'config_ids.name', 'config_ids.value')
    def _compute_db_name(self):
        for r in self:
            r.db_name = ''
            if r.config_ids:
                db_name_configs = r.config_ids.filtered(lambda c: c.name == 'db_name')
                if db_name_configs:
                    r.db_name = db_name_configs[0].value

    def _dir_size_gb(self, path):
        """Size of a host directory in GB (0.0 when it is missing or unreadable).

        No local ``os.path.isdir`` guard on purpose: the Odoo process cannot stat the
        Docker volume directory (``/var/lib/docker/volumes/...`` is root only), so the
        check is left to ``du``, which runs through sudo and reports a non-zero exit
        when the path really does not exist.
        """
        if not path:
            return 0.0
        try:
            result = subprocess.run(
                ['sudo', '/usr/bin/du', '-sb', path],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if parts and parts[0].isdigit():
                    return round(int(parts[0]) / (1024 ** 3), 2)
        except Exception as e:
            _logger.warning('Storage size failed for %s: %s', path, e)
        return 0.0

    def _storage_paths(self):
        """Host paths that count towards the instance quota.

        ``pgdata`` and the addons/backups live in the instance folder
        (``/home/<technical_name>``), but the Odoo filestore — attachments, sessions,
        anything written under ``/var/lib/odoo`` — is a *Docker volume* named
        ``<technical_name>_odoo-web-data`` living under ``/var/lib/docker/volumes``.
        That volume sits outside ``/home`` and used to be missed, so an instance could
        fill its quota with attachments while the check kept reporting almost no usage.
        """
        self.ensure_one()
        folder = '/home/%s' % self.technical_name
        return {
            'folder': folder,
            'pgdata': os.path.join(folder, 'pgdata'),
            'odoo_web_data': '/var/lib/docker/volumes/%s_odoo-web-data/_data'
                             % self.technical_name,
        }

    def _compute_storage_usage(self):
        """Used storage = instance folder (database, addons, backups) + odoo-web-data volume.

        The volume is only mounted while the container exists, so it is measured in
        addition to the folder and never double counts ``pgdata`` (which is a bind mount
        inside the folder).
        """
        for r in self:
            used_gb = odoo_gb = pg_gb = 0.0
            if r.technical_name and r.state in ('deploy', 'suspend'):
                try:
                    paths = r._storage_paths()
                    odoo_gb = r._dir_size_gb(paths['odoo_web_data'])
                    pg_gb = r._dir_size_gb(paths['pgdata'])
                    used_gb = round(r._dir_size_gb(paths['folder']) + odoo_gb, 2)
                except Exception as e:
                    _logger.warning('Storage usage compute failed for %s: %s', r.name, e)
            r.storage_odoo_data_gb = odoo_gb
            r.storage_database_gb = pg_gb
            r.storage_used_gb = used_gb
            limit = r.storage_limit_gb or 5.0
            r.storage_percentage = round((used_gb / limit) * 100, 1) if limit > 0 else 0.0

    @api.model
    def cron_check_storage_limits(self):
        """Suspend every running instance whose stored usage reached its storage limit.

        The ``storage_used_gb`` / ``storage_percentage`` values are refreshed by the storage
        monitoring cron, so this check only reads the stored percentage and stays cheap.
        """
        instances = self.search([
            ('state', '=', 'deploy'),
            ('operation_state', '=', 'run'),
            ('storage_limit_gb', '>', 0),
        ])
        today = datetime.utcnow().date()
        for instance in instances:
            try:
                if (instance.storage_percentage or 0.0) >= 100.0:
                    instance._suspend_for_storage_full()
                # One storage-usage snapshot per day so the History tab can show how much
                # storage has been used over time.
                last_snapshot = instance.history_ids.filtered(
                    lambda h: h.category == 'storage' and h.source == 'cron'
                )[:1]
                if not last_snapshot or not last_snapshot.datetime or last_snapshot.datetime.date() < today:
                    instance._log_history(
                        _("Storage usage snapshot"),
                        category='storage',
                        level='info',
                        icon='fa-hdd-o',
                        summary=_("%.2f / %.2f GB (%.1f%%)") % (
                            instance.storage_used_gb or 0.0,
                            instance.storage_limit_gb or 0.0,
                            instance.storage_percentage or 0.0,
                        ),
                        description=_("Daily storage usage: %.2f GB used out of %.2f GB.") % (
                            instance.storage_used_gb or 0.0, instance.storage_limit_gb or 0.0,
                        ),
                        source='cron',
                    )
                    # Once a day, warn the customer when the instance is running out of room.
                    # (The "limit reached" case is emailed by _suspend_for_storage_full.)
                    if 80.0 <= (instance.storage_percentage or 0.0) < 100.0:
                        instance.partner_id._saas_notify(
                            'storage_warning',
                            instance=instance,
                            title=_("Storage is running out on %s") % instance.name,
                            intro=_("Your instance has passed 80% of its storage limit. Buy extra "
                                    "storage to avoid an automatic stop when the limit is reached."),
                            rows=[('Used', '%.2f GB' % (instance.storage_used_gb or 0.0)),
                                  ('Limit', '%.2f GB' % (instance.storage_limit_gb or 0.0)),
                                  ('Usage', '%.1f%%' % (instance.storage_percentage or 0.0))],
                            cta_label=_("Buy extra storage"),
                        )
            except Exception as e:
                _logger.warning('Storage check failed for instance %s: %s', instance.name, e)

    def _suspend_for_storage_full(self):
        """Stop the instance and flag it as suspended because its storage is full.

        ``storage_full`` is the only reason that is automatically cleared (and the instance
        resumed) once additional storage is purchased. A manually suspended instance, or one
        suspended for another reason, is therefore never started by a storage purchase.
        """
        self.ensure_one()
        try:
            if self.operation_state == 'run':
                self.action_stop()
        except Exception as e:
            _logger.warning('Could not stop storage-full instance %s: %s', self.name, e)
        self.write({
            'state': 'suspend',
            'operation_state': 'stop',
            'suspension_reason': 'storage_full',
        })
        self.message_post(body=_(
            "Instance stopped: your storage limit is exceeded "
            "(%(used).2f GB / %(limit).2f GB). Buy extra storage to resume."
        ) % {'used': self.storage_used_gb or 0.0, 'limit': self.storage_limit_gb or 0.0})
        self.partner_id._saas_notify(
            'storage_full',
            instance=self,
            title=_("Storage limit exceeded — %s was stopped") % self.name,
            intro=_("Your instance was stopped because it reached its storage limit. Buy extra "
                    "storage and it starts again automatically. No data has been lost."),
            rows=[('Used', '%.2f GB' % (self.storage_used_gb or 0.0)),
                  ('Limit', '%.2f GB' % (self.storage_limit_gb or 0.0)),
                  ('Status', _("Stopped"))],
            cta_label=_("Buy extra storage"),
        )
        self._log_history(
            _("Suspended — storage full"),
            category='storage',
            level='danger',
            icon='fa-exclamation-triangle',
            summary=_("%.2f / %.2f GB") % (self.storage_used_gb or 0.0, self.storage_limit_gb or 0.0),
            description=_(
                "Instance automatically suspended: the storage limit was reached "
                "(%.2f GB / %.2f GB). Upgrade storage to resume."
            ) % (self.storage_used_gb or 0.0, self.storage_limit_gb or 0.0),
            source='cron',
        )
        _logger.info('Instance %s suspended: storage full (storage_full)', self.name)
        return True

    def _ensure_storage_not_locked(self):
        """Raise when the instance is locked because its storage is full.

        The portal must not let a customer start/restart/redeploy an instance that was
        suspended for ``storage_full`` and thereby bypass the storage restriction.
        """
        for r in self:
            if r.suspension_reason == 'storage_full':
                raise UserError(_(
                    "Your instance has been suspended because its storage limit is full. "
                    "Please upgrade your storage to continue using the instance."
                ))

    def _can_auto_resume(self):
        """Whether a paid order is allowed to automatically start this stopped instance.

        Used by the sale/payment flows so that manually suspended instances (and instances
        suspended for being full) are never started automatically.
        """
        self.ensure_one()
        if self.suspension_reason in ('manual', 'storage_full'):
            return False
        if self.storage_limit_gb and (self.storage_percentage or 0.0) >= 100.0:
            return False
        return True

    def _resume_after_storage_upgrade(self):
        """Clear a ``storage_full`` suspension and start the instance again.

        Returns ``True`` when the instance was actually (re)started. Does nothing for an
        instance suspended for any other reason.
        """
        self.ensure_one()
        if self.suspension_reason != 'storage_full':
            return False
        self.write({'suspension_reason': False, 'state': 'deploy'})
        try:
            self.action_start()
        except Exception as e:
            _logger.warning('Failed to auto-start %s after storage upgrade: %s', self.name, e)
            return False
        _logger.info('Instance %s resumed after storage upgrade', self.name)
        self.partner_id._saas_notify(
            'storage_upgrade',
            instance=self,
            title=_("Storage upgraded — %s is running again") % self.name,
            intro=_("Your extra storage is active and the instance was started again "
                    "automatically. No data was lost."),
            rows=[('New limit', '{:g} GB'.format(self.storage_limit_gb or 0.0)),
                  ('Used', '{:.2f} GB'.format(self.storage_used_gb or 0.0)),
                  ('Status', _('Running'))],
            cta_label=_("Open your instance"),
        )
        return True


    @api.depends('company_id')
    def _compute_resource_package_id(self):

        for r in self: 
            r.resource_package_id = False
            if r.company_id:
                r.resource_package_id = r.company_id.resource_package_id

    @api.depends('resource_package_id')
    def _compute_resource_package_line_ids(self):
        for r in self:
            r.resource_package_line_ids = False
            if r.resource_package_id:
                lines = []
                for line in r.resource_package_id.line_ids:
                    lines.append((0, 0, {
                        'name': line.name,
                        'value': line.value,
                        'type': line.type
                    }))
                r.resource_package_line_ids = lines

    @api.depends('name', 'based_domain_id.name')
    def _compute_url(self):
        for r in self:
            r.url = 'https://%s.%s' % (r.name, r.based_domain_id.name)

    @api.depends('name', 'based_domain_id')
    def _compute_domain_name(self):
        for r in self:
            domain_name = ''
            if r.name and r.based_domain_id:
                domain_name = r.name + '.' + r.based_domain_id.name
            r.domain_name = domain_name

    @api.depends('name', 'based_domain_id.name')
    def _compute_technical_name(self):
        for r in self:
            if not r.name or not r.based_domain_id:
                r.technical_name = ''
            else:
                # Format date as 29_04_2026
                today_date = datetime.today().strftime('%d_%m_%Y')

                # Clean strings
                name_part = (r.name or '').replace("-", "_").replace(".", "_")
                domain_part = (r.based_domain_id.name or '').replace("-", "_").replace(".", "_")

                # Final format: name_domain_date
                r.technical_name = f"{name_part}_{domain_part}_{today_date}"

    @api.depends('odoo_version_id', 'version_type')
    def _compute_odoo_server_id(self):
        """Auto-select the Odoo server matching the chosen version **and** version type.

        A server tagged ``Community``/``Enterprise`` for the same version is preferred; if
        the instance asks for an edition without a dedicated server we fall back to any
        server of that version, then to any active server.
        """
        Server = self.env['saas.odoo.server']
        for r in self:
            server = Server.browse()
            if r.odoo_version_id:
                by_version = [('odoo_version_id', '=', r.odoo_version_id.id)]
                if r.version_type:
                    by_type = by_version + [('version_type', '=', r.version_type)]
                    server = (
                        Server.search(by_type + [('active', '=', True)], limit=1, order='sequence, id')
                        or Server.search(by_type, limit=1, order='sequence, id')
                    )
                if not server:
                    server = (
                        Server.search(by_version + [('active', '=', True)], limit=1, order='sequence, id')
                        or Server.search(by_version, limit=1, order='sequence, id')
                    )
                r.odoo_server_id = server or False
            elif not r.odoo_server_id:
                # Deterministic pick: all servers usually share the same sequence, so
                # ordering by id keeps the fallback stable instead of returning a random
                # version/edition on every call.
                r.odoo_server_id = Server.search([('active', '=', True)], limit=1, order='sequence, id') or False

    @api.onchange('odoo_server_id')
    def _onchange_odoo_server_id(self):
        """Keep version + type aligned when a server is picked manually."""
        for r in self:
            if r.odoo_server_id:
                r.odoo_version_id = r.odoo_server_id.odoo_version_id
                r.version_type = r.odoo_server_id.version_type

    @api.depends('odoo_version_id')
    def _compute_template_instance_domain_ids(self):
        for r in self:
            available_template_instances = r._get__available_template_instance()
            r.template_instance_domain_ids = [(6, 0, available_template_instances.ids)]

    def _get__available_template_instance(self):
        """
        Hook method to 's_odoo_saas_plan' module can be extended
        """
        if self.odoo_version_id:
            return self.search([('is_template', '=', True), ('state', '=', 'deploy'), ('odoo_version_id', '=', self.odoo_version_id.id)])
        else:
            return self.env['saas.odoo.instance']

    @api.depends('template_instance_id', 'name')
    def _compute_custom_addon_ids(self):
        for r in self:
            r.custom_addon_ids = False
            if r.name and r.template_instance_id:
                custom_addons = []
                for addons in r.template_instance_id.custom_addon_ids:
                    custom_addons.append((0, 0, {
                        'name': addons.name,
                        'clone_uri': addons.clone_uri,
                        'branch': addons.branch,
                    }))
                r.custom_addon_ids = custom_addons

    @api.depends('custom_addon_ids', 'custom_addon_ids.cloned', 'custom_addon_ids.clone_uri', 'github_repo_url')
    def _compute_github_connected(self):
        for r in self:
            r.github_connected = bool(r.github_repo_url or r.custom_addon_ids.filtered(lambda a: a.cloned))

    @api.depends('odoo_server_id', 'docker_image_id',
        'port_ids', 'port_ids.name', 'port_ids.port',
        'config_ids', 'config_ids.name', 'config_ids.value')
    def _compute_docker_compose(self):
        for r in self:
            r.docker_container_ids = False
            r.docker_compose_volume_ids = False
            r.docker_odoo_image = ''
            r.docker_psql_image = ''
            r.docker_xmlrpc_expose_port = ''
            r.docker_xmlrpcs_expose_port = ''
            r.docker_longpolling_expose_port = ''
            r.docker_xmlrpc_container_port = ''
            r.docker_xmlrpcs_container_port = ''
            r.docker_longpolling_container_port = ''
            if r.odoo_server_id:
                if r.docker_image_id:
                    r.docker_odoo_image = r.docker_image_id.image_name
                else:
                    r.docker_odoo_image = 'odoo:%s' % r.odoo_server_id.odoo_version_id.docker_image_tag
                # Version matched on purpose: the image carries pgvector for the very
                # PostgreSQL version this instance runs, whatever was selected.
                psql_version = r.odoo_server_id.psql_version_id
                r.docker_psql_image = psql_version._get_postgres_image() if psql_version else ''
                r.docker_container_ids = r._prepare_docker_containers()
            for port in r.port_ids:
                if port.name == 'xmlrpc_port':
                    r.docker_xmlrpc_expose_port = port.port
                if port.name == 'xmlrpcs_port':
                    r.docker_xmlrpcs_expose_port = port.port
                if port.name == 'longpolling_port':
                    r.docker_longpolling_expose_port = port.port
            for config in r.config_ids:
                if config.name == 'xmlrpc_port':
                    r.docker_xmlrpc_container_port = config.value
                if config.name == 'xmlrpcs_port':
                    r.docker_xmlrpcs_container_port = config.value
                if config.name == 'longpolling_port':
                    r.docker_longpolling_container_port = config.value

    @api.depends('docker_container_ids')
    def _compute_docker_container_count(self):
        container_data = self.env['saas.odoo.instance.docker.container']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {p.id: count for p, count in container_data}
        for r in self:
            r.docker_container_count = result.get(r.id, 0)

    @api.depends('docker_compose_volume_ids')
    def _compute_docker_compose_volume_count(self):
        volume_data = self.env['saas.odoo.instance.docker.compose.volume']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {p.id: count for p, count in volume_data}
        for r in self:
            r.docker_compose_volume_count = result.get(r.id, 0)

    @api.depends('domain_name_ids')
    def _compute_domain_name_count(self):
        domain_name_data = self.env['saas.odoo.instance.domain.name']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {d.id: count for d, count in domain_name_data}
        for r in self:
            r.domain_name_count = result.get(r.id, 0)

    @api.depends('backup_ids')
    def _compute_backup_count(self):
        backup_data = self.env['saas.odoo.instance.backup']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {d.id: count for d, count in backup_data}
        for r in self:
            r.backup_count = result.get(r.id, 0)

    @api.depends('history_ids')
    def _compute_history_count(self):
        history_data = self.env['saas.odoo.instance.history']._read_group(
            [('instance_id', 'in', self.ids)], ['instance_id'], ['__count']
        )
        result = {d.id: count for d, count in history_data}
        for r in self:
            r.history_count = result.get(r.id, 0)

    def _log_history(self, action, category='system', description=None, summary=None,
                     level='info', icon=None, source=None):
        """Record one line in this instance's activity history.

        ``source`` defaults to 'backend' when the change comes from the Odoo backend and
        'portal' otherwise, so the History tab can show where a change came from.
        """
        if source is None:
            source = 'backend' if self.env.context.get('from_backend') else 'portal'
        for record in self:
            self.env['saas.odoo.instance.history']._log(
                record, action, category=category, description=description,
                summary=summary, level=level, icon=icon, source=source,
            )
        return True

    @api.depends('installed_app_ids')
    def _compute_installed_app_count(self):
        app_data = self.env['saas.odoo.instance.installed.app']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {d.id: count for d, count in app_data}
        for r in self:
            r.installed_app_count = result.get(r.id, 0)

    @api.depends('trial')
    def _compute_expiration_date(self):
        for r in self:
            if not r.trial:
                r.expiration_date = r.expiration_date
            else:
                r.expiration_date = fields.Date.today() + timedelta(days=self.env.user.company_id.instance_trial_day)

    @api.depends('sale_order_ids')
    def _compute_sale_order_count(self):
        order_data = self.env['sale.order']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {d.id: count for d, count in order_data}
        for r in self:
            r.sale_order_count = result.get(r.id, 0)

    @api.depends(
        'sale_order_ids', 'sale_order_ids.state', 'sale_order_ids.order_line',
        'sale_order_ids.order_line.product_id', 'sale_order_ids.order_line.product_uom_qty')
    def _compute_paid_user(self):
        for r in self:
            order_lines = r.sale_order_ids.order_line.filtered(lambda line: line.order_id.state == 'sale' and line.product_id.is_saas_user)
            r.paid_user = sum(order_lines.mapped('product_uom_qty'))

    @api.depends('installed_app_ids')
    def _compute_not_paid_app_count(self):
        not_paid_app_data = self.env['saas.odoo.instance.installed.app']._read_group([
            ('instance_id', 'in', self.ids), ('not_paid', '=', True)], ['instance_id'], ['__count'])
        result = {d.id: count for d, count in not_paid_app_data}
        for r in self:
            r.not_paid_app_count = result.get(r.id, 0)

    @api.depends('account_move_ids')
    def _compute_account_move_count(self):
        move_data = self.env['account.move']._read_group([('instance_id', 'in', self.ids)], ['instance_id'], ['__count'])
        result = {d.id: count for d, count in move_data}
        for r in self:
            r.account_move_count = result.get(r.id, 0)

    @api.depends('not_paid_app_count', 'active_user', 'paid_user')
    def _compute_has_extra(self):
        for r in self:
            r.has_extra = False
            if r.partner_id and r.expiration_date:
                if r.paid_user and r.active_user > r.paid_user:
                    r.has_extra = True
                    continue
                if r.not_paid_app_count:
                    r.has_extra = True
                    continue

    def _compute_access_url(self):
        super(OdooInstance, self)._compute_access_url()
        for r in self:
            r.access_url = '/my/saas/odoo-instance/%s' % (r.id)

    @api.ondelete(at_uninstall=False)
    def unlink_exception(self):
        for r in self:
            if r.state not in ('draft', 'cancel'):
                raise UserError(_('You cannot delete this record which is not draft or cancelled.'))

    def action_view_odoo_instance_config(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_config_action')
        action['context'] = {'default_instance_id': self.id}
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_odoo_instance_container(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_docker_container_action')
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_odoo_instance_volume(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_docker_compose_volume_action')
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_odoo_instance_domain_name(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_domain_name_action')
        action['context'] = {'default_instance_id': self.id}
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_odoo_instance_backup(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_backup_action')
        action['context'] = {'default_instance_id': self.id}
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_installed_app(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_installed_app_action')
        action['context'] = {'default_instance_id': self.id}
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_sale_order(self):
        action = self.env['ir.actions.act_window']._for_xml_id('sale.action_quotations_with_onboarding')
        action['context'] = {
            'default_instance_id': self.id,
            'default_partner_id': self.partner_id.id or False,
        }
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_view_account_move(self):
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_move_out_invoice_type')
        action['context'] = {
            'default_instance_id': self.id,
            'default_partner_id': self.partner_id.id or False,
            'default_move_type': 'out_invoice'}
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    def action_open_redeploy_wizard(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_redeploy_wizard_action')
        action['context'] = {'default_instance_id': self.id}
        return action

    def action_open_upgrade_module_wizard(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_upgrade_module_wizard_action')
        action['context'] = {'default_instance_id': self.id}
        return action

    def action_duplicate_wizard(self):
        'Open the duplicate wizard to let the user choose a new subdomain.'
        original_name = self.name
        new_name = original_name + "-copy"

        counter = 1
        while self.search_count([
            ("name", "=", new_name),
            ("based_domain_id", "=", self.based_domain_id.id)
        ]) > 0:
            counter += 1
            new_name = "%s-copy%s" % (original_name, counter)

        action = self.env["ir.actions.act_window"]._for_xml_id(
            "s_odoo_saas_master.saas_odoo_instance_duplicate_wizard_action"
        )
        action["context"] = {
            "default_instance_id": self.id,
            "default_new_subdomain": new_name,
        }
        return action

    def action_deploy(self):
        for r in self:
            if r.use_template and r.template_instance_id and r.template_instance_id.state != 'deploy':
                raise ValidationError(_("Template instance %s has not deployed yet. Please deploy it first."))             
            # Fail fast (with an actionable message) when the Docker images the instance
            # needs do not exist on the target server: `docker compose up` would fail and
            # the deployment used to report the misleading "containers are not running".
            r.pserver_id._check_docker_images_available(r)
            r._generate_instance_port()
            r._generate_instance_config()
            r._generate_instance_extra_addons()
            r._generate_instance_domain_name()
            # ===== CORRECTION : Invalidation du cache Odoo après génération =====
            r.invalidate_recordset(fnames=[
                'port_ids', 'config_ids',
                'docker_container_ids', 'docker_compose_volume_ids',
                'docker_odoo_image', 'docker_psql_image',
                'docker_xmlrpc_expose_port', 'docker_xmlrpcs_expose_port',
                'docker_longpolling_expose_port',
            ])
            if r.use_template and r.template_instance_id:
                r.pserver_id._deploy_odoo_instance_from_template(r)
            else:
                r.pserver_id._deploy_odoo_instance(r)
            if r.partner_id and r.partner_id.email:
                if r.use_template and r.template_instance_id and r.template_instance_id.deploy_mail_template_id:
                    r.template_instance_id.deploy_mail_template_id.sudo().send_mail(r.id, force_send=True)
                else:
                    template_id = self.env.ref('s_odoo_saas_master.deploy_instance_mail_template', raise_if_not_found=False)
                    if template_id:
                        template_id.sudo().send_mail(r.id, force_send=True)

        # Phase 0.4 : vérifier que les containers sont réellement up avant de passer en deploy/run
        for r in self:
            statuses = r.pserver_id._get_container_status(r.docker_container_ids)
            if not statuses:
                raise UserError(
                    _("Instance %s: no docker container status returned. "
                      "Deployment aborted, instance stays in draft.")
                    % r.display_name
                )
            not_running = [name for name, st in statuses.items() if st != 'running']
            if not_running:
                raise UserError(
                    _("Instance %s: containers are not running (%s). Deployment aborted, "
                      "instance stays in draft.")
                    % (r.display_name, ', '.join(not_running))
                )

        self.write({'state': 'deploy', 'operation_state': 'run', 'buy_now_from_pricing': False})
        self.domain_name_ids.write({'state': 'deploy'})
        self.custom_addon_ids.write({'cloned': True})
        for r in self:
            if r.partner_id:
                r.partner_id._saas_notify(
                    'instance_created',
                    instance=r,
                    title=_("Your instance %s is ready") % r.name,
                    intro=_("Your hosting instance finished deploying and is online now."),
                    rows=[('Workers', str(r.workers_count or 1)),
                          ('Storage', '{:g} GB'.format(r.storage_limit_gb or 5)),
                          ('Odoo version', r.odoo_version_id.name or ''),
                          ('Subdomain', r.name or '')],
                    cta_label=_("Open your instance"),
                )
            if r.template_instance_id:
                r.action_get_active_users()
                r.action_get_installed_apps()

    def _claim_deployment(self):
        """Atomically move an instance to 'deploying'. Returns True if we won the race.

        Two deployment sources (the post-commit thread and the fallback cron) could try
        to deploy the same instance at once; the conditional UPDATE guarantees only one
        of them proceeds.

        Losing the race is a normal outcome, so the database errors PostgreSQL raises for
        a concurrent write on the same row (``could not serialize access due to concurrent
        update``, deadlock, row lock timeout) are caught here and reported as "not claimed".
        Without this the fallback cron logged a full traceback and an ``ERROR`` line every
        time the background deployment thread touched the row first.
        """
        self.ensure_one()
        try:
            # The savepoint keeps the enclosing transaction usable when PostgreSQL aborts
            # the statement: without it the whole cron transaction would be broken.
            with self.env.cr.savepoint():
                self.env.cr.execute("""
                    UPDATE saas_odoo_instance
                       SET deployment_state = 'deploying',
                           deployment_started_at = (now() at time zone 'UTC'),
                           deployment_error = NULL
                     WHERE id = %s
                       AND deployment_state IN ('idle', 'pending')
                """, (self.id,))
                claimed = self.env.cr.rowcount == 1
        except (psycopg2.errors.SerializationFailure,
                psycopg2.errors.DeadlockDetected,
                psycopg2.errors.LockNotAvailable):
            _logger.info(
                "Deployment claim for instance %s lost to a concurrent transaction, "
                "leaving it to the other deployment.", self.name,
            )
            return False
        if claimed:
            self.invalidate_recordset(['deployment_state', 'deployment_started_at', 'deployment_error'])
        return claimed

    def _deploy_now(self):
        """Deploy this instance (blocking) and record the deployment progress."""
        self.ensure_one()
        if self.state == 'deploy' and self.operation_state == 'run':
            self.write({'deployment_state': 'deployed', 'deployment_error': False})
            return True
        if not self._claim_deployment():
            # Someone else is already deploying (or it is already done).
            _logger.info("Deployment for instance %s already in progress, skipping", self.name)
            return False
        try:
            self.action_deploy()
            self.write({'deployment_state': 'deployed', 'deployment_error': False})
            _logger.info("Instance %s deployed after purchase", self.name)
            return True
        except Exception as e:
            self.env.cr.rollback()
            _logger.exception("Automatic deployment failed for instance %s", self.name)
            self.write({
                'deployment_state': 'failed',
                'deployment_error': (str(e) or repr(e))[:2000],
            })
            return False

    def _schedule_deployment(self):
        """Queue the (long) deployment so it starts right after this transaction commits.

        The work runs in a background thread with its own cursor, which keeps the HTTP
        response instant while the portal shows the deployment preloader. The
        ``cron_deploy_pending_instances`` cron is the safety net.
        """
        self.ensure_one()
        if self.state == 'deploy' and self.operation_state == 'run':
            if self.deployment_state != 'deployed':
                self.write({'deployment_state': 'deployed'})
            return False
        if self.deployment_state in ('deploying', 'pending'):
            return False

        self.write({'deployment_state': 'pending', 'deployment_error': False})
        instance_id = self.id
        registry = self.env.registry

        def _launch():
            try:
                import threading
                threading.Thread(
                    target=self._run_deployment_job,
                    args=(registry, instance_id),
                    daemon=True,
                    name='saas-deploy-%s' % instance_id,
                ).start()
            except Exception:
                _logger.exception("Could not start deployment thread for instance %s", instance_id)

        self.env.cr.postcommit.add(_launch)
        return True

    @api.model
    def _run_deployment_job(self, registry, instance_id):
        try:
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env['saas.odoo.instance'].browse(instance_id)._deploy_now()
                cr.commit()
        except Exception:
            _logger.exception("Background deployment crashed for instance %s", instance_id)

    @api.model
    def cron_deploy_pending_instances(self):
        """Fallback deployment: pick up instances queued after a purchase and
        recover instances left hanging in ``deploying`` (e.g. worker restart)."""
        # Safety net: a paid "buy new" instance that was never queued.
        for instance in self.search([('state', '=', 'draft'), ('deployment_state', '=', 'idle')]):
            paid = self.env['sale.order'].sudo().search([
                ('instance_id', '=', instance.id),
                ('is_saas_order', '=', True),
                ('saas_order_type', '=', 'buy_new'),
                ('state', 'in', ('sale', 'done')),
                ('transaction_ids.state', '=', 'done'),
            ], limit=1)
            if paid:
                instance.write({'deployment_state': 'pending'})
        self.env.cr.commit()

        for instance in self.search([('deployment_state', '=', 'pending')]):
            try:
                instance._deploy_now()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Cron deployment failed for instance %s", instance.name)

        stale_limit = fields.Datetime.subtract(fields.Datetime.now(), minutes=30)
        stuck = self.search([('deployment_state', '=', 'deploying')])
        for instance in stuck:
            if not instance.deployment_started_at or instance.deployment_started_at < stale_limit:
                instance.write({'deployment_state': 'pending'})
        self.env.cr.commit()

    def _action_cancel(self):
        for r in self:
            r._free_instance_port()
            r.config_ids.unlink()
            r.pserver_id._revoke_odoo_instance(r)
            if r.partner_id and r.partner_id.email:
                template_id = self.env.ref('s_odoo_saas_master.cancel_instance_mail_template', raise_if_not_found=False)
                if template_id:
                    template_id.sudo().send_mail(r.id, force_send=True)

        self.write({'state': 'cancel', 'operation_state': 'draft'})
        self.domain_name_ids.write({'state': 'cancel'})
        self.custom_addon_ids.write({'cloned': False})
    
    def action_cancel(self):
        action = self.env['ir.actions.act_window']._for_xml_id('s_odoo_saas_master.saas_odoo_instance_cancel_wizard_action')
        action['context'] = {'default_instance_id': self.id}
        return action

    def action_draft(self):
        self.write({'state': 'draft', 'operation_state': 'draft'})
        self.domain_name_ids.write({'state': 'draft'})

    @api.model
    def _resolve_backup_dir(self, subdir='', company=None):
        """Return a directory the Odoo *host* process can actually write to.

        The directory is created by the Odoo process itself, so it is owned by the same
        user that writes the backups — no manual ``chown``/``chmod`` is ever needed.

        The company ``backup_directory`` is honoured when it is usable. Otherwise we fall
        back to a folder inside the Odoo data directory, which is always writable. This
        fixes the historical
        ``Permission denied: Cannot create directory '/var/lib/odoo/backups'`` error —
        ``/var/lib/odoo`` only exists *inside* the containers, never on the master host.
        """
        company = company or self.env.company
        configured = ((company.backup_directory or '') if company else '').strip()
        data_dir = odoo_config['data_dir'] or '/var/lib/odoo'
        candidates = [configured] if configured else []
        candidates.append(os.path.join(data_dir, 'instance-backups'))
        tried = []
        for candidate in candidates:
            path = os.path.join(candidate, subdir) if subdir else candidate
            tried.append(path)
            try:
                os.makedirs(path, mode=0o755, exist_ok=True)
            except OSError as error:
                _logger.warning("Backup directory %s is not usable: %s", path, error)
                continue
            if not os.access(path, os.W_OK):
                # The folder may have been created earlier by another user (e.g. an upgrade
                # wrongly run as root). Try to open it up before falling back.
                try:
                    os.chmod(path, 0o777)
                except OSError as error:
                    _logger.warning(
                        "Backup directory %s is not writable and could not be fixed: %s", path, error
                    )
                    continue
                if not os.access(path, os.W_OK):
                    _logger.warning("Backup directory %s is still not writable after chmod", path)
                    continue
            return path
        raise UserError(_(
            "No writable backup directory found. Please set a writable 'Backup Directory' in the "
            "company settings. Tried: %s"
        ) % ', '.join(tried))

    def _get_writable_backup_dir(self, subdir=''):
        """Writable backup directory for *this* instance (uses its own company)."""
        return self._resolve_backup_dir(subdir, company=self.company_id)

    @api.model
    def _ensure_backup_directories(self, companies=None):
        """Create the backup folders on the host so they exist right after install.

        Called from the module's ``post_init_hook`` and from the 18.0.0.x migration, so a
        fresh install (or a fresh server) already has ``instance-backups/`` and
        ``instance-backups/container/`` ready. Never raises: a failure is logged and the
        runtime fallback in :meth:`_resolve_backup_dir` takes over.
        """
        companies = companies or self.env['res.company'].search([])
        created = []
        for company in companies:
            for subdir in ('', 'container'):
                try:
                    created.append(self._resolve_backup_dir(subdir, company=company))
                except UserError as error:
                    _logger.warning(
                        "Could not create the backup directory for company %s: %s",
                        company.name, error,
                    )
        if created:
            _logger.info("Instance backup directories ready: %s", sorted(set(created)))
        return list(sorted(set(created)))

    def _enforce_backup_retention(self):
        """Keep only the newest ``backup_limit`` backups (files are deleted on unlink)."""
        for r in self:
            limit = r.backup_limit or 0
            if not limit:
                continue
            stale = r.backup_ids.sorted('datetime', reverse=True)[limit:]
            if stale:
                _logger.info(
                    "Backup retention: removing %s old backup(s) of %s", len(stale), r.name
                )
                stale.unlink()
            stale_container = r.container_backup_ids.sorted('datetime', reverse=True)[limit:]
            if stale_container:
                stale_container.unlink()

    def action_backup(self, backup_type='manual', progress_cb=None):
        """Create a backup and (optionally) report progress through ``progress_cb``.

        ``progress_cb(percent, message)`` is used by the background worker so the portal
        can display a live progress bar. Called synchronously it just builds the backup.
        """
        def report(percent, message):
            if progress_cb:
                progress_cb(percent, message)

        for r in self:
            report(6, _("Preparing the backup directory…"))
            dbname = r.technical_name.strip()
            user_root = self.env.ref('base.user_root')
            ts = fields.Datetime.context_timestamp(
                self.with_context(tz=user_root.tz), datetime.utcnow()
            )
            filename = "%s_%s.%s" % (dbname, ts.strftime("%Y-%m-%d_%H-%M-%S"), 'zip')

            backup_dir = r._get_writable_backup_dir()
            filepath = os.path.join(backup_dir, filename)
            backup_vals = {
                'name': filename,
                'datetime': fields.Datetime.to_string(ts),
                'format': 'zip',
                'file_path': filepath,
                'instance_id': r.id,
                'odoo_version_id': r.odoo_version_id.id,
                'backup_type': backup_type,
            }
            try:
                report(20, _("Dumping the database & copying the filestore…"))
                filestore_file_count = r.pserver_id._create_odoo_instance_zip_backup(r, filepath)
                report(55, _("Packaging database + filestore…"))
                filesize = os.path.getsize(filepath) / 1e+6  # File size in byte, so we convert to megabyte
                backup_vals['file_size'] = filesize
                if not filestore_file_count:
                    backup_vals['description'] = _(
                        "Warning: no filestore files were found on the server when this backup was taken "
                        "(0 attachments). Images, documents and the company logo will not be restorable from this backup."
                    )
                database_backup = self.env['saas.odoo.instance.backup'].sudo().create(backup_vals)
                # pylint: disable=invalid-commit
                self.env.cr.commit()
                report(75, _("Archiving the container data…"))
                container_backup = r.action_container_backup()
                database_backup.sudo().container_backup_id = container_backup[:1]
                report(92, _("Applying the retention policy…"))
                r._enforce_backup_retention()
                r._log_history(
                    _("Backup created"),
                    category='backup',
                    summary="%s · %.2f MB" % (filename, filesize),
                    description=_(
                        "%(kind)s backup %(file)s created (%(size).2f MB). "
                        "Retention keeps the latest %(limit)s backups."
                    ) % {
                        'kind': _('Automatic') if backup_type == 'auto' else _('Manual'),
                        'file': filename,
                        'size': filesize,
                        'limit': r.backup_limit or 0,
                    },
                    level='success',
                    icon='fa-database',
                    source='cron' if backup_type == 'auto' else None,
                )
                report(100, _("Backup completed."))
            except UserError:
                raise
            except PermissionError:
                raise UserError(
                    _("Cannot write backup file to '%s'. Ensure the Odoo process can write to this directory.")
                    % backup_dir
                )
            except Exception as e:
                error = str(e) or repr(e)
                raise UserError(_("Database backup error: %s") % error)

    def _publish_backup_progress(self, percent=0, message=None, state=None, commit=True):
        """Write the backup progress straight to the database.

        Called from the background worker with its own cursor, so the portal sees the
        progress live even though the worker's transaction is not committed yet.
        """
        vals = {}
        if percent is not None:
            vals['backup_progress'] = max(0, min(int(percent), 100))
        if message is not None:
            vals['backup_message'] = message
        if state is not None:
            vals['backup_state'] = state
        if not vals:
            return
        self.sudo().write(vals)
        if commit:
            self.env.cr.commit()

    @classmethod
    def _run_backup_job(cls, dbname, instance_id, backup_type, uid):
        """Background worker: run the backup in its own cursor/thread.

        The job keeps running even if the customer refreshes or closes the page, and the
        progress is written to the database so the portal can pick it up again on reload.
        """
        registry = Registry(dbname)
        with registry.cursor() as cr:
            env = api.Environment(cr, uid, {})
            instance = env['saas.odoo.instance'].sudo().browse(instance_id)
            try:
                def progress(percent, message):
                    instance._publish_backup_progress(percent, message)

                instance.action_backup(backup_type=backup_type, progress_cb=progress)
                instance.sudo().write({
                    'backup_state': 'done',
                    'backup_progress': 100,
                    'backup_message': _("Backup completed."),
                    'backup_finished_at': fields.Datetime.now(),
                })
                instance.partner_id._saas_notify(
                    'backup_created',
                    instance=instance,
                    title=_("Backup completed for %s") % instance.name,
                    intro=_("A new backup was stored. You can download it any time from the "
                            "Backups tab of your instance."),
                    rows=[('Created', fields.Datetime.now().strftime('%d %b %Y, %H:%M')),
                          ('Type', _('Automatic') if backup_type == 'auto' else _('Manual')),
                          ('Copies kept', str(instance.backup_limit or 0))],
                    cta_label=_("Open the Backups tab"),
                )
                cr.commit()
            except Exception as error:
                message = str(error) or repr(error)
                _logger.exception("Background backup failed for instance %s", instance_id)
                try:
                    cr.rollback()
                    instance.sudo().write({
                        'backup_state': 'failed',
                        'backup_progress': 100,
                        'backup_message': message[:250],
                        'backup_finished_at': fields.Datetime.now(),
                    })
                    instance.partner_id._saas_notify(
                        'backup_failed',
                        instance=instance,
                        title=_("Backup failed for %s") % instance.name,
                        intro=_("A backup could not be completed. Your data is untouched, but "
                                "please retry the backup and contact support if it keeps failing."),
                        rows=[('Reason', message[:200])],
                        cta_label=_("Open the Backups tab"),
                    )
                    instance._log_history(
                        _("Backup failed"),
                        category='backup',
                        level='danger',
                        icon='fa-exclamation-triangle',
                        summary=message[:140],
                        description=message,
                        source='cron' if backup_type == 'auto' else None,
                    )
                    cr.commit()
                except Exception:
                    _logger.exception("Could not record the backup failure for %s", instance_id)

    def action_backup_async(self, backup_type='manual'):
        """Start a backup in the background and return immediately.

        The portal then polls :meth:`get_backup_status` to display a progress bar that
        survives page refreshes.
        """
        self.ensure_one()
        if self.backup_state == 'running':
            return {
                'success': False,
                'error': _("A backup is already running for this instance."),
                'state': 'running',
                'progress': self.backup_progress,
            }
        self.write({
            'backup_state': 'running',
            'backup_progress': 3,
            'backup_message': _("Starting the backup…"),
            'backup_running_type': backup_type,
            'backup_started_at': fields.Datetime.now(),
            'backup_finished_at': False,
        })
        # pylint: disable=invalid-commit
        self.env.cr.commit()

        dbname = self.env.cr.dbname
        uid = self.env.uid
        instance_id = self.id
        threading.Thread(
            target=OdooInstance._run_backup_job,
            args=(dbname, instance_id, backup_type, uid),
            daemon=True,
        ).start()
        return {
            'success': True,
            'state': 'running',
            'progress': 3,
            'message': self.backup_message,
        }

    def get_backup_status(self):
        """Return the live backup state for the portal progress bar."""
        self.ensure_one()
        return {
            'success': True,
            'state': self.backup_state,
            'progress': self.backup_progress or 0,
            'message': self.backup_message or '',
            'running_type': self.backup_running_type or False,
            'started_at': fields.Datetime.to_string(self.backup_started_at) if self.backup_started_at else False,
            'finished_at': fields.Datetime.to_string(self.backup_finished_at) if self.backup_finished_at else False,
            'backup_count': self.backup_count,
            'limit': self.backup_limit or 0,
        }

    def action_container_backup(self):
        """Archive the complete remote /home/<instance> directory."""
        backup_dir = self._get_writable_backup_dir('container')

        user_root = self.env.ref('base.user_root')
        container_backups = self.env['saas.odoo.instance.container.backup']
        for instance in self:
            ts = fields.Datetime.context_timestamp(
                instance.with_context(tz=user_root.tz), datetime.utcnow()
            )
            filename = '%s_container_%s.zip' % (
                instance.technical_name.strip(), ts.strftime('%Y-%m-%d_%H-%M-%S')
            )
            file_path = os.path.join(backup_dir, filename)
            try:
                instance.pserver_id._create_odoo_instance_container_backup(instance, file_path)
            except Exception:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                raise
            container_backups |= self.env['saas.odoo.instance.container.backup'].sudo().create({
                'instance_id': instance.id,
                'name': filename,
                'datetime': fields.Datetime.to_string(ts),
                'file_path': file_path,
                'file_size': os.path.getsize(file_path) / 1e6,
            })
            # Keep the record visible if another instance fails later in the cron.
            self.env.cr.commit()
        return container_backups

    def action_restart(self):
        self._ensure_storage_not_locked()
        self.docker_container_ids.action_restart()
        self._log_history(
            _("Instance restarted"), category='lifecycle', level='info', icon='fa-refresh',
            summary=_("%s worker(s)") % (self.workers_count or 1),
            description=_("The instance containers were restarted with %s worker(s).") % (self.workers_count or 1),
        )

    def action_stop(self):
        self.docker_container_ids.action_stop()
        self.write({'operation_state': 'stop'})
        self._log_history(
            _("Instance stopped"), category='lifecycle', level='warning', icon='fa-stop-circle',
            description=_("The instance was stopped from the portal."),
        )

    def action_start(self):
        self._ensure_storage_not_locked()
        self.docker_container_ids.action_start()
        vals = {'state': 'deploy', 'operation_state': 'run'}
        # A deliberate start clears non-storage suspension reasons so the portal no longer
        # shows a stale banner. ``storage_full`` stays locked until the storage is upgraded.
        if any(r.suspension_reason and r.suspension_reason != 'storage_full' for r in self):
            vals['suspension_reason'] = False
        self.write(vals)
        self._log_history(
            _("Instance started"), category='lifecycle', level='success', icon='fa-play-circle',
            summary=_("%s worker(s)") % (self.workers_count or 1),
            description=_("The instance was started with %s worker(s).") % (self.workers_count or 1),
        )

    def action_suspend(self, has_extra=False):
        self.action_stop()
        self.write({
            'state': 'suspend',
            'operation_state': 'stop',
            'suspension_reason': 'manual',
        })
        self._log_history(
            _("Instance suspended"), category='lifecycle', level='warning', icon='fa-pause-circle',
            description=_("The instance was suspended and its containers were stopped."),
        )
        for r in self:
            if r.partner_id and r.partner_id.email:
                if not has_extra:
                    template_id = self.env.ref('s_odoo_saas_master.suspend_instance_mail_template', raise_if_not_found=False)
                else:
                    template_id = self.env.ref('s_odoo_saas_master.suspend_instance_with_extra_mail_template', raise_if_not_found=False)
                if template_id:
                    template_id.sudo().send_mail(r.id, force_send=True)

    def action_connect_github(self, repo_url=None, branch='main', token=None, repo_name=None):
        self.ensure_one()
        vals = {}
        if repo_url:
            vals['github_repo_url'] = repo_url.strip()
        if branch:
            vals['github_branch'] = branch.strip()
        if token is not None:
            vals['github_token'] = token.strip() if token else False
        if repo_name:
            vals['github_repo_name'] = repo_name.strip()
        elif repo_url:
            clean_part = repo_url.strip().replace('.git', '').rstrip('/')
            parts = clean_part.split('/')
            if len(parts) >= 2:
                vals['github_repo_name'] = f"{parts[-2]}/{parts[-1]}"
        if vals:
            self.write(vals)

        if not self.github_repo_url:
            raise UserError(_("Please provide a valid GitHub repository URL."))

        clean_url = self.github_repo_url.strip()
        branch_name = (self.github_branch or 'main').strip()

        # Build authenticated clone URI if token exists
        if self.github_token:
            clean_token = self.github_token.strip()
            if clean_url.startswith('https://'):
                repo_part = clean_url[len('https://'):]
                if '@' in repo_part:
                    repo_part = repo_part.split('@', 1)[1]
                clone_uri = f"https://{clean_token}@{repo_part}"
            else:
                clone_uri = clean_url
        else:
            clone_uri = clean_url

        if not clone_uri.endswith('.git') and not clone_uri.endswith('/'):
            clone_uri += '.git'

        addon_name = 'custom_repo'
        custom_addon = self.custom_addon_ids.filtered(lambda a: a.name == addon_name)
        previous_branch = custom_addon.branch if custom_addon else False
        if not custom_addon:
            custom_addon = self.env['saas.odoo.instance.custom.addon'].create({
                'instance_id': self.id,
                'name': addon_name,
                'clone_uri': clone_uri,
                'branch': branch_name,
            })
        else:
            url_changed = (custom_addon.clone_uri != clone_uri)
            custom_addon.write({
                'clone_uri': clone_uri,
                'branch': branch_name,
            })
            # Force a fresh clone/pull when the repository URL or the selected branch
            # changed, so switching the branch in the portal really checks it out.
            if custom_addon.cloned and (url_changed or branch_name != (previous_branch or '')):
                custom_addon.write({'cloned': False})

        # Sync the addons while the instance is deployed OR suspended (its containers
        # may still be running), so "Sync & Deploy" does not silently do nothing.
        if self.state in ('deploy', 'suspend'):
            if not custom_addon.cloned:
                custom_addon.action_clone()
            else:
                custom_addon.action_pull()

        self.last_redeploy_date = fields.Datetime.now()
        self.last_sync_message = _("Synced just now")
        self.message_post(body=_("Connected GitHub repository: %s (branch: %s)") % (self.github_repo_url, branch_name))
        # History: the connection plus the exact commits that were pulled, line by line.
        self._record_github_sync_history(branch_name=branch_name)
        return True

    def action_disconnect_github(self):
        self.ensure_one()
        addon_name = 'custom_repo'
        custom_addons = self.custom_addon_ids.filtered(
            lambda a: a.name == addon_name or (a.clone_uri and 'github.com' in a.clone_uri)
        )
        for custom_addon in custom_addons:
            if custom_addon.cloned and self.state in ('deploy', 'suspend'):
                try:
                    custom_addon.action_remove()
                except Exception as e:
                    if _is_db_concurrency_error(e):
                        raise
                    _logger.warning("Error during custom addon removal: %s", e)
            try:
                custom_addon.write({'cloned': False})
                custom_addon.unlink()
            except Exception as e:
                if _is_db_concurrency_error(e):
                    raise
                _logger.warning("Error unlinking custom addon: %s", e)
        self.write({
            'github_token': False,
            'github_repo_url': False,
            'github_repo_name': False,
            'github_branch': 'main',
            'last_sync_message': False,
        })
        self.message_post(body=_("Disconnected GitHub repository."))
        self._log_history(
            _("GitHub disconnected"), category='github', level='warning', icon='fa-unlink',
            description=_("The GitHub repository was disconnected and the custom source removed."),
        )
        return True

    def action_redeploy_latest(self):
        self.ensure_one()
        self._ensure_storage_not_locked()
        if self.state != 'deploy':
            raise UserError(_("Instance must be deployed to redeploy."))
        addon_name = 'custom_repo'
        custom_addon = self.custom_addon_ids.filtered(lambda a: a.name == addon_name)
        if custom_addon and custom_addon.cloned:
            custom_addon.action_pull()
        else:
            self.action_restart()
        self.last_redeploy_date = fields.Datetime.now()
        self.message_post(body=_("Redeployed latest revision."))
        return True

    def _github_sync_addons(self):
        """The cloned addons that a push should update."""
        self.ensure_one()
        cloned = self.custom_addon_ids.filtered('cloned')
        github = cloned.filtered(lambda a: a.clone_uri and 'github.com' in a.clone_uri)
        return github or cloned

    def action_get_live_logs(self, lines=100):
        """Return the last ``lines`` lines of the instance container's Odoo log.

        The generated ``odoo.conf`` writes to ``/var/log/odoo/odoo.log`` *inside* the
        container, so we go inside the container and tail that file. When the file does
        not exist yet we fall back to the container stdout captured by Docker.
        """
        self.ensure_one()
        if not self.pserver_id or self.state != 'deploy':
            return _("Instance is not currently running on a physical server.")
        try:
            safe_lines = max(1, min(int(lines or 100), 2000))
        except (TypeError, ValueError):
            safe_lines = 100
        container = shlex.quote('odoo_%s' % self.technical_name)
        log_file = shlex.quote('/var/log/odoo/odoo.log')
        cmd = (
            'if docker exec {container} test -f {log_file} 2>/dev/null; then '
            '  docker exec {container} tail -n {lines} {log_file} 2>&1; '
            'else '
            '  docker logs --tail {lines} {container} 2>&1; '
            'fi'
        ).format(container=container, log_file=log_file, lines=safe_lines)
        try:
            ssh = self.pserver_id._connect()
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=30)
            logs = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            ssh.close()
            return logs or err or _("No log output recorded yet.")
        except Exception as e:
            return _("Could not fetch logs: %s") % str(e)

    # ------------------------------------------------------------------
    # Live interactive shell (tmux backed)
    # ------------------------------------------------------------------

    SHELL_KINDS = ('odoo', 'psql')
    SHELL_COLS = 220
    SHELL_ROWS = 50
    # The browser may resize the terminal (fit to viewport / fullscreen); these keep a
    # rogue client from asking for absurd tmux windows.
    SHELL_MIN_COLS, SHELL_MAX_COLS = 40, 500
    SHELL_MIN_ROWS, SHELL_MAX_ROWS = 10, 200
    # How many lines of scrollback are sent to the browser so old output stays reachable
    # (the browser can scroll up). tmux keeps its own history; this only bounds one poll.
    #
    # Measured on this deployment: a pane holding 1000 lines of ordinary command output came
    # back as ~95 KB per poll, against ~4.5 KB for the visible pane alone. At the fast poll
    # rate that is hundreds of KB per second to move over SSH, parse and diff in the browser,
    # which is what made large output (``ls -la /``, ``cat bigfile``) stutter. 300 lines keeps
    # scrolling back useful and cuts the per-poll payload by roughly three quarters. Raise it
    # if deeper scrollback matters more than the smoothness.
    SHELL_HISTORY_LINES = 300
    SHELL_IDLE_TIMEOUT = 30 * 60
    SHELL_KEYS = (
        'Enter', 'BSpace', 'Tab', 'BTab', 'Escape', 'Space', 'DC',
        'Up', 'Down', 'Left', 'Right', 'Home', 'End', 'PPage', 'NPage',
        'C-c', 'C-d', 'C-z', 'C-l', 'C-a', 'C-e', 'C-u', 'C-k', 'C-w',
    )

    def _shell_check_available(self):
        """The live shell only makes sense on a deployed, running instance."""
        self.ensure_one()
        if not self.pserver_id:
            raise UserError(_("This instance is not attached to a physical server yet."))
        if self.state != 'deploy' or self.operation_state != 'run':
            raise UserError(_("Start the instance before opening a shell."))

    def _shell_container_name(self, kind):
        """Container the terminal runs in.

        The browser never sends a container name nor a credential: both are resolved here
        from the instance record and the deployment conventions.
        """
        self.ensure_one()
        if kind not in self.SHELL_KINDS:
            raise UserError(_("Unsupported shell type: %s") % kind)
        prefix = 'odoo_' if kind == 'odoo' else 'psql_'
        container = self.docker_container_ids.filtered(lambda c: c.name.startswith(prefix))[:1]
        return container.name or (prefix + self.technical_name)

    def _shell_session_name(self, kind):
        """Deterministic tmux session name for this instance + shell kind.

        Derived instead of kept in memory, so the terminal keeps working with several HTTP
        workers and survives an Odoo restart (the session lives on the physical server).
        """
        self.ensure_one()
        return 'saas_i%s_%s' % (self.id, kind)

    def _shell_program(self, kind):
        """Command tmux runs inside the container, credentials included."""
        self.ensure_one()
        container = shlex.quote(self._shell_container_name(kind))
        if kind == 'odoo':
            # ``docker exec`` runs as the image user (odoo), so the terminal stays confined
            # to the customer container and can never reach the host.
            return (
                "docker exec -it %s sh -c "
                "'command -v bash >/dev/null 2>&1 && exec bash || exec sh'" % container
            )
        database = shlex.quote(self.db_name or self.technical_name)
        # User/password come from the generated docker-compose.yml, never from the browser.
        return "docker exec -it -e PGPASSWORD=odoo %s psql -U odoo -d %s" % (container, database)

    def _shell_ssh(self):
        """Reuse one SSH connection per worker: polling must stay cheap."""
        self.ensure_one()
        pserver = self.pserver_id
        with _SHELL_SSH_LOCK:
            ssh = _SHELL_SSH_CACHE.get(pserver.id)
            transport = ssh.get_transport() if ssh is not None else None
            if transport is not None and transport.is_active():
                return ssh
            if ssh is not None:
                try:
                    ssh.close()
                except Exception:
                    pass
            ssh = pserver._connect_or_raise()
            _SHELL_SSH_CACHE[pserver.id] = ssh
            return ssh

    def _shell_exec(self, ssh, command):
        """Run one remote command and return its stdout as a list of lines."""
        try:
            output = self.pserver_id._exec_cmd(command, ssh, without_return=False)
        except Exception:
            _logger.exception("Live shell command failed: %s", command)
            return []
        return [line.rstrip('\n') for line in (output or [])]

    def _shell_open(self, kind):
        """Create the terminal, or re-attach to the one still running for this instance.

        Everything travels in a single remote command: tmux availability, the container
        state and the session creation. Opening used to do three separate SSH/sudo round
        trips plus a brand new SSH connection, which made the terminal feel sluggish.
        """
        self.ensure_one()
        self._shell_check_available()
        container = self._shell_container_name(kind)
        session = self._shell_session_name(kind)
        ssh = self._shell_ssh()

        # One remote command that checks tmux and the container, creates the session, waits a
        # moment and reports whether the shell really survived: a failing ``docker exec``
        # would otherwise leave a dead pane.
        quoted = shlex.quote(session)
        command = (
            "if ! command -v tmux >/dev/null 2>&1; then echo SAAS_NO_TMUX; exit 0; fi; "
            "status=$(docker inspect -f '{{{{.State.Status}}}}' {container} 2>/dev/null || true); "
            "if [ \"$status\" != running ]; then echo \"SAAS_NOT_RUNNING ${{status:-unknown}}\"; exit 0; fi; "
            "if tmux has-session -t {s} 2>/dev/null; then echo SAAS_REUSED; "
            "elif tmux new-session -d -s {s} -x {cols} -y {rows} {prog}; then "
            # Short settle time only: the old full second was pure waiting before the
            # terminal appeared. A container that cannot run the command is detected
            # just as well after a quarter of a second.
            "sleep 0.3; "
            "if tmux has-session -t {s} 2>/dev/null; then "
            "tmux set-option -t {s} remain-on-exit on; "
            "tmux set-option -t {s} @saas_last $(date +%s); echo SAAS_OPENED; "
            "else echo SAAS_DIED; fi; "
            "else echo SAAS_OPEN_FAILED; fi"
        ).format(container=shlex.quote(container), s=quoted,
                 cols=self.SHELL_COLS, rows=self.SHELL_ROWS,
                 prog=self._shell_program(kind))
        output = self._shell_exec(ssh, command)
        marker = output[-1].strip() if output else ''

        if marker == 'SAAS_NO_TMUX':
            raise UserError(_(
                "The live shell needs the 'tmux' package on the physical server. "
                "Install it with: apt-get install -y tmux"
            ))
        if marker.startswith('SAAS_NOT_RUNNING'):
            status = marker.partition(' ')[2] or 'unknown'
            raise UserError(_(
                "Container %(name)s is not running (status: %(status)s). "
                "Start the instance and try again."
            ) % {'name': container, 'status': status})
        if marker == 'SAAS_OPEN_FAILED':
            raise UserError(_("Could not start the shell on the server."))
        if marker == 'SAAS_DIED':
            reason = "\n".join(output[:-1])[-800:]
            raise UserError(_("The shell closed immediately: %s")
                            % (reason or _("unknown reason")))

        return {
            'success': True,
            'session': session,
            'container': container,
            'kind': kind,
            'cols': self.SHELL_COLS,
            'rows': self.SHELL_ROWS,
            'state': 'created' if marker == 'SAAS_OPENED' else 'attached',
        }

    def _shell_read(self, kind):
        """Return the terminal screen (with scrollback), plus cursor and liveness.

        A poll runs several times per second, so the whole screen is fetched with ONE
        remote command (one SSH/sudo round trip). It used to be three separate commands
        — has-session, display-message and capture-pane — which was the main reason the
        terminal felt slow and typing lagged behind.

        ``capture-pane -S -`` also returns the lines tmux keeps in its history, so the
        browser gets the previous output too and can scroll back through it. The cursor
        row is reported relative to the visible pane, so it is shifted by the number of
        history lines that were actually captured.
        """
        self.ensure_one()
        self._shell_check_available()
        quoted = shlex.quote(self._shell_session_name(kind))
        ssh = self._shell_ssh()

        # Long unlikely markers let the meta line and the captured pane be split apart
        # again out of a single output stream. They are printed by the remote shell
        # (never inside tmux), so the pane content cannot mix them up.
        meta_begin = '###SAAS_META_BEGIN###'
        screen_begin = '###SAAS_SCREEN_BEGIN###'
        screen_end = '###SAAS_SCREEN_END###'
        dead_mark = '###SAAS_DEAD###'
        command = (
            "if tmux has-session -t {s} 2>/dev/null; then "
            "echo '{screen_begin}'; "
            "tmux capture-pane -p -e -S -{history} -t {s}; "
            "echo '{screen_end}'; "
            # The cursor is read *after* the pane: if the shell prints something between
            # the two commands the cursor may only be ahead of the captured screen, never
            # behind it. A cursor behind the echo made the browser draw the same
            # characters a second time (the "llss" bug).
            "echo '{meta_begin}'; "
            "tmux display-message -p -t {s} "
            "'#{{cursor_x}} #{{cursor_y}} #{{pane_width}} #{{pane_height}} #{{pane_dead}}'; "
            "tmux set-option -t {s} @saas_last $(date +%s) 2>/dev/null; "
            "else echo '{dead_mark}'; fi"
        ).format(s=quoted, meta_begin=meta_begin, screen_begin=screen_begin,
                 screen_end=screen_end, dead_mark=dead_mark,
                 history=self.SHELL_HISTORY_LINES)
        output = self._shell_exec(ssh, command)

        ended = {
            'success': True,
            'alive': False,
            'message': _("Shell session ended. Click Reconnect to start a new one."),
        }
        if meta_begin not in output or screen_begin not in output:
            return ended
        try:
            screen_at = output.index(screen_begin)
            screen_stop = output.index(screen_end, screen_at + 1)
            meta_at = output.index(meta_begin, screen_stop + 1)
        except ValueError:
            return ended

        values = output[meta_at + 1].split() if meta_at + 1 < len(output) else []
        screen = output[screen_at + 1:screen_stop]

        def as_int(index, default):
            try:
                return int(values[index])
            except (IndexError, ValueError):
                return default

        pane_height = as_int(3, self.SHELL_ROWS) or self.SHELL_ROWS
        # tmux reports cursor_y relative to the visible pane; capturing history adds the
        # history lines on top, so shift the cursor row to match the returned screen.
        history_offset = max(0, len(screen) - pane_height)

        return {
            'success': True,
            'alive': True,
            'dead': as_int(4, 0) == 1,
            'cursor': [as_int(0, 0), history_offset + as_int(1, 0)],
            'size': [as_int(2, self.SHELL_COLS), pane_height],
            'screen': "\n".join(screen),
        }

    def _shell_resize(self, kind, cols=None, rows=None):
        """Match the tmux window to the browser viewport (fit / fullscreen).

        Without this the terminal always kept its default 220x50 grid: on a big screen it
        scrolled sideways, on a small one long lines wrapped early. Only one remote
        command is run, so resizing stays cheap.
        """
        self.ensure_one()
        self._shell_check_available()
        try:
            cols = max(self.SHELL_MIN_COLS, min(int(cols), self.SHELL_MAX_COLS))
            rows = max(self.SHELL_MIN_ROWS, min(int(rows), self.SHELL_MAX_ROWS))
        except (TypeError, ValueError):
            return {'success': True}
        quoted = shlex.quote(self._shell_session_name(kind))
        self._shell_exec(
            self._shell_ssh(),
            "tmux set-option -t {s} window-size manual 2>/dev/null; "
            "tmux resize-window -t {s} -x {cols} -y {rows} 2>/dev/null; "
            "echo SAAS_RESIZED".format(s=quoted, cols=cols, rows=rows),
        )
        return {'success': True, 'cols': cols, 'rows': rows}

    def _shell_write(self, kind, text=None, keys=None):
        """Send typed text and/or special keys to the terminal."""
        self.ensure_one()
        self._shell_check_available()
        quoted = shlex.quote(self._shell_session_name(kind))
        commands = []
        if text:
            # tmux parses ';' on its own command line, which silently swallowed a trailing
            # semicolon (e.g. "SELECT 1;" arrived as "SELECT 1"). The text therefore travels
            # as base64 on stdin, which is safe for every character (';', quotes, unicode...).
            encoded = shlex.quote(base64.b64encode(text.encode('utf-8')).decode('ascii'))
            buffer_name = 'saasbuf_$$'
            commands.append("printf %%s %s | base64 -d | tmux load-buffer -b %s -"
                            % (encoded, buffer_name))
            commands.append("tmux paste-buffer -t %s -b %s -d" % (quoted, buffer_name))
        for key in (keys or []):
            if key not in self.SHELL_KEYS:
                continue
            commands.append("tmux send-keys -t %s %s" % (quoted, key))
        if not commands:
            return {'success': True}
        commands.append("tmux set-option -t " + quoted + " @saas_last $(date +%s) 2>/dev/null")
        commands.append("echo SAAS_SENT")
        output = self._shell_exec(self._shell_ssh(), " ; ".join(commands))
        return {'success': True, 'sent': bool(output and output[-1].strip() == 'SAAS_SENT')}

    def _shell_close(self, kind):
        """Destroy the terminal session and the container process behind it."""
        self.ensure_one()
        session = shlex.quote(self._shell_session_name(kind))
        self._shell_exec(self._shell_ssh(),
                         "tmux kill-session -t %s 2>/dev/null; echo SAAS_CLOSED" % session)
        return {'success': True, 'alive': False}

    @api.model
    def cron_shell_cleanup(self):
        """Kill terminals nobody used for a while; they would pile up on the server."""
        threshold = int(time.time()) - self.SHELL_IDLE_TIMEOUT
        for pserver in self.env['saas.pserver'].search([]):
            ssh = pserver._connect()
            if not ssh:
                continue
            try:
                listing = pserver._exec_cmd(
                    "tmux list-sessions -F '#{session_name} #{?@saas_last,#{@saas_last},0}' "
                    "2>/dev/null", ssh, without_return=False) or []
                for line in listing:
                    name, _, last = line.strip().partition(' ')
                    if not name.startswith('saas_i'):
                        continue
                    try:
                        stale = int(last or 0) < threshold
                    except ValueError:
                        stale = True
                    if stale:
                        pserver._exec_cmd("tmux kill-session -t %s" % shlex.quote(name), ssh)
                        _logger.info("Killed idle instance shell %s", name)
            except Exception:
                _logger.exception("Could not clean up idle instance shells")
            finally:
                ssh.close()

    def action_get_github_commits(self, limit=10):
        """Return the newest commits of the connected GitHub repository.

        Each entry is ``{'hash', 'date', 'author', 'subject'}``. Used so the History tab
        can show, line by line, when new source was pulled from GitHub.
        """
        self.ensure_one()
        if not self.pserver_id or self.state not in ('deploy', 'suspend'):
            return []
        addon = self.custom_addon_ids.filtered(lambda a: a.name == 'custom_repo')[:1]
        if not addon or not addon.addon_path:
            return []
        try:
            safe_limit = max(1, min(int(limit or 10), 50))
        except (TypeError, ValueError):
            safe_limit = 10
        cmd = (
            "cd %s 2>/dev/null && git log -n %d --date=format:'%%Y-%%m-%%d %%H:%%M' "
            "--pretty=format:'%%h|%%ad|%%an|%%s' 2>/dev/null || true"
        ) % (shlex.quote(addon.addon_path), safe_limit)
        try:
            ssh = self.pserver_id._connect()
            _stdin, stdout, _stderr = ssh.exec_command(cmd, timeout=25)
            raw = stdout.read().decode('utf-8', errors='replace')
            ssh.close()
        except Exception as e:
            _logger.warning("Could not read GitHub commits for %s: %s", self.name, e)
            return []
        commits = []
        for line in raw.splitlines():
            parts = line.split('|', 3)
            if len(parts) == 4:
                commits.append({
                    'hash': parts[0], 'date': parts[1], 'author': parts[2], 'subject': parts[3],
                })
        return commits

    def _record_github_sync_history(self, branch_name=None, action=None):
        """Log the GitHub connection and the exact commits that were pulled, line by line."""
        self.ensure_one()
        branch_name = branch_name or self.github_branch or 'main'
        repo = self.github_repo_name or self.github_repo_url or ''
        self._log_history(
            action or _("GitHub connected"),
            category='github',
            level='success',
            icon='fa-github',
            summary="%s @ %s" % (repo, branch_name),
            description=_(
                "Connected repository %(repo)s on branch %(branch)s and synced the source."
            ) % {'repo': repo, 'branch': branch_name},
        )
        commits = self.action_get_github_commits(limit=10)
        if not commits:
            return
        body = "\n".join(
            "%s  ·  %s  ·  %s  ·  %s" % (
                c['hash'], c['date'], c['author'], c['subject'],
            )
            for c in commits
        )
        self._log_history(
            _("Source pulled from GitHub"),
            category='github',
            level='info',
            icon='fa-code-fork',
            summary=_("%(count)s commit(s) on %(branch)s") % {'count': len(commits), 'branch': branch_name},
            description=_("Latest source taken from GitHub, line by line:") + "\n" + body,
        )

    def action_redeploy_config(self):
        for r in self:
            addons_config = r.config_ids.filtered(lambda config: config.name == 'addons_path')
            if addons_config:
                addons_config.write({'value': r._get_addons_path()})
            r.pserver_id._redeploy_odoo_instance_config(r)

    def action_redeploy_nginx(self):
        for r in self:
            r.pserver_id._redeploy_odoo_instance_nginx(r.domain_name_ids)

    def action_check_config(self):
        """Phase 2 : dry-run de la config — valide odoo.conf + docker-compose.yml
        (syntaxe locale + docker-compose config sur le serveur si déployé)."""
        self.ensure_one()
        errors = []
        warnings = []

        # 1. odoo.conf — syntaxe INI
        conf = self.env['saas.odoo.instance.config']._get_config_file_content(self)
        if not conf.strip():
            errors.append(_("odoo.conf : contenu vide (aucune config générée)."))
        else:
            try:
                import configparser
                parser = configparser.ConfigParser()
                parser.read_string(conf)
            except Exception as e:
                errors.append(_("odoo.conf : %s") % e)

        # 2. docker-compose.yml — syntaxe YAML
        compose = self._get_docker_compose_file_content()
        try:
            try:
                import yaml
                data = yaml.safe_load(compose)
                if not isinstance(data, dict) or 'services' not in data:
                    errors.append(_("docker-compose.yml : clé 'services' manquante."))
            except ImportError:
                pass  # PyYAML is optional
        except Exception as e:
            errors.append(_("docker-compose.yml : %s") % e)

        # 3. Dry-run sur le serveur (si instance déployée)
        if self.state == 'deploy' and self.pserver_id:
            try:
                ssh = self.pserver_id._connect_or_raise()
                out = self.pserver_id._exec_cmd(
                    'cd /home/%s && '
                    '(docker compose version >/dev/null 2>&1 && docker compose config -q '
                    '|| docker-compose config -q); echo EXIT:$?'
                    % self.technical_name,
                    ssh, without_return=False, raise_on_error=False,
                )
                exit_code = None
                for line in out:
                    if line.startswith('EXIT:'):
                        exit_code = line.strip().split(':', 1)[1]
                if exit_code != '0':
                    errors.append(_("docker compose config (serveur) : exit %s")
                                  % exit_code)
                else:
                    warnings.append(_("docker compose config (serveur) : OK"))
            except Exception as e:
                errors.append(_("docker compose config (serveur) : %s") % e)

        if errors:
            message = _("Vérification config échouée :\n- %s") \
                % '\n- '.join(errors)
            msg_type = 'danger'
        else:
            message = _("Vérification config OK (odoo.conf + docker-compose.yml).")
            if warnings:
                message += ' ' + ' '.join(warnings)
            msg_type = 'success'

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': message,
                'type': msg_type,
                'sticky': True,
            }
        }

    def action_upgrade_modules(self, module):
        for r in self:
            odoo_command = 'odoo -u %s -d %s' % (module, r.technical_name)
            r.pserver_id._recreate_docker_compose_file(r, odoo_command)

        self.write({'need_to_compose_up': True})

    def action_install_modules(self, module):
        for r in self:
            odoo_command = 'odoo -i %s -d %s' % (module, r.technical_name)
            r.pserver_id._recreate_docker_compose_file(r, odoo_command)

        self.write({'need_to_compose_up': True})

    @api.model
    def _get_technical_name(self, length):
        return ''.join(random.choice(string.ascii_lowercase) for i in range(length))

    def _prepare_docker_containers(self):
        res = [
        (0, 0, {
            'name': 'odoo_%s' % self.technical_name,
            'container_type': 'odoo',
            'image': self.docker_odoo_image,
            'docker_compose_volume_ids': self._prepare_docker_compose_odoo_volumes()
        }),
        (0, 0, {
            'name': 'psql_%s' % self.technical_name,
            'container_type': 'psql',
            'image': self.docker_psql_image,
            'docker_compose_volume_ids': [(0, 0, {
                'instance_id': self.id,
                'name': 'pgdata',
                'volume_type': 'pgdata',
                'container_path': '/var/lib/postgresql/data/pgdata'
            })]
        })]
        return res

    def _prepare_docker_compose_odoo_volumes(self):
        volumes = [
            (0, 0, {
                'instance_id': self.id,
                'name': 'odoo-web-data',
                'volume_type': 'odoo_filestore',
                'container_path': '/var/lib/odoo'
            }),
            (0, 0, {
                'instance_id': self.id,
                'name': 'config',
                'volume_type': 'odoo_config',
                'container_path': '/etc/odoo',
            }),
            (0, 0, {
                'instance_id': self.id,
                'name': 'custom-addons',
                'volume_type': 'odoo_custom_addons',
                'container_path': '/mnt/extra-addons'
            })
        ]
        for i, extra_addon in enumerate(self.odoo_server_id.extra_addon_ids):
            container_path = '/mnt/standard-extra-addons' if i == 0 else '/mnt/standard-extra-addons-%d' % (i + 1)
            volumes.append((0, 0, {
                'instance_id': self.id,
                'name': extra_addon.source_path,
                'volume_type': 'odoo_extra_addons',
                'container_path': container_path
            }))
        return volumes

    def _generate_instance_port(self):
        starting_port = self.env.user.company_id.instance_starting_port

        free_ports = self.env['saas.odoo.instance.port'].search([('instance_id', '=', False)], order='port', limit=3)
        if free_ports:
            return free_ports.sudo().write({'instance_id': self.id})

        used_port = self.env['saas.odoo.instance.port'].search([('instance_id', '!=', False)], order='port desc', limit=1)
        if used_port:
            starting_port = used_port.port + 1

        port_vals_list = [
            {'name': 'xmlrpc_port', 'port': starting_port, 'pserver_id': self.pserver_id.id, 'instance_id': self.id},
            {'name': 'xmlrpcs_port', 'port': starting_port + 1, 'pserver_id': self.pserver_id.id, 'instance_id': self.id},
            {'name': 'longpolling_port', 'port': starting_port + 2, 'pserver_id': self.pserver_id.id, 'instance_id': self.id},
        ]
        return self.env['saas.odoo.instance.port'].sudo().create(port_vals_list)

    def _generate_instance_config(self):        
        conf_vals_list = self._prepare_conf_vals_list()
        self.config_ids.unlink()
        configs = self.env['saas.odoo.instance.config'].create(conf_vals_list)
        # Keep the instance's Master Password field in sync with the generated odoo.conf.
        admin_config = configs.filtered(lambda c: c.name == 'admin_passwd')[:1]
        if admin_config and self.admin_pass != admin_config.value:
            self.with_context(skip_admin_pass_sync=True).admin_pass = admin_config.value
        return configs

    def _apply_admin_password(self, password):
        """Set the Odoo master password (``admin_passwd``) of this instance.

        Updates the ``admin_passwd`` config row, mirrors it on the instance record,
        rewrites ``odoo.conf`` on the instance server and restarts the Odoo container so
        the new password is active immediately.
        """
        self.ensure_one()
        password = (password or '').strip()
        if not password:
            raise UserError(_("The master password cannot be empty."))
        configs = self.config_ids.filtered(lambda c: c.name == 'admin_passwd')
        if configs:
            configs[0].with_context(skip_admin_pass_sync=True).write({'value': password})
        if self.admin_pass != password:
            self.with_context(skip_admin_pass_sync=True).write({'admin_pass': password})
        if self.pserver_id and self.state == 'deploy':
            self.pserver_id._redeploy_odoo_instance_config(self)
        return True

    def write(self, vals):
        res = super().write(vals)
        if 'admin_pass' in vals and not self.env.context.get('skip_admin_pass_sync'):
            for instance in self:
                instance._apply_admin_password(instance.admin_pass)
        return res


    def _get_addons_path(self):
        """Return addon mount paths in the same order as the server lines."""
        self.ensure_one()
        addons_path = [
            '/mnt/standard-extra-addons/' if index == 0
            else '/mnt/standard-extra-addons-%d/' % (index + 1)
            for index, addon in enumerate(self.odoo_server_id.extra_addon_ids)
            if addon.source_path
        ]
        addons_path.append('/mnt/extra-addons')
        addons_path += self.custom_addon_ids.mapped('container_path')
        return ','.join(dict.fromkeys(filter(None, addons_path)))

    def _prepare_conf_vals_list(self):
        conf_vals_list = []
        for conf in self.odoo_version_id.config_ids:
            value = conf.value
            if conf.name == 'addons_path':
                value = self._get_addons_path()
            elif conf.name == 'admin_passwd':
                # Keep a password the customer already set; only generate one the first time.
                value = self.admin_pass or ''.join(
                    random.choice(string.ascii_lowercase) for i in range(32))
            elif conf.name == 'data_dir':
                value = '/var/lib/odoo'
            elif conf.name == 'db_name':
                if not self.template_instance_id:
                    value = self.technical_name
                else:
                    db_name_configs = self.template_instance_id.config_ids.filtered(lambda c: c.name == 'db_name')
                    if db_name_configs:                        
                        value = db_name_configs[0].value
                    else:
                        value = self.technical_name
            elif conf.name == 'dbfilter':
                if not self.template_instance_id:
                    value = self.technical_name
                else:
                    db_name_configs = self.template_instance_id.config_ids.filtered(lambda c: c.name == 'db_name')
                    if db_name_configs:                        
                        value = db_name_configs[0].value
                    else:
                        value = self.technical_name
            elif conf.name == 'logfile':
                value = '/var/log/odoo/odoo.log'
            elif conf.name == 'without_demo':
                value = not self.user_demo_data
            elif conf.name == 'workers':
                # Always deploy exactly the number of workers the customer owns.
                value = max(self.workers_count or 1, 1)
            elif conf.name == 'limit_memory_soft':
                value = max(self.workers_count or 1, 1) * WORKER_MEMORY_SOFT
            elif conf.name == 'limit_memory_hard':
                value = max(self.workers_count or 1, 1) * WORKER_MEMORY_HARD

            conf_vals_list.append({
                'instance_id': self.id,
                'name': conf.name,
                'value': str(value),
                'section_id': conf.section_id.id,
            })

        return conf_vals_list

    def _generate_instance_extra_addons(self):
        extra_addon_vals_list = []
        self.extra_addon_ids.sudo().unlink()
        if self.use_template and self.template_instance_id: 
            for extra_addon in self.template_instance_id.extra_addon_ids:
                extra_addon_vals_list.append({
                    'instance_id': self.id,
                    'name': extra_addon.name,
                    'addon_path': extra_addon.addon_path,
                    'copy_to': extra_addon.copy_to,
                    'container_path': extra_addon.container_path,
                })
        else:
            for extra_addon in self.odoo_server_id.extra_addon_ids:
                extra_addon_vals_list.append({
                    'instance_id': self.id,
                    'name': extra_addon.source_path.split('/')[-1],
                    'addon_path': extra_addon.source_path,
                    'copy_to': '/home/%s/custom-addons' % self.technical_name,
                    'container_path': extra_addon.docker_container_path,
                })

        self.env['saas.odoo.instance.extra.addon'].sudo().create(extra_addon_vals_list)

    def _generate_instance_domain_name(self):
        domain_name = self.env['saas.odoo.instance.domain.name'].search([('name', '=', self.domain_name)])
        if not domain_name:
            domain_name = self.env['saas.odoo.instance.domain.name'].create({
                'name': self.domain_name,
                'instance_id': self.id,
                'is_instance_domain_name': True,
                'noindex': self.noindex,
            })
        else:
            domain_name.write({'noindex': self.noindex})
        return domain_name

    def _free_instance_port(self):
        self.port_ids.sudo().write({'instance_id': False})

    def _get_domain_name(self):
        return self.name + '.' + self.based_domain_id.name

    # ------------------------------------------------------------------
    # pgvector
    # ------------------------------------------------------------------
    # Initdb script mounted into the PostgreSQL container; it lives next to
    # docker-compose.yml in the instance folder.
    PGVECTOR_INIT_FILE = 'pgvector-init.sql'
    # A restored or template-copied cluster can still be booting when we ask for the
    # extension, so the safety net below retries for a while before giving up.
    PGVECTOR_WAIT_ATTEMPTS = 10
    PGVECTOR_WAIT_SECONDS = 2

    def _get_pgvector_init_sql(self):
        """SQL the PostgreSQL entrypoint runs when the cluster is created.

        Installing the extension in ``template1`` is what makes this work for *every*
        database created in the cluster afterwards -- the instance database included --
        because ``CREATE DATABASE`` copies template1. The entrypoint runs this before the
        server accepts any connection, so the extension is in place before Odoo starts
        creating its database. Deploying first and running ``CREATE EXTENSION`` afterwards
        would race Odoo's own initialisation instead.
        """
        return (
            "-- Written by s_odoo_saas_master. Runs once, on the first start of this\n"
            "-- PostgreSQL cluster, before it accepts connections.\n"
            "\\connect template1\n"
            "CREATE EXTENSION IF NOT EXISTS vector;\n"
        )

    def _get_pgvector_init_file_path(self):
        return '/home/%s/%s' % (self.technical_name, self.PGVECTOR_INIT_FILE)

    def _ensure_pgvector_extension(self, ssh, wait=False):
        """Make sure ``vector`` exists in this instance's database (idempotent).

        A freshly created instance already has it: the initdb script installs the extension
        in template1, which every later ``CREATE DATABASE`` copies. This is the safety net
        for databases that did *not* come from a freshly initialised cluster -- template
        based deploys, restores, and instances created before pgvector existed -- so those
        get it too instead of needing a manual ``CREATE EXTENSION``.

        Never raises: pgvector is an extra, and it must not turn a working deployment into a
        failed one. A failure is logged with the server output instead.
        """
        self.ensure_one()
        container = shlex.quote('psql_%s' % self.technical_name)
        database = shlex.quote(self.db_name or self.technical_name)
        statement = shlex.quote('CREATE EXTENSION IF NOT EXISTS vector')
        command = ('docker exec -e PGPASSWORD=odoo %s psql -U odoo -d %s -c %s'
                   % (container, database, statement))
        attempts = self.PGVECTOR_WAIT_ATTEMPTS if wait else 1
        output = ''
        for attempt in range(attempts):
            try:
                exit_code, output = self.pserver_id._exec_capture(command, ssh)
            except Exception:
                _logger.exception("Could not enable pgvector on %s", self.display_name)
                return False
            if not exit_code:
                return True
            if attempt + 1 < attempts:
                time.sleep(self.PGVECTOR_WAIT_SECONDS)
        _logger.warning(
            "pgvector is not enabled on %s: the PostgreSQL image of this instance may not "
            "provide the extension. Server said: %s",
            self.display_name, (output or '').strip()[-400:])
        return False

    def _get_docker_compose_file_content(self, odoo_command=False):
        file_content = ''
        file_content += 'services:\n'
        file_content += '    web:\n'
        file_content += '        container_name: odoo_' + self.technical_name + '\n'
        file_content += '        image: ' + self.docker_odoo_image + '\n'
        file_content += '        depends_on:\n'
        file_content += '            - db\n'
        file_content += '        ports:\n'
        file_content += '            - "' + self.docker_xmlrpc_expose_port + ':' + self.docker_xmlrpc_container_port + '"\n'
        file_content += '            - "' + self.docker_xmlrpcs_expose_port + ':' + self.docker_xmlrpcs_container_port + '"\n'
        file_content += '            - "' + self.docker_longpolling_expose_port + ':' + self.docker_longpolling_container_port + '"\n'
        file_content += '        restart: unless-stopped\n'
        workers = max(self.workers_count or 1, 1)
        file_content += '        mem_limit: %dm\n' % (workers * WORKER_MEMORY_HARD // (1024 * 1024))
        file_content += '        mem_reservation: %dm\n' % (workers * WORKER_MEMORY_SOFT // (1024 * 1024))
        if self.docker_compose_volume_ids:
            file_content += '        volumes:\n'
            for volume in self.docker_compose_volume_ids.filtered(lambda v: v.volume_type != 'pgdata'):
                if volume.volume_type == 'odoo_filestore':
                    file_content += '            - ' + volume.name + ':' + volume.container_path + '\n'
                elif volume.volume_type == 'odoo_extra_addons':
                    file_content += '            - ' + volume.name + ':' + volume.container_path + '\n'
                else:
                    file_content += '            - ./' + volume.name + ':' + volume.container_path + '\n'
        if odoo_command:
            file_content += '        command: ' + odoo_command + '\n'
        file_content += '    db:\n'
        file_content += '        container_name: psql_' + self.technical_name + '\n'
        file_content += '        image: ' + self.docker_psql_image + '\n'
        file_content += '        environment:\n'
        file_content += '            - POSTGRES_DB=postgres\n'
        file_content += '            - POSTGRES_PASSWORD=odoo\n'
        file_content += '            - POSTGRES_USER=odoo\n'
        file_content += '            - PGDATA=/var/lib/postgresql/data/pgdata\n'
        file_content += '        restart: unless-stopped\n'
        if self.docker_compose_volume_ids:
            file_content += '        volumes:\n'
            for volume in self.docker_compose_volume_ids.filtered(lambda v: v.volume_type == 'pgdata'):
                file_content += '            - ./' + volume.name + ':' + volume.container_path + '\n'
            # pgvector: the postgres entrypoint executes everything in this directory on the
            # first start of an empty PGDATA, i.e. exactly when the instance is created and
            # before anything can connect to it. See _get_pgvector_init_sql().
            file_content += '            - ./%s:/docker-entrypoint-initdb.d/00-pgvector.sql:ro\n' % self.PGVECTOR_INIT_FILE

            if any(volume.volume_type == 'odoo_filestore' for volume in self.docker_compose_volume_ids):
                file_content += 'volumes:\n'
                for volume in self.docker_compose_volume_ids.filtered(lambda v: v.volume_type == 'odoo_filestore'):
                    file_content += '    ' + volume.name + ':\n'
                    file_content += '        driver: local\n'
                    file_content += '        driver_opts:\n'
                    file_content += '            type: none\n'
                    file_content += '            device: ' + volume.storage_path + '\n'
                    file_content += '            o: bind\n'

        return file_content

    def _get_docker_compose_file_path(self):
        return '/home/%s/docker-compose.yml' % self.technical_name

    @api.model
    def _recover_stuck_backups(self):
        """Mark backups left 'running' by a server restart/crash as failed."""
        stale_before = fields.Datetime.subtract(fields.Datetime.now(), minutes=45)
        stuck = self.search([
            ('backup_state', '=', 'running'),
            '|', ('backup_started_at', '=', False), ('backup_started_at', '<', stale_before),
        ])
        for instance in stuck:
            instance.write({
                'backup_state': 'failed',
                'backup_progress': 100,
                'backup_message': _("Backup interrupted (the server was restarted)."),
                'backup_finished_at': fields.Datetime.now(),
            })
            instance._log_history(
                _("Backup interrupted"),
                category='backup', level='warning', icon='fa-exclamation-triangle',
                description=_("The backup was interrupted before it finished (server restart)."),
                source='cron',
            )

    @api.model
    def cron_auto_backup(self):
        """Start the daily automatic backup for every instance with the toggle on.

        The backup itself runs in a background thread, so the cron returns immediately and
        the portal can follow the progress live.
        """
        # Clean up any backup that was left running by a previous crash.
        self._recover_stuck_backups()
        day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        for instance in self.search([('enable_autobackup', '=', True), ('state', '=', 'deploy')]):
            if instance.backup_state == 'running':
                continue
            # Never take more than one automatic backup per day, even if the cron runs twice.
            already_done = instance.backup_ids.filtered(
                lambda b: b.backup_type == 'auto' and b.datetime and b.datetime >= day_start
            )
            if already_done:
                continue
            try:
                instance.action_backup_async(backup_type='auto')
            except Exception as e:
                error = "Error when creating backup for %s: %s" % (instance.name, str(e) or repr(e))
                _logger.exception(error)
                instance._log_history(
                    _("Backup failed"),
                    category='backup',
                    level='danger',
                    icon='fa-exclamation-triangle',
                    summary=error[:140],
                    description=error,
                    source='cron',
                )

    @api.model
    def cron_clean_backup(self):
        """Enforce the retention policy: keep only the newest ``backup_limit`` backups."""
        for instance in self.search([('backup_limit', '>', 0)]):
            instance._enforce_backup_retention()

    def action_get_active_users(self):
        results = {}
        instances = self.filtered(lambda i: i.state == 'deploy')
        for pserver in self.pserver_id:
            results.update(pserver._get_active_user(instances))
        for r in self:
            active_user = results.get(r.id, 0)
            r.write({'active_user': active_user})

    def action_get_installed_apps(self):
        results = {}
        instances = self.filtered(lambda i: i.state == 'deploy')
        for pserver in self.pserver_id:
            results.update(pserver._get_installed_apps(instances))
        for r in self:
            r.installed_app_ids.sudo().unlink()
            installed_apps = results.get(r.id, [])
            installed_app_ids = []
            for app in installed_apps:
                app_product = self.env['product.product'].search([('technical_name', '=', app['technical_name'])], limit=1)
                installed_app_ids.append((0, 0, {
                    'name': app['name'],
                    'technical_name': app['technical_name'],
                    'installed_date': app['installed_date'],
                    'product_id': app_product.id or False
                }))
            r.sudo().write({'installed_app_ids': installed_app_ids})

    def _notify_expiration(self):
        for r in self:
            if r.expiration_date and r.state == 'deploy':
                delta_days = (r.expiration_date - fields.Date.today()).days
                if delta_days <= r.company_id.notification_expiration_day:
                    if r.partner_id:
                        order = r._create_renew_so()
                        if not order:
                            continue
                        order.action_confirm()
                        invoice = order._create_saas_invoice()
                        if not invoice:
                            continue
                        invoice._post()
                        if r.partner_id.email and r.partner_id._saas_notify_wants('expiry_reminder'):
                            template_id = self.env.ref('s_odoo_saas_master.instance_expiration_notify_mail_template', raise_if_not_found=False)
                            if template_id:
                                template_id.sudo().send_mail(r.id, force_send=True)

    def _notify_revoke(self):
        for r in self:
            if r.expiration_date:
                delta_days = (r.expiration_date - fields.Date.today()).days
                if delta_days <= r.company_id.revoke_instance_day:
                    if (r.partner_id and r.partner_id.email
                            and r.partner_id._saas_notify_wants('revoked')):
                        template_id = self.env.ref('s_odoo_saas_master.instance_revoke_notify_mail_template', raise_if_not_found=False)
                        if template_id:
                            template_id.sudo().send_mail(r.id, force_send=True)

    @api.model
    def cron_get_active_user(self):
        instances = self.search([('state', '=', 'deploy')])
        instances.action_get_active_users()

    @api.model
    def cron_get_installed_app(self):
        instances = self.search([('state', '=', 'deploy')])
        instances.action_get_installed_apps()

    @api.model
    def cron_expiration_notification(self):
        instances = self.search([('state', '=', 'deploy')])
        instances._notify_expiration()

    @api.model
    def cron_suspend_instance(self):
        instances = self.search([('state', '=', 'deploy')])
        for instance in instances:
            if instance.expiration_date:
                if instance.expiration_date < fields.Date.today():
                    instance.action_suspend()
                    # Fires once: the instance leaves the ('state', '=', 'deploy') search
                    # after action_suspend(), so the daily cron cannot email it again.
                    instance.partner_id._saas_notify(
                        'expired',
                        instance=instance,
                        title=_("Your hosting subscription has expired"),
                        intro=_("The instance was suspended because its subscription expired. "
                                "Renew it to start the instance again — your data is kept safe."),
                        rows=[('Expired on', str(instance.expiration_date))],
                        cta_label=_("Renew now"),
                    )
                else:
                    if instance.partner_id and instance.sale_order_ids:
                        if not instance.account_move_ids or any(move.payment_state != 'paid' for move in instance.account_move_ids):
                            instance.action_suspend(has_extra=True)

    @api.model
    def cron_create_extra_so(self):
        instances = self.search([('state', '=', 'deploy'), ('has_extra', '=', True)])
        for instance in instances:
            if not instance.subscription_type:
                # Without a subscription type ``sale.order._action_confirm()`` rejects the
                # order, which used to make this cron fail with a ValidationError every day.
                _logger.warning(
                    "Skipping extra order for instance %s: no subscription type set.",
                    instance.name,
                )
                continue
            order = instance._create_renew_so(buy_extra=True)
            if not order:
                continue
            order.action_confirm()
            invoice = order._create_saas_invoice()
            if invoice:
                invoice._post()

    @api.model
    def cron_revoke_notification(self):
        instances = self.search([('state', '=', 'deploy')])
        instances._notify_revoke()

    @api.model
    def cron_revoke_instance(self):
        """Do not automatically delete expired instance data.

        Instance cancellation removes the complete instance directory, including
        docker-compose.yml. Revocation must therefore remain a manual action.
        """
        _logger.info("Automatic instance revocation is disabled; no instance data was deleted.")
        return True

    @api.model
    def cron_check_instance_status(self):
        """Réconcilie l'état Docker réel avec l'état Odoo (Phase 0.5).

        Ne traite que les instances en 'deploy/run' : si leurs containers ne sont
        plus 'running' (crash, arrêt manuel, serveur redémarré sans docker compose
        up, ...), l'instance est repassée en 'draft' avec operation_state 'stop'
        afin de pouvoir être redéployée proprement depuis Odoo.

        Les instances en 'deploy/stop' (arrêt volontaire) et les serveurs
        injoignables (statuts 'unknown') ne sont jamais modifiés.
        """
        instances = self.search([
            ('state', '=', 'deploy'),
            ('operation_state', '=', 'run'),
        ])
        for instance in instances:
            try:
                statuses = instance.pserver_id._get_container_status(
                    instance.docker_container_ids
                )
            except Exception:
                _logger.exception(
                    "Cannot check container status of instance %s",
                    instance.display_name,
                )
                continue

            if not statuses:
                _logger.warning(
                    "Instance %s has no docker container status (server unreachable or containers missing).",
                    instance.display_name,
                )
                continue

            # Si tous les statuts sont 'unknown', le serveur est probablement
            # injoignable (ou docker inspect a échoué) : on ne touche pas à l'état.
            if all(st == 'unknown' for st in statuses.values()):
                _logger.warning(
                    "Instance %s: server %s unreachable or status unknown, state left unchanged.",
                    instance.display_name,
                    instance.pserver_id.display_name,
                )
                continue

            running = [name for name, st in statuses.items() if st == 'running']
            not_running = [
                '%s (%s)' % (name, st)
                for name, st in statuses.items()
                if st != 'running'
            ]
            if not_running:
                _logger.warning(
                    "Instance %s marked as deployed but containers are not running: %s. "
                    "Resetting to draft.",
                    instance.display_name,
                    ', '.join(not_running),
                )
                instance.write({
                    'state': 'draft',
                    'operation_state': 'stop',
                })
            elif len(running) == len(statuses) and instance.operation_state != 'run':
                instance.write({'operation_state': 'run'})

        # B1 - garde-fou : repasse en 'failed' les restores bloques en 'running'
        # au-dela de 60 min (thread daemon tue, worker redemarre, etc.).
        stale_restores = self.env['saas.odoo.instance.backup'].sudo().search([
            ('restore_state', '=', 'running'),
            ('restore_start_datetime', '!=', False),
            ('restore_start_datetime', '<', fields.Datetime.now() - timedelta(minutes=60)),
        ])
        for rec in stale_restores:
            _logger.warning(
                "Backup %s: restore stuck in 'running' for over 60 min - marking as failed.",
                rec.name,
            )
            rec.write({
                'restore_state': 'failed',
                'restore_end_datetime': fields.Datetime.now(),
                'restore_error_message': 'Restore timed out (stuck in running state).',
            })
        return True

    @api.model
    def _prepare_instance_val_to_create(self, data):
        based_domain = data.get('based_domain') or data.get('base_domain')
        if isinstance(based_domain, int):
            based_domain = self.env['saas.based.domain'].browse(based_domain)
        elif not based_domain and (data.get('based_domain_id') or data.get('base_domain_id')):
            domain_id = data.get('based_domain_id') or data.get('base_domain_id')
            based_domain = self.env['saas.based.domain'].browse(int(domain_id))

        default_modules = data.get('default_modules')
        subscription_type = data.get('subscription_type')
        trial = data.get('trial')
        sub_domain = data.get('sub_domain')
        partner = data.get('partner')
        buy_now_from_pricing = data.get('buy_now_from_pricing', False)

        odoo_version = False
        odoo_server = False
        version_type = data.get('version_type') or 'community'
        Server = self.env['saas.odoo.server']

        odoo_server_id = data.get('odoo_server_id')
        odoo_version_id = data.get('odoo_version_id')

        if odoo_server_id:
            odoo_server = Server.browse(int(odoo_server_id))
            if odoo_server.exists():
                odoo_version = odoo_server.odoo_version_id

        if not odoo_version and odoo_version_id:
            odoo_version = self.env['saas.odoo.version'].browse(int(odoo_version_id))
            if odoo_version.exists():
                odoo_server = self._find_odoo_server(odoo_version, version_type)

        if not odoo_server or not odoo_version:
            # Look for active server
            odoo_server = Server.search([('active', '=', True)], limit=1)
            if not odoo_server:
                odoo_server = Server.search([], limit=1)
            if odoo_server and odoo_server.odoo_version_id:
                odoo_version = odoo_server.odoo_version_id
                version_type = odoo_server.version_type or version_type

        if not odoo_version:
            odoo_version = self._default_odoo_version()
        if not odoo_version:
            raise ValidationError(_("Cannot find Odoo version"))

        if not odoo_server:
            odoo_server = self._find_odoo_server(odoo_version, version_type)
        if not odoo_server:
            raise ValidationError(_("Cannot find Odoo server of Odoo version % s") % odoo_version.name)

        # The server is what actually serves the edition (it owns the addons path), so it
        # is the source of truth: keep the instance in sync with it. Otherwise a customer
        # picking "Community" for a version that only has a Community server would be fine,
        # but an instance attached to an Enterprise server would still build a Community
        # database and silently ignore the Enterprise addons it mounts.
        version_type = odoo_server.version_type or version_type

        if not based_domain or not based_domain.exists():
            based_domain = self._default_based_domain()
        if not based_domain:
            raise ValidationError(_("Cannot find Based Domain"))

        default_module = self._get_default_modules(default_modules, version_type=version_type)
        expiration_date = self._get_expiration_date(subscription_type, trial=trial)
        backup_limit = self._default_backup_limit()
        storage_limit_gb = data.get('storage_limit_gb') or data.get('storage_gb') or 5.0
        workers_count = data.get('workers_count') or data.get('num_users') or data.get('users_count') or 1
        try:
            workers_count = max(int(workers_count), 1)
        except (TypeError, ValueError):
            workers_count = 1
        workers_count = min(workers_count, self.MAX_WORKERS_PER_INSTANCE)
        partner_id = partner.id if hasattr(partner, 'id') else (partner or False)

        res = {
            'name': sub_domain,
            'based_domain_id': based_domain.id,
            'partner_id': partner_id,
            'default_module': default_module,
            'odoo_version_id': odoo_version.id,
            'version_type': version_type,
            'odoo_server_id': odoo_server.id,
            'backup_limit': backup_limit,
            'trial': trial,
            'subscription_type': subscription_type,
            'expiration_date': expiration_date,
            'buy_now_from_pricing': buy_now_from_pricing,
            'storage_limit_gb': float(storage_limit_gb),
            'workers_count': workers_count,
        }
        return res

    @api.model
    def _find_odoo_server(self, odoo_version, version_type=None):
        """Return the server matching ``odoo_version`` (+ ``version_type`` when possible).

        Preference order: exact version + type (active) → version + type → version (active)
        → any server of that version. An empty recordset is returned when nothing matches,
        so callers can apply their own fallback.
        """
        Server = self.env['saas.odoo.server']
        if not odoo_version:
            return Server.browse()
        by_version = [('odoo_version_id', '=', odoo_version.id)]
        if version_type:
            by_type = by_version + [('version_type', '=', version_type)]
            server = (
                Server.search(by_type + [('active', '=', True)], limit=1, order='sequence, id')
                or Server.search(by_type, limit=1, order='sequence, id')
            )
            if server:
                return server
        return (
            Server.search(by_version + [('active', '=', True)], limit=1, order='sequence, id')
            or Server.search(by_version, limit=1, order='sequence, id')
        )

    @api.model
    def _get_default_modules(self, default_modules, version_type=None):
        """Comma separated list of modules to install when the database is created.

        ``default_modules`` may be a list/tuple or an already joined string. Enterprise
        instances always get ``web_enterprise`` first, otherwise the customer would pay
        for Enterprise and still end up with a plain Community database.
        """
        modules = []
        if isinstance(default_modules, str):
            default_modules = default_modules.split(',')
        for module in (default_modules or []):
            module = str(module).strip()
            if module and module not in modules:
                modules.append(module)
        if version_type == 'enterprise' and ENTERPRISE_MODULE not in modules:
            modules.insert(0, ENTERPRISE_MODULE)
        return ','.join(modules)

    def _get_modules_to_install(self):
        """Modules the deployment has to install with ``odoo -i``.

        Derived from ``default_module`` but re-adds the Enterprise module when the
        instance is Enterprise, so instances created or edited outside the pricing form
        (or created before this rule existed) are deployed as Enterprise too.
        """
        self.ensure_one()
        modules = self._get_default_modules(self.default_module, version_type=self.version_type)
        return [m for m in modules.split(',') if m]

    @api.model
    def _get_expiration_date(self, subscription_type, trial=False, expiration_date=False):
        today = fields.Date.today()
        if trial:
            expiration_date = today + timedelta(days=self.env.user.company_id.instance_trial_day)
            return expiration_date
        base_date = expiration_date if (expiration_date and expiration_date >= today) else today
        if subscription_type == 'monthly':
            expiration_date = base_date + relativedelta(months=1)
        elif subscription_type == 'yearly':
            expiration_date = base_date + relativedelta(months=12)
        return expiration_date

    def action_renew(self):
        order = self._create_renew_so()
        if not order:
            return
        return {
            'name': _('Quotation'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_model': 'sale.order',
            'res_id': order.id
        }

    def action_buy_extra(self):
        order = self._create_renew_so(buy_extra=True)
        if not order:
            return
        return {
            'name': _('Quotation'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_model': 'sale.order',
            'res_id': order.id
        }

    def _create_renew_so(self, buy_extra=False):
        if not self.partner_id:
            # An empty recordset keeps the ``if not order:`` guards of the callers working;
            # ``create(False)`` used to raise instead.
            return self.env['sale.order']

        vals = self._prepare_renew_so_vals(buy_extra=buy_extra)
        if not vals:
            _logger.info(
                "Nothing to bill for instance %s, no %s order created.",
                self.name, 'extra' if buy_extra else 'renew',
            )
            return self.env['sale.order']
        return self.env['sale.order'].create(vals)

    def _prepare_renew_so_vals(self, buy_extra):
        order_lines = []

        product_user = self.env['product.product']
        if not buy_extra:
            product_user = self.env['product.product']._get_saas_worker_product()
        elif (self.active_user - self.paid_user) > 0:
            product_user = self.env['product.product']._get_saas_worker_product()
        if product_user:
            order_lines.append((0, 0, {
                'product_id': product_user.id,
                'product_uom': product_user.uom_id.id,
                'price_unit': product_user.list_price,
                'product_uom_qty': max(self.active_user, self.paid_user) if not buy_extra else (self.active_user - self.paid_user),
            }))

        apps = self.env['saas.odoo.instance.installed.app']
        if not buy_extra:
            apps = self.installed_app_ids
        else:
            apps = self.installed_app_ids.filtered(lambda a: a.not_paid)
        for app in apps:
            product_app = self.env['product.product'].search([('technical_name', '=', app.technical_name)], limit=1)
            if product_app:
                order_lines.append((0, 0, {
                    'product_id': product_app.id,
                    'product_uom': product_app.uom_id.id,
                    'price_unit': product_app.list_price,
                    'product_uom_qty': 12 if self.subscription_type == 'yearly' else 1,
                }))

        if not order_lines:
            return False
        vals = {
            'partner_id': self.partner_id.id,
            'is_saas_order': True,
            'subscription_type': self.subscription_type,
            'instance_id': self.id,
            'saas_order_type': 'renew' if not buy_extra else 'buy_extra',
            'order_line': order_lines
        }
        return vals

    def action_update_resource_package(self):
        self.pserver_id._recreate_docker_compose_file(self, update=True)

    def action_apply_workers(self):
        """Regenerate odoo.conf + docker-compose.yml from ``workers_count`` and
        restart the instance so the new workers and memory limits take effect."""
        self.ensure_one()
        self._generate_instance_config()
        if self.state == 'deploy' and self.pserver_id:
            try:
                # mem_limit / mem_reservation live in the compose file.
                self.pserver_id._recreate_docker_compose_file(self)
                # workers + limit_memory_* live in odoo.conf; restart to load them.
                self.pserver_id._redeploy_odoo_instance_config(self)
            except Exception as e:
                _logger.exception("Could not apply workers for instance %s: %s", self.name, e)
        return True
