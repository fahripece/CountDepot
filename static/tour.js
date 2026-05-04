const context = window.COUNTDEPOT_TOUR_CONTEXT || {};
const csrfToken = context.csrfToken || "";

const role = (() => {
  if (context.isAdminLike || context.role === "admin") return "admin";
  if (context.role === "client_viewer" || context.role === "viewer") return "client_viewer";
  return "worker";
})();

const TOUR_STEPS = {
  admin: [
    { path: "/categories", target: "add-category", headline: "Start with Categories", copy: "Create groupings like Electronics or Tools so your inventory stays organized from day one." },
    { path: "/products", target: "add-product", headline: "Build your Product Templates", copy: "Add reusable product records here so adding inventory items later is fast and consistent." },
    { path: "/items/add", target: "item-name-field", headline: "Add Your First Item", copy: "This is where individual tracked items live — scan a barcode or type a name to add your first one now." },
    { path: "/sites", target: "add-site", headline: "Set Up Your Locations", copy: "Sites let you track where each item lives — a room, office, van, or warehouse." },
    { path: "/reservations", target: "create-reservation", headline: "Manage Reservations", copy: "Reserve items for people or projects in advance so nothing goes missing or gets double-booked." },
    { path: "/docs", target: "sops-list", headline: "SOPs and Docs", copy: "Store your team's procedures and documents here so everyone follows the same process." },
  ],
  worker: [
    { path: "/items", target: "inventory-list", headline: "Your Inventory", copy: "Every item your team tracks lives here. Search, filter, or scan to find anything fast." },
    { path: "/items/add", target: "item-name-field", headline: "Add an Item", copy: "Use this form to add new items. Scan a barcode or type the name — it takes under a minute." },
    { path: "/reservations", target: "create-reservation", headline: "Reserve Items", copy: "Need something for a job or project? Reserve it here so it's held for you." },
    { path: "/docs", target: "sops-list", headline: "Procedures and Docs", copy: "Find your team's SOPs and reference docs here before starting any task." },
  ],
  client_viewer: [
    { path: "/items", target: "inventory-list", headline: "Your Inventory View", copy: "This shows all items at your site. Use search or filters to find what you need." },
    { path: "/sites", target: "site-list", headline: "Your Sites", copy: "Switch between locations here if you have access to more than one site." },
  ],
};

const TOUR_END_SCREENS = {
  admin: {
    headline: "You're ready to start.",
    copy: "Use the next action that matches your setup flow.",
    actions: [
      { href: "/items/add", label: "Add your first item →", primary: true },
      { href: "/sites", label: "Set up a site →", primary: false },
    ],
  },
  worker: {
    headline: "You're ready to work.",
    copy: "Start by adding or finding the first item you need.",
    actions: [
      { href: "/items/add", label: "Add your first item →", primary: true },
    ],
  },
  client_viewer: {
    headline: "You're ready to browse inventory.",
    copy: "Open the inventory view and start searching what is available at your site.",
    actions: [
      { href: "/items", label: "View your inventory →", primary: true },
    ],
  },
};

const steps = TOUR_STEPS[role] || TOUR_STEPS.worker;
const endScreen = TOUR_END_SCREENS[role] || TOUR_END_SCREENS.worker;

let root;
let backdrop;
let spotlight;
let callout;
let choice;
let endCard;
let rafHandle = null;
let resizeBound = null;

