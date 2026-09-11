// curldb viewer: the page. Panes, file loading (snapshot and live), the
// tag pane, the query, the list, keys, and the reading pane. Parsing and
// rendering live in render.js (window.CV). Runs as a classic script so
// the page works from file:// as well as from a server.
(function () {
  var CV = window.CV;
  function $(id) { return document.getElementById(id); }
  var esc = CV.esc;

  // All mutable page state in one place.
  var S = {
    SQL: null, db: null, selected: null, displayedGeneration: null,
    live: { handle: null, timer: null, hash: null, generation: 0 },
    renderSeq: 0, viewMode: "rendered", searchOpen: false, helpOpen: false,
    relatedKey: null, relatedOpen: false,
    schema: { parent: false }   // does this file have the parent column (0.4+)?
  };

  // ---- panes ----------------------------------------------------------
  // Which panes are open is remembered in this browser. "focus" is the
  // reading pane alone, full width.
  var panes = { side: true, detail: true, focus: false };
  try { var savedPanes = JSON.parse(localStorage.getItem("curldb-viewer-panes") || "null"); if (savedPanes) panes = savedPanes; } catch (e) {}
  function applyPanes() {
    $("app").classList.toggle("no-side", !panes.side);
    $("split").classList.toggle("no-detail", !panes.detail);
    $("split").classList.toggle("focus", panes.detail && panes.focus);
    $("toggle-side").classList.toggle("on", panes.side);
    $("toggle-detail").classList.toggle("on", panes.detail);
    $("toggle-side").setAttribute("aria-pressed", String(panes.side));
    $("toggle-detail").setAttribute("aria-pressed", String(panes.detail));
    var full = $("full");
    if (full) { full.textContent = panes.focus ? "shrink" : "full"; full.setAttribute("aria-pressed", String(panes.focus)); }
    updateQueryUI();
    try { localStorage.setItem("curldb-viewer-panes", JSON.stringify(panes)); } catch (e) {}
  }
  function setPane(name, value) { panes[name] = value; if (name === "detail" && !value) panes.focus = false; applyPanes(); }
  $("toggle-side").addEventListener("click", function () { setPane("side", !panes.side); });
  $("toggle-detail").addEventListener("click", function () { setPane("detail", !panes.detail); });
  applyPanes();

  // Keep the filter visible even when its editor is folded away.
  function updateQueryUI() {
    var visible = S.searchOpen && !panes.focus;
    var expr = $("q").value.trim();
    $("query-editor").hidden = !visible;
    $("toggle-search").setAttribute("aria-expanded", String(visible));
    $("query-summary").hidden = !expr;
    $("query-summary-text").textContent = expr;
    $("query-summary").title = "Edit filter: " + expr;
    $("clear-query").hidden = !expr;
    $("query-help").hidden = !S.helpOpen || panes.focus;
    $("toggle-help").setAttribute("aria-expanded", String(S.helpOpen && !panes.focus));
  }
  function setSearchOpen(open, focus) {
    if (open && panes.focus) setPane("focus", false);
    S.searchOpen = open;
    updateQueryUI();
    if (focus) {
      if (open) { $("q").focus(); $("q").select(); }
      else $("list").focus({ preventScroll: true });
    }
  }
  $("toggle-search").addEventListener("click", function () { setSearchOpen(!S.searchOpen || panes.focus, true); });
  $("query-summary").addEventListener("click", function () { setSearchOpen(true, true); });
  $("close-search").addEventListener("click", function () { setSearchOpen(false, true); });
  $("clear-query").addEventListener("click", function () {
    clearTimeout(timer);
    $("q").value = "";
    updateQueryUI();
    if (S.db) runQuery("");
    $("list").focus({ preventScroll: true });
  });
  $("toggle-help").addEventListener("click", function () {
    if (panes.focus) setPane("focus", false);
    S.helpOpen = !S.helpOpen;
    updateQueryUI();
  });

  // Pane widths: drag a gutter; the value is kept in this browser.
  function makeGutter(gutter, target, cssVar, storageKey, minPx, maxFrac) {
    try { var saved = localStorage.getItem(storageKey); if (saved) target.style.setProperty(cssVar, saved); } catch (e) {}
    gutter.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      gutter.classList.add("drag");
      gutter.setPointerCapture(e.pointerId);
      var left = target.getBoundingClientRect().left;
      function move(ev) {
        var w = Math.max(minPx, Math.min(ev.clientX - left, window.innerWidth * maxFrac));
        target.style.setProperty(cssVar, w + "px");
      }
      function up() {
        gutter.classList.remove("drag");
        gutter.removeEventListener("pointermove", move);
        gutter.removeEventListener("pointerup", up);
        try { localStorage.setItem(storageKey, target.style.getPropertyValue(cssVar)); } catch (e) {}
      }
      gutter.addEventListener("pointermove", move);
      gutter.addEventListener("pointerup", up);
    });
  }
  makeGutter($("gutter"), $("app"), "--side", "curldb-viewer-side", 160, 0.6);
  makeGutter($("gutter2"), $("split"), "--listw", "curldb-viewer-listw", 220, 0.8);

  // ---- file loading -----------------------------------------------------
  // sql.js: SQLite compiled to WebAssembly, runs in this tab only.
  var ready = initSqlJs({
    locateFile: function (f) { return "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.12.0/" + f; }
  }).then(function (s) { S.SQL = s; }, function (e) {
    $("load-state").textContent = "sql.js failed to load: " + e;
  });

  function loadBuffer(buffer, name, size, mode, generation) {
    return ready.then(function () {
      if (generation !== S.live.generation) return false;
      var fresh, schema = { parent: false };
      try {
        fresh = new S.SQL.Database(new Uint8Array(buffer));
        fresh.exec("SELECT id FROM records LIMIT 1");
        fresh.exec("SELECT name, value FROM headers LIMIT 1");
        var cols = fresh.exec("PRAGMA table_info(records)");
        schema.parent = !!(cols && cols[0] && cols[0].values.some(function (c) { return c[1] === "parent"; }));
      } catch (e) {
        if (fresh) fresh.close();
        throw new Error("not a curldb file: " + e.message);
      }
      var previous = S.db, previousSelected = S.selected, previousSchema = S.schema;
      if (S.displayedGeneration !== generation) S.selected = null;
      S.db = fresh;
      S.schema = schema;
      S.renderSeq++;   // any detail render still in flight belongs to the old file
      try {
        renderTags();
        if (!runQuery($("q").value)) throw new Error("could not query snapshot");
        if (S.selected !== null) showRecord(S.selected);
        else $("detail").innerHTML = '<div class="empty">Select a record.</div>';
      } catch (e) {
        S.db = previous;
        S.schema = previousSchema;
        S.selected = previousSelected;
        fresh.close();
        throw e;
      }
      if (previous) previous.close();
      S.displayedGeneration = generation;
      var label = $("file-name");
      label.textContent = "";
      var b = document.createElement("b");
      b.textContent = name;
      label.appendChild(b);
      label.appendChild(document.createTextNode(" " + Math.round(size / 1024) + " KB"));
      var m = $("mode");
      m.hidden = false;
      m.className = "mode" + (mode === "live" ? " live" : "");
      m.textContent = mode === "live" ? "live" : "snapshot";
      $("drop").style.display = "none";
      $("app").classList.add("on");
      $("panes").classList.add("on");
      $("load-state").textContent = "";
      return true;
    });
  }

  function readInto(file, mode, generation) {
    return file.arrayBuffer().then(function (buffer) {
      return loadBuffer(buffer, file.name, file.size, mode, generation);
    });
  }

  function stopLive() {
    clearTimeout(S.live.timer);
    S.live.generation++;
    S.live.handle = null; S.live.timer = null; S.live.hash = null;
  }

  function openFile(file) {
    stopLive();
    var generation = S.live.generation;
    return readInto(file, "snapshot", generation).then(function (loaded) {
      if (loaded && generation === S.live.generation) $("list").focus();
    }).catch(function (e) {
      if (generation !== S.live.generation) return;
      $("load-state").textContent = "file read failed: " + e.message;
      $("mode").hidden = false;
      $("mode").className = "mode";
      $("mode").textContent = "file read failed: " + e.message;
    });
  }

  // Live mode: keep a handle from the picker and re-read the file whenever
  // its bytes change. curldb writers open and close per operation, so
  // SQLite checkpoints the WAL on close and the main file stays current.
  // Compare bytes, not size/mtime: a SQLite write can leave the size
  // unchanged, and file metadata through a handle is not always fresh.
  function checksum(buffer) {
    var bytes = new Uint8Array(buffer), h = 2166136261;
    for (var i = 0; i < bytes.length; i++) { h ^= bytes[i]; h = Math.imul(h, 16777619); }
    return (h >>> 0) + ":" + bytes.length;
  }

  function poll() {
    if (!S.live.handle) return Promise.resolve();
    var generation = S.live.generation, handle = S.live.handle;
    function current() { return generation === S.live.generation && handle === S.live.handle; }
    return handle.getFile().then(function (file) {
      if (!current()) return;
      return file.arrayBuffer().then(function (buffer) {
        if (!current()) return;
        var h = checksum(buffer);
        var changed = h !== S.live.hash;
        var done = changed ? loadBuffer(buffer, file.name, buffer.byteLength, "live", generation) : Promise.resolve(true);
        return done.then(function (loaded) {
          if (!current() || !loaded) return;
          S.live.hash = h;
          var m = $("mode");
          m.className = "mode live";
          m.textContent = "live " + new Date().toLocaleTimeString() + " " + Math.round(buffer.byteLength / 1024) + " KB";
        });
      });
    }).catch(function (e) {
      if (!current()) return;
      $("load-state").textContent = "live read failed: " + e.message;
      $("mode").hidden = false;
      $("mode").textContent = "live read failed: " + e.message;
      $("mode").className = "mode";
      // Keep the last good DB/hash and retry; a concurrent checkpoint can
      // produce a temporarily unreadable file. Never label that read success.
    }).then(function () { if (current()) S.live.timer = setTimeout(poll, 2000); });
  }

  function openLive() {
    var generation = S.live.generation;
    return window.showOpenFilePicker({ multiple: false }).then(function (handles) {
      if (generation !== S.live.generation) return;
      stopLive();
      S.live.handle = handles[0];
      poll();
      $("list").focus();
    }).catch(function () {});
  }

  if (window.showOpenFilePicker) {
    ["pick-live", "pick-live-2"].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.hidden = false;
      el.addEventListener("click", openLive);
    });
    $("live-hint").hidden = false;
  } else if (navigator.brave) {
    // Brave ships with the File System Access API switched off.
    var hint = $("live-hint");
    hint.hidden = false;
    hint.textContent = "Live view needs the File System Access API, which Brave disables by default: enable brave://flags/#file-system-access-api and relaunch. A dropped file is a snapshot.";
  }
  $("file-input").addEventListener("change", function () { if (this.files[0]) openFile(this.files[0]); });
  ["dragenter", "dragover"].forEach(function (ev) {
    document.addEventListener(ev, function (e) { e.preventDefault(); $("drop").classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    document.addEventListener(ev, function (e) { e.preventDefault(); $("drop").classList.remove("over"); });
  });
  document.addEventListener("drop", function (e) {
    var f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) openFile(f);
  });

  function rows(sql, params) {
    var st = S.db.prepare(sql);
    st.bind(params || []);
    var out = [];
    while (st.step()) out.push(st.getAsObject());
    st.free();
    return out;
  }
  function label(r) {
    if (r.kind === "response") return String(r.status);
    if (r.kind === "request") return r.method + " " + r.path;
    return r.kind + (r.path ? " " + r.path.split("/").pop() : "");
  }
  function labelClass(r) {
    if (r.kind !== "response") return "";
    if (r.status >= 400) return "err";
    if (r.status >= 200 && r.status < 300) return "ok";
    return "";
  }

  // ---- tags pane ------------------------------------------------------
  function renderTags() {
    var kinds = rows("SELECT kind, count(*) AS n FROM records GROUP BY kind ORDER BY n DESC");
    var statuses = rows("SELECT status, count(*) AS n FROM records WHERE status IS NOT NULL GROUP BY status ORDER BY status");
    var methods = rows("SELECT method, count(*) AS n FROM records WHERE method IS NOT NULL GROUP BY method ORDER BY n DESC");
    var hdrs = rows("SELECT name, value, count(*) AS n FROM headers GROUP BY name, value ORDER BY name, n DESC, value");
    var html = '<div class="kinds"><h2>records</h2>';
    kinds.forEach(function (k) {
      html += '<div><button class="t" data-q="kind=' + esc(k.kind) + '">' + esc(k.kind) + '</button> <span class="n">' + k.n + "</span></div>";
    });
    if (statuses.length) {
      html += '<div style="margin-top:.5rem">';
      statuses.forEach(function (s) {
        html += '<button class="t" data-q="status=' + s.status + '">' + s.status + '</button> <span class="n">' + s.n + "</span> &nbsp;";
      });
      html += "</div>";
    }
    if (methods.length) {
      html += '<div style="margin-top:.5rem">';
      methods.forEach(function (m) {
        html += '<button class="t" data-q="method=' + esc(m.method) + '">' + esc(m.method) + '</button> <span class="n">' + m.n + "</span> &nbsp;";
      });
      html += "</div>";
    }
    html += '</div><h2>headers <button type="button" class="fold" id="fold-all">fold all</button></h2>';
    var groups = Object.create(null), order = [];
    hdrs.forEach(function (h) {
      if (!groups[h.name]) { groups[h.name] = []; order.push(h.name); }
      groups[h.name].push(h);
    });
    order.forEach(function (name) {
      var vals = groups[name];
      var total = vals.reduce(function (a, v) { return a + v.n; }, 0);
      var open = vals.length <= 6 ? " open" : "";
      html += "<details" + open + '><summary><span class="sum">' + esc(name) + '<span class="n">' + vals.length + (vals.length === 1 ? " value" : " values") + " / " + total + '</span></span></summary><div class="vals">';
      vals.slice(0, 60).forEach(function (h) {
        var q = "header:" + h.name + "=" + h.value;
        html += '<button class="t" data-q="' + esc(q) + '">' + esc(h.value || "(empty)") + '</button><span class="n">' + h.n + "</span> ";
      });
      if (vals.length > 60) html += '<span class="more">+' + (vals.length - 60) + " more</span>";
      html += "</div></details>";
    });
    $("tags").innerHTML = html;
  }
  // Clicking a tag toggles it: present in the query -> removed, absent -> appended.
  function quoteTerm(term) { return /\s/.test(term) ? '"' + term + '"' : term; }
  $("tags").addEventListener("click", function (e) {
    var fold = e.target.closest("#fold-all");
    if (fold) {
      var all = $("tags").querySelectorAll("details");
      var anyOpen = Array.prototype.some.call(all, function (d) { return d.open; });
      Array.prototype.forEach.call(all, function (d) { d.open = !anyOpen; });
      fold.textContent = anyOpen ? "unfold all" : "fold all";
      return;
    }
    var b = e.target.closest("button[data-q]");
    if (!b) return;
    var q = $("q");
    var term = b.getAttribute("data-q");
    var tokens = tokenize(q.value).filter(function (t) { return t.replace(/^"|"$/g, "") !== term; });
    if (tokens.length === tokenize(q.value).length) tokens.push(quoteTerm(term));
    q.value = tokens.join(" ");
    runQuery(q.value);
  });
  function markActiveTags() {
    var active = {};
    tokenize($("q").value).forEach(function (t) { active[t.replace(/^"|"$/g, "")] = true; });
    Array.prototype.forEach.call($("tags").querySelectorAll("button[data-q]"), function (b) {
      b.classList.toggle("active", !!active[b.getAttribute("data-q")]);
    });
  }

  // ---- query ----------------------------------------------------------
  // Same shape as the command line. Body search is LIKE here.
  function tokenize(expr) {
    var out = [], cur = "", inq = false;
    for (var i = 0; i < expr.length; i++) {
      var ch = expr.charAt(i);
      if (ch === '"') { inq = !inq; cur += ch; }
      else if (ch === " " && !inq) { if (cur) { out.push(cur); cur = ""; } }
      else cur += ch;
    }
    if (cur) out.push(cur);
    return out;
  }
  function buildSql(expr) {
    var wheres = [], params = [], joins = [], hn = 0;
    tokenize(expr).forEach(function (tok) {
      if (tok.startsWith('"') && tok.endsWith('"')) tok = tok.slice(1, -1);
      var m;
      if ((m = tok.match(/^kind=(request|response|note|raw)$/))) { wheres.push("r.kind = ?"); params.push(m[1]); return; }
      if ((m = tok.match(/^status=(\d[\d,]*)$/))) {
        var codes = m[1].split(",").map(Number);
        wheres.push("r.status IN (" + codes.map(function () { return "?"; }).join(",") + ")");
        params.push.apply(params, codes);
        return;
      }
      if ((m = tok.match(/^method=([A-Za-z]+)$/))) { wheres.push("r.method = ?"); params.push(m[1].toUpperCase()); return; }
      if ((m = tok.match(/^parent=(\d+)$/))) { wheres.push(S.schema.parent ? "r.parent = ?" : "0 = ?"); params.push(Number(m[1])); return; }
      if ((m = tok.match(/^path=(\S+)$/))) { wheres.push("r.path = ?"); params.push(m[1]); return; }
      if ((m = tok.match(/^path~(\S+)$/))) { wheres.push("r.path LIKE ?"); params.push("%" + m[1] + "%"); return; }
      if ((m = tok.match(/^header:([^=]+)=(.*)$/))) {
        var a = "h" + hn++;
        joins.push("JOIN headers " + a + " ON " + a + ".record_id = r.id");
        wheres.push("LOWER(" + a + ".name) = LOWER(?)");
        wheres.push(a + ".value = ?");
        params.push(m[1], m[2]);
        return;
      }
      if ((m = tok.match(/^header:(\S+)$/))) {
        var b = "h" + hn++;
        joins.push("JOIN headers " + b + " ON " + b + ".record_id = r.id");
        wheres.push("LOWER(" + b + ".name) = LOWER(?)");
        params.push(m[1]);
        return;
      }
      m = tok.match(/^body[~:](.+)$/);
      wheres.push("r.body LIKE ?");
      var term = m ? m[1] : tok;
      if (term.startsWith('"') && term.endsWith('"')) term = term.slice(1, -1);
      params.push("%" + term + "%");
    });
    // With the parent column, a reply also carries the time of what it
    // answers, so the list can show how long the answer took.
    var extra = S.schema.parent ? ", r.parent, p.ts AS pts" : ", NULL AS parent, NULL AS pts";
    var pjoin = S.schema.parent ? " LEFT JOIN records p ON p.id = r.parent" : "";
    return {
      sql: "SELECT DISTINCT r.id, r.kind, r.status, r.method, r.path, r.ts, substr(r.body, 1, 160) AS preview" + extra +
           " FROM records r " + joins.join(" ") + pjoin + " WHERE " + (wheres.length ? wheres.join(" AND ") : "1") + " ORDER BY r.id DESC LIMIT 500",
      params: params
    };
  }

  function runQuery(expr) {
    updateQueryUI();
    markActiveTags();
    var built = buildSql(expr || "");
    var list = $("list"), result;
    try { result = rows(built.sql, built.params); }
    catch (e) { list.innerHTML = '<div class="err-box">' + esc(e.message) + "</div>"; $("count").textContent = ""; return false; }
    $("count").textContent = result.length + (result.length === 500 ? "+" : "");
    if (!result.length) { list.innerHTML = '<div class="empty">(no matches)</div>'; return true; }
    var html = '<div class="list-head" aria-hidden="true"><span>ID</span><span>Kind</span><span>Method</span><span>Status</span><span class="resource-heading">Resource</span></div>', day = null;
    result.forEach(function (r) {
      var d = CV.dayOf(r.ts);
      if (d !== day) { day = d; html += '<div class="day">' + d + "</div>"; }
      var took = r.pts ? ' <span class="took">+' + CV.duration(r.ts - r.pts) + "</span>" : "";
      var re = r.parent ? '<span class="re">re #' + r.parent + "</span> " : "";
      html += '<div class="row' + (S.selected === r.id ? " sel" : "") + '" data-id="' + r.id + '">' +
        '<span class="id">' + r.id + "</span>" +
        '<span class="kind kind-' + esc(r.kind) + '" aria-label="Kind: ' + esc(r.kind) + '">' + esc(r.kind) + '</span>' +
        '<span class="method" title="Method: ' + esc(r.method || 'not applicable') + '">' + esc(r.method || '-') + '</span>' +
        '<span class="status ' + labelClass(r) + '" title="Status: ' + esc(r.status == null ? 'not applicable' : r.status) + '">' + (r.status == null ? '-' : esc(r.status)) + '</span>' +
        '<span class="resource" title="' + esc(r.path || 'No request target') + '">' + esc(r.path || '-') + '</span>' +
        '<span class="preview">' + re + esc((r.preview || "").replace(/\s+/g, " ")) + "</span>" +
        '<span class="ts">' + CV.tsShort(r.ts) + took + "</span></div>";
    });
    list.innerHTML = html;
    return true;
  }
  var timer = null;
  $("q").addEventListener("input", function () {
    updateQueryUI();
    clearTimeout(timer);
    timer = setTimeout(function () { runQuery($("q").value); }, 120);
  });

  // ---- selection and keys ----------------------------------------------
  function rowIds() {
    return Array.prototype.map.call($("list").querySelectorAll(".row[data-id]"), function (r) { return Number(r.getAttribute("data-id")); });
  }
  function selectRow(id) {
    S.selected = id;
    Array.prototype.forEach.call($("list").querySelectorAll(".row"), function (r) {
      var on = Number(r.getAttribute("data-id")) === S.selected;
      r.classList.toggle("sel", on);
      if (on && r.scrollIntoView) r.scrollIntoView({ block: "nearest" });
    });
    if (!panes.detail) setPane("detail", true);
    showRecord(S.selected);
  }
  function moveSelection(step) {
    var ids = rowIds();
    if (!ids.length) return;
    var i = ids.indexOf(S.selected);
    var next = i < 0 ? (step > 0 ? 0 : ids.length - 1) : Math.max(0, Math.min(ids.length - 1, i + step));
    if (ids[next] !== S.selected) selectRow(ids[next]);
  }
  $("list").addEventListener("click", function (e) {
    var row = e.target.closest(".row[data-id]");
    if (!row) return;
    selectRow(Number(row.getAttribute("data-id")));
  });
  $("detail").addEventListener("click", function (e) {
    var go = e.target.closest("[data-go]");
    if (go) selectRow(Number(go.getAttribute("data-go")));
  });
  document.addEventListener("keydown", function (e) {
    var t = e.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    if (e.key === "Escape") {
      if (S.helpOpen && !panes.focus) {
        e.preventDefault(); S.helpOpen = false; updateQueryUI(); $("toggle-help").focus(); return;
      }
      if (S.searchOpen && !panes.focus && (!typing || t === $("q"))) {
        e.preventDefault(); setSearchOpen(false, true); return;
      }
      if (typing) { t.blur(); return; }
      if (panes.focus) setPane("focus", false);
      else if (panes.detail) setPane("detail", false);
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if ((e.key === "Enter" || e.key === " ") && t && t.closest && t.closest("button, a, summary, select")) return;
    if (e.key === "/") { e.preventDefault(); setSearchOpen(true, true); }
    else if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
    else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
    else if (e.key === "Enter" || e.key === "o") { if (S.selected !== null) selectRow(S.selected); }
    else if (e.key === "[") setPane("side", !panes.side);
    else if (e.key === "]") setPane("detail", !panes.detail);
    else if (e.key === "f") { if (panes.detail) setPane("focus", !panes.focus); }
  });

  // ---- reading pane ----------------------------------------------------
  // The record, then the records it is paired with: the replies to a
  // request, or the request a reply answers. Pairing is the parent
  // column when the file has it, else the Link rel="parent" header.
  var COLS = "id, kind, status, method, path, raw, ts" ;
  function fetchRow(id) {
    return rows("SELECT " + COLS + (S.schema.parent ? ", parent" : ", NULL AS parent") + " FROM records WHERE id = ?", [id])[0] || null;
  }
  function parentOf(p) {
    if (S.schema.parent && p.row.parent) return p.row.parent;
    if (p.parentInMessage) return p.parentInMessage;
    if (p.parentTarget) {
      // An address: the record whose Content-Location it names.
      var hit = rows("SELECT record_id FROM headers WHERE LOWER(name) = 'content-location' AND value = ? ORDER BY record_id LIMIT 1", [p.parentTarget])[0];
      if (hit) return hit.record_id;
    }
    return null;
  }
  function repliesTo(id, p) {
    var seen = {}, out = [];
    function take(list) { list.forEach(function (r) { if (!seen[r.id]) { seen[r.id] = true; out.push(r); } }); }
    var pcol = S.schema.parent ? ", parent" : ", NULL AS parent", pcolR = S.schema.parent ? ", r.parent" : ", NULL AS parent";
    if (S.schema.parent) take(rows("SELECT " + COLS + pcol + " FROM records WHERE parent = ? ORDER BY id", [id]));
    // By id in the message, and by address against this record's Content-Location.
    take(rows("SELECT DISTINCT r." + COLS.split(", ").join(", r.") + pcolR + " FROM records r JOIN headers h ON h.record_id = r.id" +
              " WHERE LOWER(h.name) = 'link' AND (h.value LIKE ? OR h.value LIKE ?" + (p.address ? " OR h.value LIKE ?" : "") + ") ORDER BY r.id",
              ["%</" + id + ">%parent%", "%<" + id + ">%parent%"].concat(p.address ? ["%<" + p.address + ">%parent%"] : [])));
    out.sort(function (a, b) { return a.id - b.id; });
    return out;
  }
  function goButton(r, from) {
    var took = from != null ? ' <span class="took">+' + CV.duration(r.ts - from) + "</span>" : "";
    return '<button type="button" data-go="' + r.id + '">#' + r.id + " " + esc(label(r)) + "</button>" + took;
  }

  function showRecord(id) {
    var r = fetchRow(id);
    if (!r) return;
    var seq = ++S.renderSeq;   // a later showRecord (new id or new file) wins
    var relatedKey = S.live.generation + ":" + r.id;
    if (S.relatedKey !== relatedKey) { S.relatedKey = relatedKey; S.relatedOpen = false; }
    CV.revokeUrls();
    var p = CV.recordParts(r);
    var parentId = parentOf(p), parentRow = parentId ? fetchRow(parentId) : null;
    var replies = repliesTo(r.id, p);
    var ids = rowIds(), at = ids.indexOf(r.id);
    var meta = '<div class="meta"><span class="id">#' + r.id + "</span><span>" + esc(r.kind) + "</span><span>" +
      esc(new Date(r.ts * 1000).toLocaleString()) + "</span><span>" + CV.human(p.bytes.length) + "</span>" +
      (p.bodyType ? "<span>" + esc(p.bodyType) + "</span>" : "") + "</div>" +
      '<div class="meta actions">' +
      '<button type="button" id="prev"' + (at > 0 ? "" : " disabled") + ">prev</button>" +
      '<button type="button" id="next"' + (at >= 0 && at < ids.length - 1 ? "" : " disabled") + ">next</button>" +
      '<button type="button" id="view-rendered"' + (S.viewMode === "rendered" ? ' class="on"' : "") + ">rendered</button>" +
      '<button type="button" id="view-source"' + (S.viewMode === "source" ? ' class="on"' : "") + ">source</button>" +
      '<span class="right">' +
      (p.head && p.head.method ? '<button type="button" id="curl">copy as curl</button>' : "") +
      (p.bodyBytes.length && p.bodyBytes !== p.bytes ? '<a download="' + esc(p.name) + '" href="' + CV.blobUrl(p.bodyBytes, p.bodyType || "application/octet-stream") + '">save body</a>' : "") +
      (p.media ? '<a href="' + CV.blobUrl(p.bodyBytes, p.bodyType) + '" target="_blank" rel="noopener">open</a>' : "") +
      '<a download="' + esc(p.name) + (r.kind === "request" || r.kind === "response" ? ".http" : "") + '" href="' + CV.blobUrl(p.bytes, "application/octet-stream") + '">save raw</a>' +
      (p.text ? '<button type="button" id="copy">copy</button>' : "") +
      '<button type="button" id="full">' + (panes.focus ? "shrink" : "full") + "</button>" +
      '<button type="button" id="close">close</button>' +
      "</span></div>";
    var relatedLinks = "";
    if (parentRow) relatedLinks += "<span>parent " + goButton(parentRow, null) + "</span>";
    if (replies.length) relatedLinks += "<span>children " + replies.map(function (c) { return goButton(c, r.ts); }).join(" ") + "</span>";
    // The paired records render below the current one, mail-thread style.
    var paired = (parentRow ? [{ r: parentRow, why: "answers" }] : []).concat(replies.slice(0, 5).map(function (c) { return { r: c, why: "reply" }; }));
    // The receipt: what the door says about this record in the outer
    // headers of GET /<id>. Shown above the message so the pairing is
    // visible as header lines, not only as a strip.
    var last = (rows("SELECT max(id) AS m FROM records")[0] || {}).m || 0;
    var stamp = [["X-Id", r.id], ["X-Kind", r.kind]];
    if (r.status != null) stamp.push(["X-Status", r.status]);
    if (r.method) stamp.push(["X-Method", r.method]);
    if (r.path) stamp.push(["X-Path", r.path]);
    stamp.push(["Last-Modified", new Date(r.ts * 1000).toUTCString()]);
    var links = [];
    if (parentId) links.push("</" + parentId + '>; rel="parent"');
    if (r.id > 1) links.push("</" + (r.id - 1) + '>; rel="prev"');
    if (r.id < last) links.push("</" + (r.id + 1) + '>; rel="next"');
    if (links.length) stamp.push(["Link", links.join(", ")]);
    var stampHtml = '<pre class="stamp"><span class="hn">receipt (GET /' + r.id + ")</span>\n" + stamp.map(function (kv) {
      return '<span class="hn">' + esc(kv[0]) + ":</span> " + esc(kv[1]);
    }).join("\n") + "</pre>";
    var mainHtml = CV.renderRecord(p, S.viewMode).then(function (html) { return stampHtml + html; });
    var relatedHtml = paired.length ? '<details class="related" id="related"' + (S.relatedOpen ? ' open' : '') + '><summary>Related records <span class="badge">' +
      ((parentRow ? 1 : 0) + replies.length) + '</span><span class="summary-hint">expand to preview</span></summary>' +
      '<div class="meta thread">' + relatedLinks + '</div><div id="related-body"></div></details>' : "";
    mainHtml.then(function (html) {
      if (seq !== S.renderSeq) return;
      $("detail").innerHTML = meta + relatedHtml + html;
      var related = $("related"), relatedLoaded = false;
      if (related && paired.length) related.addEventListener("toggle", function () {
        if (seq !== S.renderSeq) return;
        S.relatedOpen = related.open;
        if (!related.open || relatedLoaded) return;
        relatedLoaded = true;
        var target = $("related-body");
        target.textContent = "Loading previews...";
        var mode = S.viewMode;
        Promise.all(paired.map(function (x) {
          var q = CV.recordParts(x.r);
          return CV.renderRecord(q, mode).then(function (bodyHtml) {
            var dt = x.r.ts - r.ts;
            return '<div class="pair"><div class="pairhead">' + (x.why === "reply" ? "child " : "parent ") +
              goButton(x.r, null) + ' <span class="took">interval ' + CV.duration(Math.abs(dt)) +
              (dt >= 0 ? " later" : " earlier") + '</span></div>' + bodyHtml + '</div>';
          });
        })).then(function (parts) {
          if (seq !== S.renderSeq || $("related") !== related) return;
          target.innerHTML = parts.join("") + (replies.length > 5 ? '<p class="summary-hint">First 5 child previews shown; use the links above for more.</p>' : "");
        }).catch(function (e) {
          if (seq !== S.renderSeq || $("related") !== related) return;
          target.textContent = "Preview failed: " + e.message;
          relatedLoaded = false;
        });
      });
      $("detail").scrollTop = 0;
      function on(id, fn) { var el = $(id); if (el) el.addEventListener("click", fn); }
      function flash(id, text) { var el = $(id); if (!el) return; var was = el.textContent; el.textContent = text; setTimeout(function () { var e2 = $(id); if (e2) e2.textContent = was; }, 1200); }
      on("prev", function () { moveSelection(-1); });
      on("next", function () { moveSelection(1); });
      on("view-rendered", function () { S.viewMode = "rendered"; showRecord(r.id); });
      on("view-source", function () { S.viewMode = "source"; showRecord(r.id); });
      on("full", function () { setPane("focus", !panes.focus); showRecord(r.id); });
      on("close", function () { setPane("detail", false); });
      on("copy", function () { navigator.clipboard.writeText(CV.decode(p.bytes)).then(function () { flash("copy", "copied"); }); });
      on("curl", function () { navigator.clipboard.writeText(CV.curlCommand(p.head, p.bodyBytes)).then(function () { flash("curl", "copied"); }); });
    });
  }

  // Handles for tests and for poking at the page from the console.
  CV.app = {
    S: S, buildSql: buildSql, tokenize: tokenize, openFile: openFile, poll: poll, stopLive: stopLive, live: S.live,
    getDb: function () { return S.db; }, seq: function () { return S.renderSeq; },
    runQuery: runQuery, showRecord: showRecord, selectRow: selectRow, setPane: setPane, panes: panes,
    setSearchOpen: setSearchOpen, updateQueryUI: updateQueryUI
  };
})();
