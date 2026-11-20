/* YBM replay stage. Every frame is a pure function of time t (seconds), so the same code records video frame by frame
   (mode=record, window.__seek) and plays in the browser (mode=web). Messages, file names, numbers and real-time stamps come
   from stories.js, which build_data.py writes from saved runs. */
(() => {
  "use strict";
  const STORIES = window.YBM_STORIES;
  const q = new URLSearchParams(location.search);
  const MODE = q.get("mode") === "record" ? "record" : "web";
  const MARK = "ybm-mark.svg";
  const RUN_SRC = "Replay of a real run · Qwen3.5 9B, 4-bit, llama.cpp · 8 GB laptop GPU · November 19, 2026";

  // ---------- small helpers ----------
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const lerp = (a, b, p) => a + (b - a) * p;
  const easeOut = (p) => 1 - Math.pow(1 - p, 3);
  const easeInOut = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
  const prog = (t, at, dur) => clamp((t - at) / dur);
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  function el(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }
  function show(node, on, as) { node.style.display = on ? (as || "") : "none"; }
  function fmt(sec) { const s = Math.max(0, Math.floor(sec)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
  // Markdown as the chat shows it: **bold**, "- " or "* " bullets, `code`, links.
  function md(text) {
    const lines = esc(text).split("\n");
    let html = "", inList = false;
    for (const raw of lines) {
      const line = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<b>$1</b>")
        .replace(/(https?:\/\/[^\s)]+?)(\.?)(\s|$)/g, '<span class="link">$1</span>$2$3');
      const m = /^\s*[-*]\s+(.*)$/.exec(line);
      if (m) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${m[1]}</li>`; continue; }
      if (inList) { html += "</ul>"; inList = false; }
      if (line.trim()) html += (html && !html.endsWith("</ul>") ? "<br>" : "") + line;
    }
    if (inList) html += "</ul>";
    return html;
  }

  // Time warp: knots [[videoSecond, realSecond], ...]. The clock shows real time; the badge shows how much faster it plays.
  function warp(knots) {
    return {
      real(v) {
        if (v <= knots[0][0]) return knots[0][1];
        for (let i = 1; i < knots.length; i++) {
          const [v0, r0] = knots[i - 1], [v1, r1] = knots[i];
          if (v <= v1) return lerp(r0, r1, (v - v0) / (v1 - v0));
        }
        const [vl, rl] = knots[knots.length - 1];
        return rl + (v - vl);
      },
      video(r) {
        for (let i = 1; i < knots.length; i++) {
          const [v0, r0] = knots[i - 1], [v1, r1] = knots[i];
          if (r <= r1) return lerp(v0, v1, (r - r0) / (r1 - r0));
        }
        const [vl, rl] = knots[knots.length - 1];
        return vl + (r - rl);
      },
      speed(v) {
        for (let i = 1; i < knots.length; i++) {
          const [v0, r0] = knots[i - 1], [v1, r1] = knots[i];
          if (v < v1) return v < v0 ? 1 : (r1 - r0) / (v1 - v0);
        }
        return 1;
      },
    };
  }

  // ---------- icons ----------
  const TYPE_COLOR = { pdf: "#f0616d", doc: "#4c8df6", docx: "#4c8df6", txt: "#9aa4bc", md: "#9aa4bc", xlsx: "#34c77b", csv: "#34c77b",
    zip: "#f5b44a", "7z": "#f5b44a", mp3: "#ec6fcf", mp4: "#a77bf3", mov: "#a77bf3", jpg: "#38c6d9", png: "#38c6d9", svg: "#38c6d9",
    exe: "#8f99b3", msi: "#8f99b3", html: "#e8794a" };
  const FOLDER_COLOR = { archives: "#f5b44a", audio: "#ec6fcf", documents: "#4c8df6", images: "#38c6d9", other: "#8f99b3",
    spreadsheets: "#34c77b", video: "#a77bf3" };
  function fileIcon(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    const c = TYPE_COLOR[ext] || "#8f99b3";
    return `<svg viewBox="0 0 58 70"><path d="M6 2h32l14 14v48a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4z" fill="#1d2742" stroke="rgba(255,255,255,.16)" stroke-width="1.5"/>
      <path d="M38 2v10a4 4 0 0 0 4 4h10" fill="#28345a" stroke="rgba(255,255,255,.16)" stroke-width="1.5"/>
      <rect x="2" y="40" width="40" height="18" rx="4" fill="${c}"/>
      <text x="22" y="53.5" font-family="Geist" font-weight="600" font-size="11" text-anchor="middle" fill="#0b1120">${esc(ext.toUpperCase())}</text></svg>`;
  }
  function folderIcon(color, w = 92, h = 74) {
    return `<svg viewBox="0 0 92 74" width="${w}" height="${h}"><path d="M4 12a6 6 0 0 1 6-6h22l8 8h42a6 6 0 0 1 6 6v6H4z" fill="${color}" opacity=".55"/>
      <rect x="4" y="20" width="84" height="50" rx="7" fill="${color}"/><rect x="4" y="20" width="84" height="50" rx="7" fill="url(#fsh)"/>
      <defs><linearGradient id="fsh" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#000" stop-opacity=".1"/></linearGradient></defs></svg>`;
  }
  const SEND = `<svg viewBox="0 0 24 24"><path d="M4 12l16-8-6 16-2.5-6.5z" fill="#fff"/></svg>`;
  const CURSOR = `<svg viewBox="0 0 30 38"><path d="M3 2l22 20-10 1.5 6 11-4.5 2-6-11L3 32z" fill="#fff" stroke="#111" stroke-width="2" stroke-linejoin="round"/></svg>`;
  const CHECK = `<svg viewBox="0 0 24 24" width="22" height="22"><circle cx="12" cy="12" r="11" fill="rgba(61,214,140,.18)"/><path d="M7 12.5l3.2 3.2L17 9" fill="none" stroke="#3dd68c" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

  // ---------- frame parts ----------
  function topbar(stage, chipText) {
    const bar = el("div", "topbar");
    bar.innerHTML = `<div class="brand"><img src="${MARK}" alt="">YBM</div>${chipText ? `<span class="chip">${chipText}</span>` : ""}<div class="spacer"></div>
      <div class="clock"><span class="rt">REAL TIME</span><b>0:00</b><span class="speed">4×</span></div>`;
    stage.appendChild(bar);
    return { bar, clock: bar.querySelector(".clock"), b: bar.querySelector(".clock b"), rt: bar.querySelector(".rt"), speed: bar.querySelector(".speed") };
  }

  function phone(stage) {
    const p = el("div", "phone");
    p.innerHTML = `<div class="screen"><div class="notch"></div>
      <div class="status"><span>10:13</span><span>●●●● 5G ▇</span></div>
      <div class="chat-head"><div class="avatar"><img src="${MARK}" alt=""></div><div><div class="who">YBM</div>
        <div class="sub"><span class="dot"></span>on your computer · local model</div></div></div>
      <div class="thread"><div class="thread-inner"></div></div>
      <div class="composer"><div class="field empty">Message YBM</div><div class="send">${SEND}</div></div></div>`;
    stage.appendChild(p);
    return { root: p, thread: p.querySelector(".thread"), inner: p.querySelector(".thread-inner"), field: p.querySelector(".field"), items: [] };
  }

  // A chat item: {kind: user|bot|approval, at, versions: [{at, html, progress}], tapAt}
  function addItem(ph, item) {
    let node;
    if (item.kind === "approval") {
      node = el("div", "approval", `<div class="label">APPROVAL NEEDED</div><div class="txt">${esc(item.text)}</div>
        <div class="btns"><div class="btn deny">Deny</div><div class="btn ok">Approve once<span class="ripple"></span></div></div>
        <div class="done">✓ Approved once</div>`);
      item.btns = node.querySelector(".btns"); item.done = node.querySelector(".done"); item.ok = node.querySelector(".ok");
      item.ripple = node.querySelector(".ripple");
    } else {
      node = el("div", `msg ${item.kind}`);
    }
    node.style.display = "none";
    ph.inner.appendChild(node);
    item.node = node; item.shown = -1;
    ph.items.push(item);
    return item;
  }

  function renderPhone(ph, t, compose) {
    // composer: the ask typed out, then sent
    if (compose) {
      const { text, from, to, sendAt } = compose;
      if (t < sendAt) {
        const n = Math.round(text.length * easeInOut(prog(t, from, to - from)));
        const caret = t < to + 0.1 || Math.floor(t * 2.4) % 2 === 0;
        ph.field.classList.toggle("empty", n === 0 && t < from);
        ph.field.innerHTML = n === 0 && t < from ? "Message YBM" : esc(text.slice(0, n)) + (caret ? '<span class="caret"></span>' : "");
      } else {
        ph.field.classList.add("empty");
        ph.field.textContent = "Message YBM";
      }
    }
    let used = 0;
    const viewport = ph.thread.clientHeight;
    for (const it of ph.items) {
      const on = t >= it.at;
      show(it.node, on);
      if (!on) continue;
      const p = easeOut(prog(t, it.at, 0.35));
      it.node.style.opacity = p;
      it.node.style.transform = `translateY(${(1 - p) * 16}px) scale(${0.96 + 0.04 * p})`;
      if (it.versions) {
        let idx = 0;
        for (let i = 0; i < it.versions.length; i++) if (t >= it.versions[i].at) idx = i;
        const v = it.versions[idx];
        const done = it.doneAt !== undefined && t >= it.doneAt;
        const key = idx + (done ? "d" : "");
        if (it.shown !== key) {
          it.shown = key;
          it.node.classList.toggle("progress", !!v.progress);
          it.node.innerHTML = (v.progress && !done ? '<span class="spin"></span>' : "") + v.html;
          it.spin = it.node.querySelector(".spin");
        }
        if (idx > 0) it.node.style.opacity = p * (0.5 + 0.5 * easeOut(prog(t, v.at, 0.35)));
        if (it.spin) it.spin.style.transform = `rotate(${(t * 420) % 360}deg)`;
      }
      if (it.kind === "approval") {
        const tapped = it.tapAt !== undefined && t >= it.tapAt;
        const settled = tapped && t >= it.tapAt + 0.45;
        show(it.btns, !settled, "flex"); show(it.done, settled, "block");
        const rp = tapped ? prog(t, it.tapAt, 0.45) : 0;
        it.ripple.style.opacity = tapped && !settled ? 1 - rp : 0;
        it.ripple.style.transform = `scale(${1 + rp * 9})`;
        const pulse = !tapped ? 0.5 + 0.5 * Math.sin((t - it.at) * 5) : 0;
        it.node.style.boxShadow = `0 0 0 ${Math.round(pulse * 6)}px rgba(245,180,74,${0.18 * pulse})`;
      }
      const h = it.node.offsetHeight + 12;
      used += h * (t - it.at < 0.35 ? p : 1);
    }
    const offset = Math.max(0, used + 24 - viewport);
    ph.inner.style.transform = `translateY(${-offset}px)`;
  }

  function desk(stage, label) {
    const d = el("div", "desk");
    stage.appendChild(d);
    return d;
  }
  function win(parent, cls, x, y, w, h, title) {
    const node = el("div", `win ${cls || ""}`);
    Object.assign(node.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
    node.innerHTML = `<div class="title"><div class="lights"><i></i><i></i><i></i></div>${title}</div><div class="body"></div>`;
    parent.appendChild(node);
    return { node, body: node.querySelector(".body"), title: node.querySelector(".title") };
  }

  function caption(stage, beats) {
    const c = el("div", "caption", `<div class="cap"><div class="main"></div><div class="src"></div></div>`);
    stage.appendChild(c);
    if (q.get("nocap")) { c.style.display = "none"; return () => {}; }
    const cap = c.querySelector(".cap"), main = c.querySelector(".main"), src = c.querySelector(".src");
    let current = -1;
    return (t) => {
      let idx = -1;
      for (let i = 0; i < beats.length; i++) if (t >= beats[i][0]) idx = i;
      if (idx < 0) { cap.style.opacity = 0; return; }
      if (idx !== current) { current = idx; main.textContent = beats[idx][1]; src.textContent = beats[idx][2] || ""; }
      const p = easeOut(prog(t, beats[idx][0], 0.35));
      cap.style.opacity = p;
      cap.style.transform = `translateY(${(1 - p) * 10}px)`;
    };
  }

  function clockRender(tb, w, t, opts = {}) {
    if (opts.after !== undefined && t >= opts.after) {
      tb.rt.textContent = "AFTER THE RUN";
      tb.b.textContent = "";
      show(tb.speed, false);
      return;
    }
    tb.rt.textContent = "REAL TIME";
    tb.b.textContent = fmt(w.real(t));
    const s = w.speed(t);
    show(tb.speed, s >= 1.5);
    tb.speed.textContent = `${Math.round(s)}× speed`;
  }

  // ---------- scenes ----------
  const scenes = {};

  scenes.intro = {
    duration: 6.5,
    setup(stage) {
      const card = el("div", "card");
      card.innerHTML = `<img class="mark" src="${MARK}" alt="">
        <div class="line l1">Your computer has your files, your apps and a browser.</div>
        <div class="line soft l2" style="margin-top:18px">YBM is an assistant that runs on it. You message it from your phone.</div>
        <div class="note n1">It runs a 9B language model on the laptop's own 8 GB GPU, so a task costs nothing and the files stay on the machine.</div>`;
      stage.appendChild(card);
      const parts = [[card.querySelector(".mark"), 0.1], [card.querySelector(".l1"), 0.5], [card.querySelector(".l2"), 2.2], [card.querySelector(".n1"), 3.9]];
      return (t) => {
        for (const [node, at] of parts) {
          const p = easeOut(prog(t, at, 0.7));
          node.style.opacity = p;
          node.style.transform = `translateY(${(1 - p) * 22}px)`;
        }
      };
    },
  };

  scenes.find = {
    duration: 14,
    chip: "Find",
    setup(stage) {
      const S = STORIES.find;
      const w = warp([[2.2, 0], [4.4, 11.1], [5.6, 23.1], [9.3, 26.8]]);
      const V = (r) => w.video(r);
      const tb = topbar(stage, "Ask about this computer");
      const ph = phone(stage);
      addItem(ph, { kind: "user", at: 2.2, versions: [{ at: 2.2, html: esc(S.ask) }] });
      const first = S.events[0], edit = S.events[1], answer = S.events[2];
      addItem(ph, { kind: "bot", at: V(first.t), doneAt: V(answer.t), versions: [
        { at: V(first.t), html: esc(first.text), progress: true }, { at: V(edit.t), html: esc(edit.text), progress: true }] });
      addItem(ph, { kind: "bot", at: V(answer.t), versions: [{ at: V(answer.t), html: md(answer.text) }] });

      const d = desk(stage);
      const W = win(d, "", 0, 0, 1080, 776, `<span class="path">This computer › <b>what YBM looked at</b></span>`);
      W.body.innerHTML = `<div style="position:absolute;left:34px;top:34px" class="toolchip"><span>${S.tool}</span><span class="op">${S.toolOperation}</span><span class="state run">running</span></div>
        <div class="total"><div class="big">${S.games.length}</div><div class="small">games · about 203 GB</div></div>
        <div class="waiting" style="position:absolute;left:34px;top:40px;color:#5f6883;font-size:19px">waiting for YBM to decide what to look at…</div>
        <div class="rows"><div class="head"><span><b>Installed through Steam</b></span><span>from the Steam library files on this PC</span></div></div>`;
      const chip = W.body.querySelector(".toolchip"), state = chip.querySelector(".state"), total = W.body.querySelector(".total"), waiting = W.body.querySelector(".waiting");
      const rows = W.body.querySelector(".rows");
      const rowNodes = S.games.map((g, i) => {
        const r = el("div", "row", `<span class="n">${String(i + 1).padStart(2, "0")}</span><span class="pad"></span><span>${esc(g)}</span>`);
        rows.appendChild(r); return r;
      });
      const tStart = V(edit.t) + 0.3, tEnd = V(answer.t) - 0.4;
      const cap = caption(stage, [
        [0, "Ask your computer a question, from your phone.", RUN_SRC],
        [5.6, "It checks the machine itself: its inventory tool reads the Steam library.", RUN_SRC],
        [9.3, "14 games, about 203 GB. The answer came 27 seconds after the question.", RUN_SRC],
      ]);
      return (t) => {
        clockRender(tb, w, t);
        renderPhone(ph, t, { text: S.ask, from: 0.3, to: 1.9, sendAt: 2.2 });
        const cp = easeOut(prog(t, V(edit.t) - 0.2, 0.4));
        chip.style.opacity = cp;
        waiting.style.opacity = 1 - cp;
        const ok = t >= tEnd;
        state.className = `state ${ok ? "ok" : "run"}`; state.textContent = ok ? "done" : "running";
        rows.style.opacity = easeOut(prog(t, tStart - 0.2, 0.3));
        rowNodes.forEach((r, i) => {
          const at = lerp(tStart, tEnd, i / (rowNodes.length - 1));
          const p = easeOut(prog(t, at, 0.3));
          r.style.opacity = p; r.style.transform = `translateX(${(1 - p) * 18}px)`;
        });
        const tp = easeOut(prog(t, tEnd, 0.5));
        total.style.opacity = tp; total.style.transform = `scale(${0.9 + 0.1 * tp})`;
        cap(t);
      };
    },
  };

  scenes.organize = {
    duration: 25,
    chip: "Organize",
    gate: null, // set in setup: the moment the approval waits for a tap (web mode)
    setup(stage, scene) {
      const S = STORIES.organize;
      const ev = Object.fromEntries(S.events.map((e) => [e.kind + e.id + (e.text.startsWith("Moved") ? "m" : e.text.startsWith("Planned") ? "p" : ""), e]));
      const w = warp([[2.0, 0], [3.6, 8], [5.6, 29], [7.6, 31], [11.6, 35], [16.4, 42], [18.4, 53]]);
      const V = (r) => w.video(r);
      scene.gate = V(35);
      const tb = topbar(stage, "Change files, with approval");
      const ph = phone(stage);
      addItem(ph, { kind: "user", at: 2.0, versions: [{ at: 2.0, html: esc(S.ask) }] });
      const working = S.events[0], planned = S.events[1], approval = S.events[2], tap = S.events[3], moved = S.events[4], final = S.events[5];
      addItem(ph, { kind: "bot", at: V(working.t), doneAt: V(moved.t), versions: [
        { at: V(working.t), html: esc(working.text), progress: true },
        { at: V(planned.t), html: esc(planned.text), progress: true },
        { at: V(moved.t), html: `<span class="tick">✓</span>${esc(moved.text)}` }] });
      const appr = addItem(ph, { kind: "approval", at: V(approval.t), text: approval.text, tapAt: V(tap.t) });
      addItem(ph, { kind: "bot", at: V(final.t), versions: [{ at: V(final.t), html: md(final.text) }] });

      // explorer
      const d = desk(stage);
      const W = win(d, "", 0, 0, 1080, 776, `<span class="path">This PC › <b>Downloads</b></span>`);
      const nav = el("div", "nav"), grid = el("div", "grid");
      W.body.appendChild(nav); W.body.appendChild(grid);
      nav.appendChild(el("div", "item root", `${folderIcon("#6274e8", 26, 22)}<span>Downloads</span><span class="count">24</span>`));
      const rootCount = nav.querySelector(".root .count");
      const folders = ["archives", "audio", "documents", "images", "other", "spreadsheets", "video"];
      const destOf = Object.fromEntries(S.manifest.map((m) => [m.source, m.destination]));
      const counts = Object.fromEntries(folders.map((f) => [f, S.manifest.filter((m) => m.destination === f).length]));
      const navItems = {};
      folders.forEach((f) => {
        const it = el("div", "item sub", `${folderIcon(FOLDER_COLOR[f], 26, 22)}<span>${f}</span><span class="count">0</span>`);
        nav.appendChild(it); navItems[f] = { node: it, count: it.querySelector(".count") };
      });
      const COLS = 6, CW = 136, CH = 150, X0 = 26, Y0 = 34;
      const files = S.manifest.map((m, i) => {
        const node = el("div", "tile", `${fileIcon(m.source)}<div class="name">${esc(m.source).replace(/([-._])/g, "$1<wbr>")}</div><div class="dest" style="background:${FOLDER_COLOR[m.destination]}">→ ${m.destination}</div>`);
        const x = X0 + (i % COLS) * CW, y = Y0 + Math.floor(i / COLS) * CH;
        Object.assign(node.style, { left: `${x}px`, top: `${y}px` });
        grid.appendChild(node);
        const fly = el("div", "fly", fileIcon(m.source));
        W.body.appendChild(fly);
        return { m, node, dest: node.querySelector(".dest"), fly, x: x + 230 + 37, y: y + 0 };
      });
      const folderTiles = folders.map((f, i) => {
        const node = el("div", "foldertile", `${folderIcon(FOLDER_COLOR[f])}<div class="name">${f}</div><div class="cnt">${counts[f]} item${counts[f] > 1 ? "s" : ""}</div>`);
        Object.assign(node.style, { left: `${30 + (i % 5) * 162}px`, top: `${40 + Math.floor(i / 5) * 190}px` });
        grid.appendChild(node); return node;
      });
      const receipt = el("div", "receipt", `<div class="rh">${CHECK} Receipt · completed</div>
        ${S.manifest.slice(0, 5).map((m) => `<div class="rl"><em>Moved</em>Downloads\\${m.destination}\\${esc(m.source)}</div>`).join("")}
        <div class="rf">+ 19 more moves · No external transfer was recorded</div>`);
      W.body.appendChild(receipt);

      const tPlan = V(planned.t), tFly0 = V(tap.t) + 0.5, tFly1 = V(moved.t) - 0.5, tFinal = V(final.t);
      const flyDur = 0.85;
      const departs = files.map((_, i) => lerp(tFly0, tFly1 - flyDur, i / (files.length - 1)));
      const navY = (f) => 18 + 40 + folders.indexOf(f) * 40 + 20; // body coords of a nav row centre
      const cap = caption(stage, [
        [0, "Ask it to organize a folder of 24 downloads.", "Real run: model responses replayed at their recorded speed; YBM, the files and the approval live · Qwen3.5 9B · 8 GB laptop GPU"],
        [3.6, "It plans every move first: 7 folders, by type.", "From the run's own plan (the move list the model wrote)"],
        [7.6, "Then it stops and asks. Nothing has moved yet.", "The approval names the exact operation"],
        [11.6, "One tap approves this exact operation, once.", "An approval is bound to one request, expires, and is used once"],
        [16.4, "24 files moved into 7 folders.", "From the run's log"],
        [19.4, "A receipt lists every move. 53 seconds from asking to done.", "Real time includes about 4 seconds of waiting at the approval"],
      ]);
      return (t) => {
        clockRender(tb, w, t);
        renderPhone(ph, t, { text: S.ask, from: 0.3, to: 1.6, sendAt: 2.0 });
        let landed = 0;
        const per = Object.fromEntries(folders.map((f) => [f, 0]));
        files.forEach((f, i) => {
          // plan chips
          const cp = easeOut(prog(t, tPlan + i * 0.06, 0.3));
          f.dest.style.opacity = t < tFly0 ? cp : 0;
          f.dest.style.transform = `scale(${0.8 + 0.2 * cp})`;
          // flight to the folder in the left pane
          const dep = departs[i];
          const fp = prog(t, dep, flyDur);
          f.node.style.opacity = t < dep ? 1 : 0;
          const flying = t >= dep && fp < 1;
          show(f.fly, flying);
          if (flying) {
            const e = easeInOut(fp);
            const x0 = f.x, y0 = f.y, x1 = 8, y1 = navY(f.m.destination) - 35;
            const cx = (x0 + x1) / 2, cy = Math.min(y0, y1) - 120;
            const x = (1 - e) * (1 - e) * x0 + 2 * (1 - e) * e * cx + e * e * x1;
            const y = (1 - e) * (1 - e) * y0 + 2 * (1 - e) * e * cy + e * e * y1;
            const s = 1 - 0.65 * e;
            f.fly.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
            f.fly.style.opacity = 1 - clamp((fp - 0.8) / 0.2);
          }
          if (fp >= 1) { landed++; per[f.m.destination]++; }
        });
        rootCount.textContent = String(24 - landed + folders.filter((f) => t >= tFly0 - 0.4 + folders.indexOf(f) * 0.05).length);
        folders.forEach((f, i) => {
          const it = navItems[f];
          const p = easeOut(prog(t, tFly0 - 0.4 + i * 0.05, 0.35));
          it.node.style.opacity = p;
          it.node.style.transform = `translateX(${(1 - p) * -14}px)`;
          it.count.textContent = per[f];
        });
        const fp = easeOut(prog(t, tFly1 + 0.3, 0.5));
        folderTiles.forEach((n, i) => { const p = easeOut(prog(t, tFly1 + 0.3 + i * 0.07, 0.45)); n.style.opacity = p; n.style.transform = `translateY(${(1 - p) * 18}px)`; });
        const rp = easeOut(prog(t, tFinal + 0.6, 0.5));
        receipt.style.opacity = rp; receipt.style.transform = `translateY(${(1 - rp) * 30}px)`;
        cap(t);
        return fp;
      };
    },
  };

  scenes.build = {
    duration: 25,
    chip: "Build",
    setup(stage) {
      const S = STORIES.build;
      const w = warp([[2.8, 0], [4.2, 5.5], [6.0, 20.1], [8.6, 67.6], [13.2, 74.5], [16.7, 78.0]]);
      const V = (r) => w.video(r);
      const AFTER = 18.2;
      const tb = topbar(stage, "Write a program when no tool fits");
      const ph = phone(stage);
      addItem(ph, { kind: "user", at: 2.8, versions: [{ at: 2.8, html: esc(S.ask) }] });
      const evs = S.events;
      const final = evs[evs.length - 1];
      addItem(ph, { kind: "bot", at: V(evs[0].t), doneAt: V(final.t), versions: evs.slice(0, -1).map((e) => ({ at: V(e.t), html: esc(e.text), progress: true })) });
      addItem(ph, { kind: "bot", at: V(final.t), versions: [{ at: V(final.t), html: md(final.text) }] });

      const d = desk(stage);
      const code = win(d, "", 0, 0, 1080, 776, `<span class="path">This computer › .agent_control › workspaces › <b>task_e846…</b></span>`);
      code.body.innerHTML = `<div class="tree"><div class="t dir">task folder</div><div class="t f1">▤ TASK.md</div><div class="t f2">▤ index.html</div></div><div class="code"></div>
        <div class="launch toolchip"><span>workspace.manage</span><span class="op">launch_static</span><span class="url" style="color:#dfe4f2"></span><span class="state run">starting</span></div>`;
      const f1 = code.body.querySelector(".f1"), f2 = code.body.querySelector(".f2"), codeBox = code.body.querySelector(".code");
      const launch = code.body.querySelector(".launch"), lstate = launch.querySelector(".state"), lurl = launch.querySelector(".url");
      const lines = S.page.split("\n");
      const hl = (s) => esc(s).replace(/(&lt;\/?[a-zA-Z0-9]+)|("[^"]*")|('[^']*')/g, (m, tag) => `<span class="${tag ? "tg" : "st"}">${m}</span>`);
      const codeHtml = lines.map((l, i) => `<span class="ln">${i + 1}</span>${hl(l)}`);
      const br = win(d, "browser", 60, 40, 1020, 736, `<span class="path">Bill Tip Calculator</span>`);
      br.body.innerHTML = `<div class="urlbar"><span style="color:#8f99b3">← → ↻</span><div class="url">${S.url}</div></div><iframe title="The page YBM wrote"></iframe>`;
      const frame = br.body.querySelector("iframe");
      frame.srcdoc = S.page;
      const after = el("div", "after", "TRYING THE PAGE AFTER THE RUN");
      br.body.appendChild(after);
      const cursor = el("div", "cursor", CURSOR);
      d.appendChild(cursor);
      let doc = null;
      const ready = new Promise((res) => frame.addEventListener("load", () => { doc = frame.contentDocument; res(); }));
      const tWrite = V(67.6), tWriteEnd = V(74.5) - 0.3, tLaunch = V(74.5), tLink = V(final.t);
      const BILL = "64.50", TIP = "18";
      const cap = caption(stage, [
        [0, "Ask for something no tool on the computer does.", RUN_SRC],
        [4.4, "It sets up a project folder for the job on this computer.", RUN_SRC],
        [8.6, "Then it writes the page itself, line by line.", "The file below is the one the run wrote"],
        [13.2, "It starts the page and sends you the link. 78 seconds in all.", RUN_SRC],
        [AFTER, "This is the page it wrote, working.", "After the run: the same file, opened and used"],
      ]);
      function pos(id) {
        const r = doc.getElementById(id).getBoundingClientRect();
        return [60 + r.left + r.width * 0.5, 40 + 50 + 52 + r.top + r.height * 0.55];
      }
      const render = (t) => {
        clockRender(tb, w, t, { after: AFTER });
        renderPhone(ph, t, { text: S.ask, from: 0.2, to: 2.4, sendAt: 2.8 });
        code.node.style.opacity = easeOut(prog(t, V(20.1) - 0.2, 0.4));
        f1.style.opacity = t >= V(20.1) ? 1 : 0;
        f2.style.opacity = t >= tWrite ? 1 : 0;
        const n = Math.floor(lines.length * clamp((t - tWrite) / (tWriteEnd - tWrite)));
        const first = Math.max(0, n - 34);
        codeBox.innerHTML = t < tWrite ? '<span style="color:#5f6883">waiting for the model…</span>' : codeHtml.slice(first, n).join("\n") + (n < lines.length ? '<span class="caret" style="display:inline-block;width:9px;height:18px;background:#8e9bff;vertical-align:-3px"></span>' : "");
        const lp = easeOut(prog(t, tLaunch, 0.35));
        launch.style.opacity = lp;
        const live = t >= tLink - 0.4;
        lstate.className = `state ${live ? "ok" : "run"}`; lstate.textContent = live ? "running" : "starting";
        lurl.textContent = live ? `→ ${S.url}` : "";
        const bp = easeOut(prog(t, tLink + 0.15, 0.55));
        br.node.style.opacity = bp; br.node.style.transform = `translateY(${(1 - bp) * 40}px) scale(${0.97 + 0.03 * bp})`;
        show(br.node, t >= tLink);
        after.style.opacity = easeOut(prog(t, AFTER, 0.3));
        // the person using the page after the run
        if (doc) {
          const bill = doc.getElementById("billAmount"), tip = doc.getElementById("tipPercentage"), res = doc.getElementById("result");
          const tb0 = AFTER + 1.2, tb1 = tb0 + 0.9, tt0 = tb1 + 0.9, tt1 = tt0 + 0.5, tc = tt1 + 1.1;
          bill.value = BILL.slice(0, Math.floor(BILL.length * clamp((t - tb0) / (tb1 - tb0) + 0.001)));
          tip.value = TIP.slice(0, Math.floor(TIP.length * clamp((t - tt0) / (tt1 - tt0) + 0.001)));
          if (t >= tc) { if (!res.dataset.done) { frame.contentWindow.calculateTotal(); res.dataset.done = "1"; } }
          else { res.innerHTML = ""; delete res.dataset.done; }
          const btn = doc.querySelector("button");
          btn.style.backgroundColor = t >= tc && t < tc + 0.25 ? "#1e7e34" : "";
          const pts = [[AFTER, [900, 700]], [tb0 - 0.6, pos("billAmount")], [tt0 - 0.6, pos("billAmount")], [tt0 - 0.05, pos("tipPercentage")], [tc - 0.5, pos("tipPercentage")], [tc - 0.05, (() => { const r = btn.getBoundingClientRect(); return [60 + r.left + r.width * 0.5, 40 + 102 + r.top + r.height * 0.55]; })()]];
          let cx = pts[0][1][0], cy = pts[0][1][1];
          for (let i = 1; i < pts.length; i++) {
            const [a, pa] = pts[i - 1], [b, pb] = pts[i];
            if (t >= a) { const e = easeInOut(prog(t, a, b - a)); cx = lerp(pa[0], pb[0], e); cy = lerp(pa[1], pb[1], e); }
          }
          show(cursor, t >= AFTER);
          cursor.style.transform = `translate(${cx}px, ${cy}px) scale(${t >= tc && t < tc + 0.2 ? 0.85 : 1})`;
        }
        cap(t);
      };
      render.ready = ready;
      return render;
    },
  };

  scenes.outro = {
    duration: 12,
    setup(stage) {
      const S = STORIES.organize;
      const wrap = el("div", "");
      wrap.innerHTML = `
        <div class="card c1"><div class="kicker">Proof</div><div class="line">Every task ends with a receipt: what it touched, what ran, and what left the machine.</div>
          <div class="note">An answer that claims a file was written has to match a recorded write.</div>
          <div class="bigreceipt"><div class="rh">${CHECK} Receipt · Organize my Downloads folder by type</div>
            ${S.manifest.slice(0, 6).map((m) => `<div class="rl"><em>Moved</em>Downloads\\${m.destination}\\${esc(m.source)}</div>`).join("")}
            <div class="rl" style="color:#8f99b3">+ 18 more moves</div><div class="sep"></div>
            <div class="kv"><span>Approvals</span><b>1, approved once</b></div><div class="kv"><span>Left the machine</span><b>No external transfer was recorded</b></div></div></div>
        <div class="card c2"><div class="kicker">Measured on my laptop, November 19, 2026</div>
          <div class="stats"><div class="stat"><div class="v">30 <small>of 32</small></div><div class="k">everyday requests</div></div>
          <div class="stat"><div class="v">15 <small>of 20</small></div><div class="k">on an exam sealed before the work. My target was 18.</div></div></div>
          <div class="note">docs/MEASURED.md lists every miss, and what I have not measured.</div></div>
        <div class="card c3"><img class="mark" src="${MARK}" alt=""><div class="line">YBM</div><div class="repo">github.com/oney-erge/YBM</div>
          <div class="note">Open source, MIT. A Windows installer, and a one-line install for macOS and Linux.</div></div>`;
      stage.appendChild(wrap);
      const cards = [[wrap.querySelector(".c1"), 0, 4.2], [wrap.querySelector(".c2"), 4.2, 8.2], [wrap.querySelector(".c3"), 8.2, 99]];
      return (t) => {
        for (const [node, a, b] of cards) {
          const pin = easeOut(prog(t, a, 0.6)), pout = b < 99 ? prog(t, b - 0.4, 0.4) : 0;
          node.style.opacity = pin * (1 - pout);
          node.style.transform = `translateY(${(1 - pin) * 24 - pout * 16}px)`;
          show(node, t >= a - 0.01 && t < b);
        }
      };
    },
  };

  // ---------- player ----------
  let current = null;
  function mount(name, host, layout) {
    host.innerHTML = "";
    const stage = el("div", `stage ${layout === "portrait" ? "portrait" : ""}`);
    host.appendChild(stage);
    const scene = scenes[name];
    const render = scene.setup(stage, scene);
    current = { name, scene, stage, render, duration: scene.duration };
    return current;
  }

  window.__ybm = { scenes, mount, MODE };

  if (MODE === "record") {
    document.body.classList.add("record");
    const host = document.getElementById("host");
    const cur = mount(q.get("scene") || "organize", host, q.get("layout") || "landscape");
    window.__duration = cur.duration;
    window.__seek = (t) => { cur.render(t); return true; };
    Promise.all([document.fonts.ready, cur.render.ready || Promise.resolve()]).then(() => { cur.render(0); window.__ready = true; });
  }
})();
