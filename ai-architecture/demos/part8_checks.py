"""Part 8: CI checks over the platform's configuration."""

from aigw.catalog import Deployment, Route, SONNET, default_catalog
from aigw.checks import check_catalog, check_prompts, check_tenant_limits

cat = default_catalog()
cat.add(Route("chat-experimental", [Deployment("anthropic", "some-model-latest", SONNET, 200_000)]))
needs = {"chat-default": {"stream", "tools"}, "chat-cheap": {"stream"}, "chat-eu-only": {"stream"}}
findings = (check_catalog(cat, needs)
            + check_tenant_limits({"acme": 1_200_000, "globex": 600_000, "initech": 400_000}, 2_000_000)
            + check_prompts({"support-bot/v3": "You are Acme support.",
                             "digest/v2": "Today is {{ now }}. Summarise the following...",
                             "billing/v1": "Use sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx for billing."}))
for f in findings:
    print(f"{f.severity:5} {f.where:18} {f.message}")
errors = sum(f.severity == "error" for f in findings)
print(f"\n{errors} errors, {len(findings) - errors} warnings -> CI {'FAILS' if errors else 'passes'}")
