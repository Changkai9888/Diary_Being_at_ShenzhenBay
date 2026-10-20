const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/session_guard.js'), 'utf8');

const flush = () => new Promise(resolve => setImmediate(resolve));
function setup() {
    let now = 1000000;
    let deadline = now + 60000;
    let fail = false;
    let status = 200;
    const redirects = [];
    const timers = new Map();
    const events = new Map();
    let timerId = 0;
    const workspace = { inert: true };
    const overlay = { hidden: false, textContent: '' };
    const config = { expires_at: deadline / 1000, server_now: now / 1000,
        status_url: '/session_status', login_url: '/login?next=/' };
    const document = {
        hidden: false,
        getElementById: id => ({ diaryWorkspace: workspace, sessionGuard: overlay,
            sessionConfig: { textContent: JSON.stringify(config) } })[id],
        addEventListener: (name, fn) => events.set('document:' + name, fn)
    };
    const location = { href: 'http://localhost:8081/', origin: 'http://localhost:8081',
        replace: url => redirects.push(url) };
    const window = { location, addEventListener: (name, fn) => events.set('window:' + name, fn),
        fetch: async () => {
            if (fail) throw new Error('offline');
            return { status, ok: status === 200, redirected: false,
                json: async () => ({ authenticated: true, expires_at: deadline / 1000,
                    server_now: now / 1000 }) };
        } };
    class Clock extends Date { static now() { return now; } }
    vm.runInNewContext(source, { window, document, location, Date: Clock, URL, AbortSignal,
        performance: { now: () => now },
        setTimeout: (fn, delay) => { timers.set(++timerId, { fn, at: now + delay }); return timerId; },
        clearTimeout: id => timers.delete(id),
        setInterval: fn => events.set('heartbeat', fn)
    });
    return { workspace, overlay, window, document, redirects, timers,
        setNow: value => { now = value; }, setFailure: value => { fail = value; },
        setStatus: value => { status = value; },
        fire: (name, event) => events.get(name)(event),
        fireTimers: () => { for (const [id, timer] of [...timers]) {
            if (timer.at <= now) { timers.delete(id); timer.fn(); }
        } }
    };
}

test('locks before validation then permits editing with valid session', async () => {
    const s = setup();
    assert.equal(s.workspace.inert, true);
    await flush();
    assert.equal(s.workspace.inert, false);
    assert.equal(s.overlay.hidden, true);
});

test('deadline automatically locks and redirects without any user action', async () => {
    const s = setup(); await flush();
    s.setNow(1060000); s.fireTimers();
    assert.equal(s.workspace.inert, true);
    assert.equal(s.window.diarySession.expired, true);
    assert.deepEqual(s.redirects, ['/login?next=/']);
});

test('suspended browser cannot accept the first edit after expiry', async () => {
    const s = setup(); await flush();
    s.setNow(1060001); // Deliberately do not run the expired timer.
    let prevented = false;
    s.fire('document:beforeinput', { preventDefault: () => { prevented = true; },
        stopImmediatePropagation() {} });
    assert.equal(prevented, true);
    assert.equal(s.redirects.length, 1);
});

test('session revoked in another tab redirects on heartbeat', async () => {
    const s = setup(); await flush(); s.setStatus(401);
    await s.fire('heartbeat');
    assert.equal(s.workspace.inert, true);
    assert.equal(s.redirects.length, 1);
});

test('unauthenticated save response redirects immediately', async () => {
    const s = setup(); await flush(); s.setStatus(401);
    await assert.rejects(s.window.fetch('/save_log', { method: 'POST' }));
    assert.equal(s.redirects.length, 1);
});

test('network failure pauses editing and online validation restores it', async () => {
    const s = setup(); await flush(); s.setFailure(true);
    await s.fire('heartbeat');
    assert.equal(s.workspace.inert, true);
    assert.equal(s.redirects.length, 0);
    s.setFailure(false); await s.fire('window:online');
    assert.equal(s.workspace.inert, false);
});

test('back/forward cache and tab switches require validation before editing', async () => {
    const s = setup(); await flush();
    s.fire('window:pagehide');
    assert.equal(s.workspace.inert, true);
    s.setStatus(401); await s.fire('window:pageshow');
    assert.equal(s.workspace.inert, true);
    assert.equal(s.redirects.length, 1);
});
