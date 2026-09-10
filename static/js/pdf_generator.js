// static/js/pdf_generator.js
const { jsPDF } = window.jspdf;

// Convert image to base64 so jsPDF can use it
async function toBase64(url) {
    const response = await fetch(url);
    const blob = await response.blob();
    return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result);
        reader.readAsDataURL(blob);
    });
}

async function generateTicketPDF(ticket) {
    const doc = new jsPDF('p', 'mm', [105, 170]); // Standard ticket size like server

    // 1. LOAD IMAGES
    let bgImg = null;
    let logoImg = null;
    try {
        bgImg = await toBase64('/static/img/owambe-flyer.jpg');
        logoImg = await toBase64('/static/img/owambe-logo.png');
    } catch(e) {
        console.log("Images not found, using fallback")
    }

    // 2. BACKGROUND FLIER - faded
    if(bgImg){
        doc.setFillColor(0,0,0);
        doc.rect(0, 0, 105, 170, 'F'); // black base
        doc.setGState(new doc.GState({opacity: 0.20})); // 20% opacity
        doc.addImage(bgImg, 'JPG', 0, 0, 105, 170);
        doc.setGState(new doc.GState({opacity: 1})); // reset
    } else {
        doc.setFillColor(11, 31, 43); // fallback navy
        doc.rect(0, 0, 105, 170, 'F');
    }

    // 3. DARK OVERLAY
    doc.setFillColor(11, 31, 43);
    doc.setGState(new doc.GState({opacity: 0.80}));
    doc.rect(0, 0, 105, 170, 'F');
    doc.setGState(new doc.GState({opacity: 1}));

    // 4. LOGO
    if(logoImg){
        doc.addImage(logoImg, 'PNG', (105-55)/2, 8, 55, 22); // centered
    } else {
        doc.setTextColor(212, 175, 55); // gold
        doc.setFont("helvetica", "bold");
        doc.setFontSize(18);
        doc.text("OWAMBE", 52.5, 20, {align: "center"});
    }

    // 5. EVENT TITLE
    doc.setTextColor(255,255,255);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(11);
    const eventName = "Bioelites Class of 26' Owambe Experience and Award Ceremony";
    const splitEvent = doc.splitTextToSize(eventName, 95);
    doc.text(splitEvent, 52.5, 35, { align: "center" });

    // 6. TICKET CARD
    const cardY = 55;
    doc.setFillColor(26, 47, 58);
    doc.setGState(new doc.GState({opacity: 0.88}));
    doc.roundedRect(8, cardY, 89, 72, 3, 3, 'F');
    doc.setGState(new doc.GState({opacity: 1}));
    doc.setDrawColor(212, 175, 55); // gold border
    doc.setLineWidth(0.5);
    doc.roundedRect(8, cardY, 89, 72, 3, 3, 'S');

    // 7. QR CODE
    const qrData = `${window.location.origin}/t/${ticket.ticket_code}/pdf`;
    const qrCanvas = document.createElement('canvas');
    await QRCode.toCanvas(qrCanvas, qrData, { width: 200, margin: 1 });
    const qrImgData = qrCanvas.toDataURL('image/png');
    doc.setFillColor(255,255,255);
    doc.roundedRect((105-52)/2 - 4, cardY + 6, 60, 60, 2, 2, 'F');
    doc.addImage(qrImgData, 'PNG', (105-52)/2, cardY + 10, 52, 52);
    
    // 8. GUEST INFO
    doc.setFont("helvetica", "normal");
    doc.setFontSize(7.5);
    doc.setTextColor(66, 215, 233); // cyan labels
    let y = cardY + 68;

    const category = ticket.category || "Regular";
    const ticketTypeDisplay = ticket.table_id
        ? `${category} \u2022 ${ticket.seat || ''}`
        : category;

    const fields = [
        ["GUEST NAME", ticket.guest_name],
        ["TICKET CODE", ticket.ticket_code],
        ["TICKET TYPE", ticketTypeDisplay],
        ["AMOUNT", `NGN ${Number(ticket.amount_paid || 3500).toLocaleString()}`]
    ];
    
    fields.forEach(([label, value]) => {
        doc.text(label, 12, y);
        doc.setFont("helvetica", "bold");
        doc.setFontSize(11);
        doc.setTextColor(255,255,255);
        doc.text(String(value).substring(0,35), 12, y + 5);
        doc.setFont("helvetica", "normal");
        doc.setFontSize(7.5);
        doc.setTextColor(66, 215, 233);
        y += 13;
    });

    // 9. ADMIT ONE LINE
    doc.setDrawColor(212, 175, 55);
    doc.setLineDashPattern([2,2], 0);
    doc.line(10, 145, 95, 145);
    doc.setLineDashPattern([], 0);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(8);
    doc.setTextColor(212, 175, 55);
    doc.text("ADMIT ONE • PRESENT QR AT GATE", 52.5, 152, {align: "center"});

    // 10. FOOTER
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.5);
    doc.setTextColor(168, 193, 200);
    doc.text(`Issued: ${ticket.created_at || ''} | Seller: ${ticket.sold_by || ''}`, 52.5, 162, {align: "center"});

    // 11. CANCELLED STAMP
    if(ticket.cancelled){
        doc.setTextColor(237, 91, 99);
        doc.setFont("helvetica", "bold");
        doc.setFontSize(32);
        doc.text("CANCELLED", 52.5, 85, {align: "center", angle: 20});
    }

    return doc;
}

async function downloadTicket(ticket) {
    const doc = await generateTicketPDF(ticket);
    doc.save(`Bioelites_Owambe_${ticket.ticket_code}.pdf`);
}

async function sendTicketViaWhatsApp(ticket) {
    const doc = await generateTicketPDF(ticket);
    const pdfBlob = doc.output('blob');
    const pdfUrl = URL.createObjectURL(pdfBlob);

    // Auto download
    const a = document.createElement('a');
    a.href = pdfUrl;
    a.download = `Bioelites_Owambe_${ticket.ticket_code}.pdf`;
    a.click();

    // Send WhatsApp link - backend should host the PDF
    const ticketUrl = `${window.location.origin}/t/${ticket.ticket_code}/pdf`;
    const message = encodeURIComponent(`Hi ${ticket.guest_name}! 🎉\n\nYour ticket for "Bioelites Class of 26' Owambe" is ready.\nTicket code: ${ticket.ticket_code}\nDownload your ticket & QR here: ${ticketUrl}\n\nPlease present the QR code at the gate. See you there!`);
    const waUrl = `https://wa.me/${ticket.phone}?text=${message}`;
    window.open(waUrl, '_blank');

    setTimeout(() => URL.revokeObjectURL(pdfUrl), 5000);
}
