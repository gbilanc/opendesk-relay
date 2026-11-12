/**
 * Dashboard UI for OpenDesk Relay Server.
 * Polls the REST API and updates the UI every 3 seconds.
 */

const REFRESH_INTERVAL = 3000; // ms
let refreshTimer = null;

// ── DOM refs ──────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
    version:       $('#version'),
    uptime:        $('#uptime'),
    peerCount:     $('#peerCount'),
    sessionCount:  $('#sessionCount'),
    deviceCount:   $('#deviceCount'),
    errorCount:    $('#errorCount'),
    peersBody:     $('#peersBody'),
    sessionsBody:  $('#sessionsBody'),
    devicesBody:   $('#devicesBody'),
    infoVersion:   $('#infoVersion'),
    infoUptime:    $('#infoUptime'),
    infoRelayAddr: $('#infoRelayAddr'),
    infoAuth:      $('#infoAuth'),
    infoDashboard: $('#infoDashboard'),
    infoUpdated:   $('#infoUpdated'),
    refreshInfo:   $('#refreshInfo'),
    statusDot:     $('#statusIndicator .status-dot'),
    statusText:    $('#statusIndicator .status-text'),
};

// ── Tab switching ────────────────────────────────────────────────────
$$('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
        $$('.tab-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const tabId = btn.dataset.tab;
        $$('.tab-content').forEach((t) => t.classList.remove('active'));
        $(`#tab-${tabId}`).classList.add('active');
    });
});

// ── Format helpers ──────────────────────────────────────────────────
function fmtDuration(seconds) {
    if (seconds == null || seconds < 0) return '--';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    const parts = [];
    if (d > 0) parts.push(`${d}d`);
    if (h > 0) parts.push(`${h}h`);
    if (m > 0) parts.push(`${m}m`);
    parts.push(`${s}s`);
    return parts.join(' ');
}

function fmtTime(timestamp) {
    if (!timestamp) return '--';
    const d = new Date(timestamp * 1000);
    return d.toLocaleTimeString();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── Authentication ───────────────────────────────────────────────────
// Credentials are kept in sessionStorage (cleared when the tab closes)
// and sent as HTTP Basic / Bearer on every API call.

function savedAuth() {
    return sessionStorage.getItem('relayAuth'); // "Basic xxx" or "Bearer xxx"
}

function authHeader() {
    const auth = savedAuth();
    return auth ? { 'Authorization': auth } : {};
}

function loginSubmit(token, user, pass) {
    let header;
    if (token) {
        header = `Bearer ${token}`;
    } else {
        header = 'Basic ' + btoa(`${user}:${pass}`);
    }
    return fetch('/api/status', { headers: { 'Authorization': header } })
        .then(async (res) => {
            if (!res.ok) {
                const body = await res.text().catch(() => '');
                throw new Error(`HTTP ${res.status}: ${body.slice(0, 120)}`);
            }
            sessionStorage.setItem('relayAuth', header);
            return true;
        })
        .catch((err) => {
            const el = $('#loginError');
            el.textContent = err.message || 'Errore di rete';
            el.hidden = false;
            return false;
        });
}

// ── API calls ────────────────────────────────────────────────────────
async function fetchJSON(url) {
    const res = await fetch(url, { headers: authHeader() });
    if (res.status === 401) {
        showLogin();
        throw new Error('Autenticazione richiesta');
    }
    if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    return res.json();
}

function showLogin() {
    $('#loginOverlay').hidden = false;
    $('#loginUser').focus();
    refreshTimer && clearInterval(refreshTimer);
    refreshTimer = null;
}

function hideLogin() {
    $('#loginOverlay').hidden = true;
    $('#loginError').hidden = true;
    if (!refreshTimer) refreshTimer = setInterval(refresh, REFRESH_INTERVAL);
}

// ── UI updates ───────────────────────────────────────────────────────
function updateDashboard(status) {
    // Status indicator
    dom.statusDot.style.background = '#3fb950';
    dom.statusText.textContent = 'Connected';
    dom.statusText.style.color = '#7ee787';

    // Version
    dom.version.textContent = `v${status.version || '0.0.0'}`;

    // Uptime
    dom.uptime.textContent = `Uptime: ${fmtDuration(status.uptime_seconds)}`;

    // Cards
    dom.peerCount.textContent = status.connections_active ?? 0;
    dom.sessionCount.textContent = status.sessions_active ?? 0;
    dom.deviceCount.textContent = status.devices_online ?? 0;
    dom.errorCount.textContent = status.config?.errors_total ?? 0;
}

function updatePeers(peers) {
    if (!peers || peers.length === 0) {
        dom.peersBody.innerHTML = '<tr><td colspan="7" class="empty">No peers connected</td></tr>';
        return;
    }

    dom.peersBody.innerHTML = peers.map((p) => `
        <tr>
            <td><code>${escapeHtml(p.peer_id)}</code></td>
            <td>${escapeHtml(p.device_name || '—')}</td>
            <td><code>${escapeHtml(p.session_id || '—')}</code></td>
            <td>${p.paired_with ? `<code>${escapeHtml(p.paired_with)}</code>` : '—'}</td>
            <td>${escapeHtml(p.address)}</td>
            <td>${fmtDuration(p.connected_seconds)}</td>
            <td>
                <button class="btn-disconnect" data-peer="${escapeHtml(p.peer_id)}">
                    Disconnect
                </button>
            </td>
        </tr>
    `).join('');

    // Attach disconnect handlers
    dom.peersBody.querySelectorAll('.btn-disconnect').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const peerId = btn.dataset.peer;
            if (!confirm(`Disconnect peer "${peerId}"?`)) return;
            try {
                await fetch(`/api/peers/${encodeURIComponent(peerId)}`, { method: 'DELETE' });
            } catch (err) {
                console.error('Failed to disconnect peer:', err);
            }
        });
    });
}

