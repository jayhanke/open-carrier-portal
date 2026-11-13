from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from test_admin import BASE, login, post
from test_admin import admin as admin
from test_admin import company_link as company_link
from test_admin import company_source as company_source


def test_partners_workflow_keeps_exact_approval_fields_and_does_not_grant_on_selection(
    admin, company_link
):
    app, _, _ = admin
    portal, recipient, _ = company_link
    repo = app.state.repository
    receipts = repo.portal_policy_receipts(portal)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = client.get(
            "/admin/partners", params={"profile": "customer-db", "portal_company_id": recipient}
        )
        assert response.status_code == 200
        markup = response.text
        assert 'class="partner-workspace"' in markup
        assert 'aria-labelledby="shared-companies"' in markup
        assert '<span class="partner-step">1</span>Choose a portal company' in markup
        assert '<span class="partner-step">2</span>Companies from your database' in markup
        assert '<span class="partner-step">3</span>Review sharing' in markup
        assert 'data-action-id="connector.partners.request.select" aria-current="true"' in markup
        assert "Recipient &lt;Carrier&gt;" in markup and "Recipient <Carrier>" not in markup
        approval = re.search(
            r'<form method="post" action="/admin/link-portal-company">(.*?)</form>',
            markup,
            re.S,
        ).group(1)
        for field in (
            "csrf", "profile", "portal_company_id", "expected_policy_id", "expected_revision",
            "company", "visibility", "consent", "layout_consent",
        ):
            assert f'name="{field}"' in approval
        assert 'data-company-name="Example &lt;Carrier&gt;"' in approval
        assert " checked" not in approval
        assert 'name="consent" value="yes" required' in approval
        assert 'data-action-id="connector.action.link-portal-company"' in approval
        assert 'src="/admin/assets/partners.js" defer' in markup
        assert repo.portal_policy_receipts(portal) == receipts
        snapshot = repo.administration_snapshot()
        assert snapshot["partners"] == snapshot["grants"] == snapshot["shares"] == []


def test_sharing_panel_uses_database_name_and_review_preserves_exact_company_id(
    admin, company_link, monkeypatch
):
    app, _, _ = admin
    _, _, state = company_link
    monkeypatch.setattr("open_carrier_portal.admin.send_heartbeat", lambda *args, **kwargs: None)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        markup = client.get("/admin/partners").text
        assert "Existing recipient" in markup and "Awaiting portal" in markup
        assert "Database: <strong>customer-db</strong>" in markup
        assert "Billing company ID: 77" in markup
        review = html.unescape(
            re.search(r'<a class="partner-secondary" href="([^"]+)"', markup).group(1)
        )
        assert urlsplit(review).fragment == "link-company"
        assert parse_qs(urlsplit(review).query) == {
            "portal_company_id": [state["policies"][0]["recipient_organization_id"]],
            "profile": ["customer-db"],
            "q": ["77"],
        }
        assert 'data-action-id="connector.action.stop-company-policy"' in markup
        stopped = post(client, "stop-company-policy", policy=state["policies"][0]["policy_id"])
        assert stopped.status_code == 200
        markup = client.get("/admin/partners").text
        assert "stopped or mapping changed; new approval required" in markup
        assert 'data-action-id="connector.action.stop-company-policy"' not in markup
        assert "Review / change sharing" in markup


def test_partners_empty_state_and_progressive_enhancement_asset(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        markup = client.get("/admin/partners").text
        assert "Pair Carrier Portal" in markup
        assert "Select a database connection above" in markup
        assert 'action="/admin/link-portal-company"' not in markup
        assert '<a href="#direct-partners"' in markup
        assert '<details class="partner-advanced" id="direct-partners">' in markup
        assert "Advanced: direct connector access" in markup
        assert "No portal account or pairing is needed." in markup
        script = client.get("/admin/assets/partners.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        assert script.headers["x-content-type-options"] == "nosniff"