function ensureStyles() {
  if (document.getElementById("countdepot-tour-styles")) return;
  const style = document.createElement("style");
  style.id = "countdepot-tour-styles";
  style.textContent = `
    .cd-tour-root{position:fixed;inset:0;z-index:12000;display:none}
    .cd-tour-root.active{display:block}
    .cd-tour-backdrop{position:absolute;inset:0;background:rgba(15,23,42,.32)}
    .cd-tour-spotlight{position:fixed;display:none;border:2px solid #6442D6;border-radius:18px;box-shadow:0 0 0 9999px rgba(15,23,42,.48),0 24px 48px rgba(15,23,42,.28);pointer-events:none;z-index:12001}
    .cd-tour-target{position:relative;z-index:12002!important}
    .cd-tour-card,.cd-tour-choice,.cd-tour-end{position:fixed;background:#fff;border:1px solid #e2e8f0;border-radius:20px;box-shadow:0 24px 64px rgba(15,23,42,.28);color:#111827;z-index:12003}
    .cd-tour-card{width:min(360px,calc(100vw - 24px));padding:18px;display:none}
    .cd-tour-choice,.cd-tour-end{width:min(520px,calc(100vw - 24px));left:50%;top:50%;transform:translate(-50%,-50%);padding:26px;display:none}
    .cd-tour-visible{display:block}
    .cd-tour-arrow{position:absolute;width:16px;height:16px;background:#fff;border-left:1px solid #e2e8f0;border-top:1px solid #e2e8f0;transform:rotate(45deg)}
    .cd-tour-step{font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#6442D6;margin-bottom:10px}
    .cd-tour-title{font-family:Roboto,Inter,sans-serif;font-size:24px;line-height:1.08;letter-spacing:-.04em;margin:0 0 10px}
    .cd-tour-copy{font-size:14px;line-height:1.6;color:#64748B;margin:0}
    .cd-tour-progress{margin:16px 0 12px}
    .cd-tour-progress-row{display:flex;align-items:center;justify-content:space-between;font-size:12px;color:#64748B;margin-bottom:8px}
    .cd-tour-progress-bar{height:6px;background:#E2E8F0;border-radius:999px;overflow:hidden}
    .cd-tour-progress-bar span{display:block;height:100%;background:#6442D6;border-radius:999px}
    .cd-tour-actions{display:flex;align-items:center;gap:10px;justify-content:space-between;margin-top:16px}
    .cd-tour-actions-right{display:flex;align-items:center;gap:10px;margin-left:auto}
    .cd-tour-link{background:none;border:none;color:#64748B;font-size:13px;font-weight:700;cursor:pointer;padding:0}
    .cd-tour-link:hover{color:#111827}
    .cd-tour-btn{display:inline-flex;align-items:center;justify-content:center;min-height:40px;padding:9px 16px;border-radius:999px;border:1px solid #e2e8f0;background:#fff;color:#111827;font-size:13px;font-weight:800;cursor:pointer;text-decoration:none}
    .cd-tour-btn-primary{background:#6442D6;border-color:#6442D6;color:#fff}
    .cd-tour-btn-primary:hover{background:#4f2fbd;border-color:#4f2fbd}
    .cd-tour-choice-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
    .cd-tour-choice-actions .cd-tour-btn{flex:1 1 180px}
    .cd-tour-choice-subtle{display:block;margin-top:10px;text-align:center}
    .cd-tour-end-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
    @media (max-width: 780px){
      .cd-tour-card{left:12px!important;right:12px!important;top:auto!important;bottom:12px!important;width:auto}
      .cd-tour-arrow{display:none}
      .cd-tour-choice,.cd-tour-end{width:min(520px,calc(100vw - 24px))}
    }
  `;
  document.head.appendChild(style);
}

function ensureDom() {
  if (root) return;
  ensureStyles();
  root = document.createElement("div");
  root.className = "cd-tour-root";
  root.innerHTML = `
    <div class="cd-tour-backdrop"></div>
    <div class="cd-tour-spotlight" aria-hidden="true"></div>
    <section class="cd-tour-card" role="dialog" aria-modal="true">
      <div class="cd-tour-arrow" aria-hidden="true"></div>
      <div class="cd-tour-step"></div>
      <h2 class="cd-tour-title"></h2>
      <p class="cd-tour-copy"></p>
      <div class="cd-tour-progress">
        <div class="cd-tour-progress-row">
          <span class="cd-tour-progress-label"></span>
          <span class="cd-tour-progress-fraction"></span>
        </div>
        <div class="cd-tour-progress-bar"><span></span></div>
      </div>
      <div class="cd-tour-actions">
        <button class="cd-tour-link cd-tour-back" type="button">Back</button>
        <div class="cd-tour-actions-right">
          <button class="cd-tour-link cd-tour-skip" type="button">Skip tour</button>
          <button class="cd-tour-btn cd-tour-btn-primary cd-tour-next" type="button">Next →</button>
        </div>
      </div>
    </section>
    <section class="cd-tour-choice" role="dialog" aria-modal="true">
      <div class="cd-tour-step">First login guide</div>
      <h2 class="cd-tour-title">Welcome to CountDepot — want a quick tour?</h2>
      <p class="cd-tour-copy">We'll walk you through the 6 key areas in about 2 minutes.</p>
      <div class="cd-tour-choice-actions">
        <button class="cd-tour-btn cd-tour-btn-primary cd-tour-start" type="button">Start tour →</button>
        <button class="cd-tour-btn cd-tour-remind" type="button">Remind me later</button>
      </div>
      <button class="cd-tour-link cd-tour-choice-subtle cd-tour-skip-forever" type="button">I'll figure it out myself</button>
    </section>
    <section class="cd-tour-end" role="dialog" aria-modal="true">
      <div class="cd-tour-step">Tour complete</div>
      <h2 class="cd-tour-title"></h2>
      <p class="cd-tour-copy"></p>
      <div class="cd-tour-end-actions"></div>
    </section>
  `;
  document.body.appendChild(root);
  backdrop = root.querySelector(".cd-tour-backdrop");
  spotlight = root.querySelector(".cd-tour-spotlight");
  callout = root.querySelector(".cd-tour-card");
  choice = root.querySelector(".cd-tour-choice");
  endCard = root.querySelector(".cd-tour-end");

  root.querySelector(".cd-tour-start").addEventListener("click", () => startTour(false));
  root.querySelector(".cd-tour-remind").addEventListener("click", async () => {
    await updateStatus("remind_later");
    hideAll();
  });
  root.querySelector(".cd-tour-skip-forever").addEventListener("click", async () => {
    await updateStatus("skipped");
    hideAll();
  });
  root.querySelector(".cd-tour-back").addEventListener("click", () => moveStep(-1));
  root.querySelector(".cd-tour-next").addEventListener("click", () => moveStep(1));
  root.querySelector(".cd-tour-skip").addEventListener("click", async () => {
    await updateStatus("skipped");
    clearTourQuery();
    hideAll();
  });
}

