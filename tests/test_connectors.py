from cyfun.connectors.cloudflare import evaluate_tls, evaluate_waf
from cyfun.connectors.github import evaluate_branch_protection, evaluate_dependabot, evaluate_two_factor
from cyfun.connectors.microsoft import evaluate_ca_policies, evaluate_global_admins, evaluate_mfa_registration, evaluate_stale_accounts
from cyfun.framework import load_framework


def test_all_check_requirement_ids_exist():
    fw = load_framework()
    checks = [
        evaluate_ca_policies([], None),
        evaluate_mfa_registration([]),
        evaluate_global_admins([]),
        evaluate_stale_accounts([]),
        evaluate_two_factor(True, "x"),
        evaluate_dependabot([], "x"),
        evaluate_branch_protection({}),
        evaluate_tls({}),
        evaluate_waf({}),
    ]
    for c in checks:
        for rid in c.requirement_ids:
            assert rid in fw.by_id, rid


def test_ca_policy_requires_all_users_all_apps():
    strong = {
        "state": "enabled",
        "displayName": "MFA all",
        "grantControls": {"builtInControls": ["mfa"]},
        "conditions": {"users": {"includeUsers": ["All"]}, "applications": {"includeApplications": ["All"]}},
    }
    partial = {
        "state": "enabled",
        "displayName": "MFA admins",
        "grantControls": {"builtInControls": ["mfa"]},
        "conditions": {"users": {"includeRoles": ["x"]}, "applications": {"includeApplications": ["All"]}},
    }
    disabled = dict(strong, state="disabled")
    assert evaluate_ca_policies([strong], False).status == "pass"
    assert evaluate_ca_policies([partial], False).status == "warn"
    assert evaluate_ca_policies([disabled], False).status == "fail"
    assert evaluate_ca_policies([], True).status == "pass"
    strength = dict(strong, grantControls={"authenticationStrength": {"id": "x"}})
    assert evaluate_ca_policies([strength], None).status == "pass"


def test_mfa_registration():
    rows = [{"userPrincipalName": "a@x", "isMfaRegistered": True}, {"userPrincipalName": "b@x", "isMfaRegistered": False, "isAdmin": True}]
    c = evaluate_mfa_registration(rows)
    assert c.status == "fail"
    assert c.details["admins_not_registered"] == ["b@x"]
    assert evaluate_mfa_registration(rows[:1]).status == "pass"


def test_global_admins():
    assert evaluate_global_admins([{"userPrincipalName": "a"}]).status == "warn"
    assert evaluate_global_admins([{"userPrincipalName": str(i)} for i in range(3)]).status == "pass"
    assert evaluate_global_admins([{"userPrincipalName": str(i)} for i in range(6)]).status == "fail"


def test_stale_accounts():
    users = [
        {"userPrincipalName": "old@x", "accountEnabled": True, "signInActivity": {"lastSignInDateTime": "2020-01-01T00:00:00Z"}},
        {"userPrincipalName": "disabled@x", "accountEnabled": False, "signInActivity": {"lastSignInDateTime": "2020-01-01T00:00:00Z"}},
    ]
    c = evaluate_stale_accounts(users)
    assert c.status == "warn"
    assert [s["user"] for s in c.details["stale_accounts"]] == ["old@x"]


def test_github_evaluations():
    assert evaluate_two_factor(True, "org x").status == "pass"
    assert evaluate_two_factor(False, "org x").status == "fail"
    assert evaluate_two_factor(None, "org x").status == "warn"
    alerts = [{"security_advisory": {"severity": "high"}}, {"security_advisory": {"severity": "low"}}]
    assert evaluate_dependabot(alerts, "x").status == "fail"
    assert evaluate_dependabot(alerts[1:], "x").status == "warn"
    assert evaluate_dependabot([], "x").status == "pass"
    assert evaluate_branch_protection({"a/b": True, "a/c": False}).status == "warn"
    assert evaluate_branch_protection({"a/b": True}).status == "pass"


def test_cloudflare_evaluations():
    good = {"z1": {"ssl": "strict", "min_tls_version": "1.2", "always_use_https": "on"}}
    weak = {"z1": {"ssl": "flexible", "min_tls_version": "1.0", "always_use_https": "off"}}
    assert evaluate_tls(good).status == "pass"
    assert evaluate_tls(weak).status == "warn"
    assert evaluate_waf({"z1": True}).status == "pass"
    assert evaluate_waf({"z1": False}).status == "warn"
