import frappe
from frappe.utils.password import get_decrypted_password

from testifield_bridge.api import TF_DOCTYPES
from testifield_bridge.webhook_body import body_template

# Submit/cancel events exist only on submittable doctypes (Frappe refuses them
# elsewhere); Project, Warehouse and Cost Center get insert and update.
SUBMIT_EVENTS = ["after_insert", "on_submit", "on_cancel", "on_update_after_submit"]
PLAIN_EVENTS = ["after_insert", "on_update"]


def webhook_events(doctype):
    return SUBMIT_EVENTS if frappe.get_meta(doctype).is_submittable else PLAIN_EVENTS

# The least roles the posting map needs. Nothing about HR, nothing about
# system settings; the accountant and payroll use their own accounts.
INTEGRATION_ROLES = [
    "Projects User", "Stock User", "Purchase User", "Accounts User", "Sales User", "Item Manager",
]


def after_install():
    # Only the fields: Frappe will not save a webhook without a request URL,
    # so the webhooks are made by point_webhooks (site-setup.sh) once the
    # instance's address and secret are known.
    ensure_custom_fields()


def after_migrate():
    ensure_custom_fields()
    from testifield_bridge.hr import hrms_installed, ensure_hr_custom_fields
    if hrms_installed():
        ensure_hr_custom_fields()
    ensure_webhooks()  # re-applies the shape of webhooks that already exist


# where the two fields sit on each form: after the first field every form has
INSERT_AFTER = {"Project": "project_name", "Warehouse": "warehouse_name", "Cost Center": "cost_center_name"}


def ensure_custom_fields():
    """tf_ref and tf_user on every doctype the posting map writes. Idempotent."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    fields = {}
    for dt in TF_DOCTYPES:
        fields[dt] = [
            {
                "fieldname": "tf_ref", "label": "Testifield ref", "fieldtype": "Data", "length": 140,
                "insert_after": INSERT_AFTER.get(dt, "naming_series"),
                "read_only": 1, "allow_on_submit": 0, "in_standard_filter": 1, "search_index": 1, "no_copy": 1,
                "description": "Idempotency key from Testifield; the same event never makes two documents",
            },
            {
                "fieldname": "tf_user", "label": "Testifield user", "fieldtype": "Data", "length": 140,
                "insert_after": "tf_ref", "read_only": 1, "no_copy": 1,
                "description": "Who in Testifield caused this posting",
            },
        ]
    create_custom_fields(fields, ignore_validate=True, update=True)
    frappe.db.commit()


def ensure_webhooks(url=None, secret=None):
    """One webhook per doctype per event, named 'TF <Doctype> <event>'. The
    URL and the secret come from point_webhooks; without a URL only webhooks
    that already exist are re-shaped (Frappe refuses one with no URL)."""
    from testifield_bridge.hr import hrms_installed, hr_doctypes_present
    # the HR doctypes too, where Frappe HR runs: a decision taken in Desk reaches Testifield
    for dt in TF_DOCTYPES + (hr_doctypes_present() if hrms_installed() else []):
        for ev in webhook_events(dt):
            name = f"TF {dt} {ev}"
            if frappe.db.exists("Webhook", name):
                wh = frappe.get_doc("Webhook", name)
            elif url:
                wh = frappe.new_doc("Webhook")
                wh.name = name
                wh.flags.name_set = True  # keep our name, not the doctype's autoname
            else:
                continue
            wh.webhook_doctype = dt
            wh.webhook_docevent = ev
            wh.request_method = "POST"
            wh.request_structure = "JSON"
            # The webhook renders with the document as plain data in Frappe's
            # sandbox (no doc.as_json()); every value goes in through `tojson`
            # (webhook_body), so the body is valid JSON whatever a name holds.
            wh.webhook_json = body_template(ev, whole_doc=dt in TF_DOCTYPES)
            # an HR record reaches Testifield only if Testifield made it (its tf_ref): never
            # an applicant, an employee or a claim entered in Desk (a Job Applicant's name
            # is an email address)
            wh.condition = "" if dt in TF_DOCTYPES else "doc.tf_ref"
            wh.set("webhook_headers", [])
            wh.append("webhook_headers", {"key": "Content-Type", "value": "application/json"})
            wh.enable_security = 1
            if url is not None:
                wh.request_url = url
            if secret is not None:
                wh.webhook_secret = secret
            wh.enabled = 1 if (wh.request_url and (secret or wh.get_password("webhook_secret", raise_exception=False))) else 0
            wh.flags.ignore_permissions = True
            wh.save()
    frappe.db.commit()


def point_webhooks(url, secret):
    """Where the webhooks go and what they are signed with. Run at install
    and whenever the Testifield instance's address or secret changes."""
    ensure_webhooks(url=url, secret=secret)
    n = frappe.db.count("Webhook", {"name": ["like", "TF %"], "enabled": 1})
    return {"enabled": n, "url": url}


