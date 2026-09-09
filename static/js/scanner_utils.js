// static/js/scanner_utils.js
const successAudio = new Audio('/static/audio/success-beep.mp3'); // new success sound
const errorAudio = new Audio('/static/audio/invalid-long.mp3'); // error sound

function showScannerLoader() {
    if(document.getElementById('scan-loader')) return;
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

function playSuccessSound() {
    successAudio.currentTime = 0;
    successAudio.play().catch(e => console.log("Audio play failed", e));
}

function playErrorSound() {
    errorAudio.currentTime = 0;
    errorAudio.play().catch(e => console.log("Audio play failed", e));
}

// Backend returns: VALID, ALREADY USED, CANCELLED, INVALID, MULTIPLE
function handleScanResult(data) {
    showScannerLoader();
    
    setTimeout(() => {
        hideScannerLoader();
        
        // VALID and MULTIPLE = success sound
        if(data.status === 'VALID' || data.status === 'MULTIPLE') {
            playSuccessSound();
        } else {
            // ALREADY USED, CANCELLED, INVALID = error sound
            playErrorSound();
        }
        
        showScanOverlay(data);
    }, 2000);
}

function showScanOverlay(data) {
    const overlay = document.querySelector('.scan-overlay');
    const title = overlay.querySelector('.status-title');
    const msg = overlay.querySelector('.status-message');
    const details = overlay.querySelector('.ticket-details');
    
    let statusClass = 'valid';
    
    if(data.status === 'VALID' || data.status === 'MULTIPLE') {
        statusClass = 'valid'; // green
    } else {
        statusClass = 'invalid'; // red
    }
    
    title.className = `status-title ${statusClass}`;
    title.textContent = data.status;
    msg.textContent = data.msg; 
    
    if(details && data.name) {
        details.innerHTML = `<strong>${data.name}</strong><br><small>${data.ticket_code}</small>`;
        details.hidden = false;
    }

    overlay.hidden = false;
    document.body.classList.add('modal-open');
}
