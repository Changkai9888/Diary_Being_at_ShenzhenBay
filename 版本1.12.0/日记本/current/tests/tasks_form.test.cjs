const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/calendar.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../static/css/style.css'), 'utf8');
const response = data => ({ ok: true, status: 200, json: async () => data });
function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

function setup({ deferCalendars = false } = {}) {
    const logRequests = [];
    const saveRequests = [];
    const calendarRequests = [];
    const alerts = [];
    const fields = Object.create(null);
    for (const match of template.matchAll(/<(?:textarea|select)\b[^>]*\bname="([^"]+)"[^>]*>/g)) {
        const listeners = new Map();
        fields[match[1]] = { value: '', addEventListener: (name, fn) => listeners.set(name, fn), listeners };
    }
    const form = { elements: { namedItem: name => fields[name] } };
    const nodes = { logForm: form, logTitle: { textContent: '' }, monthTitle: { textContent: '' },
        calendarBody: { innerHTML: '' }, globalSaveBtn: { disabled: false, textContent: '💾 保存日志' } };
    const document = {
        body: { dataset: { diaryBase: '/shenzhen-bay-diary/' }, classList: { toggle() {} } },
        getElementById: id => nodes[id] || null,
        addEventListener() {}
    };
    const window = { location: { href: 'http://localhost/shenzhen-bay-diary/' },
        fetch: async (url, options) => {
            const pathname = new URL(url).pathname;
            if (pathname.endsWith('/get_calendar')) {
                const pending = deferred();
                calendarRequests.push({ ...pending, url });
                if (!deferCalendars) pending.resolve(response({ weeks: [] }));
                return pending.promise;
            }
            if (pathname.endsWith('/get_log')) {
                const pending = deferred();
                logRequests.push({ ...pending, url });
                return pending.promise;
            }
            if (pathname.endsWith('/save_log')) {
                const pending = deferred();
                saveRequests.push({ ...pending, data: JSON.parse(options.body) });
                return pending.promise;
            }
            throw new Error(`Unexpected request: ${url}`);
        } };
    // HTMLFormControlsCollection includes inputs associated using form="logForm".
    class FormDataForAssociatedControls {
        constructor(owner) {
            assert.equal(owner, form);
            this.values = Object.entries(fields).map(([name, field]) => [name, field.value]);
        }
        entries() { return this.values[Symbol.iterator](); }
    }
    const context = vm.createContext({ document, window, URL, Date, FormData: FormDataForAssociatedControls,
        alert: message => alerts.push(message), console: { error() {}, warn() {} }, setTimeout, clearTimeout,
        setInterval, clearInterval });
    vm.runInContext(source, context);
    vm.runInContext('bindSharedFieldDrafts()', context);
    return {
        fields, nodes, logRequests, saveRequests, calendarRequests, alerts,
        select: date => context.selectDate(date),
        month: offset => context.changeMonth(offset),
        input: (name, value) => { fields[name].value = value; fields[name].listeners.get('input')?.(); },
        save: () => context.saveLog({ preventDefault() {}, target: form }),
        state: field => vm.runInContext(`({ ...sharedFieldState.get(${JSON.stringify(field)}) })`, context),
        resolveLog: (index, data) => logRequests[index].resolve(response(data)),
        resolveSave: (index, data = { status: 'success' }) => saveRequests[index].resolve(response(data))
    };
}

test('task field is external to the diary form, linked to it, four rows and vertically resizable', () => {
    const field = template.match(/<textarea\b[^>]*id="inFlightTasks"[^>]*>/)[0];
    assert.match(field, /name="在途任务"/);
    assert.match(field, /form="logForm"/);
    assert.match(field, /rows="4"/);
    assert.ok(template.indexOf(field) < template.indexOf('<form id="logForm"'));
    assert.match(css, /textarea\[form="logForm"\]\s*\{[^}]*resize:\s*vertical/);
    assert.match(template, /workspace-tab workspace-tab-cognitive/);
    assert.doesNotMatch(template, /系统提示词|用户提示词|AI分析|AI助手|继续聊聊/);
    assert.doesNotMatch(source, /analyze_ai|chat_ai|get_ai_prompt_defaults|save_prompt_part/);
});

test('loads the shared fields and submits the external task field with the selected diary date', async () => {
    const s = setup();
    const loading = s.select('2026-10-17');
    s.resolveLog(0, { '在途任务': '项目 A\n项目 B', '待提升属性': '专注', '计划与摘要': '当天计划' });
    await loading;
    assert.equal(s.fields['在途任务'].value, '项目 A\n项目 B');
    assert.equal(s.fields['计划与摘要'].value, '当天计划');
    s.input('在途任务', '下一步');
    const saving = s.save();
    assert.equal(s.saveRequests[0].data.date, '2026-10-17');
    assert.equal(s.saveRequests[0].data['在途任务'], '下一步');
    assert.equal(s.saveRequests[0].data['待提升属性'], '专注');
    s.resolveSave(0);
    await saving;
    assert.equal(s.state('在途任务').dirty, false);
});

test('an inherited plan is displayed and saved unchanged as the selected date content', async () => {
    const s = setup();
    const loading = s.select('2026-10-20');
    s.resolveLog(0, { '计划与摘要': '沿用之前已保存的计划\n下一步' });
    await loading;
    assert.equal(s.fields['计划与摘要'].value, '沿用之前已保存的计划\n下一步');
    const saving = s.save();
    assert.equal(s.saveRequests[0].data.date, '2026-10-20');
    assert.equal(s.saveRequests[0].data['计划与摘要'], '沿用之前已保存的计划\n下一步');
    s.resolveSave(0);
    await saving;
});

