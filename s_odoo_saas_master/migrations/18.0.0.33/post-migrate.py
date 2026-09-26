# Superseded by 18.0.0.34 (which also fixes the backup directory and seeds history).
# Kept so upgrading from an older version runs without error.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Retention is now 5 backups per instance.
    env['res.company'].search([('instance_backup_limit', '=', 7)]).write({'instance_backup_limit': 5})
    env['saas.odoo.instance'].search([('backup_limit', '=', 7)]).write({'backup_limit': 5})
