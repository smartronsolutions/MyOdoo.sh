/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import publicWidget from "@web/legacy/js/public/public_widget";
import PortalSidebar from "@portal/js/portal_sidebar";
import { SaaSBlockUI } from "../components/saas_block_ui";
import { rpc } from '@web/core/network/rpc';

const mainComponentRegistry = registry.category("main_components");

publicWidget.registry.SaaSPortalSidebar = PortalSidebar.extend({
	selector: 'div.o_portal_instance_sidebar',
	events: {
        'click .o_instance_start': '_startInstance',
		'click .o_instance_stop': '_stopInstance',
		'click .o_instance_restart': '_restartInstance',
		'click .o_instance_create_backup': '_createBackup',
		'click .o_instance_download_backup': '_downloadBackup',
		'click .o_instance_add_custom_domain': '_addInstanceDomainName',
		'click .o_instance_domain_name_remove': '_removeInstanceDomainName',
		'click .o_instance_get_app': '_getAppInstance',
    },
	
	init() {
        this._super(...arguments);
        this.rpc = rpc;
		this.notification = this.bindService("notification");
    },
	
	start: async function () {
		this._super.apply(this, arguments);
		const needToDeploy = this.$('#o_need_to_deploy').data('need_to_deploy');
		if (needToDeploy == 'on') {
			$(document).ready(async () => {
				const instanceId = this.$('#o_instance_name').data('instance_id');
	            this.blockUI('Your Instance is initializing...');
				try {
		            await this.rpc('/saas/instance/deploy', {instance_id: instanceId});
		            this.unblockUI();
					window.location.reload();
		        } catch (e) {
		            this.unblockUI();
		            console.log(e.message);
		        }
	        });	
		}		
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _startInstance(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		this.blockUI('Starting your Instance...');
		try {
            await this.rpc('/saas/instance/start', {instance_id: instanceId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _stopInstance(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		this.blockUI('Stopping your Instance...');
		try {
            await this.rpc('/saas/instance/stop', {instance_id: instanceId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _restartInstance(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		this.blockUI('Restarting your Instance...');
		try {
            await this.rpc('/saas/instance/restart', {instance_id: instanceId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _createBackup(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		this.blockUI('Backup your Instance...');
		try {
            await this.rpc('/saas/instance/create-backup', {instance_id: instanceId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _downloadBackup(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		var backupId = parseInt($link.data('backup_id'));
		this.blockUI('Backup your Instance...');
		try {
            await this.rpc('/saas/instance/download-backup', {instance_id: instanceId, backup_id: backupId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _getAppInstance(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		this.blockUI('Getting your Apps and Active Workers...');
		try {
            await this.rpc('/saas/instance/get-app-and-user', {instance_id: instanceId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	async _checkDomain() {
        var domainName = this.$('input#domain_name').val();
        if (/^\d/.test(domainName)){
			this.notification.add(_t("Your domain name cannot start with a number."), { type: 'warning', sticky: true });
			return false;
		}
        if (!domainName) {
			this.notification.add(_t("You have to enter domain name."), { type: 'warning', sticky: true });
            return false;
        } else if (!/^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$/g.test(domainName)) {
            this.notification.add(_t("Your domain name can only contains characters from 'a' to 'z', '0' to '9' and '-'."), { type: 'warning', sticky: true });
            return false;
        }
        var result = await this.rpc('/saas/instance/check-domain-name', {domain_name: domainName});
        if (result.error) {
            this.notification.add(result.error, { type: 'warning', sticky: true });
            return false;
        }
        return true;
    },
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _addInstanceDomainName(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var instanceId = parseInt($link.data('instance_id'));
		var checkResult = await this._checkDomain();
        if (!checkResult) {
            return;
        }
		var domainName = this.$('input#domain_name').val();
		this.blockUI('Your domain name being added...');
		try {
            await this.rpc('/saas/instance/add-domain-name', {instance_id: instanceId, domain_name: domainName});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	/**
     * @private
     * @param {MouseEvent} ev
     */
	async _removeInstanceDomainName(ev) {
		ev.preventDefault();
        ev.stopPropagation();
		var $link = $(ev.currentTarget);
        var domainNameId = parseInt($link.data('domain_name_id'));
		this.blockUI('Your domain name being removed...');
		try {
            await this.rpc('/saas/instance/remove-domain-name', {domain_name_id: domainNameId});
            this.unblockUI();
            window.location.reload();
        } catch (e) {
            this.unblockUI();
            console.log(e.message);
        }
	},
	
	blockUI(message) {
        mainComponentRegistry.add(
            "SaaSBlockUI",
            {
                Component: SaaSBlockUI,
                props: {
                    message,
                },
            },
            { force: true }
        );
    },

    unblockUI() {
        mainComponentRegistry.remove("SaaSBlockUI");
    }
})
