// static/js/pdf_generator.js
const { jsPDF } = window.jspdf;

function generateTicketPDF(ticket) {
    const doc = new jsPDF('p', 'mm', [80, 200]); // Receipt size 80mm

    doc.setFontSize(16);
    doc.text("OWAMBE TICKET", 40, 10, { align: "center" });
    
    doc.setFontSize(10);
    doc.text(`Event: ${ticket.event_name}`, 10, 25);
    doc.text(`Guest: ${ticket.guest_name}`, 10, 35);
    doc.text(`Phone: ${ticket.phone}`, 10, 45);
    doc.text(`Seat: ${ticket.seat}`, 10, 55);
    doc.text(`Ticket ID: ${ticket.ticket_id}`, 10, 65);
    doc.text(`Status: ${ticket.status}`, 10, 75);

    // Add QR here later if you want

    return doc;
}

function downloadTicket(ticket) {
    const doc = generateTicketPDF(ticket);
    doc.save(`Ticket_${ticket.ticket_id}.pdf`);
}

function sendTicketViaWhatsApp(ticket) {
    const doc = generateTicketPDF(ticket);
    const pdfBlob = doc.output('blob');
    const pdfUrl = URL.createObjectURL(pdfBlob);

    // Auto download first
    const a = document.createElement('a');
    a.href = pdfUrl;
    a.download = `Ticket_${ticket.ticket_id}.pdf`;
    a.click();

    // Open WhatsApp with message
    const message = encodeURIComponent(`Hi ${ticket.guest_name}, here is your Owambe ticket for ${ticket.event_name}. PDF attached.`);
    const waUrl = `https://wa.me/${ticket.phone}?text=${message}`;
    window.open(waUrl, '_blank');

    // Clear from memory
    setTimeout(() => URL.revokeObjectURL(pdfUrl), 5000);
}
