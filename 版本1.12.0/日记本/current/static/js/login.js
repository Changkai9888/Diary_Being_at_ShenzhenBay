document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const errorDiv = document.getElementById('errorMsg');
    try {
        const formData = new FormData(e.target);
        const response = await fetch(e.target.action, {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
            body: new URLSearchParams(formData)
        });
        if (response.ok) {
            const result = await response.json();
            window.location.replace(result.redirect);
        } else {
            errorDiv.textContent = await response.text();
            errorDiv.classList.remove('d-none');
        }
    } catch (_) {
        errorDiv.textContent = '登录请求失败，请检查网络后重试。';
        errorDiv.classList.remove('d-none');
    }
});
