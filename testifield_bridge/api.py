import frappe
from frappe import _

# The doctypes the posting map writes (adapters/erpnext/src/map.ts). The
# same list drives the custom fields and the webhooks.
TF_DOCTYPES = [
    "Project", "Stock Entry", "Purchase Receipt", "Purchase Invoice",
    "Sales Invoice", "Journal Entry", "Payment Entry", "Warehouse", "Cost Center",
]


@frappe.whitelist()
def ping():
    """The test exchange's first step: is the bridge installed, which apps are here."""
    apps = frappe.get_installed_apps()
    return {
        "ok": True,
        "site": frappe.local.site,
        "bridge": frappe.get_attr("testifield_bridge.__version__"),
        "apps": {a: True for a in apps},
        "india_compliance": "india_compliance" in apps,
        "hrms": "hrms" in apps,
        "user": frappe.session.user,
        "roles": frappe.get_roles(),
    }


@frappe.whitelist()
def find_by_tf_ref(doctype, tf_ref):
    """The idempotency lookup, as a method so it works even where the list
    endpoint's filters are not granted."""
    from testifield_bridge.hr import HR_DOCTYPES, HR_USER
    if not isinstance(tf_ref, str) or not tf_ref:
        frappe.throw(_("tf_ref is required, as text"))
    if doctype in HR_DOCTYPES:
        # records about people: the HR integration user only, never the finance key or a Desk user
        if frappe.session.user != HR_USER:
            frappe.throw(_("Only {0} may look up {1}").format(HR_USER, doctype), frappe.PermissionError)
    elif doctype not in TF_DOCTYPES:
        frappe.throw(_("{0} is not a doctype Testifield posts to").format(doctype))
    # HR records through the HR user's own permissions (company, User Permissions)
    lookup = frappe.get_list if doctype in HR_DOCTYPES else frappe.get_all
    rows = lookup(doctype, filters={"tf_ref": tf_ref}, fields=["name", "docstatus", "modified"], limit=1)
    return rows[0] if rows else None
