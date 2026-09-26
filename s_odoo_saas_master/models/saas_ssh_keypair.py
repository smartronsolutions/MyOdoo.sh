from odoo import fields, models, api


class SSHKeypair(models.Model):
    _name = 'saas.ssh.keypair'
    _description = "SaaS SSH Key Pair"

    name = fields.Char(string='Name', required=True)
    type = fields.Selection([('rsa', 'RSA')], string='Type', required=True, default='rsa')
    active = fields.Boolean(string="Active", default=True)
    public_key_id = fields.Many2one('ir.attachment', string='Public Key')
    private_key_id = fields.Many2one('ir.attachment', string='Private Key')
    private_key_data = fields.Binary(string="Private Key File", attachment=False)
    private_key_filename = fields.Char(string="Private Key Filename", default="id_rsa")
    can_edit_private_key = fields.Boolean(string="Can Edit Private Key", compute="_compute_can_edit_private_key")

    @api.model_create_multi
    def create(self, vals_list):
        """Override create to handle key data and create attachments."""
        for vals in vals_list:
            if vals.get('private_key_data'):
                attachment_vals = {
                    'name': vals.get('private_key_filename', 'id_rsa'),
                    'type': 'binary',
                    'datas': vals['private_key_data'],
                    'res_model': self._name,
                    'res_id': 0,  # Will be updated after creation
                    'mimetype': 'application/octet-stream',
                }
                attachment = self.env['ir.attachment'].sudo().create(attachment_vals)
                vals['private_key_id'] = attachment.id
                # Don't store binary data directly in the record
                vals.pop('private_key_data', None)
                vals.pop('private_key_filename', None)

        return super(SSHKeypair, self).create(vals_list)
    
    def write(self, vals):
        """Override write to handle key data and create attachments."""
        for record in self:
            if vals.get('private_key_data'):
                filename = vals.get('private_key_filename', 'id_rsa')
                if record.private_key_id:
                    # Update existing attachment
                    record.private_key_id.sudo().write({
                        'name': filename,
                        'datas': vals['private_key_data'],
                    })
                else:
                    # Create new attachment
                    attachment_vals = {
                        'name': filename, 
                        'type': 'binary',
                        'datas': vals['private_key_data'],
                        'res_model': self._name,
                        'res_id': record.id,
                        'mimetype': 'application/octet-stream',
                    }
                    attachment = self.env['ir.attachment'].sudo().create(attachment_vals)
                    vals['private_key_id'] = attachment.id
                    
                # Don't store binary data directly in the record
                vals.pop('private_key_data', None)
                vals.pop('private_key_filename', None)
                    
        return super(SSHKeypair, self).write(vals)
    
    def _compute_can_edit_private_key(self):
        """Check if the current user can edit private key."""
        is_saas_master = self.env.user.has_group('s_odoo_saas_master.group_odoo_saas_master')
        for record in self:
            record.can_edit_private_key = is_saas_master
