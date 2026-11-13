(() => {
  const directAccess = document.getElementById("direct-partners");
  const directShortcut = document.querySelector('[data-action-id="connector.partners.jump.direct"]');
  if (directAccess && directShortcut) {
    directShortcut.addEventListener("click", () => { directAccess.open = true; });
    if (location.hash === "#direct-partners") directAccess.open = true;
  }

  const approval = document.querySelector('form[action="/admin/link-portal-company"]');
  if (!approval) return;

  const selection = approval.querySelector("[data-partner-selection]");
  const visibility = approval.elements.namedItem("visibility");
  const layoutConsent = approval.elements.namedItem("layout_consent");
  const layoutLabel = layoutConsent.closest("label");

  function updateReview() {
    const company = approval.querySelector('input[name="company"]:checked');
    selection.textContent = company
      ? `Billing company: ${company.dataset.companyName} (database ID: ${company.value})`
      : "Choose a database company above.";
    const fullLayout = visibility.value === "full_shared_path";
    layoutLabel.hidden = !fullLayout;
    layoutConsent.disabled = !fullLayout;
    layoutConsent.required = fullLayout;
  }

  approval.addEventListener("change", updateReview);
  window.addEventListener("pageshow", updateReview);
  updateReview();
})();
