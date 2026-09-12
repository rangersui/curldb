// Print the SQL the viewer would run for each query expression on stdin
// (a JSON list), so tests/test_parity.py can run it against curldb.query.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const html = readFileSync(new URL('../curldb/viewer/index.html', import.meta.url), 'utf8');
const scripts = Array.from(html.matchAll(/<script src="(viewer\/[^"]+)"><\/script>/g), m => m[1]);
const sources = scripts.map(p => readFileSync(new URL('../curldb/viewer/' + p.split('/').pop(), import.meta.url), 'utf8'));
const el = () => ({ textContent: '', innerHTML: '', hidden: false, className: '', value: '', style: { setProperty() {}, getPropertyValue() {} }, classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, setAttribute() {}, getAttribute() { return null; }, querySelectorAll: () => [], appendChild() {}, focus() {} });
const ctx = vm.createContext({ document: { getElementById: el, createElement: el, createTextNode: t => ({}), addEventListener() {} }, window: {}, navigator: {}, localStorage: { getItem: () => null }, TextDecoder, TextEncoder, Blob, URL: { createObjectURL: () => '', revokeObjectURL() {} }, initSqlJs: () => Promise.resolve({}), setTimeout: () => 0, clearTimeout() {} });
sources.forEach(s => vm.runInContext(s, ctx));
const app = ctx.window.CV.app;
app.S.folder = 'all'; app.S.schema.parent = true;
const input = JSON.parse(readFileSync(0, 'utf8'));
const exprs = input.exprs, saved = input.saved || {};
// The only table the query builder reads itself is the saved queries; hand those in.
const savedRows = Object.keys(saved).map(name => ({ method: 'PUT', path: '/queries/' + name, body: saved[name] }));
app.S.db = { prepare: sql => { const data = sql.includes("GLOB '/queries/?*'") ? savedRows : []; let i = -1; return { bind() {}, step: () => ++i < data.length, getAsObject: () => data[i], free() {} }; } };
console.log(JSON.stringify(exprs.map(e => { try { const b = app.buildSql(e); return { expr: e, sql: b.sql, params: Array.from(b.params) }; } catch (err) { return { expr: e, error: String(err) }; } })));
