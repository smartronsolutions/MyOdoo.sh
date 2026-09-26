from . import models
from . import wizard
from . import controllers


def _price_list_post_init_hook(env):
    env['res.company']._generate_saas_price_list()
    # Create the instance-backup folders right after install. They are created by the
    # Odoo process itself, so ownership and permissions are automatically correct.
    env['saas.odoo.instance']._ensure_backup_directories()
