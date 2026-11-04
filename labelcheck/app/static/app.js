"use strict";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STATUS = {fail:"FAIL", warn:"CHECK", pass:"PASS", manual:"MANUAL", na:"N/A"};
const COLOR = {fail:"#c62828", warn:"#e08a00", pass:"#2e7d32", manual:"#3d6fa0", na:"#808080"};
const VERDICT = {issues_found:"Issues found", needs_review:"No automated failures. Manual checks pending.",
                 no_issues_found:"No issues found in automated checks"};
const REVIEW = {not_submitted:"Not submitted", in_review:"In review", approved:"Approved", changes_requested:"Changes requested"};
const CHANGE = {fixed:"Fixed", new_issue:"New issue", improved:"Improved", worse:"Worse", unchanged:"Unchanged", changed:"Changed"};
let ME = null, META = null, CURRENT = null, file = null, bfiles = [], pollTimer = null, isInitializing = false;

// ---------------- Session / Token Storage (Tab-Isolated) ----------------
const SESSION_TOKEN_KEY = "lc_session_token";
const SESSION_USER_KEY = "lc_session_user";
const SEED_TOKEN_KEY = "lc_seed_session_token";
const SEED_USER_KEY = "lc_seed_session_user";

function getStoredToken() {
  try {
    let tok = sessionStorage.getItem(SESSION_TOKEN_KEY);
    if (!tok) {
      tok = localStorage.getItem(SEED_TOKEN_KEY);
      if (tok) {
        sessionStorage.setItem(SESSION_TOKEN_KEY, tok);
        const seedUser = localStorage.getItem(SEED_USER_KEY);
        if (seedUser) sessionStorage.setItem(SESSION_USER_KEY, seedUser);
      }
    }
    return tok || null;
  } catch (_) {
    return null;
  }
}

function setSessionAuth(token, user) {
  try {
    if (token) {
      sessionStorage.setItem(SESSION_TOKEN_KEY, token);
      localStorage.setItem(SEED_TOKEN_KEY, token);
      if (user) {
        sessionStorage.setItem(SESSION_USER_KEY, JSON.stringify(user));
        localStorage.setItem(SEED_USER_KEY, JSON.stringify(user));
      }
    } else {
      clearSessionAuth(false);
    }
  } catch (_) {}
}

function clearSessionAuth(allTabs = false) {
  try {
    sessionStorage.removeItem(SESSION_TOKEN_KEY);
    sessionStorage.removeItem(SESSION_USER_KEY);
    sessionStorage.removeItem("lc_active_tab");
    if (allTabs) {
      localStorage.removeItem(SEED_TOKEN_KEY);
      localStorage.removeItem(SEED_USER_KEY);
    }
  } catch (_) {}
  ME = null;
}

// ---------------- API helper ----------------
async function api(path, opts = {}) {
  opts.headers = Object.assign({"X-LabelCheck": "1"}, opts.headers || {});
  const token = getStoredToken();
  if (token && !opts.headers["Authorization"]) {
    opts.headers["Authorization"] = `Bearer ${token}`;
  }
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers["Content-Type"] = "application/json";
    delete opts.json;
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/auth/login" && path !== "/api/auth/register") {
    clearSessionAuth(false);
    if (!isInitializing && path !== "/api/auth/me") showLogin();
    throw new Error("Please log in.");
  }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    let d = body && body.detail !== undefined ? body.detail : body;
    if (Array.isArray(d)) d = d.map(x => x.msg).join("; ");
    if (d && typeof d === "object") d = d.message || JSON.stringify(d);
    throw new Error(d || `Request failed (${r.status})`);
  }
  return body;
}
const when = t => t ? esc(String(t).replace("T", " ").slice(0, 16)) : "-";

// ---------------- Day / Night Mode ----------------
function initTheme() {
  const getSaved = () => {
    const s = localStorage.getItem("labelcheck_theme");
    return (s === "light" || s === "dark") ? s : null;
  };
  const getSystem = () => window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

  const apply = (theme, mode) => {
    document.documentElement.setAttribute("data-theme", theme);
    const isDark = theme === "dark";
    $$(".theme-toggle").forEach(btn => {
      const icon = btn.querySelector(".theme-icon");
      const label = btn.querySelector(".theme-label");
      if (icon) icon.textContent = isDark ? "☀️" : "🌙";
      if (label) label.textContent = isDark ? "Day" : "Night";
      btn.title = isDark ? "Switch to Day mode" : "Switch to Night mode";
    });
    const currentMode = mode || getSaved() || "system";
    $$(".theme-pref-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.setTheme === currentMode);
    });
  };

  const toggle = () => {
    const current = document.documentElement.getAttribute("data-theme") || getSystem();
    const next = current === "dark" ? "light" : "dark";
    localStorage.setItem("labelcheck_theme", next);
    apply(next, next);
  };

  $$(".theme-toggle").forEach(b => b.onclick = toggle);
  $$(".theme-pref-btn").forEach(b => b.onclick = () => {
    const m = b.dataset.setTheme;
    if (m === "system") {
      localStorage.removeItem("labelcheck_theme");
      apply(getSystem(), "system");
    } else {
      localStorage.setItem("labelcheck_theme", m);
      apply(m, m);
    }
  });

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => {
    if (!getSaved()) apply(e.matches ? "dark" : "light", "system");
  });

  const initial = getSaved();
  apply(initial || getSystem(), initial || "system");
}

// ---------------- auth / start ----------------
const VIEWS = ["landing-view", "login-view", "signup-view", "terms-view", "app-view", "forgot-view", "setpw-view", "verify-view", "resend-view", "auth-loading"];
function showOnly(id) {
  VIEWS.forEach(v => {
    const el = $("#" + v);
    if (el) el.classList.toggle("hidden", v !== id);
  });
  const authToggle = $("#theme-toggle-auth");
  if (authToggle) authToggle.classList.toggle("hidden", id === "app-view" || id === "landing-view" || id === "auth-loading");
}
function showHome() { showOnly("landing-view"); }
function showLogin() { showOnly("login-view"); $("#l-email").focus(); }

function clientPasswordProblem(pw) {
  if (pw.length < 12) return "Password must be at least 12 characters long.";
  if (!/[A-Z]/.test(pw)) return "Password must contain at least one uppercase letter (A-Z).";
  if (!/[a-z]/.test(pw)) return "Password must contain at least one lowercase letter (a-z).";
  if (!/\d/.test(pw)) return "Password must contain at least one number (0-9).";
  if (!/[^A-Za-z0-9]/.test(pw)) return "Password must contain at least one special symbol (e.g. !@#$%^&*).";
  return null;
}

const PLAN_PRICES = {
  trial: { label: "Create Free Account", price: 0 },
  standard: { label: "Proceed to Payment (₹4,999)", price: 4999 },
  business: { label: "Proceed to Payment (₹14,999)", price: 14999 },
  pack_1: { label: "Proceed to Payment (₹99)", price: 99 },
  pack_50: { label: "Proceed to Payment (₹3,999)", price: 3999 },
  pack_250: { label: "Proceed to Payment (₹14,999)", price: 14999 },
};
function updateSignupButton() {
  const p = $("#su-plan") ? $("#su-plan").value : "trial";
  const info = PLAN_PRICES[p] || PLAN_PRICES.trial;
  if ($("#su-btn")) $("#su-btn").textContent = info.label;
}
if ($("#su-plan")) $("#su-plan").onchange = updateSignupButton;

function showSignup(plan) {
  ME = null;
  showOnly("signup-view");
  if (plan && $("#su-plan")) {
    const opts = Array.from($("#su-plan").options).map(o => o.value);
    if (opts.includes(plan)) $("#su-plan").value = plan;
  }
  updateSignupButton();
  $("#su-cname").focus();
}
let SETPW = null;   // {kind: "reset"|"invite", token}

// Pricing switcher on landing page
const btnMonthly = $("#btn-show-monthly");
const btnPayg = $("#btn-show-payg");
if (btnMonthly && btnPayg) {
  btnMonthly.onclick = () => {
    btnMonthly.classList.add("active");
    btnPayg.classList.remove("active");
    $("#pricing-monthly").classList.remove("hidden");
    $("#pricing-payg").classList.add("hidden");
  };
  btnPayg.onclick = () => {
    btnPayg.classList.add("active");
    btnMonthly.classList.remove("active");
    $("#pricing-payg").classList.remove("hidden");
    $("#pricing-monthly").classList.add("hidden");
  };
}

