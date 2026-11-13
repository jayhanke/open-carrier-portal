"use strict";

(() => {
  const csrf = document.querySelector('meta[name="ocp-admin-csrf"]');
  const notice = document.getElementById("admin-session-notice");
  if (!csrf || !notice) return;
  const username = document.querySelector('meta[name="ocp-admin-username"]').content;
  const idleMilliseconds = Number(
    document.querySelector('meta[name="ocp-admin-idle-timeout"]').content
  ) * 1000;
  let confirmed = Date.now(), attempted = confirmed;
  let pending = false, busy = false, signedOut = false;
  const show = text => {
    notice.hidden = false;
    notice.replaceChildren(document.createTextNode(text));
  };
  const expired = () => {
    signedOut = true;
    show("Your session expired or was signed out. This page and any unsaved entries " +
      "have been kept. Sign in in another tab, then choose Resume session here. ");
    const link = document.createElement("a");
    link.href = "/admin/login";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Sign in";
    const resume = document.createElement("button");
    resume.type = "button";
    resume.textContent = "Resume session";
    resume.addEventListener("click", async () => {
      const send = window.ocpInteractions?.capture(resume).fetch || fetch;
      if (busy) return;
      busy = true;
      resume.disabled = true;
      try {
        const response = await send("/admin/session", {cache: "no-store"});
        if (response.status === 401) {
          expired();
          return;
        }
        if (!response.ok) throw new Error("Cannot verify the signed-in account. Try again.");
        const session = await response.json();
        if (session.username !== username) {
          show("A different administrator is signed in. Your entries are unchanged. " +
            "Sign in with the original account before resuming this page.");
          notice.append(link, " ", resume);
          return;
        }
        csrf.content = session.csrf;
        document.querySelectorAll('input[name="csrf"]').forEach(input => {
          input.value = session.csrf;
        });
        const mapper = document.getElementById("mapping-workspace");
        if (mapper) mapper.dataset.csrf = session.csrf;
        signedOut = false;
        pending = true;
        attempted = 0;
      } catch (error) {
        show(error.message + " Your entries are unchanged. ");
        notice.append(link, " ", resume);
      } finally {
        busy = false;
        resume.disabled = false;
      }
      if (!signedOut) renew(send);
    });
    notice.append(link, " ", resume);
  };
  const renew = async (send = fetch) => {
    if (busy || signedOut || !pending || document.visibilityState !== "visible" ||
        Date.now() - attempted < 60000) return;
    busy = true;
    pending = false;
    attempted = Date.now();
    try {
      const response = await send("/admin/session", {
        method: "POST",
        headers: {"X-CSRF-Token": csrf.content}
      });
      if (response.status === 401 || response.status === 403) {
        expired();
        return;
      }
      if (!response.ok) throw new Error("The server could not renew your session.");
      confirmed = Date.now();
      notice.hidden = true;
      notice.replaceChildren();
    } catch (error) {
      pending = true;
      show("Cannot renew your session: " + error.message +
        " Your entries are unchanged. Check connectivity; activity renewal will retry.");
    } finally {
      busy = false;
    }
  };
  const activity = event => {
    if (!event.isTrusted || document.visibilityState !== "visible") return;
    pending = true;
    renew();
  };
  for (const name of ["pointerdown", "keydown", "input", "change", "dragstart", "drop", "wheel"]) {
    document.addEventListener(name, activity, {capture: true, passive: true});
  }
  document.addEventListener("submit", event => {
    if (!signedOut) return;
    event.preventDefault();
    expired();
  }, true);
  document.addEventListener("ocp-admin-session-expired", expired);
  setInterval(() => {
    if (signedOut) return;
    renew();
    const elapsed = Date.now() - confirmed;
    if (elapsed >= idleMilliseconds && !busy) {
      expired();
    } else if (elapsed >= idleMilliseconds - 300000 && !pending && !busy) {
      show("Your session will expire soon because there has been no recent activity. " +
        "Interact with this page to stay signed in. Unsaved entries will not be cleared.");
    }
  }, 30000);
})();
