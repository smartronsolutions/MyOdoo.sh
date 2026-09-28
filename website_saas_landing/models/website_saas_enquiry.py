# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import re

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

ENQUIRY_SEQUENCE_CODE = 'website.saas.enquiry'
NOTIFICATION_EMAIL_PARAM = 'website_saas_landing.enquiry_recipient_email'
NOTIFICATION_FROM_PARAM = 'website_saas_landing.enquiry_from_email'

# Address used when the Settings values are left empty. It is the sender bound
# to the default outgoing mail server (``from_filter``), so Odoo automatically
# routes enquiry emails through that server.
DEFAULT_NOTIFICATION_EMAIL = 'notifications@myodoo.sh'

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Requirement options, shared with the mirrored CRM lead field.
INTERESTED_IN_SELECTION = [
    ('implementation', 'New Odoo Implementation'),
    ('hosting', 'Odoo Hosting / MyOdoo.sh'),
    ('migration', 'Odoo Migration'),
    ('customisation', 'Odoo Customisation'),
    ('module_development', 'Custom Module Development'),
    ('integration', 'Integration / API'),
    ('backup_recovery', 'Backup & Recovery'),
    ('support_maintenance', 'Support & Maintenance'),
    ('existing_project', 'Existing Odoo Project'),
    ('other', 'Other'),
]