$("#login-form").onsubmit = async e => {
  e.preventDefault();
  $("#l-msg").textContent = "";
  try {
    const res = await api("/api/auth/login", {
      method: "POST",
      json: {
        email: $("#l-email").value,
        password: $("#l-pass").value,
        totp: $("#l-totp").value
      }
    });
    if (res && res.token) {
      setSessionAuth(res.token, res.user);
    }
    $("#l-pass").value = ""; $("#l-totp").value = ""; $("#l-totp-wrap").classList.add("hidden");
    if (location.hash === "#/login" || location.hash === "#login") {
      history.replaceState(null, "", window.location.pathname + window.location.search + "#/");
    }
    await start();
  } catch (err) {
    if (/6-digit code|Wrong or expired code/.test(err.message)) { $("#l-totp-wrap").classList.remove("hidden"); $("#l-totp").focus(); }
    $("#l-msg").textContent = err.message;
  }
};
$("#signup-form").onsubmit = async e => {
  e.preventDefault();
  $("#su-msg").textContent = "";
  const pw = $("#su-pass").value;
  const pwErr = clientPasswordProblem(pw);
  if (pwErr) { $("#su-msg").textContent = pwErr; return; }

  $("#su-btn").disabled = true;
  const chosenPlan = $("#su-plan") ? $("#su-plan").value : "trial";
  const website = $("#su-website") ? $("#su-website").value : "";
  try {
    const reg = await api("/api/auth/register", {
      method: "POST",
      json: {
        company_name: $("#su-cname").value,
        name: $("#su-name").value,
        email: $("#su-email").value,
        password: pw,
        plan: "trial",
        website: website
      }
    });
    $("#su-pass").value = "";

    if (reg.verify_token) {
      // In dev mode without SMTP, allow instant 1-click verification
      $("#su-msg").style.color = "#16a34a";
      $("#su-msg").innerHTML = `Account created! <a href="${esc(reg.verify_url)}">Click here to verify email</a>.`;
      return;
    }

    if (reg.user && !reg.user.email_verified) {
      $("#su-msg").style.color = "#16a34a";
      $("#su-msg").textContent = "Account created! We've sent a verification link to your email. Please check your inbox and verify before logging in.";
      return;
    }

    if (reg && reg.token) {
      setSessionAuth(reg.token, reg.user);
    }
    location.hash = "#/";
    await start();

    if (chosenPlan && chosenPlan !== "trial") {
      await checkoutRazorpay(chosenPlan);
    }
  } catch (err) {
    $("#su-msg").style.color = "#dc2626";
    $("#su-msg").textContent = err.message;
  } finally {
    $("#su-btn").disabled = false;
  }
};
$("#forgot-form").onsubmit = async e => {
  e.preventDefault();
  try { const r = await api("/api/auth/forgot", {method: "POST", json: {email: $("#f-email").value}}); $("#f-msg").textContent = r.message; }
  catch (err) { $("#f-msg").textContent = err.message; }
};
$("#resend-form").onsubmit = async e => {
  e.preventDefault();
  $("#rv-btn").disabled = true;
  try {
    const r = await api("/api/auth/resend-verification", {method: "POST", json: {email: $("#rv-email").value}});
    $("#rv-msg").textContent = r.message;
    $("#rv-msg").style.color = "#16a34a";
  } catch (err) {
    $("#rv-msg").textContent = err.message;
    $("#rv-msg").style.color = "#dc2626";
  } finally {
    $("#rv-btn").disabled = false;
  }
};
$("#setpw-form").onsubmit = async e => {
  e.preventDefault();
  if ($("#s-pass").value !== $("#s-pass2").value) { $("#s-msg").textContent = "The two passwords are different."; return; }
  const pwErr = clientPasswordProblem($("#s-pass").value);
  if (pwErr) { $("#s-msg").textContent = pwErr; return; }
  try {
    const res = await api(SETPW.kind === "invite" ? "/api/auth/accept-invite" : "/api/auth/reset",
              {method: "POST", json: {token: SETPW.token, password: $("#s-pass").value}});
    $("#s-pass").value = $("#s-pass2").value = "";
    location.hash = "#/";
    if (SETPW.kind === "invite") {
      if (res && res.token) setSessionAuth(res.token, res.user);
      await start();
    } else {
      clearSessionAuth(false);
      showLogin();
      $("#l-msg").textContent = "Password changed. Please log in.";
    }
  } catch (err) { $("#s-msg").textContent = err.message; }
};
$("#terms-ok").onclick = async () => { await api("/api/auth/accept-terms", {method: "POST"}); await start(); };
$("#logout").onclick = async () => {
  try { await api("/api/auth/logout", {method: "POST"}); } catch (_) {}
  clearSessionAuth(true);
  location.hash = "#/";
  showHome();
};

async function route() {
  const hash = location.hash || "";
  if (hash.startsWith("#about-sec") || hash.startsWith("#features-sec") || hash.startsWith("#pricing-sec")) {
    showHome();
    const target = document.querySelector(hash);
    if (target) target.scrollIntoView({behavior: "smooth"});
    return true;
  }

  const m = hash.match(/^#\/(login|signup|reset|invite|forgot|verify|resend|scan|batch)\/?([A-Za-z0-9_\-]*)/);
  if (!m) {
    if (hash === "#/login") {
      if (ME) {
        history.replaceState(null, "", window.location.pathname + window.location.search + "#/");
        return false;
      }
      showLogin();
      return true;
    }
    if (hash === "#/resend") { showOnly("resend-view"); return true; }
    if (hash === "#/signup" || hash.startsWith("#/signup?")) {
      if (ME) {
        history.replaceState(null, "", window.location.pathname + window.location.search + "#/");
        return false;
      }
      const planMatch = hash.match(/[?&]plan=([a-z0-9_]+)/);
      showSignup(planMatch ? planMatch[1] : "trial");
      return true;
    }
    return false;
  }
  const [, kind, arg] = m;
  if (kind === "login") {
    if (ME) {
      history.replaceState(null, "", window.location.pathname + window.location.search + "#/");
      return false;
    }
    showLogin();
    return true;
  }
  if (kind === "resend") { showOnly("resend-view"); return true; }
  if (kind === "signup") {
    if (ME) {
      history.replaceState(null, "", window.location.pathname + window.location.search + "#/");
      return false;
    }
    const planMatch = hash.match(/[?&]plan=([a-z0-9_]+)/);
    showSignup(planMatch ? planMatch[1] : (arg || "trial"));
    return true;
  }
  if (kind === "forgot") { showOnly("forgot-view"); return true; }
  if (kind === "verify") {
    showOnly("verify-view");
    $("#verify-icon").textContent = "⏳";
    $("#verify-title").textContent = "Verifying email…";
    $("#verify-msg").textContent = "Please wait while we confirm your email address.";
    try {
      const res = await api("/api/auth/verify-email", {method: "POST", json: {token: arg}});
      $("#verify-icon").textContent = "✅";
      $("#verify-title").textContent = "Email Verified!";
      $("#verify-msg").textContent = res.message || "Your email address has been confirmed. You can now log in.";
      $("#verify-action-btn").textContent = "Log In";
      $("#verify-action-btn").href = "#/login";
    } catch (err) {
      $("#verify-icon").textContent = "❌";
      $("#verify-title").textContent = "Verification Failed";
      $("#verify-msg").textContent = err.message || "This verification link is invalid or expired.";
      $("#verify-action-btn").textContent = "Request New Link";
      $("#verify-action-btn").href = "#/resend";
    }
    return true;
  }
  if (kind === "reset" || kind === "invite") {
    SETPW = {kind, token: arg};
    $("#s-msg").textContent = "";
    $("#setpw-title").textContent = kind === "invite" ? "Welcome to LabelCheck" : "Choose a new password";
    $("#setpw-info").textContent = "";
    if (kind === "invite") {
      try { const i = await api("/api/auth/invite/" + arg); $("#setpw-info").textContent = `Set a password for ${i.email} (${i.org_name}).`; }
      catch (err) { $("#setpw-info").textContent = err.message; }
    }
    showOnly("setpw-view");
    return true;
  }
  return false;
}

async function start() {
  try {
    ME = await api("/api/auth/me");
    setSessionAuth(getStoredToken(), ME);
  } catch (_) {
    clearSessionAuth(false);
    showHome();
    return;
  }
  if (!ME.terms_accepted) { showOnly("terms-view"); return; }
  META = META || await api("/api/meta");
  showOnly("app-view");
  const rset = $("#ruleset");
  if (rset) {
    rset.textContent = "Rule set " + META.ruleset.version;
    rset.title = META.ruleset.note;
  }
  const opts = Object.entries(META.categories).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  $("#category").innerHTML = opts; $("#bcat").innerHTML = opts;
  const isPlatformAdmin = Boolean(ME.is_platform_admin || ME.name === "Administrator");
  const isAdmin = ME.role === "admin" || isPlatformAdmin;
  const isReviewerOrAdmin = ME.role === "reviewer" || isAdmin;
  $$(".reviewer-only").forEach(b => b.classList.toggle("hidden", !isReviewerOrAdmin));
  $$(".admin-only").forEach(b => b.classList.toggle("hidden", !isAdmin));
  $$(".platform-only").forEach(b => b.classList.toggle("hidden", !isPlatformAdmin));
  $("#me-btn").textContent = ME.name.split(" ")[0];
  $("#me-info").innerHTML = `<table>
    <tr><td>Name</td><td>${esc(ME.name)}</td></tr><tr><td>Email</td><td>${esc(ME.email)}</td></tr>
    <tr><td>Company</td><td>${esc(ME.org_name)}</td></tr><tr><td>Role</td><td><span class="pill role-${esc(ME.role)}">${esc(ME.role.toUpperCase())}</span></td></tr></table>`;
  $("#notify").checked = ME.notify;
  renderTfa();
  if (ME.org_requires_2fa && !ME.totp_enabled) {
    openTab("account");
    $("#tfa-body").insertAdjacentHTML("afterbegin", `<p class="err">Your company requires two-step login. Set it up to continue.</p>`);
    return;
  }
  refreshQueueCount();
  const m = location.hash.match(/^#\/(scan|batch)\/(\d+)/);
  if (m && m[1] === "scan") openScan(m[2]);
  else if (m && m[1] === "batch") { openTab("batch"); showBatch(m[2]); }
  else {
    const savedTab = sessionStorage.getItem("lc_active_tab");
    if (savedTab && canAccessTab(savedTab)) {
      openTab(savedTab);
    } else {
      openTab("scan");
    }
  }
}

// ---------------- tabs ----------------
function canAccessTab(tab) {
  if (!ME) return false;
  const isPlatformAdmin = Boolean(ME.is_platform_admin || ME.name === "Administrator");
  const isAdmin = ME.role === "admin" || isPlatformAdmin;
  if (tab === "platform") return isPlatformAdmin;
  if (tab === "users" || tab === "audit") return isAdmin;
  if (tab === "review") return ME.role === "reviewer" || isAdmin;
  return true;
}

const LOADERS = {history: loadHistory, dash: loadDash, batch: loadBatches, review: loadReviewQueue,
                 users: loadCompany, audit: loadAudit, platform: loadPlatform};
$$("#nav button").forEach(b => b.onclick = () => openTab(b.dataset.tab));
function openTab(tab) {
  if (!canAccessTab(tab)) {
    tab = "history";
  }
  try { sessionStorage.setItem("lc_active_tab", tab); } catch (_) {}
  $$("#nav button").forEach(x => x.classList.toggle("on", x.dataset.tab === tab));
  $$("main > section").forEach(s => s.classList.toggle("hidden", s.id !== "tab-" + tab));
  if (LOADERS[tab]) LOADERS[tab]();
}

// ---------------- single scan ----------------
function wireDrop(dropEl, input, onPick) {
  dropEl.onclick = () => input.click();
  dropEl.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
  input.onchange = e => onPick([...e.target.files]);
  dropEl.ondragover = e => e.preventDefault();
  dropEl.ondrop = e => { e.preventDefault(); onPick([...e.dataTransfer.files]); };
}
wireDrop($("#drop"), $("#file"), fs => {
  if (!fs[0]) return;
  file = fs[0]; $("#drop").textContent = file.name; $("#drop").classList.add("has");
  const pdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  $("#straighten").checked = !pdf;
});

$("#form").onsubmit = async e => {
  e.preventDefault();
  if (!file) { $("#status").textContent = "Choose a label image or PDF first."; return; }
  const fd = new FormData();
  fd.append("image", file);
  fd.append("product_name", $("#product").value);
  fd.append("sku", $("#sku").value);
  fd.append("category", $("#category").value);
  if ($("#pw").value) fd.append("panel_width_mm", $("#pw").value);
  if ($("#ph").value) fd.append("panel_height_mm", $("#ph").value);
  fd.append("embossed", $("#embossed").checked);
  fd.append("imported", $("#imported").checked);
  fd.append("auto_straighten", $("#straighten").checked);
  fd.append("listing_text", $("#listing").value);
  $("#go").disabled = true; $("#status").textContent = "Reading the label…";
  try {
    const j = await api("/api/scans", {method: "POST", body: fd});
    render(j);
    $("#status").textContent = j.status === "error" ? j.error : `Saved as scan #${j.id} (version ${j.version}).`;
  } catch (err) { $("#status").textContent = err.message; }
  finally { $("#go").disabled = false; }
};

// ---------------- pagination component ----------------
function renderPaginationBar({page, total, limit}) {
  const totalPages = Math.ceil(total / limit) || 1;
  if (total <= limit) return ""; // Only paginate when exceeding limit (>5 for checks, >10 for other lists)

  const start = (page - 1) * limit + 1;
  const end = Math.min(page * limit, total);

  let pages = [];
  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) pages.push(i);
  } else {
    pages.push(1);
    if (page > 3) pages.push("...");
    const winStart = Math.max(2, page - 1);
    const winEnd = Math.min(totalPages - 1, page + 1);
    for (let i = winStart; i <= winEnd; i++) pages.push(i);
    if (page < totalPages - 2) pages.push("...");
    pages.push(totalPages);
  }

  const buttons = pages.map(p => {
    if (p === "...") return `<span class="pg-ellipsis">…</span>`;
    return `<button type="button" class="pg-btn ${p === page ? "active" : ""}" data-pg="${p}">${p}</button>`;
  }).join("");

  return `
    <div class="pagination-bar">
      <span class="pg-info">Showing ${start}–${end} of ${total}</span>
      <div class="pg-controls">
        <button type="button" class="pg-btn" data-pg="${page - 1}" ${page <= 1 ? "disabled" : ""}>« Prev</button>
        ${buttons}
        <button type="button" class="pg-btn" data-pg="${page + 1}" ${page >= totalPages ? "disabled" : ""}>Next »</button>
      </div>
    </div>`;
}

