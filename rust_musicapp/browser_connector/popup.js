"use strict";
document.getElementById("connect").addEventListener("click", async () => {
  const button = document.getElementById("connect"), status = document.getElementById("status");
  button.disabled = true;
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const address = new URL(tab?.url || "about:blank");
    const token = new URLSearchParams(address.hash.slice(1)).get("musicapp");
    if (address.protocol !== "http:" || address.hostname !== "127.0.0.1" || !address.port || address.pathname !== "/connect" || !/^[a-f0-9]{64}$/.test(token || "")) {
      throw new Error("Run connect-browser.bat and keep its connection tab active.");
    }
    const stores = await chrome.cookies.getAllCookieStores();
    const storeId = stores.find(store => store.tabIds.includes(tab.id))?.id;
    if (storeId === undefined) throw new Error("Cannot identify this browser profile's cookie store.");
    const cookies = (await chrome.cookies.getAll({domain: "youtube.com", storeId})).map(({name,value,domain,path,expirationDate}) => ({name,value,domain,path,expirationDate}));
    if (!cookies.some(cookie => ["SAPISID","__Secure-3PAPISID","__Secure-1PAPISID"].includes(cookie.name) && cookie.value)) {
      throw new Error("Sign in to YouTube in this browser profile first, then return here and retry.");
    }
    status.textContent = "Verifying your YouTube session…";
    const response = await fetch(address.origin + "/session", {method:"POST", headers:{"Content-Type":"application/json","X-MusicApp-Token":token}, body:JSON.stringify(cookies), signal:AbortSignal.timeout(45000)});
    const result = await response.json();
    if (!response.ok || result.verified !== true) throw new Error(result.error || "YouTube did not accept this session.");
    status.textContent = "Connected. MusicApp remembered your session. You can close this tab and start MusicApp.";
  } catch (error) {
    status.textContent = error.message || "Connection failed. Run connect-browser.bat to retry.";
  } finally { button.disabled = false; }
});
