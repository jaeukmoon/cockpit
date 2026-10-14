/* Read-only cloud overlay. Key material lives in memory until relock. */
(function () {
  let generation = 0, timer = null;
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  window.WBQMonitoring = {
    stop() { generation++; clearInterval(timer); timer = null;const notice=document.getElementById('research-refresh-status');if(notice){notice.textContent='';notice.hidden=true;} },
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
          if(current!==generation)return;
          let notice=document.getElementById('research-refresh-status');
          if(!notice){notice=document.createElement('p');notice.id='research-refresh-status';notice.setAttribute('role','status');frame.before(notice);}
          if (data.data_state === 'DELAYED') {
            notice.hidden = false;
            notice.textContent = `미국 시세 갱신 지연: ${data.expected_us_as_of} 종가를 아직 확인하지 못했습니다. 기존 ${data.us_as_of} 기준 데이터를 유지합니다. 마지막 데이터 생성: ${data.generated_at}`;
          } else {
          notice.hidden=data.data_state!=='PARTIAL_HELD';
          notice.textContent=notice.hidden?'':'일부 연구 관측 갱신 보류 · 검증되지 않은 과거 일봉이 있어 마지막 검증 수익률을 유지합니다. 다른 정상 시세는 갱신합니다.';
          }
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
