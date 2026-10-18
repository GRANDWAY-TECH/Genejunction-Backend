"""Build the public API reference: one OpenAPI 3.1 document for all services, plus Redoc / Swagger pages.

    python scripts/build_api_docs.py          # writes docs/api/openapi.json (served at /docs on the gateway)

Each service's spec is generated from code (no running stack needed), /internal routes are dropped,
schema-name clashes between services are resolved, and docs/API_GUIDE.md becomes the introduction.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
OUT = ROOT / "docs" / "api"
SERVICES = ["identity", "tenant", "partner", "catalog", "order", "payment", "billing", "pharmacy", "lab",
            "wholesale", "notification", "legacy_bridge"]

# raw router tag -> (display name, description)
TAGS = {
    "auth": ("Authentication", "Phone-OTP login for patients, email/password for business users, token refresh."),
    "users": ("Users", "The signed-in user's profile; user and role administration for GeneJunction admins."),
    "tenants": ("Brands & white label", "Brand configuration for apps, and the clinic-group business console: "
                                         "branding, domains, admins and provider network."),
    "catalog": ("Catalog", "Search products and offers visible in the caller's app; partners manage their offers."),
    "consultations (existing system)": ("Consultations", "Doctors and slots from the existing GeneJunction "
                                                         "system, filtered to the caller's network."),
    "health folio (existing system)": ("Health folio", "Patient records held in the existing GeneJunction system."),
    "cart": ("Cart", "One cart per patient per app: medicines, lab tests and one consultation."),
    "orders": ("Orders", "Checkout, tracking, cancellation and unified order history."),
    "prescriptions": ("Prescriptions", "E-prescriptions received from consultations."),
    "payments": ("Payments", "Payment status for payers; payment search for GeneJunction finance."),
    "partners": ("Partner onboarding", "Register, upload documents, bank details, submit for verification; "
                                       "ops review."),
    "partner orders": ("Partner order inbox", "Sub-orders from every app, tagged by channel."),
    "pharmacy": ("Pharmacy fulfilment", "Accept, verify prescription, pack, dispatch, deliver."),
    "lab": ("Lab bookings", "Collection slots, sample pickup, report upload; patients' reports."),
    "wholesale": ("Wholesale (stockists)", "B2B purchase orders between pharmacies/labs and stockists."),
    "billing": ("Billing & credits", "Credit wallets, packs and rate card, settlements, payouts and invoices "
                                     "for partners and clinic groups."),
    "finance": ("Finance console", "GeneJunction finance: commission rules, rate overrides, payouts, adjustments."),
    "ops": ("Ops console", "GeneJunction operations views."),
    "notifications": ("Notifications", "In-app inboxes, push device registration, patient campaigns."),
    "dev": ("Development only", "Helpers that exist only in local/dev environments (mock gateway, simulated "
                                "doctor). Disabled in production."),
}
GROUPS = [
    ("Accounts & brands", ["Authentication", "Users", "Brands & white label"]),
    ("Patients", ["Catalog", "Consultations", "Health folio", "Cart", "Orders", "Prescriptions", "Payments",
                  "Notifications"]),
    ("Partners", ["Partner onboarding", "Partner order inbox", "Pharmacy fulfilment", "Lab bookings",
                  "Wholesale (stockists)"]),
    ("Money", ["Billing & credits", "Finance console"]),
    ("GeneJunction staff", ["Ops console"]),
    ("Development", ["Development only"]),
]
HIDDEN_PREFIXES = ("/internal",)


def service_spec(name: str) -> dict:
    code = "import json, app.main; print(json.dumps(app.main.app.openapi()))"
    out = subprocess.run([str(PY), "-c", code], cwd=ROOT / "services" / name, capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"{name}: {out.stderr[-2000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def rename_refs(node, mapping: dict[str, str]):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            if name in mapping:
                node["$ref"] = f"#/components/schemas/{mapping[name]}"
        for v in node.values():
            rename_refs(v, mapping)
    elif isinstance(node, list):
        for v in node:
            rename_refs(v, mapping)


def collect_refs(node, found: set[str]):
    if isinstance(node, dict):
        if (ref := node.get("$ref")) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for v in node.values():
            collect_refs(v, found)
    elif isinstance(node, list):
        for v in node:
            collect_refs(v, found)


def op_id(summary: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", summary.lower()).strip("_")


def build() -> dict:
    paths: dict = {}
    schemas: dict = {}
    used_ids: set[str] = set()
    prefix = {"legacy_bridge": "Consult", "order": "Order", "catalog": "Catalog"}

    for name in SERVICES:
        spec = service_spec(name)
        own = spec.get("components", {}).get("schemas", {})
        mapping = {}
        for sname, sdef in own.items():
            if sname in schemas and schemas[sname] != sdef:
                mapping[sname] = f"{prefix.get(name, name.title())}{sname}"
        rename_refs(spec, mapping)
        for sname, sdef in own.items():
            schemas[mapping.get(sname, sname)] = sdef

        for path, item in spec["paths"].items():
            if path.startswith(HIDDEN_PREFIXES):
                continue
            for method, op in item.items():
                op["tags"] = [TAGS[t][0] for t in op.get("tags", [])] or ["Development only"]
                oid = op_id(op.get("summary", op["operationId"]))
                if oid in used_ids:
                    oid = f"{name}_{oid}"
                used_ids.add(oid)
                op["operationId"] = oid
                op["x-service"] = name.replace("_", "-")
            paths.setdefault(path, {}).update(item)

    guide = (ROOT / "docs" / "API_GUIDE.md").read_text()
    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "GeneJunction Marketplace API",
            "version": "1.0.0",
            "description": guide,
            "contact": {"name": "GeneJunction engineering"},
        },
        "servers": [{"url": "http://localhost:8080", "description": "Local (docker compose)"}],
        "tags": [{"name": display, "description": desc} for display, desc in TAGS.values()],
        "x-tagGroups": [{"name": g, "tags": t} for g, t in GROUPS],
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": schemas,
            "securitySchemes": {"HTTPBearer": {
                "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                "description": "Access token from `/auth/otp/verify`, `/auth/login` or `/auth/refresh`."}},
        },
    }
    # Keep only schemas reachable from public paths (full transitive closure).
    used: set[str] = set()
    collect_refs(doc["paths"], used)
    pending = list(used)
    while pending:
        found: set[str] = set()
        collect_refs(schemas.get(pending.pop(), {}), found)
        for name in found - used:
            used.add(name)
            pending.append(name)
    missing = used - set(schemas)
    if missing:
        sys.exit(f"dangling schema refs: {sorted(missing)}")
    doc["components"]["schemas"] = {k: schemas[k] for k in sorted(used)}
    return doc


def main() -> None:
    doc = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "openapi.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    ops = sum(len(v) for v in doc["paths"].values())
    print(f"docs/api/openapi.json: {ops} operations, {len(doc['paths'])} paths, "
          f"{len(doc['components']['schemas'])} schemas")


if __name__ == "__main__":
    main()
