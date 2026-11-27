"""The bridge's HR side (bridge 0.1.8, ISPL-IN/testifield #149): what Testifield
needs on a site that runs Frappe HR, read from docs/specs/hr-lifecycle.md,
The integration account.

- A second integration user, testifield-hr, with HR roles only (never System
  Manager), so a leak of one key is not a leak of the other.
- tf_ref and tf_user on the HR doctypes Testifield writes, and signed webhooks
  on them, so a decision taken in Desk reaches Testifield.
- Four atomic methods for what that user cannot do as shipped (create a Job
  Requisition, an Employee Separation, an Exit Interview; submit a Gratuity):
  each checks the caller and the tf_ref, finds before it creates, and only then
  acts with ignore_permissions. A method, never a rule or a screen (ADR-003).
- HR Settings' two self-approval switches on, as a second wall for Desk users;
  the ESI and Labour Welfare Fund components; a fixed-term Gratuity Rule at one
  year, copied from the Indian standard rule.
Nothing here runs on a site without hrms.
"""
import hashlib

import frappe
from frappe import _

HR_USER = "testifield-hr@erp.internal"
# hr-lifecycle.md, The integration account, 2: these five and nothing else
HR_ROLES = ["HR User", "HR Manager", "Expense Approver", "Leave Approver", "Interviewer"]
FORBIDDEN_ROLES = {"System Manager", "Administrator"}

# The HR doctypes Testifield writes or reads back (hr-lifecycle.md): recruitment,
# the employee record, attendance and leave, claims and advances, movement,
# separation and full and final, and the payroll inputs.
HR_DOCTYPES = [
    "Job Requisition", "Job Opening", "Job Applicant", "Interview", "Interview Feedback", "Job Offer",
    "Appointment Letter", "Employee Onboarding", "Employee", "Leave Application", "Attendance Request",
    "Attendance", "Expense Claim", "Employee Advance", "Employee Promotion", "Employee Transfer",
    "Employee Separation", "Exit Interview", "Full and Final Statement", "Gratuity", "Additional Salary",
]

# Salary components the India set-up does not ship (hr-lifecycle.md, 6); rates come
# from the salary structure, so these carry no figures
STATUTORY_COMPONENTS = [
    {"salary_component": "ESI (Employee)", "salary_component_abbr": "ESI", "type": "Deduction",
     "description": "Employees' State Insurance, the employee's share"},
    {"salary_component": "Labour Welfare Fund (Employee)", "salary_component_abbr": "LWF", "type": "Deduction",
     "description": "Labour Welfare Fund, the employee's share (state rates)"},
]
STANDARD_GRATUITY_RULE = "Indian Standard Gratuity Rule"
FIXED_TERM_GRATUITY_RULE = "Fixed-term Gratuity Rule (one year)"


def hrms_installed():
    return "hrms" in frappe.get_installed_apps()


def hr_doctypes_present():
    """Only the HR doctypes this site has (a doctype can be absent in an hrms version)."""
    return [dt for dt in HR_DOCTYPES if frappe.db.exists("DocType", dt)]


# ---- set-up (site-setup's HR step, hr-setup.sh) ---------------------------------------------

def setup_hr(sandbox=0):
    """Everything below, idempotent. Returns what it did, for the log. The fields
    and HR Settings' two switches are for any site with Frappe HR; the ESI/LWF
    components and the fixed-term Gratuity Rule (still to be tested, hr-lifecycle.md)
    only where the site is marked a sandbox, until hr has tested them."""
    if not hrms_installed():
        return {"hrms": False}
    out = {"hrms": True}
    out["fields"] = ensure_hr_custom_fields(strict=True)
    out["settings"] = ensure_hr_settings()
    if int(sandbox or 0) == 1:
        out["components"] = ensure_statutory_components()
        out["gratuity_rule"] = ensure_fixed_term_gratuity_rule()
    else:
        out["components"] = out["gratuity_rule"] = "not on a site that is not a sandbox (TF_SANDBOX=1), until tested"
    out["gratuity_submit_roles"] = submit_roles("Gratuity")
    frappe.db.commit()
    return out


