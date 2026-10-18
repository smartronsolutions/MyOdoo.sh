"""Storage is now live — drop the hourly storage check cron.

The storage values are recomputed on the portal page load and right after every
backup / image change (``_refresh_storage_usage``), so the hourly
``check_storage`` cron was removed from the manifest and is deleted here.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

OLD_CRON_NAMES = (
    'Check Instance Storage',
    'Kontrollo ruajtjen e instancës',
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Cron = env['ir.cron'].sudo()
    cron = env.ref('saas_storage_management.ir_cron_check_storage', raise_if_not_found=False)
    crones = cron | Cron.search([('cron_name', 'in', list(OLD_CRON_NAMES))])
    if crones:
        _logger.info("Removing storage cron(s): %s", crones.mapped('cron_name'))
        crones.unlink()