class WebsiteSaasEnquiry(models.Model):
    """Enquiry submitted from the public website (Contact Us / SaaS pages).

    Records are always created by the public controller; only internal users
    have access to the backend list, kanban and form views.
    """

    _name = 'website.saas.enquiry'
    _description = 'Website SaaS Enquiry'
    # The backend form view embeds a <chatter/>, which relies on the
    # mail.thread API (`_get_thread_with_access`, `message_ids`, ...).
    # Without this mixin the frontend /mail/thread/data route raises
    # AttributeError: 'website.saas.enquiry' object has no attribute
    # '_get_thread_with_access'.
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'submission_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Enquiry Number', required=True, copy=False, readonly=True,
        default=lambda self: _('New'),
    )
    submission_date = fields.Datetime(
        string='Submitted On', default=fields.Datetime.now, readonly=True, copy=False,
    )

    # Contact details
    full_name = fields.Char(string='Full Name', required=True)
    company_name = fields.Char(string='Company Name', required=True)
    email = fields.Char(string='Email', required=True)
    phone = fields.Char(string='Phone / WhatsApp')
    country = fields.Char(string='Country')

    # Requirement
    interested_in = fields.Selection(
        selection=INTERESTED_IN_SELECTION,
        string='Interested In', required=True, default='implementation',
    )
    odoo_version = fields.Char(string='Current Odoo Version')
    estimated_users = fields.Char(string='Estimated Users')
    message = fields.Text(string='Project Details', required=True)

    # Origin
    source_page = fields.Char(string='Source Page', readonly=True)
    lang = fields.Char(string='Language', readonly=True)
    company_id = fields.Many2one(
        'res.company', string='Company', index=True,
        default=lambda self: self.env.company,
    )

    # Follow-up
    state = fields.Selection(
        selection=[
            ('new', 'New'),
            ('contacted', 'Contacted'),
            ('qualified', 'Qualified'),
            ('in_progress', 'In Progress'),
            ('won', 'Won'),
            ('closed', 'Closed'),
        ],
        string='Status', default='new', required=True, group_expand='_group_expand_states',
    )
    user_id = fields.Many2one('res.users', string='Assigned To', index=True)
    internal_notes = fields.Text(string='Internal Notes')

    # CRM mirror (one lead is created automatically per enquiry)
    crm_lead_ids = fields.One2many('crm.lead', 'saas_enquiry_id', string='CRM Leads')
    crm_lead_count = fields.Integer(string='CRM Leads', compute='_compute_crm_lead_count')

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    ENQUIRY_SEQUENCE_CODE
                ) or _('New')
        enquiries = super().create(vals_list)
        # Mirror every new enquiry into CRM so the sales team can follow it up.
        for enquiry in enquiries:
            enquiry._create_crm_lead()
        return enquiries

    @api.model
    def _group_expand_states(self, states, domain, order=None):
        return [key for key, _label in self._fields['state'].selection]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_notification_email(self):
        """Return the address that receives new-enquiry notifications.

        Configured from Settings; defaults to ``notifications@myodoo.sh``.
        """
        self_sudo = self.sudo()
        recipient = self_sudo.env['ir.config_parameter'].sudo().get_param(
            NOTIFICATION_EMAIL_PARAM, ''
        )
        recipient = (recipient or '').strip()
        if recipient and EMAIL_RE.match(recipient):
            return recipient
        return DEFAULT_NOTIFICATION_EMAIL

    @api.model
    def _get_from_email(self):
        """Return the From address used for enquiry emails.

        This is the address bound to the default outgoing mail server
        (``from_filter``), which is what makes Odoo route the emails through
        that server. No explicit ``mail_server_id`` is set anywhere.
        """
        self_sudo = self.sudo()
        from_email = self_sudo.env['ir.config_parameter'].sudo().get_param(
            NOTIFICATION_FROM_PARAM, ''
        )
        from_email = (from_email or '').strip()
        if from_email and EMAIL_RE.match(from_email):
            return from_email
        return DEFAULT_NOTIFICATION_EMAIL

    @api.model
    def normalise_email(self, email):
        """Return a normalised email address or an empty string when invalid."""
        email = (email or '').strip()
        return email if EMAIL_RE.match(email) else ''

    # ------------------------------------------------------------------
    # CRM
    # ------------------------------------------------------------------
    def _prepare_crm_lead_values(self):
        """Build the ``crm.lead`` values mirroring this enquiry."""
        self.ensure_one()
        return {
            'name': _('SaaS Enquiry: %s (%s)') % (
                self.company_name or self.full_name, self.name
            ),
            'contact_name': self.full_name,
            'partner_name': self.company_name,
            'email_from': self.email,
            'phone': self.phone,
            'description': self.message,
            'saas_enquiry_id': self.id,
            'saas_interested_in': self.interested_in,
            'saas_odoo_version': self.odoo_version,
            'saas_estimated_users': self.estimated_users,
            'saas_country': self.country,
            'saas_source_page': self.source_page,
            'saas_lang': self.lang,
        }

    def _create_crm_lead(self):
        """Create the CRM lead for this enquiry (idempotent).

        Never raises: the visitor flow must keep working even if CRM is not
        reachable, so failures are only logged.
        """
        self.ensure_one()
        Lead = self.env['crm.lead'].sudo()
        existing = Lead.search([('saas_enquiry_id', '=', self.id)], limit=1)
        if existing:
            return existing
        try:
            return Lead.create(self._prepare_crm_lead_values())
        except Exception:  # noqa: BLE001 - never break the visitor flow
            _logger.exception(
                "SaaS enquiry %s: CRM lead could not be created.", self.name
            )
            return Lead.browse()

    @api.depends('crm_lead_ids')
    def _compute_crm_lead_count(self):
        for enquiry in self:
            enquiry.crm_lead_count = len(enquiry.crm_lead_ids)

    def action_open_crm_lead(self):
        self.ensure_one()
        lead = self.crm_lead_ids[:1]
        return {
            'type': 'ir.actions.act_window',
            'name': _('CRM Lead'),
            'res_model': 'crm.lead',
            'res_id': lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _notify_new_enquiry(self):
        """Send the internal notification and the customer confirmation."""
        self.ensure_one()
        from_email = self._get_from_email()
        # Internal notification
        recipient = self._get_notification_email()
        if recipient:
            template = self.env.ref(
                'website_saas_landing.mail_template_enquiry_internal',
                raise_if_not_found=False,
            )
            if template:
                try:
                    template.send_mail(
                        self.id,
                        email_values={
                            'email_to': recipient,
                            'email_from': from_email,
                            # Let the team reply straight to the visitor.
                            'reply_to': self.email or from_email,
                        },
                        force_send=False,
                    )
                except Exception:  # noqa: BLE001 - never break the visitor flow
                    _logger.exception(
                        "SaaS enquiry %s: internal notification could not be queued.",
                        self.name,
                    )
        else:
            _logger.warning(
                "SaaS enquiry %s: no notification address configured "
                "(Settings > Website > SaaS Enquiry Recipient Email).",
                self.name,
            )

        # Customer confirmation
        confirmation = self.env.ref(
            'website_saas_landing.mail_template_enquiry_customer',
            raise_if_not_found=False,
        )
        if confirmation and self.email:
            try:
                confirmation.send_mail(
                    self.id,
                    email_values={'email_from': from_email},
                    force_send=False,
                )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "SaaS enquiry %s: customer confirmation could not be queued.",
                    self.name,
                )
