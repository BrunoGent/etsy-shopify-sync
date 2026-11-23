// Passes captured orders to the listener on this PC (email_server.py). Nothing goes to Etsy.
chrome.runtime.onMessage.addListener((message) => {
  if (!message || !Array.isArray(message.orders)) return;
  fetch("http://127.0.0.1:8765/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orders: message.orders }),
  }).catch(() => {
    // Listener not running: ignore. Re-open the page after starting email_server.py.
  });
});
