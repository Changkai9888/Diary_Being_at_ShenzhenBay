(() => {
    const config = JSON.parse(document.getElementById('sessionConfig').textContent);
    const workspace = document.getElementById('diaryWorkspace');
    const overlay = document.getElementById('sessionGuard');
    const nativeFetch = window.fetch.bind(window);
    let remaining = (config.expires_at - config.server_now) * 1000;
    let wallAnchor = Date.now();
    let monotonicAnchor = performance.now();
    let expiryTimer;
    let checking = null;
    let expired = false;
    window.diarySession = { get expired() { return expired; } };

    function lock(message = '正在确认登录状态…') {
        workspace.inert = true;
        overlay.hidden = false;
        overlay.textContent = message;
    }

    function expire() {
        if (expired) return;
        expired = true;
        clearTimeout(expiryTimer);
        lock('登录已过期，正在返回登录页…');
        window.location.replace(config.login_url);
    }

    function timeLeft() {
        return remaining - Math.max(Date.now() - wallAnchor, performance.now() - monotonicAnchor);
    }

    function enforceExpiry() {
        if (!Number.isFinite(remaining) || timeLeft() <= 0) expire();
        return expired;
    }

    function armTimer() {
        clearTimeout(expiryTimer);
        if (!enforceExpiry()) expiryTimer = setTimeout(armTimer, Math.max(1, timeLeft()));
    }

    function isAuthFailure(response) {
        return response.status === 401 || response.status === 403 ||
            (response.redirected && new URL(response.url, location.href).pathname ===
                new URL(config.login_url, location.href).pathname);
    }

    // Every diary API call reacts immediately when the server rejects the session.
    window.fetch = async (input, options) => {
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        const local = url.origin === location.origin;
        if (local && enforceExpiry()) throw new Error('登录已过期');
        const response = await nativeFetch(input, options);
        if (local && isAuthFailure(response)) {
            expire();
            throw new Error('登录已过期');
        }
        return response;
    };

    function checkSession(blockEditing = false) {
        if (enforceExpiry()) return Promise.resolve();
        if (blockEditing) lock();
        if (checking) return checking;
        const started = performance.now();
        checking = (async () => {
            try {
                const response = await nativeFetch(config.status_url, {
                    cache: 'no-store', credentials: 'same-origin',
                    signal: AbortSignal.timeout(10000)
                });
                if (isAuthFailure(response)) { expire(); return; }
                if (!response.ok) throw new Error('status unavailable');
                const data = await response.json();
                if (!data.authenticated) { expire(); return; }
                // Use server time, subtracting the full round trip conservatively.
                remaining = (data.expires_at - data.server_now) * 1000 - (performance.now() - started);
                wallAnchor = Date.now();
                monotonicAnchor = performance.now();
                armTimer();
                if (!expired && !document.hidden) {
                    workspace.inert = false;
                    overlay.hidden = true;
                }
            } catch (_) {
                if (!expired) lock('暂时无法确认登录状态，已暂停编辑；网络恢复后自动重试。');
            } finally {
                checking = null;
            }
        })();
        return checking;
    }

    // Catch suspended timers before the first edit after a browser/OS resume.
    for (const event of ['beforeinput', 'keydown', 'pointerdown', 'submit']) {
        document.addEventListener(event, (e) => {
            if (enforceExpiry() || workspace.inert) {
                e.preventDefault();
                e.stopImmediatePropagation();
            }
        }, true);
    }
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) lock();
        else checkSession(true);
    });
    window.addEventListener('pagehide', () => lock());
    window.addEventListener('pageshow', () => checkSession(true));
    window.addEventListener('focus', () => checkSession(true));
    window.addEventListener('online', () => checkSession(true));
    window.addEventListener('offline', () => lock('网络已断开，已暂停编辑；连接恢复后自动确认登录状态。'));
    setInterval(() => checkSession(), 15000);
    armTimer();
    checkSession(true);
})();