def make_integration_user(email="testifield@erp.internal", full_name="Testifield", regenerate=False):
    """The one user Testifield's server signs in as. Returns the API key and
    secret ONCE; keep them in the server's environment, never in a database."""
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
    else:
        user = frappe.new_doc("User")
        user.email = email
        user.first_name = full_name
        user.user_type = "System User"
        user.send_welcome_email = 0
        user.enabled = 1
    user.set("roles", [])
    for r in INTEGRATION_ROLES:
        if frappe.db.exists("Role", r):
            user.append("roles", {"role": r})
    if not user.api_key or regenerate:
        user.api_key = frappe.generate_hash(length=15)
        user.api_secret = frappe.generate_hash(length=15)
    user.flags.ignore_permissions = True
    user.save()
    frappe.db.commit()
    secret = get_decrypted_password("User", user.name, "api_secret", raise_exception=False)
    return {"user": user.name, "api_key": user.api_key, "api_secret": secret, "roles": INTEGRATION_ROLES}


def make_sandbox_fixtures(company="INOV IT Solutions Pvt Ltd", abbr="ISPL"):
    """A sandbox's minimum: a company, a free-issue supplier, a serialised
    LED item, a site warehouse, a project cost centre. Idempotent. On a fresh
    site it first completes ERPNext's setup wizard (India, INR, April–March),
    which makes the company with its defaults; a company inserted by hand on
    a site that never ran the wizard fails ('Could not find Warehouse Type')."""
    out = {}
    if not frappe.db.get_single_value("System Settings", "setup_complete"):
        out["setup_wizard"] = complete_setup_wizard(company, abbr)
    if not frappe.db.exists("Company", company):
        c = frappe.new_doc("Company")
        c.company_name = company
        c.abbr = abbr
        c.default_currency = "INR"
        c.country = "India"
        c.chart_of_accounts = "Standard"
        c.flags.ignore_permissions = True
        c.insert()
    out["company"] = company
    if not frappe.db.exists("Supplier", "PWD (free issue)"):
        s = frappe.new_doc("Supplier")
        s.supplier_name = "PWD (free issue)"
        s.supplier_group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name") or "All Supplier Groups"
        s.flags.ignore_permissions = True
        s.insert()
    out["supplier"] = "PWD (free issue)"
    if not frappe.db.exists("Item", "LED-120W"):
        i = frappe.new_doc("Item")
        i.item_code = "LED-120W"
        i.item_name = "LED fitting 120 W"
        i.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
        i.stock_uom = "Nos"
        i.is_stock_item = 1
        i.has_serial_no = 1
        i.serial_no_series = "LED120-.#####"
        if frappe.get_meta("Item").has_field("gst_hsn_code"):  # India Compliance requires one
            hsn = "940542"  # luminaires and lighting fittings designed for use solely with LEDs
            if not frappe.db.exists("GST HSN Code", hsn):
                h = frappe.new_doc("GST HSN Code")
                h.hsn_code = hsn
                h.description = "Luminaires and lighting fittings, LED"
                h.flags.ignore_permissions = True
                h.insert()
            i.gst_hsn_code = hsn
        i.flags.ignore_permissions = True
        i.insert()
    out["item"] = "LED-120W"
    wh = f"Delhi store - {abbr}"
    if not frappe.db.exists("Warehouse", wh):
        w = frappe.new_doc("Warehouse")
        w.warehouse_name = "Delhi store"
        w.company = company
        w.flags.ignore_permissions = True
        w.insert()
    out["warehouse"] = wh
    cc = f"PWD-SSL - {abbr}"
    if not frappe.db.exists("Cost Center", cc):
        c = frappe.new_doc("Cost Center")
        c.cost_center_name = "PWD-SSL"
        c.company = company
        c.parent_cost_center = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
        c.flags.ignore_permissions = True
        c.insert()
    out["cost_center"] = cc
    frappe.db.commit()
    return out


def complete_setup_wizard(company, abbr):
    """ERPNext's first-run wizard without the browser: the same stages the
    desk runs, for an Indian company with an April–March fiscal year."""
    from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
    from frappe.utils import getdate, nowdate

    today = getdate(nowdate())
    start_year = today.year if today.month >= 4 else today.year - 1
    args = {
        "language": "English",
        "country": "India",
        "timezone": "Asia/Kolkata",
        "currency": "INR",
        "full_name": "Administrator",
        "company_name": company,
        "company_abbr": abbr,
        "chart_of_accounts": "Standard",
        "fy_start_date": f"{start_year}-04-01",
        "fy_end_date": f"{start_year + 1}-03-31",
        "setup_demo": 0,
    }
    frappe.set_user("Administrator")
    r = setup_complete(args)
    frappe.db.commit()
    return r if isinstance(r, dict) else {"status": "done"}