def ensure_hr_custom_fields(strict=False):
    """tf_ref and tf_user on each HR doctype present, after its first field (HR
    doctypes do not all have a naming_series). tf_ref is unique where no two records
    share one. strict (setup_hr, a step allowed to fail) stops on a doctype whose
    records share one; after_migrate (never allowed to stop a site's upgrade) leaves
    that doctype's tf_ref not unique and logs only a count."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    fields = {}
    for dt in hr_doctypes_present():
        first = next((f.fieldname for f in frappe.get_meta(dt).fields if f.fieldname not in ("tf_ref", "tf_user")), None)
        fields[dt] = [
            {"fieldname": "tf_ref", "label": "Testifield ref", "fieldtype": "Data", "length": 140,
             **({"insert_after": first} if first else {}),
             "read_only": 1, "allow_on_submit": 0, "in_standard_filter": 1, "unique": 1, "no_copy": 1,
             "description": "Idempotency key from Testifield; the same event never makes two documents"},
            {"fieldname": "tf_user", "label": "Testifield user", "fieldtype": "Data", "length": 140,
             "insert_after": "tf_ref", "read_only": 1, "no_copy": 1,
             "description": "Who in Testifield decided this (the real approver; the submitter is the integration user)"},
        ]
    # tf_ref becomes unique only where no two records share a value. tf_ref is read-only
    # and set only by Testifield, so an empty string there is a blank, not a key: the
    # count is checked over non-empty values, and Frappe stores an empty unique field as
    # NULL from then on. Nothing is rewritten.
    shared = {}
    for dt in fields:
        if frappe.db.has_column(dt, "tf_ref"):
            n = frappe.db.sql(f"select count(*) from (select tf_ref from `tab{dt}` where ifnull(tf_ref, '') != '' group by tf_ref having count(*) > 1) d")[0][0]
            blank = frappe.db.sql(f"select count(*) from `tab{dt}` where tf_ref = ''")[0][0]
            if n or blank > 1:
                shared[dt] = {"shared_values": n, "blank": blank}
                fields[dt][0]["unique"] = 0
    if shared and strict:
        frappe.throw(_("tf_ref is shared or blank on more than one record in {0}: a person must look before the bridge is set up").format(shared))
    create_custom_fields(fields, ignore_validate=True, update=True)
    if shared:
        frappe.log_error(title="testifield_bridge: tf_ref left not unique", message=str(shared))
    return sorted(fields)


def ensure_hr_settings():
    """The two self-approval switches on. They compare the session user with the
    employee's user, so they never stop the integration user; Testifield refuses
    self-approval itself. These guard the payroll officer working in Desk."""
    settings = frappe.get_single("HR Settings")
    done = {}
    for field in ("prevent_self_leave_approval", "prevent_self_expense_approval"):
        if settings.meta.has_field(field):
            # what it was is logged, so a change on a client's site is never silent
            done[field] = {"was": settings.get(field), "now": 1}
            settings.set(field, 1)
        else:
            done[field] = "absent in this hrms version"
    settings.flags.ignore_permissions = True
    settings.save()
    return done


def ensure_statutory_components():
    made = []
    for c in STATUTORY_COMPONENTS:
        if frappe.db.exists("Salary Component", c["salary_component"]):
            continue
        doc = frappe.new_doc("Salary Component")
        doc.update(c)
        doc.flags.ignore_permissions = True
        doc.insert()
        made.append(c["salary_component"])
    return {"made": made}


def ensure_fixed_term_gratuity_rule():
    """A copy of the Indian standard rule with a minimum of one year (fixed-term
    staff, Labour Codes); the slabs and earnings stay the standard rule's."""
    if frappe.db.exists("Gratuity Rule", FIXED_TERM_GRATUITY_RULE):
        return "exists"
    if not frappe.db.exists("Gratuity Rule", STANDARD_GRATUITY_RULE):
        return f"not made: {STANDARD_GRATUITY_RULE} is not on this site"
    rule = frappe.copy_doc(frappe.get_doc("Gratuity Rule", STANDARD_GRATUITY_RULE))
    rule.name = FIXED_TERM_GRATUITY_RULE
    rule.flags.name_set = True
    rule.minimum_year_for_gratuity = 1
    rule.flags.ignore_permissions = True
    rule.insert()
    return "made"


def submit_roles(doctype):
    """Which roles can submit a doctype on this site, from its permission rows and
    any Custom DocPerm (hr-lifecycle.md asks this of Gratuity)."""
    roles = set()
    for table in ("DocPerm", "Custom DocPerm"):
        for row in frappe.get_all(table, filters={"parent": doctype, "submit": 1}, fields=["role"]):
            roles.add(row.role)
    return sorted(roles)


