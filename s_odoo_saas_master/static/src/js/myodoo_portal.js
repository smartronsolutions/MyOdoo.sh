/** @odoo-module **/

import publicWidget from "@web/legacy/js/public/public_widget";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import SaaSAuth from "./saas_auth_guard";

function notify(message) {
    let toast = document.getElementById("toast");
    if (!toast) {
        toast = document.createElement("div");
        toast.className = "toast";
        toast.id = "toast";
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(window.toastTimer);
    window.toastTimer = setTimeout(() => toast.classList.remove("show"), 3000);
}

// Restarting the containers takes only a few seconds, so the portal counts down 30 seconds
// and then confirms the instance is running again. The countdown is just the prediction the
// user reads; the polls below keep checking afterwards, so a slower restart still resolves
// to "running" instead of leaving a stale banner behind.
const INSTANCE_RESTART_WINDOW_MS = 30 * 1000;
const INSTANCE_RESTART_POLL_MS = 2000;
const INSTANCE_RESTART_MAX_POLLS = 30;

const STATUS_BANNER_COPY = {
    running: {
        className: "is-running",
        icon: "fa-check-circle",
        title: "Your instance is running",
        sub: "All services are online and reachable.",
    },
    suspended: {
        className: "is-suspended",
        icon: "fa-pause-circle",
        title: "Your instance is suspended",
        sub: "Public access and background workers are stopped. Start the instance to bring it back online.",
    },
    restarting: {
        className: "is-restarting",
        icon: "fa-refresh",
        title: "Your instance is restarting",
        sub: "It will take about 30 seconds.",
    },
};

// Instance shell polling. The terminal used to repaint on a fixed 700 ms tick, which is
// what made typing feel laggy: a keystroke could wait most of a second before it showed
// up. Polls now start fast, back off while nothing happens, and fire right after a write.
const SHELL_POLL_FAST_MS = 160;
const SHELL_POLL_IDLE_MS = 1200;
const SHELL_RESIZE_MIN_COLS = 40;
const SHELL_RESIZE_MAX_COLS = 500;
const SHELL_RESIZE_MIN_ROWS = 10;
const SHELL_RESIZE_MAX_ROWS = 200;
// A write that fails must never swallow what was typed: the characters are put back on the
// queue and retried after this delay (instead of hammering a server that is already unwell).
const SHELL_WRITE_RETRY_MS = 500;

publicWidget.registry.MyOdooPortal = publicWidget.Widget.extend({
    selector: ".page, .myodoo-portal-root",

    init() {
        this._super(...arguments);
        this.rpc = rpc;
    },

    start() {
        this._super.apply(this, arguments);
        // The portal templates wrap `main.page` inside `.myodoo-portal-root`, so the selector
        // above matches BOTH elements and this widget used to start twice on the same page:
        // every handler was bound twice (a key typed in the shell was sent twice, actions
        // fired twice). Initialise once per portal root; the widget only works on document,
        // so which of the two matching elements wins does not matter.
        const host = this.el.closest(".myodoo-portal-root") || this.el;
        if (host.dataset.myodooPortalReady) {
            return;
        }
        host.dataset.myodooPortalReady = "1";
        // Removed instance: the whole page is blurred behind the "Your instance is
        // removed" overlay, so nothing must be wired and no poll started on an
        // instance whose containers no longer exist.
        const removedFlag = document.getElementById("o_instance_removed");
        if (removedFlag && removedFlag.value === "1") {
            return;
        }
        this._initTabs();
        this._initActions();
        this._initModals();
        this._initInstanceStatusBanner();
        this._initSearch();
        this._initSettings();
        this._initMails();
        this._initPricing();
        this._initRenewModal();
        this._initStorageUpgrade();
        this._initWorkersUpgrade();
        this._initPostPaymentFlow();
        this._initDeploymentFlow();
        this._initGithubIntegration();
        this._initGithubLogs();
        this._initAutoBackupToggle();
        this._initBackupResume();
        this._initVersionSelector();
        this._startLogsAutoRefresh();
        this._fetchLiveLogsIfVisible();
        this._initInstanceShell();
        this._initInstanceAdminPassword();

        if (location.hash) {
            const targetTab = document.querySelector('[data-tab="' + location.hash.slice(1) + '"]');
            if (targetTab) {
                targetTab.click();
            }
        }
    },

    _getInstanceId() {
        const el = document.getElementById("o_instance_id") || document.querySelector("[data-instance-id]");
        if (el) {
            return parseInt(el.dataset.instanceId || el.value);
        }
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get("id")) {
            return parseInt(urlParams.get("id"));
        }
        return false;
    },

    _initTabs() {
        document.querySelectorAll("[data-tab]").forEach(button => {
            button.addEventListener("click", () => {
                document.querySelectorAll("[data-tab]").forEach(item => item.classList.remove("active"));
                document.querySelectorAll(".tab-pane").forEach(item => item.classList.remove("active"));
                button.classList.add("active");
                const targetPane = document.getElementById(button.dataset.tab);
                if (targetPane) {
                    targetPane.classList.add("active");
                }
                if (button.dataset.tab === "logs") {
                    this._fetchLiveLogs();
                }
                this._rememberTab(button.dataset.tab);
            });
        });

        document.querySelectorAll("[data-tab-jump]").forEach(button => {
            button.addEventListener("click", () => {
                const target = document.querySelector('[data-tab="' + button.dataset.tabJump + '"]');
                if (target) {
                    target.click();
                    window.scrollTo({ top: 0, behavior: "smooth" });
                }
            });
        });

        // A refresh (soft or hard) and back/forward must land on the tab that was open, so
        // the current tab is written to the URL hash *and* to localStorage (the hash covers
        // F5, localStorage covers opening the page again without a hash).
        this._restoreTab();
        window.addEventListener("hashchange", () => this._restoreTab());
    },

    _rememberTab(name) {
        if (!name) {
            return;
        }
        try {
            window.localStorage.setItem("saasPortalTab-" + location.pathname, name);
        } catch (err) {
            // Private mode / storage disabled: the hash alone still survives a reload.
        }
        if (("#" + name) !== location.hash) {
            history.replaceState(null, "", "#" + name);
        }
    },

    _restoreTab() {
        let name = (location.hash || "").slice(1);
        if (!name) {
            try {
                name = window.localStorage.getItem("saasPortalTab-" + location.pathname) || "";
            } catch (err) {
                name = "";
            }
        }
        if (!name) {
            return;
        }
        const button = document.querySelector('[data-tab="' + name + '"]');
        if (button && !button.classList.contains("active")) {
            button.click();
        }
    },

    _initActions() {
        document.querySelectorAll("[data-action]").forEach(button => {
            // Guard so the handler is not bound twice when the table is re-rendered.
            if (button.dataset.actionBound) return;
            button.dataset.actionBound = "1";
            button.addEventListener("click", async (e) => {
                e.preventDefault();
                const action = button.dataset.action;
                const instanceId = this._getInstanceId();

                if (action === "github") {
                    const ghModal = document.getElementById("githubModal");
                    if (ghModal) {
                        ghModal.classList.add("show");
                    }
                } else if (action === "backup") {
                    if (!instanceId) {
                        notify(_t("No instance selected."));
                        return;
                    }
                    await this._runBackupFlow();
                } else if (action === "delete-backup") {
                    await this._deleteBackup(button.dataset.backupId, button.dataset.backupName, button);
                } else if (action === "rebuild") {
                    if (!instanceId) {
                        notify(_t("No instance selected."));
                        return;
                    }
                    notify(_t("Redeploying latest Git revision..."));
                    try {
                        const res = await this.rpc("/saas/instance/redeploy", { instance_id: instanceId });
                        if (res && res.success !== false) {
                            notify(_t("Redeployment triggered successfully."));
                        } else {
                            notify("Redeploy failed: " + (res?.error || "Error"));
                        }
                    } catch (err) {
                        notify("Redeploy error: " + err.message);
                    }
                } else if (action === "open") {
                    const url = button.dataset.url || document.getElementById("instanceUrl")?.value;
                    if (url) {
                        window.open(url, "_blank");
                    } else {
                        notify(_t("Instance URL not available."));
                    }
                } else if (action === "renew") {
                    const renewModal = document.getElementById("renewInstanceModal");
                    if (renewModal) {
                        renewModal.classList.add("show");
                    }
                } else if (action === "add-domain") {
                    const input = document.getElementById("domainInput");
                    const domainName = input ? input.value.trim().toLowerCase() : "";
                    if (!domainName) {
                        notify(_t("Enter a domain name first."));
                        return;
                    }
                    if (!instanceId) {
                        notify(_t("No instance selected."));
                        return;
                    }
                    // Step by step: DNS check -> nginx -> SSL, in a small dialog.
                    const overlay = this._ghEl("div");
                    overlay.setAttribute("style",
                        "position:fixed;inset:0;z-index:150;background:rgba(9,32,36,.45);"
                        + "display:flex;align-items:center;justify-content:center;padding:16px");
                    const card = this._ghEl("div");
                    card.setAttribute("style", "width:100%;max-width:520px;background:#fff;"
                        + "border-radius:16px;padding:26px;box-shadow:0 20px 50px rgba(9,32,36,.3)");
                    const spin = this._ghEl("style");
                    spin.textContent = "@keyframes saasSpin{to{transform:rotate(360deg)}}"
                        + ".saas-spin{display:inline-block;animation:saasSpin 1s linear infinite}";
                    card.appendChild(spin);
                    const stepRow = (icon, title, text, dim) =>
                        '<div class="domain-step" style="display:flex;gap:10px;align-items:flex-start;'
                        + 'font-size:12px;padding:9px 0;border-top:1px solid #eef3f4;'
                        + (dim ? "opacity:.35" : "") + '">'
                        + '<span style="font-size:14px">' + icon + '</span><span><b>' + title + '</b>'
                        + '<div style="color:#5b6b73;margin-top:3px">' + text + '</div></span></div>';
                    card.innerHTML =
                        '<div style="font-size:17px;font-weight:800;color:#0d3a44;margin-bottom:6px">'
                        + 'Setting up ' + domainName + '</div>'
                        + '<div style="font-size:12px;line-height:1.6;color:#5b6b73">Keep this window open — '
                        + 'we verify your DNS, configure the web server and then issue the SSL certificate.</div>'
                        + '<div id="dnsVerifyRow">'
                        + stepRow("&#8987;", "Verifying your domain",
                                  "Checking that " + domainName + " points to our server&hellip;") + '</div>'
                        + '<div id="dnsSetupRow">'
                        + stepRow("&#8987;", "Setting your domain",
                                  "Configuring nginx for " + domainName + "&hellip;", true) + '</div>'
                        + '<div id="dnsSslRow">'
                        + stepRow("&#8987;", "Issuing SSL certificate",
                                  "Let&rsquo;s Encrypt is issuing HTTPS for " + domainName + "&hellip;", true)
                        + '</div>'
                        + '<div id="dnsResult" style="font-size:12px;line-height:1.6;margin-top:14px"></div>'
                        + '<div style="display:flex;justify-content:flex-end;margin-top:18px">'
                        + '<button id="dnsClose" type="button" style="padding:10px 18px;'
                        + 'border-radius:10px;border:1px solid #dfe7ea;background:#fff;color:#0d3a44;'
                        + 'font-size:12px;font-weight:700;cursor:pointer">Close</button></div>';
                    overlay.appendChild(card);
                    document.body.appendChild(overlay);
                    const closeDialog = () => overlay.remove();
                    document.getElementById("dnsClose").addEventListener("click", closeDialog);
                    overlay.addEventListener("click", (ev) => { if (ev.target === overlay) closeDialog(); });
                    const setRow = (id, icon, dim) => {
                        const el = document.getElementById(id);
                        if (!el) return;
                        el.style.opacity = dim ? ".35" : "1";
                        const mark = el.querySelector("span");
                        if (mark) {
                            mark.innerHTML = icon === "SPIN"
                                ? '<span class="saas-spin">&#8635;</span>'
                                : icon;
                        }
                    };
                    const showResult = (html, isError) => {
                        const box = document.getElementById("dnsResult");
                        if (box) {
                            box.innerHTML = html;
                            box.style.color = isError ? "#a9483e" : "#168652";
                        }
                    };
                    try {
                        setRow("dnsVerifyRow", "SPIN", false);
                        const check = await this.rpc("/saas/instance/verify-domain",
                            { instance_id: instanceId, domain_name: domainName });
                        if (!check || !check.success) {
                            setRow("dnsVerifyRow", "&#10006;");
                            showResult((check && check.error)
                                || "We could not verify this domain.", true);
                            return;
                        }
                        setRow("dnsVerifyRow", "&#10004;");
                        setRow("dnsSetupRow", "SPIN", false);
                        await new Promise((done) => setTimeout(done, 450));
                        const taken = await this.rpc("/saas/instance/check-domain-name",
                            { domain_name: domainName });
                        if (taken && taken.success === false) {
                            setRow("dnsSetupRow", "&#10006;");
                            showResult(taken.error || "This domain is already in use.", true);
                            return;
                        }
                        const added = await this.rpc("/saas/instance/add-domain-name",
                            { instance_id: instanceId, domain_name: domainName });
                        if (added && added.success === false) {
                            setRow("dnsSetupRow", "&#10006;");
                            showResult(added.error || "Could not add the domain.", true);
                            return;
                        }
                        setRow("dnsSetupRow", "&#10004;");
                        setRow("dnsSslRow", "SPIN", false);
                        showResult("Domain added. Let&rsquo;s Encrypt is issuing your certificate — "
                                   + "this usually takes a few seconds.");
                        // Keep polling until the certificate really exists on the server.
                        let issued = false;
                        for (let attempt = 0; attempt < 36 && !issued; attempt += 1) {
                            await new Promise((done) => setTimeout(done, 2500));
                            try {
                                const status = await this.rpc("/saas/instance/domain-status",
                                    { instance_id: instanceId, domain_name: domainName });
                                if (status && (status.ssl || status.https === 301 || status.https === 200)) {
                                    issued = true;
                                }
                            } catch (err) {
                                // a failed poll is not fatal, the next one retries
                            }
                        }
                        if (issued) {
                            setRow("dnsSslRow", "&#10004;");
                            showResult("<b>" + domainName + " is ready over HTTPS.</b> The domain and its "
                                       + "SSL certificate are installed — your instance answers on "
                                       + "https://" + domainName);
                        } else {
                            setRow("dnsSslRow", "&#8987;", false);
                            showResult("The domain is added. The certificate is still being issued in "
                                       + "the background — the Domains tab shows SSL Active as soon as "
                                       + "it is ready (usually under a minute).");
                        }
                        setTimeout(() => {
                            closeDialog();
                            window.location.reload();
                        }, issued ? 3500 : 6000);
                    } catch (err) {
                        const message = (err && (err.data && err.data.message || err.message))
                            || err || "The request failed.";
                        showResult("We could not finish the setup: " + message
                            + " Please try again in a moment.", true);
                    }
                } else if (action === "remove-domain") {
                    const domainId = button.dataset.domainId
                        || button.getAttribute("data-domain-id")
                        || button.closest("[data-domain-id]")?.getAttribute("data-domain-id");
                    if (!domainId) {
                        button.closest(".option")?.remove();
                        notify(_t("Domain removed."));
                        return;
                    }
                    const domainLabel = (button.closest(".option")?.querySelector("strong")?.textContent
                        || "this domain").trim();
                    const overlay = this._ghEl("div");
                    overlay.setAttribute("style",
                        "position:fixed;inset:0;z-index:150;background:rgba(9,32,36,.45);"
                        + "display:flex;align-items:center;justify-content:center;padding:16px");
                    const card = this._ghEl("div");
                    card.setAttribute("style", "width:100%;max-width:470px;background:#fff;"
                        + "border-radius:16px;padding:24px;box-shadow:0 20px 50px rgba(9,32,36,.3)");
                    card.innerHTML =
                        '<div style="font-size:17px;font-weight:800;color:#0d3a44;margin-bottom:8px">'
                        + 'Remove ' + domainLabel + '?</div>'
                        + '<div style="font-size:12px;line-height:1.7;color:#5b6b73">This deletes the domain '
                        + 'together with its <b>nginx configuration</b> and its <b>SSL certificate</b>. '
                        + 'Adding it back later starts again from the DNS setup.</div>'
                        + '<div id="removeDomainStatus" style="font-size:12px;color:#5b6b73;margin-top:12px"></div>'
                        + '<div style="display:flex;gap:12px;justify-content:flex-end;margin-top:22px">'
                        + '<button id="removeDomainNo" type="button" style="padding:11px 20px;'
                        + 'border-radius:11px;border:1px solid #dfe7ea;background:#fff;color:#0d3a44;'
                        + 'font-size:13px;font-weight:700;cursor:pointer">No, keep it</button>'
                        + '<button id="removeDomainYes" type="button" style="padding:11px 20px;'
                        + 'border-radius:11px;border:1px solid transparent;background:#c25146;'
                        + 'color:#fff;font-size:13px;font-weight:700;cursor:pointer">Yes, remove</button>'
                        + '</div>';
                    overlay.appendChild(card);
                    document.body.appendChild(overlay);
                    const closeDialog = () => overlay.remove();
                    document.getElementById("removeDomainNo").addEventListener("click", closeDialog);
                    overlay.addEventListener("click", (ev) => { if (ev.target === overlay) closeDialog(); });
                    document.getElementById("removeDomainYes").addEventListener("click", async () => {
                        const statusEl = document.getElementById("removeDomainStatus");
                        const yesBtn = document.getElementById("removeDomainYes");
                        yesBtn.disabled = true;
                        if (statusEl) statusEl.textContent =
                            "Removing the domain, its nginx configuration and its SSL certificate…";
                        try {
                            const res = await this.rpc("/saas/instance/remove-domain-name",
                                { domain_name_id: parseInt(domainId) });
                            const ok = res === true || (res && res.success);
                            if (ok) {
                                if (statusEl) {
                                    statusEl.style.color = "#168652";
                                    statusEl.textContent = "Done — domain, nginx configuration and SSL certificate removed.";
                                }
                                button.closest(".option")?.remove();
                                notify(_t("Domain removed (nginx config + SSL deleted)."));
                                setTimeout(() => { closeDialog(); window.location.reload(); }, 1500);
                            } else {
                                yesBtn.disabled = false;
                                if (statusEl) {
                                    statusEl.style.color = "#a9483e";
                                    statusEl.textContent = (res && res.error) || "Could not remove the domain.";
                                }
                            }
                        } catch (err) {
                            yesBtn.disabled = false;
                            if (statusEl) {
                                statusEl.style.color = "#a9483e";
                                statusEl.textContent = "Error: " + (err && err.message ? err.message : err);
                            }
                        }
                    });
                } else if (action === "refresh-logs") {
                    this._fetchLiveLogs();
                } else if (action === "download-logs") {
                    await this._downloadLogs();
                } else if (action === "save") {
                    notify(_t("Instance settings saved."));
                } else {
                    notify(button.textContent.trim() + " selected.");
                }
            });
        });
    },

    async _fetchLiveLogs() {
        const consoleEl = document.getElementById("instanceLogs") || document.querySelector(".console");
        const statusEl = document.getElementById("logsStatus");
        const instanceId = this._getInstanceId();
        if (!consoleEl || !instanceId) return;
        if (statusEl) statusEl.textContent = "loading…";
        try {
            const res = await this.rpc("/saas/instance/live-logs", {
                instance_id: instanceId,
                lines: 100,
            });
            const logs = (res && res.logs) || "";
            if (!logs) {
                consoleEl.textContent = "No log output recorded yet.";
            } else {
                // Full log, line by line: keep everything, just colour the notable lines so
                // errors stand out while the rest stays readable.
                const lines = String(logs).replace(/\n$/, "").split("\n");
                consoleEl.textContent = "";
                for (const line of lines) {
                    const span = document.createElement("span");
                    span.textContent = line + "\n";
                    const lower = line.toLowerCase();
                    if (/\b(error|critical|traceback|exception)\b/.test(lower)) {
                        span.style.color = "#ff8a7a";
                    } else if (/\bwarning\b/.test(lower)) {
                        span.style.color = "#ffc24b";
                    } else if (/\b(?:info|werkzeug)\b/.test(lower)) {
                        span.style.color = "#9fc7c2";
                    }
                    consoleEl.appendChild(span);
                }
                consoleEl.scrollTop = consoleEl.scrollHeight;
            }
            if (statusEl) {
                const errorCount = (String(logs).match(/\bERROR\b/g) || []).length;
                statusEl.textContent = "updated " + new Date().toLocaleTimeString() +
                    (errorCount ? " · " + errorCount + " error line(s)" : " · no errors");
            }
        } catch (err) {
            if (statusEl) statusEl.textContent = "failed";
            if (consoleEl) consoleEl.textContent = "Failed to fetch logs: " + (err && err.message ? err.message : err);
        }
    },

    /** Periodically refresh the log view while the Logs tab is open and auto-refresh is on. */
    _startLogsAutoRefresh() {
        if (this._logsTimer) return;
        const tick = () => {
            const toggle = document.getElementById("logsAutoRefresh");
            const pane = document.getElementById("logs");
            const isVisible = pane && pane.classList.contains("active");
            if (isVisible && (!toggle || toggle.checked)) {
                this._fetchLiveLogs();
            }
        };
        this._logsTimer = setInterval(tick, 8000);
        document.getElementById("logsAutoRefresh")?.addEventListener("change", (ev) => {
            if (ev.currentTarget.checked) this._fetchLiveLogs();
        });
    },

    /** Load the Odoo logs as soon as the Logs tab is opened. */
    _fetchLiveLogsIfVisible() {
        const pane = document.getElementById("logs");
        if (pane && pane.classList.contains("active")) {
            this._fetchLiveLogs();
        }
        document.querySelectorAll('[data-tab="logs"]').forEach((btn) => {
            if (btn.dataset.logsBound) return;
            btn.dataset.logsBound = "1";
            btn.addEventListener("click", () => setTimeout(() => this._fetchLiveLogs(), 60));
        });
    },

    /** Download the current Odoo log as a text file. */
    async _downloadLogs() {
        const instanceId = this._getInstanceId();
        if (!instanceId) return;
        try {
            const res = await this.rpc("/saas/instance/live-logs", { instance_id: instanceId, lines: 100 });
            const logs = (res && res.logs) || "";
            if (!logs) {
                notify(_t("No logs available to download yet."));
                return;
            }
            const blob = new Blob([logs], { type: "text/plain;charset=utf-8" });
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = "instance-" + instanceId + "-odoo.log.txt";
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(link.href);
        } catch (err) {
            notify("Could not download logs: " + (err && err.message ? err.message : err));
        }
    },

    /** Paint the backup progress in both the in-page bar and the popup. */
    _paintBackupProgress(percent, message) {
        const clamped = Math.max(0, Math.min(Number(percent) || 0, 100));
        const fill = document.getElementById("backupProgressFill");
        const pct = document.getElementById("backupProgressPct");
        const label = document.getElementById("backupProgressLabel");
        const modalFill = document.getElementById("backupModalProgressFill");
        const modalPct = document.getElementById("backupModalPct");
        if (fill) fill.style.width = clamped + "%";
        if (pct) pct.textContent = Math.round(clamped) + "%";
        if (modalFill) modalFill.style.width = clamped + "%";
        if (modalPct) modalPct.textContent = Math.round(clamped) + "%";
        if (label && message) label.textContent = message;
    },

    /**
     * Start a manual backup and follow its progress.
     *
     * The backup runs in a background thread on the server, so refreshing or closing the
     * page does NOT stop it — on the next load `_initBackupResume()` picks the progress
     * back up and the bar keeps running.
     */
    async _runBackupFlow() {
        const instanceId = this._getInstanceId();
        if (!instanceId) {
            notify(_t("No instance selected."));
            return;
        }
        const wrap = document.getElementById("backupProgressWrap");
        const modal = document.getElementById("backupProgressModal");
        if (wrap) wrap.style.display = "";
        if (modal) modal.classList.add("show");
        this._paintBackupProgress(3, "Starting the backup…");

        try {
            const res = await this.rpc("/saas/instance/create-backup", { instance_id: instanceId });
            if (!res || res.success === false) {
                const message = (res && res.error) || "Could not start the backup.";
                this._paintBackupProgress(100, message);
                notify(message);
                this._hideBackupOverlayLater(3);
                return;
            }
        } catch (err) {
            notify("Backup error: " + (err && err.message ? err.message : err));
            this._hideBackupOverlayLater(3);
            return;
        }
        this._startBackupPolling();
    },

    /** Poll the server until the running backup reaches a final state. */
    _startBackupPolling() {
        if (this._backupTimer) return;
        const instanceId = this._getInstanceId();
        const tick = async () => {
            let status = null;
            try {
                status = await this.rpc("/saas/instance/backup-status", { instance_id: instanceId });
            } catch (err) {
                return; // transient network hiccup: retry on the next tick
            }
            if (!status) return;
            this._paintBackupProgress(status.progress, status.message);
            if (status.state === "done" || status.state === "failed") {
                clearInterval(this._backupTimer);
                this._backupTimer = null;
                const ok = status.state === "done";
                notify(ok
                    ? "Backup completed successfully. It is now in the Backup history."
                    : "Backup failed: " + (status.message || "unknown error"));
                this._renderBackupRows(status.backups);
                // Keep the popup visible for ~3 seconds, then hide it (no page reload).
                this._hideBackupOverlayLater(3);
            }
        };
        tick();
        this._backupTimer = setInterval(tick, 2500);
    },

    /** Hide the popup + progress bar after a few seconds. */
    _hideBackupOverlayLater(seconds = 3) {
        setTimeout(() => {
            const modal = document.getElementById("backupProgressModal");
            const wrap = document.getElementById("backupProgressWrap");
            if (modal) modal.classList.remove("show");
            if (wrap) wrap.style.display = "none";
        }, Math.max(1, seconds) * 1000);
    },

    /** On page load: if a backup is already running, keep showing its progress. */
    _initBackupResume() {
        const data = document.getElementById("backupStateData");
        if (!data || data.dataset.state !== "running") return;
        const wrap = document.getElementById("backupProgressWrap");
        const modal = document.getElementById("backupProgressModal");
        if (wrap) wrap.style.display = "";
        if (modal) modal.classList.add("show");
        this._paintBackupProgress(data.dataset.progress || 0, data.dataset.message || "");
        this._startBackupPolling();
    },

    /** Re-render the backup table from the status payload (no page reload). */
    _renderBackupRows(backups) {
        const body = document.getElementById("backupTableBody");
        if (!body) return;
        if (!backups || !backups.length) {
            body.innerHTML = '<tr><td colspan="6" class="empty-state"><p>No backups created yet. Click above to create your first backup.</p></td></tr>';
            return;
        }
        body.innerHTML = "";
        const addCell = (row, text, className, style) => {
            const cell = document.createElement("td");
            cell.textContent = text;
            if (className) cell.className = className;
            if (style) cell.setAttribute("style", style);
            row.appendChild(cell);
            return cell;
        };
        for (const bk of backups) {
            const tr = document.createElement("tr");
            addCell(tr, bk.date || "");
            addCell(tr, bk.name || "", "mono", "font-size:10px");
            addCell(tr, bk.type_label || "", "status" + (bk.type === "auto" ? " warning" : ""));
            addCell(tr, bk.size || "");
            addCell(tr, "Ready", "status");

            const actions = document.createElement("td");
            actions.className = "backup-actions";
            const link = document.createElement("a");
            link.href = bk.download_url;
            link.className = "ghost";
            link.style.textDecoration = "none";
            link.textContent = "Download";
            const del = document.createElement("button");
            del.type = "button";
            del.className = "ghost backup-delete-btn";
            del.dataset.action = "delete-backup";
            del.dataset.backupId = bk.id;
            del.dataset.backupName = bk.name;
            del.title = "Delete this backup permanently";
            del.textContent = "Delete";
            actions.appendChild(link);
            actions.appendChild(del);
            tr.appendChild(actions);
            body.appendChild(tr);
        }
        this._initActions();
    },

    /** Delete one backup (record + archive file). */
    async _deleteBackup(backupId, name, button) {
        const instanceId = this._getInstanceId();
        if (!instanceId || !backupId) return;
        if (!window.confirm("Delete this backup?\n\n" + (name || "") +
            "\n\nThe archive file is removed permanently. This cannot be undone.")) {
            return;
        }
        if (button) {
            button.disabled = true;
            button.textContent = "Deleting…";
        }
        try {
            const res = await this.rpc("/saas/instance/delete-backup", {
                instance_id: instanceId,
                backup_id: backupId,
            });
            if (res && res.success) {
                notify(_t("Backup deleted."));
                const target = button || document.querySelector('[data-backup-id="' + backupId + '"]');
                const row = target ? target.closest("tr") : null;
                if (row) row.remove();
                // Show the empty state again when the last backup is gone.
                const body = document.getElementById("backupTableBody");
                if (body && !body.querySelector("tr")) {
                    body.innerHTML = '<tr><td colspan="6" class="empty-state"><p>No backups created yet. Click above to create your first backup.</p></td></tr>';
                }
            } else {
                notify((res && res.error) || "Could not delete the backup.");
                if (button) {
                    button.disabled = false;
                    button.textContent = "Delete";
                }
            }
        } catch (err) {
            notify("Could not delete the backup: " + (err && err.message ? err.message : err));
            if (button) {
                button.disabled = false;
                button.textContent = "Delete";
            }
        }
    },

    /** Toggle the per-instance automatic daily backup. */
    _initAutoBackupToggle() {
        // The Backups tab and the Settings tab expose the same instance field
        // (``enable_autobackup``), so both switches are bound to the same route and are
        // mirrored on to each other: turning one on turns the other one on too.
        const toggles = [
            document.getElementById("autoBackupToggle"),
            document.getElementById("settingsAutoBackupToggle"),
        ].filter((el) => el && !el.dataset.bound);

        for (const toggle of toggles) {
            toggle.dataset.bound = "1";
            toggle.addEventListener("change", async (ev) => {
                const instanceId = this._getInstanceId();
                const enabled = ev.currentTarget.checked;
                this._applyAutoBackupState(enabled, ev.currentTarget);
                try {
                    const res = await this.rpc("/saas/instance/toggle-autobackup", {
                        instance_id: instanceId,
                        enabled: enabled,
                    });
                    if (res && res.success) {
                        notify(enabled
                            ? "Automatic daily backups are now enabled."
                            : "Automatic daily backups are now disabled.");
                    } else {
                        this._applyAutoBackupState(!enabled, ev.currentTarget);
                        notify((res && res.error) || "Could not update the auto-backup setting.");
                    }
                } catch (err) {
                    this._applyAutoBackupState(!enabled, ev.currentTarget);
                    notify("Could not update the auto-backup setting: " + (err && err.message ? err.message : err));
                }
            });
        }
    },

    /** Mirror one auto-backup switch onto the other one (Backups tab <-> Settings tab). */
    _applyAutoBackupState(enabled, source) {
        for (const id of ["autoBackupToggle", "settingsAutoBackupToggle"]) {
            const el = document.getElementById(id);
            if (el && el !== source) {
                el.checked = enabled;
            }
        }
        for (const id of ["autoBackupLabel", "settingsAutoBackupLabel"]) {
            const el = document.getElementById(id);
            if (el) {
                el.textContent = enabled ? "On" : "Off";
            }
        }
    },

    /**
     * Pricing card: keep the Edition list in sync with the chosen Odoo version.
     * An edition is only offered when a server of that version + edition exists.
     */
    _initVersionSelector() {
        const versionSelect = document.getElementById("planOdooVersion");
        const typeSelect = document.getElementById("planVersionType");
        const note = document.getElementById("versionTypeNote");
        if (!versionSelect || !typeSelect || versionSelect.dataset.bound) return;
        versionSelect.dataset.bound = "1";

        const syncTypes = () => {
            const option = versionSelect.options[versionSelect.selectedIndex];
            if (!option) return;
            const hasCommunity = option.dataset.community === "1";
            const hasEnterprise = option.dataset.enterprise === "1";
            let available = 0;
            for (const opt of typeSelect.options) {
                const ok = opt.value === "enterprise" ? hasEnterprise : hasCommunity;
                opt.disabled = !ok;
                opt.hidden = !ok;
                if (ok) available += 1;
            }
            // Fall back to an edition that actually has a server for this version.
            if (typeSelect.selectedOptions[0] && typeSelect.selectedOptions[0].disabled) {
                const first = Array.from(typeSelect.options).find(o => !o.disabled);
                if (first) typeSelect.value = first.value;
            }
            if (note) {
                note.textContent = available
                    ? _t("The matching Odoo server is selected and deployed automatically.")
                    : _t("No server is available for this version yet.");
            }
        };

        versionSelect.addEventListener("change", syncTypes);
        syncTypes();
    },

    _initModals() {
        const modal = document.getElementById("confirmModal");
        const confirmTitle = document.getElementById("confirmTitle");
        const confirmText = document.getElementById("confirmText");
        const confirmButton = document.getElementById("confirmAction");
        let pendingAction = "";

        const actionCopy = {
            start: [_t("Start Instance"), _t("Start Odoo, workers and public web services?")],
            suspend: [_t("Stop Instance"), _t("This will stop public access and all background workers until started again.")],
            restart: [_t("Restart Services"), _t("The instance may be briefly unavailable while services reload.")],
            redeploy: [_t("Redeploy Latest Revision"), _t("Pull the latest connected GitHub code and restart the instance?")],
            remove: [
                _t("Remove Instance"),
                _t("Are you sure you want to delete this instance? Its containers, database, "
                   + "files, domain and SSL certificate will be permanently deleted and the "
                   + "instance can no longer be started. This cannot be undone."),
            ],
        };

        // Destructive action: the confirmation buttons must not read "Confirm"/"Cancel"
        // for it, the customer has to make an explicit yes/no choice.
        const setConfirmLabels = (action) => {
            const destructive = action === "remove";
            const cancelButton = document.getElementById("cancelAction");
            if (confirmButton) {
                confirmButton.textContent = destructive
                    ? _t("Yes, remove it")
                    : _t("Confirm");
                confirmButton.classList.toggle("danger", destructive);
            }
            if (cancelButton) {
                cancelButton.textContent = destructive ? _t("No, keep it") : _t("Cancel");
            }
        };

        document.querySelectorAll("[data-confirm]").forEach(button => {
            button.addEventListener("click", () => {
                pendingAction = button.dataset.confirm;
                const copy = actionCopy[pendingAction] || [_t("Confirm Action"), _t("Are you sure you want to proceed?")];
                if (confirmTitle) confirmTitle.textContent = copy[0];
                if (confirmText) confirmText.textContent = copy[1];
                setConfirmLabels(pendingAction);
                if (modal) modal.classList.add("show");
            });
        });

        document.getElementById("cancelAction")?.addEventListener("click", () => {
            if (modal) modal.classList.remove("show");
        });

        confirmButton?.addEventListener("click", async () => {
            if (modal) modal.classList.remove("show");
            const instanceId = this._getInstanceId();
            if (!instanceId) {
                notify(_t("No instance selected."));
                return;
            }

            const action = pendingAction;
            pendingAction = "";
            const endpoints = {
                start: "/saas/instance/start",
                suspend: "/saas/instance/suspend",
                restart: "/saas/instance/restart",
                redeploy: "/saas/instance/redeploy",
                remove: "/saas/instance/remove",
            };
            const endpoint = endpoints[action];
            if (!endpoint) {
                return;
            }

            const label = actionCopy[action] ? actionCopy[action][0] : _t("Action");
            notify(label + " " + _t("in progress..."));
            confirmButton.disabled = true;
            const originalLabel = confirmButton.textContent;
            confirmButton.textContent = _t("Please wait...");

            try {
                const res = await this.rpc(endpoint, { instance_id: instanceId });
                if (res && res.success) {
                    if (action === "restart") {
                        // The containers come back within seconds: show the 30 second countdown
                        // banner instead of reloading (which would immediately display
                        // "Running" again) and confirm once the polls see it running.
                        notify(_t("Restart in progress — your instance will be back in about 30 seconds."));
                        this._beginInstanceRestart(instanceId);
                        return;
                    }
                    if (action === "remove") {
                        // The server keeps the record in 'cancel' state: the reload shows the
                        // "Your instance is removed" blur instead of the controls.
                        notify(_t("Instance removed. Its data has been deleted."));
                        setTimeout(() => location.reload(), 1200);
                        return;
                    }
                    if (action === "suspend") {
                        this._syncInstanceState("Suspended", "suspended");
                    } else if (action === "start") {
                        this._syncInstanceState("Running", "running");
                    }
                    notify(label + " completed successfully.");
                    // Reload so the whole page (state pill, buttons, storage banner) is in sync.
                    setTimeout(() => location.reload(), 900);
                } else {
                    notify(label + " failed: " + ((res && res.error) || "Unknown error"));
                }
            } catch (err) {
                notify("Operation error: " + (err && err.message ? err.message : err));
            } finally {
                confirmButton.disabled = false;
                confirmButton.textContent = originalLabel;
            }
        });

    },

    /* ------------------------------------------------------------------
     * Live instance status banner (Overview tab, above "Instance Controls")
     * ------------------------------------------------------------------ */

    _initInstanceStatusBanner() {
        const banner = document.getElementById("instanceStatusBanner");
        if (!banner) {
            return;
        }
        const instanceId = this._getInstanceId();
        if (!instanceId) {
            return;
        }
        // Survive a manual reload while the restart is still running.
        const key = this._restartStorageKey(instanceId);
        const startedAt = parseInt(sessionStorage.getItem(key) || "0", 10);
        if (!startedAt) {
            return;
        }
        const elapsed = Date.now() - startedAt;
        if (elapsed < INSTANCE_RESTART_WINDOW_MS) {
            this._showRestartCountdown(INSTANCE_RESTART_WINDOW_MS - elapsed, instanceId, key);
        } else {
            sessionStorage.removeItem(key);
        }
    },

    _restartStorageKey(instanceId) {
        return "saas_instance_restart_" + instanceId;
    },

    _applyStatusBanner(mode, subtitle) {
        const banner = document.getElementById("instanceStatusBanner");
        const copy = STATUS_BANNER_COPY[mode];
        if (!banner || !copy) {
            return;
        }
        banner.classList.remove("is-running", "is-suspended", "is-restarting");
        banner.classList.add(copy.className);
        banner.dataset.state = mode;
        const icon = banner.querySelector(".isb-icon i");
        if (icon) {
            icon.className = "fa " + copy.icon;
        }
        const title = document.getElementById("instanceStatusTitle");
        if (title) {
            title.textContent = copy.title;
        }
        const sub = document.getElementById("instanceStatusSub");
        if (sub) {
            sub.textContent = subtitle || copy.sub;
        }
    },

    _syncInstanceState(label, bannerMode) {
        const pill = document.getElementById("instanceState");
        if (pill) {
            pill.textContent = label;
            pill.classList.toggle("suspended", label !== "Running");
        }
        if (bannerMode) {
            this._applyStatusBanner(bannerMode);
        }
    },

    _beginInstanceRestart(instanceId) {
        const key = this._restartStorageKey(instanceId);
        sessionStorage.setItem(key, String(Date.now()));
        this._showRestartCountdown(INSTANCE_RESTART_WINDOW_MS, instanceId, key);
    },

    _showRestartCountdown(msLeft, instanceId, key) {
        clearInterval(this._restartTickTimer);
        clearTimeout(this._restartPollTimer);
        const total = Math.max(0, Math.ceil(msLeft / 1000));
        const render = (remaining) => {
            if (remaining > 0) {
                const mins = Math.floor(remaining / 60);
                const secs = String(remaining % 60).padStart(2, "0");
                this._applyStatusBanner(
                    "restarting",
                    "It will take about 30 seconds. Time remaining: " + mins + ":" + secs + "."
                );
            } else {
                this._applyStatusBanner(
                    "restarting",
                    "Almost done — waiting for the instance to come back online…"
                );
            }
        };
        let remaining = total;
        render(remaining);
        if (remaining <= 0) {
            this._pollInstanceRunning(instanceId, key, 0);
            return;
        }
        this._restartTickTimer = setInterval(() => {
            remaining -= 1;
            render(remaining);
            if (remaining <= 0) {
                clearInterval(this._restartTickTimer);
                this._pollInstanceRunning(instanceId, key, 0);
            }
        }, 1000);
    },

    _pollInstanceRunning(instanceId, key, attempt) {
        clearTimeout(this._restartPollTimer);
        this.rpc("/saas/instance/deployment-status", { instance_id: instanceId })
            .then((res) => {
                if (res && res.success && res.operation_state === "run") {
                    sessionStorage.removeItem(key);
                    this._syncInstanceState("Running", "running");
                    notify(_t("Your instance is running."));
                    return;
                }
                if (attempt >= INSTANCE_RESTART_MAX_POLLS) {
                    sessionStorage.removeItem(key);
                    const isStopped = !!(res && res.success && res.operation_state === "stop");
                    this._applyStatusBanner(
                        isStopped ? "suspended" : "restarting",
                        isStopped
                            ? "The restart did not complete. Please start the instance again."
                            : "The restart is taking longer than expected. Please refresh the page in a moment."
                    );
                    return;
                }
                this._restartPollTimer = setTimeout(
                    () => this._pollInstanceRunning(instanceId, key, attempt + 1),
                    INSTANCE_RESTART_POLL_MS
                );
            })
            .catch(() => {
                if (attempt >= INSTANCE_RESTART_MAX_POLLS) {
                    sessionStorage.removeItem(key);
                    return;
                }
                this._restartPollTimer = setTimeout(
                    () => this._pollInstanceRunning(instanceId, key, attempt + 1),
                    INSTANCE_RESTART_POLL_MS
                );
            });
    },

    _initGithubIntegration() {
        const ghModal = document.getElementById("githubModal");
        const instanceId = this._getInstanceId();
        const repoSelect = document.getElementById("githubRepoSelect");
        const branchSelect = document.getElementById("githubBranchSelect");
        const manualToggle = document.getElementById("toggleManualGit");
        const manualFields = document.getElementById("ghManualFields");
        const btnFetchReposWithToken = document.getElementById("btnFetchReposWithToken");
        const ghReloadRepos = document.getElementById("ghReloadRepos");
        const confirmGithub = document.getElementById("confirmGithub");
        const cancelGithub = document.getElementById("cancelGithub");
        const btnResyncRepo = document.getElementById("btnResyncRepo");
        const ghUserName = document.getElementById("ghUserName");
        const ghUserSub = document.getElementById("ghUserSub");

        let loadedRepos = [];

        const openModal = () => {
            if (ghModal) {
                ghModal.classList.add("show");
                const tokenBlock = document.getElementById("ghTokenSetupBlock");
                const isConnected = !tokenBlock || tokenBlock.style.display === "none";
                if (isConnected && repoSelect && repoSelect.options.length <= 1) {
                    fetchRepos();
                }
            }
        };

        const closeModal = () => {
            if (ghModal) ghModal.classList.remove("show");
        };

        const loadBranches = async (repoFullName, defaultBranch = "main") => {
            if (!branchSelect) return;
            const branchField = document.getElementById("ghBranchField");
            if (branchField) branchField.style.display = "block";
            branchSelect.disabled = true;
            branchSelect.innerHTML = '<option value="' + defaultBranch + '">' + defaultBranch + '</option>';
            const applySelection = (branch) => {
                branchSelect.value = branch;
                const manualBranchInput = document.getElementById("githubBranch");
                if (manualBranchInput) manualBranchInput.value = branch;
            };
            try {
                const res = await this.rpc("/saas/github/branches", {
                    repo_full_name: repoFullName,
                    instance_id: instanceId || null,
                    token: document.getElementById("githubToken")?.value?.trim() || null
                });
                if (res && res.success && res.branches && res.branches.length) {
                    const wanted = res.branches.includes(defaultBranch) ? defaultBranch : res.branches[0];
                    branchSelect.innerHTML = "";
                    res.branches.forEach(branch => {
                        const opt = document.createElement("option");
                        opt.value = branch;
                        opt.textContent = branch + (branch === defaultBranch ? " (default)" : "");
                        if (branch === wanted) opt.selected = true;
                        branchSelect.appendChild(opt);
                    });
                    applySelection(wanted);
                } else {
                    if (res && res.error) {
                        notify("Could not load branches (" + res.error + "). Using " + defaultBranch + ".");
                    }
                    applySelection(defaultBranch);
                }
            } catch (e) {
                notify("Could not load branches. Using " + defaultBranch + ".");
                applySelection(defaultBranch);
            } finally {
                branchSelect.disabled = false;
            }
        };

        const fetchRepos = async (customToken = "") => {
            if (!repoSelect) return;
            repoSelect.innerHTML = '<option value="">⏳ Loading repositories from GitHub...</option>';
            repoSelect.disabled = true;

            try {
                const res = await this.rpc("/saas/github/repos", {
                    instance_id: instanceId || null,
                    token: customToken || null
                });

                if (res && res.success && res.repos && res.repos.length) {
                    loadedRepos = res.repos;
                    repoSelect.innerHTML = '<option value="">-- Select a repository --</option>';
                    if (res.github_login && ghUserName) {
                        ghUserName.textContent = "Connected as @" + res.github_login;
                    }
                    if (ghUserSub) {
                        ghUserSub.textContent = "Found " + res.repos.length + " repositories";
                    }
                    const tokenBlock = document.getElementById("ghTokenSetupBlock");
                    if (tokenBlock) tokenBlock.style.display = "none";
                    const oauthBlock = document.getElementById("ghOAuthOptionBlock");
                    if (oauthBlock) oauthBlock.style.display = "none";
                    const notConnectedPrompt = document.getElementById("ghNotConnectedPrompt");
                    if (notConnectedPrompt) notConnectedPrompt.style.display = "none";
                    const userBadge = document.getElementById("ghUserBadge");
                    if (userBadge) userBadge.style.display = "flex";
                    const dropdownSec = document.getElementById("ghRepoDropdownSection");
                    if (dropdownSec) dropdownSec.style.display = "block";
                    const confirmBtn = document.getElementById("confirmGithub");
                    if (confirmBtn) confirmBtn.style.display = "inline-block";

                    res.repos.forEach(repo => {
                        const opt = document.createElement("option");
                        opt.value = repo.clone_url;
                        opt.dataset.fullName = repo.full_name;
                        opt.dataset.defaultBranch = repo.default_branch || "main";
                        opt.dataset.private = repo.private ? "1" : "0";
                        opt.textContent = `${repo.full_name} ${repo.private ? '(Private)' : '(Public)'}`;
                        repoSelect.appendChild(opt);
                    });

                    // Select current repo if already configured
                    const currentUrl = document.getElementById("githubRepoUrl")?.value?.trim() || "";
                    if (currentUrl) {
                        for (let opt of repoSelect.options) {
                            if (opt.value === currentUrl || (opt.dataset.fullName && currentUrl.includes(opt.dataset.fullName))) {
                                opt.selected = true;
                                loadBranches(opt.dataset.fullName, opt.dataset.defaultBranch);
                                break;
                            }
                        }
                    }
                } else {
                    const errMsg = res?.error || "No repositories found or authorization needed.";
                    repoSelect.innerHTML = '<option value="">⚠️ ' + errMsg + '</option>';
                    if (manualFields) manualFields.style.display = "block";
                }
            } catch (err) {
                repoSelect.innerHTML = '<option value="">⚠️ Error fetching repositories: ' + err.message + '</option>';
                if (manualFields) manualFields.style.display = "block";
            } finally {
                repoSelect.disabled = false;
            }
        };

        // Event: Open GitHub repository modal
        document.querySelectorAll('[data-action="github"], [data-action="github-select-repo"]').forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.preventDefault();
                openModal();
            });
        });

        cancelGithub?.addEventListener("click", closeModal);

        manualToggle?.addEventListener("click", () => {
            if (manualFields) {
                manualFields.style.display = (manualFields.style.display === "none") ? "block" : "none";
            }
        });

        const btnToggleOauthConfig = document.getElementById("btnToggleOauthConfig");
        const ghOauthSetupInline = document.getElementById("ghOauthSetupInline");
        const btnSaveOauthConfig = document.getElementById("btnSaveOauthConfig");

        btnToggleOauthConfig?.addEventListener("click", (e) => {
            e.preventDefault();
            if (ghOauthSetupInline) {
                ghOauthSetupInline.style.display = (ghOauthSetupInline.style.display === "none") ? "block" : "none";
            }
        });

        btnSaveOauthConfig?.addEventListener("click", async (e) => {
            e.preventDefault();
            const clientId = document.getElementById("oauthClientIdInput")?.value?.trim();
            const clientSecret = document.getElementById("oauthClientSecretInput")?.value?.trim();
            if (!clientId || !clientSecret) {
                notify(_t("Please enter both GitHub Client ID and Client Secret."));
                return;
            }
            btnSaveOauthConfig.disabled = true;
            btnSaveOauthConfig.textContent = "Saving...";
            try {
                const res = await this.rpc("/saas/github/save-oauth-config", {
                    client_id: clientId,
                    client_secret: clientSecret
                });
                if (res && res.success) {
                    notify(_t("GitHub OAuth credentials saved! Redirecting to GitHub..."));
                    setTimeout(() => {
                        window.location.href = `/saas/github/login?instance_id=${instanceId}`;
                    }, 600);
                } else {
                    notify("Error saving credentials: " + (res?.error || "Error"));
                    btnSaveOauthConfig.disabled = false;
                    btnSaveOauthConfig.textContent = "Save & Connect";
                }
            } catch (err) {
                notify("Error: " + err.message);
                btnSaveOauthConfig.disabled = false;
                btnSaveOauthConfig.textContent = "Save & Connect";
            }
        });

        ghReloadRepos?.addEventListener("click", () => {
            fetchRepos(document.getElementById("githubToken")?.value?.trim() || "");
        });

        btnFetchReposWithToken?.addEventListener("click", () => {
            const token = document.getElementById("githubToken")?.value?.trim();
            if (!token) {
                notify(_t("Please enter a personal access token."));
                return;
            }
            fetchRepos(token);
        });

        repoSelect?.addEventListener("change", () => {
            const selectedOpt = repoSelect.selectedOptions[0];
            const branchField = document.getElementById("ghBranchField");
            if (selectedOpt && selectedOpt.value) {
                const defaultBranch = selectedOpt.dataset.defaultBranch || "main";
                const manualUrlInput = document.getElementById("githubRepoUrl");
                const manualBranchInput = document.getElementById("githubBranch");
                if (manualUrlInput) manualUrlInput.value = selectedOpt.value;
                if (manualBranchInput) manualBranchInput.value = defaultBranch;
                loadBranches(selectedOpt.dataset.fullName, defaultBranch);
            } else if (branchField) {
                branchField.style.display = "none";
            }
        });

        branchSelect?.addEventListener("change", () => {
            const manualBranchInput = document.getElementById("githubBranch");
            if (manualBranchInput) manualBranchInput.value = branchSelect.value;
        });

        // Confirm & Sync repository
        confirmGithub?.addEventListener("click", async () => {
            let repoUrl = "";
            let repoName = "";
            let branch = "main";
            const token = document.getElementById("githubToken")?.value?.trim() || "";

            const selectedOpt = repoSelect?.selectedOptions?.[0];
            if (selectedOpt && selectedOpt.value) {
                repoUrl = selectedOpt.value;
                repoName = selectedOpt.dataset.fullName || "";
                branch = branchSelect?.value || selectedOpt.dataset.defaultBranch || "main";
            } else {
                repoUrl = document.getElementById("githubRepoUrl")?.value?.trim() || "";
                branch = document.getElementById("githubBranch")?.value?.trim() || "main";
            }

            if (!repoUrl) {
                notify(_t("Please select or enter a repository URL."));
                return;
            }

            confirmGithub.disabled = true;
            confirmGithub.textContent = "Syncing & Deploying...";
            notify(_t("Cloning repository and configuring custom addons..."));

            try {
                const res = await this.rpc("/saas/github/connect-repo", {
                    instance_id: instanceId,
                    repo_url: repoUrl,
                    repo_name: repoName,
                    branch: branch,
                    token: token || null
                });

                if (res && res.success) {
                    notify(_t("Repository synced successfully! Custom addons are now active."));
                    closeModal();
                    setTimeout(() => location.reload(), 1200);
                } else {
                    notify("Sync error: " + (res?.error || "Could not deploy repository."));
                }
            } catch (err) {
                notify("Error: " + err.message);
            } finally {
                confirmGithub.disabled = false;
                confirmGithub.textContent = "Sync & Deploy";
            }
        });

        // Re-sync button handler
        btnResyncRepo?.addEventListener("click", async (e) => {
            e.preventDefault();
            const instId = btnResyncRepo.dataset.instanceId || instanceId;
            btnResyncRepo.disabled = true;
            btnResyncRepo.innerHTML = "⏳ Syncing...";
            notify(_t("Pulling latest git changes and reloading instance..."));

            try {
                const res = await this.rpc("/saas/github/resync", { instance_id: instId });
                if (res && res.success) {
                    notify(_t("Custom addons re-synced! Instance updated with latest code."));
                    const syncTimestamp = document.getElementById("syncTimestamp");
                    if (syncTimestamp) {
                        syncTimestamp.textContent = "Synced just now";
                    }
                } else {
                    notify("Re-sync failed: " + (res?.error || "Error"));
                }
            } catch (err) {
                notify("Re-sync error: " + err.message);
            } finally {
                btnResyncRepo.disabled = false;
                btnResyncRepo.innerHTML = "↻ Re-sync";
            }
        });

        // Single "Disconnect GitHub" action: opens a Yes/No confirmation popup and,
        // when confirmed, removes all cloned custom addons and unlinks the account.
        const ghDisconnectModal = document.getElementById("ghDisconnectModal");
        const ghDisconnectYes = document.getElementById("ghDisconnectYes");
        const ghDisconnectNo = document.getElementById("ghDisconnectNo");
        let disconnectInstanceId = instanceId;

        const closeDisconnectModal = () => {
            if (ghDisconnectModal) ghDisconnectModal.classList.remove("show");
        };

        document.querySelectorAll('[data-action="github-disconnect"]').forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.preventDefault();
                disconnectInstanceId = e.currentTarget?.dataset?.instanceId || instanceId;
                if (ghDisconnectModal) {
                    ghDisconnectModal.classList.add("show");
                }
            });
        });

        ghDisconnectNo?.addEventListener("click", closeDisconnectModal);

        ghDisconnectYes?.addEventListener("click", async () => {
            closeDisconnectModal();
            ghDisconnectYes.disabled = true;
            notify(_t("Disconnecting GitHub and removing custom addons..."));
            try {
                const res = await this.rpc("/saas/github/disconnect-all", {
                    instance_id: disconnectInstanceId || null,
                });
                if (res && res.success) {
                    notify(res.message || "GitHub disconnected successfully.");
                    setTimeout(() => location.reload(), 1200);
                } else {
                    notify("Failed to disconnect: " + ((res && res.error) || "Unknown error"));
                }
            } catch (err) {
                notify("Error: " + (err && err.message ? err.message : err));
            } finally {
                ghDisconnectYes.disabled = false;
            }
        });

        // Check URL parameters for OAuth returns or errors
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get("github_action") === "select_repo" || urlParams.get("open_github_modal") === "1") {
            openModal();
            window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
        }
        if (urlParams.get("github_error")) {
            const err = decodeURIComponent(urlParams.get("github_error"));
            if (err === "oauth_not_configured") {
                notify(_t("GitHub OAuth app is not configured by the admin yet."));
                openModal();
                const warningBox = document.getElementById("ghOauthWarning");
                if (warningBox) warningBox.style.display = "block";
                if (manualFields) manualFields.style.display = "block";
            } else {
                notify("GitHub connection error: " + err);
            }
            window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
        }
    },

    /** Notification switches: saved instantly, and a test email to prove SMTP works. */
    _initNotifications() {
        const status = document.getElementById("notifyStatus");
        const setStatus = (text, isError) => {
            if (status) {
                status.textContent = text;
                status.style.color = isError ? "#c25146" : "var(--muted)";
            }
        };

        document.querySelectorAll("[data-notify]").forEach((button) => {
            if (button.dataset.bound) return;
            button.dataset.bound = "1";
            button.addEventListener("click", async () => {
                const enabled = !button.classList.contains("on");
                button.classList.toggle("on", enabled);
                try {
                    const events = {};
                    events[button.dataset.notify] = enabled;
                    const res = await this.rpc("/saas/settings/notifications", { events: events });
                    setStatus(res && res.success ? "Saved." : "Could not save.", !(res && res.success));
                } catch (err) {
                    button.classList.toggle("on", !enabled);
                    setStatus("Could not save: " + (err && err.message ? err.message : err), true);
                }
            });
        });

        const emailInput = document.getElementById("notifyEmail");
        document.getElementById("saveNotifyPrefs")?.addEventListener("click", async () => {
            const events = {};
            document.querySelectorAll("[data-notify]").forEach((button) => {
                events[button.dataset.notify] = button.classList.contains("on");
            });
            try {
                const res = await this.rpc("/saas/settings/notifications", {
                    events: events,
                    email: emailInput ? emailInput.value : "",
                });
                if (res && res.success) {
                    setStatus("Notification settings saved to " + (res.email || "your account email") + ".");
                } else {
                    setStatus((res && res.error) || "Could not save.", true);
                }
            } catch (err) {
                setStatus("Could not save: " + (err && err.message ? err.message : err), true);
            }
        });

        document.getElementById("sendTestNotify")?.addEventListener("click", async () => {
            setStatus("Sending test email…");
            try {
                const res = await this.rpc("/saas/settings/notifications/test", {});
                if (res && res.success) {
                    setStatus(res.message || "Test email sent.");
                    notify(res.message || "Test email sent.");
                } else {
                    setStatus((res && res.error) || "Test email failed.", true);
                    notify((res && res.error) || "Test email failed.");
                }
            } catch (err) {
                setStatus("Test email failed: " + (err && err.message ? err.message : err), true);
            }
        });
    },

    _initSearch() {
        const search = document.getElementById("instanceSearch");
        if (search) {
            search.addEventListener("input", () => {
                const query = search.value.toLowerCase();
                document.querySelectorAll("#instanceList [data-name]").forEach(card => {
                    const match = card.dataset.name.toLowerCase().includes(query);
                    card.classList.toggle("hidden", !match);
                });
            });
        }
    },

    _initSettings() {
        // Tab switching
        document.querySelectorAll("[data-setting]").forEach(btn => {
            btn.addEventListener("click", () => {
                document.querySelectorAll("[data-setting]").forEach(b => b.classList.remove("active"));
                document.querySelectorAll(".settings-pane").forEach(p => p.classList.remove("active"));
                btn.classList.add("active");
                const target = document.getElementById("setting-" + btn.dataset.setting);
                if (target) target.classList.add("active");
            });
        });

        // URL hash support
        if (location.hash) {
            const hashBtn = document.querySelector('[data-setting="' + location.hash.slice(1) + '"]');
            if (hashBtn) hashBtn.click();
        }

        // Decorative toggles only: anything with a real server handler (notifications,
        // 2FA, login alerts) must not have its class flipped behind its back.
        document.querySelectorAll(".toggle-button").forEach(btn => {
            if (btn.dataset.notify || btn.dataset.bound
                    || btn.id === "totpToggle" || btn.id === "loginAlertsToggle") {
                return;
            }
            btn.addEventListener("click", () => {
                btn.classList.toggle("on");
                notify(btn.classList.contains("on") ? "Setting enabled." : "Setting disabled.");
            });
        });

        // Save settings button
        const saveBtn = document.getElementById("saveSettings");
        if (saveBtn) {
            saveBtn.addEventListener("click", async () => {
                const status = document.getElementById("saveStatus");
                if (status) status.textContent = "Saving...";
                const payload = {};
                document.querySelectorAll("[data-save]").forEach(input => {
                    payload[input.dataset.save] = input.value;
                });
                try {
                    await this.rpc("/saas/settings/save", payload);
                    if (status) status.textContent = "Saved!";
                    notify(_t("Settings saved successfully."));
                    setTimeout(() => { if (status) status.textContent = ""; }, 3000);
                } catch (e) {
                    if (status) status.textContent = "Error";
                    notify("Failed to save settings: " + e.message);
                }
            });
        }

        const loginEmailBtn = document.getElementById("changeLoginEmail");
        if (loginEmailBtn) {
            loginEmailBtn.addEventListener("click", async () => {
                const input = document.getElementById("loginEmail");
                if (!input || !input.value.includes("@")) {
                    notify(_t("Enter a valid email address."));
                    return;
                }
                loginEmailBtn.disabled = true;
                try {
                    const res = await this.rpc("/saas/settings/login-email",
                        { new_email: input.value });
                    if (res && res.success) {
                        notify(res.message || "Login email updated.");
                    } else {
                        notify((res && res.error) || "Could not change the login email.");
                    }
                } catch (err) {
                    notify(_t("Could not change the login email."));
                }
                loginEmailBtn.disabled = false;
            });
        }

        // Password change (the current password is verified server side)
        const changePassBtn = document.getElementById("changePassword");
        if (changePassBtn) {
            changePassBtn.addEventListener("click", async () => {
                const current = document.getElementById("currentPassword");
                const newPass = document.getElementById("newPassword");
                const confirmPass = document.getElementById("confirmPassword");
                if (!newPass || !confirmPass) return;
                if ((newPass.value || "").length < 8) {
                    notify(_t("Password must contain at least 8 characters."));
                    return;
                }
                if (newPass.value !== confirmPass.value) {
                    notify(_t("Passwords do not match."));
                    return;
                }
                changePassBtn.disabled = true;
                try {
                    const res = await this.rpc("/saas/settings/password", {
                        current: current ? current.value : "",
                        new_password: newPass.value,
                        confirm: confirmPass.value,
                    });
                    if (res && res.success) {
                        newPass.value = "";
                        confirmPass.value = "";
                        if (current) current.value = "";
                        notify(res.message || "Password updated successfully.");
                    } else {
                        notify((res && res.error) || "Could not change the password.");
                    }
                } catch (err) {
                    notify("Could not change the password: " + (err && err.message ? err.message : err));
                } finally {
                    changePassBtn.disabled = false;
                }
            });
        }

        // Sign out everywhere: kills every stored session, then logs this tab out too
        const signOutBtn = document.getElementById("signOutDevices");
        if (signOutBtn) {
            signOutBtn.addEventListener("click", async () => {
                signOutBtn.disabled = true;
                try {
                    const res = await this.rpc("/saas/settings/signout-all", {});
                    notify((res && res.message) || "Signed out on every device.");
                } catch (err) {
                    notify(_t("Signing out…"));
                }
                window.location.href = "/web/session/logout?redirect=/web/login";
            });
        }

        // Login alerts (email on every sign-in)
        const alertsToggle = document.getElementById("loginAlertsToggle");
        if (alertsToggle && !alertsToggle.dataset.bound) {
            alertsToggle.dataset.bound = "1";
            alertsToggle.addEventListener("click", async () => {
                const enabled = !alertsToggle.classList.contains("on");
                alertsToggle.classList.toggle("on", enabled);
                try {
                    const res = await this.rpc("/saas/settings/login-alerts", { enabled: enabled });
                    if (res && res.success) {
                        notify(enabled ? "Login alerts are on." : "Login alerts are off.");
                    } else {
                        alertsToggle.classList.toggle("on", !enabled);
                        notify((res && res.error) || "Could not save login alerts.");
                    }
                } catch (err) {
                    alertsToggle.classList.toggle("on", !enabled);
                    notify(_t("Could not save login alerts."));
                }
            });
        }

        // Two-factor authentication: QR + code, enforced by Odoo's own auth_totp module
        const totpToggle = document.getElementById("totpToggle");
        if (totpToggle && !totpToggle.dataset.bound) {
            totpToggle.dataset.bound = "1";
            const box = document.getElementById("totpSetup");
            const qr = document.getElementById("totpQr");
            const secretText = document.getElementById("totpSecret");
            const codeInput = document.getElementById("totpCode");
            const applyBtn = document.getElementById("totpApply");
            const status = document.getElementById("totpStatus");
            const setStatus = (text, isError) => {
                if (status) {
                    status.textContent = text;
                    status.style.color = isError ? "#c25146" : "var(--muted)";
                }
            };

            const syncState = async () => {
                try {
                    const res = await this.rpc("/saas/settings/totp/init", {});
                    if (!res || !res.success) return false;
                    // The server is the only trustworthy source: the generic .toggle-button
                    // handler further up flips the CSS class on every click.
                    totpToggle.dataset.enabled = res.enabled ? "1" : "0";
                    totpToggle.classList.toggle("on", !!res.enabled);
                    setStatus(res.enabled
                        ? "Two-factor authentication is active — the app code is required on every sign-in."
                        : "Two-factor authentication is off.");
                    return true;
                } catch (err) {
                    return false;
                }
            };
            syncState();

            // In-app confirmation dialog (no browser prompt): Yes removes 2FA, No closes it.
            const openRemoveDialog = () => {
                const overlay = this._ghEl("div");
                overlay.setAttribute("style",
                    "position:fixed;inset:0;z-index:150;background:rgba(9,32,36,.45);"
                    + "display:flex;align-items:center;justify-content:center;padding:16px");
                const card = this._ghEl("div");
                card.setAttribute("style", "width:100%;max-width:460px;background:#fff;"
                    + "border-radius:16px;padding:24px;box-shadow:0 20px 50px rgba(9,32,36,.3)");
                const title = this._ghEl("div", "", "Remove two-factor authentication?");
                title.setAttribute("style", "font-size:17px;font-weight:800;color:#0d3a44;margin-bottom:8px");
                const text = this._ghEl("div", "",
                    "Are you sure you want to remove 2-factor authentication from this account? "
                    + "The authenticator code will no longer be asked when you sign in.");
                text.setAttribute("style",
                    "font-size:13px;line-height:1.6;color:#5b6b73;margin-bottom:20px");
                const actions = this._ghEl("div");
                actions.setAttribute("style",
                    "display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap");
                const no = this._ghEl("button", "ghost", "No, keep it");
                const yes = this._ghEl("button", "primary", "Yes, remove it");
                yes.setAttribute("style", "background:#c25146;border-color:transparent");
                const close = () => overlay.remove();
                no.addEventListener("click", close);
                overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
                yes.addEventListener("click", async () => {
                    yes.disabled = true;
                    try {
                        const res = await this.rpc("/saas/settings/totp/disable", {});
                        if (res && res.success) {
                            close();
                            if (box) box.style.display = "none";
                            await syncState();
                            notify(res.message || "Two-factor authentication removed.");
                        } else {
                            yes.disabled = false;
                            setStatus((res && res.error) || "Could not remove 2FA.", true);
                        }
                    } catch (err) {
                        yes.disabled = false;
                        setStatus("Could not remove 2FA.", true);
                    }
                });
                actions.appendChild(no);
                actions.appendChild(yes);
                card.appendChild(title);
                card.appendChild(text);
                card.appendChild(actions);
                overlay.appendChild(card);
                document.body.appendChild(overlay);
            };

            totpToggle.addEventListener("click", async () => {
                // Always re-read the state from the server before deciding what the click
                // means, otherwise turning 2FA on would ask for the disable password.
                await syncState();
                if (totpToggle.dataset.enabled === "1") {
                    openRemoveDialog();
                    return;
                }
                try {
                    const res = await this.rpc("/saas/settings/totp/init", {});
                    if (res && res.success) {
                        if (qr) qr.src = res.qr_url;
                        if (secretText) secretText.textContent = res.secret || "";
                        totpToggle.dataset.secret = res.secret || "";
                        if (box) box.style.display = "block";
                        setStatus("Scan the QR with your authenticator app, then enter the 6-digit code.");
                    } else {
                        setStatus((res && res.error) || "Could not start the 2FA setup.", true);
                    }
                } catch (err) {
                    setStatus("Could not start the 2FA setup.", true);
                }
            });

            if (applyBtn) {
                applyBtn.addEventListener("click", async () => {
                    const code = codeInput ? codeInput.value.trim() : "";
                    if (!/^\d{6}$/.test(code)) {
                        setStatus("Enter the 6-digit code shown in the app.", true);
                        return;
                    }
                    applyBtn.disabled = true;
                    try {
                        const res = await this.rpc("/saas/settings/totp/apply", {
                            secret: totpToggle.dataset.secret || "", code: code,
                        });
                        if (res && res.success) {
                            totpToggle.classList.add("on");
                            if (box) box.style.display = "none";
                            if (codeInput) codeInput.value = "";
                            setStatus(res.message || "Two-factor authentication is active.");
                            notify(res.message || "Two-factor authentication is active.");
                        } else {
                            setStatus((res && res.error) || "That code is not valid.", true);
                        }
                    } catch (err) {
                        setStatus("Could not verify the code.", true);
                    } finally {
                        applyBtn.disabled = false;
                    }
                });
            }
        }

        // Download latest invoice
        const invoiceBtn = document.getElementById("downloadInvoice");
        if (invoiceBtn) {
            invoiceBtn.addEventListener("click", () => {
                notify(_t("Latest invoice download initiated."));
            });
        }

        // Team management: real invites (Odoo outgoing mail server) and manual members.
        const teamNotifyAndReload = (message) => {
            notify(message);
            setTimeout(() => window.location.reload(), 1200);
        };

        const bindRemove = () => {
            document.querySelectorAll("[data-remove-member]").forEach(button => {
                button.onclick = async () => {
                    const row = button.closest(".member-row");
                    const memberId = row ? row.dataset.memberId : null;
                    if (!memberId) return;
                    button.disabled = true;
                    try {
                        // Full removal: the member is archived, so the address can be
                    // added again straight away.
                    const res = await this.rpc("/saas/team/remove", { member_id: memberId });
                        if (res && res.success) {
                            teamNotifyAndReload(res.message || "Team member removed.");
                        } else {
                            button.disabled = false;
                            notify((res && res.error) || "Could not remove the team member.");
                        }
                    } catch (err) {
                        button.disabled = false;
                        notify(_t("Could not remove the team member."));
                    }
                };
            });
        };
        bindRemove();

        // The owner can set a member's password directly, or mail them a reset link.
        const resetPasswordFor = (memberId, label) => {
            const overlay = this._ghEl("div");
            overlay.setAttribute("style",
                "position:fixed;inset:0;z-index:150;background:rgba(9,32,36,.45);"
                + "display:flex;align-items:center;justify-content:center;padding:16px");
            const card = this._ghEl("div");
            card.setAttribute("style", "width:100%;max-width:440px;background:#fff;"
                + "border-radius:16px;padding:24px;box-shadow:0 20px 50px rgba(9,32,36,.3)");
            const title = this._ghEl("div", "", "Reset password");
            title.setAttribute("style", "font-size:17px;font-weight:800;color:#0d3a44;margin-bottom:6px");
            const text = this._ghEl("div", "",
                "Set a new password for " + label + ". Leave it empty and we email them a reset link instead.");
            text.setAttribute("style", "font-size:12px;line-height:1.6;color:#5b6b73;margin-bottom:14px");
            const input = document.createElement("input");
            input.type = "password";
            input.placeholder = "New password (min 8 characters)";
            input.setAttribute("style",
                "width:100%;border:1px solid #dfe7ea;border-radius:10px;padding:11px;font-size:13px");
            const status = this._ghEl("div", "");
            status.setAttribute("style", "font-size:11px;color:#c25146;margin-top:8px;min-height:14px");
            const actions = this._ghEl("div");
            actions.setAttribute("style",
                "display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;margin-top:16px");
            const cancel = this._ghEl("button", "ghost", "Cancel");
            const save = this._ghEl("button", "primary dark", "Save");
            const close = () => overlay.remove();
            cancel.addEventListener("click", close);
            overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
            save.addEventListener("click", async () => {
                const value = input.value || "";
                if (value && value.length < 8) {
                    status.textContent = "Password must be at least 8 characters.";
                    return;
                }
                save.disabled = true;
                try {
                    const res = await this.rpc("/saas/team/reset-password",
                        { member_id: memberId, password: value });
                    if (res && res.success) {
                        close();
                        teamNotifyAndReload(res.message || "Password updated.");
                    } else {
                        save.disabled = false;
                        status.textContent = (res && res.error) || "Could not reset the password.";
                    }
                } catch (err) {
                    save.disabled = false;
                    status.textContent = "Could not reset the password.";
                }
            });
            actions.appendChild(cancel);
            actions.appendChild(save);
            card.appendChild(title);
            card.appendChild(text);
            card.appendChild(input);
            card.appendChild(status);
            card.appendChild(actions);
            overlay.appendChild(card);
            document.body.appendChild(overlay);
            input.focus();
        };

        // Pick which instances a member may open.
        const openInstancePicker = async (memberId, label) => {
            let list = [];
            try {
                const res = await this.rpc("/saas/team/instances", { member_id: memberId });
                list = (res && res.instances) || [];
            } catch (err) {
                list = [];
            }
            const overlay = this._ghEl("div");
            overlay.setAttribute("style",
                "position:fixed;inset:0;z-index:150;background:rgba(9,32,36,.45);"
                + "display:flex;align-items:center;justify-content:center;padding:16px");
            const card = this._ghEl("div");
            card.setAttribute("style", "width:100%;max-width:460px;background:#fff;"
                + "border-radius:16px;padding:24px;box-shadow:0 20px 50px rgba(9,32,36,.3)");
            const title = this._ghEl("div", "", "Instance access");
            title.setAttribute("style", "font-size:17px;font-weight:800;color:#0d3a44;margin-bottom:6px");
            const text = this._ghEl("div", "",
                "Choose which instances " + label + " can open. Anything left unchecked stays invisible to them.");
            text.setAttribute("style", "font-size:12px;line-height:1.6;color:#5b6b73;margin-bottom:12px");
            card.appendChild(title);
            card.appendChild(text);
            if (!list.length) {
                const empty = this._ghEl("div", "", "This account has no instance yet.");
                empty.setAttribute("style", "font-size:12px;color:#5b6b73");
                card.appendChild(empty);
            }
            list.forEach(item => {
                const line = document.createElement("label");
                line.setAttribute("style", "display:flex;align-items:center;gap:10px;"
                    + "padding:9px 0;font-size:13px;color:#0d3a44;border-top:1px solid #eef3f4");
                const box = document.createElement("input");
                box.type = "checkbox";
                box.checked = !!item.selected;
                box.dataset.instanceId = item.id;
                const nameEl = document.createElement("span");
                nameEl.textContent = item.name;
                line.appendChild(box);
                line.appendChild(nameEl);
                card.appendChild(line);
            });
            const actions = this._ghEl("div");
            actions.setAttribute("style",
                "display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;margin-top:16px");
            const cancel = this._ghEl("button", "ghost", "Cancel");
            const save = this._ghEl("button", "primary dark", "Save access");
            const close = () => overlay.remove();
            cancel.addEventListener("click", close);
            overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
            save.addEventListener("click", async () => {
                const ids = Array.from(card.querySelectorAll("input[type=checkbox]"))
                    .filter(box => box.checked)
                    .map(box => box.dataset.instanceId);
                save.disabled = true;
                try {
                    const res = await this.rpc("/saas/team/instances/set",
                        { member_id: memberId, instance_ids: ids });
                    if (res && res.success) {
                        close();
                        teamNotifyAndReload(res.message || "Instance access updated.");
                    } else {
                        save.disabled = false;
                        notify((res && res.error) || "Could not save the instance access.");
                    }
                } catch (err) {
                    save.disabled = false;
                    notify(_t("Could not save the instance access."));
                }
            });
            actions.appendChild(cancel);
            actions.appendChild(save);
            card.appendChild(actions);
            overlay.appendChild(card);
            document.body.appendChild(overlay);
        };

        const accessInstanceId = (() => {
            const holder = document.getElementById("accessInstanceId");
            return holder ? holder.dataset.instanceId : "";
        })();

        const bindMemberButtons = () => {
        document.querySelectorAll(".member-row[data-member-id]").forEach(row => {
            const labelEl = row.querySelector("strong");
            const label = labelEl ? labelEl.textContent : "this member";
            let instancesBtn = null;
            if (!accessInstanceId) {
                // Only shown outside an instance tab: inside Access the instance is fixed.
                instancesBtn = this._ghEl("button", "ghost", "Instances");
                instancesBtn.setAttribute("style", "white-space:nowrap");
                instancesBtn.addEventListener("click", () => openInstancePicker(row.dataset.memberId, label));
            }
            const reinviteBtn = this._ghEl("button", "ghost", "Re-invite");
            reinviteBtn.setAttribute("style", "white-space:nowrap");
            reinviteBtn.addEventListener("click", async () => {
                reinviteBtn.disabled = true;
                try {
                    const res = await this.rpc("/saas/team/re-invite",
                        { member_id: row.dataset.memberId });
                    if (res && res.success) {
                        notify(res.message || "Invitation sent again.");
                    } else {
                        notify((res && res.error) || "Could not send the invitation.");
                    }
                } catch (err) {
                    notify(_t("Could not send the invitation."));
                }
                reinviteBtn.disabled = false;
            });
            const resetBtn = this._ghEl("button", "ghost", "Reset password");
            resetBtn.addEventListener("click", () => resetPasswordFor(row.dataset.memberId, label));
            // All three buttons live in one non-wrapping group so they stay on one line.
            const actions = document.createElement("div");
            actions.setAttribute("style",
                "display:flex;align-items:center;gap:8px;flex-wrap:nowrap;"
                + "margin-left:auto;white-space:nowrap");
            resetBtn.setAttribute("style", "white-space:nowrap");
            const removeBtn = row.querySelector("[data-remove-member]");
            if (instancesBtn) actions.appendChild(instancesBtn);
            actions.appendChild(reinviteBtn);
            actions.appendChild(resetBtn);
            if (removeBtn) {
                removeBtn.setAttribute("style",
                    (removeBtn.getAttribute("style") || "") + ";white-space:nowrap");
                actions.appendChild(removeBtn);
            }
            row.appendChild(actions);
        });
        };

        const renderMemberRows = (list) => {
            const host = document.getElementById("memberList");
            if (!host) return;
            host.querySelectorAll(".member-row[data-member-id]").forEach(r => r.remove());
            (list || []).forEach(m => {
                const row = document.createElement("div");
                row.className = "member-row";
                row.dataset.memberId = m.id;
                const info = document.createElement("div");
                const strong = document.createElement("strong");
                strong.textContent = m.name || m.email;
                const small = document.createElement("small");
                small.textContent = m.email || "";
                info.appendChild(strong);
                info.appendChild(small);
                const role = document.createElement("span");
                role.textContent = m.role_label || m.role;
                const status = document.createElement("span");
                status.className = "status";
                status.textContent = m.invited ? "Invited" : "Active";
                const remove = document.createElement("button");
                remove.className = "ghost";
                remove.setAttribute("data-remove-member", "data-remove-member");
                remove.textContent = "Remove";
                row.appendChild(info);
                row.appendChild(role);
                row.appendChild(status);
                row.appendChild(remove);
                host.appendChild(row);
            });
            bindRemove();
            bindMemberButtons();
        };

        const loadInstanceMembers = async () => {
            if (!accessInstanceId) return;
            try {
                const res = await this.rpc("/saas/team/list", { instance_id: accessInstanceId });
                if (res && res.success) renderMemberRows(res.members);
            } catch (err) { /* the owner row stays visible */ }
        };
        bindMemberButtons();
        loadInstanceMembers();

        const submitMember = async (payload, button, clear) => {
            button.disabled = true;
            try {
                const res = await this.rpc("/saas/team/add", payload);
                if (res && res.success) {
                    clear();
                    teamNotifyAndReload(res.message || "Team member added.");
                } else {
                    button.disabled = false;
                    notify((res && res.error) || "Could not add the team member.");
                }
            } catch (err) {
                button.disabled = false;
                notify(_t("Could not add the team member."));
            }
        };

        const inviteBtn = document.getElementById("inviteMember");
        if (inviteBtn) {
            inviteBtn.addEventListener("click", () => {
                const name = document.getElementById("inviteName");
                const email = document.getElementById("inviteEmail");
                const role = document.getElementById("inviteRole");
                if (!email || !email.value.includes("@")) {
                    notify(_t("Enter a valid email address."));
                    return;
                }
                submitMember({
                    name: name ? name.value : "",
                    email: email.value,
                    role: role ? role.value : "developer",
                    instance_id: accessInstanceId || undefined,
                }, inviteBtn, () => {
                    if (name) name.value = "";
                    email.value = "";
                });
            });
        }

        const manualBtn = document.getElementById("addMember");
        if (manualBtn) {
            manualBtn.addEventListener("click", () => {
                const name = document.getElementById("manualName");
                const email = document.getElementById("manualEmail");
                const password = document.getElementById("manualPassword");
                const role = document.getElementById("manualRole");
                if (!email || !email.value.includes("@")) {
                    notify(_t("Enter a valid email address."));
                    return;
                }
                if (!password || password.value.length < 8) {
                    notify(_t("Password must contain at least 8 characters."));
                    return;
                }
                submitMember({
                    name: name ? name.value : "",
                    email: email.value,
                    password: password.value,
                    role: role ? role.value : "developer",
                    instance_id: accessInstanceId || undefined,
                }, manualBtn, () => {
                    if (name) name.value = "";
                    email.value = "";
                    password.value = "";
                });
            });
        }
    },

    _initMails() {
        document.querySelectorAll(".mail-item").forEach(item => {
            item.addEventListener("click", () => {
                document.querySelectorAll(".mail-item").forEach(m => m.classList.remove("active"));
                item.classList.add("active");
                notify(_t("Email preview loaded."));
            });
        });
    },

    _initPricing() {
        const planModal = document.getElementById("orderPlanModal");
        const userPriceMonthly = parseFloat(planModal?.dataset.userPriceMonthly || 100);
        const userPriceYearly = parseFloat(planModal?.dataset.userPriceYearly || 1020);
        const userPriceAnnualMonth = parseFloat(planModal?.dataset.userPriceAnnualMonth || 85);
        const userPriceMonthlyFmt = planModal?.dataset.userPriceMonthlyFmt || "100";
        const userPriceYearlyFmt = planModal?.dataset.userPriceYearlyFmt || "1,020";
        const userPriceAnnualMonthFmt = planModal?.dataset.userPriceAnnualMonthFmt || "85";

        const storagePriceMonthly = parseFloat(planModal?.dataset.storagePriceMonthly || 220);
        const storagePriceYearly = parseFloat(planModal?.dataset.storagePriceYearly || 2244);
        const storagePriceAnnualMonth = parseFloat(planModal?.dataset.storagePriceAnnualMonth || 187);
        const storagePriceMonthlyFmt = planModal?.dataset.storagePriceMonthlyFmt || "220";
        const storagePriceYearlyFmt = planModal?.dataset.storagePriceYearlyFmt || "2,244";
        const storagePriceAnnualMonthFmt = planModal?.dataset.storagePriceAnnualMonthFmt || "187";

        // Currency symbol is provided by the server (company currency, e.g. €).
        const currencySymbol = planModal?.dataset.currencySymbol || "";

        const formatMoney = (val) => {
            if (val === null || val === undefined || isNaN(val)) return "0";
            const num = parseFloat(val);
            if (Number.isInteger(num)) {
                return num.toLocaleString();
            }
            return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        };

        const updateModalPricing = () => {
            const activePlanBtn = document.querySelector("[data-choose-plan].selected") || document.querySelector('[data-choose-plan="Standard"]');
            const planName = activePlanBtn ? activePlanBtn.dataset.choosePlan : "Standard";
            const planProductId = activePlanBtn ? (activePlanBtn.dataset.productId || "") : "";
            const baseStorage = parseInt(activePlanBtn?.dataset.baseStorage || (planName === "Growth" ? 20 : 5));
            const annual = document.querySelector('[data-billing="annual"]')?.classList.contains("active");

            // Plan base price
            const planMonthlyRaw = parseFloat(activePlanBtn?.dataset.monthlyRaw || (planName === "Growth" ? 39900 : 14900));
            const planAnnualRaw = parseFloat(activePlanBtn?.dataset.annualRaw || (planName === "Growth" ? 406980 : 151980));
            const planPrice = annual ? planAnnualRaw : planMonthlyRaw;

            // Users count
            const countInput = document.getElementById("planUsersCount");
            let usersCount = countInput ? (parseInt(countInput.value) || 1) : 1;
            if (usersCount < 1) usersCount = 1;

            // Storage GB
            const storageInput = document.getElementById("planStorageGb");
            if (storageInput) {
                storageInput.min = baseStorage;
                let currentStorage = parseInt(storageInput.value) || baseStorage;
                if (currentStorage < baseStorage) {
                    currentStorage = baseStorage;
                    storageInput.value = baseStorage;
                }
            }
            let storageGb = storageInput ? (parseInt(storageInput.value) || baseStorage) : baseStorage;
            if (storageGb < baseStorage) storageGb = baseStorage;
            const extraStorageGb = Math.max(0, storageGb - baseStorage);

            // Worker pricing display — workers cost the same on monthly and annual billing.
            const userPriceEl = document.getElementById("userPriceDisplay");
            if (userPriceEl) {
                userPriceEl.textContent = userPriceMonthlyFmt + " " + currencySymbol + " / worker / month";
            }

            // Storage pricing display — same price on monthly and annual billing.
            const storagePriceEl = document.getElementById("storagePriceDisplay");
            if (storagePriceEl) {
                storagePriceEl.textContent = baseStorage + " GB included · + " + storagePriceMonthlyFmt
                    + " " + currencySymbol + " / extra GB";
            }

            // Calculate exact total (Plan + Workers + Extra Storage)
            const userTotal = annual ? (usersCount * userPriceYearly) : (usersCount * userPriceMonthly);
            const storageTotal = annual ? (extraStorageGb * storagePriceYearly) : (extraStorageGb * storagePriceMonthly);
            const grandTotal = planPrice + userTotal + storageTotal;

            const modalTitle = document.getElementById("planModalTitle");
            const orderPlanName = document.getElementById("orderPlanName");
            const orderPlanProductId = document.getElementById("orderPlanProductId");
            const orderPlanSummary = document.getElementById("orderPlanSummary");
            const orderCycleNote = document.getElementById("orderCycleNote");

            if (modalTitle) modalTitle.textContent = "Deploy " + planName + " Plan";
            if (orderPlanName) orderPlanName.value = planName;
            if (orderPlanProductId) orderPlanProductId.value = planProductId;
            if (orderPlanSummary) {
                // Itemised summary: the plan, the workers and the extra storage each get
                // their own line, and the grand total is shown separately.
                const cycle = annual ? "year" : "month";
                const money = (value) => formatMoney(value) + " " + currencySymbol;
                const setLine = (id, text) => {
                    const node = document.getElementById(id);
                    if (node) {
                        node.textContent = text;
                    }
                };
                setLine("orderPlanLineLabel", planName + " Plan");
                setLine("orderPlanLinePrice", money(planPrice) + " / " + cycle);
                setLine("orderWorkerLineLabel",
                    usersCount + " " + (usersCount === 1 ? "Worker" : "Workers"));
                setLine("orderWorkerLinePrice", money(userTotal) + " / " + cycle);
                const storageRow = document.getElementById("orderStorageLineRow");
                if (extraStorageGb > 0) {
                    if (storageRow) {
                        storageRow.style.display = "flex";
                    }
                    setLine("orderStorageLineLabel", "Extra storage (+" + extraStorageGb + " GB)");
                    setLine("orderStorageLinePrice", money(storageTotal) + " / " + cycle);
                } else if (storageRow) {
                    storageRow.style.display = "none";
                }
                setLine("orderGrandTotal", money(grandTotal) + " / " + cycle);
            }
            if (orderCycleNote) {
                orderCycleNote.textContent = annual ? "Annual subscription · 15% discount included · Instant Setup" : "Monthly subscription · Cancel or upgrade anytime · Instant Setup";
            }
        };

        // Monthly / Annual toggle
        const billingBtns = document.querySelectorAll("[data-billing]");
        if (billingBtns.length) {
            billingBtns.forEach(button => {
                button.addEventListener("click", () => {
                    const annual = button.dataset.billing === "annual";
                    billingBtns.forEach(item => item.classList.toggle("active", item === button));

                    document.querySelectorAll("[data-monthly]").forEach(price => {
                        price.textContent = annual ? price.dataset.annual : price.dataset.monthly;
                    });
                    document.querySelectorAll("[data-period]").forEach(period => {
                        period.textContent = annual ? (currencySymbol + " / year") : (currencySymbol + " / month");
                    });
                    document.querySelectorAll(".annual-note").forEach(note => {
                        note.textContent = annual ? "15% annual discount applied" : "";
                    });

                    const orderPriceBy = document.getElementById("orderPriceBy");
                    if (orderPriceBy) orderPriceBy.value = annual ? "yearly" : "monthly";

                    updateModalPricing();
                });
            });
        }

        // Plan Choose button -> Open Modal
        document.querySelectorAll("[data-choose-plan]").forEach(btn => {
            btn.addEventListener("click", () => {
                document.querySelectorAll("[data-choose-plan]").forEach(b => b.classList.remove("selected"));
                btn.classList.add("selected");
                
                const baseStorage = parseInt(btn.dataset.baseStorage || (btn.dataset.choosePlan === "Growth" ? 20 : 5));
                const storageInput = document.getElementById("planStorageGb");
                if (storageInput) {
                    storageInput.min = baseStorage;
                    storageInput.value = baseStorage;
                }
                
                updateModalPricing();
                if (planModal) planModal.classList.add("show");
            });
        });

        // Cancel modal
        document.getElementById("cancelPlanModal")?.addEventListener("click", () => {
            if (planModal) planModal.classList.remove("show");
        });

        // Subdomain formatting and live check
        const subInput = document.getElementById("planSubDomain");
        const baseSelect = document.getElementById("planBaseDomain");
        const liveCheck = document.getElementById("subdomainLiveCheck");
        const submitOrderBtn = document.getElementById("submitOrderBtn");
        const startTrialBtn = document.getElementById("startTrialBtn");
        const planOrderForm = document.getElementById("planOrderForm");

        let checkSubdomainTimer = null;
        let isSubdomainAvailable = false;
        let lastCheckedSubdomain = "";

        const validateSubdomainAvailability = async () => {
            if (!subInput || !liveCheck) return;
            const rawVal = subInput.value.trim().toLowerCase();
            const baseId = baseSelect ? baseSelect.value : "1";
            const baseText = baseSelect ? (baseSelect.options[baseSelect.selectedIndex]?.text || "edc.nc") : "edc.nc";

            if (!rawVal) {
                liveCheck.innerHTML = '<span style="color:var(--muted)">Instance will be hosted at this URL.</span>';
                subInput.style.borderColor = "var(--line)";
                isSubdomainAvailable = false;
                if (submitOrderBtn) {
                    submitOrderBtn.disabled = false;
                    submitOrderBtn.style.opacity = "1";
                    submitOrderBtn.style.cursor = "pointer";
                }
                if (startTrialBtn) {
                    startTrialBtn.disabled = false;
                    startTrialBtn.style.opacity = "1";
                    startTrialBtn.style.cursor = "pointer";
                }
                return;
            }

            if (rawVal.length < 2) {
                liveCheck.innerHTML = '<span style="color:#ef4444;font-weight:700">Subdomain must be at least 2 characters.</span>';
                subInput.style.borderColor = "#ef4444";
                isSubdomainAvailable = false;
                if (submitOrderBtn) {
                    submitOrderBtn.disabled = true;
                    submitOrderBtn.style.opacity = "0.5";
                    submitOrderBtn.style.cursor = "not-allowed";
                }
                if (startTrialBtn) {
                    startTrialBtn.disabled = true;
                    startTrialBtn.style.opacity = "0.5";
                    startTrialBtn.style.cursor = "not-allowed";
                }
                return;
            }

            if (/^[0-9]/.test(rawVal)) {
                liveCheck.innerHTML = '<span style="color:#ef4444;font-weight:700">Subdomain cannot start with a number.</span>';
                subInput.style.borderColor = "#ef4444";
                isSubdomainAvailable = false;
                if (submitOrderBtn) {
                    submitOrderBtn.disabled = true;
                    submitOrderBtn.style.opacity = "0.5";
                    submitOrderBtn.style.cursor = "not-allowed";
                }
                if (startTrialBtn) {
                    startTrialBtn.disabled = true;
                    startTrialBtn.style.opacity = "0.5";
                    startTrialBtn.style.cursor = "not-allowed";
                }
                return;
            }

            liveCheck.innerHTML = `<span style="color:var(--muted)"><i class="fa fa-spinner fa-spin me-1"></i> Checking availability for ${rawVal}.${baseText}...</span>`;

            try {
                const res = await this.rpc("/pricing/check-domain", {
                    sub_domain: rawVal,
                    domain_id: parseInt(baseId) || 1
                });

                if (res && res.available) {
                    liveCheck.innerHTML = `<span style="color:#0fa8a0;font-weight:700"><i class="fa fa-check-circle me-1"></i> ${rawVal}.${baseText} is available!</span>`;
                    subInput.style.borderColor = "#0fa8a0";
                    isSubdomainAvailable = true;
                    lastCheckedSubdomain = rawVal;
                    if (submitOrderBtn) {
                        submitOrderBtn.disabled = false;
                        submitOrderBtn.style.opacity = "1";
                        submitOrderBtn.style.cursor = "pointer";
                    }
                    if (startTrialBtn) {
                        startTrialBtn.disabled = false;
                        startTrialBtn.style.opacity = "1";
                        startTrialBtn.style.cursor = "pointer";
                    }
                } else {
                    const errMsg = (res && res.error) ? res.error : `${rawVal} domain already taken`;
                    liveCheck.innerHTML = `<span style="color:#ef4444;font-weight:700"><i class="fa fa-times-circle me-1"></i> ${errMsg}</span>`;
                    subInput.style.borderColor = "#ef4444";
                    isSubdomainAvailable = false;
                    lastCheckedSubdomain = rawVal;
                    if (submitOrderBtn) {
                        submitOrderBtn.disabled = true;
                        submitOrderBtn.style.opacity = "0.5";
                        submitOrderBtn.style.cursor = "not-allowed";
                    }
                    if (startTrialBtn) {
                        startTrialBtn.disabled = true;
                        startTrialBtn.style.opacity = "0.5";
                        startTrialBtn.style.cursor = "not-allowed";
                    }
                }
            } catch (err) {
                console.warn("Subdomain check error:", err);
            }
        };

        if (subInput) {
            subInput.addEventListener("input", () => {
                subInput.value = subInput.value.toLowerCase().replace(/[^a-z0-9\-]/g, "");
                clearTimeout(checkSubdomainTimer);
                checkSubdomainTimer = setTimeout(validateSubdomainAvailability, 300);
            });
            subInput.addEventListener("blur", validateSubdomainAvailability);
        }

        if (baseSelect) {
            baseSelect.addEventListener("change", validateSubdomainAvailability);
        }

        // Check URL parameters for server redirect errors
        const urlParams = new URLSearchParams(window.location.search);
        const domainErrParam = urlParams.get("domain_error");
        const subdomainParam = urlParams.get("subdomain");
        if (domainErrParam) {
            notify(domainErrParam);
            if (planModal) {
                planModal.classList.add("show");
                if (subInput && subdomainParam) {
                    subInput.value = subdomainParam;
                    validateSubdomainAvailability();
                }
            }
        }

        // Form submission check
        if (planOrderForm) {
            planOrderForm.addEventListener("submit", async (e) => {
                const subDomain = subInput ? subInput.value.trim().toLowerCase() : "";
                if (!subDomain) {
                    e.preventDefault();
                    notify(_t("Please enter a subdomain first."));
                    subInput?.focus();
                    return;
                }

                if (!isSubdomainAvailable || lastCheckedSubdomain !== subDomain) {
                    e.preventDefault();
                    await validateSubdomainAvailability();
                    if (!isSubdomainAvailable) {
                        notify(`${subDomain} domain already taken. Please choose another one.`);
                        subInput?.focus();
                        return;
                    }
                    planOrderForm.submit();
                }
            });
        }

        // Workers increment / decrement buttons
        document.getElementById("btnMinusUser")?.addEventListener("click", () => {
            const countInput = document.getElementById("planUsersCount");
            if (countInput) {
                let val = parseInt(countInput.value) || 1;
                if (val > 1) {
                    countInput.value = val - 1;
                    updateModalPricing();
                }
            }
        });

        document.getElementById("btnPlusUser")?.addEventListener("click", () => {
            const countInput = document.getElementById("planUsersCount");
            if (countInput) {
                const max = parseInt(countInput.getAttribute("max")) || 4;
                countInput.value = Math.min((parseInt(countInput.value) || 1) + 1, max);
                updateModalPricing();
            }
        });

        document.getElementById("planUsersCount")?.addEventListener("input", (ev) => {
            const countInput = ev.currentTarget;
            const max = parseInt(countInput.getAttribute("max")) || 4;
            let val = parseInt(countInput.value) || 1;
            if (val > max) countInput.value = max;
            if (val < 1) countInput.value = 1;
            updateModalPricing();
        });

        // Storage increment / decrement buttons
        document.getElementById("btnMinusStorage")?.addEventListener("click", () => {
            const activePlanBtn = document.querySelector("[data-choose-plan].selected") || document.querySelector('[data-choose-plan="Standard"]');
            const planName = activePlanBtn ? activePlanBtn.dataset.choosePlan : "Standard";
            const baseStorage = parseInt(activePlanBtn?.dataset.baseStorage || (planName === "Growth" ? 20 : 5));
            const storageInput = document.getElementById("planStorageGb");
            if (storageInput) {
                let val = parseInt(storageInput.value) || baseStorage;
                if (val > baseStorage) {
                    storageInput.value = val - 1;
                    updateModalPricing();
                }
            }
        });

        document.getElementById("btnPlusStorage")?.addEventListener("click", () => {
            const activePlanBtn = document.querySelector("[data-choose-plan].selected") || document.querySelector('[data-choose-plan="Standard"]');
            const planName = activePlanBtn ? activePlanBtn.dataset.choosePlan : "Standard";
            const baseStorage = parseInt(activePlanBtn?.dataset.baseStorage || (planName === "Growth" ? 20 : 5));
            const storageInput = document.getElementById("planStorageGb");
            if (storageInput) {
                let val = parseInt(storageInput.value) || baseStorage;
                storageInput.value = val + 1;
                updateModalPricing();
            }
        });

        document.getElementById("planStorageGb")?.addEventListener("input", () => {
            updateModalPricing();
        });

        // Start 15 Days Free Trial with Animated Coffee Preloader & Congratulations Popup
        const trialBtn = document.getElementById("startTrialBtn");
        const deployOverlay = document.getElementById("saasDeployingOverlay");
        const congratsModal = document.getElementById("saasCongratsModal");
        const progressBarFill = document.getElementById("saasProgressBarFill");
        const progressPercent = document.getElementById("saasProgressPercent");
        const coffeeHeading = document.getElementById("saasCoffeeHeading");
        const coffeeMsg = document.getElementById("saasCoffeeMsg");
        const spinnerIcon = document.getElementById("saasSpinnerIcon");
        let trialSubmitting = false;

        if (trialBtn) {
            trialBtn.addEventListener("click", async () => {
                // Guard against double clicks creating two requests (which caused
                // the "subdomain already taken" duplicate-key errors).
                if (trialSubmitting) {
                    return;
                }
                const subDomain = document.getElementById("planSubDomain")?.value?.trim().toLowerCase();
                const baseDomainId = document.getElementById("planBaseDomain")?.value;
                const baseText = baseSelect ? (baseSelect.options[baseSelect.selectedIndex]?.text || "edc.nc") : "edc.nc";
                const usersCount = parseInt(document.getElementById("planUsersCount")?.value) || 1;
                const activePlanBtn = document.querySelector("[data-choose-plan].selected") || document.querySelector('[data-choose-plan="Standard"]');
                const planName = activePlanBtn ? activePlanBtn.dataset.choosePlan : "Standard";
                const baseStorage = parseInt(activePlanBtn?.dataset.baseStorage || (planName === "Growth" ? 20 : 5));
                const storageGb = parseInt(document.getElementById("planStorageGb")?.value) || baseStorage;
                // Version + edition picked in the same modal. They must be forwarded,
                // otherwise the backend falls back to its default server and the trial
                // always ends up being a plain Community instance.
                const versionSelect = document.getElementById("planOdooVersion");
                const odooVersionId = versionSelect && versionSelect.value ? parseInt(versionSelect.value) : null;
                const typeSelect = document.getElementById("planVersionType");
                const versionType = typeSelect && typeSelect.value ? typeSelect.value : "community";

                if (!subDomain) {
                    notify(_t("Please enter a subdomain first."));
                    subInput?.focus();
                    return;
                }

                if (!isSubdomainAvailable || lastCheckedSubdomain !== subDomain) {
                    await validateSubdomainAvailability();
                    if (!isSubdomainAvailable) {
                        notify(`${subDomain} domain already taken. Please choose another one.`);
                        subInput?.focus();
                        return;
                    }
                }

                // Close plan modal and show preloader overlay
                trialSubmitting = true;
                if (trialBtn) {
                    trialBtn.disabled = true;
                    trialBtn.style.opacity = "0.5";
                    trialBtn.style.cursor = "not-allowed";
                }
                if (planModal) planModal.classList.remove("show");
                if (deployOverlay) {
                    deployOverlay.style.display = "flex";
                }

                // Reset progress & steps
                let progress = 12;
                if (progressBarFill) progressBarFill.style.width = "12%";
                if (progressPercent) progressPercent.textContent = "12%";

                const setStepState = (stepId, state) => {
                    const el = document.getElementById(stepId);
                    if (!el) return;
                    const bullet = el.querySelector(".step-bullet");
                    el.classList.remove("active", "done");
                    if (state === "active") {
                        el.classList.add("active");
                        if (bullet) bullet.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
                    } else if (state === "done") {
                        el.classList.add("done");
                        if (bullet) bullet.innerHTML = '<i class="fa fa-check-circle"></i>';
                    } else {
                        if (bullet) bullet.innerHTML = '<i class="fa fa-circle"></i>';
                    }
                };

                setStepState("saasStep1", "active");
                setStepState("saasStep2", "pending");
                setStepState("saasStep3", "pending");
                setStepState("saasStep4", "pending");

                const coffeeQuotes = [
                    { heading: "Grab a coffee and relax! ☕", msg: "Your trial instance is loading and being set up...", icon: "☕" },
                    { heading: "Provisioning Docker Container 🚀", msg: "Setting up isolated container & Odoo environment...", icon: "⚙️" },
                    { heading: "Setting up PostgreSQL 🗄️", msg: "Creating database schema, tables & system views...", icon: "💾" },
                    { heading: "Configuring Network & Domain ⚡", msg: "Routing ports and configuring reverse proxy...", icon: "⚡" },
                    { heading: "Almost there! Sit tight ☕", msg: "Just a few moments left - your trial instance is about to launch!", icon: "☕" }
                ];
                let quoteIndex = 0;

                const progressInterval = setInterval(() => {
                    if (progress < 90) {
                        progress += Math.floor(Math.random() * 8) + 4;
                        if (progress > 90) progress = 90;
                        if (progressBarFill) progressBarFill.style.width = progress + "%";
                        if (progressPercent) progressPercent.textContent = progress + "%";

                        if (progress >= 28 && progress < 52) {
                            setStepState("saasStep1", "done");
                            setStepState("saasStep2", "active");
                        } else if (progress >= 52 && progress < 76) {
                            setStepState("saasStep2", "done");
                            setStepState("saasStep3", "active");
                        } else if (progress >= 76) {
                            setStepState("saasStep3", "done");
                            setStepState("saasStep4", "active");
                        }

                        quoteIndex = (quoteIndex + 1) % coffeeQuotes.length;
                        if (coffeeHeading) coffeeHeading.textContent = coffeeQuotes[quoteIndex].heading;
                        if (coffeeMsg) coffeeMsg.textContent = coffeeQuotes[quoteIndex].msg;
                        if (spinnerIcon) spinnerIcon.textContent = coffeeQuotes[quoteIndex].icon;
                    }
                }, 1400);

                try {
                    const res = await this.rpc("/saas/instance/create-trial", {
                        instance_vals: {
                            base_domain_id: parseInt(baseDomainId) || 1,
                            sub_domain: subDomain,
                            plan: planName,
                            users_count: usersCount,
                            storage_gb: storageGb,
                            // Forward the version + edition chosen in the modal so the trial
                            // is created on the matching server (Community or Enterprise).
                            odoo_version_id: odooVersionId,
                            version_type: versionType,
                            default_app_ids: []
                        }
                    });

                    clearInterval(progressInterval);
                    trialSubmitting = false;
                    if (trialBtn) {
                        trialBtn.disabled = false;
                        trialBtn.style.opacity = "1";
                        trialBtn.style.cursor = "pointer";
                    }

                    if (res && res.id) {
                        // Complete progress
                        if (progressBarFill) progressBarFill.style.width = "100%";
                        if (progressPercent) progressPercent.textContent = "100%";
                        setStepState("saasStep1", "done");
                        setStepState("saasStep2", "done");
                        setStepState("saasStep3", "done");
                        setStepState("saasStep4", "done");
                        if (coffeeHeading) coffeeHeading.textContent = "Instance Created! 🎉";
                        if (coffeeMsg) coffeeMsg.textContent = "Opening your celebration dashboard...";

                        setTimeout(() => {
                            if (deployOverlay) deployOverlay.style.display = "none";

                            // Populate congrats modal
                            const domainDisplay = document.getElementById("congratsInstanceDomain");
                            const expiryDisplay = document.getElementById("congratsExpiryDate");
                            const btnOpenInstance = document.getElementById("btnOpenTrialInstance");
                            const btnGoDashboard = document.getElementById("btnGoToDashboard");

                            const fullDomain = res.domain_name || res.url || (subDomain + "." + baseText);
                            if (domainDisplay) domainDisplay.textContent = fullDomain;
                            if (expiryDisplay) expiryDisplay.textContent = res.expiration_date || "15 Days Free Trial";
                            if (btnOpenInstance) {
                                btnOpenInstance.href = res.url || ("https://" + fullDomain);
                            }
                            if (btnGoDashboard) {
                                btnGoDashboard.href = "/my/saas/odoo-instance/" + res.id;
                            }

                            if (congratsModal) congratsModal.classList.add("show");
                        }, 700);
                    } else {
                        if (deployOverlay) deployOverlay.style.display = "none";
                        notify("Failed to create trial: " + (res?.error || "Unknown error"));
                        if (planModal) planModal.classList.add("show");
                    }
                } catch (e) {
                    clearInterval(progressInterval);
                    trialSubmitting = false;
                    if (trialBtn) {
                        trialBtn.disabled = false;
                        trialBtn.style.opacity = "1";
                        trialBtn.style.cursor = "pointer";
                    }
                    if (deployOverlay) deployOverlay.style.display = "none";
                    // The session may have expired between opening the wizard and
                    // submitting it: ask for an account instead of surfacing a
                    // raw technical error.
                    if (!SaaSAuth.isAuthenticated() || SaaSAuth.isAuthenticationError(e)) {
                        SaaSAuth.openAccountRequiredModal({
                            action: "trial",
                            form: planOrderForm,
                        });
                    } else {
                        notify("Trial error: " + (e.message || "Please check connection or sign in first"));
                    }
                    if (planModal) planModal.classList.add("show");
                }
            });
        }

        // A visitor who clicked "Start Free Trial" / "Order & Deploy" was sent
        // to sign in or create an account. Once back and logged in, restore the
        // exact plan configuration so nothing has to be entered again.
        document.addEventListener("saas:auth-action-restored", (ev) => {
            if (!planModal) {
                return;
            }
            const fields = (ev.detail && ev.detail.fields) || {};
            // Selecting the plan resets users/storage fields, so do it first.
            if (fields.plan === "Standard" || fields.plan === "Essential" || fields.plan === "Growth") {
                const planBtn = document.querySelector(`[data-choose-plan="${fields.plan}"]`);
                if (planBtn && !planBtn.classList.contains("selected")) {
                    planBtn.click();
                }
            }
            // Sync the monthly/annual toggle with the remembered billing cycle.
            const cycle = fields.price_by === "yearly" ? "annual" : "monthly";
            const billingBtn = document.querySelector(`[data-billing="${cycle}"]`);
            if (billingBtn && !billingBtn.classList.contains("active")) {
                billingBtn.click();
            }
            updateModalPricing();
            validateSubdomainAvailability();
            planModal.classList.add("show");
            notify(_t("Welcome back! Your plan configuration has been restored."));
        });

        SaaSAuth.restorePendingAction();
    },

    _initRenewModal() {
        const renewModal = document.getElementById("renewInstanceModal");
        if (!renewModal) return;

        const openBtn = document.getElementById("btnOpenRenewModal");
        const openBtnHeader = document.getElementById("btnOpenRenewModalHeader");
        const closeBtn = document.getElementById("btnCloseRenewModal");
        const cancelBtn = document.getElementById("renewCancelBtn");

        const showModal = () => renewModal.classList.add("show");
        const hideModal = () => renewModal.classList.remove("show");

        openBtn?.addEventListener("click", showModal);
        openBtnHeader?.addEventListener("click", showModal);
        closeBtn?.addEventListener("click", hideModal);
        cancelBtn?.addEventListener("click", hideModal);

        // Prices from data attributes
        const userPriceMonthly = parseFloat(renewModal.dataset.userPriceMonthly || 100);
        const userPriceYearly = parseFloat(renewModal.dataset.userPriceYearly || 1020);
        const userPriceMonthlyFmt = renewModal.dataset.userPriceMonthlyFmt || "100";
        const userPriceYearlyFmt = renewModal.dataset.userPriceYearlyFmt || "1,020";

        const storagePriceMonthly = parseFloat(renewModal.dataset.storagePriceMonthly || 220);
        const storagePriceYearly = parseFloat(renewModal.dataset.storagePriceYearly || 2244);
        const storagePriceMonthlyFmt = renewModal.dataset.storagePriceMonthlyFmt || "220";
        const storagePriceYearlyFmt = renewModal.dataset.storagePriceYearlyFmt || "2,244";

        const essentialMonthly = parseFloat(renewModal.dataset.essentialMonthly || 14900);
        const essentialAnnual = parseFloat(renewModal.dataset.essentialAnnual || 151980);
        const growthMonthly = parseFloat(renewModal.dataset.growthMonthly || 39900);
        const growthAnnual = parseFloat(renewModal.dataset.growthAnnual || 406980);

        // Currency symbol is provided by the server (company currency, e.g. €).
        const currencySymbol = renewModal.dataset.currencySymbol || "";

        const formatMoney = (val) => {
            if (val === null || val === undefined || isNaN(val)) return "0";
            const num = parseFloat(val);
            if (Number.isInteger(num)) return num.toLocaleString();
            return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        };

        const updateRenewPricing = () => {
            const priceBy = document.getElementById("renewPriceBy")?.value || "monthly";
            const isAnnual = (priceBy === "yearly");

            const activePlanCard = document.querySelector(".renew-plan-card.active") || document.getElementById("renewPlanCardEssential");
            const planName = activePlanCard?.dataset.plan || "Standard";
            const planProductId = activePlanCard?.dataset.productId || "";
            const baseStorage = parseInt(activePlanCard?.dataset.baseStorage || (planName === "Growth" ? 20 : 5));

            const planBasePrice = isAnnual ? (planName === "Growth" ? growthAnnual : essentialAnnual) : (planName === "Growth" ? growthMonthly : essentialMonthly);

            // Users count
            const usersInput = document.getElementById("renewUsersCount");
            let usersCount = usersInput ? (parseInt(usersInput.value) || 1) : 1;
            if (usersCount < 1) usersCount = 1;

            // Storage count
            const storageInput = document.getElementById("renewStorageGb");
            if (storageInput) {
                storageInput.min = baseStorage;
                let curStorage = parseInt(storageInput.value) || baseStorage;
                if (curStorage < baseStorage) {
                    curStorage = baseStorage;
                    storageInput.value = baseStorage;
                }
            }
            let storageGb = storageInput ? (parseInt(storageInput.value) || baseStorage) : baseStorage;
            if (storageGb < baseStorage) storageGb = baseStorage;
            const extraStorageGb = Math.max(0, storageGb - baseStorage);

            // Dynamic user & storage unit total
            const userTotal = isAnnual ? (usersCount * userPriceYearly) : (usersCount * userPriceMonthly);
            const storageTotal = isAnnual ? (extraStorageGb * storagePriceYearly) : (extraStorageGb * storagePriceMonthly);
            const grandTotal = planBasePrice + userTotal + storageTotal;

            // Update DOM
            const planNameInput = document.getElementById("renewPlanName");
            const planProductInput = document.getElementById("renewPlanProductId");
            if (planNameInput) planNameInput.value = planName;
            if (planProductInput) planProductInput.value = planProductId;

            const grandTotalDisplay = document.getElementById("renewGrandTotalDisplay");
            if (grandTotalDisplay) {
                grandTotalDisplay.textContent = formatMoney(grandTotal) + " " + currencySymbol + " / " + (isAnnual ? "year" : "month");
            }

            const cycleBadge = document.getElementById("renewSummaryCycleBadge");
            if (cycleBadge) {
                cycleBadge.textContent = isAnnual ? "Annual" : "Monthly";
            }

            const cycleNote = document.getElementById("renewSummaryNote");
            if (cycleNote) {
                cycleNote.textContent = isAnnual
                    ? "Billed annually · the 15% discount applies to the plan only"
                    : "Cancel or modify anytime";
            }

            // Plan card price previews
            const essDisplay = document.getElementById("renewEssPriceDisplay");
            if (essDisplay) {
                essDisplay.textContent = formatMoney(isAnnual ? essentialAnnual : essentialMonthly) + " " + currencySymbol + (isAnnual ? " / yr" : " / mo");
            }
            const groDisplay = document.getElementById("renewGroPriceDisplay");
            if (groDisplay) {
                groDisplay.textContent = formatMoney(isAnnual ? growthAnnual : growthMonthly) + " " + currencySymbol + (isAnnual ? " / yr" : " / mo");
            }

            // Worker helper text
            const userDisplay = document.getElementById("renewUserPriceDisplay");
            if (userDisplay) {
                const priceFmt = isAnnual ? userPriceYearlyFmt : userPriceMonthlyFmt;
                userDisplay.textContent = priceFmt + " " + currencySymbol + " / worker / " + (isAnnual ? "year" : "month");
            }

            // Storage helper text
            const storageDisplay = document.getElementById("renewStoragePriceDisplay");
            if (storageDisplay) {
                const storageFmt = isAnnual ? storagePriceYearlyFmt : storagePriceMonthlyFmt;
                storageDisplay.textContent = baseStorage + " GB included · + " + storageFmt + " " + currencySymbol + " / extra GB / " + (isAnnual ? "year" : "month");
            }
        };

        // Billing Cycle Buttons
        const btnMonthly = document.getElementById("renewBillingMonthly");
        const btnAnnual = document.getElementById("renewBillingAnnual");
        const priceByInput = document.getElementById("renewPriceBy");

        btnMonthly?.addEventListener("click", () => {
            btnMonthly.classList.add("active");
            btnAnnual?.classList.remove("active");
            if (priceByInput) priceByInput.value = "monthly";
            updateRenewPricing();
        });

        btnAnnual?.addEventListener("click", () => {
            btnAnnual.classList.add("active");
            btnMonthly?.classList.remove("active");
            if (priceByInput) priceByInput.value = "yearly";
            updateRenewPricing();
        });

        // Plan Selection Cards
        document.querySelectorAll(".renew-plan-card").forEach(card => {
            card.addEventListener("click", () => {
                document.querySelectorAll(".renew-plan-card").forEach(c => c.classList.remove("active"));
                card.classList.add("active");
                const baseStorage = parseInt(card.dataset.baseStorage || 5);
                const storageInput = document.getElementById("renewStorageGb");
                if (storageInput) {
                    storageInput.min = baseStorage;
                    if (parseInt(storageInput.value) < baseStorage) {
                        storageInput.value = baseStorage;
                    }
                }
                updateRenewPricing();
            });
        });

        // Workers increment/decrement
        document.getElementById("renewBtnMinusUser")?.addEventListener("click", () => {
            const input = document.getElementById("renewUsersCount");
            if (input) {
                let val = parseInt(input.value) || 1;
                if (val > 1) {
                    input.value = val - 1;
                    updateRenewPricing();
                }
            }
        });

        document.getElementById("renewBtnPlusUser")?.addEventListener("click", () => {
            const input = document.getElementById("renewUsersCount");
            if (input) {
                const max = parseInt(input.getAttribute("max")) || 4;
                input.value = Math.min((parseInt(input.value) || 1) + 1, max);
                updateRenewPricing();
            }
        });

        document.getElementById("renewUsersCount")?.addEventListener("input", (ev) => {
            const input = ev.currentTarget;
            const max = parseInt(input.getAttribute("max")) || 4;
            let val = parseInt(input.value) || 1;
            if (val > max) input.value = max;
            if (val < 1) input.value = 1;
            updateRenewPricing();
        });

        // Storage increment/decrement
        document.getElementById("renewBtnMinusStorage")?.addEventListener("click", () => {
            const activePlanCard = document.querySelector(".renew-plan-card.active");
            const baseStorage = parseInt(activePlanCard?.dataset.baseStorage || 5);
            const input = document.getElementById("renewStorageGb");
            if (input) {
                let val = parseInt(input.value) || baseStorage;
                if (val > baseStorage) {
                    input.value = val - 1;
                    updateRenewPricing();
                }
            }
        });

        document.getElementById("renewBtnPlusStorage")?.addEventListener("click", () => {
            const activePlanCard = document.querySelector(".renew-plan-card.active");
            const baseStorage = parseInt(activePlanCard?.dataset.baseStorage || 5);
            const input = document.getElementById("renewStorageGb");
            if (input) {
                input.value = (parseInt(input.value) || baseStorage) + 1;
                updateRenewPricing();
            }
        });

        document.getElementById("renewStorageGb")?.addEventListener("input", updateRenewPricing);

        // Form Submit
        document.getElementById("renewOrderForm")?.addEventListener("submit", () => {
            notify(_t("Redirecting to checkout cart..."));
        });

        // Initial calculation
        updateRenewPricing();
    },

    _initStorageUpgrade() {
        const section = document.getElementById("storage");
        if (!section) return;
        const instanceId = this._getInstanceId();
        if (!instanceId) return;

        const pricePerGb = parseFloat(section.dataset.storagePriceMonthly || "220") || 220;
        const currency = section.dataset.currencySymbol || "";

        const fmtMoney = (val) => {
            const rounded = Math.round(val * 100) / 100;
            return Number.isInteger(rounded)
                ? rounded.toLocaleString("en-US")
                : rounded.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        };

        const cards = Array.from(section.querySelectorAll(".storage-pkg-card"));
        const customInput = document.getElementById("storageCustomGb");
        const summaryGb = document.getElementById("storageSummaryGb");
        const summaryTotal = document.getElementById("storageSummaryTotal");
        const customPrice = document.getElementById("storageCustomPriceDisplay");
        const proceedBtn = document.getElementById("btnProceedStorageUpgrade");

        let selectedGb = customInput ? (parseInt(customInput.value) || 5) : 5;
        if (cards.length) {
            selectedGb = parseInt(cards[0].dataset.gb) || selectedGb;
        }

        const updateSummary = () => {
            const total = pricePerGb * selectedGb;
            if (summaryGb) summaryGb.textContent = selectedGb + " GB";
            if (summaryTotal) summaryTotal.textContent = fmtMoney(total) + " " + currency;
            if (customPrice) customPrice.textContent = fmtMoney(total) + " " + currency;
        };

        const applySelection = (gb) => {
            selectedGb = Math.max(1, Math.min(parseInt(gb) || 1, 500));
            cards.forEach(card => {
                const isActive = parseInt(card.dataset.gb) === selectedGb;
                card.style.border = isActive ? "2px solid var(--teal)" : "1.5px solid var(--line)";
                card.style.background = isActive ? "rgba(15,168,160,0.06)" : "rgba(255,255,255,0.7)";
                card.style.transform = isActive ? "translateY(-2px)" : "none";
            });
            if (customInput) customInput.value = selectedGb;
            updateSummary();
        };

        cards.forEach(card => {
            card.addEventListener("click", () => applySelection(card.dataset.gb));
        });

        const stepSelection = (delta) => {
            const current = parseInt(customInput?.value) || selectedGb;
            applySelection(current + delta);
        };
        document.getElementById("storageBtnMinus")?.addEventListener("click", () => stepSelection(-1));
        document.getElementById("storageBtnPlus")?.addEventListener("click", () => stepSelection(1));
        customInput?.addEventListener("input", () => applySelection(customInput.value));

        applySelection(selectedGb);

        proceedBtn?.addEventListener("click", async () => {
            if (proceedBtn.disabled) return;
            proceedBtn.disabled = true;
            const originalHtml = proceedBtn.innerHTML;
            proceedBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Creating your storage order...';
            try {
                const res = await this.rpc("/saas/instance/buy-storage", {
                    instance_id: instanceId,
                    additional_gb: selectedGb,
                    subscription_type: "monthly",
                });
                if (res && res.success && res.redirect) {
                    notify(_t("Redirecting to checkout - only your storage upgrade is charged..."));
                    window.location.href = res.redirect;
                    return;
                }
                notify((res && res.error) || "Could not start the storage upgrade.");
            } catch (err) {
                notify("Storage upgrade error: " + (err && err.message ? err.message : err));
            }
            proceedBtn.disabled = false;
            proceedBtn.innerHTML = originalHtml;
        });
    },

    _initWorkersUpgrade() {
        const modal = document.getElementById("workersUpgradeModal");
        if (!modal) return;
        const card = modal.querySelector(".modal-card");
        const instanceId = parseInt(card?.dataset.instanceId) || this._getInstanceId();
        if (!instanceId) return;

        const priceMonthly = parseFloat(card?.dataset.workerPriceMonthly || "100") || 100;
        const priceFmt = card?.dataset.workerPriceFormatted || "100";
        const currency = card?.dataset.currencySymbol || "";
        const currentWorkers = parseInt(card?.dataset.currentWorkers)
            || parseInt(modal.querySelector("input[readonly]")?.value) || 1;
        const maxWorkers = parseInt(card?.dataset.maxWorkers) || 4;
        const remaining = Math.max(0, maxWorkers - currentWorkers);

        const countInput = document.getElementById("workersAddCount");
        const summaryTotal = document.getElementById("workersSummaryTotal");
        const summaryAmount = document.getElementById("workersSummaryAmount");
        const priceDisplay = document.getElementById("workersPriceDisplay");
        const limitNote = document.getElementById("workersLimitNote");
        const proceedBtn = document.getElementById("btnProceedWorkersUpgrade");

        const fmtMoney = (val) => {
            const rounded = Math.round(val * 100) / 100;
            return Number.isInteger(rounded)
                ? rounded.toLocaleString("en-US")
                : rounded.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        };

        const close = () => modal.classList.remove("show");
        const update = () => {
            let add = parseInt(countInput?.value) || 1;
            add = Math.max(1, Math.min(add, Math.max(remaining, 1)));
            if (countInput) countInput.value = add;
            const total = currentWorkers + add;
            if (summaryTotal) summaryTotal.textContent = currentWorkers + " → " + total + " workers";
            if (summaryAmount) summaryAmount.textContent = fmtMoney(priceMonthly * add) + " " + currency + " / month";
            if (priceDisplay) priceDisplay.textContent = priceFmt + " " + currency + " / worker / month";
            if (limitNote) {
                limitNote.textContent = remaining > 0
                    ? "You can add up to " + remaining + " more worker" + (remaining > 1 ? "s" : "")
                        + " (maximum " + maxWorkers + " per instance)."
                    : "This instance already runs the maximum of " + maxWorkers + " workers.";
            }
            if (proceedBtn) {
                proceedBtn.disabled = remaining <= 0;
                proceedBtn.style.opacity = remaining <= 0 ? "0.55" : "1";
                proceedBtn.style.cursor = remaining <= 0 ? "not-allowed" : "pointer";
            }
        };

        countInput?.addEventListener("input", update);
        document.getElementById("workersBtnMinus")?.addEventListener("click", () => {
            if (countInput) countInput.value = (parseInt(countInput.value) || 1) - 1;
            update();
        });
        document.getElementById("workersBtnPlus")?.addEventListener("click", () => {
            if (countInput) countInput.value = (parseInt(countInput.value) || 1) + 1;
            update();
        });
        document.getElementById("workersUpgradeCancel")?.addEventListener("click", close);
        modal.addEventListener("click", (ev) => { if (ev.target === modal) close(); });

        update();

        proceedBtn?.addEventListener("click", async () => {
            if (proceedBtn.disabled || remaining <= 0) return;
            const add = Math.max(1, Math.min(parseInt(countInput?.value) || 1, remaining));
            proceedBtn.disabled = true;
            const originalHtml = proceedBtn.innerHTML;
            proceedBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Creating your workers order...';
            try {
                const res = await this.rpc("/saas/instance/buy-workers", {
                    instance_id: instanceId,
                    additional_workers: add,
                    subscription_type: "monthly",
                });
                if (res && res.success && res.redirect) {
                    notify(_t("Redirecting to checkout - only the extra workers are charged..."));
                    window.location.href = res.redirect;
                    return;
                }
                notify((res && res.error) || "Could not start the workers upgrade.");
            } catch (err) {
                notify("Workers upgrade error: " + (err && err.message ? err.message : err));
            }
            update();
            proceedBtn.innerHTML = originalHtml;
        });
    },

    async _runWorkersUpgradeFlow(orderIdOverride) {
        const urlParams = new URLSearchParams(window.location.search);
        const orderId = orderIdOverride || parseInt(urlParams.get("order_id")) || false;
        const instanceId = this._getInstanceId();

        const overlay = document.getElementById("saasWorkersOverlay");
        const progressFill = document.getElementById("saasWorkersProgressFill");
        const successModal = document.getElementById("saasWorkersSuccessModal");
        const newCountEl = document.getElementById("saasWorkersNewCount");

        const markDone = (id) => {
            const step = document.getElementById(id);
            if (!step) return;
            step.classList.remove("active");
            step.classList.add("done");
            const bullet = step.querySelector(".step-bullet");
            if (bullet) bullet.innerHTML = '<i class="fa fa-check-circle"></i>';
        };
        const markActive = (id) => {
            const step = document.getElementById(id);
            if (!step) return;
            step.classList.add("active");
            const bullet = step.querySelector(".step-bullet");
            if (bullet) bullet.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
        };

        if (overlay) overlay.style.display = "flex";
        setTimeout(() => {
            if (progressFill) progressFill.style.width = "55%";
            markDone("workersStep1");
            markActive("workersStep2");
        }, 500);

        let status = null;
        try {
            status = await this.rpc("/saas/instance/workers-upgrade-status", {
                instance_id: instanceId || undefined,
                order_id: orderId || undefined,
            });
        } catch (err) {
            status = { success: false, error: (err && err.message) || String(err) };
        }

        setTimeout(() => {
            if (progressFill) progressFill.style.width = "85%";
            markDone("workersStep2");
            markActive("workersStep3");
        }, 1100);

        setTimeout(() => {
            if (progressFill) progressFill.style.width = "100%";
            markDone("workersStep3");
            setTimeout(() => {
                if (overlay) overlay.style.display = "none";
                if (status && status.success) {
                    if (newCountEl) newCountEl.textContent = parseInt(status.workers_count || 0);
                    if (successModal) successModal.classList.add("show");
                } else {
                    notify((status && status.error) || "We could not confirm your workers upgrade. Please contact support.");
                    if (successModal) successModal.classList.add("show");
                }
                window.history.replaceState({}, document.title, window.location.pathname);
            }, 400);
        }, 1700);

        document.getElementById("btnCloseWorkersSuccess")?.addEventListener("click", () => {
            window.location.reload();
        });
    },

    async _runStorageUpgradeFlow(orderIdOverride) {
        const urlParams = new URLSearchParams(window.location.search);
        const orderId = orderIdOverride || parseInt(urlParams.get("order_id")) || false;
        const instanceId = this._getInstanceId();

        const overlay = document.getElementById("saasStorageOverlay");
        const progressFill = document.getElementById("saasStorageProgressFill");
        const successModal = document.getElementById("saasStorageSuccessModal");
        const newLimitEl = document.getElementById("saasStorageNewLimit");
        const resumeNote = document.getElementById("saasStorageResumeNote");

        const markDone = (id) => {
            const step = document.getElementById(id);
            if (!step) return;
            step.classList.remove("active");
            step.classList.add("done");
            const bullet = step.querySelector(".step-bullet");
            if (bullet) bullet.innerHTML = '<i class="fa fa-check-circle"></i>';
        };
        const markActive = (id) => {
            const step = document.getElementById(id);
            if (!step) return;
            step.classList.add("active");
            const bullet = step.querySelector(".step-bullet");
            if (bullet) bullet.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
        };

        if (overlay) overlay.style.display = "flex";
        setTimeout(() => {
            if (progressFill) progressFill.style.width = "55%";
            markDone("storageStep1");
            markActive("storageStep2");
        }, 500);

        let status = null;
        try {
            status = await this.rpc("/saas/instance/storage-upgrade-status", {
                instance_id: instanceId || undefined,
                order_id: orderId || undefined,
            });
        } catch (err) {
            status = { success: false, error: (err && err.message) || String(err) };
        }

        setTimeout(() => {
            if (progressFill) progressFill.style.width = "85%";
            markDone("storageStep2");
            markActive("storageStep3");
        }, 1100);

        setTimeout(() => {
            if (progressFill) progressFill.style.width = "100%";
            markDone("storageStep3");
            setTimeout(() => {
                if (overlay) overlay.style.display = "none";
                if (status && status.success) {
                    const newLimit = parseFloat(status.storage_limit_gb || 0);
                    if (newLimitEl) newLimitEl.textContent = newLimit.toFixed(0);
                    if (resumeNote) resumeNote.style.display = status.resumed ? "block" : "none";
                    if (successModal) successModal.classList.add("show");
                } else {
                    notify((status && status.error) || "We could not confirm your storage upgrade. Please contact support.");
                    if (successModal) successModal.classList.add("show");
                }
                // Clean the URL so refreshing the page never replays the upgrade.
                window.history.replaceState({}, document.title, window.location.pathname);
            }, 400);
        }, 1700);

        document.getElementById("btnCloseStorageSuccess")?.addEventListener("click", () => {
            // Reload so every storage widget reflects the new limit immediately.
            window.location.reload();
        });
    },

    _initDeploymentFlow() {
        const urlParams = new URLSearchParams(window.location.search);
        const flagEl = document.getElementById("o_instance_deploying");
        const stateEl = document.getElementById("o_deployment_state");
        const urlFlag = urlParams.get("deploying") === "1";
        const flagOn = flagEl ? (flagEl.value || "") !== "" : false;
        if (!urlFlag && !flagOn) {
            return;
        }
        this._runInstanceDeploymentFlow(stateEl ? stateEl.value : "idle");
    },

    _runInstanceDeploymentFlow(initialState) {
        const overlay = document.getElementById("saasInstanceDeployOverlay");
        if (!overlay) {
            return;
        }
        const instanceId = this._getInstanceId();
        const progressFill = document.getElementById("saasInstanceDeployProgressFill");
        const percentEl = document.getElementById("saasInstanceDeployPercent");
        const elapsedEl = document.getElementById("saasInstanceDeployElapsed");
        const vibeMsg = document.getElementById("saasDeployVibeMessage");
        const errorBox = document.getElementById("saasDeployErrorBox");
        const errorText = document.getElementById("saasDeployErrorText");
        const retryBtn = document.getElementById("btnRetryDeploy");
        const steps = [1, 2, 3, 4, 5].map((n) => document.getElementById("deployStep" + n));

        const messages = [
            "Sit back and relax — we are preparing your brand new workspace.",
            "Provisioning your Docker container... this is the fun part 🐳",
            "Spinning up PostgreSQL and wiring the databases 🗄️",
            "Booting Odoo and warming up the services ⚙️",
            "Polishing the final touches — almost ready ✨",
        ];

        overlay.style.display = "flex";
        document.body.style.overflow = "hidden";

        let done = false;
        let startedAt = Date.now();
        let fakeProgress = 6;

        const setStep = (index, finished) => {
            steps.forEach((el, i) => {
                if (!el) return;
                el.classList.toggle("active", i === index && !finished);
                el.classList.toggle("done", i < index || finished);
                const bullet = el.querySelector(".step-bullet");
                if (!bullet) return;
                if (i < index || finished) {
                    bullet.innerHTML = '<i class="fa fa-check"></i>';
                } else if (i === index) {
                    bullet.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
                } else {
                    bullet.innerHTML = '<i class="fa fa-circle"></i>';
                }
            });
        };

        let ticker = null;
        const startTicker = () => {
            clearInterval(ticker);
            startedAt = Date.now();
            ticker = setInterval(() => {
                if (done) return;
                const elapsed = Math.round((Date.now() - startedAt) / 1000);
                if (elapsedEl) elapsedEl.textContent = elapsed + "s";
                fakeProgress = Math.min(92, fakeProgress + Math.max(0.6, (92 - fakeProgress) * 0.06));
                const pct = Math.round(fakeProgress);
                if (progressFill) progressFill.style.width = pct + "%";
                if (percentEl) percentEl.textContent = pct + "%";
                const phase = Math.min(4, Math.floor(elapsed / 18) + 1);
                setStep(Math.max(1, phase), false);
                if (vibeMsg) vibeMsg.textContent = messages[Math.min(messages.length - 1, phase)];
            }, 1000);
        };

        const poll = async () => {
            if (done) return;
            try {
                const res = await this.rpc("/saas/instance/deployment-status", { instance_id: instanceId });
                if (!res || !res.success) {
                    setTimeout(poll, 4000);
                    return;
                }
                if (res.deployment_state === "deployed" || (res.state === "deploy" && res.operation_state === "run")) {
                    done = true;
                    clearInterval(ticker);
                    if (progressFill) progressFill.style.width = "100%";
                    if (percentEl) percentEl.textContent = "100%";
                    if (elapsedEl) elapsedEl.textContent = "ready 🎉";
                    setStep(5, true);
                    if (vibeMsg) vibeMsg.textContent = "All set! Your instance is live 🚀";
                    setTimeout(() => this._showInstanceReady(), 750);
                    return;
                }
                if (res.deployment_state === "failed") {
                    done = true;
                    clearInterval(ticker);
                    if (errorBox) errorBox.style.display = "block";
                    if (errorText) errorText.textContent = res.error || "Deployment failed. Please retry.";
                    return;
                }
                setTimeout(poll, 4000);
            } catch (err) {
                setTimeout(poll, 5000);
            }
        };

        retryBtn?.addEventListener("click", async () => {
            retryBtn.disabled = true;
            if (errorBox) errorBox.style.display = "none";
            try {
                await this.rpc("/saas/instance/deployment-status", { instance_id: instanceId, retry: true });
            } catch (err) {
                /* ignore, we reload anyway */
            }
            window.location.reload();
        });

        if (initialState === "failed") {
            done = true;
            if (errorBox) errorBox.style.display = "block";
            if (errorText) errorText.textContent = "The previous deployment attempt did not finish. You can retry safely.";
        } else {
            if (initialState === "deploying") {
                fakeProgress = 35;
                setStep(3, false);
            }
            startTicker();
            poll();
        }
    },

    _showInstanceReady() {
        const overlay = document.getElementById("saasInstanceDeployOverlay");
        if (overlay) overlay.style.display = "none";
        document.body.style.overflow = "";

        const modal = document.getElementById("saasInstanceReadyModal");
        if (!modal) return;
        const nameEl = document.getElementById("o_instance_name");
        const readyName = document.getElementById("saasInstanceReadyName");
        if (readyName && nameEl && nameEl.value) {
            readyName.textContent = "Welcome aboard, " + nameEl.value + "! 🚀";
        }
        this._fireInstanceConfetti();
        modal.classList.add("show");

        document.getElementById("btnCloseInstanceReady")?.addEventListener("click", () => {
            modal.classList.remove("show");
            const url = new URL(window.location.href);
            url.searchParams.delete("deploying");
            window.location.href = url.toString();
        });
    },

    _fireInstanceConfetti() {
        const host = document.getElementById("saasInstanceConfetti");
        if (!host) return;
        const colors = ["#6366f1", "#a855f7", "#ec4899", "#22d3ee", "#f59e0b", "#10b981"];
        host.innerHTML = "";
        for (let i = 0; i < 70; i++) {
            const piece = document.createElement("span");
            piece.className = "saas-confetti-piece";
            piece.style.left = Math.random() * 100 + "%";
            piece.style.background = colors[i % colors.length];
            piece.style.animationDelay = (Math.random() * 0.9).toFixed(2) + "s";
            piece.style.animationDuration = (2.2 + Math.random() * 1.6).toFixed(2) + "s";
            piece.style.setProperty("--drift", (Math.random() * 140 - 70).toFixed(0) + "px");
            host.appendChild(piece);
        }
        setTimeout(() => {
            host.innerHTML = "";
        }, 5200);
    },

    _initPostPaymentFlow() {
        const urlParams = new URLSearchParams(window.location.search);
        const isPaymentSuccess = urlParams.get("payment_success") === "1";
        const isRenewed = urlParams.get("renewed") === "1";
        const pendingEl = document.getElementById("o_pending_storage_order");
        const pendingOrderId = pendingEl ? (parseInt(pendingEl.value) || 0) : 0;
        const isStorageExtended = urlParams.get("storage_extended") === "1" || !!pendingOrderId;

        const workersFlag = document.getElementById("workersUpgradedFlag");
        const isWorkersUpgraded = urlParams.get("workers_upgraded") === "1" || !!workersFlag;
        if (isWorkersUpgraded) {
            const wOrderId = workersFlag
                ? (parseInt(workersFlag.dataset.orderId) || 0)
                : (parseInt(urlParams.get("order_id")) || 0);
            this._runWorkersUpgradeFlow(wOrderId || undefined);
            return;
        }

        if (isStorageExtended) {
            this._runStorageUpgradeFlow(pendingOrderId || undefined);
            return;
        }

        if (isPaymentSuccess || isRenewed) {
            const overlay = document.getElementById("saasRenewalOverlay");
            const congratsModal = document.getElementById("saasRenewCongratsModal");
            const progressFill = document.getElementById("saasRenewProgressFill");
            const closeBtn = document.getElementById("btnCloseRenewCongratsModal");

            closeBtn?.addEventListener("click", () => {
                if (congratsModal) congratsModal.classList.remove("show");
            });

            if (overlay) {
                overlay.style.display = "flex";

                const step1 = document.getElementById("renewStep1");
                const step2 = document.getElementById("renewStep2");
                const step3 = document.getElementById("renewStep3");

                const markDone = (step) => {
                    if (!step) return;
                    step.classList.remove("active");
                    step.classList.add("done");
                    const b = step.querySelector(".step-bullet");
                    if (b) b.innerHTML = '<i class="fa fa-check-circle"></i>';
                };

                const markActive = (step) => {
                    if (!step) return;
                    step.classList.add("active");
                    const b = step.querySelector(".step-bullet");
                    if (b) b.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
                };

                setTimeout(() => {
                    if (progressFill) progressFill.style.width = "65%";
                    markDone(step1);
                    markActive(step2);
                }, 800);

                setTimeout(() => {
                    if (progressFill) progressFill.style.width = "90%";
                    markDone(step2);
                    markActive(step3);
                }, 1600);

                setTimeout(() => {
                    if (progressFill) progressFill.style.width = "100%";
                    markDone(step3);

                    setTimeout(() => {
                        overlay.style.display = "none";
                        if (congratsModal) congratsModal.classList.add("show");

                        // Clean URL params so refreshing doesn't replay
                        const cleanUrl = window.location.pathname;
                        window.history.replaceState({}, document.title, cleanUrl);
                    }, 500);
                }, 2400);
            } else if (congratsModal) {
                congratsModal.classList.add("show");
                const cleanUrl = window.location.pathname;
                window.history.replaceState({}, document.title, cleanUrl);
            }
        }
    },

    // =====================================================================
    // GitHub push auto-sync (webhook card)
    // =====================================================================

    _initGithubWebhook() {
        const box = document.getElementById("githubWebhookBox");
        if (!box) {
            return;
        }
        const instanceId = parseInt(box.dataset.instanceId || 0) || this._getInstanceId();
        const toggle = document.getElementById("githubAutosyncToggle");
        toggle?.addEventListener("change", async () => {
            const enabled = toggle.checked;
            const label = document.getElementById("githubAutosyncLabel");
            if (label) {
                label.textContent = enabled ? "On" : "Off";
            }
            try {
                const res = await this.rpc("/saas/github/autosync", {
                    instance_id: instanceId, enabled: enabled,
                });
                if (res && res.success) {
                    notify(enabled ? "Auto-sync on push enabled." : "Auto-sync on push disabled.");
                } else {
                    toggle.checked = !enabled;
                    if (label) {
                        label.textContent = enabled ? "Off" : "On";
                    }
                    notify((res && res.error) || "Could not change auto-sync.");
                }
            } catch (err) {
                toggle.checked = !enabled;
                notify("Could not change auto-sync: " + (err && err.message ? err.message : err));
            }
        });
        document.getElementById("btnCopyWebhook")?.addEventListener("click", async () => {
            const input = document.getElementById("githubWebhookUrl");
            if (!input) {
                return;
            }
            try {
                await navigator.clipboard.writeText(input.value);
                notify(_t("Webhook URL copied."));
            } catch (err) {
                // Older browsers / non-secure contexts have no clipboard API.
                input.select();
                document.execCommand("copy");
                notify(_t("Webhook URL copied."));
            }
        });
        this._initGithubLogs();
    },

    // =====================================================================
    // GitHub Logs tab — live refresh + "new push" banner
    // =====================================================================

    _initGithubLogs() {
        const box = document.getElementById("ghLogsList");
        if (!box) {
            return;
        }
        this._ghLastId = parseInt(box.dataset.lastId || 0) || 0;
        this._ghBusy = false;
        this._ghFirstRun = true;

        // Manual only: the button pulls the repository, restarts the node and reloads the list.
        document.getElementById("btnResyncFromLogs")?.addEventListener("click", () => {
            this._resyncFromLogs();
        });
    },

    async _resyncFromLogs() {
        const button = document.getElementById("btnResyncFromLogs");
        const instanceId = this._getInstanceId();
        if (!button || !instanceId || this._ghBusy) {
            return;
        }
        button.disabled = true;
        this._ghStatus("syncing…");
        try {
            const res = await this.rpc("/saas/github/resync", { instance_id: instanceId });
            if (res && res.success) {
                notify(_t("Repository pulled and the instance restarted."));
            } else {
                notify((res && res.error) || "Re-sync failed.");
            }
        } catch (err) {
            notify("Re-sync failed: " + (err && err.message ? err.message : err));
        } finally {
            button.disabled = false;
            await this._refreshGithubLogs(false);
        }
    },

    _ghStatus(text) {
        const el = document.getElementById("ghLogsStatus");
        if (el) {
            el.textContent = text;
        }
    },

    async _refreshGithubLogs(showFeedback) {
        const box = document.getElementById("ghLogsList");
        const instanceId = this._getInstanceId();
        if (!box || !instanceId || this._ghBusy) {
            return;
        }
        this._ghBusy = true;
        try {
            const res = await this.rpc("/saas/instance/github-logs", {
                instance_id: instanceId, limit: 40,
            });
            if (!res || !res.success) {
                return;
            }
            const logs = res.logs || [];
            const latest = logs.length ? logs[0].id : 0;
            const isNew = !this._ghFirstRun && latest && latest > this._ghLastId;

            this._renderGithubLogs(logs);
            this._ghStatus(logs.length
                ? (logs[0].status === "failed" ? "last sync failed" : "last sync " + logs[0].datetime)
                : "no sync yet");
            this._ghLastId = latest;
            this._ghFirstRun = false;
            if (showFeedback) {
                notify(_t("GitHub logs refreshed."));
            }
        } catch (err) {
            // A failed poll is not fatal, the next tick retries.
        } finally {
            this._ghBusy = false;
        }
    },

    _ghEl(tag, className, text) {
        const el = document.createElement(tag);
        if (className) {
            el.className = className;
        }
        if (text !== undefined && text !== null) {
            el.textContent = String(text);
        }
        return el;
    },

    _renderGithubLogs(logs) {
        const box = document.getElementById("ghLogsList");
        if (!box) {
            return;
        }
        box.textContent = "";
        if (!logs.length) {
            box.appendChild(this._ghEl("div", "",
                "No GitHub sync recorded yet. Connect a repository and push a change — "
                + "every push will show up here."));
            box.firstChild.setAttribute("style", "padding:26px;font-size:12px;color:var(--muted)");
            return;
        }
        for (const log of logs) {
            const row = this._ghEl("div");
            row.setAttribute("style", "padding:16px;border-bottom:1px solid var(--line)");

            const head = this._ghEl("div");
            head.setAttribute("style", "display:flex;align-items:center;gap:12px;flex-wrap:wrap");
            const badge = this._ghEl("span", "status " + (
                log.status === "failed" ? "offline" : (log.status === "nochange" ? "warning" : "")),
                log.status === "failed" ? "Failed" : (log.status === "nochange" ? "No change" : "Synced"));
            head.appendChild(badge);
            const trig = this._ghEl("strong", "", log.trigger_label);
            trig.setAttribute("style", "font-size:12px");
            head.appendChild(trig);
            for (const [value, cls] of [[log.branch, "mono"], [log.addon_name, "mono"]]) {
                if (value) {
                    const span = this._ghEl("span", cls, value);
                    span.setAttribute("style", "font-size:10px");
                    head.appendChild(span);
                }
            }
            const when = this._ghEl("span", "", log.datetime);
            when.setAttribute("style", "margin-left:auto;font-size:10px;color:var(--muted)");
            head.appendChild(when);
            row.appendChild(head);

            const grid = this._ghEl("div");
            grid.setAttribute("style",
                "display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:12px");
            const cells = [
                ["Revision", log.ref_before + " .. " + log.ref_after],
                ["Commits", log.commit_count],
                ["Files", log.file_count],
                ["Diff", "+" + log.insertions + " / -" + log.deletions],
            ];
            for (const [label, value] of cells) {
                const cell = this._ghEl("div");
                const small = this._ghEl("div", "", label);
                small.setAttribute("style",
                    "font-size:9px;font-weight:800;text-transform:uppercase;color:var(--muted)");
                cell.appendChild(small);
                const val = this._ghEl("div", String(value).startsWith("+") ? "" : "mono", value);
                val.setAttribute("style", "font-size:11px;font-weight:800");
                cell.appendChild(val);
                grid.appendChild(cell);
            }
            row.appendChild(grid);

            if (log.message) {
                const msg = this._ghEl("div", "sub", log.message);
                msg.setAttribute("style", "margin-top:8px");
                row.appendChild(msg);
            }
            for (const [label, content] of [["Commits pulled", log.commits], ["Files changed", log.files]]) {
                if (!content) {
                    continue;
                }
                const title = this._ghEl("div", "", label);
                title.setAttribute("style",
                    "font-size:9px;font-weight:800;text-transform:uppercase;color:var(--muted);margin-top:12px");
                row.appendChild(title);
                const pre = this._ghEl("pre", "", content);
                pre.setAttribute("style", "margin:6px 0 0;padding:10px;background:#142327;color:#b8d9d5;"
                    + "border-radius:10px;font:11px/1.6 monospace;white-space:pre;overflow:auto");
                row.appendChild(pre);
            }
            const details = document.createElement("details");
            details.setAttribute("style", "margin-top:12px");
            const summary = this._ghEl("summary", "", "Raw git output (as printed on the server)");
            summary.setAttribute("style", "cursor:pointer;font-size:10px;font-weight:800;color:var(--deep)");
            details.appendChild(summary);
            const raw = this._ghEl("pre", "console", log.output);
            raw.setAttribute("style", "margin:8px 0 0;max-height:340px;overflow:auto;white-space:pre;font-size:11px");
            details.appendChild(raw);
            row.appendChild(details);

            if (log.repo) {
                const repo = this._ghEl("div", "mono", log.repo);
                repo.setAttribute("style", "font-size:10px;margin-top:8px");
                row.appendChild(repo);
            }
            box.appendChild(row);
        }
    },

    _removeGithubBanner() {
        document.getElementById("ghPushBanner")?.remove();
    },

    _showGithubPushBanner(log) {
        const key = "saas_gh_banner_" + log.id;
        if (sessionStorage.getItem(key)) {
            return;
        }
        this._removeGithubBanner();
        const el = this._ghEl("div");
        el.id = "ghPushBanner";
        el.setAttribute("style", "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:120;"
            + "display:flex;align-items:center;gap:14px;max-width:min(720px,94vw);padding:12px 16px;"
            + "background:linear-gradient(135deg,#0fa8a0,#0b6e68);color:#fff;border-radius:14px;"
            + "box-shadow:0 14px 34px rgba(11,46,51,.28);font-size:12px;font-weight:700");
        const icon = this._ghEl("i", "fa fa-code-fork");
        icon.setAttribute("style", "font-size:16px");
        el.appendChild(icon);

        const text = this._ghEl("div");
        text.appendChild(this._ghEl("div", "", "New code pushed from GitHub"));
        const detail = this._ghEl("div", "",
            "+" + log.insertions + " / -" + log.deletions + " · " + log.file_count
            + " file(s), " + log.commit_count + " commit(s) on " + log.branch
            + (log.status === "failed" ? " — sync FAILED" : " — synced & instance restarted"));
        detail.setAttribute("style", "font-weight:500;font-size:11px;opacity:.92;margin-top:2px");
        text.appendChild(detail);
        el.appendChild(text);

        const open = this._ghEl("button", "ghost", "View logs");
        open.setAttribute("style", "background:rgba(255,255,255,.92);border:0;font-weight:800");
        open.addEventListener("click", () => {
            sessionStorage.setItem(key, "1");
            el.remove();
            document.querySelector('[data-tab="ghlogs"]')?.click();
        });
        el.appendChild(open);

        const close = this._ghEl("button", "ghost", "✕");
        close.setAttribute("title", "Close");
        close.setAttribute("style", "background:transparent;border:0;color:#fff;font-weight:900;font-size:14px");
        close.addEventListener("click", () => {
            sessionStorage.setItem(key, "1");
            el.remove();
        });
        el.appendChild(close);

        document.body.appendChild(el);
    },

    // =====================================================================
    // Instance Settings — Odoo master password (admin_passwd)
    // =====================================================================

    _initInstanceAdminPassword() {
        const button = document.getElementById("saveInstanceAdminPass");
        const input = document.getElementById("instanceAdminPass");
        if (!button || !input || button.dataset.bound) {
            return;
        }
        button.dataset.bound = "1";
        button.addEventListener("click", async () => {
            const value = (input.value || "").trim();
            const status = document.getElementById("instanceAdminPassStatus");
            if (!value) {
                notify(_t("Please enter a master password."));
                input.focus();
                return;
            }
            button.disabled = true;
            if (status) status.textContent = "Saving…";
            try {
                const res = await this.rpc("/saas/instance/admin-password", {
                    instance_id: this._getInstanceId(),
                    admin_pass: value,
                });
                if (res && res.success) {
                    if (status) status.textContent = "Saved — Odoo restarted";
                    notify(_t("Master password updated. Odoo restarted."));
                } else if (res && res.storage_full) {
                    if (status) status.textContent = "";
                    notify(res.error || _t("Could not update the master password."));
                } else {
                    if (status) status.textContent = "Error";
                    notify((res && res.error) || _t("Could not update the master password."));
                }
            } catch (err) {
                if (status) status.textContent = "Error";
                notify(_t("Could not update the master password."));
            }
            button.disabled = false;
        });
    },

    // =====================================================================
    // Instance shell — live terminal inside the customer containers
    // =====================================================================

    _initInstanceShell() {
        this._shellKind = "odoo";
        this._shellConnected = false;
        this._shellConnecting = false;
        this._shellBusy = false;
        this._shellTimer = null;
        this._shellPollDelay = SHELL_POLL_FAST_MS;
        this._shellKickPending = false;
        this._shellHotUntil = 0;
        this._shellHash = "";
        this._shellTextBuffer = "";
        this._shellKeys = [];
        this._shellWriting = false;
        this._shellWriteRetryAt = 0;
        // The terminal paints the server screen only. Nothing is ever drawn from a local
        // keystroke, so what is on screen is exactly what the shell received: no second
        // drawing layer, no display offset, and deleted characters cannot reappear.
        this._shellServer = null;
        this._shellRows = [];
        this._shellRowKeys = [];
        this._shellSize = null;
        this._shellFullscreen = false;
        this._shellFitTimer = null;
        // True while the view should stick to the newest output. Scrolling up turns it
        // off so new output does not yank the user away from the lines they are reading.
        this._shellFollow = true;

        const wrap = document.getElementById("shellScreenWrap");
        if (!wrap) {
            return;
        }
        // The `start()` guard above already stops a second widget instance from
        // initialising, but the terminal DOM is looked up by id: if this method ever runs
        // again (widget restart, a new instance on the same page, a soft re-render) the
        // click/key/paste handlers below would all be bound a second time. Key the guard
        // on the shell DOM itself so one terminal has exactly one set of handlers.
        if (wrap.dataset.shellInitialized) {
            return;
        }
        wrap.dataset.shellInitialized = "1";

        document.querySelectorAll("[data-shell-kind]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const kind = btn.dataset.shellKind;
                if (kind === this._shellKind) {
                    return;
                }
                this._shellKind = kind;
                document.querySelectorAll("[data-shell-kind]").forEach((other) =>
                    other.classList.toggle("active", other.dataset.shellKind === kind));
                this._shellConnect();
            });
        });

        document.querySelector('[data-action="shell-reconnect"]')?.addEventListener("click", async () => {
            const instanceId = this._getInstanceId();
            const screen = document.getElementById("shellScreen");
            // Clearing is done here, and the old tmux session is killed below: connecting
            // alone used to re-attach to the same session, so the old output stayed.
            if (screen) {
                screen.textContent = "Starting a fresh shell…";
            }
            // The rendered rows are gone with the text above, so forget them too.
            this._shellRows = [];
            this._shellRowKeys = [];
            this._shellHash = "";
            this._shellStopPolling();
            if (instanceId) {
                try {
                    await this.rpc("/saas/instance/shell/close", {
                        instance_id: instanceId, kind: this._shellKind,
                    });
                } catch (err) {
                    // best effort: the connect below recreates the session anyway
                }
            }
            this._shellConnected = false;
            this._shellConnect();
        });
        document.querySelector('[data-action="shell-close"]')?.addEventListener("click", () => {
            const instanceId = this._getInstanceId();
            if (instanceId) {
                this.rpc("/saas/instance/shell/close", {
                    instance_id: instanceId, kind: this._shellKind,
                }).catch(() => {});
            }
            this._shellStopPolling();
            this._shellConnected = false;
            this._shellStatus("closed");
            this._shellVeil(true, "Session closed. Click Reconnect to start a new shell.");
        });

        document.querySelector('[data-action="shell-fullscreen"]')?.addEventListener(
            "click", () => this._shellToggleFullscreen(),
        );
        // Escape leaves fullscreen; Shift+Escape still sends a real ESC to the terminal.
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && this._shellFullscreen && !event.shiftKey
                    && !event.ctrlKey && !event.altKey && !event.metaKey) {
                this._shellToggleFullscreen(false);
            }
        });
        // The terminal is a fixed grid, so a resized window has to be told to tmux too.
        window.addEventListener("resize", () => {
            if (!this._shellConnected) {
                return;
            }
            clearTimeout(this._shellFitTimer);
            this._shellFitTimer = setTimeout(() => this._shellFit(), 250);
        });

        // The screen is rendered, never edited: a hidden input captures the keystrokes.
        wrap.addEventListener("click", () => document.getElementById("shellKeys")?.focus());
        document.querySelectorAll('[data-tab="shell"]').forEach((btn) => {
            btn.addEventListener("click", () => setTimeout(() => this._shellActivate(), 80));
        });
        this._shellBindKeys();

        // --- paste only: Ctrl+V inside the terminal, or the Paste button ---
        const shellKeysInput = document.getElementById("shellKeys");
        const sendPasted = (text) => {
            if (text) {
                this._shellSendText(text.replace(/\r\n/g, "\n"));
            }
        };
        if (shellKeysInput) {
            shellKeysInput.addEventListener("paste", (event) => {
                const data = event.clipboardData || window.clipboardData;
                const text = data ? data.getData("text") : "";
                if (text) {
                    event.preventDefault();
                    sendPasted(text);
                }
            });
        }
        document.querySelectorAll('[data-action="shell-paste"]').forEach((button) => {
            button.addEventListener("click", async (event) => {
                event.preventDefault();
                try {
                    sendPasted(await navigator.clipboard.readText());
                } catch (err) {
                    notify(_t("Press Ctrl+V to paste."));
                }
                document.getElementById("shellKeys")?.focus();
            });
        });

        // Chrome never offers "Paste" on a <pre>, so the terminal gets its own small menu:
        // Paste / Copy / Select all. Ctrl+Shift+V (paste) and Ctrl+Shift+C (copy) also work.
        const termScreen = document.getElementById("shellScreen");
        const termWrap = document.getElementById("shellScreenWrap");
        const closeTermMenu = () => {
            document.querySelectorAll(".saas-term-menu").forEach((menu) => menu.remove());
        };
        const termSelection = () => (window.getSelection() || "").toString();
        let termRemembered = "";
        document.addEventListener("selectionchange", () => {
            const text = termSelection();
            if (text && text.trim()) {
                termRemembered = text;
            }
        });
        if (termScreen) {
            termScreen.addEventListener("mousedown", (event) => {
                if (!event || event.button === 0) {
                    termRemembered = "";
                }
            });
            // The pane now also returns tmux history, so the <pre> really has content to
            // scroll. Follow the newest output only while the user stays at the bottom:
            // scrolling up pins the view to what they are reading.
            termScreen.addEventListener("scroll", () => {
                const atBottom = termScreen.scrollTop + termScreen.clientHeight
                    >= termScreen.scrollHeight - 4;
                this._shellFollow = atBottom;
                this._shellUpdateJump();
            });
        }
        // The button is part of the template, but create it on the fly when an older page
        // cache does not have it yet, so the scroll fix works without a module upgrade.
        if (!document.getElementById("shellJump") && wrap) {
            const jump = document.createElement("button");
            jump.type = "button";
            jump.id = "shellJump";
            jump.className = "shell-jump";
            jump.title = "Jump to the newest output";
            jump.innerHTML = '<i class="fa fa-arrow-down"></i> Latest output';
            wrap.appendChild(jump);
        }
        document.getElementById("shellJump")?.addEventListener("click", () => {
            this._shellFollow = true;
            const el = document.getElementById("shellScreen");
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
            this._shellUpdateJump();
            document.getElementById("shellKeys")?.focus();
        });
        this._shellUpdateJump();
        const pasteFromClipboard = async () => {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    this._shellSendText(text.replace(/\r\n/g, "\n"));
                } else {
                    notify(_t("Clipboard is empty."));
                }
            } catch (err) {
                notify(_t("Browser blocks automatic paste — use Ctrl+Shift+V."));
            }
            document.getElementById("shellKeys")?.focus();
        };
        const copyTermSelection = async () => {
            const text = termSelection() || termRemembered;
            if (!text || !text.trim()) {
                notify(_t("Select some text first, then Copy."));
                return;
            }
            try {
                await navigator.clipboard.writeText(text);
                notify(_t("Copied."));
            } catch (err) {
                notify(_t("Copy blocked by the browser — press Ctrl+C."));
            }
        };
        if (termWrap) {
            termWrap.addEventListener("contextmenu", (event) => {
                event.preventDefault();
                closeTermMenu();
                const menu = document.createElement("div");
                menu.className = "saas-term-menu";
                menu.setAttribute("style", "position:fixed;left:"
                    + Math.min(event.clientX, window.innerWidth - 200) + "px;top:"
                    + Math.min(event.clientY, window.innerHeight - 160) + "px;z-index:99999;"
                    + "background:#fff;border:1px solid #dfe7ea;border-radius:12px;padding:6px;"
                    + "min-width:186px;box-shadow:0 16px 36px rgba(9,32,36,.22);font-size:12px");
                const entry = (label, hint, handler) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.setAttribute("style", "display:flex;justify-content:space-between;"
                        + "align-items:center;gap:14px;width:100%;padding:9px 12px;border:0;"
                        + "border-radius:8px;background:transparent;font:inherit;font-weight:700;"
                        + "color:#0d3a44;cursor:pointer;text-align:left");
                    button.innerHTML = "<span>" + label + "</span>"
                        + (hint ? "<span style='color:#8b9aa1;font-weight:600'>" + hint + "</span>" : "");
                    button.addEventListener("mouseenter", () => { button.style.background = "#eef6f6"; });
                    button.addEventListener("mouseleave", () => { button.style.background = "transparent"; });
                    button.addEventListener("click", () => { closeTermMenu(); handler(); });
                    return button;
                };
                menu.appendChild(entry("Paste", "Ctrl+Shift+V", () => pasteFromClipboard()));
                menu.appendChild(entry("Copy", "Ctrl+Shift+C", () => copyTermSelection()));
                menu.appendChild(entry("Select all", "", () => {
                    const range = document.createRange();
                    range.selectNodeContents(termScreen);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                    termRemembered = termSelection();
                }));
                document.body.appendChild(menu);
                setTimeout(() => {
                    document.addEventListener("click", closeTermMenu, { once: true });
                    document.addEventListener("keydown", (ev) => {
                        if (ev.key === "Escape") {
                            closeTermMenu();
                        }
                    }, { once: true });
                }, 0);
            });
        }
        document.addEventListener("keydown", (event) => {
            if (!event.ctrlKey || !event.shiftKey) {
                return;
            }
            const key = (event.key || "").toLowerCase();
            if (key === "v") {
                event.preventDefault();
                pasteFromClipboard();
            } else if (key === "c") {
                event.preventDefault();
                copyTermSelection();
            }
        });

        if (document.getElementById("shell")?.classList.contains("active")) {
            this._shellActivate();
        }
    },

    _shellActivate() {
        const pane = document.getElementById("shell");
        if (!pane || !pane.classList.contains("active")) {
            return;
        }
        document.getElementById("shellKeys")?.focus();
        if (this._shellConnected) {
            this._shellStopPolling(); this._shellStartPolling();
        } else {
            this._shellConnect();
        }
    },

    _shellRpc(path, params) {
        const instanceId = this._getInstanceId();
        if (!instanceId) {
            return Promise.resolve({ success: false, error: "No instance selected." });
        }
        return this.rpc(path, Object.assign(
            { instance_id: instanceId, kind: this._shellKind }, params || {}));
    },

    _shellStatus(text) {
        const el = document.getElementById("shellStatus");
        if (el) {
            el.textContent = text;
        }
    },

    _shellVeil(show, message) {
        const veil = document.getElementById("shellVeil");
        if (!veil) {
            return;
        }
        veil.classList.toggle("show", !!show);
        const label = veil.querySelector("span");
        if (message && label) {
            label.textContent = message;
        }
    },

    /** Show "jump to latest" only while the user is reading older output. */
    _shellUpdateJump() {
        const button = document.getElementById("shellJump");
        if (button) {
            button.classList.toggle("show", !this._shellFollow);
        }
    },

    _shellUpdateTarget() {
        const el = document.getElementById("shellTarget");
        if (!el) {
            return;
        }
        const prefix = this._shellKind === "psql" ? "psql_" : "odoo_";
        el.textContent = prefix + (el.dataset.techName || "");
    },

    /** Expand the terminal to the whole viewport (and back). */
    _shellToggleFullscreen(force) {
        const frame = document.getElementById("shellFrame");
        if (!frame) {
            return;
        }
        this._shellFullscreen = typeof force === "boolean" ? force : !this._shellFullscreen;
        const on = this._shellFullscreen;
        frame.classList.toggle("shell-fullscreen", on);
        document.body.classList.toggle("shell-fullscreen-open", on);
        const button = document.querySelector('[data-action="shell-fullscreen"]');
        if (button) {
            const label = button.querySelector("span");
            if (label) {
                label.textContent = on ? "Exit fullscreen" : "Fullscreen";
            }
            const icon = button.querySelector("i");
            if (icon) {
                icon.classList.toggle("fa-expand", !on);
                icon.classList.toggle("fa-compress", on);
            }
        }
        // Let the layout settle, then match tmux to the new viewport.
        setTimeout(() => {
            this._shellFit();
            const screen = document.getElementById("shellScreen");
            if (screen) {
                screen.scrollTop = screen.scrollHeight;
            }
        }, 60);
    },

    /** Ask tmux for exactly the rows/columns the browser terminal can show. */
    async _shellFit() {
        if (!this._shellConnected) {
            return;
        }
        const size = this._shellMeasure();
        if (!size) {
            return;
        }
        if (this._shellSize
                && this._shellSize[0] === size[0] && this._shellSize[1] === size[1]) {
            return;
        }
        this._shellSize = size;
        try {
            await this._shellRpc("/saas/instance/shell/resize",
                { cols: size[0], rows: size[1] });
        } catch (err) {
            // Resizing is cosmetic: a failure must never break the terminal.
        }
        this._shellKickPoll();
    },

    /** Work out how many monospace cells fit in the terminal box. */
    _shellMeasure() {
        const screen = document.getElementById("shellScreen");
        if (!screen || screen.clientWidth <= 0 || screen.clientHeight <= 0) {
            return null;
        }
        const style = window.getComputedStyle(screen);
        const probe = document.createElement("span");
        probe.textContent = "0000000000";
        probe.style.position = "absolute";
        probe.style.visibility = "hidden";
        probe.style.whiteSpace = "pre";
        probe.style.fontSize = style.fontSize;
        probe.style.fontFamily = style.fontFamily;
        probe.style.lineHeight = style.lineHeight;
        screen.appendChild(probe);
        const charWidth = (probe.getBoundingClientRect().width || 72) / 10;
        probe.remove();
        const lineHeight = parseFloat(style.lineHeight) || 16.2;
        const padX = (parseFloat(style.paddingLeft) || 0)
            + (parseFloat(style.paddingRight) || 0);
        const padY = (parseFloat(style.paddingTop) || 0)
            + (parseFloat(style.paddingBottom) || 0);
        const cols = Math.floor((screen.clientWidth - padX) / (charWidth || 7.2));
        const rows = Math.floor((screen.clientHeight - padY) / lineHeight);
        return [
            Math.max(SHELL_RESIZE_MIN_COLS, Math.min(cols || 0, SHELL_RESIZE_MAX_COLS)),
            Math.max(SHELL_RESIZE_MIN_ROWS, Math.min(rows || 0, SHELL_RESIZE_MAX_ROWS)),
        ];
    },

    async _shellConnect() {
        if (this._shellConnecting) {
            return;
        }
        this._shellConnecting = true;
        this._shellStopPolling();
        this._shellHash = "";
        this._shellTextBuffer = "";
        this._shellKeys = [];
        this._shellWriteRetryAt = 0;
        this._shellServer = null;
        this._shellSize = null;
        // A fresh session starts at the newest output.
        this._shellFollow = true;
        this._shellUpdateTarget();
        const blankScreen = document.getElementById("shellScreen");
        if (blankScreen && !this._shellRows.length) {
            blankScreen.textContent = "";
        }
        this._shellVeil(true, "We are preparing your instance shell…");
        this._shellStatus("starting…");
        try {
            const res = await this._shellRpc("/saas/instance/shell/open");
            if (!res || !res.success) {
                this._shellConnected = false;
                this._shellVeil(true, (res && res.error) || "Could not start the shell.");
                this._shellStatus("error");
                return;
            }
            this._shellConnected = true;
            this._shellStatus(res.state === "attached" ? "session attached" : "session ready");
            // Fit the tmux grid to the browser before painting: the terminal has to match the
            // real viewport, otherwise wide screens scroll sideways and small ones wrap early.
            await this._shellFit();
            await this._shellPoll();
            this._shellStartPolling();
        } catch (err) {
            this._shellConnected = false;
            this._shellVeil(true, "Could not start the shell: " +
                (err && err.message ? err.message : err));
            this._shellStatus("error");
        } finally {
            this._shellConnecting = false;
        }
    },

    _shellStartPolling() {
        this._shellPollDelay = SHELL_POLL_FAST_MS;
        this._shellSchedulePoll(0);
    },

    _shellSchedulePoll(delay) {
        this._shellStopPolling();
        this._shellTimer = setTimeout(() => this._shellPollTick(), Math.max(0, delay));
    },

    _shellStopPolling() {
        if (this._shellTimer) {
            clearTimeout(this._shellTimer);
            this._shellTimer = null;
        }
    },

    async _shellPollTick() {
        if (!this._shellConnected) {
            return;
        }
        const pane = document.getElementById("shell");
        if (!pane || !pane.classList.contains("active")) {
            // Hidden tab: keep the loop alive, but only slowly.
            this._shellSchedulePoll(SHELL_POLL_IDLE_MS);
            return;
        }
        const changed = await this._shellPoll();
        if (!this._shellConnected) {
            return;
        }
        if (changed || Date.now() < this._shellHotUntil) {
            // Something moved, or we typed very recently: keep polling fast.
            this._shellPollDelay = SHELL_POLL_FAST_MS;
        } else {
            // Nothing moved: back off towards the idle interval, and snap back on activity.
            this._shellPollDelay = Math.min(
                SHELL_POLL_IDLE_MS, Math.round(this._shellPollDelay * 1.6));
        }
        this._shellSchedulePoll(this._shellPollDelay);
    },

    /** A write just happened: read again right away so the echo is not up to a tick late. */
    _shellKickPoll() {
        if (!this._shellConnected) {
            return;
        }
        this._shellPollDelay = SHELL_POLL_FAST_MS;
        // Stay responsive for a while: a command typed now may only print later.
        this._shellHotUntil = Date.now() + 4000;
        if (this._shellBusy) {
            this._shellKickPending = true;
            return;
        }
        this._shellSchedulePoll(30);
    },

    /**
     * A write already returned the screen: keep the poll loop fast for the next few seconds
     * (a command may still be printing) without spending an extra request right now.
     */
    _shellKeepPollingFast() {
        if (!this._shellConnected) {
            return;
        }
        this._shellPollDelay = SHELL_POLL_FAST_MS;
        this._shellHotUntil = Date.now() + 4000;
        if (this._shellBusy) {
            this._shellKickPending = true;
            return;
        }
        this._shellSchedulePoll(SHELL_POLL_FAST_MS);
    },

    async _shellPoll() {
        if (this._shellBusy || !this._shellConnected) {
            return false;
        }
        this._shellBusy = true;
        try {
            const res = await this._shellRpc("/saas/instance/shell/read");
            if (!res || !res.success) {
                if (res && res.error) {
                    this._shellVeil(true, res.error);
                    this._shellStatus("error");
                }
                return false;
            }
            if (!res.alive) {
                this._shellConnected = false;
                this._shellStopPolling();
                this._shellStatus("ended");
                this._shellVeil(true, res.message || "Shell session ended.");
                return false;
            }
            const changed = this._shellRender(res);
            this._shellVeil(false);
            this._shellStatus(res.dead ? "process exited" : "live");
            return changed;
        } catch (err) {
            // One failed poll is not fatal, the next tick simply retries.
            this._shellStatus("reconnecting…");
            return false;
        } finally {
            this._shellBusy = false;
            if (this._shellKickPending) {
                this._shellKickPending = false;
                if (this._shellConnected) {
                    this._shellSchedulePoll(20);
                }
            }
        }
    },

    /** Paint the tmux screen (tmux already laid it out in a fixed grid). */
    _shellRender(res) {
        const screen = res.screen || "";
        const cursor = res.cursor || [0, 0];
        // Most polls return the very same screen, so skip the DOM work in that case.
        // The whole screen is compared, not just its tail: editing a character in the middle
        // of a long line leaves both the total length and the last 160 characters untouched,
        // and that used to make the repaint skip so the keystroke never appeared.
        const hash = cursor.join(",") + ":" + screen;
        if (hash === this._shellHash) {
            return false;
        }
        const el = document.getElementById("shellScreen");
        if (!el) {
            return false;
        }
        // While text is selected, leave the DOM alone: a repaint would throw the
        // selection away and copying output would be impossible.
        if (this._shellSelectionInside(el)) {
            return false;
        }
        this._shellHash = hash;
        const rows = this._shellParseAnsi(screen);
        this._shellServer = { rows: rows, cursor: cursor };
        this._shellPaint();
        return true;
    },

    /** Draw the last screen the server reported — the only thing ever painted. */
    _shellPaint() {
        const server = this._shellServer;
        const el = document.getElementById("shellScreen");
        if (!server || !el) {
            return;
        }
        const rows = server.rows;
        const cursorRow = server.cursor[1];
        // Surplus rows (a shorter screen) go first, so all the other rows can stay in place.
        while (this._shellRows.length > rows.length) {
            const extra = this._shellRows.pop();
            this._shellRowKeys.pop();
            if (extra) {
                extra.remove();
            }
        }
        for (let index = 0; index < rows.length; index++) {
            // Only rows that really changed are rewritten: rebuilding the whole screen on
            // every keystroke is what made typing crawl.
            const onCursor = index === cursorRow ? server.cursor[0] : -1;
            const html = this._shellRowHtml(rows[index], onCursor);
            let row = this._shellRows[index];
            if (row && this._shellRowKeys[index] === html) {
                if (!row.parentNode) {
                    el.appendChild(row);
                }
                continue;
            }
            if (!row) {
                row = document.createElement("div");
                row.className = "shell-row";
                this._shellRows[index] = row;
            }
            row.innerHTML = html;
            this._shellRowKeys[index] = html;
            if (!row.parentNode) {
                el.appendChild(row);
            }
        }
        // Keep the newest line in view while following; when the user scrolled up we leave
        // the viewport exactly where they put it.
        if (this._shellFollow) {
            el.scrollTop = el.scrollHeight;
        }
        this._shellUpdateJump();
    },

    /**
     * One terminal row as HTML: a single innerHTML write beats hundreds of DOM nodes.
     * The row is rendered exactly as the server reported it; the only thing added is the
     * cursor cell, so no character on screen is ever invented locally.
     */
    _shellRowHtml(cells, cursorCol) {
        const list = cells || [];
        let html = "";
        let column = 0;
        let placed = false;
        list.forEach((cell) => {
            const start = column;
            const end = column + cell.text.length;
            const style = cell.style ? ' style="' + cell.style + '"' : "";
            if (!placed && cursorCol >= start && cursorCol < end) {
                const offset = cursorCol - start;
                html += "<span" + style + ">"
                    + this._shellEscape(cell.text.slice(0, offset)) + "</span>";
                html += '<span class="shell-cursor"' + style + ">"
                    + this._shellEscape(cell.text.slice(offset, offset + 1) || " ") + "</span>";
                html += "<span" + style + ">"
                    + this._shellEscape(cell.text.slice(offset + 1)) + "</span>";
                placed = true;
            } else {
                html += "<span" + style + ">" + this._shellEscape(cell.text) + "</span>";
            }
            column = end;
        });
        if (!placed && cursorCol >= column) {
            html += '<span class="shell-cursor">'
                + " ".repeat(Math.max(1, cursorCol - column)) + "</span>";
        }
        return html;
    },

    _shellEscape(text) {
        return String(text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    },

    /** True while the user is selecting text inside the terminal (then hands off the DOM). */
    _shellSelectionInside(el) {
        const selection = window.getSelection ? window.getSelection() : null;
        if (!selection || selection.isCollapsed || !selection.rangeCount) {
            return false;
        }
        const range = selection.getRangeAt(0);
        return el.contains(range.startContainer) || el.contains(range.endContainer);
    },

    /** Split an ANSI coloured line into styled runs. */
    _shellParseAnsi(text) {
        const rows = [];
        String(text).split("\n").forEach((raw) => {
            const cells = [];
            let style = "";
            let buffer = "";
            const flush = () => {
                if (buffer) {
                    cells.push({ text: buffer, style: style });
                    buffer = "";
                }
            };
            for (let i = 0; i < raw.length; i++) {
                if (raw[i] !== "\u001b") {
                    buffer += raw[i];
                    continue;
                }
                const sgr = /^\u001b\[([0-9;]*)m/.exec(raw.slice(i));
                if (sgr) {
                    flush();
                    style = this._shellApplySgr(style, sgr[1]);
                    i += sgr[0].length - 1;
                    continue;
                }
                // Other escapes (cursor moves, ...) are already applied by tmux.
                const other = /^\u001b\[[0-9;?]*[A-Za-z]/.exec(raw.slice(i));
                i += other ? other[0].length - 1 : 0;
            }
            flush();
            rows.push(cells);
        });
        return rows;
    },

    _shellApplySgr(current, params) {
        const fg = {
            30: "#2e3436", 31: "#cc0000", 32: "#4e9a06", 33: "#c4a000",
            34: "#3465a4", 35: "#75507b", 36: "#06989a", 37: "#d3d7cf",
            90: "#555753", 91: "#ef2929", 92: "#8ae234", 93: "#fce94f",
            94: "#729fcf", 95: "#ad7fa8", 96: "#34e2e2", 97: "#eeeeec",
        };
        const bg = {
            40: "#2e3436", 41: "#cc0000", 42: "#4e9a06", 43: "#c4a000",
            44: "#3465a4", 45: "#75507b", 46: "#06989a", 47: "#d3d7cf",
            100: "#555753", 101: "#ef2929", 102: "#8ae234", 103: "#fce94f",
            104: "#729fcf", 105: "#ad7fa8", 106: "#34e2e2", 107: "#eeeeec",
        };
        const parts = (current || "").split(";").filter((part) => part);
        (params || "").split(";").forEach((raw) => {
            const code = parseInt(raw || "0", 10);
            if (code === 0) {
                parts.length = 0;
            } else if (code === 1) {
                parts.push("font-weight:700");
            } else if (code === 22) {
                parts.push("font-weight:400");
            } else if (code === 7) {
                parts.push("background:#d3d7cf;color:#11161a");
            } else if (fg[code]) {
                parts.push("color:" + fg[code]);
            } else if (bg[code]) {
                parts.push("background:" + bg[code]);
            } else if (code === 39) {
                parts.push("color:inherit");
            } else if (code === 49) {
                parts.push("background:transparent");
            }
        });
        return parts.join(";");
    },

    _shellBindKeys() {
        const input = document.getElementById("shellKeys");
        if (!input) {
            return;
        }
        // Exactly one keydown + one input listener on the keystroke field. The input is
        // found by id, so if this method ever ran twice a second pair would send every
        // physical key twice. Drop any previous pair first and keep the handlers on the
        // element itself, so re-running this method can never duplicate the binding.
        if (input._saasShellOnKeydown) {
            input.removeEventListener("keydown", input._saasShellOnKeydown);
        }
        if (input._saasShellOnInput) {
            input.removeEventListener("input", input._saasShellOnInput);
        }
        const special = {
            Enter: "Enter", Backspace: "BSpace", Tab: "Tab", Escape: "Escape",
            ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
            Home: "Home", End: "End", PageUp: "PPage", PageDown: "NPage", Delete: "DC",
        };
        const control = {
            c: "C-c", d: "C-d", z: "C-z", l: "C-l", a: "C-a",
            e: "C-e", u: "C-u", k: "C-k", w: "C-w", r: "C-r",
        };
        const onKeydown = (ev) => {
            // In fullscreen, Escape leaves fullscreen instead of reaching the terminal;
            // Shift+Escape still sends a real ESC (for vim, less, ...).
            if (ev.key === "Escape" && this._shellFullscreen && !ev.shiftKey) {
                ev.preventDefault();
                return;
            }
            if (ev.ctrlKey && !ev.altKey && !ev.metaKey) {
                const key = control[ev.key.toLowerCase()];
                if (key) {
                    ev.preventDefault();
                    this._shellSendKeys([key]);
                }
                return;
            }
            if (special[ev.key]) {
                // Enter/Tab/arrows/Backspace...: real keys, never text, so they must not
                // reach the input event below (otherwise one press would be sent twice).
                // Backspace goes out as tmux ``BSpace``, which the PTY reads as its erase
                // character (0x7f / DEL). Nothing is deleted on screen here: the shell
                // redraws the line and that redraw is what gets painted.
                ev.preventDefault();
                this._shellSendKeys([special[ev.key]]);
            }
        };
        // Plain characters — typing, paste, mobile keyboards, IME — are only sent from here,
        // so one keystroke always equals exactly one send.
        const onInput = () => {
            const value = input.value;
            input.value = "";
            if (value) {
                // Send only. No character is ever drawn from here: the screen comes back
                // from the PTY, so the shell's own echo is the single source of truth.
                this._shellSendText(value);
            }
        };
        input._saasShellOnKeydown = onKeydown;
        input._saasShellOnInput = onInput;
        input.addEventListener("keydown", onKeydown);
        input.addEventListener("input", onInput);
    },

    // There is deliberately no local echo and no line editing in JS: every keystroke is sent
    // raw to the PTY and the terminal only ever paints what the PTY sent back, so the screen
    // can never disagree with what the shell received.

    /**
     * Queue the characters and send them at once. There is deliberately no debounce: a timer
     * here would delay every keystroke by its own duration before the request had even left
     * the browser, and the single write below already keeps the order intact. Typing faster
     * than the round trip simply accumulates here and travels in the next request.
     */
    _shellSendText(text) {
        if (!this._shellConnected) {
            return;
        }
        this._shellTextBuffer += text;
        this._shellPump();
    },

    _shellSendKeys(keys) {
        if (!this._shellConnected) {
            return;
        }
        (keys || []).forEach((key) => this._shellKeys.push(key));
        // Send straight away; buffered text rides along in the same request so a key can
        // never overtake the characters typed just before it.
        this._shellPump();
    },

    /**
     * Exactly one write on the wire at a time. While a request is in flight, new characters
     * and keys simply accumulate and travel in the next one. The previous version queued a
     * promise per keystroke, so typing faster than the round trip built an ever-growing
     * delay: the more you typed, the further behind the terminal fell.
     */
    _shellPump() {
        if (!this._shellConnected || this._shellWriting) {
            return;
        }
        if (this._shellWriteRetryAt && Date.now() < this._shellWriteRetryAt) {
            // A previous request failed: wait the backoff out instead of spinning on it.
            clearTimeout(this._shellWriteTimer);
            this._shellWriteTimer = setTimeout(
                () => this._shellPump(), this._shellWriteRetryAt - Date.now());
            return;
        }
        const text = this._shellTextBuffer;
        const keys = this._shellKeys;
        if (!text && !keys.length) {
            return;
        }
        this._shellTextBuffer = "";
        this._shellKeys = [];
        this._shellWriting = true;
        this._shellRpc("/saas/instance/shell/write", { text: text || null, keys: keys })
            // Read back immediately: waiting for the next scheduled poll would show the
            // echo a whole tick late.
            .then((res) => {
                if (!res || res.success === false) {
                    throw new Error((res && res.error) || "write failed");
                }
                this._shellWriteRetryAt = 0;
                if (res.screen !== undefined) {
                    // The response carries the screen captured straight after the keystroke
                    // was applied, so the echo is painted from it: no second round trip.
                    this._shellRender(res);
                    this._shellVeil(false);
                    this._shellStatus(res.dead ? "process exited" : "live");
                    // A command may still be printing, so keep the loop fast for a moment --
                    // but do not spend an extra request now, the screen is already here.
                    this._shellKeepPollingFast();
                } else {
                    // A cached page without the merged screen: fall back to polling for it.
                    this._shellKickPoll();
                }
            })
            .catch(() => {
                // The text and the keys were already taken off the queue. Putting them back
                // in front is what stops a failed round trip from silently swallowing what
                // was typed: the characters are retried instead of being lost.
                this._shellKeys = keys.concat(this._shellKeys);
                this._shellTextBuffer = text + this._shellTextBuffer;
                this._shellWriteRetryAt = Date.now() + SHELL_WRITE_RETRY_MS;
            })
            .finally(() => {
                this._shellWriting = false;
                this._shellPump();
            });
    }
});

export default publicWidget.registry.MyOdooPortal;

// ---------------------------------------------------------------------------
// Developer team members only get GitHub, GitHub Logs, Shell and Odoo Logs.
// The portal templates are shared, so the restriction is applied per role here.
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
    if (!document.querySelector(".myodoo-portal-root")) {
        return;
    }
    let role = "owner";
    try {
        const response = await fetch("/saas/team/list", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {} }),
        });
        const payload = await response.json();
        role = (payload && payload.result && payload.result.my_role) || "owner";
    } catch (err) {
        return; // never block the portal because of this check
    }
    if (role !== "developer") {
        return;
    }
    const allowed = ["logs", "ghlogs", "shell"];
    document.querySelectorAll("[data-tab]").forEach((button) => {
        if (!allowed.includes(button.dataset.tab)) {
            button.style.display = "none";
        }
    });
    // Account settings are the owner's: a developer only keeps Security.
    document.querySelectorAll("[data-setting]").forEach((button) => {
        if (button.dataset.setting !== "security") {
            button.style.display = "none";
        }
    });
    // Portal navigation a developer has no business in
    // (billing, settings, pricing, plan history...).
    const blockedLinks = [/\/my\/saas\/billing/i, /\/my\/saas\/settings/i,
                          /\/saas\/pricing/i, /\/my\/saas\/pricing/i];
    document.querySelectorAll("a[href]").forEach((link) => {
        const href = link.getAttribute("href") || "";
        if (!href || href.startsWith("#")) return;
        if (blockedLinks.some((rx) => rx.test(href))) {
            const block = link.closest(".control-btn, .card, .glass, .option, li") || link;
            block.style.display = "none";
        }
    });
    // Owner-only actions: no new backups, no plan renewal, no repository changes.
    // Backups, domains, logs, GitHub and shell are allowed; these are not.
    const blockedLabels = ["renew plan", "renew subscription", "change plan", "upgrade",
                           "buy extra", "re-sync", "resync", "redeploy",
                           "connect github", "disconnect", "restart instance", "reboot",
                           "stop instance", "delete instance", "terminate", "deploy"];
    document.querySelectorAll("button, a.primary, a.ghost, a.open-link").forEach((el) => {
        const text = (el.textContent || "").trim().toLowerCase();
        if (text && blockedLabels.some((label) => text.includes(label))) {
            el.style.display = "none";
        }
    });
    const firstAllowed = document.querySelector('[data-tab="logs"]');
    if (firstAllowed) {
        firstAllowed.click();
    }
});
