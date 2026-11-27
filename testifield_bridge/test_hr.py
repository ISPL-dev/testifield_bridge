"""The HR side's logic (hr.py, #149), without a site: a small stand-in for the
frappe module records what hr.py asks of it. Only installed when the real
frappe cannot be imported, so bench run-tests on a site is unaffected.

    python3 -m unittest testifield_bridge.test_hr
"""
import sys
import types
import unittest


class _Throw(Exception):
    pass


class _Doc(dict):
    """A document: attribute access, flags, insert / save / submit recorded."""
    def __init__(self, doctype, store, **kw):
        super().__init__(kw)
        self.doctype = doctype
        self.flags = types.SimpleNamespace(ignore_permissions=False, name_set=False)
        self._store = store
        self.setdefault("docstatus", 0)
        self.setdefault("roles", [])

    def __getattr__(self, k):
        if k.startswith("_") or k in ("doctype", "flags"):
            raise AttributeError(k)
        return self.get(k)

    def __setattr__(self, k, v):
        if k in ("doctype", "flags", "_store"):
            object.__setattr__(self, k, v)
        else:
            self[k] = v

    def set(self, k, v):
        self[k] = v

    def append(self, k, v):
        self.setdefault(k, []).append(types.SimpleNamespace(**v))

    def update(self, d):
        for k, v in d.items():
            self[k] = v

    def insert(self):
        self._store.events.append(("insert", self.doctype, self.flags.ignore_permissions))
        self.setdefault("name", f"{self.doctype}-{len(self._store.docs) + 1}")
        self._store.docs[(self.doctype, self["name"])] = self
        return self

    def save(self):
        self._store.events.append(("save", self.doctype, self.flags.ignore_permissions))
        self.setdefault("name", self.get("email"))    # a User is named by its email
        self._store.docs[(self.doctype, self.get("name") or self.get("email"))] = self

    def run_method(self, m):
        self._store.events.append(("run_method", m))

    def calculate_work_experience_and_amount(self):
        # hrms recomputes from salary slips and the rule; a test sets "recomputed" to differ
        return {"current_work_experience": 1, "amount": self.get("recomputed", self.get("amount"))}

    def submit(self):
        self._store.events.append(("submit", self.doctype, self.flags.ignore_permissions))
        self["docstatus"] = 1


def _stub_frappe():
    store = types.SimpleNamespace(docs={}, events=[], perms=[], roles={"HR User", "HR Manager", "Expense Approver",
                                                                       "Leave Approver", "Interviewer", "System Manager"},
                                  doctypes={"Job Requisition", "Employee Separation", "Exit Interview", "Gratuity", "Employee"},
                                  readable_employees={"EMP-1", "EMP-2"}, unreadable={"Other Company Ltd", "PROJ-OTHER"}, rolled_back=[])
    f = types.ModuleType("frappe")
    f.PermissionError = type("PermissionError", (_Throw,), {})
    f.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
    f.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    f._ = lambda s: s

    def throw(msg, exc=None):
        raise (exc or _Throw)(msg)
    f.throw = throw
    f.session = types.SimpleNamespace(user="Guest")
    f.parse_json = __import__("json").loads
    f.generate_hash = lambda length=10: "h" * length
    f.get_installed_apps = lambda: ["frappe", "erpnext", "hrms", "testifield_bridge"]
    f.new_doc = lambda dt: _Doc(dt, store)
    store.not_unique = set()
    f.get_meta = lambda dt: types.SimpleNamespace(get_field=lambda fn: types.SimpleNamespace(unique=0 if dt in store.not_unique else 1))

    def get_doc(dt, name, for_update=False):
        if for_update:
            store.events.append(("for_update", dt, name))
        d = store.docs.get((dt, name))
        if d is None:
            raise _Throw(f"{dt} {name} not found")
        return d
    f.get_doc = get_doc

    def exists(dt, name):
        if dt == "Role":
            return name in store.roles
        if dt == "DocType":
            return name in store.doctypes
        return (dt, name) in store.docs
    def get_value(dt, filters, fields, as_dict=False):
        for (t, _n), d in store.docs.items():
            if t == dt and all(d.get(k) == v for k, v in filters.items()):
                return types.SimpleNamespace(name=d["name"], docstatus=d["docstatus"])
        return None
    f.db = types.SimpleNamespace(exists=exists, get_value=get_value, commit=lambda: store.events.append(("commit",)),
                                 rollback=lambda: store.rolled_back.append(True),
                                 sql=lambda q, *a: store.events.append(("sql", q.split("(")[0])) or [[1]])
    f.get_all = lambda table, filters, fields: [types.SimpleNamespace(role=r) for (t, p, r, sub) in store.perms
                                                if t == table and p == filters["parent"] and sub == filters.get("submit", sub)]
    f.has_permission = lambda dt, ptype, doc=None, user=None: doc in store.readable_employees if dt == "Employee" else doc not in store.unreadable
    pw = types.ModuleType("frappe.utils.password")
    pw.get_decrypted_password = lambda *a, **k: "SECRET"
    sys.modules.update({"frappe": f, "frappe.utils": types.ModuleType("frappe.utils"), "frappe.utils.password": pw})
    return f, store