def make_hr_integration_user(email=HR_USER, full_name="Testifield HR", regenerate=False):
    """The HR integration user: the five HR roles and nothing else. Every role must
    exist on the site, and the roles are checked after validation (a Role Profile
    could add some) and before anything is saved. Returns the API key and secret
    (box-up.sh keeps them in a mode-600 file, never in a log)."""
    missing = [r for r in HR_ROLES if not frappe.db.exists("Role", r)]
    if missing:
        frappe.throw(_("Roles missing on this site, so testifield-hr cannot be made: {0}").format(missing))
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
    else:
        user = frappe.new_doc("User")
        user.email = email
        user.first_name = full_name
        user.user_type = "System User"
        user.send_welcome_email = 0
        user.enabled = 1
    user.role_profile_name = None
    user.set("roles", [])
    for r in HR_ROLES:
        user.append("roles", {"role": r})
    new_key = not user.api_key or bool(regenerate)
    if new_key:
        user.api_key = frappe.generate_hash(length=15)
        user.api_secret = frappe.generate_hash(length=15)
    user.flags.ignore_permissions = True
    user.run_method("validate")
    held = {r.role for r in user.get("roles")}
    if held != set(HR_ROLES):
        frappe.db.rollback()
        frappe.throw(_("testifield-hr would hold {0}, not exactly the five HR roles").format(sorted(held)))
    user.save()
    frappe.db.commit()
    out = {"user": user.name, "api_key": user.api_key, "roles": sorted(held)}
    if new_key:
        # only when made now: a re-run never prints it again (redact-setup.py keeps the file)
        from frappe.utils.password import get_decrypted_password
        out["api_secret"] = get_decrypted_password("User", user.name, "api_secret", raise_exception=False)
    return out


# ---- the four methods (called by testifield-hr only) ------------------------------------------

# What each create method accepts, besides tf_ref and tf_user: anything else is refused, so
# no caller can set naming, amendment, child-row identity or a higher-permlevel field
ALLOWED_FIELDS = {
    "Job Requisition": {"designation", "department", "company", "no_of_positions", "expected_compensation",
                        "reason_for_requesting", "description", "requested_by", "posting_date", "expected_by"},
    "Employee Separation": {"employee", "company", "department", "designation", "boarding_begins_on",
                            "resignation_letter_date", "project", "employee_separation_template"},
    "Exit Interview": {"employee", "company", "date", "reference_document_type", "reference_document_name"},
}


def _caller_is_hr_user():
    if frappe.session.user != HR_USER:
        frappe.throw(_("Only {0} may call this method").format(HR_USER), frappe.PermissionError)


def _ref(value, what):
    """A non-empty string: a list or a dict would be read as a filter by the lookups."""
    if not isinstance(value, str) or not value.strip():
        frappe.throw(_("{0} is required, as text").format(what))
    return value


# the links a create method may set, and the doctype each points to: testifield-hr may
# set only what its own permissions (company, User Permissions) let it read
LINKS = {"company": "Company", "department": "Department", "project": "Project", "designation": "Designation",
         "employee_separation_template": "Employee Separation Template"}


def _readable(doctype, name, field):
    if not frappe.has_permission(doctype, "read", doc=name, user=HR_USER):
        frappe.throw(_("{0} may not use {1} {2} ({3})").format(HR_USER, doctype, name, field), frappe.PermissionError)


def _employee_readable(employee):
    """testifield-hr acts for an employee only where its own permissions (company,
    User Permissions) let it read that employee, though the insert itself skips them."""
    if not frappe.has_permission("Employee", "read", doc=employee, user=HR_USER):
        frappe.throw(_("{0} may not act for employee {1}").format(HR_USER, employee), frappe.PermissionError)


def _create(doctype, doc):
    """Find by tf_ref, else insert as a draft with ignore_permissions; one at a time per
    tf_ref (a named lock, held until the new row is committed), and tf_ref is a unique
    column, so two overlapping retries never make two whatever the timing."""
    _caller_is_hr_user()
    doc = frappe.parse_json(doc) if isinstance(doc, str) else dict(doc)
    tf_ref = _ref(doc.pop("tf_ref", None), "tf_ref")
    tf_user = _ref(doc.pop("tf_user", None), "tf_user")
    # a doctype whose tf_ref a migrate left not unique (records shared one) takes no new
    # record from Testifield until a person has cleaned it: the lock alone is not enough
    field = frappe.get_meta(doctype).get_field("tf_ref")
    if not field or not field.unique:
        frappe.throw(_("{0}: tf_ref is not unique on this site (see the error log); a person must clean it first").format(doctype))
    extra = sorted(set(doc) - ALLOWED_FIELDS[doctype])
    if extra:
        frappe.throw(_("{0} does not accept {1} from Testifield").format(doctype, extra))
    # every value Testifield sends is plain data, and any employee it names (employee,
    # or a requisition's requested_by) is one testifield-hr may read
    for field, value in doc.items():
        if isinstance(value, (dict, list)):
            frappe.throw(_("{0} must be a single value").format(field))
    for field in ("employee", "requested_by"):
        if doc.get(field) is not None:
            _employee_readable(_ref(doc[field], field))
    for field, linked in LINKS.items():
        if doc.get(field):
            _readable(linked, _ref(doc[field], field), field)
    if doc.get("reference_document_type") and not doc.get("reference_document_name"):
        frappe.throw(_("reference_document_type is accepted only with reference_document_name"))
    if doc.get("reference_document_name"):
        _readable(_ref(doc.get("reference_document_type"), "reference_document_type"),
                  _ref(doc["reference_document_name"], "reference_document_name"), "reference_document_name")
    lock = "tf:" + hashlib.sha1(f"{doctype}\0{tf_ref}".encode()).hexdigest()   # one lock per tf_ref, never a shared prefix
    if not frappe.db.sql("select get_lock(%s, 10)", lock)[0][0]:
        frappe.throw(_("another call for {0} is running; retry").format(tf_ref))
    try:
        existing = frappe.db.get_value(doctype, {"tf_ref": tf_ref}, ["name", "docstatus"], as_dict=True)
        if existing:
            return {"name": existing.name, "docstatus": existing.docstatus, "existing": True}
        new = frappe.new_doc(doctype)
        new.update(doc)
        new.tf_ref = tf_ref
        new.tf_user = tf_user
        new.flags.ignore_permissions = True
        try:
            new.insert()
        except frappe.DuplicateEntryError:
            # the unique tf_ref: a row this call could not yet see (made over REST, or in a
            # read that came before the lock) is the one, found as any retry would find it
            frappe.db.rollback()
            existing = frappe.db.get_value(doctype, {"tf_ref": tf_ref}, ["name", "docstatus"], as_dict=True)
            if not existing:
                raise    # the clash was another key (a name), not tf_ref: say so as it is
            return {"name": existing.name, "docstatus": existing.docstatus, "existing": True}
        frappe.db.commit()      # before the lock goes: the next caller must see this row
        return {"name": new.name, "docstatus": new.docstatus, "existing": False}
    finally:
        frappe.db.sql("select release_lock(%s)", lock)


