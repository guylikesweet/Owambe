// static/js/scanner_utils.js
let scannerAudio = new Audio('/static/audio/invalid-long.mp3'); // new sound

function showScannerLoader() {
    const loader = document.createElement('div');
    loader.id = 'scan-loader';
    loader.innerHTML = `
        <div class="loader-circle"></div>
        <p>Verifying ticket...</p>
    `;
    document.body.appendChild(loader);
}

function hideScannerLoader() {
    const loader = document.getElementById('scan-loader');
    if(loader) loader.remove();
}

function playInvalidSound() {
    scannerAudio.currentTime = 0; // rewind
    scannerAudio.play().catch(e => console.log("Audio play failed", e));
}

// This is the function you call in scan.html after qr code is read
function handleScanResult(result, resultType) {
    showScannerLoader();
    
    // Wait 2 seconds before showing result
    setTimeout(() => {
        hideScannerLoader();
        
        // Example: check if ticket is valid
        // You probably already have this logic. Just wrap it
        if(result.status === 'invalid' || result.status === 'used') {
            playInvalidSound();
            showScanOverlay('invalid', result.message);
        } else {
            showScanOverlay('valid', result.message);
        }
    }, 2000);
}

// Reuse your existing overlay but add loader styles
function showScanOverlay(status, message) {
    const overlay = document.querySelector('.scan-overlay');
    const title = overlay.querySelector('.status-title');
    const msg = overlay.querySelector('.status-message');
    
    title.className = `status-title ${status}`;
    title.textContent = status === 'valid' ? 'VALID' : 'INVALID';
    msg.textContent = message;
    
    overlay.hidden = false;
    document.body.classList.add('modal-open');
}
