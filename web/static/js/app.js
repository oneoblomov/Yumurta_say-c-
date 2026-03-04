/**
 * app.js - Alpine.js Ana Uygulama
 * =================================
 * WebSocket bağlantısı, global state yönetimi,
 * toast bildirimleri ve yardımcı fonksiyonlar.
 */

// ============================================================ Toast
function showToast(message, type = 'info', duration = 3000) {
    const appEl = document.querySelector('[x-data]');
    if (appEl && appEl.__x) {
        const data = appEl.__x.$data || appEl._x_dataStack?.[0];
        if (data) {
            data.toast = { show: true, message, type };
            setTimeout(() => { data.toast.show = false; }, duration);
            return;
        }
    }
    // Fallback: console
    console.log(`[${type.toUpperCase()}] ${message}`);
}

// ============================================================ Alpine App
function app() {
    return {
        // Sidebar
        sidebarOpen: false,
        currentPage: 'dashboard',

        // Status (updated via WebSocket)
        status: {
            running: false,
            paused: false,
            debug: false,
            fps: 0,
            total_count: 0,
            active_tracks: 0,
            frame_count: 0,
            session_id: null,
            resolution: 'N/A',
        },

        // Alerts
        showAlerts: false,
        alerts: [],
        alertCount: 0,

        // Toast
        toast: { show: false, message: '', type: 'info' },

        // i18n (injected from base.html via __appInit)
        lang: window.__appInit?.lang || 'tr',
        langs: window.__appInit?.langs || {},
        t: window.__appInit?.t || {},

        // WebSocket
        ws: null,
        wsReconnectTimer: null,

        init() {
            // Determine current page from URL
            const path = window.location.pathname.replace('/', '') || 'dashboard';
            this.currentPage = path;

            // Start WebSocket
            this.connectWS();

            // Load initial alerts
            this.fetchAlerts();

            // Periodic alert refresh
            setInterval(() => this.fetchAlertCount(), 10000);
        },

        // ---------------------------------------- WebSocket
        connectWS() {
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const url = `${protocol}//${location.host}/ws`;

            try {
                this.ws = new WebSocket(url);

                this.ws.onmessage = (event) => {
                    try {
                        const data = JSON.parse(event.data);

                        // Update status
                        if (data.status) {
                            this.status = { ...this.status, ...data.status };
                        }

                        // Process events
                        if (data.events && data.events.length > 0) {
                            for (const ev of data.events) {
                                this.handleEvent(ev);
                            }
                        }

                        // Alert count
                        if (data.alert_count !== undefined) {
                            this.alertCount = data.alert_count;
                        }
                    } catch (e) { }
                };

                this.ws.onclose = () => {
                    this.ws = null;
                    // Reconnect after 2s
                    if (this.wsReconnectTimer) clearTimeout(this.wsReconnectTimer);
                    this.wsReconnectTimer = setTimeout(() => this.connectWS(), 2000);
                };

                this.ws.onerror = () => {
                    this.ws?.close();
                };
            } catch (e) {
                // Fallback: poll status
                setInterval(() => this.pollStatus(), 1000);
            }
        },

        async pollStatus() {
            try {
                const r = await fetch('/api/pipeline/status');
                this.status = await r.json();
            } catch (e) { }
        },

        handleEvent(ev) {
            if (ev.type === 'egg_counted') {
                // Could play sound, flash, etc.
            } else if (ev.type === 'alert') {
                showToast(ev.message, ev.severity === 'critical' ? 'error' : 'warning');
                this.fetchAlerts();
            } else if (ev.type === 'goal_reached') {
                showToast(
                    `🎯 ${ev.type === 'daily' ? 'Günlük' : 'Haftalık'} hedef tamamlandı!`,
                    'success',
                    5000
                );
            }
        },

        // ---------------------------------------- Alerts
        async fetchAlerts() {
            try {
                const r = await fetch('/api/alerts?unack_only=false&limit=20');
                this.alerts = await r.json();
                this.alertCount = this.alerts.filter(a => !a.acknowledged).length;
            } catch (e) { }
        },

        async fetchAlertCount() {
            try {
                const r = await fetch('/api/alerts/count');
                const d = await r.json();
                this.alertCount = d.count || 0;
            } catch (e) { }
        },

        async ackAlert(id) {
            await fetch(`/api/alerts/${id}/ack`, { method: 'POST' });
            this.fetchAlerts();
        },

        async ackAllAlerts() {
            await fetch('/api/alerts/ack-all', { method: 'POST' });
            this.fetchAlerts();
        },

        // ---------------------------------------- Language
        async setLanguage(lang) {
            await fetch('/api/settings/language', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ language: lang }),
            });
            location.reload();
        },
    };
}

// ============================================================ HTMX Events
document.body.addEventListener('htmx:afterSwap', function () {
    // Re-create Lucide icons after HTMX content swap
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
});

// Handle browser back/forward
window.addEventListener('popstate', function () {
    const path = window.location.pathname.replace('/', '') || 'dashboard';
    htmx.ajax('GET', window.location.pathname, {
        target: '#main-content',
        swap: 'innerHTML',
    });
    // Update sidebar
    const appEl = document.querySelector('[x-data]');
    if (appEl?.__x) {
        const data = appEl.__x.$data || appEl._x_dataStack?.[0];
        if (data) data.currentPage = path;
    }
});
