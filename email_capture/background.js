// Passes captured orders to the listener on this PC (email_server.py). Nothing goes to Etsy.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !Array.isArray(message.orders)) return;
  const orders = message.orders.map(({ order_id, buyer_id, email, name }) => ({ order_id, buyer_id, email, name }));
  fetch("http://127.0.0.1:8765/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orders: orders }),
  })
    .then((r) => sendResponse({ ok: r.ok }))
    .catch(() => sendResponse({ ok: false })); // listener not running: the page retries
  return true; // keep the channel open for the async reply
});