function wirePagination(containerEl, onPage) {
  if (!containerEl) return;
  containerEl.querySelectorAll("[data-pg]").forEach(b => {
    b.onclick = (e) => {
      e.preventDefault();
      const p = parseInt(b.dataset.pg, 10);
      if (!isNaN(p)) onPage(p);
    };
  });
}

// ---------------- scan detail ----------------
let CURRENT_FINDINGS_PAGE = 1;
let CURRENT_FINDINGS_FILTER = "all";
const FINDINGS_PAGE_SIZE = 5;

async function openScan(id) {
  openTab("scan");
  $("#out").innerHTML = `<div class="empty">Loading scan #${id}…</div>`;
  try {
    CURRENT_FINDINGS_PAGE = 1;
    CURRENT_FINDINGS_FILTER = "all";
    render(await api("/api/scans/" + id), true);
  } catch (err) { $("#out").innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}

function renderFindingsSection(j) {
  const res = j.result;
  const allFindings = res?.findings || [];
  const c = res?.summary?.counts || {};
  const totalAll = allFindings.length;

  const filtered = allFindings.filter(f => {
    if (CURRENT_FINDINGS_FILTER === "issues") return ["fail", "warn", "manual"].includes(f.status);
    if (CURRENT_FINDINGS_FILTER === "manual") return f.status === "manual";
    if (CURRENT_FINDINGS_FILTER === "pass") return f.status === "pass";
    if (CURRENT_FINDINGS_FILTER === "fail") return f.status === "fail";
    return true;
  });

  const totalFiltered = filtered.length;
  const totalPages = Math.ceil(totalFiltered / FINDINGS_PAGE_SIZE) || 1;
  if (CURRENT_FINDINGS_PAGE > totalPages) CURRENT_FINDINGS_PAGE = totalPages;
  if (CURRENT_FINDINGS_PAGE < 1) CURRENT_FINDINGS_PAGE = 1;

  const start = (CURRENT_FINDINGS_PAGE - 1) * FINDINGS_PAGE_SIZE;
  const visible = filtered.slice(start, start + FINDINGS_PAGE_SIZE);

  const flHtml = visible.map(f => {
    const origIdx = allFindings.indexOf(f);
    return findingItem(j, f, origIdx);
  }).join("");

  const issueCount = (c.fail || 0) + (c.warn || 0) + (c.manual || 0);

  const filterBar = `
    <div class="findings-header">
      <div style="font-weight:600;font-size:13.5px;">Statutory Checks (${totalAll})</div>
      <div class="findings-filter-bar">
        <button type="button" class="findings-filter-pill ${CURRENT_FINDINGS_FILTER === "all" ? "active" : ""}" data-ffilter="all">All (${totalAll})</button>
        ${issueCount ? `<button type="button" class="findings-filter-pill ${CURRENT_FINDINGS_FILTER === "issues" ? "active" : ""}" data-ffilter="issues">Issues (${issueCount})</button>` : ""}
        ${c.manual ? `<button type="button" class="findings-filter-pill ${CURRENT_FINDINGS_FILTER === "manual" ? "active" : ""}" data-ffilter="manual">Manual (${c.manual})</button>` : ""}
        ${c.pass ? `<button type="button" class="findings-filter-pill ${CURRENT_FINDINGS_FILTER === "pass" ? "active" : ""}" data-ffilter="pass">Pass (${c.pass})</button>` : ""}
      </div>
    </div>`;

  const pgBar = renderPaginationBar({
    page: CURRENT_FINDINGS_PAGE,
    total: totalFiltered,
    limit: FINDINGS_PAGE_SIZE
  });

  return `
    ${filterBar}
    <ul class="findings" id="flist">${flHtml.length ? flHtml : `<li style="display:block;padding:16px 0;text-align:center;color:var(--muted)">No checks matching '${CURRENT_FINDINGS_FILTER}'.</li>`}</ul>
    <div id="findings-pg">${pgBar}</div>`;
}

function wireFindings(j) {
  $$(".findings-filter-pill").forEach(btn => {
    btn.onclick = () => {
      CURRENT_FINDINGS_FILTER = btn.dataset.ffilter;
      CURRENT_FINDINGS_PAGE = 1;
      $("#findings-block").innerHTML = renderFindingsSection(j);
      wireFindings(j);
    };
  });
  wirePagination($("#findings-pg"), (p) => {
    CURRENT_FINDINGS_PAGE = p;
    $("#findings-block").innerHTML = renderFindingsSection(j);
    wireFindings(j);
  });
  $$("#flist li").forEach(li => {
    const g = document.querySelector(`#out svg g[data-i="${li.dataset.i}"] rect`);
    li.onmouseenter = () => g && g.setAttribute("fill-opacity", "0.35");
    li.onmouseleave = () => g && g.setAttribute("fill-opacity", "0.08");
  });
  $$("#flist .decide button").forEach(b => b.onclick = async () => {
    const box = b.closest(".decide");
    try {
      await api(`/api/scans/${j.id}/findings/${encodeURIComponent(box.dataset.key)}`,
        {method: "PUT", json: {decision: b.dataset.d, comment: box.querySelector("input").value}});
      const updated = await api("/api/scans/" + j.id);
      render(updated, false);
    } catch (err) { alertMsg(err.message); }
  });
}

function render(j, reset = true) {
  if (reset || !CURRENT || CURRENT.id !== j.id) {
    CURRENT_FINDINGS_PAGE = 1;
    CURRENT_FINDINGS_FILTER = "all";
  }
  CURRENT = j;
  if (j.status !== "done") {
    $("#out").innerHTML = `<div class="empty">Scan #${j.id} (${esc(j.filename)}): <span class="pill st-${j.status}">${esc(j.status)}</span>
      ${j.error ? `<p class="err">${esc(j.error)}</p>` : ""}</div>`;
    return;
  }
  const res = j.result, c = res.summary.counts, ex = j.extracted, W = j.image.width, H = j.image.height;
  const boxes = res.findings.map((f, i) => f.box ? `
    <g data-i="${i}" style="cursor:pointer;"><rect x="${f.box[0]}" y="${f.box[1]}" width="${f.box[2]}" height="${f.box[3]}"
      fill="${COLOR[f.status]}" fill-opacity="0.08" stroke="${COLOR[f.status]}" stroke-width="${Math.max(2, W / 400)}"/>
    <text x="${f.box[0]}" y="${Math.max(14, f.box[1] - 4)}" fill="${COLOR[f.status]}" font-size="${Math.max(14, W / 55)}" font-weight="700">${i + 1}</text></g>` : "").join("");
  const parties = Object.entries(ex.parties || {}).map(([k, p]) => `<tr><td>${esc(k[0].toUpperCase() + k.slice(1))}</td><td>${esc(p.value)}</td></tr>`).join("");
  const cc = ex.consumer_care || {};
  const ou = res.options_used || j.options || {};
  const versions = j.versions.length > 1 ? `<div class="versions">Versions:
      ${j.versions.map(v => `<button data-open="${v.id}" class="${v.id === j.id ? "cur" : ""}" title="${esc(REVIEW[v.review_status])}">v${v.version}</button>`).join("")}</div>` : "";
  $("#out").innerHTML = `
    <div class="verdict v-${res.summary.verdict}">
      <span>${VERDICT[res.summary.verdict]}</span>
      <small>Scan #${j.id} · ${esc(j.product || j.filename)}${j.sku ? " · " + esc(j.sku) : ""} · v${j.version}</small>
      <span class="counts">${["fail","warn","manual","pass","na"].map(s => c[s] ? `<span class="pill s-${s}">${c[s]} ${STATUS[s]}</span>` : "").join("")}</span>
    </div>
    <div class="actions">
      <a href="/api/scans/${j.id}/report.pdf">PDF report</a>
      <a href="/api/scans/${j.id}/report.docx">Word report</a>
      <a href="/api/scans/${j.id}/upload">Original file</a>
      ${j.previous_id ? `<button id="cmp">Compare with previous version</button>` : ""}
      ${j.can.delete ? `<button id="del-scan" class="danger">Delete scan</button>` : ""}
      ${versions}
    </div>
    <div id="cmp-out"></div>
    ${reviewPanel(j)}
    ${j.prep && j.prep.length ? `<ul class="prep">${j.prep.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    <div class="result">
      <div>
        <div class="imgwrap"><img src="/api/scans/${j.id}/image" alt="Checked label image">
          <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${boxes}</svg></div>
        <h2 style="font-size:14px;margin:16px 0 6px">Declarations read</h2>
        <table>
          <tr><td>Net quantity</td><td>${esc(ex.net_quantity?.value || "not read")}</td></tr>
          <tr><td>MRP</td><td>${esc(ex.mrp?.value ? "Rs " + ex.mrp.value : "not read")}</td></tr>
          <tr><td>Month / year</td><td>${esc(ex.date?.value || "not read")}</td></tr>
          ${parties || `<tr><td>Maker / packer</td><td>not read</td></tr>`}
          <tr><td>Consumer care</td><td>${esc([cc.phone, cc.email].filter(Boolean).join(", ") || "not read")}</td></tr>
          ${ex.fssai ? `<tr><td>FSSAI licence</td><td>${esc(ex.fssai.licence || "not read")}</td></tr>` : ""}
          <tr><td>Panel size used</td><td>${ou.panel_width_mm ? esc(ou.panel_width_mm + " x " + ou.panel_height_mm + " mm") : "not given"}</td></tr>
        </table>
        ${j.listing && j.listing.text ? `<details><summary>E-commerce listing text</summary><pre>${esc(j.listing.text.slice(0, 4000))}</pre></details>` : ""}
        <details><summary>Raw OCR text</summary><pre>${esc(j.ocr_text)}</pre></details>
      </div>
      <div>
        <div id="findings-block">${renderFindingsSection(j)}</div>
        <p class="note">${esc(res.ruleset.note)} Assistive tool only; not legal advice or a compliance certificate.</p>
        ${commentsPanel(j)}
        <details id="act"><summary>Activity log</summary><div id="act-body" class="hint">Loading…</div></details>
      </div>
    </div>`;
  wireScan(j);
}

function findingItem(j, f, i) {
  const d = j.decisions[f.key];
  const needs = !["pass", "na"].includes(f.status);
  const decided = d ? `<div class="dec ${d.decision}">${d.decision === "ok" ? "✓ Checked, OK" : "✗ Confirmed issue"} · ${esc(d.user_name)}${d.comment ? ": " + esc(d.comment) : ""}</div>` : "";
  const controls = needs && j.can.decide ? `<div class="decide" data-key="${esc(f.key)}">
      <button type="button" class="sbtn ok ${d?.decision === "ok" ? "on" : ""}" data-d="ok">OK</button>
      <button type="button" class="sbtn issue ${d?.decision === "issue" ? "on" : ""}" data-d="issue">Issue</button>
      <input type="text" placeholder="Note (optional)" maxlength="2000" value="${esc(d?.comment || "")}"></div>` : "";
  return `<li data-i="${i}"><span class="num">${i + 1}</span>
    <span><span class="pill s-${f.status}">${STATUS[f.status]}</span></span>
    <span><span class="rule">${esc(f.rule)}</span> · ${esc(f.title)}<br>${esc(f.detail)}
      <div class="meta">Basis: ${esc(sourceLabel(f.source))}${f.method !== "automated" ? " · " + esc(f.method) : ""}</div>
      ${decided}${controls}</span></li>`;
}
const SOURCE_LABELS = {verified_2011: "2011 rules text", verified_amendment: "Amendment text (read)",
  unverified_amendment: "Amendment (not yet verified)", fssai_2020: "FSSAI Labelling & Display Regs 2020", heuristic: "Engineering threshold"};
const sourceLabel = s => SOURCE_LABELS[s] || s;

function reviewPanel(j) {
  const rs = j.review_status;
  const pending = j.result.findings.filter(f => !["pass", "na"].includes(f.status));
  const open = pending.filter(f => !j.decisions[f.key]).length;
  const issues = pending.filter(f => j.decisions[f.key]?.decision === "issue").length;
  let who = "";
  if (j.reviewed_by && ["approved", "changes_requested"].includes(rs)) who = ` by ${esc(j.reviewed_by)} · ${when(j.reviewed_at)}`;
  const btns = [];
  if (j.can.submit) btns.push(`<button class="sbtn primary" data-act="submit">Submit for review</button>`);
  if (j.can.decide && rs === "in_review") {
    btns.push(`<button class="sbtn primary" data-act="approve" ${j.can.approve ? "" : "disabled"}>Approve label</button>`);
    btns.push(`<button class="sbtn" data-act="request_changes">Request changes</button>`);
  }
  const help = rs === "in_review"
    ? (j.can.decide ? `${open} finding(s) need a decision; ${issues} confirmed issue(s). Approval needs every FAIL / CHECK / MANUAL marked OK.`
       + (!j.can.approve && META.four_eyes && j.uploaded_by === ME.name ? " You uploaded this label, so another reviewer must approve it." : "")
       : "Waiting for a reviewer.")
    : rs === "changes_requested" ? "Fix the label, then upload the new version with the same SKU (or resubmit this one)." : "";
  return `<div class="panel"><h3>Review: <span class="pill r-${rs}">${REVIEW[rs]}</span><small class="hint">${who}</small></h3>
    <div class="hint">Uploaded by ${esc(j.uploaded_by)} · ${when(j.created_at)} UTC. ${help}</div>
    ${btns.length ? `<div class="decide"><input type="text" id="rnote" placeholder="Note for the team (required when requesting changes)" maxlength="4000">${btns.join("")}</div>` : ""}
    <div class="hint err" id="rmsg"></div></div>`;
}

function commentsPanel(j) {
  return `<div class="panel"><h3>Comments</h3>
    ${j.comments.length ? j.comments.map(c => `<div class="comment"><b>${esc(c.user_name)}</b> <small>${when(c.created_at)}</small><br>${esc(c.text)}</div>`).join("") : `<div class="hint">No comments yet.</div>`}
    <div class="decide"><input type="text" id="cnew" placeholder="Add a comment" maxlength="4000"><button class="sbtn" id="cadd">Add</button></div></div>`;
}

function wireScan(j) {
  wireFindings(j);
  $$("#out svg g[data-i]").forEach(g => {
    g.onclick = () => {
      const idx = parseInt(g.dataset.i, 10);
      CURRENT_FINDINGS_FILTER = "all";
      CURRENT_FINDINGS_PAGE = Math.floor(idx / FINDINGS_PAGE_SIZE) + 1;
      $("#findings-block").innerHTML = renderFindingsSection(j);
      wireFindings(j);
      const targetLi = $(`#flist li[data-i="${idx}"]`);
      if (targetLi) {
        targetLi.scrollIntoView({behavior: "smooth", block: "center"});
        targetLi.style.transition = "background .3s";
        targetLi.style.background = "var(--bg)";
        setTimeout(() => { targetLi.style.background = ""; }, 1500);
      }
    };
  });
  $$("[data-act]").forEach(b => b.onclick = async () => {
    try {
      render(await api(`/api/scans/${j.id}/review`, {method: "POST", json: {action: b.dataset.act, note: $("#rnote")?.value || ""}}), false);
      refreshQueueCount();
    } catch (err) { $("#rmsg").textContent = err.message; }
  });
  $("#cadd").onclick = async () => {
    const t = $("#cnew").value.trim(); if (!t) return;
    try { await api(`/api/scans/${j.id}/comments`, {method: "POST", json: {text: t}}); render(await api("/api/scans/" + j.id), false); }
    catch (err) { alertMsg(err.message); }
  };
  $$("[data-open]").forEach(b => b.onclick = () => openScan(b.dataset.open));
  if ($("#cmp")) $("#cmp").onclick = () => showCompare(j.id);
  if ($("#del-scan")) $("#del-scan").onclick = async () => {
    if (!confirmTwice($("#del-scan"))) return;
    try { await api(`/api/scans/${j.id}`, {method: "DELETE"}); $("#out").innerHTML = `<div class="empty">Scan #${j.id} deleted.</div>`; }
    catch (err) { alertMsg(err.message); }
  };
  $("#act").ontoggle = async () => {
    if (!$("#act").open) return;
    const rows = await api(`/api/scans/${j.id}/audit`);
    $("#act-body").innerHTML = `<table>${rows.map(a => `<tr><td>${when(a.created_at)}</td><td>${esc(a.user_name || "-")}</td><td>${esc(a.action.replace(/_/g, " "))}</td><td>${esc(fmtDetail(a.detail))}</td></tr>`).join("")}</table>`;
  };
}
function fmtDetail(d) {
  if (!d) return "";
  if (d.finding) return `${d.finding.split("~")[0]}: ${d.decision === "ok" ? "OK" : "Issue"}${d.comment ? " – " + d.comment : ""}`;
  return Object.entries(d).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(", ");
}
// Two clicks instead of a browser confirm() dialog (dialogs block automation and look dated).
function confirmTwice(btn) {
  if (btn.dataset.armed) return true;
  btn.dataset.armed = "1"; const t = btn.textContent; btn.textContent = "Click again to confirm";
  setTimeout(() => { delete btn.dataset.armed; btn.textContent = t; }, 4000);
  return false;
}
function alertMsg(m) { const el = $("#rmsg"); if (el) el.textContent = m; }

