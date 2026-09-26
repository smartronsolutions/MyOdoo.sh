# Fix the backup directory (the old default only exists *inside* the containers) and
# switch the retention to 5 backups.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Paths that can never work for the *host* Odoo process: /var/lib/odoo only exists inside
# the containers, and the master service never runs as root.
UNUSABLE_PREFIXES = ('/var/lib/odoo', '/root/')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Clear any unusable backup directory so the runtime falls back to a writable
    #    'instance-backups' folder inside the Odoo data directory.
    companies = env['res.company'].search([])
    for company in companies:
        path = (company.backup_directory or '').strip()
        if not path:
            continue
        if path.startswith(UNUSABLE_PREFIXES):
            _logger.info(
                "Clearing unusable backup directory %s on company %s", path, company.name
            )
            company.backup_directory = False

    # 2. Retention is now 5 backups per instance.
    env['res.company'].search([('instance_backup_limit', '=', 7)]).write({'instance_backup_limit': 5})
    env['saas.odoo.instance'].search([('backup_limit', '=', 7)]).write({'backup_limit': 5})
    env['saas.odoo.instance'].search([('backup_limit', '=', False)]).write({'backup_limit': 5})

    # 3. Give existing instances a first history line so the tab is not empty.
    for instance in env['saas.odoo.instance'].search([]):
        try:
            if instance.history_ids:
                continue
            instance._log_history(
                "Instance created",
                category='lifecycle',
                level='info',
                icon='fa-cloud',
                summary=instance.name,
                description=(
                    "Instance %(name)s (workers: %(workers)s, storage: %(storage).2f GB). "
                    "Activity history tracking started from now on."
                ) % {
                    'name': instance.name,
                    'workers': instance.workers_count or 1,
                    'storage': instance.storage_limit_gb or 0.0,
                },
                source='system',
            )
            if instance.github_repo_url:
                instance._log_history(
                    "GitHub repository connected",
                    category='github',
                    level='info',
                    icon='fa-github',
                    summary=instance.github_repo_url,
                    description="Repository already connected to this instance.",
                    source='system',
                )
        except Exception:
            _logger.exception("Could not seed history for instance %s", instance.id)
