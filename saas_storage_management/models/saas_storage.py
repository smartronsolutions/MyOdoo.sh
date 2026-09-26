from odoo import api, models, fields, _
from odoo.exceptions import ValidationError, UserError
import os
import logging

_logger = logging.getLogger(__name__)


class SaasOdooInstance(models.Model):
    _inherit = 'saas.odoo.instance'

    # Storage fields
    storage_limit_gb = fields.Float(
        string='Storage Limit (GB)',
        default=5.0,
        help='Maximum storage allowed for this instance in GB. Default: 5 GB for Standard, 20 GB for Growth. Leave empty or 0 for unlimited.'
    )

    @api.model
    def _prepare_instance_val_to_create(self, data):
        res = super()._prepare_instance_val_to_create(data)

        # 1. Direct explicit storage passed in data
        if data.get('storage_limit_gb'):
            res['storage_limit_gb'] = float(data.get('storage_limit_gb'))
            return res
        if data.get('storage_gb'):
            res['storage_limit_gb'] = float(data.get('storage_gb'))
            return res

        # 2. Extract plan and extra storage from order or data
        plan = data.get('plan') or data.get('plan_name')
        extra_storage_gb = float(data.get('extra_storage_gb') or 0.0)

        sub_domain = data.get('sub_domain')
        if sub_domain:
            order = self.env['sale.order'].search([('subdomain', '=', sub_domain)], limit=1)
            if order:
                if hasattr(order, 'storage_limit_gb') and order.storage_limit_gb > 0:
                    res['storage_limit_gb'] = float(order.storage_limit_gb)
                    return res
                for line in order.order_line:
                    code = (line.product_id.default_code or '').lower()
                    name = (line.product_id.name or '').lower()
                    if not plan:
                        if 'growth' in code or 'growth' in name:
                            plan = 'Growth'
                        elif 'standard' in code or 'standard' in name or 'essential' in code or 'essential' in name:
                            plan = 'Standard'
                    if code == 'saas_extra_storage' or 'extra storage' in name:
                        extra_storage_gb += float(line.product_uom_qty or 0.0)

        base_storage = 20.0 if (plan and 'growth' in str(plan).lower()) else 5.0
        res['storage_limit_gb'] = base_storage + extra_storage_gb
        return res
    
    # Stored (not recomputed on every read): filesystem scanning is expensive, so the
    # value only changes when the hourly cron (check_storage) explicitly writes it -
    # portal/backend reads hit the DB column directly instead of walking the filesystem
    # on every page view.
    storage_used_gb = fields.Float(
        string='Storage Used (GB)',
        compute='_compute_storage_used',
        store=True
    )

    storage_percentage = fields.Float(
        string='Storage Usage %',
        compute='_compute_storage_percentage',
        store=True
    )

    storage_status = fields.Selection([
        ('ok', 'OK'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
        ('full', 'Full'),
        ('unlimited', 'Unlimited'),
    ], string='Storage Status', compute='_compute_storage_status', store=True)

    storage_is_unlimited = fields.Boolean(
        string='Unlimited Storage',
        compute='_compute_storage_is_unlimited',
        store=False
    )
    
    storage_last_check = fields.Datetime(string='Last Check')
    storage_notification_sent = fields.Boolean(default=False, help='Warning email (80%+) already sent for the current fill cycle.')
    storage_full_action_taken = fields.Boolean(default=False, help='Instance already auto-stopped and notified for the current full (100%+) cycle.')
    
    storage_upgrade_requested = fields.Boolean(default=False)
    storage_upgrade_requested_gb = fields.Float()
    storage_upgrade_request_date = fields.Datetime()
    storage_upgrade_request_reason = fields.Text()
    
    # Relationship to storage history
    storage_history_ids = fields.One2many('saas.storage.history', 'instance_id', string='Storage History', readonly=True)

    def _compute_storage_used(self):
        """Compute storage used - only runs once per record (no depends), the hourly
        check_storage() cron is what keeps this field's stored value up to date."""
        for record in self:
            try:
                record.storage_used_gb = round(self._calculate_filestore_size(record), 2)
            except Exception as e:
                _logger.error(f"[STORAGE] Error computing storage for {record.name}: {str(e)}")
                record.storage_used_gb = 0.0

    @api.depends('storage_used_gb', 'storage_limit_gb', 'storage_is_unlimited')
    def _compute_storage_percentage(self):
        """Compute storage percentage"""
        for record in self:
            if record.storage_is_unlimited or not record.storage_limit_gb or record.storage_limit_gb == 0:
                record.storage_percentage = 0.0
            else:
                record.storage_percentage = round((record.storage_used_gb / record.storage_limit_gb) * 100, 1)

    @api.depends('storage_limit_gb')
    def _compute_storage_is_unlimited(self):
        """Check if storage is unlimited"""
        for record in self:
            record.storage_is_unlimited = not record.storage_limit_gb or record.storage_limit_gb == 0

    @api.depends('storage_percentage', 'storage_is_unlimited')
    def _compute_storage_status(self):
        """Compute storage status"""
        for record in self:
            if record.storage_is_unlimited:
                record.storage_status = 'unlimited'
            elif record.storage_percentage >= 100:
                record.storage_status = 'full'
            elif record.storage_percentage >= 90:
                record.storage_status = 'critical'
            elif record.storage_percentage >= 80:
                record.storage_status = 'warning'
            else:
                record.storage_status = 'ok'

    def _calculate_filestore_size(self, instance):
        """
        Calculate total size of /home/{technical_name}/ directory in GB
        
        Path structure:
        /home/{technical_name}/
        ├── Dockerfile
        ├── config/
        ├── custom-addons/
        ├── docker-compose.yml
        ├── odoo-web-data/
        └── pgdata/
        """
        try:
            # Use technical_name for path
            if not instance.technical_name:
                _logger.warning(f"[STORAGE] No technical_name for instance {instance.name}")
                return 0.0
            
            instance_path = f"/home/{instance.technical_name}"
            
            _logger.info(f"[STORAGE] Starting calculation for {instance.name} at {instance_path}")
            
            # Check if path exists locally
            if not os.path.exists(instance_path):
                # If local path doesn't exist, try via remote server SSH if configured
                if hasattr(instance, 'server_id') and instance.server_id:
                    try:
                        ssh = instance.server_id._open_ssh_connection()
                        stdin, stdout, stderr = ssh.exec_command(f"du -sb {instance_path} 2>/dev/null")
                        out = stdout.read().decode('utf-8').strip()
                        ssh.close()
                        if out:
                            bytes_size = int(out.split()[0])
                            size_gb = bytes_size / (1024 ** 3)
                            _logger.info(f"[STORAGE] SSH du for {instance.name}: {bytes_size} bytes = {size_gb:.4f} GB")
                            return size_gb
                    except Exception as ssh_ex:
                        _logger.warning(f"[STORAGE] SSH du check failed for {instance.name}: {str(ssh_ex)}")
                
                _logger.warning(f"[STORAGE] Path not found: {instance_path}")
                return 0.0
            
            # Check if path is readable
            if not os.access(instance_path, os.R_OK):
                _logger.warning(f"[STORAGE] No read permission for: {instance_path}")
                return 0.0
            
            total_size = 0
            file_count = 0
            error_count = 0
            
            # Walk through all directories and files
            try:
                for dirpath, dirnames, filenames in os.walk(instance_path):
                    for filename in filenames:
                        filepath = os.path.join(dirpath, filename)
                        try:
                            if os.path.isfile(filepath) and os.path.exists(filepath):
                                file_size = os.path.getsize(filepath)
                                total_size += file_size
                                file_count += 1
                        except (OSError, IOError):
                            error_count += 1
                            continue
            except Exception as e:
                _logger.error(f"[STORAGE] Error walking directory {instance_path}: {str(e)}")
            
            size_gb = total_size / (1024 ** 3)
            _logger.info(f"[STORAGE] {instance.name}: {total_size} bytes ({file_count} files, {error_count} errors) = {size_gb:.4f} GB")
            return size_gb
            
        except Exception as e:
            _logger.error(f"[STORAGE] Error calculating storage for {instance.name}: {str(e)}")
            return 0.0

    def check_storage(self):
        """Check storage, create history record, notify and auto-stop as thresholds are crossed"""
        for record in self:
            try:
                # Calculate storage - writing storage_used_gb cascades (via @api.depends) to
                # recompute + store storage_percentage and storage_status automatically.
                size_gb = self._calculate_filestore_size(record)
                record.storage_used_gb = round(size_gb, 2)
                record.storage_last_check = fields.Datetime.now()

                status = record.storage_status

                # Create history record
                self.env['saas.storage.history'].create({
                    'instance_id': record.id,
                    'storage_used_gb': record.storage_used_gb,
                    'storage_limit_gb': record.storage_limit_gb,
                    'storage_percentage': record.storage_percentage,
                    'storage_status': status,
                })

                _logger.info(f"[STORAGE] Updated instance {record.name}: {record.storage_used_gb:.2f}GB ({record.storage_percentage:.1f}%)")

                record._handle_storage_thresholds(status)

            except Exception as e:
                _logger.error(f"[STORAGE] Error checking storage for {record.name}: {str(e)}")

    def _handle_storage_thresholds(self, status):
        """Send warning/full notifications and auto-stop the instance once storage is full.

        Uses storage_notification_sent / storage_full_action_taken as one-shot flags so the
        hourly cron doesn't resend emails or re-trigger action_stop every run - they reset
        once usage drops back below the threshold (e.g. after cleanup or a limit increase).
        """
        self.ensure_one()

        if status in ('warning', 'critical'):
            if not self.storage_notification_sent:
                self._send_storage_warning_email()
                self.storage_notification_sent = True
        elif status in ('ok', 'unlimited'):
            if self.storage_notification_sent:
                self.storage_notification_sent = False
            if self.storage_full_action_taken:
                self.storage_full_action_taken = False

        if status == 'full':
            if not self.storage_full_action_taken:
                self.storage_notification_sent = True
                self._send_storage_full_email()
                try:
                    # Sets state=suspend + suspension_reason=storage_full so the portal
                    # locks the lifecycle actions and offers a storage upgrade.
                    self._suspend_for_storage_full()
                    _logger.info(f"[STORAGE] Instance {self.name} suspended: storage full")
                except Exception as e:
                    _logger.error(f"[STORAGE] Error suspending {self.name}: {str(e)}")
                self.storage_full_action_taken = True
        elif self.storage_full_action_taken:
            # Usage dropped back under 100% (still >= 80%, kept in 'warning'/'critical')
            self.storage_full_action_taken = False

    def _send_storage_warning_email(self):
        """Send the 80%+ storage warning email using the storage_warning_mail_template"""
        self.ensure_one()
        if not self.partner_id.email:
            _logger.warning(f"[STORAGE] No email for customer {self.partner_id.name}, skipping warning email")
            return
        template = self.env.ref('saas_storage_management.storage_warning_mail_template', raise_if_not_found=False)
        if template:
            template.sudo().send_mail(self.id, force_send=True)
            _logger.info(f"[STORAGE] Warning email sent for {self.name} ({self.storage_percentage:.1f}%)")

    def _send_storage_full_email(self):
        """Send the 100% storage-full / instance-stopped email using the storage_full_mail_template"""
        self.ensure_one()
        if not self.partner_id.email:
            _logger.warning(f"[STORAGE] No email for customer {self.partner_id.name}, skipping full-storage email")
            return
        template = self.env.ref('saas_storage_management.storage_full_mail_template', raise_if_not_found=False)
        if template:
            template.sudo().send_mail(self.id, force_send=True)
            _logger.info(f"[STORAGE] Full-storage email sent for {self.name}")

    def request_upgrade(self, new_limit_gb):
        """Client requests storage upgrade - returns status dict"""
        for record in self:
            try:
                # Validate new limit
                if not new_limit_gb or new_limit_gb <= 0:
                    return {
                        'status': 'error',
                        'message': 'Storage limit must be greater than 0'
                    }
                
                # Check if new limit is not greater than current limit
                if not record.storage_is_unlimited and new_limit_gb <= record.storage_limit_gb:
                    current_limit = record.storage_limit_gb
                    return {
                        'status': 'error',
                        'message': f'You already have {current_limit} GB. Please request a limit above {current_limit} GB.'
                    }
                
                # Check if there's already a pending request
                if record.storage_upgrade_requested:
                    pending_limit = record.storage_upgrade_requested_gb
                    return {
                        'status': 'error',
                        'message': f'You already have a pending upgrade request for {pending_limit} GB. Support will contact you soon.'
                    }
                
                # Create upgrade request
                record.storage_upgrade_requested = True
                record.storage_upgrade_requested_gb = new_limit_gb
                record.storage_upgrade_request_date = fields.Datetime.now()
                
                _logger.info(f"[STORAGE] Upgrade request for {record.name}: {record.storage_limit_gb}GB → {new_limit_gb}GB")
                
                # Send email to admin and log an Activity on the instance so it shows up
                # in the backend Activities view / chatter for follow-up.
                self._send_upgrade_request_email(record, new_limit_gb)
                record._schedule_storage_upgrade_activity(new_limit_gb)

                # Return success message
                return {
                    'status': 'success',
                    'message': f'Your request for {new_limit_gb} GB has been submitted. Support will contact you soon via email or phone. Thank you!'
                }
                
            except Exception as e:
                _logger.error(f"[STORAGE] Error requesting upgrade for {record.name}: {str(e)}")
                return {
                    'status': 'error',
                    'message': f'An error occurred: {str(e)}'
                }

    def _get_storage_admin_users(self):
        """SaaS Manager group users, falling back to the super admin (uid 2)"""
        admin_users = self.env['res.users'].search([('groups_id.name', 'ilike', 'SaaS Manager')])
        if not admin_users:
            admin_users = self.env['res.users'].search([('id', '=', 2)])
        return admin_users

    def _schedule_storage_upgrade_activity(self, new_limit_gb):
        """Log a To-Do activity on the instance so admins see the request in Odoo's
        Activities view / chatter, in addition to the notification email."""
        self.ensure_one()
        admin_users = self._get_storage_admin_users()
        if not admin_users:
            _logger.warning("[STORAGE] No admin users found to assign upgrade request activity")
            return
        self.activity_schedule(
            act_type_xmlid='mail.mail_activity_data_todo',
            summary=_('Storage upgrade requested: %s GB') % new_limit_gb,
            note=_(
                '%(partner)s requested a storage increase for instance %(instance)s: '
                '%(current)s GB → %(requested)s GB.'
            ) % {
                'partner': self.partner_id.name,
                'instance': self.name,
                'current': self.storage_limit_gb,
                'requested': new_limit_gb,
            },
            user_id=admin_users[0].id,
        )

    def _send_upgrade_request_email(self, instance, new_limit_gb):
        """Send email notification to admin about upgrade request"""
        try:
            # Get admin users
            admin_users = self._get_storage_admin_users()

            if not admin_users:
                _logger.warning(f"[STORAGE] No admin users found to send upgrade request email")
                return

            # Prepare email content
            subject = f"Storage Upgrade Request - {instance.name}"
            body = f"""
            <p>Storage upgrade request received:</p>
            <ul>
                <li><strong>Instance:</strong> {instance.name}</li>
                <li><strong>Current Limit:</strong> {instance.storage_limit_gb} GB</li>
                <li><strong>Current Usage:</strong> {instance.storage_used_gb:.2f} GB ({instance.storage_percentage:.1f}%)</li>
                <li><strong>Requested Limit:</strong> {new_limit_gb} GB</li>
                <li><strong>Request Date:</strong> {instance.storage_upgrade_request_date}</li>
                <li><strong>Customer:</strong> {instance.partner_id.name}</li>
            </ul>
            <p><a href="/web#id={instance.id}&model=saas.odoo.instance&view_type=form">View Instance</a></p>
            """
            
            # Send email
            mail_values = {
                'subject': subject,
                'body_html': body,
                'email_from': self.env.user.email,
                'email_to': ','.join([user.email for user in admin_users if user.email]),
            }
            
            mail = self.env['mail.mail'].create(mail_values)
            mail.send()
            
            _logger.info(f"[STORAGE] Upgrade request email sent for {instance.name}")
            
        except Exception as e:
            _logger.error(f"[STORAGE] Error sending upgrade request email: {str(e)}")

    def approve_upgrade(self):
        """Admin approves upgrade"""
        for record in self:
            try:
                if not record.storage_upgrade_requested:
                    raise UserError('No upgrade request for this instance')

                old_limit = record.storage_limit_gb
                new_limit = record.storage_upgrade_requested_gb

                # Update storage limit (cascades: storage_percentage/storage_status recompute automatically)
                record.storage_limit_gb = new_limit
                record.storage_upgrade_requested = False
                record.storage_upgrade_requested_gb = 0

                # New limit should clear a previous full/warning cycle - the next cron run
                # will re-raise them if usage is still high relative to the new limit.
                record.storage_notification_sent = False
                record.storage_full_action_taken = False

                _logger.info(f"[STORAGE] Upgrade approved for {record.name}: {old_limit}GB → {new_limit}GB")

                # If the instance was suspended because its storage was full, resume it
                # now that there is room. Any other suspension reason is left untouched.
                if record.suspension_reason == 'storage_full':
                    if record._resume_after_storage_upgrade():
                        _logger.info(f"[STORAGE] Instance {record.name} restarted after storage upgrade approval")
                elif record.state == 'deploy' and record.operation_state == 'stop':
                    try:
                        record.action_start()
                        _logger.info(f"[STORAGE] Instance {record.name} restarted after storage upgrade approval")
                    except Exception as e:
                        _logger.error(f"[STORAGE] Error restarting {record.name} after upgrade approval: {str(e)}")

                # Send approval email to customer (partner_id.email from this saas.odoo.instance record)
                record._send_storage_upgrade_approved_email()

            except Exception as e:
                _logger.error(f"[STORAGE] Error approving upgrade for {record.name}: {str(e)}")
                raise

    def reject_upgrade(self):
        """Admin rejects upgrade"""
        for record in self:
            try:
                if not record.storage_upgrade_requested:
                    raise UserError('No upgrade request for this instance')
                
                requested_limit = record.storage_upgrade_requested_gb
                
                # Reject request
                record.storage_upgrade_requested = False
                record.storage_upgrade_requested_gb = 0
                
                _logger.info(f"[STORAGE] Upgrade rejected for {record.name}: {requested_limit}GB")
                
                # Send rejection email to customer
                self._send_rejection_email(record, requested_limit)
                
            except Exception as e:
                _logger.error(f"[STORAGE] Error rejecting upgrade for {record.name}: {str(e)}")
                raise

    def _send_storage_upgrade_approved_email(self):
        """Send the new-storage-limit approval email using storage_upgrade_approved_mail_template.

        email_to is resolved from partner_id.email on this saas.odoo.instance record.
        """
        self.ensure_one()
        if not self.partner_id.email:
            _logger.warning(f"[STORAGE] No email for customer {self.partner_id.name}, skipping approval email")
            return
        template = self.env.ref('saas_storage_management.storage_upgrade_approved_mail_template', raise_if_not_found=False)
        if template:
            template.sudo().send_mail(self.id, force_send=True)
            _logger.info(f"[STORAGE] Approval email sent for {self.name} (new limit: {self.storage_limit_gb} GB)")

    def _send_rejection_email(self, instance, requested_limit):
        """Send rejection email to customer"""
        try:
            customer_email = instance.partner_id.email
            if not customer_email:
                _logger.warning(f"[STORAGE] No email for customer {instance.partner_id.name}")
                return
            
            subject = f"Storage Upgrade Request Rejected - {instance.name}"
            body = f"""
            <p>Your storage upgrade request has been rejected.</p>
            <ul>
                <li><strong>Instance:</strong> {instance.name}</li>
                <li><strong>Requested Limit:</strong> {requested_limit} GB</li>
                <li><strong>Current Limit:</strong> {instance.storage_limit_gb} GB</li>
                <li><strong>Rejection Date:</strong> {fields.Datetime.now()}</li>
            </ul>
            <p>Please contact support for more information.</p>
            """
            
            mail_values = {
                'subject': subject,
                'body_html': body,
                'email_from': self.env.user.email,
                'email_to': customer_email,
            }
            
            mail = self.env['mail.mail'].create(mail_values)
            mail.send()
            
            _logger.info(f"[STORAGE] Rejection email sent to {customer_email}")
            
        except Exception as e:
            _logger.error(f"[STORAGE] Error sending rejection email: {str(e)}")


class SaasStorageHistory(models.Model):
    _name = 'saas.storage.history'
    _description = 'Storage Check History'
    _order = 'check_date desc'

    instance_id = fields.Many2one('saas.odoo.instance', string='Instance', ondelete='cascade')
    check_date = fields.Datetime(string='Check Date', default=fields.Datetime.now)
    storage_used_gb = fields.Float(string='Storage Used (GB)')
    storage_limit_gb = fields.Float(string='Storage Limit (GB)')
    storage_percentage = fields.Float(string='Storage Usage %')
    storage_status = fields.Selection([
        ('ok', 'OK'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
        ('full', 'Full'),
        ('unlimited', 'Unlimited'),
    ], string='Storage Status')
