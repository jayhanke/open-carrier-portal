"use strict";

(() => {
  const meta = name => document.querySelector(`meta[name="${name}"]`)?.content || "";
  const page = meta("ocp-page-id");
  if (!page) return;
  const endpoint = meta("ocp-log-endpoint");
  const actionQuery = "__ocp_action", interactionQuery = "__ocp_interaction";
  const selector = "a[href],button,input[type=submit],input[type=button],input[type=reset]," +
    "input[type=image],summary,form";
  const originalFetch = window.fetch.bind(window);
  const initial = {action_id: meta("ocp-action-id"), interaction_id: meta("ocp-interaction-id")};
  const forms = new WeakMap();
  let active = null, booting = true;
  const uuid = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const hash = value => {
    let first = 2166136261, second = 3339675911;
    for (const char of value) {
      first = Math.imul(first ^ char.charCodeAt(0), 16777619);
      second = Math.imul(second ^ char.charCodeAt(0), 16777619);
    }
    return [first, second].map(value => (value >>> 0).toString(16).padStart(8, "0")).join("");
  };
  const domKey = node => {
    const path = [];
    for (let element = node; element && element !== document.body; element = element.parentElement) {
      const siblings = [...(element.parentElement?.children || [])];
      path.push(`${element.localName}:${siblings.indexOf(element)}`);
    }
    return path.reverse().join("/");
  };
  const identify = node => {
    if (!node.dataset.actionId) {
      const key = node.id ? `#${node.id}` : domKey(node);
      node.dataset.actionId = `${page}.${node.localName}.${hash(key)}`;
    }
    if (!node.id) node.id = `ocp-${hash(page + domKey(node))}`;
    return node.dataset.actionId;
  };
  const scan = root => {
    if (root instanceof Element && root.matches(selector)) identify(root);
    root.querySelectorAll?.(selector).forEach(identify);
  };
  const fresh = node => ({
    action_id: node ? identify(node) : `${page}.background`,
    interaction_id: uuid()
  });
  const headers = (trace, existing) => {
    const result = new Headers(existing);
    result.set("X-Action-ID", trace.action_id);
    result.set("X-Interaction-ID", trace.interaction_id);
    return result;
  };
  const tracedFetch = (trace, input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin) return originalFetch(input, options);
    const existing = options.headers ?? (input instanceof Request ? input.headers : undefined);
    const supplied = new Headers(existing);
    return originalFetch(input, {
      ...options,
      headers: supplied.has("X-Action-ID") || supplied.has("X-Interaction-ID")
        ? supplied : headers(trace, existing)
    });
  };
  const capture = node => {
    const trace = active || (booting ? initial : fresh(node));
    return {...trace, fetch: (input, options) => tracedFetch(trace, input, options)};
  };
  window.ocpInteractions = {capture};
  window.fetch = (input, options) => capture().fetch(input, options);
  const activate = node => {
    const trace = fresh(node);
    active = trace;
    setTimeout(() => { if (active === trace) active = null; }, 0);
    if (endpoint) {
      originalFetch(endpoint, {
        method: "POST", credentials: "same-origin", keepalive: true,
        headers: headers(trace, {
          "Content-Type": "application/json",
          "X-CSRF-Token": meta("ocp-log-csrf") || meta("ocp-admin-csrf")
        }),
        body: "{}"
      }).then(response => {
        if (!response.ok) console.error("Interaction log rejected:", response.status);
      }).catch(error => console.error("Interaction log could not be recorded:", error));
    }
    return trace;
  };
  const annotate = (url, trace) => {
    url.searchParams.set(actionQuery, trace.action_id);
    url.searchParams.set(interactionQuery, trace.interaction_id);
    return url;
  };
  const click = event => {
    const node = event.target instanceof Element ? event.target.closest(selector) : null;
    if (!node || node.localName === "form" || node.disabled) return;
    if (event.type === "auxclick" && event.button !== 1) return;
    const trace = activate(node);
    if (node.form && (node.type === "submit" || node.type === "image")) {
      forms.set(node.form, trace);
      setTimeout(() => { if (forms.get(node.form) === trace) forms.delete(node.form); }, 0);
    }
    if (node.localName !== "a") return;
    const url = new URL(node.href, location.href);
    if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    const original = node.getAttribute("href");
    node.href = annotate(url, trace).href;
    setTimeout(() => node.setAttribute("href", original), 0);
  };
  document.addEventListener("click", click, true);
  document.addEventListener("auxclick", click, true);
  document.addEventListener("submit", event => {
    const form = event.target;
    const trace = forms.get(form) || activate(event.submitter || form);
    forms.delete(form);
    const url = new URL(
      event.submitter?.hasAttribute("formaction") ? event.submitter.formAction : form.action,
      location.href
    );
    if (url.origin !== location.origin) return;
    const method = (event.submitter?.getAttribute("formmethod") || form.method).toLowerCase();
    if (method === "get") {
      for (const [name, value] of [[actionQuery, trace.action_id], [interactionQuery, trace.interaction_id]]) {
        let input = form.elements.namedItem(name);
        if (!input) {
          input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          form.append(input);
        }
        input.value = value;
      }
    } else {
      const submitter = event.submitter?.hasAttribute("formaction") ? event.submitter : form;
      const attribute = submitter === form ? "action" : "formaction";
      const original = submitter.getAttribute(attribute);
      submitter.setAttribute(attribute, annotate(url, trace).href);
      setTimeout(() => {
        if (original === null) submitter.removeAttribute(attribute);
        else submitter.setAttribute(attribute, original);
      }, 0);
    }
  }, true);
  scan(document);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(scan)))
    .observe(document.documentElement, {childList: true, subtree: true});
  document.addEventListener("DOMContentLoaded", () => { booting = false; }, {once: true});
  const address = new URL(location.href);
  if (address.searchParams.has(actionQuery) || address.searchParams.has(interactionQuery)) {
    address.searchParams.delete(actionQuery);
    address.searchParams.delete(interactionQuery);
    history.replaceState(history.state, "", address);
  }
})();