test('an unsaved plan draft is replaced by the next selected date response', async () => {
    const s = setup();
    let loading = s.select('2026-10-20');
    s.resolveLog(0, { '计划与摘要': '之前已保存的计划' });
    await loading;
    s.input('计划与摘要', '未保存的新草稿');
    loading = s.select('2026-10-21');
    s.resolveLog(1, { '计划与摘要': '之前已保存的计划' });
    await loading;
    assert.equal(s.fields['计划与摘要'].value, '之前已保存的计划');
    assert.equal(s.saveRequests.length, 0);
});

test('date and month changes retain shared drafts while daily fields change', async () => {
    const s = setup();
    let loading = s.select('2026-10-17');
    s.resolveLog(0, { '在途任务': '已存任务', '待提升属性': '已存属性', '上午': '17 日' });
    await loading;
    s.input('在途任务', '未保存任务');
    s.input('待提升属性', '未保存属性');
    s.month(1);
    loading = s.select('2026-11-18');
    s.resolveLog(1, { '在途任务': '旧值', '待提升属性': '旧属性', '上午': '18 日' });
    await loading;
    assert.equal(s.fields['在途任务'].value, '未保存任务');
    assert.equal(s.fields['待提升属性'].value, '未保存属性');
    assert.equal(s.fields['上午'].value, '18 日');
});

test('typing before the first response is preserved and saving waits for the log to load', async () => {
    const s = setup();
    const loading = s.select('2026-10-17');
    s.input('在途任务', '刚输入的任务');
    await s.save();
    assert.equal(s.saveRequests.length, 0);
    assert.match(s.alerts[0], /加载/);
    s.resolveLog(0, { '在途任务': '服务器任务', '待提升属性': '服务器属性' });
    await loading;
    assert.equal(s.fields['在途任务'].value, '刚输入的任务');
    assert.equal(s.fields['待提升属性'].value, '服务器属性');
});

test('a delayed previous date cannot overwrite the currently selected date or shared draft', async () => {
    const s = setup();
    const previous = s.select('2026-10-17');
    const current = s.select('2026-10-18');
    s.resolveLog(1, { '在途任务': '当前任务', '上午': '18 日' });
    await current;
    s.input('在途任务', '保留草稿');
    s.resolveLog(0, { '在途任务': '旧任务', '上午': '17 日' });
    await previous;
    assert.equal(s.fields['在途任务'].value, '保留草稿');
    assert.equal(s.fields['上午'].value, '18 日');
    assert.equal(s.nodes.logTitle.textContent, '2026年10月18日日志');
});

test('a delayed calendar response cannot reset the month the user just selected', async () => {
    const s = setup({ deferCalendars: true });
    const loading = s.select('2026-10-17');
    s.month(1);
    s.calendarRequests[1].resolve(response({ month_title: '2026年11月', weeks: [] }));
    s.resolveLog(0, { '在途任务': '任务' });
    s.calendarRequests[0].resolve(response({ month_title: '2026年10月', weeks: [] }));
    await loading;
    assert.equal(s.nodes.monthTitle.textContent, '2026年11月');
});

test('editing while a save is pending remains dirty and is submitted by the next save', async () => {
    const s = setup();
    const loading = s.select('2026-10-17');
    s.resolveLog(0, { '在途任务': '初始任务' });
    await loading;
    s.input('在途任务', '提交版本');
    let saving = s.save();
    s.input('在途任务', '请求期间的新版本');
    s.resolveSave(0);
    await saving;
    assert.equal(s.fields['在途任务'].value, '请求期间的新版本');
    assert.equal(s.state('在途任务').dirty, true);
    saving = s.save();
    assert.equal(s.saveRequests[1].data['在途任务'], '请求期间的新版本');
    s.resolveSave(1);
    await saving;
    assert.equal(s.state('在途任务').dirty, false);
});

test('HTTP 200 without application success retains the shared draft and reports failure', async () => {
    const s = setup();
    const loading = s.select('2026-10-17');
    s.resolveLog(0, { '在途任务': '初始任务' });
    await loading;
    s.input('在途任务', '尚未保存');
    const saving = s.save();
    s.resolveSave(0, { status: 'error', message: '存储失败' });
    await saving;
    assert.equal(s.fields['在途任务'].value, '尚未保存');
    assert.equal(s.state('在途任务').dirty, true);
    assert.match(s.alerts.at(-1), /保存失败.*存储失败/s);
});

test('explicit empty tasks are submitted as a clear and a fresh page loads the latest saved state', async () => {
    const s = setup();
    let loading = s.select('2026-10-17');
    s.resolveLog(0, { '在途任务': '原任务' });
    await loading;
    s.input('在途任务', '');
    const saving = s.save();
    assert.equal(s.saveRequests[0].data['在途任务'], '');
    s.resolveSave(0);
    await saving;
    loading = s.select('2026-10-18');
    s.resolveLog(1, { '在途任务': '较旧的异步快照' });
    await loading;
    assert.equal(s.fields['在途任务'].value, '');
    const refreshed = setup();
    loading = refreshed.select('2026-10-18');
    refreshed.resolveLog(0, { '在途任务': '另一页面最新保存的任务' });
    await loading;
    assert.equal(refreshed.fields['在途任务'].value, '另一页面最新保存的任务');
});
