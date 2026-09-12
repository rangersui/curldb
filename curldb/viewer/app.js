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
    served: null,               // {etag, events} when the page came from curldb serve
    folder: "inbox",            // inbox (responses) | outbox (requests) | notes | flagged | archived | all
    read: {},                   // record id -> true, this browser's own reading state
    schema: { parent: false, host: false }   // parent column (0.4+), host column (0.6+)
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
  $("save-query").addEventListener("click", function () {
    var expr = $("q").value.trim();
    if (!expr) { $("load-state").textContent = "nothing to save: the query is empty"; return; }
    var name = window.prompt("Save this query as @name", "");
    if (!name || !/^[A-Za-z0-9_.-]+$/.test(name)) return;
    saveQueryServed(name, expr).catch(function (e) { $("load-state").textContent = e.message; });
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
      var fresh, schema = { parent: false, host: false };
      try {
        fresh = new S.SQL.Database(new Uint8Array(buffer));
        fresh.exec("SELECT id FROM records LIMIT 1");
        fresh.exec("SELECT name, value FROM headers LIMIT 1");
        var cols = fresh.exec("PRAGMA table_info(records)");
        var names = cols && cols[0] ? cols[0].values.map(function (c) { return c[1]; }) : [];
        schema.parent = names.indexOf("parent") >= 0;
        schema.host = names.indexOf("host") >= 0;
        fresh.exec(effectiveView(schema.host));   // this copy is private; the view overlays PATCH /n amendments
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
      m.className = "mode" + (mode === "live" || mode === "served" ? " live" : "");
      m.textContent = mode === "live" ? "live" : mode === "served" ? "served " + new Date().toLocaleTimeString() : "snapshot";
      loadRead();
      $("save-query").hidden = !S.served;
      $("drop").style.display = "none";
      $("app").classList.add("on");
      $("panes").classList.add("on");
      $("load-state").textContent = "";
      return true;
    });
  }

  // Same definition as curldb's _init: amendments are `PATCH /12` records
  // whose headers replace record 12's headers of the same name; an empty
  // value clears the name. Older files have no view, so the copy gets one.
  // A PATCH /n addressed to some other host (a gateway or replay record,
  // host column set) is that host's traffic, not an amendment here.
  function ownClause(hasHost) { return hasHost ? " AND host IS NULL" : ""; }
  function effectiveView(hasHost) { return EFFECTIVE_VIEW.replace("{OWN}", ownClause(hasHost)); }
  var EFFECTIVE_VIEW =
    "DROP VIEW IF EXISTS effective_headers; CREATE VIEW effective_headers AS " +
    "WITH amendrec AS (SELECT id, CAST(substr(path, 2) AS INTEGER) AS target FROM records WHERE kind = 'request' " +
    " AND method = 'PATCH'{OWN} AND path GLOB '/[0-9]*' AND path NOT GLOB '/*[^0-9]*'), " +
    "amend AS (SELECT p.id AS aid, h.rowid AS hid, p.target, h.name, h.value FROM amendrec p JOIN headers h ON h.record_id = p.id " +
    " WHERE lower(h.name) <> 'via'), " +
    "latest AS (SELECT a.target, a.name, a.value FROM amend a WHERE (a.aid, a.hid) = (SELECT b.aid, b.hid FROM amend b " +
    " WHERE b.target = a.target AND lower(b.name) = lower(a.name) ORDER BY b.aid DESC, b.hid DESC LIMIT 1)) " +
    "SELECT h.record_id, h.name, h.value FROM headers h WHERE h.record_id NOT IN (SELECT id FROM amendrec) " +
    " AND NOT EXISTS (SELECT 1 FROM latest l " +
    " WHERE l.target = h.record_id AND lower(l.name) = lower(h.name)) " +
    "UNION ALL SELECT target AS record_id, name, value FROM latest WHERE value <> ''";

  // ---- served mode -----------------------------------------------------
  // When this page came from `curldb serve`, the door is the source: GET
  // /db is the file, GET /events says when it grew, and PATCH /<id> files
  // an amendment. Same origin, so no CORS and no token; a dropped file
  // still works and switches the page back to file mode.
  function probeServed() {
    if (typeof location === "undefined" || typeof fetch === "undefined") return Promise.resolve(false);
    if (location.protocol !== "http:" && location.protocol !== "https:") return Promise.resolve(false);
    return fetch("/db", { method: "HEAD", cache: "no-store" }).then(function (r) {
      return r.ok && (r.headers.get("Content-Type") || "").indexOf("application/vnd.sqlite3") === 0;
    }).catch(function () { return false; });
  }
  function tailOf(etag) { var m = /"?(\d+)"?/.exec(etag || ""); return m ? Number(m[1]) : 0; }
  function loadServed(generation) {
    // Reloads are ordered: a snapshot only lands if it is the newest
    // request and newer than what the page holds (the ETag is the tail).
    var served = S.served, seq = ++served.seq;
    var headers = served.etag ? { "If-None-Match": served.etag } : {};
    return fetch("/db", { headers: headers, cache: "no-store" }).then(function (r) {
      if (r.status === 304) return true;
      if (!r.ok) throw new Error("GET /db: " + r.status);
      var etag = r.headers.get("ETag");
      return r.arrayBuffer().then(function (buffer) {
        if (generation !== S.live.generation || S.served !== served || seq !== served.seq) return false;
        if (tailOf(etag) < tailOf(served.etag)) return false;
        return loadBuffer(buffer, location.host, buffer.byteLength, "served", generation).then(function (ok) {
          if (ok && S.served === served) served.etag = etag;
          return ok;
        });
      });
    });
  }
  function openServed() {
    stopLive();
    var generation = S.live.generation;
    var served = { etag: null, events: null, timer: null, seq: 0, backoff: 0 };
    S.served = served;
    function current() { return generation === S.live.generation && S.served === served; }
    // A reload that fails is retried with backoff (0.25 s doubling to 30 s)
    // until one lands, so a stream event is never lost to a bad download.
    // Errors are shown only while this source is still the page's source.
    function scheduleReload(delay) {
      clearTimeout(served.timer);
      served.timer = setTimeout(function () {
        if (!current()) return;
        loadServed(generation).then(function () {
          if (current()) { served.backoff = 0; $("load-state").textContent = ""; }
        }, function (e) {
          if (!current()) return;
          served.backoff = Math.min(served.backoff ? served.backoff * 2 : 1000, 30000);
          $("load-state").textContent = "reload failed: " + e.message + "; retrying in " + Math.round(served.backoff / 1000) + " s";
          scheduleReload(served.backoff);
        });
      }, delay);
    }
    return loadServed(generation).then(function () {
      if (!current()) return;
      // The stream starts right after the snapshot the page holds, so a
      // record filed between the two is not skipped.
      var es = new EventSource("/events?after=" + tailOf(served.etag));
      served.events = es;
      es.onmessage = function () { if (current()) scheduleReload(250); };   // coalesces bursts
      es.onerror = function () { if (current()) { var m = $("mode"); if (m) m.textContent = "served (reconnecting)"; } };
    }).catch(function (e) {
      if (!current()) return;
      $("load-state").textContent = "served load failed: " + e.message;
    });
  }
  function amendServed(rid, headers) {
    if (!S.served) return Promise.reject(new Error("amendments need curldb serve"));
    var h = { "Via": "curldb-viewer" };
    Object.keys(headers).forEach(function (k) { h[k] = headers[k]; });
    return fetch("/" + rid, { method: "PATCH", headers: h, cache: "no-store" }).then(function (r) {
      if (r.status !== 201) throw new Error("PATCH /" + rid + ": " + r.status);
    });
  }
  function saveQueryServed(name, expr) {
    return fetch("/queries/" + encodeURIComponent(name), { method: "PUT", headers: { "Content-Type": "text/plain" }, body: expr, cache: "no-store" })
      .then(function (r) { if (r.status !== 201) throw new Error("PUT /queries: " + r.status); });
  }

  // Reading state is the reader's, not the letter's: kept in this browser.
  function readKey() { return "curldb-read:" + (S.served ? location.host : ($("file-name").textContent || "file")); }
  function loadRead() { try { S.read = JSON.parse(localStorage.getItem(readKey()) || "{}"); } catch (e) { S.read = {}; } }
  function markRead(id, on) {
    if (on) S.read[id] = true; else delete S.read[id];
    try { localStorage.setItem(readKey(), JSON.stringify(S.read)); } catch (e) {}
    var row = $("list").querySelector('.row[data-id="' + id + '"]');
    if (row) row.classList.toggle("unread", !S.read[id]);
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
    if (S.served) { clearTimeout(S.served.timer); if (S.served.events) S.served.events.close(); S.served = null; }
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
  probeServed().then(function (yes) { if (yes) openServed(); });
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
    var hdrs = rows("SELECT name, value, count(*) AS n FROM effective_headers GROUP BY name, value ORDER BY name, n DESC, value");
    var saved = savedQueries();
    var marks = { flagged: 0, archived: 0 };
    rows("SELECT lower(name) AS n, count(DISTINCT record_id) AS c FROM effective_headers WHERE lower(name) IN ('x-flag', 'x-archive') AND value = 'true' GROUP BY lower(name)")
      .forEach(function (x) { marks[x.n === "x-flag" ? "flagged" : "archived"] = x.c; });
    var html = '<div class="kinds folders"><h2>mailbox</h2>';
    // Folder counts use the folder's own rule: not archived, no bookkeeping.
    var byKind = {};
    rows("SELECT CASE WHEN r.kind = 'response' THEN 'response' WHEN r.kind = 'request' THEN 'request' ELSE 'note' END AS k, count(*) AS n FROM records r" +
         " WHERE NOT " + ARCHIVED_CLAUSE + " AND NOT " + bookkeepingClause() + " GROUP BY k").forEach(function (k) { byKind[k.k] = k.n; });
    // A mailbox: what came in (responses) and what went out (requests);
    // notes are neither. Flag and archive cut across all of them.
    [["inbox", "inbox", "responses", byKind.response], ["outbox", "outbox", "requests", byKind.request],
     ["notes", "notes", "notes and files", byKind.note],
     ["flagged", "flagged", null, marks.flagged], ["archived", "archived", null, marks.archived], ["all", "everything", null, null]].forEach(function (f) {
      html += '<div><button class="t folder' + (S.folder === f[0] ? " active" : "") + '" data-folder="' + f[0] + '"' + (f[2] ? ' title="' + f[2] + '"' : "") + ">" + f[1] + "</button>" +
        (f[2] ? ' <span class="n">' + f[2] + "</span>" : "") + (f[3] ? ' <span class="n">' + f[3] + "</span>" : "") + "</div>";
    });
    html += '</div><div class="kinds"><h2>records</h2>';
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
    var names = Object.keys(saved);
    if (names.length) {
      html += '</div><div class="kinds"><h2>saved queries</h2>';
      names.forEach(function (n) { html += '<div><button class="t" data-q="@' + esc(n) + '" title="' + esc(saved[n]) + '">@' + esc(n) + "</button></div>"; });
    }
    html += '</div><h2>headers <button type="button" class="fold" id="fold-all">fold all</button></h2>';
    var groups = Object.create(null), order = [];
    hdrs.forEach(function (h) {
      var low = h.name.toLowerCase();
      if (low === "x-flag" || low === "x-archive") return;   // the mailbox folders above
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
    var folder = e.target.closest("button[data-folder]");
    if (folder) {
      S.folder = folder.getAttribute("data-folder");
      Array.prototype.forEach.call($("tags").querySelectorAll("button[data-folder]"), function (x) {
        x.classList.toggle("active", x.getAttribute("data-folder") === S.folder);
      });
      runQuery($("q").value);
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
  // The clauses the folders are made of (r is the records row).
  var ARCHIVED_CLAUSE = "EXISTS (SELECT 1 FROM effective_headers x WHERE x.record_id = r.id AND lower(x.name) = 'x-archive' AND x.value = 'true')";
  var FLAGGED_CLAUSE = "EXISTS (SELECT 1 FROM effective_headers x WHERE x.record_id = r.id AND lower(x.name) = 'x-flag' AND x.value = 'true')";
  function bookkeepingClause() {
    return "(r.kind = 'request'" + (S.schema.host ? " AND r.host IS NULL" : "") +
      " AND ((r.method = 'PATCH' AND r.path GLOB '/[0-9]*' AND r.path NOT GLOB '/*[^0-9]*') OR r.path GLOB '/queries/?*'))";
  }

  // Saved queries are `PUT /queries/<name>` records, latest wins, a
  // `DELETE /queries/<name>` removes one. `@name` in a query expands.
  function savedQueries() {
    var out = {};
    rows("SELECT method, path, body FROM records WHERE kind = 'request'" + ownClause(S.schema.host) + " AND path GLOB '/queries/?*' AND method IN ('PUT', 'DELETE') ORDER BY id")
      .forEach(function (r) { var n = r.path.slice("/queries/".length); if (r.method === "DELETE") delete out[n]; else out[n] = (r.body || "").trim(); });
    return out;
  }
  function expandSaved(expr, depth) {
    if (expr.indexOf("@") < 0) return expr;
    var saved = savedQueries();
    return tokenize(expr).map(function (t) {
      if (t.charAt(0) !== "@") return t;
      var e = saved[t.slice(1)];
      if (e === undefined) throw new Error("no saved query " + t);
      return (depth || 0) > 8 ? e : expandSaved(e, (depth || 0) + 1);
    }).join(" ");
  }
  function buildSql(expr) {
    var wheres = [], params = [], joins = [], hn = 0, explicit = false;
    expr = expandSaved(expr);
    tokenize(expr).forEach(function (tok) {
      if (tok.startsWith('"') && tok.endsWith('"')) tok = tok.slice(1, -1);
      var m, negate = tok.length > 1 && tok.charAt(0) === "-", before = wheres.length;
      if (negate) tok = tok.slice(1);
      function done() { if (negate && wheres.length > before) { var added = wheres.splice(before); wheres.push("NOT COALESCE((" + added.join(" AND ") + "), 0)"); } }
      if ((m = tok.match(/^kind=(request|response|note|raw)$/))) { wheres.push("r.kind = ?"); params.push(m[1]); done(); return; }
      if ((m = tok.match(/^status=(\d[\d,]*)$/))) {
        var codes = m[1].split(",").map(Number);
        wheres.push("r.status IN (" + codes.map(function () { return "?"; }).join(",") + ")");
        params.push.apply(params, codes);
        done();
        return;
      }
      if ((m = tok.match(/^method=([A-Za-z]+)$/))) { wheres.push("r.method = ?"); params.push(m[1].toUpperCase()); explicit = true; done(); return; }
      if ((m = tok.match(/^parent=(\d+)$/))) { wheres.push(S.schema.parent ? "r.parent = ?" : "0 = ?"); params.push(Number(m[1])); done(); return; }
      if ((m = tok.match(/^path=(\S+)$/))) { wheres.push("r.path = ?"); params.push(m[1]); explicit = true; done(); return; }
      if ((m = tok.match(/^path~(\S+)$/))) { wheres.push("r.path LIKE ?"); params.push("%" + m[1] + "%"); explicit = true; done(); return; }
      if ((m = tok.match(/^header:([^=]+)=(.*)$/))) {
        if (m[1].toLowerCase() === "x-archive") explicit = true;
        if (negate) { wheres.push("NOT EXISTS (SELECT 1 FROM effective_headers x WHERE x.record_id = r.id AND LOWER(x.name) = LOWER(?) AND x.value = ?)"); params.push(m[1], m[2]); return; }
        var a = "h" + hn++;
        joins.push("JOIN effective_headers " + a + " ON " + a + ".record_id = r.id");
        wheres.push("LOWER(" + a + ".name) = LOWER(?)");
        wheres.push(a + ".value = ?");
        params.push(m[1], m[2]);
        return;
      }
      if ((m = tok.match(/^header:(\S+)$/))) {
        if (m[1].toLowerCase() === "x-archive") explicit = true;
        if (negate) { wheres.push("NOT EXISTS (SELECT 1 FROM effective_headers x WHERE x.record_id = r.id AND LOWER(x.name) = LOWER(?))"); params.push(m[1]); return; }
        var b = "h" + hn++;
        joins.push("JOIN effective_headers " + b + " ON " + b + ".record_id = r.id");
        wheres.push("LOWER(" + b + ".name) = LOWER(?)");
        params.push(m[1]);
        return;
      }
      m = tok.match(/^body[~:](.+)$/);
      wheres.push(negate ? "r.body NOT LIKE ?" : "r.body LIKE ?");
      var term = m ? m[1] : tok;
      if (term.startsWith('"') && term.endsWith('"')) term = term.slice(1, -1);
      params.push("%" + term + "%");
    });
    // With the parent column, a reply also carries the time of what it
    // answers, so the list can show how long the answer took.
    var extra = S.schema.parent ? ", r.parent, p.ts AS pts" : ", NULL AS parent, NULL AS pts";
    var pjoin = S.schema.parent ? " LEFT JOIN records p ON p.id = r.parent" : "";
    // The mailbox folders. Flag and archive are ordinary amendments in
    // the log, but the two that shape the view, so they are not filters
    // among the tags: inbox is everything not archived, minus the
    // bookkeeping records (amendments, saved queries).
    var archivedClause = ARCHIVED_CLAUSE, flaggedClause = FLAGGED_CLAUSE, bookkeeping = bookkeepingClause();
    if (S.folder === "flagged") { wheres.push(flaggedClause); wheres.push("NOT " + bookkeeping); }
    else if (S.folder === "archived") wheres.push(archivedClause);
    else if (S.folder === "all" || explicit) { /* the whole log, or what the query names */ }
    else {
      wheres.push("NOT " + archivedClause);
      if (S.folder === "outbox") { wheres.push("r.kind = 'request'"); wheres.push("NOT " + bookkeeping); }
      else if (S.folder === "notes") wheres.push("r.kind IN ('note', 'raw')");
      else wheres.push("r.kind = 'response'");
    }
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
    var flagged = {}, archived = {};
    rows("SELECT record_id, lower(name) AS n FROM effective_headers WHERE lower(name) IN ('x-flag', 'x-archive') AND value = 'true'")
      .forEach(function (x) { (x.n === "x-flag" ? flagged : archived)[x.record_id] = true; });
    var html = '<div class="list-head" aria-hidden="true"><span></span><span>ID</span><span>Kind</span><span>Method</span><span>Status</span><span class="resource-heading">Resource</span></div>', day = null;
    result.forEach(function (r) {
      var d = CV.dayOf(r.ts);
      if (d !== day) { day = d; html += '<div class="day">' + d + "</div>"; }
      var took = r.pts ? ' <span class="took">+' + CV.duration(r.ts - r.pts) + "</span>" : "";
      var re = r.parent ? '<span class="re">re #' + r.parent + "</span> " : "";
      html += '<div class="row' + (S.selected === r.id ? " sel" : "") + (flagged[r.id] ? " flagged" : "") + (archived[r.id] ? " archived" : "") + (S.read[r.id] ? "" : " unread") + '" data-id="' + r.id + '">' +
        '<span class="marks"><button type="button" class="mark flag" data-mark="flag" title="' + (flagged[r.id] ? "flagged: click to unflag" : "flag") + '"' + (S.served ? "" : " disabled") + "></button>" +
        '<button type="button" class="mark archive" data-mark="archive" title="' + (archived[r.id] ? "archived: click to restore" : "archive") + '"' + (S.served ? "" : " disabled") + "></button></span>" +
        '<span class="id">' + r.id + "</span>" +
        '<span class="kind kind-' + esc(r.kind) + '" aria-label="Kind: ' + esc(r.kind) + '">' + esc(r.kind) + '</span>' +
        '<span class="method" title="Method: ' + esc(r.method || 'not applicable') + '">' + esc(r.method || '-') + '</span>' +
        '<span class="status ' + labelClass(r) + '" title="Status: ' + esc(r.status == null ? 'not applicable' : r.status) + '">' + (r.status == null ? '-' : esc(r.status)) + '</span>' +
        '<span class="resource" title="' + esc(r.path || 'No request target') + '">' + esc(r.path || '-') + '</span>' +
        '<span class="preview">' + (archived[r.id] ? '<span class="chip">archived</span> ' : "") + re + esc((r.preview || "").replace(/\s+/g, " ")) + "</span>" +
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
    markRead(id, true);
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
    var mark = e.target.closest("button[data-mark]");
    if (mark) {
      // The flag column: one PATCH through the door, no selecting, no marking read.
      e.stopPropagation();
      if (!S.served) return;
      var id = Number(row.getAttribute("data-id")), h = {};
      if (mark.getAttribute("data-mark") === "archive") h["X-Archive"] = row.classList.contains("archived") ? "" : "true";
      else h["X-Flag"] = row.classList.contains("flagged") ? "" : "true";
      amendServed(id, h).catch(function (err) { $("load-state").textContent = err.message; });
      return;
    }
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
  function hostCol(prefix) { return S.schema.host ? ", " + prefix + "host" : ", NULL AS host"; }
  function fetchRow(id) {
    return rows("SELECT " + COLS + (S.schema.parent ? ", parent" : ", NULL AS parent") + hostCol("") + " FROM records WHERE id = ?", [id])[0] || null;
  }
  // The parent column is the door's own bookkeeping and always counts.
  // Reading a Link out of the message is the pre-0.4 fallback, and only
  // for this file's own records: a message that went to or came from
  // another host (host column set) talks about that host's records.
  function parentOf(p) {
    if (S.schema.parent && p.row.parent) return p.row.parent;
    if (S.schema.host && p.row.host) return null;
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
    var pcol = (S.schema.parent ? ", parent" : ", NULL AS parent") + hostCol(""), pcolR = (S.schema.parent ? ", r.parent" : ", NULL AS parent") + hostCol("r.");
    if (S.schema.parent) take(rows("SELECT " + COLS + pcol + " FROM records WHERE parent = ? ORDER BY id", [id]));
    // By id in the message, and by address against this record's
    // Content-Location; only this file's own records (see parentOf).
    take(rows("SELECT DISTINCT r." + COLS.split(", ").join(", r.") + pcolR + " FROM records r JOIN headers h ON h.record_id = r.id" +
              " WHERE LOWER(h.name) = 'link'" + (S.schema.host ? " AND r.host IS NULL" : "") +
              " AND (h.value LIKE ? OR h.value LIKE ?" + (p.address ? " OR h.value LIKE ?" : "") + ") ORDER BY r.id",
              ["%</" + id + ">%parent%", "%<" + id + ">%parent%"].concat(p.address ? ["%<" + p.address + ">%parent%"] : [])));
    out.sort(function (a, b) { return a.id - b.id; });
    return out;
  }
  function goButton(r, from) {
    var took = from != null ? ' <span class="took">+' + CV.duration(r.ts - from) + "</span>" : "";
    return '<button type="button" data-go="' + r.id + '">#' + r.id + " " + esc(label(r)) + "</button>" + took;
  }

  // A light snapshot carries big records without their bytes; the door
  // has them at GET /<id>. Fetched once per page, records never change.
  var rawCache = {};
  function withBytes(r) {
    var raw = typeof r.raw === "string" ? r.raw : (r.raw || new Uint8Array(0));
    if (raw.length || !S.served) return Promise.resolve(r);
    if (rawCache[r.id]) { r.raw = rawCache[r.id]; return Promise.resolve(r); }
    return fetch("/" + r.id, { cache: "no-store" }).then(function (resp) {
      if (!resp.ok) throw new Error("GET /" + r.id + ": " + resp.status);
      return resp.arrayBuffer();
    }).then(function (buf) { rawCache[r.id] = new Uint8Array(buf); r.raw = rawCache[r.id]; return r; });
  }
  function showRecord(id) {
    var r0 = fetchRow(id);
    if (!r0) return;
    var seq0 = S.renderSeq + 1;
    var raw0 = typeof r0.raw === "string" ? r0.raw : (r0.raw || new Uint8Array(0));
    if (!raw0.length && S.served) {
      $("detail").innerHTML = '<div class="empty">Loading #' + r0.id + " from the door...</div>";
      var gen = S.live.generation;
      withBytes(r0).then(function (r1) {
        if (gen !== S.live.generation || S.selected !== r1.id) return;
        showRecordNow(r1);
      }, function (e) { if (gen === S.live.generation) $("detail").innerHTML = '<div class="err-box">' + esc(e.message) + "</div>"; });
      return;
    }
    showRecordNow(r0);
  }
  function showRecordNow(r) {
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
    if (S.served) {
      var eff = {};
      rows("SELECT name, value FROM effective_headers WHERE record_id = ?", [r.id]).forEach(function (h) { eff[h.name.toLowerCase()] = h.value; });
      var flagged = eff["x-flag"] === "true", archived = eff["x-archive"] === "true";
      meta += '<div class="meta actions mail">' +
        '<button type="button" id="flag">' + (flagged ? "unflag" : "flag") + "</button>" +
        '<button type="button" id="archive">' + (archived ? "unarchive" : "archive") + "</button>" +
        '<button type="button" id="unread">mark unread</button>' +
        '<button type="button" id="add-header">add header</button>' +
        '<span class="took">each one is a PATCH /' + r.id + " filed through the door</span></div>";
    }
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
    var mainHtml = CV.renderRecord(p, S.viewMode, parentRow && parentRow.path).then(function (html) { return stampHtml + html; });
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
          return CV.renderRecord(q, mode, x.why === "reply" ? r.path : null).then(function (bodyHtml) {
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
      function patch(headers) { amendServed(r.id, headers).catch(function (e) { $("load-state").textContent = e.message; }); }
      on("flag", function () { patch({ "X-Flag": $("flag").textContent === "flag" ? "true" : "" }); });
      on("archive", function () { patch({ "X-Archive": $("archive").textContent === "archive" ? "true" : "" }); });
      on("unread", function () { markRead(r.id, false); });
      on("add-header", function () {
        var line = window.prompt("Header to add to #" + r.id + " (Name: value; Name: alone clears)", "X-");
        if (!line || line.indexOf(":") < 0) return;
        var h = {}; h[line.slice(0, line.indexOf(":")).trim()] = line.slice(line.indexOf(":") + 1).trim();
        patch(h);
      });
      on("copy", function () { navigator.clipboard.writeText(CV.decode(p.bytes)).then(function () { flash("copy", "copied"); }); });
      on("curl", function () { navigator.clipboard.writeText(CV.curlCommand(p.head, p.bodyBytes)).then(function () { flash("curl", "copied"); }); });
    });
  }

  // Handles for tests and for poking at the page from the console.
  CV.app = {
    S: S, buildSql: buildSql, tokenize: tokenize, openFile: openFile, poll: poll, stopLive: stopLive, live: S.live,
    getDb: function () { return S.db; }, seq: function () { return S.renderSeq; },
    runQuery: runQuery, showRecord: showRecord, selectRow: selectRow, setPane: setPane, panes: panes,
    setSearchOpen: setSearchOpen, updateQueryUI: updateQueryUI, savedQueries: savedQueries, expandSaved: expandSaved,
    probeServed: probeServed, openServed: openServed, amendServed: amendServed, markRead: markRead,
    effectiveView: effectiveView, fetchRow: fetchRow, parentOf: parentOf, repliesTo: repliesTo
  };
})();
