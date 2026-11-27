"""The webhook body renders to valid JSON whatever the document's name,
modified time or tf_ref holds. Rendered with Jinja's sandbox, as Frappe
renders webhook_json; needs only jinja2 (no site):

    python3 -m unittest testifield_bridge.test_webhook_body
"""
import json
import unittest

from jinja2.sandbox import SandboxedEnvironment

from testifield_bridge.webhook_body import body_template

ENV = SandboxedEnvironment()


def render(event, doc):
    return ENV.from_string(body_template(event)).render(doc=doc)


class WebhookBody(unittest.TestCase):
    def test_hostile_values_stay_valid_json_and_unchanged(self):
        doc = {
            "doctype": "Purchase Receipt",
            "name": 'MAT-PRE-"1"\\x\nend',
            "modified": "2026-11-26 10:00:00.123456",
            "docstatus": 1,
            "tf_ref": 'tf:rcpt:"quoted"',
            "items": [{"item_code": "LED-120W", "qty": 1}],
        }
        body = json.loads(render("on_submit", doc))
        self.assertEqual(body["event"], "on_submit")
        for field in ("doctype", "name", "modified", "docstatus", "tf_ref"):
            self.assertEqual(body[field], doc[field], field)
        self.assertEqual(body["doc"], doc)

    def test_a_document_without_tf_ref_sends_null_not_the_word_none(self):
        body = json.loads(render("on_update", {"doctype": "Project", "name": "PROJ-0001", "modified": "x", "docstatus": 0}))
        self.assertIsNone(body["tf_ref"])

    def test_html_characters_are_escaped_but_round_trip(self):
        doc = {"doctype": "Journal Entry", "name": "<b>&'", "modified": "m", "docstatus": 2, "tf_ref": "a</script>"}
        text = render("on_cancel", doc)
        self.assertNotIn("</script>", text)
        self.assertEqual(json.loads(text)["tf_ref"], "a</script>")

    def test_an_hr_record_sends_no_personal_fields_only_the_decision(self):
        doc = {"doctype": "Employee", "name": "EMP-1", "modified": "m", "docstatus": 0, "tf_ref": "w:1",
               "date_of_birth": "1990-01-01", "bank_ac_no": "123456789", "personal_email": "a@b.c", "ctc": 600000,
               "status": "Active", "employee": "EMP-1", "tf_user": "asha"}
        text = ENV.from_string(body_template("on_update", whole_doc=False)).render(doc=doc)
        body = json.loads(text)
        for secret in ("1990-01-01", "123456789", "a@b.c", "600000"):
            self.assertNotIn(secret, text)
        self.assertEqual(body["doc"], {"tf_user": "asha", "employee": "EMP-1", "status": "Active", "approval_status": None})
        self.assertEqual(body["tf_ref"], "w:1")


if __name__ == "__main__":
    unittest.main()
