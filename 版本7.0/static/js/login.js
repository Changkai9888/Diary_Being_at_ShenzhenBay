document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const response = await fetch('/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams(formData)
    });
    
    const errorDiv = document.getElementById('errorMsg');
    if (response.ok) {
        window.location.href = '/';
    } else {
        errorDiv.textContent = await response.text();
        errorDiv.classList.remove('d-none');
        if(response.status === 403) {
            setTimeout(() => window.location.reload(), 3600000);
        }
    }
});