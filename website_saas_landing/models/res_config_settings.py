# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    saas_enquiry_recipient_email = fields.Char(
        string='SaaS Enquiry Recipient Email',
        config_parameter='website_saas_landing.enquiry_recipient_email',
        help="Email address that receives a notification for every enquiry "
             "submitted from the website. Leave empty to use the company "
             "email address.",
    )
