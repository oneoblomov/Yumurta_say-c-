/**
 * app.js - Alpine.js Ana Uygulama
 * =================================
 * WebSocket bağlantısı, global state yönetimi,
 * toast bildirimleri, tema ve yardımcı fonksiyonlar.
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
    console.log(`[${type.toUpperCase()}] ${message}`);
}

// ============================================================ Theme Helpers
function getStoredTheme() {
    return localStorage.getItem('app-theme') || 'system';
}

function applyTheme(theme) {
    const html = document.documentElement;
    const body = document.body;
    const setClass = (add) => {
        if (add) {
            html.classList.add('dark');
            body.classList.add('dark');
        } else {
            html.classList.remove('dark');
            body.classList.remove('dark');
        }
    };

    if (theme === 'dark') {
        setClass(true);
    } else if (theme === 'light') {
        setClass(false);
    } else {
        // system: follow prefers-color-scheme
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        setClass(prefersDark);
    }
    localStorage.setItem('app-theme', theme);
}

// Apply theme immediately on page load (before Alpine init) to avoid flash
(function () {
    applyTheme(getStoredTheme());
})();

// Listen for system theme changes
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (getStoredTheme() === 'system') applyTheme('system');
});

// ============================================================ Alpine App
function app() {
    return {
        // Sidebar
        sidebarOpen: false,
        sidebarCollapsed: localStorage.getItem('sidebar-collapsed') === 'true',
        currentPage: 'dashboard',

        // Theme
        theme: getStoredTheme(),

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

            // Apply theme
            applyTheme(this.theme);

            // Apply sidebar state
            this._applySidebarCollapsed();

            // Start WebSocket
            this.connectWS();

            // Load initial alerts
            this.fetchAlerts();

            // Periodic alert refresh
            setInterval(() => this.fetchAlertCount(), 10000);
        },

        // ---------------------------------------- Theme
        setTheme(theme) {
            this.theme = theme;
            applyTheme(theme);
            // Persist to settings (non-blocking)
            fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ theme }),
            }).catch(() => {});
        },


        // ---------------------------------------- Sidebar collapse
        toggleSidebarCollapsed() {
            this.sidebarCollapsed = !this.sidebarCollapsed;
            localStorage.setItem('sidebar-collapsed', this.sidebarCollapsed);
            this._applySidebarCollapsed();
        },


        _applySidebarCollapsed() {
            const wrapper = document.getElementById('main-wrapper');
            if (wrapper) {
                if (this.sidebarCollapsed) {
                    wrapper.classList.remove('lg:ml-64');
                    wrapper.classList.add('lg:ml-16');
                } else {
                    wrapper.classList.remove('lg:ml-16');
                    wrapper.classList.add('lg:ml-64');
                }
            }
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

                        if (data.status) {
                            this.status = { ...this.status, ...data.status };
                        }

                        if (data.events && data.events.length > 0) {
                            for (const ev of data.events) {
                                this.handleEvent(ev);
                            }
                        }

                        if (data.alert_count !== undefined) {
                            this.alertCount = data.alert_count;
                        }
                    } catch (e) { }
                };

                this.ws.onclose = () => {
                    this.ws = null;
                    if (this.wsReconnectTimer) clearTimeout(this.wsReconnectTimer);
                    this.wsReconnectTimer = setTimeout(() => this.connectWS(), 2000);
                };

                this.ws.onerror = () => { this.ws?.close(); };
            } catch (e) {
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
            if (ev.type === 'alert') {
                showToast(ev.message, ev.severity === 'critical' ? 'error' : 'warning');
                this.fetchAlerts();
            } else if (ev.type === 'goal_reached') {
                showToast(`🎯 Hedef tamamlandı!`, 'success', 5000);
            }
        },

        // ---------------------------------------- Alerts
        async fetchAlerts() {
            try {
                const r = await fetch('/api/alerts');
                this.alerts = await r.json();
                this.alertCount = this.alerts.filter(a => !a.acknowledged).length;
            } catch (e) { }
        },

        async fetchAlertCount() {
            try {
                const r = await fetch('/api/alerts/count');
                const d = await r.json();
                this.alertCount = d.count;
            } catch (e) { }
        },

        async ackAlert(id) {
            await fetch(`/api/alerts/${id}/ack`, { method: 'POST' });
            this.fetchAlerts();
        },

        async ackAllAlerts() {
            await fetch('/api/alerts/ack-all', { method: 'POST' });
            this.fetchAlerts();
        }
    };
}
