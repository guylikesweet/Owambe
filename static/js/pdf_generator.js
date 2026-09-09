// static/js/pdf_generator.js
const { jsPDF } = window.jspdf;

function generateTicketPDF(ticket) {
    const doc = new jsPDF('p', 'mm', [80, 200]); // Receipt size 80mm

    // 1. HEADER + BORDER
    doc.setDrawColor(0, 200, 200); // cyan border
    doc.setLineWidth(0.8);
    doc.rect(3, 3, 74, 194); // border
    
    doc.setFont("helvetica", "bold");
    doc.setFontSize(12);
    
    // Wrap long event name
    const eventName = "Bioelites Class of 26' Owambe Experience and Award Ceremony";
    const splitEvent = doc.splitTextToSize(eventName, 70);
    doc.text(splitEvent, 40, 10, { align: "center" });
    
    doc.setLineWidth(0.3);
    doc.line(5, 22, 75, 22); // divider line

    // 2. GUEST INFO
    doc.setFont("helvetica", "normal");
    doc.setFontSize(10);
    let y = 30;
    
    doc.text(`Guest: ${ticket.guest_name}`, 8, y); y += 8;
    doc.text(`Phone: ${ticket.phone}`, 8, y); y += 8;
    
    // FIX: seat: undefined
    const seatText = ticket.seat && ticket.seat !== 'undefined' ? `Seat: ${ticket.seat}` : `Seat: General`;
    doc.text(seatText, 8, y); y += 8;
    
    doc.text(`Ticket ID: ${ticket.ticket_id}`, 8, y); y += 8;
    doc.text(`Status: ${ticket.status}`, 8, y); y += 10;

    // 3. ADD QR CODE
    // QR should contain verification link
    const qrData = `https://owambe-q3kv.onrender.com/verify/${ticket.ticket_id}`;
    const qrCanvas = document.createElement('canvas');
    QRCode.toCanvas(qrCanvas, qrData, { width: 120 }, function (error) {
        if (error) console.error(error);
    });
    const qrImgData = qrCanvas.toDataURL('image/png');
    
    doc.addImage(qrImgData, 'PNG', 25, y, 30, 30); // center QR
    doc.setFontSize(8);
    doc.text("Scan at Gate", 40, y + 35, { align: "center" });

    return doc;
}

function downloadTicket(ticket) {
    const doc = generateTicketPDF(ticket);
    doc.save(`Bioelites_Ticket_${ticket.ticket_id}.pdf`);
}

function sendTicketViaWhatsApp(ticket) {
    const doc = generateTicketPDF(ticket);
    const pdfBlob = doc.output('blob');
    const pdfUrl = URL.createObjectURL(pdfBlob);

    // Auto download first
    const a = document.createElement('a');
    a.href = pdfUrl;
    a.download = `Bioelites_Ticket_${ticket.ticket_id}.pdf`;
    a.click();

    // FIX: WhatsApp cannot attach files. Send download link instead
    const ticketUrl = `${window.location.origin}/ticket/download/${ticket.ticket_id}`; // You'll need a backend route for this
    const message = encodeURIComponent(`Hi ${ticket.guest_name}! 🎉\n\nYour ticket for Bioelites Owambe is ready.\n\nDownload: ${ticketUrl}\n\nPlease present the QR code at the gate.`);
    const waUrl = `https://wa.me/${ticket.phone}?text=${message}`;
    window.open(waUrl, '_blank');

    setTimeout(() => URL.revokeObjectURL(pdfUrl), 5000);
}
