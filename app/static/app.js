/* Pin-on-Expand — client. Drives /api/run and renders the reconciliation. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var fmt = function (n) { return Math.round(n).toLocaleString("en-US"); };
  var pct = function (v) { return (v * 100).toFixed(1) + "%"; };
  var signed = function (v) {
    return (v < 0 ? "−" : "+") + Math.abs(v * 100).toFixed(1) + "%";
  };

  var state = { turns: 3, expands: true, running: false };

  /* ── theme ─────────────────────────────────────────── */

  var stored = null;
  try { stored = localStorage.getItem("poe-theme"); } catch (e) { /* private mode */ }
  if (!stored) {
    stored = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", stored);

  $("theme-toggle").addEventListener("click", function () {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("poe-theme", next); } catch (e) { /* ignore */ }
  });

  /* ── backend badge ─────────────────────────────────── */

  fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
    var pill = $("backend-pill");
    if (h.hosted_gpu) {
      pill.className = "pill live";
      $("backend-label").textContent = "paritok hosted GPU";
      pill.title = "Compression runs on Paritok's hosted 4B model";
    } else {
      pill.className = "pill mock";
      $("backend-label").textContent = "mock compressor";
      pill.title = "No PARITOK_API_KEY set — compression is a deterministic stand-in. " +
        "The proxies, the resolve loop and the token accounting are all real.";
    }
  }).catch(function () {
    $("backend-label").textContent = "offline";
  });

  /* ── token estimate (display only; server counts authoritatively) ── */

  function estimateTokens(text) {
    if (!text) return 0;
    // Rough stand-in for tiktoken so the field can update as you type.
    return Math.round(text.length / 3.6);
  }

  var sourceEl = $("source");
  function refreshCount() {
    $("tok-count").textContent = "~" + fmt(estimateTokens(sourceEl.value));
  }
  sourceEl.addEventListener("input", refreshCount);

  /* ── samples ───────────────────────────────────────── */

  var SAMPLES = {
    argparse: { path: "/api/sample/argparse", name: "argparse.py" },
    server: { path: "/api/sample/server", name: "server.py" }
  };

  Array.prototype.forEach.call(document.querySelectorAll("[data-sample]"), function (btn) {
    btn.addEventListener("click", function () {
      var s = SAMPLES[btn.getAttribute("data-sample")];
      btn.disabled = true;
      fetch(s.path)
        .then(function (r) {
          if (!r.ok) throw new Error("sample unavailable");
          return r.text();
        })
        .then(function (text) {
          sourceEl.value = text;
          $("filename").value = s.name;
          refreshCount();
        })
        .catch(function () { showError("Could not load that sample on this host."); })
        .finally(function () { btn.disabled = false; });
    });
  });

  $("clear-btn").addEventListener("click", function () {
    sourceEl.value = "";
    refreshCount();
  });

  /* ── controls ──────────────────────────────────────── */

  $("turns").addEventListener("input", function () {
    state.turns = parseInt(this.value, 10);
    $("turns-val").textContent = state.turns;
  });

  var segBtns = document.querySelectorAll("[data-expands]");
  Array.prototype.forEach.call(segBtns, function (btn) {
    btn.addEventListener("click", function () {
      Array.prototype.forEach.call(segBtns, function (b) {
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      });
      state.expands = btn.getAttribute("data-expands") === "1";
    });
  });

  /* ── stages ────────────────────────────────────────── */

  var STAGES = ["boot", "stock", "pinned", "done"];

  function setStage(name) {
    var idx = STAGES.indexOf(name);
    STAGES.forEach(function (s, i) {
      var li = document.querySelector('[data-stage="' + s + '"]');
      li.setAttribute("data-state", i < idx ? "done" : (i === idx ? "active" : ""));
    });
  }

  function clearStages() {
    STAGES.forEach(function (s) {
      document.querySelector('[data-stage="' + s + '"]').removeAttribute("data-state");
    });
  }

  function showError(msg) {
    var el = $("err");
    el.textContent = msg;
    el.hidden = false;
  }

  /* ── run ───────────────────────────────────────────── */

  $("run-btn").addEventListener("click", function () {
    if (state.running) return;
    var source = sourceEl.value.trim();
    if (!source) { showError("Paste a source file first."); return; }

    state.running = true;
    $("err").hidden = true;
    $("run-btn").disabled = true;
    $("stages").classList.add("on");
    document.querySelector(".panel-results").setAttribute("aria-busy", "true");
    clearStages();
    setStage("boot");

    // Stages come from the server's own job state, not a timer: a run on a
    // small instance can take minutes, and a faked progress bar that finishes
    // before the work does is worse than none.
    fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: source,
        turns: state.turns,
        expands: state.expands,
        filename: $("filename").value || "pasted.py"
      })
    })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || ("could not start (" + r.status + ")"));
          return body.job_id;
        });
      })
      .then(poll)
      .then(function (data) {
        setStage("done");
        render(data);
        setTimeout(function () { $("stages").classList.remove("on"); }, 700);
      })
      .catch(function (e) {
        clearStages();
        $("stages").classList.remove("on");
        showError(e.message || "Something went wrong running the session.");
      })
      .finally(function () {
        state.running = false;
        $("run-btn").disabled = false;
        document.querySelector(".panel-results").setAttribute("aria-busy", "false");
      });
  });

  function poll(jobId) {
    return new Promise(function (resolve, reject) {
      var tries = 0;
      (function tick() {
        tries++;
        // Generous ceiling: ~10 minutes at 2s. A constrained host is slow, not
        // broken, and giving up early would misreport it as a failure.
        if (tries > 300) { reject(new Error("run timed out")); return; }
        fetch("/api/run/" + jobId)
          .then(function (r) {
            if (!r.ok) throw new Error("lost track of the run (" + r.status + ")");
            return r.json();
          })
          .then(function (j) {
            if (j.stage) setStage(j.stage);
            if (j.detail) setDetail(j.stage, j.detail, j.elapsed);
            if (j.status === "done") { resolve(j.result); return; }
            if (j.status === "error") { reject(new Error(j.error)); return; }
            setTimeout(tick, 2000);
          })
          .catch(reject);
      })();
    });
  }

  function setDetail(stage, detail, elapsed) {
    var li = document.querySelector('[data-stage="' + stage + '"]');
    if (!li) return;
    var note = li.querySelector(".stage-note");
    if (!note) {
      note = document.createElement("span");
      note.className = "stage-note";
      li.appendChild(note);
    }
    note.textContent = " — " + detail + (elapsed ? " (" + elapsed + "s)" : "");
  }

  /* ── render ────────────────────────────────────────── */

  function render(d) {
    $("empty").hidden = true;
    $("results").hidden = false;

    var stock = d.stock, pinned = d.pinned;

    $("r-reported").textContent = pct(stock.reported_saving);
    $("r-actual").textContent = signed(stock.vs_no_proxy);
    $("r-actual-note").textContent = stock.vs_no_proxy < 0
      ? "i.e. MORE than sending it uncompressed"
      : "against sending it uncompressed";

    // verdict
    var v = $("verdict");
    if (stock.vs_no_proxy < 0) {
      v.innerHTML = "Stock Paritok reported saving <b class='good'>" +
        pct(stock.reported_saving) + "</b> on a session that actually billed <b>" +
        fmt(Math.abs(stock.billed - d.no_proxy_tokens)) +
        " more tokens</b> than sending the file with no compression at all. " +
        "<b class='good'>" + fmt(stock.uncounted) + "</b> tokens were billed but never counted.";
    } else {
      v.innerHTML = "Stock Paritok saved <b class='good'>" + pct(stock.reported_saving) +
        "</b> as reported, and " + fmt(stock.uncounted) +
        " tokens went billed-but-uncounted. With no expansion in this session, " +
        "the reported figure and the bill agree.";
    }

    // ledger — stock variant, per POST
    var ledger = $("ledger");
    ledger.innerHTML = "";
    var maxPost = Math.max.apply(null, stock.posts.map(function (p) { return p.input_tokens; }).concat([1]));

    ledger.appendChild(row("head", "Request", "What went upstream", "Tokens", null));
    stock.posts.forEach(function (p) {
      var counted = !p.carries_expanded;
      var what = counted
        ? "Turn " + p.turn + " — compressed request <span class='tag ok'>counted</span>"
        : "Turn " + p.turn + " — carries the full expanded original <span class='tag warn'>not counted</span>";
      ledger.appendChild(row(counted ? "counted" : "", "POST " + p.index, what,
        fmt(p.input_tokens), p.input_tokens / maxPost, !counted));
    });
    ledger.appendChild(row("total", "Billed", "Total the provider charged for",
      fmt(stock.billed), null, true));
    ledger.appendChild(row("", "Reported", "<code>input_tokens_compressed</code> from <code>/stats</code>",
      fmt(stock.reported_compressed), null, false));

    $("ledger-meta").textContent = stock.post_count + " POSTs · " + d.turns +
      (d.turns === 1 ? " turn" : " turns");

    // bars
    var maxBar = Math.max(stock.billed, pinned.billed, d.no_proxy_tokens) * 1.06;
    var bars = $("bars");
    bars.innerHTML = "";
    bars.appendChild(barRow("stock Paritok", stock.billed, maxBar, "stock", stock.post_count));
    bars.appendChild(barRow("pin-on-expand", pinned.billed, maxBar, "pinned", pinned.post_count));
    bars.appendChild(baselineRow(d.no_proxy_tokens, maxBar));

    // stats
    var sg = $("statgrid");
    sg.innerHTML = "";
    sg.appendChild(stat("Billed — stock", fmt(stock.billed), "bill"));
    sg.appendChild(stat("Billed — pinned", fmt(pinned.billed), "rep"));
    sg.appendChild(stat("Saved by pinning",
      d.delta.billed_saved > 0 ? pct(d.delta.billed_saved_pct) : "0.0%", "rep"));
    sg.appendChild(stat("Round-trips removed", String(d.delta.posts_saved), "rep"));
    sg.appendChild(stat("Billed but uncounted", fmt(stock.uncounted), "bill"));
    sg.appendChild(stat("File size", fmt(d.source_tokens) + " tok", ""));

    $("caveat").innerHTML = d.compressor === "paritok-hosted-gpu"
      ? "Compression ran on <b>Paritok's hosted 4B GPU</b>. The provider is mocked so " +
        "the expansion can be scripted and every POST counted exactly — a real provider " +
        "reports neither. Prompt caching is not modelled, so the overspend is an upper bound."
      : "No <code>PARITOK_API_KEY</code> is set, so compression used a deterministic " +
        "stand-in rather than the 4B model — the proxies, the resolve loop and the token " +
        "accounting are all real. Prompt caching is not modelled, so the overspend is an " +
        "upper bound.";
  }

  function row(cls, id, what, n, fill, emphasise) {
    var el = document.createElement("div");
    el.className = "lrow " + cls;
    if (fill != null) {
      var f = document.createElement("div");
      f.className = "lrow-fill";
      f.style.width = (fill * 100).toFixed(1) + "%";
      el.appendChild(f);
    }
    var a = document.createElement("div"); a.className = "l-id"; a.textContent = id;
    var b = document.createElement("div"); b.className = "l-what"; b.innerHTML = what;
    var c = document.createElement("div");
    c.className = "l-n" + (emphasise ? " em" : "");
    c.textContent = n;
    el.appendChild(a); el.appendChild(b); el.appendChild(c);
    return el;
  }

  function barRow(label, value, max, cls, posts) {
    var row = document.createElement("div");
    row.className = "barrow";

    var l = document.createElement("div");
    l.className = "barrow-label";
    l.textContent = label;

    var track = document.createElement("div");
    track.className = "bartrack";

    var w = Math.max(1, (value / max) * 100);
    var fill = document.createElement("div");
    fill.className = "barfill " + cls + (w < 22 ? " outside" : "");
    fill.style.width = w + "%";
    var span = document.createElement("span");
    span.textContent = fmt(value);
    fill.appendChild(span);
    fill.title = label + " — " + fmt(value) + " tokens across " + posts + " POSTs";

    track.appendChild(fill);
    row.appendChild(l); row.appendChild(track);
    return row;
  }

  function baselineRow(value, max) {
    var row = document.createElement("div");
    row.className = "barrow";
    var l = document.createElement("div");
    l.className = "barrow-label";
    l.textContent = "no proxy at all";
    var track = document.createElement("div");
    track.className = "bartrack";
    track.style.background = "transparent";
    var mark = document.createElement("div");
    mark.className = "baseline";
    mark.style.left = ((value / max) * 100) + "%";
    mark.title = "no proxy — " + fmt(value) + " tokens";
    track.appendChild(mark);
    var lab = document.createElement("div");
    lab.style.cssText = "position:absolute;left:0;top:9px;font-family:var(--mono);" +
      "font-size:12px;color:var(--base-mark);font-variant-numeric:tabular-nums";
    lab.textContent = fmt(value);
    track.appendChild(lab);
    row.appendChild(l); row.appendChild(track);
    return row;
  }

  function stat(key, val, cls) {
    var d = document.createElement("div");
    d.className = "stat";
    var dt = document.createElement("dt"); dt.textContent = key;
    var dd = document.createElement("dd"); dd.className = cls; dd.textContent = val;
    d.appendChild(dt); d.appendChild(dd);
    return d;
  }

  refreshCount();
})();
