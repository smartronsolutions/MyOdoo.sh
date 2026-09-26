from odoo import fields, models, _
from odoo.exceptions import UserError


class InstanceDuplicateWizard(models.TransientModel):
    _name = "saas.odoo.instance.duplicate.wizard"
    _description = "SaaS Odoo Instance Duplicate Wizard"

    instance_id = fields.Many2one(
        "saas.odoo.instance", string="Original Instance",
        required=True, ondelete="cascade", readonly=True,
    )
    current_subdomain = fields.Char(
        string="Current Subdomain", readonly=True,
        related="instance_id.name",
    )
    based_domain_id = fields.Many2one(
        "saas.based.domain", string="Based Domain",
        required=True, readonly=True,
        related="instance_id.based_domain_id",
    )
    new_subdomain = fields.Char(
        string="New Subdomain", required=True,
    )

    def action_duplicate(self):
        self.ensure_one()

        if not self.new_subdomain:
            raise UserError(_("Please enter a new subdomain name."))

        if self.new_subdomain[0].isdigit():
            raise UserError(_("Subdomain cannot start with a number."))

        # Check if the new subdomain already exists on this based domain
        existing = self.env["saas.odoo.instance"].search_count([
            ("name", "=", self.new_subdomain),
            ("based_domain_id", "=", self.based_domain_id.id),
        ])
        if existing:
            raise UserError(_(
                'The subdomain "%s" already exists for domain "%s". '
                "Please choose a different name."
            ) % (self.new_subdomain, self.based_domain_id.name))

        # Duplicate with the new subdomain -- full clone from original
        new_instance = self.instance_id.copy(default={
            "name": self.new_subdomain,
            "state": "draft",
            "operation_state": "draft",
            "use_template": True,
            "template_instance_id": self.instance_id.id,
        })

        return {
            "name": _("Duplicated Instance"),
            "type": "ir.actions.act_window",
            "res_model": "saas.odoo.instance",
            "view_mode": "form",
            "res_id": new_instance.id,
            "target": "current",
        }
