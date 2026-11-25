// Passive reader: looks only at what is already rendered on the Orders page you have open.
// It makes no requests to Etsy, clicks nothing and changes nothing on the page.
// Works on the normal list and on search results (the page is rebuilt without a reload).
(function () {
  const sent = new Set();
  let timer = null;

  // Climb from an email link to the smallest block that also holds that order's link.
  function rowFor(mail) {
    let el = mail.parentElement;
    for (let i = 0; el && i < 15; i++, el = el.parentElement) {
      if (el.querySelector('a[href*="order_id="]')) return el;
    }
    return null;
  }

  function describe(row) {
    const orderLink = row.querySelector('a[href*="order_id="]');
    const order = orderLink && /order_id=(\d+)/.exec(orderLink.getAttribute("href"));
    if (!order) return null;
    const buyerLink = row.querySelector('a[href*="buyer_id="]');
    const buyer = buyerLink && /buyer_id=(\d+)/.exec(buyerLink.getAttribute("href"));
    const nameEl = row.querySelector("[data-dropdown-button]");
    return {
      order_id: order[1],
      buyer_id: buyer ? buyer[1] : "",
      name: nameEl ? nameEl.textContent.trim() : "",
    };
  }

  function scan() {
    const found = [];
    // Orders that show an email.
    document.querySelectorAll('a[href^="mailto:"]').forEach((mail) => {
      const row = rowFor(mail);
      const info = row && describe(row);
      if (!info) return;
      const email = decodeURIComponent(mail.getAttribute("href").slice(7)).split("?")[0].trim();
      const key = info.order_id + "|" + email;
      if (!email || sent.has(key)) return;
      found.push(Object.assign({ key: key, email: email }, info));
    });
    // Orders where Etsy says "Inactive email" (deleted accounts, no usable address): remember them so
    // they stop showing up as missing.
    document.querySelectorAll(".fs-exclude").forEach((el) => {
      if (el.textContent.trim() !== "Inactive email") return;
      const row = rowFor(el);
      const info = row && describe(row);
      if (!info) return;
      const key = info.order_id + "|inactive";
      if (sent.has(key)) return;
      found.push(Object.assign({ key: key, email: "", inactive: true }, info));
    });
    if (!found.length) return;
    try {
      chrome.runtime.sendMessage({ orders: found }, (reply) => {
        if (chrome.runtime.lastError || !reply || !reply.ok) {
          console.warn("[BeeMine email capture] not saved (is email_server.py running?); will retry");
          return; // not marked as sent, so the next scan tries again
        }
        found.forEach((f) => sent.add(f.key));
        console.log("[BeeMine email capture] saved " + found.length + " order(s)");
      });
    } catch (e) {
      console.warn("[BeeMine email capture] extension was reloaded: refresh this Etsy tab (F5)");
    }
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(scan, 1200);
  }

  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  setInterval(scan, 5000); // safety net: retries failures and catches anything the observer missed
  schedule();
})();
