/** @odoo-module **/

import publicWidget from '@web/legacy/js/public/public_widget';
import { ConnectionLostError, rpc, RPCError } from '@web/core/network/rpc';

/**
 * Live handling of the SaaS payment status page (/payment/status).
 *
 * Polls /payment/status/poll like Odoo does, but instead of blindly redirecting it
 * shows a clear state to the customer:
 *   - processing : "do not refresh" spinner + progress (our own UI)
 *   - success    : payment confirmed -> redirect to the landing route, which the SaaS
 *                  controller turns into the instance page + preloader for what was paid
 *   - declined   : "Oops! your payment was declined / change card" with a retry button
 */
const SUCCESS_STATES = new Set(['done', 'authorized']);
const DECLINED_STATES = new Set(['cancel', 'error']);

publicWidget.registry.SaasPaymentStatus = publicWidget.Widget.extend({
    selector: '#saasPaymentStatus',

    timeout: 2000,
    pollCount: 0,
    progress: 25,

    async start() {
        this.retryUrl = this.el.dataset.retryUrl || '/shop/checkout';
        this.instanceUrl = this.el.dataset.instanceUrl || '';

        document.getElementById('saasPayRetryBtn')?.addEventListener('click', () => {
            window.location = this.retryUrl;
        });

        this._animateProgress();
        this._poll();
        return this._super.apply(this, arguments);
    },

    /** Slowly fill the progress bar while we wait for the provider. */
    _animateProgress() {
        this._progressTimer = setInterval(() => {
            const fill = document.getElementById('saasPayProgressFill');
            if (!fill) return;
            if (this.progress < 92) {
                this.progress += Math.max(1, Math.round((92 - this.progress) / 8));
                fill.style.width = this.progress + '%';
            }
        }, 900);
    },

    _setState(state) {
        const map = {
            processing: 'saasPayProcessing',
            success: 'saasPaySuccess',
            declined: 'saasPayDeclined',
        };
        for (const [name, id] of Object.entries(map)) {
            const el = document.getElementById(id);
            if (el) el.style.display = name === state ? '' : 'none';
        }
        if (state !== 'processing' && this._progressTimer) {
            clearInterval(this._progressTimer);
            this._progressTimer = null;
        }
        if (state === 'success') {
            const fill = document.getElementById('saasPayProgressFill');
            if (fill) fill.style.width = '100%';
        }
    },

    _poll() {
        this._updateTimeout();
        setTimeout(async () => {
            let values;
            try {
                values = await rpc('/payment/status/poll', { csrf_token: odoo.csrf_token });
            } catch (error) {
                const isRetry = error instanceof ConnectionLostError
                    || (error instanceof RPCError && error.data && error.data.message === 'retry');
                if (isRetry) {
                    this._poll();
                    return;
                }
                // An unexpected error: don't leave the customer hanging on the spinner.
                this._setState('declined');
                return;
            }

            const { state, landing_route: landingRoute } = values || {};
            if (SUCCESS_STATES.has(state)) {
                this._setState('success');
                // The landing route is /shop/payment/validate?tx_id=... which the SaaS
                // controller turns into the instance page carrying the preloader flag.
                setTimeout(() => { window.location = landingRoute; }, 700);
            } else if (DECLINED_STATES.has(state)) {
                this._setState('declined');
            } else {
                this._poll();
            }
        }, this.timeout);
    },

    _updateTimeout() {
        if (this.pollCount >= 1 && this.pollCount < 10) {
            this.timeout = 2500;
        } else if (this.pollCount >= 10 && this.pollCount < 20) {
            this.timeout = 6000;
        } else if (this.pollCount >= 20) {
            this.timeout = 15000;
        }
        this.pollCount++;
    },

    destroy() {
        if (this._progressTimer) {
            clearInterval(this._progressTimer);
        }
        return this._super.apply(this, arguments);
    },
});

export default publicWidget.registry.SaasPaymentStatus;
