// Run: node --test tests/test_viewer.mjs
// Execute the actual inline viewer script with DOM/SQLite/I/O doubles; no CDN or browser needed.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const html = readFileSync(new URL('../docs/viewer.html', import.meta.url), 'utf8');
const source = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1].replace(/\}\)\(\);\s*$/, `
  globalThis.hooks = { buildSql, openFile, poll, stopLive, live,
    getDb: function () { return db; } };
})();`);
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function file(name, marker) {
  return { name, size: 1, arrayBuffer: async () => new Uint8Array([marker]).buffer };
}
function harness(options = {}) {
  const elements = new Map(), timers = new Map(), databases = [];
  let timerId = 0;
  function element() {
    return { textContent: '', innerHTML: '', hidden: false, className: '',
      value: '', children: [], style: { setProperty() {}, getPropertyValue() {} },
      classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {},
      querySelectorAll: () => [], appendChild(child) { this.children.push(child); }, focus() {} };
  }
  function getElement(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }
  class Database {
    constructor(bytes) { this.marker = bytes[0]; this.closed = false; databases.push(this); }
    exec() {
      if (this.marker === 255 || options.failOnce?.has(this.marker)) {
        options.failOnce?.delete(this.marker);
        throw new Error('synthetic unreadable snapshot');
      }
    }
    close() { this.closed = true; }
    prepare(sql) {
      if (sql.startsWith('SELECT DISTINCT') && options.failQueryOnce?.has(this.marker)) {
        options.failQueryOnce.delete(this.marker);
        throw new Error('synthetic query failure');
      }
      const data = sql.startsWith('SELECT name, value, count') ? (options.headers ?? []) : [];
      let i = -1;
      return { bind() {}, step: () => ++i < data.length, getAsObject: () => data[i], free() {} };
    }
  }
  const context = vm.createContext({
    document: { getElementById: getElement, createElement: element,
      createTextNode: text => ({ textContent: text }), addEventListener() {} },
    window: {}, navigator: {}, localStorage: { getItem: () => null },
    initSqlJs: () => options.ready ? options.ready.promise.then(() => ({ Database })) : Promise.resolve({ Database }),
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(source, context);
  return { h: context.hooks, el: getElement, timers, databases };
}

test('quoted body phrases compile without stray quotes', () => {
  const { h } = harness();
  for (const expr of ['body~"a phrase"', 'body:"a phrase"', '"a phrase"', '"body~a phrase"']) {
    assert.deepEqual(Array.from(h.buildSql(expr).params), ['%a phrase%'], expr);
  }
  assert.deepEqual(Array.from(h.buildSql('kind=request body~"a phrase"').params), ['request', '%a phrase%']);
});

test('prototype-like header names render as ordinary tags', async () => {
  const { h, el } = harness({ headers: ['constructor', '__proto__', 'toString'].map(name => ({ name, value: 'valid', n: 1 })) });
  await h.openFile(file('headers.sqlite', 1));
  assert.equal(h.getDb().marker, 1);
  for (const name of ['constructor', '__proto__', 'toString']) assert(el('tags').innerHTML.includes(name));
});

test('late old live read cannot overwrite a newly opened snapshot', async () => {
  const { h, el, timers } = harness();
  const oldRead = deferred();
  h.live.handle = { getFile: async () => ({ name: 'old.sqlite', size: 1, arrayBuffer: () => oldRead.promise }) };
  const pending = h.poll();
  await Promise.resolve();
  await h.openFile(file('new.sqlite', 2));
  oldRead.resolve(new Uint8Array([1]).buffer);
  await pending;
  assert.equal(h.getDb().marker, 2);
  assert.equal(el('mode').textContent, 'snapshot');
  assert.equal(h.live.hash, null);
  assert.equal(timers.size, 0);
});

test('stale errors cannot stop or relabel a new live source', async () => {
  const { h, el } = harness();
  const oldRead = deferred();
  h.live.handle = { getFile: () => oldRead.promise };
  const pending = h.poll();
  h.stopLive();
  const newHandle = { getFile: async () => file('new.sqlite', 2) };
  h.live.handle = newHandle;
  await h.poll();
  oldRead.reject(new Error('old source gone'));
  await pending;
  assert.equal(h.live.handle, newHandle);
  assert.equal(h.getDb().marker, 2);
  assert.equal(el('mode').className, 'mode live');
});

test('pending SQLite startup cannot revive an old file selection', async () => {
  const ready = deferred();
  const { h, databases } = harness({ ready });
  const old = h.openFile(file('old.sqlite', 1));
  await Promise.resolve();
  const current = h.openFile(file('new.sqlite', 2));
  ready.resolve();
  await Promise.all([old, current]);
  assert.equal(h.getDb().marker, 2);
  assert.deepEqual(databases.map(db => db.marker), [2]);
});

test('failed live load keeps last good DB/hash and retries the same bytes', async () => {
  const { h, el, timers, databases } = harness({ failOnce: new Set([2]) });
  await h.openFile(file('good.sqlite', 1));
  const good = h.getDb();
  h.live.handle = { getFile: async () => file('growing.sqlite', 2) };
  await h.poll();
  assert.equal(h.getDb(), good);
  assert.equal(good.closed, false);
  assert.equal(h.live.hash, null);
  assert.equal(el('mode').className, 'mode');
  assert.match(el('mode').textContent, /live read failed/);
  assert.equal(databases[1].closed, true);
  assert.equal(timers.size, 1);
  const retry = timers.values().next().value;
  timers.clear();
  await retry();
  assert.equal(h.getDb().marker, 2);
  assert.equal(good.closed, true);
  assert.notEqual(h.live.hash, null);
  assert.equal(el('mode').className, 'mode live');
});

test('query failure during refresh is not committed as a successful load', async () => {
  const { h, el } = harness({ failQueryOnce: new Set([2]) });
  await h.openFile(file('good.sqlite', 1));
  const good = h.getDb();
  h.live.handle = { getFile: async () => file('growing.sqlite', 2) };
  await h.poll();
  assert.equal(h.getDb(), good);
  assert.equal(good.closed, false);
  assert.equal(h.live.hash, null);
  assert.match(el('mode').textContent, /could not query snapshot/);
});

test('snapshot read failure is visible without replacing the prior DB', async () => {
  const { h, el } = harness();
  await h.openFile(file('good.sqlite', 1));
  const good = h.getDb();
  await h.openFile({ name: 'bad.sqlite', arrayBuffer: async () => { throw new Error('permission denied'); } });
  assert.equal(h.getDb(), good);
  assert.match(el('mode').textContent, /permission denied/);
});