function currentPath() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path || "/";
}

function tourStepIndexFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const active = params.get("tour");
  const raw = parseInt(params.get("tour_step") || "", 10);
  if (active !== "1" || Number.isNaN(raw)) return null;
  return Math.max(0, Math.min(steps.length - 1, raw));
}

function withTourQuery(path, index) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("tour", "1");
  url.searchParams.set("tour_step", String(index));
  return url.pathname + url.search;
}

function clearTourQuery() {
  const url = new URL(window.location.href);
  url.searchParams.delete("tour");
  url.searchParams.delete("tour_step");
  window.history.replaceState({}, "", url.pathname + (url.search ? url.search : "") + url.hash);
}

async function updateStatus(status, extra = {}) {
  await fetch("/api/tour/status", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ status, ...extra }),
  }).catch(() => null);
}

function hideAll() {
  ensureDom();
  root.classList.remove("active");
  callout.classList.remove("cd-tour-visible");
  choice.classList.remove("cd-tour-visible");
  endCard.classList.remove("cd-tour-visible");
  spotlight.style.display = "none";
  document.querySelectorAll(".cd-tour-target").forEach((node) => node.classList.remove("cd-tour-target"));
  if (rafHandle) {
    cancelAnimationFrame(rafHandle);
    rafHandle = null;
  }
}

function showChoice() {
  ensureDom();
  root.classList.add("active");
  choice.classList.add("cd-tour-visible");
  callout.classList.remove("cd-tour-visible");
  endCard.classList.remove("cd-tour-visible");
  spotlight.style.display = "none";
}

function showEndScreen() {
  ensureDom();
  hideAll();
  root.classList.add("active");
  endCard.classList.add("cd-tour-visible");
  endCard.querySelector(".cd-tour-title").textContent = endScreen.headline;
  endCard.querySelector(".cd-tour-copy").textContent = endScreen.copy;
  const actions = endCard.querySelector(".cd-tour-end-actions");
  actions.innerHTML = "";
  endScreen.actions.forEach((action) => {
    const link = document.createElement("a");
    link.className = `cd-tour-btn ${action.primary ? "cd-tour-btn-primary" : ""}`.trim();
    link.href = action.href;
    link.textContent = action.label;
    actions.appendChild(link);
  });
}

function findTarget(step) {
  return document.querySelector(`[data-tour="${step.target}"]`);
}

