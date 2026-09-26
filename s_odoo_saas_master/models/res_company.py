import os

from odoo import fields, models, api


class Company(models.Model):
    _inherit = 'res.company'

    instance_starting_port = fields.Integer(string='Default Instance Starting Port', required=True, default=9000,
        help="Default starting port of odoo instance when create physical server")
    instance_backup_limit = fields.Integer(string='Default Instance Backup Limit', required=True, default=5,
        help="Set to zero to be no limit. New backups beyond this limit delete the oldest ones.")
    backup_directory = fields.Char(string='Backup Directory',
        help="Directory where instance backups are stored. Leave empty to use an 'instance-backups' "
             "folder inside the Odoo data directory, which is always writable by the Odoo process. "
             "Do not use a path that only exists inside the containers (e.g. /var/lib/odoo).")
    instance_trial_day = fields.Integer(string='Default Trial Day', required=True, default=15)
    notification_expiration_day = fields.Integer(string='Notification Expiration Day', required=True, default=5)
    revoke_instance_day = fields.Integer(string='Revoke Odoo Instance Day', required=True, default=15)
    limit_trial = fields.Integer(string='Maximum Trial per Customer', required=True, default=1)
    default_ssh_username = fields.Char(string='Default SSH Username', default='root', help='Default SSH username for new physical servers.')
    resource_package_id = fields.Many2one('saas.resource.package', string='Default Resource Package')
    github_client_id = fields.Char(string='GitHub OAuth Client ID')
    github_client_secret = fields.Char(string='GitHub OAuth Client Secret')
    saas_price_currency_id = fields.Many2one(
        'res.currency', string='SaaS Price Currency',
        help="OPTIONAL. Leave empty (default) to use the product sale price as-is, in "
             "the company currency. Only set a currency here when the plan / worker / "
             "storage sale prices are entered in another currency and must be converted "
             "into the company currency before being shown or billed.")
    saas_price_rate = fields.Float(
        string='SaaS Price Rate', digits=(16, 6), default=119.3317,
        help="Only used when a SaaS Price Currency is set. Number of units of that "
             "currency that equal one unit of the company currency. Example: company "
             "currency EUR and SaaS price currency XPF, 1 EUR = 119.3317 XPF.")

    def _saas_display_currency(self):
        """Currency in which SaaS prices must be shown / billed."""
        self.ensure_one()
        return self.currency_id

    def _saas_convert_price(self, amount, from_currency=None, date=None):
        """Convert ``amount`` into the company currency, when a conversion is configured.

        By default no conversion happens: the product sale price is used as-is and
        simply displayed in the company currency. If the company sets a *SaaS Price
        Currency* (e.g. XPF) then the amount is converted into the company currency
        (e.g. EUR) using ``saas_price_rate`` (or Odoo's rates for other currencies).

        :param amount: amount expressed in the SaaS price currency.
        :param from_currency: override the source currency (e.g. a pricelist
            currency); defaults to ``saas_price_currency_id``.
        :param date: conversion date, defaults to today.
        :return: the amount, rounded in the company currency.
        """
        self.ensure_one()
        if amount is None or amount is False:
            return amount
        target = self.currency_id
        base = self.saas_price_currency_id
        if not target or not base or base == target:
            # Conversion disabled: prices are already in the company currency.
            return amount
        source = from_currency or base
        if not source or source == target:
            return amount
        # Prices entered in the configured SaaS currency use the configured rate.
        if source == base and self.saas_price_rate:
            return target.round(float(amount) / self.saas_price_rate)
        # Anything else goes through Odoo's multi-currency rates.
        return source._convert(
            float(amount), target, self, date or fields.Date.context_today(self))

    @api.model
    def _generate_saas_price_list(self):
        companies = self.env['res.company'].search([])
        default_price_list = self.env['product.pricelist'].sudo().with_context(active_test=False).search(
            [('company_id', 'in', companies.ids)]
        )
        price_list_item_vals_list = []
        for price_list in default_price_list:
            # monthly
            price_list_item_vals_list.append({
                'pricelist_id': price_list.id,
                'base': 'list_price',
                'applied_on': '3_global',
                'price_discount': 0,
                'min_quantity': 0,
                'compute_price': 'formula',
                'subscription_type': 'monthly'
            })
            # yearly
            price_list_item_vals_list.append({
                'pricelist_id': price_list.id,
                'base': 'list_price',
                'applied_on': '3_global',
                'price_discount': 15,
                'min_quantity': 0,
                'compute_price': 'formula',
                'subscription_type': 'yearly'
            })
        self.env['product.pricelist.item'].create(price_list_item_vals_list)
