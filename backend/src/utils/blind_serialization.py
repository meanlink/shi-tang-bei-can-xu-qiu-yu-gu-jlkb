from src.services.blind_engine import RULE_LABELS


def campaign_dict(c):
    return {
        "id": c.id, "name": c.name, "dataset_id": c.dataset_id,
        "current_rule": c.current_rule, "candidate_rule": c.candidate_rule,
        "threshold": c.threshold, "start_date": c.start_date, "end_date": c.end_date,
        "lock_hour": c.lock_hour, "status": c.status, "active_rule": c.active_rule,
        "active_rule_label": RULE_LABELS.get(c.active_rule, c.active_rule),
        "created_at": c.created_at, "closed_at": c.closed_at,
    }


def day_dict(d):
    return {
        "id": d.id, "campaign_id": d.campaign_id, "day": d.day,
        "current_forecast": d.current_forecast, "candidate_forecast": d.candidate_forecast,
        "current_locked_at": d.current_locked_at, "candidate_locked_at": d.candidate_locked_at,
        "current_late": bool(d.current_late), "candidate_late": bool(d.candidate_late),
        "actual": d.actual, "actual_reported_at": d.actual_reported_at,
        "score_version": d.score_version, "scored": bool(d.scored),
        "excluded_reason": d.excluded_reason,
    }


def version_dict(v):
    return {
        "id": v.id, "campaign_id": v.campaign_id, "version": v.version,
        "status": v.status, "result": v.result, "decision": v.decision,
        "basis": v.basis, "created_at": v.created_at,
    }


def promotion_dict(p):
    return {
        "id": p.id, "campaign_id": p.campaign_id, "action": p.action,
        "from_rule": p.from_rule, "to_rule": p.to_rule,
        "score_version_id": p.score_version_id, "reason": p.reason,
        "actor": p.actor, "at": p.at,
    }


def trial_dict(t):
    return {
        "id": t.id, "campaign_id": t.campaign_id, "name": t.name, "status": t.status,
        "bound_rule": t.bound_rule, "bound_rule_label": RULE_LABELS.get(t.bound_rule, t.bound_rule),
        "rule_snapshot": t.rule_snapshot, "target_day": t.target_day,
        "result": t.result, "error": t.error,
        "created_at": t.created_at, "finished_at": t.finished_at,
    }