async function showCompare(id) {
  try {
    const c = await api(`/api/scans/${id}/compare`);
    const s = c.summary;
    $("#cmp-out").innerHTML = `<div class="panel"><h3>v${c.old.version} → v${c.new.version}:
      ${s.fixed} fixed, ${s.new_issue + s.worse} new or worse, ${s.unchanged} unchanged</h3>
      <div class="tablewrap"><table><tr><th>Rule</th><th>Before</th><th>Now</th><th>Change</th></tr>
      ${c.rules.filter(r => r.change !== "unchanged" || ["fail","warn"].includes(r.new)).map(r => `<tr><td>${esc(r.rule)} ${esc(r.title)}</td>
        <td>${r.old ? `<span class="pill s-${r.old}">${STATUS[r.old]}</span>` : "-"}</td>
        <td>${r.new ? `<span class="pill s-${r.new}">${STATUS[r.new]}</span>` : "-"}</td>
        <td class="ch-${r.change}">${CHANGE[r.change]}</td></tr>`).join("")}</table></div>
      <div class="tablewrap"><table><tr><th>Field</th><th>Before</th><th>Now</th></tr>
      ${c.fields.map(f => `<tr><td>${esc(f.field)}</td><td>${esc(f.old ?? "not read")}</td><td class="${f.changed ? "ch-changed" : ""}">${esc(f.new ?? "not read")}${f.changed ? " ●" : ""}</td></tr>`).join("")}
      </table></div></div>`;
  } catch (err) { $("#cmp-out").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
}

// ---------------- batch ----------------
wireDrop($("#bdrop"), $("#bfiles"), fs => {
  bfiles = fs.slice(0, 200);
  $("#bdrop").textContent = `${bfiles.length} file(s) selected`; $("#bdrop").classList.toggle("has", bfiles.length > 0);
});
$("#bform").onsubmit = async e => {
  e.preventDefault();
  if (!bfiles.length) { $("#bstatus").textContent = "Choose files first."; return; }
  const fd = new FormData();
  bfiles.forEach(f => fd.append("files", f));
  if ($("#manifest").files[0]) fd.append("manifest", $("#manifest").files[0]);
  fd.append("name", $("#bname").value);
  fd.append("category", $("#bcat").value);
  if ($("#bpw").value) fd.append("panel_width_mm", $("#bpw").value);
  if ($("#bph").value) fd.append("panel_height_mm", $("#bph").value);
  fd.append("auto_straighten", $("#bstraighten").checked);
  $("#bgo").disabled = true; $("#bstatus").textContent = "Uploading…";
  try {
    const r = await api("/api/batches", {method: "POST", body: fd});
    let msg = `Batch #${r.id} started with ${r.scan_ids.length} file(s).`;
    if (r.rejected.length) msg += ` Rejected: ${r.rejected.map(x => x.file + " (" + x.error + ")").join("; ")}.`;
    if (r.manifest_rows_without_file.length) msg += ` CSV rows with no matching file: ${r.manifest_rows_without_file.join(", ")}.`;
    $("#bstatus").textContent = msg;
    bfiles = []; $("#bdrop").textContent = "Click or drop many labels (JPG, PNG, PDF). Up to 200."; $("#bdrop").classList.remove("has");
    loadBatches(); showBatch(r.id);
  } catch (err) { $("#bstatus").textContent = err.message; }
  finally { $("#bgo").disabled = false; }
};
let BATCHES_PAGE = 1;
async function loadBatches(page = 1) {
  BATCHES_PAGE = page;
  const res = await api(`/api/batches?page=${BATCHES_PAGE}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#batches").innerHTML = rows.length ? `<div class="tablewrap"><table><tr><th>#</th><th>Name</th><th>Created</th><th>Progress</th></tr>
    ${rows.map(b => { const d = (b.status_counts?.done || 0) + (b.status_counts?.error || 0);
      return `<tr class="click" data-b="${b.id}"><td>${b.id}</td><td>${esc(b.name || "-")}</td><td>${when(b.created_at)}</td><td>${d} / ${b.total}</td></tr>`; }).join("")}</table></div>
      <div id="batches-pg">${renderPaginationBar({page: BATCHES_PAGE, total, limit: 10})}</div>`
    : `<div class="empty">No batches yet.</div>`;
  $$("[data-b]").forEach(tr => tr.onclick = () => showBatch(tr.dataset.b));
  wirePagination($("#batches-pg"), loadBatches);
}

let CURRENT_BATCH_ID = null;
let CURRENT_BATCH_PAGE = 1;
const BATCH_SCANS_PAGE_SIZE = 10;

async function showBatch(id, page = 1) {
  clearTimeout(pollTimer);
  CURRENT_BATCH_ID = id;
  CURRENT_BATCH_PAGE = page;
  const b = await api("/api/batches/" + id);
  const done = (b.status_counts?.done || 0) + (b.status_counts?.error || 0);
  const pct = Math.round(100 * done / Math.max(1, b.total));
  const scans = b.scans || [];
  const totalScans = scans.length;
  const totalScanPages = Math.ceil(totalScans / BATCH_SCANS_PAGE_SIZE) || 1;
  if (CURRENT_BATCH_PAGE > totalScanPages) CURRENT_BATCH_PAGE = totalScanPages;
  const start = (CURRENT_BATCH_PAGE - 1) * BATCH_SCANS_PAGE_SIZE;
  const visibleScans = scans.slice(start, start + BATCH_SCANS_PAGE_SIZE);

  $("#bdetail").innerHTML = `<div class="panel"><h3>Batch #${b.id} ${esc(b.name || "")}: ${done} of ${b.total} finished</h3>
    <div class="progress"><span style="width:${pct}%"></span></div>
    <div class="actions"><a href="/api/batches/${b.id}/summary.csv">Summary CSV</a><a href="/api/batches/${b.id}/reports.zip">All PDF reports (ZIP)</a></div>
    <div class="tablewrap"><table><tr><th>#</th><th>File</th><th>Product</th><th>Status</th><th>Result</th><th>Fail</th><th>Check</th></tr>
    ${visibleScans.map(s => `<tr class="click" data-open="${s.id}"><td>${s.id}</td><td>${esc(s.filename)}</td><td>${esc(s.product || "-")}${s.sku ? " · " + esc(s.sku) : ""}</td>
      <td><span class="pill st-${s.status}">${esc(s.status)}</span>${s.error ? `<div class="hint err">${esc(s.error)}</div>` : ""}</td>
      <td>${s.verdict ? `<span class="pill v-${s.verdict}">${esc(VERDICT[s.verdict])}</span>` : "-"}</td><td>${s.fails ?? ""}</td><td>${s.warns ?? ""}</td></tr>`).join("")}
    </table></div>
    <div id="bdetail-pg">${renderPaginationBar({page: CURRENT_BATCH_PAGE, total: totalScans, limit: BATCH_SCANS_PAGE_SIZE})}</div></div>`;
  $$("#bdetail [data-open]").forEach(tr => tr.onclick = () => openScan(tr.dataset.open));
  wirePagination($("#bdetail-pg"), (p) => showBatch(id, p));
  if (done < b.total && !$("#tab-batch").classList.contains("hidden")) pollTimer = setTimeout(() => { showBatch(id, CURRENT_BATCH_PAGE); loadBatches(BATCHES_PAGE); }, 2500);
}

// ---------------- lists ----------------
function scanRows(rows, empty) {
  return `<tr><th>#</th><th>When (UTC)</th><th>Product</th><th>SKU</th><th>Ver.</th><th>File</th><th>Result</th><th>Fail</th><th>Check</th><th>Review</th></tr>` +
    (rows.length ? rows.map(r => `<tr class="click" data-open="${r.id}"><td>${r.id}</td><td>${when(r.created_at)}</td>
      <td>${esc(r.product || "(no name)")}</td><td>${esc(r.sku || "-")}</td><td>v${r.version}</td><td>${esc(r.filename)}</td>
      <td>${r.status === "done" ? `<span class="pill v-${r.verdict}">${esc(VERDICT[r.verdict])}</span>` : `<span class="pill st-${r.status}">${esc(r.status)}</span>`}</td>
      <td>${r.fails ?? ""}</td><td>${r.warns ?? ""}</td><td><span class="pill r-${r.review_status}">${REVIEW[r.review_status]}</span></td></tr>`).join("")
     : `<tr><td colspan="10" class="empty">${empty}</td></tr>`);
}
let qTimer;
$("#q").oninput = () => { clearTimeout(qTimer); qTimer = setTimeout(() => loadHistory(1), 250); };
$("#qrs").onchange = () => loadHistory(1);

let HIST_PAGE = 1;
async function loadHistory(page = 1) {
  HIST_PAGE = page;
  const p = new URLSearchParams({q: $("#q").value, page: HIST_PAGE, limit: 10, paginated: "true"});
  if ($("#qrs").value) p.set("review_status", $("#qrs").value);
  const res = await api("/api/scans?" + p);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#hist").innerHTML = scanRows(rows, "No scans yet.");
  $$("#hist [data-open]").forEach(tr => tr.onclick = () => openScan(tr.dataset.open));
  const pgEl = $("#hist-pg");
  if (pgEl) {
    pgEl.innerHTML = renderPaginationBar({page: HIST_PAGE, total, limit: 10});
    wirePagination(pgEl, loadHistory);
  }
}

let RQ_PAGE = 1;
async function loadReviewQueue(page = 1) {
  RQ_PAGE = page;
  const res = await api(`/api/scans?review_status=in_review&page=${RQ_PAGE}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#rq").innerHTML = scanRows(rows, "Nothing waiting for review.");
  $$("#rq [data-open]").forEach(tr => tr.onclick = () => openScan(tr.dataset.open));
  $("#rq-count").textContent = total || "";
  const pgEl = $("#rq-pg");
  if (pgEl) {
    pgEl.innerHTML = renderPaginationBar({page: RQ_PAGE, total, limit: 10});
    wirePagination(pgEl, loadReviewQueue);
  }
}

async function refreshQueueCount() {
  if (!ME || (ME.role !== "reviewer" && ME.role !== "admin")) {
    $("#rq-count").textContent = "";
    return;
  }
  try {
    const res = await api("/api/scans?review_status=in_review&limit=1&paginated=true");
    const count = res.total ?? (Array.isArray(res) ? res.length : 0);
    $("#rq-count").textContent = count || "";
  } catch (_) {}
}

// ---------------- dashboard ----------------
async function loadDash() {
  const d = await api("/api/dashboard");
  const v = d.by_verdict, r = d.by_review;
  $("#kpis").innerHTML = [
    ["Total scans", d.total], ["Issues found", v.issues_found || 0], ["Manual checks pending", v.needs_review || 0],
    ["In review", r.in_review || 0], ["Approved", r.approved || 0], ["Changes requested", r.changes_requested || 0],
    ["In queue", d.in_queue],
  ].map(([l, n]) => `<div class="card kpi"><div class="v">${n}</div><div class="l">${l}</div></div>`).join("");
  const max = Math.max(1, ...d.top_failing_rules.map(x => x.count));
  $("#bars").innerHTML = d.top_failing_rules.length ? d.top_failing_rules.map(x => `
    <div class="bar"><span>${esc(x.rule)}</span><span class="track"><span class="fill" style="width:${100 * x.count / max}%"></span></span><span>${x.count}</span></div>`).join("")
    : `<div class="empty">No failures recorded yet.</div>`;
}

// ---------------- users / audit / account ----------------
let USERS_PAGE = 1;
async function loadCompany(userPage = 1) {
  const isPlatformAdmin = Boolean(ME && (ME.is_platform_admin || ME.name === "Administrator"));
  const isAdmin = Boolean(ME && (ME.role === "admin" || isPlatformAdmin));
  if (!isAdmin) {
    return;
  }
  USERS_PAGE = userPage;
  const o = await api("/api/org");
  const quota = o.scan_quota === null ? "unlimited" : o.scan_quota;
  const ulim = o.user_limit === null ? "unlimited" : o.user_limit;
  $("#org-kpis").innerHTML = [["Plan", o.plan_label], ["Scans this month", `${o.scans_this_month} / ${quota}`],
    ["Active users", `${o.users} / ${ulim}`]].map(([l, n]) => `<div class="card kpi"><div class="v">${esc(n)}</div><div class="l">${l}</div></div>`).join("");
  $("#o-name").value = o.name; $("#o-ret").value = o.retention_days || ""; $("#o-2fa").checked = o.require_2fa;

  // Strict RBAC: Company settings inputs & buttons only editable to Admin and Platform Admin
  $("#o-name").disabled = !isAdmin;
  $("#o-ret").disabled = !isAdmin;
  $("#o-2fa").disabled = !isAdmin;
  const oSubmit = $("#oform button[type='submit']");
  if (oSubmit) oSubmit.disabled = !isAdmin;

  $("#u-name").disabled = !isAdmin;
  $("#u-email").disabled = !isAdmin;
  $("#u-role").disabled = !isAdmin;
  const uSubmit = $("#uform button[type='submit']");
  if (uSubmit) uSubmit.disabled = !isAdmin;

  const res = await api(`/api/users?page=${USERS_PAGE}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#ulist").innerHTML = `<tr><th>Name</th><th>Email</th><th>Role</th><th>2FA</th><th>Last login</th><th>Status</th><th></th></tr>` + rows.map(u => `
    <tr><td>${esc(u.name)}</td><td>${esc(u.email)}</td>
      <td><select data-role="${u.id}" ${u.id === ME.id || !isAdmin ? "disabled" : ""}>${["uploader","reviewer","admin"].map(r => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
      <td>${u.totp_enabled ? "On" : "Off"}</td><td>${when(u.last_login_at)}</td>
      <td>${u.active ? "Active" : "Disabled"}</td>
      <td class="nowrap">${u.id === ME.id || !isAdmin ? "" : `
        <button class="sbtn" data-toggle="${u.id}" data-active="${u.active}">${u.active ? "Disable" : "Enable"}</button>
        ${!u.last_login_at && u.active ? `<button class="sbtn" data-reinvite="${u.id}">Resend invite</button>` : ""}
        ${u.totp_enabled ? `<button class="sbtn" data-reset2fa="${u.id}">Reset 2FA</button>` : ""}
        <button class="sbtn" data-deluser="${u.id}">Delete</button>`}</td></tr>`).join("");
  const pgEl = $("#ulist-pg");
  if (pgEl) {
    pgEl.innerHTML = renderPaginationBar({page: USERS_PAGE, total, limit: 10});
    wirePagination(pgEl, (p) => loadCompany(p));
  }
  const patch = async (id, body) => {
    if (!isAdmin) { $("#umsg").textContent = "Permission denied: Only Administrators can modify users."; return; }
    try { await api("/api/users/" + id, {method: "PATCH", json: body}); } catch (err) { $("#umsg").textContent = err.message; }
    loadCompany(USERS_PAGE);
  };
  $$("[data-role]").forEach(x => x.onchange = () => patch(x.dataset.role, {role: x.value}));
  $$("[data-toggle]").forEach(b => b.onclick = () => patch(b.dataset.toggle, {active: b.dataset.active !== "true"}));
  $$("[data-reset2fa]").forEach(b => b.onclick = () => patch(b.dataset.reset2fa, {reset_2fa: true}));
  $$("[data-reinvite]").forEach(b => b.onclick = async () => {
    if (!isAdmin) { $("#umsg").textContent = "Permission denied."; return; }
    try { const r = await api(`/api/users/${b.dataset.reinvite}/invite`, {method: "POST"}); showLink("#umsg", "Invite sent again.", r.invite_url); }
    catch (err) { $("#umsg").textContent = err.message; }
  });
  $$("[data-deluser]").forEach(b => b.onclick = async () => {
    if (!isAdmin) { $("#umsg").textContent = "Permission denied."; return; }
    if (!confirmTwice(b)) return;
    try { await api("/api/users/" + b.dataset.deluser, {method: "DELETE"}); } catch (err) { $("#umsg").textContent = err.message; }
    loadCompany(USERS_PAGE);
  });
  loadKeys();
  loadHooks();
  loadBilling(o);
  const emailSec = $("#email-notif-section");
  if (emailSec) {
    emailSec.classList.toggle("hidden", !isPlatformAdmin);
  }
  if ($("#email-status-box")) {
    if (META.email_enabled) {
      $("#email-status-box").innerHTML = `<span style="color:#16a34a; font-weight:600;">✓ SMTP is active</span> — outgoing emails (user invites, password resets, review approvals, batch notifications) are sent automatically.`;
    } else {
      $("#email-status-box").innerHTML = `<span style="color:#d97706; font-weight:600;">⚠ No SMTP server configured</span> (development mode) — outgoing emails are logged to server output, and invites/reset links appear on screen. To send real emails, configure <code>LABELCHECK_SMTP_HOST</code>.`;
    }
  }
  const testMailBtn = $("#send-test-email");
  if (testMailBtn) {
    testMailBtn.onclick = async () => {
      const msgEl = $("#test-email-msg");
      testMailBtn.disabled = true;
      msgEl.textContent = "Sending test email…";
      msgEl.style.color = "#64748b";
      try {
        const res = await api("/api/org/test-email", {method: "POST"});
        msgEl.textContent = res.message || "Test email sent successfully!";
        msgEl.style.color = "#16a34a";
      } catch (err) {
        msgEl.textContent = err.message || "Failed to send test email.";
        msgEl.style.color = "#dc2626";
      } finally {
        testMailBtn.disabled = false;
      }
    };
  }
}

// ---------------- billing & razorpay ----------------
let BILLING_PAGE = 1;
async function loadBilling(org) {
  if (!$("#billing-card")) return;
  const isEligible = Boolean(ME && (ME.role === "admin" || ME.is_platform_admin || ME.name === "Administrator"));
  if (!isEligible) {
    $("#billing-card").classList.add("hidden");
    return;
  }
  $("#billing-card").classList.remove("hidden");
  const plan = org.plan || "trial";
  const quota = org.scan_quota === null ? "unlimited" : org.scan_quota;
  const ulim = org.user_limit === null ? "unlimited" : org.user_limit;
  const isTrial = plan === "trial";
  const isStandard = plan === "standard";
  const isBusiness = plan === "business";

  $("#billing-current-info").innerHTML = `
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
      <span style="font-size:15px; font-weight:700;">Current Plan:</span>
      <span class="pill" style="font-weight:700; ${isTrial ? 'background:#e2e8f0; color:#334155;' : (isStandard ? 'background:#dbeafe; color:#1d4ed8;' : 'background:#fef3c7; color:#b45309;')}">${esc(org.plan_label || plan.toUpperCase())}</span>
      ${isTrial ? '<span style="color:#16a34a; font-weight:600; font-size:12px;">(Free Tier)</span>' : ''}
    </div>
    <div><b>Monthly scan quota:</b> ${quota} scans &nbsp;·&nbsp; <b>Team seats:</b> up to ${ulim} users</div>
  `;

  if ($("#btn-upgrade-standard")) {
    $("#btn-upgrade-standard").textContent = isStandard ? "Active Plan (Standard)" : "Upgrade to Standard (₹4,999/mo)";
    $("#btn-upgrade-standard").disabled = isStandard;
    $("#btn-upgrade-standard").onclick = () => checkoutRazorpay("standard");
  }
  if ($("#btn-upgrade-business")) {
    $("#btn-upgrade-business").textContent = isBusiness ? "Active Plan (Business)" : "Upgrade to Business (₹14,999/mo)";
    $("#btn-upgrade-business").disabled = isBusiness;
    $("#btn-upgrade-business").onclick = () => checkoutRazorpay("business");
  }

  if ($("#btn-buy-pack1")) $("#btn-buy-pack1").onclick = () => checkoutRazorpay("pack_1");
  if ($("#btn-buy-pack50")) $("#btn-buy-pack50").onclick = () => checkoutRazorpay("pack_50");
  if ($("#btn-buy-pack250")) $("#btn-buy-pack250").onclick = () => checkoutRazorpay("pack_250");

  await loadBillingHistory();
}

async function loadBillingHistory(page = 1) {
  BILLING_PAGE = page;
  try {
    const res = await api(`/api/billing/history?page=${BILLING_PAGE}&limit=5&paginated=true`);
    const rows = res.items || (Array.isArray(res) ? res : []);
    const total = res.total ?? rows.length;
    $("#billing-history-table").innerHTML = rows.length
      ? `<tr><th>Date</th><th>Plan / Item</th><th>Amount</th><th>Status</th><th>Receipt / ID</th></tr>` + rows.map(p => `
        <tr>
          <td>${when(p.created_at)}</td>
          <td><b>${esc(p.plan)}</b></td>
          <td style="color:#16a34a; font-weight:600;">₹${((p.amount_paise || 0) / 100).toLocaleString("en-IN")}</td>
          <td><span class="pill" style="${p.status === 'paid' ? 'background:#dcfce7; color:#15803d;' : 'background:#fee2e2; color:#b91c1c;'}">${esc(p.status.toUpperCase())}</span></td>
          <td style="font-family:monospace; font-size:12px;">${esc(p.payment_id || p.order_id)}</td>
        </tr>`).join("")
      : `<tr><td class="empty" colspan="5">No payment history yet.</td></tr>`;
    const pgEl = $("#billing-history-pg");
    if (pgEl) {
      pgEl.innerHTML = renderPaginationBar({page: BILLING_PAGE, total, limit: 5});
      wirePagination(pgEl, loadBillingHistory);
    }
  } catch (_) {}
}

async function checkoutRazorpay(plan) {
  const isEligible = Boolean(ME && (ME.role === "admin" || ME.is_platform_admin || ME.name === "Administrator"));
  const msgEl = $("#billing-msg") || $("#su-msg");
  if (!isEligible) {
    if (msgEl) {
      msgEl.textContent = "Only Company Administrators and Platform Administrators can upgrade plans.";
      msgEl.style.color = "#dc2626";
    }
    return;
  }
  if (msgEl) {
    msgEl.textContent = "Connecting to Razorpay checkout…";
    msgEl.style.color = "var(--text-dim)";
  }
  try {
    const order = await api("/api/billing/create-order", {method: "POST", json: {plan}});

    if (typeof Razorpay === "undefined") {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://checkout.razorpay.com/v1/checkout.js";
        s.onload = resolve;
        s.onerror = () => reject(new Error("Unable to load Razorpay checkout script. Please check your internet connection."));
        document.head.appendChild(s);
      });
    }

    const options = {
      key: order.key_id,
      amount: order.amount,
      currency: order.currency || "INR",
      name: "LabelCheck",
      description: order.description,
      order_id: order.order_id,
      handler: async function (response) {
        try {
          if (msgEl) { msgEl.textContent = "Verifying payment with Razorpay…"; msgEl.style.color = "var(--text-dim)"; }
          const verify = await api("/api/billing/verify-payment", {
            method: "POST",
            json: {
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
              plan: plan
            }
          });
          if (msgEl) { msgEl.textContent = verify.message; msgEl.style.color = "#16a34a"; }
          await start();
          loadCompany();
        } catch (err) {
          if (msgEl) { msgEl.textContent = "Verification failed: " + err.message; msgEl.style.color = "#dc2626"; }
        }
      },
      modal: {
        ondismiss: function() {
          if (msgEl) {
            msgEl.textContent = "Razorpay checkout closed. Plan was not upgraded.";
            msgEl.style.color = "var(--text-dim)";
          }
        }
      },
      prefill: {
        name: ME ? ME.name : "",
        email: ME ? ME.email : ""
      },
      theme: {
        color: "#2563eb"
      }
    };

    const rzp = new Razorpay(options);
    rzp.on("payment.failed", function (resp) {
      if (msgEl) {
        msgEl.textContent = "Payment failed: " + (resp.error ? resp.error.description : "Transaction declined");
        msgEl.style.color = "#dc2626";
      }
    });

    if (msgEl) {
      msgEl.textContent = "Redirecting to Razorpay checkout…";
      msgEl.style.color = "var(--text-dim)";
    }
    rzp.open();
  } catch (err) {
    if (msgEl) {
      msgEl.textContent = err.message || "Failed to initialize Razorpay checkout.";
      msgEl.style.color = "#dc2626";
    }
  }
}

function showLink(sel, msg, url) {
  $(sel).innerHTML = `${esc(msg)} ${META.email_enabled ? "" : "E-mail is not set up on this server, so share this link yourself:"}
    <br><input type="text" readonly value="${esc(url)}" class="copy">`;
}
$("#uform").onsubmit = async e => {
  e.preventDefault();
  const isPlatformAdmin = Boolean(ME && (ME.is_platform_admin || ME.name === "Administrator"));
  const isAdmin = Boolean(ME && (ME.role === "admin" || isPlatformAdmin));
  if (!isAdmin) {
    $("#umsg").textContent = "Permission denied: Only Administrators can invite users.";
    $("#umsg").style.color = "#dc2626";
    return;
  }
  try {
    const r = await api("/api/users", {method: "POST", json: {name: $("#u-name").value, email: $("#u-email").value, role: $("#u-role").value}});
    showLink("#umsg", "Invite sent.", r.invite_url); $("#uform").reset(); loadCompany();
  } catch (err) { $("#umsg").textContent = err.message; }
};
$("#oform").onsubmit = async e => {
  e.preventDefault();
  const isPlatformAdmin = Boolean(ME && (ME.is_platform_admin || ME.name === "Administrator"));
  const isAdmin = Boolean(ME && (ME.role === "admin" || isPlatformAdmin));
  if (!isAdmin) {
    $("#omsg").textContent = "Permission denied: Only Administrators can edit company settings.";
    $("#omsg").style.color = "#dc2626";
    return;
  }
  try {
    await api("/api/org", {method: "PATCH", json: {name: $("#o-name").value, retention_days: Number($("#o-ret").value || 0), require_2fa: $("#o-2fa").checked}});
    $("#omsg").textContent = "Saved."; loadCompany();
  } catch (err) { $("#omsg").textContent = err.message; }
};
let KEYS_PAGE = 1;
async function loadKeys(page = 1) {
  KEYS_PAGE = page;
  const res = await api(`/api/api-keys?page=${KEYS_PAGE}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#klist").innerHTML = rows.length ? `<tr><th>Name</th><th>Key starts</th><th>Scopes</th><th>Created by</th><th>Last used</th><th></th></tr>` + rows.map(k => `
    <tr><td>${esc(k.name)}</td><td><code>lc_${esc(k.prefix)}_…</code></td><td>${esc(k.scopes)}</td><td>${esc(k.created_by_name)}</td>
      <td>${when(k.last_used_at)}</td><td>${k.revoked_at ? "Revoked" : `<button class="sbtn" data-revoke="${k.id}">Revoke</button>`}</td></tr>`).join("")
    : `<tr><td class="empty">No API keys yet.</td></tr>`;
  const pgEl = $("#klist-pg");
  if (pgEl) {
    pgEl.innerHTML = renderPaginationBar({page: KEYS_PAGE, total, limit: 10});
    wirePagination(pgEl, loadKeys);
  }
  $$("[data-revoke]").forEach(b => b.onclick = async () => {
    if (!confirmTwice(b)) return;
    await api("/api/api-keys/" + b.dataset.revoke, {method: "DELETE"}); loadKeys(KEYS_PAGE);
  });
}
$("#kform").onsubmit = async e => {
  e.preventDefault();
  const isPlatformAdmin = Boolean(ME && (ME.is_platform_admin || ME.name === "Administrator"));
  const isAdmin = Boolean(ME && (ME.role === "admin" || isPlatformAdmin));
  if (!isAdmin) {
    $("#k-new").innerHTML = `<p class="err">Permission denied: Only Administrators can create API keys.</p>`;
    return;
  }
  try {
    const r = await api("/api/api-keys", {method: "POST", json: {name: $("#k-name").value, scopes: $("#k-scope").value === "r" ? ["read"] : ["read", "write"]}});
    $("#k-new").innerHTML = `<p class="note"><b>Copy this key now. It will not be shown again.</b><br><input type="text" readonly class="copy" value="${esc(r.key)}"></p>`;
    $("#k-name").value = ""; loadKeys();
  } catch (err) { $("#k-new").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
};

// ---------------- webhooks ----------------
let HOOK_EVENTS = null;
const DSTATUS = {pending: "manual", sending: "manual", delivered: "pass", failed: "fail"};
async function loadHooks() {
  HOOK_EVENTS = HOOK_EVENTS || await api("/api/webhooks/events");
  if (!$("#wh-events").children.length) {
    $("#wh-events").innerHTML = Object.entries(HOOK_EVENTS).map(([k, d]) => `
      <div class="check"><input type="checkbox" id="whe-${esc(k)}" value="${esc(k)}" ${k === "scan.completed" || k.startsWith("review.") ? "checked" : ""}>
        <label for="whe-${esc(k)}" style="margin:0;color:var(--ink)"><code>${esc(k)}</code> · ${esc(d)}</label></div>`).join("");
  }
  const rows = await api("/api/webhooks");
  $("#whlist").innerHTML = rows.length ? `<tr><th>URL</th><th>Events</th><th>Status</th><th>Last delivery</th><th></th></tr>` + rows.map(h => {
    const ld = h.last_delivery;
    return `<tr><td style="overflow-wrap:anywhere">${esc(h.url)}${h.description ? `<div class="meta">${esc(h.description)}</div>` : ""}</td>
      <td>${h.events.map(e => `<code>${esc(e)}</code>`).join("<br>")}</td>
      <td>${h.active ? `<span class="pill s-pass">On</span>` : `<span class="pill s-fail">Off</span>${h.disabled_reason ? `<div class="meta">${esc(h.disabled_reason)}</div>` : ""}`}</td>
      <td>${ld ? `<span class="pill s-${DSTATUS[ld.status]}">${esc(ld.status)}</span> ${ld.last_code ? "HTTP " + ld.last_code : ""}<div class="meta">${when(ld.created_at)}</div>` : "-"}</td>
      <td><div class="decide"><button class="sbtn" data-whtest="${h.id}">Send test</button>
        <button class="sbtn" data-whlog="${h.id}">Deliveries</button>
        <button class="sbtn" data-whtoggle="${h.id}" data-active="${h.active}">${h.active ? "Turn off" : "Turn on"}</button>
        <button class="sbtn" data-whrotate="${h.id}">New secret</button>
        <button class="sbtn" data-whdel="${h.id}">Delete</button></div></td></tr>`;
  }).join("") : `<tr><td class="empty">No webhooks yet.</td></tr>`;
  const act = (sel, fn) => $$(sel).forEach(b => b.onclick = async () => {
    try { await fn(b); } catch (err) { $("#wh-new").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
  });
  act("[data-whtest]", async b => {
    await api(`/api/webhooks/${b.dataset.whtest}/test`, {method: "POST"});
    $("#wh-new").innerHTML = `<p class="note">Test event queued. It is sent within a few seconds; open Deliveries to see the result.</p>`;
  });
  act("[data-whtoggle]", async b => { await api(`/api/webhooks/${b.dataset.whtoggle}`, {method: "PATCH", json: {active: b.dataset.active !== "true"}}); loadHooks(); });
  act("[data-whrotate]", async b => {
    if (!confirmTwice(b)) return;
    const r = await api(`/api/webhooks/${b.dataset.whrotate}/rotate-secret`, {method: "POST"});
    showSecret(r.secret, "The old secret stopped working. Update your receiver now.");
  });
  act("[data-whdel]", async b => {
    if (!confirmTwice(b)) return;
    await api(`/api/webhooks/${b.dataset.whdel}`, {method: "DELETE"}); $("#wh-deliveries").innerHTML = ""; loadHooks();
  });
  act("[data-whlog]", b => showDeliveries(b.dataset.whlog));
}
function showSecret(secret, msg) {
  $("#wh-new").innerHTML = `<p class="note"><b>Signing secret: copy it now, it will not be shown again.</b> ${esc(msg || "")}<br>
    <input type="text" readonly class="copy" value="${esc(secret)}"></p>`;
}
async function showDeliveries(id, page = 1) {
  const res = await api(`/api/webhooks/${id}/deliveries?page=${page}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#wh-deliveries").innerHTML = `<div class="panel"><h3>Last deliveries</h3><div class="tablewrap"><table>
    <tr><th>When</th><th>Event</th><th>Status</th><th>Tries</th><th>Result</th><th></th></tr>
    ${rows.length ? rows.map(d => `<tr><td>${when(d.created_at)}</td><td><code>${esc(d.event)}</code></td>
      <td><span class="pill s-${DSTATUS[d.status]}">${esc(d.status)}</span>${d.status === "pending" && d.attempts ? `<div class="meta">next try ${when(d.next_attempt_at)}</div>` : ""}</td>
      <td>${d.attempts}</td><td style="overflow-wrap:anywhere">${d.last_code ? "HTTP " + d.last_code : ""} ${esc(d.last_error || "")}</td>
      <td>${["delivered", "failed"].includes(d.status) ? `<button class="sbtn" data-redeliver="${d.id}">Send again</button>` : ""}</td></tr>`).join("")
      : `<tr><td colspan="6" class="empty">No deliveries yet.</td></tr>`}</table></div>
    <div id="wh-deliv-pg">${renderPaginationBar({page, total, limit: 10})}</div></div>`;
  wirePagination($("#wh-deliv-pg"), (p) => showDeliveries(id, p));
  $$("[data-redeliver]").forEach(b => b.onclick = async () => {
    try { await api(`/api/webhooks/${id}/deliveries/${b.dataset.redeliver}/retry`, {method: "POST"}); showDeliveries(id, page); }
    catch (err) { $("#wh-new").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
  });
}
$("#whform").onsubmit = async e => {
  e.preventDefault();
  const isPlatformAdmin = Boolean(ME && (ME.is_platform_admin || ME.name === "Administrator"));
  const isAdmin = Boolean(ME && (ME.role === "admin" || isPlatformAdmin));
  if (!isAdmin) {
    $("#wh-new").innerHTML = `<p class="err">Permission denied: Only Administrators can configure webhooks.</p>`;
    return;
  }
  const events = $$("#wh-events input:checked").map(x => x.value);
  try {
    const r = await api("/api/webhooks", {method: "POST", json: {url: $("#wh-url").value, events, description: $("#wh-desc").value}});
    showSecret(r.secret, "Use it to verify the X-LabelCheck-Signature header.");
    $("#wh-url").value = ""; $("#wh-desc").value = ""; loadHooks();
  } catch (err) { $("#wh-new").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
};

// ---------------- platform admin ----------------
let PF_PAGE = 1;
async function loadPlatform(page = 1) {
  PF_PAGE = page;
  $("#pf-plan").innerHTML = Object.entries(META.plans).map(([k, p]) => `<option value="${esc(k)}">${esc(p.label)}</option>`).join("");
  const res = await api(`/api/platform/orgs?page=${PF_PAGE}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#pf-list").innerHTML = `<tr><th>#</th><th>Company</th><th>Plan</th><th>Scans this month</th><th>Users</th><th>Status</th><th></th></tr>` + rows.map(o => `
    <tr><td>${o.id}</td><td>${esc(o.name)}</td>
      <td><select data-plan="${o.id}">${Object.entries(META.plans).map(([k, p]) => `<option value="${esc(k)}" ${k === o.plan ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</select></td>
      <td>${o.scans_this_month} / ${o.scan_quota ?? "∞"}</td><td>${o.users} / ${o.user_limit ?? "∞"}</td>
      <td>${o.active ? "Active" : "Disabled"}</td>
      <td>${o.id === ME.org_id ? "" : `<button class="sbtn" data-orgact="${o.id}" data-active="${o.active}">${o.active ? "Disable" : "Enable"}</button>`}</td></tr>`).join("");
  const pgEl = $("#pf-pg");
  if (pgEl) {
    pgEl.innerHTML = renderPaginationBar({page: PF_PAGE, total, limit: 10});
    wirePagination(pgEl, loadPlatform);
  }
  const patch = async (id, body) => { try { await api("/api/platform/orgs/" + id, {method: "PATCH", json: body}); } catch (err) { $("#pf-msg").textContent = err.message; } loadPlatform(PF_PAGE); };
  $$("[data-plan]").forEach(x => x.onchange = () => patch(x.dataset.plan, {plan: x.value, scan_quota: -1, user_limit: -1}));
  $$("[data-orgact]").forEach(b => b.onclick = () => { if (confirmTwice(b)) patch(b.dataset.orgact, {active: b.dataset.active !== "true"}); });
}
$("#pf-form").onsubmit = async e => {
  e.preventDefault();
  try {
    const r = await api("/api/platform/orgs", {method: "POST", json: {name: $("#pf-name").value, plan: $("#pf-plan").value,
      admin_name: $("#pf-aname").value, admin_email: $("#pf-aemail").value}});
    showLink("#pf-msg", `Company #${r.id} created; invite sent to its admin.`, r.invite_url); $("#pf-form").reset(); loadPlatform();
  } catch (err) { $("#pf-msg").textContent = err.message; }
};

// ---------------- account: 2FA + notifications ----------------
function renderTfa() {
  $("#tfa-body").innerHTML = ME.totp_enabled
    ? `<p>Two-step login is <b>on</b>. You need a code from your authenticator app at every login.</p>
       ${ME.org_requires_2fa ? `<p class="hint">Your company requires it, so it cannot be turned off.</p>` : `
       <label for="t-pass">Password</label><input type="password" id="t-pass" autocomplete="current-password">
       <label for="t-code">Current 6-digit code</label><input type="text" id="t-code" inputmode="numeric" maxlength="6">
       <button class="btn ghost" id="t-off">Turn off</button>`}<div class="hint" id="t-msg"></div>`
    : `<p>Protect your account with a code from an app such as Google Authenticator or Microsoft Authenticator.</p>
       <button class="btn" id="t-setup">Set up two-step login</button><div id="t-step"></div><div class="hint" id="t-msg"></div>`;
  if ($("#t-setup")) $("#t-setup").onclick = async () => {
    try {
      const r = await api("/api/auth/2fa/setup", {method: "POST"});
      const formattedKey = (r.secret.match(/.{1,4}/g) || [r.secret]).join(" ");
      $("#t-step").innerHTML = `
        <p>1. Scan this QR code in your authenticator app (Google Authenticator, Microsoft Authenticator, Apple Passwords, Authy, etc.):</p>
        <div class="qr">${r.qr_svg}</div>
        <p class="hint">Or enter this key manually: <code>${esc(formattedKey)}</code>
          <button type="button" class="sbtn" id="copy-tfa-key" style="margin-left:8px;">Copy key</button>
        </p>
        <div style="margin: 8px 0 14px;"><a href="${esc(r.otpauth_uri)}" class="sbtn" style="text-decoration:none;">Open directly in Authenticator app</a></div>
        <label for="t-new">2. Enter the 6-digit code shown in your app</label>
        <input type="text" id="t-new" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="123456" style="max-width:200px;">
        <button class="btn" id="t-on" style="margin-top:10px;">Turn on</button>`;
      const copyBtn = $("#copy-tfa-key");
      if (copyBtn) copyBtn.onclick = () => {
        if (navigator.clipboard) navigator.clipboard.writeText(r.secret);
        copyBtn.textContent = "Copied!";
        setTimeout(() => { copyBtn.textContent = "Copy key"; }, 2000);
      };
      $("#t-on").onclick = async () => {
        const code = ($("#t-new").value || "").replace(/\s|-/g, "");
        try { await api("/api/auth/2fa/enable", {method: "POST", json: {code}}); ME = await api("/api/auth/me"); renderTfa(); start(); }
        catch (err) { $("#t-msg").textContent = err.message; }
      };
    } catch (err) { $("#t-msg").textContent = err.message; }
  };
  if ($("#t-off")) $("#t-off").onclick = async () => {
    try { await api("/api/auth/2fa/disable", {method: "POST", json: {password: $("#t-pass").value, code: $("#t-code").value}}); ME = await api("/api/auth/me"); renderTfa(); }
    catch (err) { $("#t-msg").textContent = err.message; }
  };
}
$("#notify").onchange = async () => { try { await api("/api/auth/preferences", {method: "POST", json: {notify: $("#notify").checked}}); } catch (_) {} };

let AUDIT_PAGE = 1;
async function loadAudit(page = 1) {
  AUDIT_PAGE = page;
  const res = await api(`/api/audit?page=${AUDIT_PAGE}&limit=10&paginated=true`);
  const rows = res.items || (Array.isArray(res) ? res : []);
  const total = res.total ?? rows.length;
  $("#alist").innerHTML = `<tr><th>When (UTC)</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th><th>IP</th></tr>` + rows.map(a => `
    <tr><td>${when(a.created_at)}</td><td>${esc(a.user_name || "-")}</td><td>${esc(a.action.replace(/_/g, " "))}</td>
    <td>${a.target_type === "scan" ? `<a href="#" data-open="${a.target_id}">scan #${a.target_id}</a>` : esc((a.target_type || "") + (a.target_id ? " #" + a.target_id : ""))}</td>
    <td>${esc(fmtDetail(a.detail))}</td><td>${esc(a.ip || "")}</td></tr>`).join("");
  const pgEl = $("#alist-pg");
  if (pgEl) {
    pgEl.innerHTML = renderPaginationBar({page: AUDIT_PAGE, total, limit: 10});
    wirePagination(pgEl, loadAudit);
  }
  $$("#alist [data-open]").forEach(x => x.onclick = e => { e.preventDefault(); openScan(x.dataset.open); });
}
$("#pform").onsubmit = async e => {
  e.preventDefault();
  try {
    await api("/api/auth/password", {method: "POST", json: {current: $("#p-cur").value, new: $("#p-new").value}});
    $("#pmsg").textContent = "Password changed. Please log in again."; setTimeout(showLogin, 1200);
  } catch (err) { $("#pmsg").textContent = err.message; }
};

window.addEventListener("hashchange", async () => {
  if (isInitializing) return;
  const handled = await route();
  if (!handled) {
    if (ME) {
      showOnly("app-view");
      const m = location.hash.match(/^#\/(scan|batch)\/(\d+)/);
      if (m && m[1] === "scan") openScan(m[2]);
      else if (m && m[1] === "batch") { openTab("batch"); showBatch(m[2]); }
    } else {
      const ok = await rehydrateSession();
      if (ok) await start();
      else showHome();
    }
  }
});

async function rehydrateSession() {
  const token = getStoredToken();
  try {
    const u = await api("/api/auth/me");
    if (u && u.id) {
      ME = u;
      setSessionAuth(token || getStoredToken(), u);
      return true;
    }
  } catch (_) {
    clearSessionAuth(false);
  }
  return false;
}

async function initApp() {
  initTheme();
  isInitializing = true;
  showOnly("auth-loading");

  const hasSession = await rehydrateSession();
  isInitializing = false;

  const handled = await route();
  if (!handled) {
    if (hasSession) {
      await start();
    } else {
      showHome();
    }
  }
}

initApp().catch(() => {
  isInitializing = false;
  showHome();
});
