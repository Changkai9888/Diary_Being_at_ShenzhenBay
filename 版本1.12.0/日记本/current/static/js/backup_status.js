(() => {
    const status = document.getElementById('backupStatus');
    if (!status) return;
    const label = document.getElementById('backupStatusLabel');
    const successTime = document.getElementById('backupSuccessTime');

    function render(healthy) {
        status.classList.toggle('backup-ok', healthy);
        status.classList.toggle('backup-failed', !healthy);
        label.textContent = healthy ? '备份成功' : '备份失败';
    }

    async function refresh() {
        try {
            const response = await fetch(status.dataset.statusUrl, {
                cache: 'no-store', signal: AbortSignal.timeout(10000)
            });
            if (!response.ok || response.redirected) throw new Error('status unavailable');
            const data = await response.json();
            status.dataset.lastSuccess = data.last_success_at || '';
            successTime.textContent = data.last_success_display;
            status.title = data.detail;
            render(data.healthy);
        } catch (_) {
            // Network errors cannot create or advance a successful sync time.
            const last = Date.parse(status.dataset.lastSuccess);
            if (!Number.isFinite(last) || Date.now() - last > 30 * 3600000 || last > Date.now()) {
                render(false);
            }
            status.title = '备份状态暂时无法刷新；显示上次已确认成功的时间';
        }
    }

    refresh();
    setInterval(refresh, 60000);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refresh();
    });
})();
