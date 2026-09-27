from . import controllers
from . import models

from odoo.addons.payment import setup_provider, reset_payment_provider


def post_init_hook(env):
    """Register the EpayNC provider-specific accounting payment method.

    `setup_provider` triggers `payment.provider._setup_provider`, which (with
    `account_payment` installed) creates the matching `account.payment.method`
    and the journal payment method line required to post payments.
    """
    setup_provider(env, 'epaync')


def uninstall_hook(env):
    """Remove the EpayNC provider-specific accounting data on uninstall."""
    reset_payment_provider(env, 'epaync')