@frappe.whitelist()
def create_job_requisition(doc):
    """Job Requisition: System Manager only, as shipped."""
    return _create("Job Requisition", doc)


@frappe.whitelist()
def create_employee_separation(doc):
    """Employee Separation: System Manager only, as shipped."""
    return _create("Employee Separation", doc)


@frappe.whitelist()
def create_exit_interview(doc):
    """Exit Interview: System Manager only, as shipped."""
    return _create("Exit Interview", doc)


@frappe.whitelist()
def submit_gratuity(name, tf_ref, tf_user, employee, amount, modified):
    """Gratuity: no role has submit in its rows as shipped. Submits the named draft only
    when it is still the one the approver saw: the same tf_ref, employee and amount, and
    not edited since (its modified time, as str(datetime): "2026-11-27 10:00:00.123456").
    Frappe HR recomputes the amount on submit from the salary slips, the relieving date
    and the rule, so the check is on the recomputed amount. A submitted one is returned
    as it is."""
    _caller_is_hr_user()
    _ref(name, "name")
    _ref(tf_ref, "tf_ref")
    _ref(tf_user, "tf_user")
    _ref(employee, "employee")
    _ref(modified, "modified")
    # the Gratuity itself must be one testifield-hr may read (company, User Permissions),
    # not only its employee; then the row is locked from this read to the submit
    _readable("Gratuity", name, "name")
    doc = frappe.get_doc("Gratuity", name, for_update=True)
    if doc.get("tf_ref") != tf_ref:
        frappe.throw(_("Gratuity {0} does not carry tf_ref {1}").format(name, tf_ref))
    # who it is for is checked first, so a submitted one is never confirmed to a caller
    # that names another employee or may not read this one
    _employee_readable(employee)
    if doc.get("employee") != employee:
        frappe.throw(_("Gratuity {0} is not the one that was approved (employee changed): approve it again").format(name))
    if doc.docstatus == 1:
        return {"name": doc.name, "docstatus": 1, "existing": True}
    if doc.docstatus == 2:
        frappe.throw(_("Gratuity {0} is cancelled").format(name))
    # what submit would post: hrms' validate sets amount from this, on every save and submit
    recomputed = doc.calculate_work_experience_and_amount()["amount"]
    changed = [f for f, ok in (("employee", doc.get("employee") == employee),
                               ("amount", abs(float(doc.get("amount") or 0) - float(amount)) < 0.005
                                and abs(float(recomputed or 0) - float(amount)) < 0.005),
                               ("modified", str(doc.get("modified")) == modified)) if not ok]
    if changed:
        frappe.throw(_("Gratuity {0} is not the one that was approved ({1} changed): approve it again").format(name, ", ".join(changed)))
    doc.tf_user = tf_user
    doc.flags.ignore_permissions = True
    doc.submit()
    # submit runs hrms' validate, which recomputes the amount again: if a salary slip or
    # the rule changed since the check, what would post is not what was approved, and the
    # throw rolls the whole request back
    if abs(float(doc.get("amount") or 0) - float(amount)) >= 0.005:
        frappe.throw(_("Gratuity {0} would post {1}, not the {2} approved: approve it again").format(name, doc.get("amount"), amount))
    return {"name": doc.name, "docstatus": doc.docstatus, "existing": False}
