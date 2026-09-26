/** @odoo-module **/

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillUnmount, useState } from "@odoo/owl";

export class SaasBackupRestoreProgress extends Component {
    static template = "s_odoo_saas_master.SaasBackupRestoreProgress";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            message: this.props.record.data[this.props.name],
            refreshing: false,
        });
        this.pollTimer = null;
        this._schedulePoll();
        onWillUnmount(() => this._clearPoll());
    }

    _clearPoll() {
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
    }

    _schedulePoll() {
        this._clearPoll();
        if (this.props.record.data.restore_state === "running") {
            this.pollTimer = setTimeout(() => this._poll(), 3000);
        }
    }

    async _poll() {
        try {
            await this.refresh();
        } catch (e) {
            console.warn("Restore progress poll failed, will retry", e);
        } finally {
            this._schedulePoll();
        }
    }

    async refresh() {
        if (!this.props.record.resId) {
            return;
        }
        this.state.refreshing = true;
        try {
            if (this.props.record.load) {
                await this.props.record.load();
                this.state.message = this.props.record.data[this.props.name];
            } else {
                const [values] = await this.orm.read(this.props.record.resModel, [this.props.record.resId], [
                    "restore_state",
                    "restore_status_message",
                ]);
                this.state.message = values.restore_status_message;
                await this.props.record.update({
                    restore_state: values.restore_state,
                    restore_status_message: values.restore_status_message,
                });
            }
        } finally {
            this.state.refreshing = false;
        }
    }

    async onRefreshClick() {
        this._clearPoll();
        await this.refresh();
        this._schedulePoll();
    }
}

registry.category("fields").add("saas_backup_restore_progress", {
    component: SaasBackupRestoreProgress,
});