function updateSessions(sessions) {
    if (!sessions || sessions.length === 0) {
        dom.sessionsBody.innerHTML = '<tr><td colspan="4" class="empty">No active sessions</td></tr>';
        return;
    }

    dom.sessionsBody.innerHTML = sessions.map((s) => `
        <tr>
            <td><code>${escapeHtml(s.session_id)}</code></td>
            <td><code>${escapeHtml(s.host_peer_id)}</code></td>
            <td>${escapeHtml(s.host_device_name || '—')}</td>
            <td>
                <span class="status-badge ${s.host_online ? 'online' : 'offline'}">
                    ${s.host_online ? 'Online' : 'Offline'}
                </span>
            </td>
        </tr>
    `).join('');
}

function updateDevices(devices) {
    if (!devices || devices.length === 0) {
        dom.devicesBody.innerHTML = '<tr><td colspan="5" class="empty">No devices registered</td></tr>';
        return;
    }

    dom.devicesBody.innerHTML = devices.map((d) => `
        <tr>
            <td><code>${escapeHtml(d.device_id)}</code></td>
            <td>${escapeHtml(d.device_name)}</td>
            <td><code>${escapeHtml(d.session_id || '—')}</code></td>
            <td>
                <span class="status-badge ${d.online ? 'online' : 'offline'}">
                    ${d.online ? 'Online' : 'Offline'}
                </span>
            </td>
            <td>${d.online ? fmtDuration(d.last_seen_seconds) : '—'}</td>
        </tr>
    `).join('');
}

function updateInfo(status, peers) {
    dom.infoVersion.textContent = `v${status.version || '0.0.0'}`;
    dom.infoUptime.textContent = fmtDuration(status.uptime_seconds);

    const relayConfig = status.config || {};
    dom.infoRelayAddr.textContent = `${relayConfig.relay_host || '?'}:${relayConfig.relay_port || '?'}`;
    dom.infoAuth.textContent = relayConfig.auth_enabled ? '✅ Enabled' : '❌ Disabled';
    dom.infoDashboard.textContent = relayConfig.admin_enabled ? '✅ Active' : '❌ Disabled';
    dom.infoUpdated.textContent = fmtTime(status.timestamp);
}

// ── Main refresh ─────────────────────────────────────────────────────
async function refresh() {
    try {
        const [status, peers, sessions, devices] = await Promise.all([
            fetchJSON('/api/status'),
            fetchJSON('/api/peers'),
            fetchJSON('/api/sessions'),
            fetchJSON('/api/devices'),
        ]);

        updateDashboard(status);
        updatePeers(peers);
        updateSessions(sessions);
        updateDevices(devices);
        updateInfo(status, peers);

        dom.refreshInfo.textContent = `Last updated: ${new Date().toLocaleTimeString()} · Auto-refresh every 3s`;
    } catch (err) {
        console.error('Refresh failed:', err);
        dom.statusDot.style.background = '#f85149';
        dom.statusText.textContent = 'Error';
        dom.statusText.style.color = '#f85149';
        dom.refreshInfo.textContent = `⚠ Connection error: ${err.message}`;
    }
}

// ── Login form wiring ─────────────────────────────────────────────────
$('#loginForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const user = $('#loginUser').value.trim();
    const useToken = $('#loginTokenMode').checked;
    const pass = $('#loginPass').value;
    const ok = useToken
        ? await loginSubmit(user)                // username field holds the API token
        : await loginSubmit(null, user, pass);   // admin credentials (Basic)
    if (ok) {
        hideLogin();
        refresh();
    }
    // on failure loginSubmit() already shows the detailed error
});

// In token mode the password field is irrelevant (and may be autofilled
// by the browser): disable it so it can't be submitted accidentally.
$('#loginTokenMode').addEventListener('change', (ev) => {
    $('#loginPass').disabled = ev.target.checked;
    if (ev.target.checked) $('#loginPass').value = '';
});

// ── Init ─────────────────────────────────────────────────────────────
if (savedAuth()) {
    refresh();
    refreshTimer = setInterval(refresh, REFRESH_INTERVAL);
} else {
    showLogin();
}
