// curldb viewer: parsing and rendering of one record. No DOM here beyond
// building HTML strings, so the same code runs under node for tests.
// Everything hangs off window.CV; app.js wires it to the page.
(function () {
  var CV = window.CV = window.CV || {};

  var ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return ESC[c]; }); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tsShort(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function dayOf(ts) {
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  function human(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }
  function duration(sec) {
    if (sec < 0) sec = -sec;
    if (sec < 1) return Math.round(sec * 1000) + " ms";
    if (sec < 60) return sec.toFixed(1) + " s";
    if (sec < 3600) return Math.floor(sec / 60) + "m " + Math.round(sec % 60) + "s";
    return Math.floor(sec / 3600) + "h " + Math.round((sec % 3600) / 60) + "m";
  }
  function decode(bytes) { return new TextDecoder().decode(bytes); }

  // ---- bytes ------------------------------------------------------------
  function splitEnvelope(bytes) {
    // Index of the first blank line, found on the bytes so lengths are exact.
    // Returns [headEnd, bodyStart].
    var n = bytes.length;
    for (var i = 0; i + 1 < n; i++) {
      if (bytes[i] === 10 && bytes[i + 1] === 10) return [i, i + 2];
      if (bytes[i] === 13 && bytes[i + 1] === 10 && bytes[i + 2] === 13 && bytes[i + 3] === 10) return [i, i + 4];
    }
    return [n, n];
  }
  function isText(bytes) {
    if (bytes.indexOf(0) >= 0) return false;
    try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); return true; } catch (e) { return false; }
  }
  function startsWith(b, s, at) {
    at = at || 0;
    if (b.length < at + s.length) return false;
    for (var i = 0; i < s.length; i++) if (b[at + i] !== s.charCodeAt(i)) return false;
    return true;
  }
  function sniff(bytes) {
    // Media type from the first bytes, for bodies whose message names none.
    var b = bytes;
    if (b.length < 4) return "";
    if (b[0] === 0x89 && startsWith(b, "PNG", 1)) return "image/png";
    if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
    if (startsWith(b, "GIF8")) return "image/gif";
    if (startsWith(b, "RIFF") && startsWith(b, "WEBP", 8)) return "image/webp";
    if (startsWith(b, "RIFF") && startsWith(b, "WAVE", 8)) return "audio/wav";
    if (startsWith(b, "%PDF")) return "application/pdf";
    if (startsWith(b, "ID3")) return "audio/mpeg";
    if (startsWith(b, "ftyp", 4)) return "video/mp4";
    if (startsWith(b, "OggS")) return "audio/ogg";
    if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return "video/webm";
    if (startsWith(b, "PK") && b[2] === 3 && b[3] === 4) return "application/zip";
    if (b[0] === 0x1f && b[1] === 0x8b) return "application/gzip";
    if (startsWith(b, "SQLite format 3")) return "application/vnd.sqlite3";
    if (isText(b)) {
      var t = decode(b.slice(0, 512)).replace(/^\s+/, "");
      if (/^<(!doctype html|html)/i.test(t)) return "text/html";
      if (/^(<\?xml[^>]*>\s*)?<svg/i.test(t)) return "image/svg+xml";
      if (t.charAt(0) === "<") return "text/xml";
      if (t.charAt(0) === "{" || t.charAt(0) === "[") return "application/json";
      if (/^(diff --git |--- .*\n\+\+\+ |Index: )/.test(t)) return "text/x-diff";
      if (/^(HTTP\/\d(\.\d)? \d{3}|[A-Z]+ \S+ HTTP\/\d)/.test(t)) return "message/http";
    }
    return "";
  }

  // Object URLs handed to <img>/<a>; revoked when the next record renders.
  var urls = [];
  function blobUrl(bytes, type) {
    var u = URL.createObjectURL(new Blob([bytes], { type: type }));
    urls.push(u);
    return u;
  }
  function revokeUrls() {
    urls.forEach(function (u) { URL.revokeObjectURL(u); });
    urls = [];
  }

  function inflate(bytes, encoding) {
    // Content-Encoding gzip/deflate: the stored bytes are compressed; show
    // them decompressed. Resolves null when there is nothing to do.
    if ((encoding !== "gzip" && encoding !== "deflate") || typeof DecompressionStream === "undefined") return Promise.resolve(null);
    return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream(encoding))).arrayBuffer()
      .then(function (buf) { return new Uint8Array(buf); })
      .catch(function () { return null; });
  }

  // A stored HTML page is shown with no scripts (sandbox) and no network:
  // the CSP meta at the top of the document allows inline styles and
  // data: images only, so a page in the archive cannot call out.
  var FRAME_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:";
  function htmlFrame(html) {
    var doc = '<meta http-equiv="Content-Security-Policy" content="' + FRAME_CSP + '">' + html;
    return '<iframe sandbox referrerpolicy="no-referrer" srcdoc="' + esc(doc) + '"></iframe>';
  }

  // ---- text renderers (all output is escaped) ---------------------------
  function prettyJson(text) {
    var v;
    try { v = JSON.parse(text); } catch (e) { return null; }
    var out = JSON.stringify(v, null, 2);
    if (out === undefined) return null;
    var re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
    var html = "", last = 0, m;
    while ((m = re.exec(out))) {
      html += esc(out.slice(last, m.index));
      if (m[1] !== undefined) html += '<span class="' + (m[2] ? "j-k" : "j-s") + '">' + esc(m[1]) + "</span>" + esc(m[2] || "");
      else html += '<span class="j-n">' + esc(m[0]) + "</span>";
      last = re.lastIndex;
    }
    return "<pre>" + html + esc(out.slice(last)) + "</pre>";
  }

  function diffHtml(text) {
    return "<pre>" + text.split("\n").map(function (l) {
      var cls = /^\+\+\+ |^--- /.test(l) ? "d-meta" : l.charAt(0) === "+" ? "d-add" : l.charAt(0) === "-" ? "d-del" :
                l.indexOf("@@") === 0 ? "d-hunk" : /^(diff |index |Index: |=+$)/.test(l) ? "d-meta" : "";
      return cls ? '<span class="' + cls + '">' + esc(l) + "</span>" : esc(l) + "\n";
    }).join("") + "</pre>";
  }

  function parseCsv(text, sep) {
    var rowsOut = [], row = [], cell = "", inq = false;
    for (var i = 0; i < text.length; i++) {
      var c = text.charAt(i);
      if (inq) {
        if (c === '"') { if (text.charAt(i + 1) === '"') { cell += '"'; i++; } else inq = false; }
        else cell += c;
      } else if (c === '"') inq = true;
      else if (c === sep) { row.push(cell); cell = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text.charAt(i + 1) === "\n") i++;
        row.push(cell); rowsOut.push(row); row = []; cell = "";
      } else cell += c;
    }
    if (cell.length || row.length) { row.push(cell); rowsOut.push(row); }
    return rowsOut;
  }
  function tableHtml(rowsIn, headerRow) {
    var html = '<div class="tablewrap"><table>';
    rowsIn.slice(0, 500).forEach(function (r, i) {
      var tag = headerRow && i === 0 ? "th" : "td";
      html += "<tr>" + r.map(function (c) { return "<" + tag + ">" + esc(c) + "</" + tag + ">"; }).join("") + "</tr>";
    });
    html += "</table>" + (rowsIn.length > 500 ? '<span class="note">+' + (rowsIn.length - 500) + " rows</span>" : "") + "</div>";
    return html;
  }

  function hexDump(bytes, limit) {
    var n = Math.min(bytes.length, limit || 4096), lines = [];
    for (var off = 0; off < n; off += 16) {
      var hex = "", asc = "";
      for (var i = 0; i < 16; i++) {
        if (off + i < n) {
          var v = bytes[off + i];
          hex += (v < 16 ? "0" : "") + v.toString(16) + (i === 7 ? "  " : " ");
          asc += v >= 32 && v < 127 ? String.fromCharCode(v) : ".";
        } else hex += "   " + (i === 7 ? " " : "");
      }
      lines.push(("00000000" + off.toString(16)).slice(-8) + "  " + hex + " <b>" + esc(asc) + "</b>");
    }
    if (bytes.length > n) lines.push("... " + (bytes.length - n) + " more bytes");
    return '<pre class="hex">' + lines.join("\n") + "</pre>";
  }

  // Markdown: headings, paragraphs, lists, quotes, fenced code, rules,
  // tables, inline code/bold/italic/links. Images are named, never loaded.
  function inlineMd(s) {
    var parts = s.split(/(`[^`]*`)/);
    return parts.map(function (p, i) {
      if (i % 2) return "<code>" + esc(p.slice(1, -1)) + "</code>";
      var h = esc(p);
      h = h.replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g, function (_, alt, url) { return '<span class="img">[image: ' + (alt || url) + "]</span>"; });
      h = h.replace(/\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g, function (_, text, url) {
        return /^(https?:|mailto:)/i.test(url) ? '<a href="' + url + '" target="_blank" rel="noreferrer noopener">' + text + "</a>" : text + " (" + url + ")";
      });
      h = h.replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, "<strong>$2</strong>");
      h = h.replace(/(^|[^*\w])\*(?=\S)([^*]*?\S)\*(?!\w)/g, "$1<em>$2</em>");
      h = h.replace(/(^|[^_\w])_(?=\S)([^_]*?\S)_(?!\w)/g, "$1<em>$2</em>");
      return h;
    }).join("");
  }
  function renderMarkdown(md) {
    var lines = md.replace(/\r\n/g, "\n").split("\n"), out = [], i = 0, m;
    function para(buf) { if (buf.length) out.push("<p>" + inlineMd(buf.join(" ")) + "</p>"); }
    var buf = [];
    while (i < lines.length) {
      var l = lines[i];
      if (/^```/.test(l)) {
        para(buf); buf = [];
        var code = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
        continue;
      }
      if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { para(buf); buf = []; var n = Math.min(m[1].length, 4); out.push("<h" + n + ">" + inlineMd(m[2]) + "</h" + n + ">"); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { para(buf); buf = []; out.push("<hr>"); i++; continue; }
      if (/^>/.test(l)) {
        para(buf); buf = [];
        var q = [];
        while (i < lines.length && /^>/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, ""));
        out.push("<blockquote>" + renderMarkdown(q.join("\n")) + "</blockquote>");
        continue;
      }
      if (/^\s*[-*+]\s+/.test(l) || /^\s*\d+[.)]\s+/.test(l)) {
        para(buf); buf = [];
        var ordered = /^\s*\d+[.)]\s+/.test(l), items = [];
        while (i < lines.length && (/^\s*[-*+]\s+/.test(lines[i]) || /^\s*\d+[.)]\s+/.test(lines[i]) || /^\s{2,}\S/.test(lines[i]))) {
          var t = lines[i++];
          if (/^\s{2,}\S/.test(t) && items.length && !/^\s*([-*+]|\d+[.)])\s+/.test(t)) items[items.length - 1] += " " + t.trim();
          else items.push(t.replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        }
        out.push((ordered ? "<ol>" : "<ul>") + items.map(function (it) { return "<li>" + inlineMd(it) + "</li>"; }).join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        para(buf); buf = [];
        var trs = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          var cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
          if (!/^:?-{2,}/.test(cells[0])) trs.push(cells);
          i++;
        }
        out.push("<table>" + trs.map(function (r, k) { var tag = k === 0 ? "th" : "td"; return "<tr>" + r.map(function (c) { return "<" + tag + ">" + inlineMd(c) + "</" + tag + ">"; }).join("") + "</tr>"; }).join("") + "</table>");
        continue;
      }
      if (!l.trim()) { para(buf); buf = []; i++; continue; }
      buf.push(l.trim()); i++;
    }
    para(buf);
    return out.join("\n");
  }

  function frontMatter(text) {
    // Returns {meta: [[name, value]...], body} for a --- block, else null.
    var lines = text.replace(/\r\n/g, "\n").split("\n");
    if (lines[0].trim() !== "---") return null;
    var end = lines.indexOf("---", 1);
    if (end < 0) return null;
    var meta = [], last = null, m;
    lines.slice(1, end).forEach(function (l) {
      if ((m = l.match(/^\s*-\s+(.*)$/)) && last) meta.push([last, m[1].trim()]);
      else if ((m = l.match(/^(\s*)([^:]+):\s*(.*)$/))) { last = m[2].trim(); if (m[3].trim()) meta.push([last, m[3].trim()]); }
      else if (last && l.trim() && meta.length && meta[meta.length - 1][0] === last) meta[meta.length - 1][1] += " " + l.trim();
    });
    return { meta: meta, body: lines.slice(end + 1).join("\n") };
  }

  function parseMultipart(bytes, boundary) {
    // Split a multipart body into parts: [{headers, headText, body: Uint8Array}].
    var text = new TextDecoder("latin1").decode(bytes), delim = "--" + boundary, parts = [], pos = text.indexOf(delim);
    while (pos >= 0) {
      var start = pos + delim.length;
      if (text.substr(start, 2) === "--") break;
      var next = text.indexOf(delim, start);
      var chunk = text.slice(start, next < 0 ? text.length : next).replace(/^\r?\n/, "").replace(/\r?\n$/, "");
      var cut = chunk.search(/\r?\n\r?\n/);
      var headText = cut < 0 ? "" : chunk.slice(0, cut);
      var bodyStart = cut < 0 ? 0 : cut + (chunk.substr(cut, 4) === "\r\n\r\n" ? 4 : 2);
      var headers = {};
      headText.split(/\r?\n/).forEach(function (l) { var i = l.indexOf(":"); if (i > 0) headers[l.slice(0, i).trim().toLowerCase()] = l.slice(i + 1).trim(); });
      var bodyLatin = chunk.slice(bodyStart), body = new Uint8Array(bodyLatin.length);
      for (var i = 0; i < bodyLatin.length; i++) body[i] = bodyLatin.charCodeAt(i);
      parts.push({ headers: headers, headText: headText, body: body });
      if (next < 0) break;
      pos = next;
    }
    return parts;
  }

  // Link: </12>; rel="parent" names the record this one answers. The target
  // is either a record id (/12) or an address matched against another
  // record's Content-Location (the pi extension writes /chat/<id>).
  var LINK_PARENT = /<\s*([^>]+?)\s*>\s*;\s*rel\s*=\s*"?parent"?/i;
  function parseLinkTarget(value) {
    var m = value ? LINK_PARENT.exec(value) : null;
    return m ? m[1] : null;
  }
  function parseLinkParent(value) {
    var t = parseLinkTarget(value);
    return t && /^\/?\d+$/.test(t) ? Number(t.replace("/", "")) : null;
  }

  function headLines(bytes, kindHint) {
    // Parse an HTTP head from bytes: {first, method, path, html, headers,
    // list, bodyStart}, or null when the bytes do not start with a status
    // line or a request line.
    var cut = splitEnvelope(bytes);
    var text = decode(bytes.slice(0, cut[0])), lines = text.split(/\r?\n/), headers = {}, list = [];
    var first = lines[0] || "";
    var isResp = /^HTTP\/\d(\.\d)? \d{3}/.test(first), isReq = /^[A-Z]+ \S+ HTTP\/\d/.test(first);
    if (!isResp && !isReq && kindHint !== "request" && kindHint !== "response") return null;
    var code = isResp ? Number(first.split(" ")[1]) : 0;
    var html = '<span class="start' + (code >= 400 ? " err" : "") + '">' + esc(first) + "</span>\n";
    lines.slice(1).forEach(function (l) {
      var i = l.indexOf(":");
      if (i > 0) {
        var name = l.slice(0, i).trim(), value = l.slice(i + 1).trim();
        headers[name.toLowerCase()] = value;
        list.push([name, value]);
        html += '<span class="hn">' + esc(l.slice(0, i + 1)) + "</span>" + esc(l.slice(i + 1)) + "\n";
      } else html += esc(l) + "\n";
    });
    var words = first.split(" ");
    return { first: first, method: isReq ? words[0] : null, path: isReq ? words[1] : null, status: code || null,
             html: html, headers: headers, list: list, bodyStart: cut[1] };
  }

  // A stored request as a curl command line. Hop-by-hop and framing
  // headers are left to curl; a binary body points at a saved file.
  function shellQuote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }
  function curlCommand(head, bodyBytes) {
    if (!head || !head.method) return null;
    var host = head.headers.host || "localhost:200";
    var url = /^[a-z]+:\/\//i.test(head.path) ? head.path : "http://" + host + head.path;
    var args = ["curl", "-X", head.method, shellQuote(url)];
    head.list.forEach(function (kv) {
      var n = kv[0].toLowerCase();
      if (n === "host" || n === "content-length" || n === "transfer-encoding" || n === "connection" || n === "expect") return;
      args.push("-H", shellQuote(kv[0] + ": " + kv[1]));
    });
    if (bodyBytes && bodyBytes.length) {
      if (isText(bodyBytes)) args.push("--data-binary", shellQuote(decode(bodyBytes)));
      else args.push("--data-binary", "@body.bin");
    }
    var out = [], line = "";
    args.forEach(function (a, i) {
      var sep = i === 0 ? "" : (a === "-H" || a === "--data-binary" ? " \\\n  " : " ");
      line += sep + a;
    });
    out.push(line);
    return out.join("");
  }

  // renderBody: Promise of HTML for a body, by media type. Text falls back
  // to an escaped <pre>. depth limits nested message/http and multipart.
  function renderBody(bytes, type, encoding, depth) {
    depth = depth || 0;
    return inflate(bytes, encoding).then(function (inflated) {
      var b = inflated || bytes;
      if (!b.length) return '<span class="note">(empty body)</span>';
      var full = (type || "").trim(), main = full.split(";")[0].trim().toLowerCase();
      if (!main) { main = sniff(b); full = main; }
      var text = isText(b), t = text ? decode(b) : "";
      if (main.indexOf("image/") === 0 && (main !== "image/svg+xml" || text)) return '<img src="' + blobUrl(b, main) + '" alt="">';
      if (main.indexOf("audio/") === 0) return '<audio controls src="' + blobUrl(b, main) + '"></audio>';
      if (main.indexOf("video/") === 0) return '<video controls src="' + blobUrl(b, main) + '"></video>';
      if (main === "application/pdf") return '<embed type="application/pdf" src="' + blobUrl(b, main) + '">';
      if (main === "text/html" && text) return htmlFrame(t) + "<details><summary>source</summary><pre>" + esc(t) + "</pre></details>";
      if (main === "message/http" && depth < 4) {
        var head = headLines(b);
        if (head) {
          return renderBody(b.slice(head.bodyStart), head.headers["content-type"], (head.headers["content-encoding"] || "").toLowerCase(), depth + 1)
            .then(function (inner) { return '<div class="nested"><pre>' + head.html + '</pre><div class="media">' + inner + "</div></div>"; });
        }
      }
      if (main.indexOf("multipart/") === 0 && depth < 4) {
        var bm = full.match(/boundary="?([^";]+)"?/i);
        if (bm) {
          var parts = parseMultipart(b, bm[1]);
          return Promise.all(parts.map(function (p) {
            return renderBody(p.body, p.headers["content-type"], "", depth + 1).then(function (inner) {
              return '<div class="part"><div class="ph">' + esc(p.headText).replace(/\n/g, "<br>") + '</div><div class="media">' + inner + "</div></div>";
            });
          })).then(function (all) { return all.join("") || '<span class="note">(no parts)</span>'; });
        }
      }
      if ((main === "application/json" || /\+json$/.test(main)) && text) { var pj = prettyJson(t); if (pj) return pj; }
      if ((main === "text/markdown" || main === "text/x-markdown") && text) return '<div class="md">' + renderMarkdown(t) + "</div>";
      if ((main === "text/csv" || main === "text/tab-separated-values") && text) return tableHtml(parseCsv(t, main === "text/csv" ? "," : "\t"), true);
      if (main === "application/x-www-form-urlencoded" && text) {
        var kv = []; new URLSearchParams(t).forEach(function (v, k) { kv.push([k, v]); });
        return tableHtml([["name", "value"]].concat(kv), true);
      }
      if ((main === "text/x-diff" || main === "text/x-patch") && text) return diffHtml(t);
      if (text) return "<pre>" + esc(t.replace(/\r\n/g, "\n")) + "</pre>";
      return '<span class="note">' + human(b.length) + (main ? ", " + esc(main) : ", binary") + "</span>" + hexDump(b, 512);
    });
  }

  // recordParts: everything showRecord needs about one row, computed once.
  function recordParts(r) {
    var bytes = typeof r.raw === "string" ? new TextEncoder().encode(r.raw) : r.raw;
    var text = isText(bytes);
    var p = { row: r, bytes: bytes, text: text, headHtml: "", headers: {}, head: null, bodyBytes: bytes,
              name: String(r.path || "").split("/").pop() || ("record-" + r.id) };
    if (r.kind === "request" || r.kind === "response") {
      var head = headLines(bytes, r.kind);
      if (head) { p.head = head; p.headHtml = head.html + "\n"; p.headers = head.headers; p.bodyBytes = bytes.slice(head.bodyStart); }
    } else if (r.kind === "note" && text) {
      var fm = frontMatter(decode(bytes));
      if (fm) {
        p.headHtml = fm.meta.map(function (kv) { return '<span class="hn">' + esc(kv[0]) + ":</span> " + esc(kv[1]) + "\n"; }).join("") + "\n";
        p.bodyBytes = new TextEncoder().encode(fm.body.replace(/^\n+/, ""));
      }
    }
    p.type = p.headers["content-type"] || (r.kind === "note" ? "text/markdown" : sniff(p.bodyBytes));
    p.encoding = (p.headers["content-encoding"] || "").trim().toLowerCase();
    p.bodyType = p.type.split(";")[0].trim().toLowerCase();
    p.media = /^(image|audio|video)\//.test(p.bodyType) || p.bodyType === "application/pdf";
    p.parentInMessage = parseLinkParent(p.headers.link);
    p.parentTarget = parseLinkTarget(p.headers.link);
    p.address = p.headers["content-location"] || null;
    return p;
  }

  // renderRecord: Promise of the head + body HTML for one record, in the
  // rendered or the source view.
  function renderRecord(p, mode) {
    if (mode === "source") {
      return Promise.resolve(p.text
        ? "<pre>" + esc(decode(p.bytes).replace(/\r\n/g, "\n")) + "</pre>"
        : "<pre>" + p.headHtml + "</pre>" + hexDump(p.bodyBytes, 65536));
    }
    return renderBody(p.bodyBytes, p.type, p.encoding, 0).then(function (bodyHtml) {
      var plain = bodyHtml.indexOf("<pre>") === 0 && bodyHtml.indexOf("<pre>", 5) < 0 && bodyHtml.indexOf("<pre class") < 0;
      return plain ? "<pre>" + p.headHtml + bodyHtml.slice(5) : (p.headHtml ? "<pre>" + p.headHtml + "</pre>" : "") + '<div class="media">' + bodyHtml + "</div>";
    });
  }

  CV.esc = esc; CV.tsShort = tsShort; CV.dayOf = dayOf; CV.human = human; CV.duration = duration; CV.decode = decode;
  CV.splitEnvelope = splitEnvelope; CV.isText = isText; CV.sniff = sniff;
  CV.blobUrl = blobUrl; CV.revokeUrls = revokeUrls; CV.inflate = inflate; CV.htmlFrame = htmlFrame;
  CV.prettyJson = prettyJson; CV.diffHtml = diffHtml; CV.parseCsv = parseCsv; CV.tableHtml = tableHtml; CV.hexDump = hexDump;
  CV.renderMarkdown = renderMarkdown; CV.frontMatter = frontMatter; CV.parseMultipart = parseMultipart;
  CV.parseLinkParent = parseLinkParent; CV.parseLinkTarget = parseLinkTarget; CV.headLines = headLines; CV.curlCommand = curlCommand;
  CV.renderBody = renderBody; CV.recordParts = recordParts; CV.renderRecord = renderRecord;
})();
