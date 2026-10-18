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

        # Every plan includes the same base container: 2 workers + 5 GB of storage
        # (Standard and Growth are sold at the same 36 / month base price).
        base_storage = 5.0
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

    storage_folder_gb = fields.Float(
        string='Instance Usage (GB)', readonly=True, copy=False,
        help="Total size the instance itself uses: database, filestore, custom addons, "
             "backups and logs — every file the instance wrote. Measured with `du` as "
             "root over the server connection when the storage cron runs.")

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
                usage_gb = self._get_instance_usage_gb(record)
                image_gb = self._get_image_size_gb(record)
                record.storage_folder_gb = round(usage_gb, 2)
                record.storage_used_gb = round(usage_gb + image_gb, 2)
            except Exception as e:
                _logger.error(f"[STORAGE] Error computing storage for {record.name}: {str(e)}")
                record.storage_folder_gb = 0.0
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

    def _get_backups_size_gb(self, instance):
        """Storage taken by the backups of this instance, in GB.

        Backups are written outside the instance folder (the master's
        ``instance-backups`` directory and its ``container`` sub-folder), so ``du`` on
        the instance folder never saw them. Both backup tables are summed here:
        ``saas.odoo.instance.backup`` (database + filestore) and
        ``saas.odoo.instance.container.backup`` (whole container).

        The real file size is used when the file can be read locally; otherwise the
        measured ``file_size`` stored on the record is used (it is in decimal MB).
        """
        technical = instance.technical_name or ''
        total_bytes = 0
        for model in ('saas.odoo.instance.backup',
                      'saas.odoo.instance.container.backup'):
            try:
                records = self.env[model].sudo().search([('instance_id', '=', instance.id)])
            except Exception as error:
                _logger.warning("[STORAGE] Cannot read %s of %s: %s", model, instance.name, error)
                continue
            for record in records:
                path = record.file_path or ''
                # A backup stored inside the instance folder is already counted by `du`.
                if technical and ('/home/%s/' % technical) in path:
                    continue
                size = 0
                if path:
                    try:
                        if os.path.isfile(path):
                            size = os.path.getsize(path)
                    except OSError:
                        size = 0
                if not size:
                    # ``file_size`` is stored in MB (bytes / 1e6) when the backup was made.
                    size = int(float(record.file_size or 0.0) * 1000 * 1000)
                total_bytes += size
        return round(total_bytes / (1000 ** 3), 4)

    def _get_image_size_gb(self, instance):
        """Storage taken by the instance's private Docker image, in GB.

        Python libraries and system packages are baked into a private image
        (``odoo:<version>-custom-<technical_name>``) that lives in
        ``/var/lib/docker/overlay2``, outside ``/home/<technical_name>``. Its real size
        is recorded on the ``saas.docker.image`` record after every build, so it is
        added here: rebuilding the image with extra packages grows the customer's
        storage usage exactly like a bigger database would.
        """
        try:
            image = self.env['saas.docker.image'].sudo().search(
                [('instance_id', '=', instance.id)], limit=1)
            return round((image.size_gb or 0.0), 2) if image else 0.0
        except Exception as error:
            _logger.warning(
                "[STORAGE] Could not read the image size of %s: %s", instance.name, error)
            return 0.0

    def _get_instance_usage_gb(self, instance):
        """Everything the instance itself uses, in GB: folder + its backups.

        This is what the portal shows as **Instance Usage**: the whole instance folder
        (database, filestore, addons, logs) plus every backup file that was written for
        this instance outside the folder.
        """
        folder_gb = self._calculate_folder_size(instance) or 0.0
        backups_gb = self._get_backups_size_gb(instance) or 0.0
        return round(folder_gb + backups_gb, 4)

    def _calculate_filestore_size(self, instance):
        """Total storage of an instance, in GB: folder + backups + private image."""
        usage_gb = self._get_instance_usage_gb(instance)
        image_gb = self._get_image_size_gb(instance)
        return round(usage_gb + image_gb, 4)

    def _ssh_folder_size_gb(self, instance):
        """Total size of the instance folder over SSH, in GB (``None`` when no SSH).

        The Odoo process cannot read ``pgdata/`` (owned by postgres, mode 0700) nor the
        Docker volume, so walking the folder locally only counted a few kilobytes and
        every instance looked empty. ``du`` through the instance server connection runs
        as root and really sees the database, the filestore and the backups.
        """
        if not instance.pserver_id or not instance.technical_name:
            return None
        folder = '/home/%s' % instance.technical_name
        paths = [folder]
        # A named volume that is NOT bind-mounted inside the folder is extra space; when
        # ``odoo-web-data`` lives inside the folder ``du`` already counted it.
        if not os.path.exists(os.path.join(folder, 'odoo-web-data')):
            paths.append('/var/lib/docker/volumes/%s_odoo-web-data/_data'
                         % instance.technical_name)
        command = 'du -sb %s 2>/dev/null || true' % ' '.join(paths)
        try:
            ssh = instance.pserver_id.sudo()._connect()
            try:
                _exit_code, output = instance.pserver_id._exec_capture(command, ssh)
            finally:
                ssh.close()
        except Exception as error:
            _logger.warning(
                "[STORAGE] SSH du failed for %s: %s", instance.name, error)
            return None
        total = 0
        for line in (output or '').splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                total += int(parts[0])
        if not total:
            return None
        return round(total / (1024 ** 3), 4)

    def _calculate_folder_size(self, instance):
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
        ssh_gb = self._ssh_folder_size_gb(instance)
        if ssh_gb is not None:
            return ssh_gb
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

    def _refresh_storage_usage(self):
        """Recompute the stored storage values **right now** (no cron, no waiting).

        Called from the portal page and after every change that can move the numbers
        (new/deleted backup, image rebuild, staging copy), so the customer always reads
        live values. Also enforces the storage limits instantly: once an instance reaches
        100% of its quota it is stopped on the spot, which the (now disabled) hourly cron
        used to do.
        """
        for record in self:
            try:
                usage_gb = self._get_instance_usage_gb(record)
                image_gb = self._get_image_size_gb(record)
                record.storage_folder_gb = round(usage_gb, 2)
                record.storage_used_gb = round(usage_gb + image_gb, 2)
                record.storage_last_check = fields.Datetime.now()
            except Exception as error:
                _logger.error(
                    "[STORAGE] Live refresh failed for %s: %s", record.name, error)
                continue
            # 100% reached: stop the instance immediately instead of waiting for a cron.
            try:
                if ((record.storage_percentage or 0.0) >= 100.0
                        and record.state == 'deploy'
                        and record.operation_state == 'run'):
                    record._suspend_for_storage_full()
            except Exception as error:
                _logger.warning(
                    "[STORAGE] Auto-stop at 100%% failed for %s: %s", record.name, error)
        return True

    def check_storage(self):
        """Check storage, create history record, notify and auto-stop as thresholds are crossed"""
        for record in self:
            try:
                # Calculate storage - writing storage_used_gb cascades (via @api.depends) to
                # recompute + store storage_percentage and storage_status automatically.
                # Instance usage = the whole instance folder (one `du`) + every backup file;
                # the total adds the private Docker image of Python libs / system packages.
                usage_gb = self._get_instance_usage_gb(record)
                image_gb = self._get_image_size_gb(record)
                record.storage_folder_gb = round(usage_gb, 2)
                record.storage_used_gb = round(usage_gb + image_gb, 2)
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