function placeCallout(target) {
  const rect = target.getBoundingClientRect();
  const cardRect = callout.getBoundingClientRect();
  const arrow = callout.querySelector(".cd-tour-arrow");
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const mobile = viewportWidth < 780;

  spotlight.style.display = "block";
  spotlight.style.left = `${Math.max(8, rect.left - 8)}px`;
  spotlight.style.top = `${Math.max(8, rect.top - 8)}px`;
  spotlight.style.width = `${rect.width + 16}px`;
  spotlight.style.height = `${rect.height + 16}px`;

  if (mobile) {
    callout.style.left = "12px";
    callout.style.right = "12px";
    callout.style.top = "auto";
    callout.style.bottom = "12px";
    arrow.style.display = "none";
    return;
  }

  arrow.style.display = "block";
  const spaceBelow = viewportHeight - rect.bottom;
  const showBelow = spaceBelow >= cardRect.height + 30 || rect.top < cardRect.height + 40;
  const top = showBelow
    ? Math.min(viewportHeight - cardRect.height - 12, rect.bottom + 18)
    : Math.max(12, rect.top - cardRect.height - 18);
  const left = Math.min(
    viewportWidth - cardRect.width - 12,
    Math.max(12, rect.left + rect.width / 2 - cardRect.width / 2),
  );

  callout.style.left = `${left}px`;
  callout.style.top = `${top}px`;
  callout.style.right = "auto";
  callout.style.bottom = "auto";

  const arrowLeft = Math.max(24, Math.min(cardRect.width - 24, rect.left + rect.width / 2 - left));
  arrow.style.left = `${arrowLeft - 8}px`;
  if (showBelow) {
    arrow.style.top = "-8px";
    arrow.style.bottom = "auto";
    arrow.style.transform = "rotate(45deg)";
  } else {
    arrow.style.top = "auto";
    arrow.style.bottom = "-8px";
    arrow.style.transform = "rotate(225deg)";
  }
}

function renderStep(index) {
  ensureDom();
  const step = steps[index];
  if (!step) return;
  const target = findTarget(step);
  if (!target) {
    rafHandle = requestAnimationFrame(() => renderStep(index));
    return;
  }
  root.classList.add("active");
  callout.classList.add("cd-tour-visible");
  choice.classList.remove("cd-tour-visible");
  endCard.classList.remove("cd-tour-visible");
  target.classList.add("cd-tour-target");

  callout.querySelector(".cd-tour-step").textContent = `${index + 1} of ${steps.length}`;
  callout.querySelector(".cd-tour-title").textContent = step.headline;
  callout.querySelector(".cd-tour-copy").textContent = step.copy;
  callout.querySelector(".cd-tour-progress-label").textContent = "Progress";
  callout.querySelector(".cd-tour-progress-fraction").textContent = `${index + 1} of ${steps.length}`;
  callout.querySelector(".cd-tour-progress-bar span").style.width = `${((index + 1) / steps.length) * 100}%`;
  const back = callout.querySelector(".cd-tour-back");
  back.style.visibility = index === 0 ? "hidden" : "visible";
  const next = callout.querySelector(".cd-tour-next");
  next.textContent = index === steps.length - 1 ? "Finish" : "Next →";

  window.history.replaceState({}, "", withTourQuery(step.path, index));
  placeCallout(target);
}

function goToStep(index) {
  const step = steps[index];
  if (!step) return;
  if (currentPath() !== step.path) {
    window.location.assign(withTourQuery(step.path, index));
    return;
  }
  renderStep(index);
}

function moveStep(delta) {
  const index = tourStepIndexFromUrl();
  if (index === null) return;
  const nextIndex = index + delta;
  if (nextIndex < 0) return;
  if (nextIndex >= steps.length) {
    completeTour();
    return;
  }
  goToStep(nextIndex);
}

async function startTour(manualRestart) {
  await updateStatus("pending", { suppress_prompt: true });
  if (manualRestart) {
    clearTourQuery();
  }
  goToStep(0);
}

async function completeTour() {
  await updateStatus("completed");
  clearTourQuery();
  showEndScreen();
}

function bindResize() {
  if (resizeBound) return;
  resizeBound = () => {
    const index = tourStepIndexFromUrl();
    if (index !== null) renderStep(index);
  };
  window.addEventListener("resize", resizeBound);
  window.addEventListener("scroll", resizeBound, true);
}

function init() {
  ensureDom();
  bindResize();
  const activeIndex = tourStepIndexFromUrl();
  if (activeIndex !== null) {
    goToStep(activeIndex);
    return;
  }
  if ((context.tourStatus === "pending" || context.tourStatus === "remind_later") && !context.promptSuppressed) {
    showChoice();
  }
}

window.CountDepotTour = {
  restart: async () => {
    await startTour(true);
  },
  start: async () => {
    await startTour(false);
  },
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