try:
    import frappe  # noqa: F401  (on a real site: leave it alone)
    REAL = True
except ImportError:
    REAL = False


@unittest.skipIf(REAL, "runs without a site; on a site the bridge is exercised by the smoke")
class HrBridge(unittest.TestCase):
    def setUp(self):
        self.frappe, self.store = _stub_frappe()
        import importlib
        import testifield_bridge
        sys.modules.pop("testifield_bridge.hr", None)
        if hasattr(testifield_bridge, "hr"):
            delattr(testifield_bridge, "hr")
        self.hr = importlib.import_module("testifield_bridge.hr")    # bound to this test's stand-in

    def as_hr(self):
        self.frappe.session.user = self.hr.HR_USER

    def test_only_the_hr_user_may_call_the_methods(self):
        self.frappe.session.user = "testifield@erp.internal"     # the finance user
        with self.assertRaises(self.frappe.PermissionError):
            self.hr.create_job_requisition({"tf_ref": "r1", "tf_user": "u"})
        self.assertEqual(self.store.events, [])

    def test_tf_ref_and_tf_user_are_required(self):
        self.as_hr()
        with self.assertRaises(_Throw):
            self.hr.create_employee_separation({"tf_user": "u"})
        with self.assertRaises(_Throw):
            self.hr.create_exit_interview({"tf_ref": "x1"})

    def test_create_finds_before_it_creates_and_inserts_with_ignore_permissions(self):
        self.as_hr()
        with self.assertRaisesRegex(_Throw, "does not accept"):
            self.hr.create_job_requisition('{"tf_ref": "jr:1", "tf_user": "asha", "designation": "Surveyor", "docstatus": 1}')
        first = self.hr.create_job_requisition('{"tf_ref": "jr:1", "tf_user": "asha", "designation": "Surveyor"}')
        again = self.hr.create_job_requisition({"tf_ref": "jr:1", "tf_user": "asha", "designation": "Surveyor"})
        self.assertFalse(first["existing"])
        self.assertTrue(again["existing"])
        self.assertEqual(first["name"], again["name"])
        self.assertEqual(first["docstatus"], 0, "made as a draft")
        self.assertEqual([e for e in self.store.events if e[0] == "insert"], [("insert", "Job Requisition", True)])

    def _draft_gratuity(self):
        g = self.frappe.new_doc("Gratuity")
        g.update({"name": "GR-1", "tf_ref": "gr:1", "employee": "EMP-1", "amount": 48076.92, "modified": "2026-11-27 10:00:00"})
        g.insert()
        return g

    def test_submit_gratuity_checks_the_tf_ref_and_submits_once(self):
        self.as_hr()
        self._draft_gratuity()
        approved = ("EMP-1", 48076.92, "2026-11-27 10:00:00")
        with self.assertRaises(_Throw):
            self.hr.submit_gratuity("GR-1", "gr:other", "asha", *approved)
        out = self.hr.submit_gratuity("GR-1", "gr:1", "asha", *approved)
        self.assertEqual(out["docstatus"], 1)
        self.assertEqual(self.store.docs[("Gratuity", "GR-1")]["tf_user"], "asha")
        again = self.hr.submit_gratuity("GR-1", "gr:1", "asha", *approved)
        self.assertTrue(again["existing"])
        self.assertEqual([e for e in self.store.events if e[0] == "submit"], [("submit", "Gratuity", True)])

    def test_submit_gratuity_refuses_a_draft_edited_after_it_was_approved(self):
        self.as_hr()
        g = self._draft_gratuity()
        g["amount"] = 96153.84           # someone in Desk changed the rule or the amount
        with self.assertRaisesRegex(_Throw, "amount changed"):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")
        g["amount"] = 48076.92; g["modified"] = "2026-11-27 10:05:00"
        with self.assertRaisesRegex(_Throw, "modified changed"):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")
        self.assertFalse([e for e in self.store.events if e[0] == "submit"])

    def test_submit_gratuity_refuses_when_frappe_hr_would_post_another_amount(self):
        self.as_hr()
        g = self._draft_gratuity()
        g["recomputed"] = 52000.00       # a salary slip or the relieving date changed after approval
        with self.assertRaisesRegex(_Throw, "amount changed"):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")
        with self.assertRaisesRegex(_Throw, "name is required"):
            self.hr.submit_gratuity({"tf_ref": "gr:1"}, "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")
        self.assertFalse([e for e in self.store.events if e[0] == "submit"])

    def test_submit_gratuity_refuses_when_submit_itself_recomputes_another_amount(self):
        self.as_hr()
        g = self._draft_gratuity()
        def submit_recomputes():          # a salary slip changed between the check and the submit
            g["docstatus"] = 1; g["amount"] = 50000.00
        object.__setattr__(g, "submit", submit_recomputes)
        with self.assertRaisesRegex(_Throw, "would post 50000.0, not the 48076.92 approved"):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")

    def test_the_new_row_is_committed_before_the_lock_is_released(self):
        self.as_hr()
        self.hr.create_exit_interview({"tf_ref": "x:2", "tf_user": "u", "employee": "EMP-1"})
        order = [e[0] if e[0] != "sql" else e[1] for e in self.store.events if e[0] in ("insert", "commit", "sql")]
        self.assertEqual(order, ["select get_lock", "insert", "commit", "select release_lock"])

    def test_a_duplicate_tf_ref_at_insert_returns_the_row_that_holds_it(self):
        self.as_hr()
        held = self.frappe.new_doc("Exit Interview"); held.update({"name": "EXIT-7", "tf_ref": "x:7"})
        real_get_value = self.frappe.db.get_value
        calls = []
        def unseen_once(*a, **k):        # the first find misses it (read before the row was visible)
            calls.append(1)
            return None if len(calls) == 1 else real_get_value(*a, **k)
        self.frappe.db.get_value = unseen_once
        def insert_clashes():
            self.store.docs[("Exit Interview", "EXIT-7")] = held
            raise self.frappe.DuplicateEntryError("Duplicate entry 'x:7' for key 'tf_ref'")
        orig_new = self.frappe.new_doc
        def new_doc(dt):
            d = orig_new(dt); object.__setattr__(d, "insert", insert_clashes); return d
        self.frappe.new_doc = new_doc
        out = self.hr.create_exit_interview({"tf_ref": "x:7", "tf_user": "u", "employee": "EMP-1"})
        self.assertEqual(out, {"name": "EXIT-7", "docstatus": 0, "existing": True})
        self.assertEqual(self.store.rolled_back, [True])

    def test_create_refuses_a_company_or_project_testifield_hr_may_not_read(self):
        self.as_hr()
        with self.assertRaisesRegex(self.frappe.PermissionError, "Company Other Company Ltd"):
            self.hr.create_job_requisition({"tf_ref": "jr:5", "tf_user": "u", "designation": "Surveyor", "company": "Other Company Ltd"})
        with self.assertRaisesRegex(self.frappe.PermissionError, "Project PROJ-OTHER"):
            self.hr.create_employee_separation({"tf_ref": "s:5", "tf_user": "u", "employee": "EMP-1", "project": "PROJ-OTHER"})
        with self.assertRaises(self.frappe.PermissionError):
            self.hr.create_exit_interview({"tf_ref": "x:5", "tf_user": "u", "employee": "EMP-1",
                                           "reference_document_type": "Employee", "reference_document_name": "EMP-OTHER-COMPANY"})
        self.assertFalse([e for e in self.store.events if e[0] == "insert"])
        out = self.hr.create_job_requisition({"tf_ref": "jr:6", "tf_user": "u", "designation": "Surveyor", "company": "INOV IT Solutions Pvt Ltd"})
        self.assertFalse(out["existing"])

    def test_submit_gratuity_refuses_a_gratuity_testifield_hr_may_not_read(self):
        self.as_hr()
        self._draft_gratuity()
        self.store.unreadable.add("GR-1")
        with self.assertRaisesRegex(self.frappe.PermissionError, "Gratuity GR-1"):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")
        self.assertFalse([e for e in self.store.events if e[0] == "submit"])

    def test_an_exit_interview_reference_type_needs_its_name(self):
        self.as_hr()
        with self.assertRaisesRegex(_Throw, "only with reference_document_name"):
            self.hr.create_exit_interview({"tf_ref": "x:8", "tf_user": "u", "employee": "EMP-1", "reference_document_type": "User"})

    def test_submit_gratuity_reads_the_draft_for_update(self):
        self.as_hr()
        self._draft_gratuity()
        self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-1", 48076.92, "2026-11-27 10:00:00")
        self.assertIn(("for_update", "Gratuity", "GR-1"), self.store.events)

    def test_create_refuses_a_doctype_whose_tf_ref_a_migrate_left_not_unique(self):
        self.as_hr()
        self.store.not_unique.add("Exit Interview")
        with self.assertRaisesRegex(_Throw, "not unique on this site"):
            self.hr.create_exit_interview({"tf_ref": "x:9", "tf_user": "u", "employee": "EMP-1"})
        self.assertFalse([e for e in self.store.events if e[0] == "insert"])

    def test_create_checks_value_types_and_every_employee_it_names(self):
        self.as_hr()
        with self.assertRaisesRegex(_Throw, "single value"):
            self.hr.create_exit_interview({"tf_ref": "x:3", "tf_user": "u", "employee": {"name": "EMP-1"}})
        with self.assertRaises(self.frappe.PermissionError):
            self.hr.create_job_requisition({"tf_ref": "jr:9", "tf_user": "u", "designation": "Surveyor",
                                            "requested_by": "EMP-OTHER-COMPANY"})
        with self.assertRaisesRegex(_Throw, "does not accept"):
            self.hr.create_employee_separation({"tf_ref": "s:2", "tf_user": "u", "employee": "EMP-1",
                                                "notify_users_by_email": 1})
        self.assertFalse([e for e in self.store.events if e[0] == "insert"])

    def test_hr_webhooks_fire_only_for_records_testifield_made(self):
        import importlib
        import testifield_bridge
        for m in ("setup", "api"):
            sys.modules.pop(f"testifield_bridge.{m}", None)
            if hasattr(testifield_bridge, m):
                delattr(testifield_bridge, m)
        setup = importlib.import_module("testifield_bridge.setup")
        _Doc.get_password = lambda self, *a, **k: None
        self.frappe.get_meta = lambda dt: types.SimpleNamespace(is_submittable=dt in ("Purchase Receipt", "Gratuity"))
        self.addCleanup(lambda: delattr(_Doc, "get_password"))
        setup.ensure_webhooks(url="https://tf.example/api/finance/webhook", secret="s")
        hooks = {d["webhook_doctype"]: d for (t, _n), d in self.store.docs.items() if t == "Webhook"}
        self.assertEqual(hooks["Employee Separation"]["condition"], "doc.tf_ref")
        self.assertEqual(hooks["Gratuity"]["condition"], "doc.tf_ref")
        self.assertEqual(hooks["Purchase Receipt"]["condition"], "")

    def _fields_stub(self, shared_counts):
        made = {}
        cf = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
        cf.create_custom_fields = lambda fields, **k: made.update(fields)
        for m in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
            sys.modules[m] = types.ModuleType(m)
        sys.modules["frappe.custom.doctype.custom_field.custom_field"] = cf
        self.frappe.get_meta = lambda dt: types.SimpleNamespace(fields=[types.SimpleNamespace(fieldname="employee")])
        self.frappe.db.has_column = lambda dt, col: True
        logged = []
        self.frappe.log_error = lambda title, message: logged.append(message)
        def sql(q, *a):
            self.store.events.append(("sql", q.split(" ")[0]))
            dt = q.split("`tab")[1].split("`")[0]
            return [[shared_counts.get(dt, (0, 0))[0 if "having" in q else 1]]]
        self.frappe.db.sql = sql
        return made, logged

    def test_tf_ref_is_unique_where_no_records_share_one_and_nothing_is_rewritten(self):
        made, logged = self._fields_stub({"Employee": (2, 0)})
        self.hr.ensure_hr_custom_fields()                       # after_migrate: never stops the upgrade
        self.assertEqual(made["Employee"][0]["unique"], 0)
        self.assertEqual(made["Gratuity"][0]["unique"], 1)
        self.assertIn("shared_values", logged[0])
        self.assertNotIn("update", [e[1] for e in self.store.events if e[0] == "sql"])
        with self.assertRaisesRegex(_Throw, "a person must look"):
            self.hr.ensure_hr_custom_fields(strict=True)        # setup_hr: may fail, and says why

    def test_the_hr_secret_is_returned_only_when_the_key_is_made(self):
        first = self.hr.make_hr_integration_user()
        self.assertEqual(first["api_secret"], "SECRET")
        again = self.hr.make_hr_integration_user()
        self.assertNotIn("api_secret", again, "a re-run never prints the secret again")

    def test_submit_gratuity_never_confirms_a_submitted_one_for_another_employee(self):
        self.as_hr()
        g = self._draft_gratuity(); g["docstatus"] = 1
        with self.assertRaisesRegex(_Throw, "employee changed"):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-2", 48076.92, "2026-11-27 10:00:00")
        g["employee"] = "EMP-OTHER-COMPANY"
        with self.assertRaises(self.frappe.PermissionError):
            self.hr.submit_gratuity("GR-1", "gr:1", "asha", "EMP-OTHER-COMPANY", 48076.92, "2026-11-27 10:00:00")

    def test_the_hr_user_gets_the_five_hr_roles_and_never_system_manager(self):
        out = self.hr.make_hr_integration_user()
        self.assertEqual(out["roles"], sorted(self.hr.HR_ROLES))
        self.assertNotIn("System Manager", out["roles"])
        self.assertEqual(out["user"], self.hr.HR_USER)

    def test_submit_roles_reads_docperm_and_custom_docperm(self):
        self.store.perms += [("DocPerm", "Gratuity", "HR Manager", 1), ("Custom DocPerm", "Gratuity", "Accounts Manager", 1),
                             ("DocPerm", "Gratuity", "HR User", 0), ("DocPerm", "Leave Application", "Leave Approver", 1)]
        self.assertEqual(self.hr.submit_roles("Gratuity"), ["Accounts Manager", "HR Manager"])
        self.assertEqual(self.hr.submit_roles("Exit Interview"), [])

    def test_create_refuses_fields_outside_its_list_and_a_tf_ref_that_is_not_text(self):
        self.as_hr()
        for bad in ({"tf_ref": "s:1", "tf_user": "u", "employee": "EMP-1", "amended_from": "SEP-9"},
                    {"tf_ref": "s:1", "tf_user": "u", "employee": "EMP-1", "naming_series": "X-"}):
            with self.assertRaisesRegex(_Throw, "does not accept"):
                self.hr.create_employee_separation(bad)
        with self.assertRaisesRegex(_Throw, "as text"):
            self.hr.create_employee_separation({"tf_ref": ["is", "set"], "tf_user": "u", "employee": "EMP-1"})
        self.assertFalse([e for e in self.store.events if e[0] == "insert"])

    def test_create_refuses_an_employee_testifield_hr_may_not_read(self):
        self.as_hr()
        with self.assertRaises(self.frappe.PermissionError):
            self.hr.create_exit_interview({"tf_ref": "x:1", "tf_user": "u", "employee": "EMP-OTHER-COMPANY"})
        out = self.hr.create_exit_interview({"tf_ref": "x:1", "tf_user": "u", "employee": "EMP-1"})
        self.assertFalse(out["existing"])

    def test_the_hr_user_is_refused_when_a_role_is_missing_on_the_site(self):
        self.store.roles.discard("Interviewer")
        with self.assertRaisesRegex(_Throw, "Roles missing"):
            self.hr.make_hr_integration_user()
        self.assertFalse([e for e in self.store.events if e[0] == "save"])

    def test_the_components_and_the_rule_only_on_a_sandbox(self):
        settings = self.frappe.new_doc("HR Settings")
        settings.meta = types.SimpleNamespace(has_field=lambda f: True)
        self.frappe.get_single = lambda dt: settings
        self.hr.ensure_hr_custom_fields = lambda **k: []       # needs a site's meta; covered on the box
        out = self.hr.setup_hr(sandbox=0)
        self.assertIn("not on a site that is not a sandbox", out["components"])
        self.assertFalse(self.frappe.db.exists("Salary Component", "ESI (Employee)"))

    def test_find_by_tf_ref_on_hr_doctypes_is_for_testifield_hr_only_and_takes_text(self):
        import importlib
        import testifield_bridge
        sys.modules.pop("testifield_bridge.api", None)
        if hasattr(testifield_bridge, "api"):
            delattr(testifield_bridge, "api")
        api = importlib.import_module("testifield_bridge.api")
        used = []
        self.frappe.get_all = lambda dt, filters, fields, limit: used.append("get_all") or []
        self.frappe.get_list = lambda dt, filters, fields, limit: used.append("get_list") or []
        self.frappe.session.user = "testifield@erp.internal"      # the finance key
        with self.assertRaises(self.frappe.PermissionError):
            api.find_by_tf_ref("Employee Separation", "sep:1")
        self.as_hr()
        self.assertIsNone(api.find_by_tf_ref("Employee Separation", "sep:1"))
        self.assertEqual(used, ["get_list"], "an HR record through the HR user's own permissions")
        with self.assertRaisesRegex(_Throw, "as text"):
            api.find_by_tf_ref("Employee Separation", ["like", "sep:%"])


if __name__ == "__main__":
    unittest.main()
