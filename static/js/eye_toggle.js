// static/js/eye_toggle.js
document.addEventListener('DOMContentLoaded', function() {
    const passwordFields = document.querySelectorAll('input[type="password"]');
    
    passwordFields.forEach(field => {
        // Wrap input in a container
        const wrapper = document.createElement('div');
        wrapper.classList.add('password-wrapper');
        field.parentNode.insertBefore(wrapper, field);
        wrapper.appendChild(field);

        // Create eye icon
        const eye = document.createElement('span');
        eye.classList.add('eye-icon');
        eye.innerHTML = `
            <svg class="eye-open" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                <circle cx="12" cy="12" r="3"></circle>
            </svg>
            <svg class="eye-closed" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path>
                <line x1="1" y1="1" x2="23" y2="23"></line>
            </svg>
        `;
        wrapper.appendChild(eye);

        // Toggle logic with animation
        eye.addEventListener('click', function() {
            const isPassword = field.type === 'password';
            field.type = isPassword ? 'text' : 'password';
            
            wrapper.classList.toggle('visible', !isPassword);
            
            const openEye = eye.querySelector('.eye-open');
            const closedEye = eye.querySelector('.eye-closed');
            
            if(isPassword) {
                openEye.style.display = 'none';
                closedEye.style.display = 'block';
            } else {
                openEye.style.display = 'block';
                closedEye.style.display = 'none';
            }
        });
    });
});
