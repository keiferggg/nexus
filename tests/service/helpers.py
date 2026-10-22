from nexus.domain.missions import create_mission, start_mission


def new_mission(ctx, project_id, *, objective="Brief the Larkspur council on a bike-share pilot",
                success=("Research resident demand using workspace sources", "Write a one-page recommendation for the council"),
                constraints=("Use only workspace sources",), mode="offline_fake", cap=None, start=True, **kw):
    with ctx.db.tx() as cur:
        mid = create_mission(cur, project_id=project_id, title=objective[:60], objective=objective,
                             success_conditions=list(success), constraints=list(constraints), execution_mode=mode,
                             spend_cap_usd=cap, **kw)
        if start:
            start_mission(cur, mid)
    return mid


def tasks(ctx, mid):
    return {r["key"]: dict(r) for r in ctx.db.query("SELECT * FROM tasks WHERE mission_id = ?", (mid,))}


def events(ctx, mid, type_=None):
    q = "SELECT * FROM events WHERE mission_id = ?" + (" AND type = ?" if type_ else "") + " ORDER BY seq"
    return ctx.db.query(q, (mid, type_) if type_ else (mid,))
