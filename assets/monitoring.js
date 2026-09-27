/* Read-only cloud overlay. Key material lives in memory until relock. */
(function () {
  let generation = 0, timer = null;
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  window.WBQMonitoring = {
    stop() { generation++; clearInterval(timer); timer = null; },
    async start(unlockKey, frame) {
      this.stop();
      const current = generation;
      let key;
      try {
        const wrapped = await unlockKey();
        if (current !== generation) return;
        key = await crypto.subtle.importKey('raw', b64(wrapped.key), 'AES-GCM', false, ['decrypt']);
      } catch (_) {
        if (current === generation) frame.contentWindow.postMessage({type: 'wbq-cloud-monitor', payload: null}, '*');
        return;
      }
      async function poll() {
        try {
          const response = await fetch('data/monitoring.enc.json', {cache: 'no-store'});
          if (!response.ok) throw new Error('missing');
          const enc = await response.json();
          if (enc.v !== 1 || enc.alg !== 'AES-256-GCM') throw new Error('schema');
          const raw = await crypto.subtle.decrypt({name: 'AES-GCM', iv: b64(enc.nonce),
            additionalData: new TextEncoder().encode('WBQ closing research v1')}, key, b64(enc.ct));
          const data = JSON.parse(new TextDecoder().decode(raw));
          if (data.schema !== 1 || data.orders_enabled !== false || !Number.isFinite(Date.parse(data.generated_at))) throw new Error('contract');
          if (current === generation) frame.contentWindow.postMessage({type: 'wbq-cloud-monitor', payload: data}, '*');
        } catch (_) {
          if (current === generation) frame.contentWindow.postMessage({type: 'wbq-cloud-monitor', payload: null}, '*');
        }
      }
      await poll();
      if (current === generation) timer = setInterval(poll, 5 * 60 * 1000);
    }
  };
})();
