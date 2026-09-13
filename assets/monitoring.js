/* Read-only cloud overlay. Key material lives in memory until relock. */
(function () {
  let generation = 0, timer = null;
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  function receive() {
    let latest = null;
    const status = document.createElement('div');
    status.id = 'cloud-monitor-status';
    status.style.cssText = 'padding:10px 16px;background:#142638;color:#cfe6ff;font-size:13px;line-height:1.6';
    status.textContent = '클라우드 종가 모니터 연결 중…';
    document.body.prepend(status);
    window.addEventListener('message', event => {
      if (event.source !== parent || !event.data || event.data.type !== 'wbq-cloud-monitor') return;
      const data = event.data.payload;
      if (!data) {
        latest = null;
        status.textContent = '클라우드 갱신 확인 실패 · 표시된 마지막 자료는 최신 시세가 아닐 수 있습니다.';
        return;
      }
      latest = data;
      updateStatus();
      const page = document.getElementById('page-llm');
      if (page && typeof data.llm_html === 'string') {
        const replacement = new DOMParser().parseFromString(data.llm_html, 'text/html').getElementById('page-llm');
        if (replacement) page.innerHTML = replacement.innerHTML;
      }
      let summary = document.getElementById('cloud-benchmark-summary');
      if (!summary) {
        summary = document.createElement('section');
        summary.id = 'cloud-benchmark-summary';
        summary.className = 'notice';
        const overview = document.getElementById('page-dashboard');
        if (overview) overview.prepend(summary);
      }
      summary.replaceChildren();
      const heading = document.createElement('h2');
      heading.textContent = '미국 ETF · 클라우드 종가 추적';
      summary.append(heading);
      (data.benchmarks || []).forEach(row => {
        const p = document.createElement('p');
        p.textContent = `${row.symbol} · $${row.close.toFixed(2)} · ${row.baseline_date} 기준 ${row.return_pct >= 0 ? '+' : ''}${row.return_pct.toFixed(2)}% · ${row.as_of} 종가`;
        summary.append(p);
      });
      const note = document.createElement('p');
      note.textContent = '가격 수익률(배당·환율·비용 제외). LLM 실험은 별도 비용 반영 가상 원장입니다. 실계좌·주문·백테스트·월간 종목 재선정은 자동갱신 대상이 아닙니다.';
      summary.append(note);
    });
    function updateStatus() {
      if (!latest) return;
      const stale = Date.now() - Date.parse(latest.generated_at) > 36 * 3600 * 1000;
      status.textContent = `${stale ? '갱신 지연 확인 필요' : '클라우드 종가 모니터'} · 한국 ${latest.kr_as_of} / 미국 ${latest.us_as_of} · 마지막 성공 ${new Date(latest.generated_at).toLocaleString('ko-KR')} · 주문 기능 없음`;
    }
    setInterval(updateStatus, 60000);
  }
  window.WBQMonitoring = {
    decorate(html) { return html.replace('</body>', '<script>(' + receive.toString() + ')();<\/script></body>'); },
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
