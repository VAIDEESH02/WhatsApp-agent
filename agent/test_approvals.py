"""Tests for agent.approvals (temp SQLite DB). Run: python3 -m pytest agent/test_approvals.py -v"""

import pytest

from agent import approvals


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "DB_PATH", tmp_path / "state.sqlite3")


def add(text="Dinner ku varuviya?", draft="Paakalam da"):
    return approvals.add_pending("919000000001@s.whatsapp.net", "919000000001@s.whatsapp.net", "family", text, draft, "needs approval: plan")


def test_new_drafts_are_pending_and_not_in_outbox():
    approval_id = add()
    assert approvals.get(approval_id)["status"] == "pending"
    assert approvals.outbox() == []
    assert approvals.counts()["pending"] == 1


def test_approve_with_edit_puts_edited_text_in_outbox():
    approval_id = add()
    approved = approvals.approve(approval_id, "  Paakalam da, will confirm  ")
    assert approved["status"] == "approved" and approved["final_text"] == "Paakalam da, will confirm"
    assert approvals.outbox()[0]["text"] == "Paakalam da, will confirm"


def test_approve_without_edit_uses_the_draft():
    approval_id = add()
    assert approvals.approve(approval_id)["final_text"] == "Paakalam da"


def test_empty_edited_text_is_refused():
    approval_id = add()
    with pytest.raises(approvals.ApprovalError, match="empty"):
        approvals.approve(approval_id, "   ")
    assert approvals.get(approval_id)["status"] == "pending"


def test_dismissed_drafts_never_reach_the_outbox():
    approval_id = add()
    approvals.dismiss(approval_id)
    assert approvals.outbox() == []
    with pytest.raises(approvals.ApprovalError, match="dismissed"):
        approvals.approve(approval_id)


def test_sent_and_dry_run_leave_the_outbox_once():
    first, second = add(), add()
    approvals.approve(first)
    approvals.approve(second)
    assert approvals.mark_sent(first)["status"] == "sent"
    assert approvals.mark_sent(second, dry_run=True)["status"] == "dry_run"
    assert approvals.outbox() == []
    with pytest.raises(approvals.ApprovalError):
        approvals.mark_sent(first)


def test_only_pending_can_be_approved_and_only_approved_can_be_sent():
    approval_id = add()
    with pytest.raises(approvals.ApprovalError, match="expected approved"):
        approvals.mark_sent(approval_id)
    approvals.approve(approval_id)
    with pytest.raises(approvals.ApprovalError, match="expected pending"):
        approvals.approve(approval_id)


def test_unknown_id_is_reported():
    with pytest.raises(approvals.ApprovalError, match="not found"):
        approvals.dismiss(999)


def test_list_is_newest_first_and_filterable():
    ids = [add(text=f"message {i}") for i in range(3)]
    approvals.dismiss(ids[0])
    assert [row["id"] for row in approvals.list_approvals()] == list(reversed(ids))
    assert [row["id"] for row in approvals.list_approvals("pending")] == [ids[2], ids[1]]
