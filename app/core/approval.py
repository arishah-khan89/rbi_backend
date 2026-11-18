"""States of an inspection record in the approval workflow and the wording of the locks."""

DRAFT, PENDING, RETURNED, APPROVED = "draft", "pending", "returned", "approved"
STATES = (DRAFT, PENDING, RETURNED, APPROVED)
LOCKED = (PENDING, APPROVED)          # nobody edits, deletes or adds readings to a record in these states
REVIEWER_ROLES = ("admin",)          # admins review; the super admin oversees (can withdraw / reopen) but is not a reviewer


EDITABLE = (DRAFT, RETURNED)          # the states in which the owner changes and sends the inspection


class RecordLockedError(ValueError):
    """The inspection is waiting for approval or approved: its readings cannot be changed."""


class NotOwnerError(ValueError):
    """The inspection belongs to somebody else: only that person changes it or sends it for approval."""


def owner_of(record) -> str | None:
    """Whose inspection it is: the person who sent it (it may have come back to them), otherwise who entered it.
    Old records and records made while sign-in was off have no owner ("System"); anybody who may write can work on them."""
    for name in (record.submitted_by, record.created_by):
        if name and name.strip() and name.strip().lower() != "system":
            return name.strip()
    return None


def ownership_problem(record, actor: str | None) -> str | None:
    """A sentence when `actor` may not change / send this draft or returned inspection, else None.
    Locked inspections are handled by the lock rules; with sign-in off (actor "System") nobody is held back."""
    if not actor or actor.strip().lower() == "system" or record.approval_status not in EDITABLE:
        return None
    owner = owner_of(record)
    if owner and owner.lower() != actor.strip().lower():
        return (f"The inspection of {record.equipment_tag} on {record.inspection_date:%d-%b-%Y} belongs to {owner}. "
                f"Only {owner} can change it or send it for approval.")
    return None


def locked_message(record) -> str:
    what = f"{record.equipment_tag} on {record.inspection_date:%d-%b-%Y}"
    if record.approval_status == APPROVED:
        return (f"The inspection of {what} is approved and locked. The admin who approved it can reopen it "
                f"(with a reason) if it must be changed.")
    return f"The inspection of {what} is waiting for approval and is locked. Withdraw it first if it must be changed."
