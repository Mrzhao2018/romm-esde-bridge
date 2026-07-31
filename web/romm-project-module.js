(() => {
  "use strict";

  if (window.top !== window.self || document.getElementById("romm-esde-project-button")) return;

  const bridgeUrl = `${window.location.protocol}//${window.location.hostname}:8090`;
  const style = document.createElement("style");
  style.textContent = `
    #romm-esde-project-button {
      position: fixed; left: 18px; bottom: 18px; z-index: 2147483000;
      display: flex; align-items: center; gap: 10px; min-height: 44px;
      padding: 0 16px; border: 1px solid rgba(190, 129, 255, .42);
      border-radius: 14px; color: #f7efff; background: rgba(31, 19, 48, .94);
      box-shadow: 0 12px 34px rgba(0, 0, 0, .34); backdrop-filter: blur(16px);
      font: 600 14px/1 system-ui, sans-serif; cursor: pointer;
      transition: transform .16s ease, border-color .16s ease, background .16s ease;
    }
    #romm-esde-project-button:hover { transform: translateY(-2px); border-color: #c794ff; background: #382154; }
    #romm-esde-project-button svg { width: 20px; height: 20px; color: #b878ff; }
    #romm-esde-project-modal { position: fixed; inset: 0; z-index: 2147483640; display: none; }
    #romm-esde-project-modal[data-open="true"] { display: block; }
    #romm-esde-project-backdrop { position: absolute; inset: 0; background: rgba(5, 2, 10, .76); backdrop-filter: blur(8px); }
    #romm-esde-project-shell {
      position: absolute; inset: clamp(12px, 3vw, 42px); overflow: hidden;
      border: 1px solid rgba(190, 129, 255, .35); border-radius: 22px;
      background: #100b18; box-shadow: 0 30px 90px rgba(0, 0, 0, .65);
    }
    #romm-esde-project-frame { width: 100%; height: 100%; border: 0; background: #100b18; }
    #romm-esde-project-close {
      position: absolute; top: 14px; right: 14px; z-index: 2; width: 40px; height: 40px;
      border: 1px solid rgba(255,255,255,.15); border-radius: 50%; color: white;
      background: rgba(20, 13, 30, .86); font: 25px/1 system-ui; cursor: pointer;
    }
    @media (max-width: 700px) {
      #romm-esde-project-button { left: 12px; bottom: 12px; width: 46px; padding: 0; justify-content: center; }
      #romm-esde-project-button span { display: none; }
      #romm-esde-project-shell { inset: 0; border: 0; border-radius: 0; }
    }
  `;

  const icon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 10.7v6M12 7.2h.01"/></svg>`;
  const button = document.createElement("button");
  button.id = "romm-esde-project-button";
  button.type = "button";
  button.setAttribute("aria-label", "打开项目说明");
  button.innerHTML = `${icon}<span>项目说明</span>`;

  const modal = document.createElement("div");
  modal.id = "romm-esde-project-modal";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-label", "RomM ES-DE 项目说明");
  modal.innerHTML = `<div id="romm-esde-project-backdrop"></div><div id="romm-esde-project-shell"><button id="romm-esde-project-close" type="button" aria-label="关闭">×</button><iframe id="romm-esde-project-frame" title="RomM ES-DE 项目说明"></iframe></div>`;

  const open = () => {
    const frame = modal.querySelector("iframe");
    if (!frame.src) frame.src = `${bridgeUrl}/project/`;
    modal.dataset.open = "true";
    document.documentElement.style.overflow = "hidden";
    modal.querySelector("button").focus();
  };
  const close = () => {
    modal.dataset.open = "false";
    document.documentElement.style.overflow = "";
    button.focus();
  };
  button.addEventListener("click", open);
  modal.querySelector("#romm-esde-project-close").addEventListener("click", close);
  modal.querySelector("#romm-esde-project-backdrop").addEventListener("click", close);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal.dataset.open === "true") close();
  });

  document.head.appendChild(style);
  document.body.append(button, modal);

  const pc98Style = document.createElement("style");
  pc98Style.textContent = `
    #romm-esde-pc98-button {
      position: fixed; left: 18px; bottom: 76px; z-index: 2147483001;
      display: none; align-items: center; gap: 10px; min-height: 44px;
      padding: 0 16px; border: 1px solid rgba(112, 213, 194, .48);
      border-radius: 8px; color: #071313; background: #70d5c2;
      box-shadow: 0 12px 34px rgba(0, 0, 0, .34);
      font: 700 14px/1 system-ui, sans-serif; cursor: pointer;
    }
    #romm-esde-pc98-button[data-visible="true"] { display: flex; }
    #romm-esde-pc98-button:hover { background: #9de8da; transform: translateY(-2px); }
    #romm-esde-pc98-button svg { width: 20px; height: 20px; }
    @media (max-width: 700px) {
      #romm-esde-pc98-button { left: 12px; bottom: 68px; width: 46px; padding: 0; justify-content: center; }
      #romm-esde-pc98-button span { display: none; }
    }
  `;
  document.head.appendChild(pc98Style);

  const pc98Button = document.createElement("button");
  pc98Button.id = "romm-esde-pc98-button";
  pc98Button.type = "button";
  pc98Button.setAttribute("aria-label", "在浏览器中游玩 PC-98 游戏");
  pc98Button.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="1.5"/><path d="M8 20h8M12 17v3"/></svg><span>浏览器游玩</span>`;
  pc98Button.addEventListener("click", () => {
    const romId = pc98Button.dataset.romId;
    if (!romId) return;
    const opened = window.open(`${bridgeUrl}/pc98/?rom=${encodeURIComponent(romId)}`, "_blank");
    if (opened) opened.opener = null;
  });
  document.body.append(pc98Button);

  let checkedRomId = null;
  let checkedIsPc98 = false;
  let checkInFlight = false;
  const syncPc98Button = async () => {
    const match = window.location.pathname.match(/^\/rom\/(\d+)(?:\/|$)/);
    if (!match) {
      pc98Button.dataset.visible = "false";
      pc98Button.dataset.romId = "";
      checkedRomId = null;
      checkedIsPc98 = false;
      return;
    }
    const romId = match[1];
    pc98Button.dataset.romId = romId;
    if (checkedRomId === romId) {
      pc98Button.dataset.visible = checkedIsPc98 ? "true" : "false";
      return;
    }
    if (checkInFlight) return;
    checkInFlight = true;
    try {
      const response = await fetch(`/api/roms/${encodeURIComponent(romId)}/simple`, {
        credentials: "same-origin", headers: { Accept: "application/json" },
      });
      const rom = response.ok ? await response.json() : null;
      checkedRomId = romId;
      checkedIsPc98 = rom?.platform_slug === "pc-9800-series";
      pc98Button.dataset.visible = checkedIsPc98 ? "true" : "false";
    } catch (_) {
      checkedRomId = romId;
      checkedIsPc98 = false;
      pc98Button.dataset.visible = "false";
    } finally {
      checkInFlight = false;
    }
  };
  const routeObserver = new MutationObserver(() => syncPc98Button());
  routeObserver.observe(document.body, { childList: true, subtree: true });
  window.addEventListener("popstate", syncPc98Button);
  window.addEventListener("hashchange", syncPc98Button);
  syncPc98Button();
})();
