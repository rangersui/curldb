// Run: node --test tests/test_viewer.mjs
// Execute the actual inline viewer script with DOM/SQLite/I/O doubles; no CDN or browser needed.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const html = readFileSync(new URL('../docs/viewer.html', import.meta.url), 'utf8');
const source = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1].replace(/\}\)\(\);\s*$/, `
  globalThis.hooks = { buildSql, openFile, poll, stopLive, live, splitEnvelope, isText, sniff, htmlFrame,
    renderMarkdown, prettyJson, parseCsv, frontMatter, parseMultipart, hexDump, diffHtml, headLines,
    getDb: function () { return db; }, seq: function () { return renderSeq; } };
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
    window: {}, navigator: {}, localStorage: { getItem: () => null }, TextDecoder, TextEncoder,
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

test('envelope split works on the bytes, so binary body lengths are exact', () => {
  const { h } = harness();
  const enc = s => new TextEncoder().encode(s);
  const crlf = new Uint8Array([...enc('HTTP/1.1 200 OK\r\nA: b\r\n\r\n'), 0, 1, 2]);
  const lf = new Uint8Array([...enc('GET /x HTTP/1.1\nA: b\n\n'), 0xff, 0xfe]);
  assert.deepEqual(Array.from(h.splitEnvelope(crlf)), [crlf.length - 7, crlf.length - 3]);
  assert.deepEqual(Array.from(h.splitEnvelope(lf)), [lf.length - 4, lf.length - 2]);
  assert.deepEqual(Array.from(h.splitEnvelope(enc('no blank line'))), [13, 13]);
});

test('text means UTF-8 without NUL; sniff names common binary types', () => {
  const { h } = harness();
  const enc = s => new TextEncoder().encode(s);
  assert.equal(h.isText(enc('\u4e2d\u6587 ok')), true);
  assert.equal(h.isText(new Uint8Array([0, 1, 2])), false);
  assert.equal(h.isText(new Uint8Array([0xff, 0xfe])), false);
  assert.equal(h.sniff(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 13, 10, 26, 10])), 'image/png');
  assert.equal(h.sniff(new Uint8Array([0xff, 0xd8, 0xff, 0xe0])), 'image/jpeg');
  assert.equal(h.sniff(enc('%PDF-1.7')), 'application/pdf');
  assert.equal(h.sniff(enc('plain')), '');
});

test('stored HTML renders in a frame with no scripts and no network', () => {
  const { h } = harness();
  const frame = h.htmlFrame('<img src="https://tracker.example/p.gif"><script>x()</script>');
  assert.match(frame, /^<iframe sandbox referrerpolicy="no-referrer" srcdoc="/);
  assert.match(frame, /Content-Security-Policy/);
  assert.match(frame, /default-src &#39;none&#39;|default-src 'none'/);
  assert.ok(frame.includes('&lt;script&gt;'), 'page markup is attribute-escaped, not live');
  assert.ok(!frame.includes('<script>'));
});

test('loading a file invalidates any detail render still in flight', async () => {
  const { h } = harness();
  await h.openFile(file('a.sqlite', 1));
  const before = h.seq();
  await h.openFile(file('b.sqlite', 2));
  assert.ok(h.seq() > before, 'render sequence advances on every file load');
});

test('markdown renders structure and escapes markup', () => {
  const { h } = harness();
  const md = '# Title\n\nSome *em* and **bold** with `code` and <b>tag</b>.\n\n- one\n- two\n\n```\nx < y\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n![alt](http://x/y.png) [link](https://example.com)';
  const html = h.renderMarkdown(md);
  assert.match(html, /<h1>Title<\/h1>/);
  assert.match(html, /<em>em<\/em> and <strong>bold<\/strong> with <code>code<\/code> and &lt;b&gt;tag&lt;\/b&gt;/);
  assert.match(html, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
  assert.match(html, /<pre><code>x &lt; y<\/code><\/pre>/);
  assert.match(html, /<table><tr><th>a<\/th><th>b<\/th><\/tr><tr><td>1<\/td><td>2<\/td><\/tr><\/table>/);
  assert.ok(html.includes('[image: alt]') && !html.includes('<img'), 'images are named, not loaded');
  assert.match(html, /<a href="https:\/\/example.com" target="_blank" rel="noreferrer noopener">link<\/a>/);
});

test('json pretty-prints with classes and falls back on bad input', () => {
  const { h } = harness();
  const html = h.prettyJson('{"a":[1,true,null],"b":"<x>"}');
  assert.match(html, /<span class="j-k">&quot;a&quot;<\/span>:/);
  assert.match(html, /<span class="j-n">1<\/span>/);
  assert.match(html, /<span class="j-s">&quot;&lt;x&gt;&quot;<\/span>/);
  assert.equal(h.prettyJson('{not json'), null);
});

test('csv, front matter, multipart, diff and hex helpers', () => {
  const { h } = harness();
  assert.deepEqual(Array.from(h.parseCsv('a,b\n"x, y","q""q"\n', ','), r => Array.from(r)), [['a', 'b'], ['x, y', 'q"q']]);
  const fm = h.frontMatter('---\ntitle: T\ntags:\n  - a\n  - b\n---\nbody here');
  assert.deepEqual(Array.from(fm.meta, p => Array.from(p)), [['title', 'T'], ['tags', 'a'], ['tags', 'b']]);
  assert.equal(fm.body, 'body here');
  const mp = new TextEncoder().encode('--B\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--B--\r\n');
  const parts = h.parseMultipart(mp, 'B');
  assert.equal(parts.length, 1);
  assert.equal(parts[0].headers['content-type'], 'text/plain');
  assert.equal(new TextDecoder().decode(parts[0].body), 'hello');
  const diff = h.diffHtml('--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n same');
  assert.match(diff, /<span class="d-del">-old<\/span>/);
  assert.match(diff, /<span class="d-add">\+new<\/span>/);
  const hex = h.hexDump(new Uint8Array([0x41, 0x42, 0, 0xff]));
  assert.match(hex, /00000000  41 42 00 ff/);
  assert.match(hex, /<b>AB..<\/b>/);
});

test('sniff recognises text formats and headLines parses an envelope', () => {
  const { h } = harness();
  const enc = s => new TextEncoder().encode(s);
  assert.equal(h.sniff(enc('{"a":1}')), 'application/json');
  assert.equal(h.sniff(enc('<!doctype html><p>x')), 'text/html');
  assert.equal(h.sniff(enc('<svg xmlns="x"></svg>')), 'image/svg+xml');
  assert.equal(h.sniff(enc('diff --git a/x b/x\n')), 'text/x-diff');
  assert.equal(h.sniff(enc('HTTP/1.1 200 OK\r\n\r\n')), 'message/http');
  assert.equal(h.sniff(new Uint8Array([0x50, 0x4b, 3, 4, 0])), 'application/zip');
  const head = h.headLines(enc('HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nbody'));
  assert.equal(head.headers['content-type'], 'text/plain');
  assert.match(head.html, /class="start err"/);
  assert.equal(head.bodyStart, 'HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\n'.length);
  assert.equal(h.headLines(enc('just prose')), null);
});
