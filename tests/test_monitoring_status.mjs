import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../assets/monitoring.js', import.meta.url), 'utf8');
const snapshot = state => ({schema: 1, orders_enabled: false, data_state: state,
  generated_at: '2026-10-13T00:00:00+00:00', us_as_of: '2026-10-12', expected_us_as_of: '2026-10-13'});

function harness(values, decryptOverride) {
  const nodes = new Map(), messages = [];
  let active, poll;
  const frame = {before(node) { nodes.set(node.id, node); },
    contentWindow: {postMessage(value) { messages.push(value); }}};
  const context = {window: {}, Date, Number, JSON, Uint8Array, TextEncoder, TextDecoder, atob,
    document: {getElementById: id => nodes.get(id), createElement: () => ({setAttribute() {}})},
    crypto: {subtle: {importKey: async () => ({}), decrypt: async () => decryptOverride ? decryptOverride(active) : new TextEncoder().encode(JSON.stringify(active))}},
    fetch: async () => { active = values.shift(); return {ok: true, json: async () => ({v: 1, alg: 'AES-256-GCM', nonce: 'eA==', ct: 'eA=='})}; },
    setInterval(fn) {poll = fn; return 1;}, clearInterval() {poll = null;}};
  vm.runInNewContext(source, context);
  return {monitor: context.window.WBQMonitoring, frame, messages,
    notice: () => nodes.get('research-refresh-status'), poll: () => poll(),
    start: () => context.window.WBQMonitoring.start(async () => ({key: 'eA=='}), frame)};
}

test('delayed data stays dated and exposes expected and retained sessions', async () => {
  const value = snapshot('DELAYED'), h = harness([value]);
  await h.start();
  assert.equal(h.notice().hidden, false);
  assert.match(h.notice().textContent, /갱신 지연/);
  assert.match(h.notice().textContent, /2026-10-13/);
  assert.match(h.notice().textContent, /2026-10-12/);
  assert.equal(h.messages[0].payload.generated_at, value.generated_at);
  assert.equal(h.messages[0].payload.data_state, 'DELAYED');
});

test('recovery clears the delay notice and supplies recovered payload', async () => {
  const h = harness([snapshot('DELAYED'), snapshot('READY')]);
  await h.start(); await h.poll();
  assert.equal(h.notice().hidden, true);
  assert.equal(h.notice().textContent, '');
  assert.equal(h.messages.at(-1).payload.data_state, 'READY');
});

test('partial hold remains visible and relock clears the notice', async () => {
  const h = harness([snapshot('PARTIAL_HELD')]);
  await h.start();
  assert.equal(h.notice().hidden, false);
  assert.ok(h.notice().textContent.length > 0);
  h.monitor.stop();
  assert.equal(h.notice().hidden, true);
  assert.equal(h.notice().textContent, '');
});

test('a decrypt completing after relock cannot restore a stale notice or payload', async () => {
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  const h = harness([snapshot('DELAYED')], active => {
    entered(); return new Promise(resolve => {release = () => resolve(new TextEncoder().encode(JSON.stringify(active)));});
  });
  const work = h.start(); await started;
  h.monitor.stop(); release(); await work;
  assert.equal(h.messages.length, 0);
  assert.equal(h.notice(), undefined);
});
