// Passive reader: looks only at what is already rendered on the Orders page you have open.
// It makes no requests, clicks nothing and changes nothing on the page.
(function () {
  const sent = new Set();
  let timer = null;

  function scan() {
    const found = [];
    document.querySelectorAll(".panel-body-row").forEach((row) => {
      const mail = row.querySelector('a[href^="mailto:"]');
      if (!mail) return;
      const email = decodeURIComponent(mail.getAttribute("href").slice(7)).split("?")[0].trim();
      const orderLink = row.querySelector('a[href*="order_id="]');
      const order = orderLink && /order_id=(\d+)/.exec(orderLink.getAttribute("href"));
      const buyerLink = row.querySelector('a[href*="buyer_id="]');
      const buyer = buyerLink && /buyer_id=(\d+)/.exec(buyerLink.getAttribute("href"));
      const nameEl = row.querySelector("[data-dropdown-button]");
      const key = (order ? order[1] : "") + "|" + email;
      if (!email || !order || sent.has(key)) return;
      sent.add(key);
      found.push({
        order_id: order[1],
        buyer_id: buyer ? buyer[1] : "",
        email: email,
        name: nameEl ? nameEl.textContent.trim() : "",
      });
    });
    if (found.length) chrome.runtime.sendMessage({ orders: found });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(scan, 1500);
  }

  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  schedule();
})();
