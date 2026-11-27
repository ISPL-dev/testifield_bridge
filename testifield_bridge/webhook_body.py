"""The body of every Testifield webhook, as the Jinja template Frappe renders
for each event. Every value goes in through `tojson`, never inside a quoted
string, so a name, a modified time or a tf_ref holding a quote, a backslash or
a newline still gives valid JSON (a body that does not parse is refused by
Testifield, which would then never learn of the submit or the cancel). No
frappe import, so it can be rendered and tested on its own."""
import json

FIELDS = ("doctype", "name", "modified", "docstatus", "tf_ref")


# For a record about a person (the HR doctypes), only what Testifield needs to
# follow a decision: never the document itself, so no date of birth, bank
# account, salary or CV is copied into Testifield on each save (#149).
DECISION_FIELDS = ("tf_user", "employee", "status", "approval_status")


def body_template(event, whole_doc=True):
    """The webhook_json for one event: doctype, name, event, modified,
    docstatus, tf_ref, each a JSON value; then the whole document (finance
    doctypes) or only the decision fields (HR doctypes, whole_doc=False)."""
    parts = [f'"event": {json.dumps(event)}']
    # a field the document does not carry is null, not a render error
    parts += [f'"{f}": {{{{ doc.{f} | default(none) | tojson }}}}' for f in FIELDS]
    if whole_doc:
        parts.append('"doc": {{ doc | tojson }}')
    else:
        inner = ", ".join(f'"{f}": {{{{ doc.{f} | default(none) | tojson }}}}' for f in DECISION_FIELDS)
        parts.append('"doc": {' + inner + '}')
    return "{" + ", ".join(parts) + "}"
