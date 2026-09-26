from odoo import fields, models, _
from odoo.exceptions import ValidationError


class OdooInstanceCancelWizard(models.Model):
    _name = 'saas.odoo.instance.cancel.wizard'
    _description = "Odoo Instance Cancel Wizard"

    instance_id = fields.Many2one('saas.odoo.instance', string='Odoo Instance', required=True, ondelete='cascade')
    confirmation = fields.Char(string='Confirmation', required=True)

    def action_cancel(self):
        if self.confirmation == 'yes':
            self.instance_id._action_cancel()
        else:
            raise ValidationError(_("Please enter 'yes' in the confirmation box before canceling the instance"))
