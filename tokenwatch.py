#!/usr/bin/env python3
"""Claude Code usage analyzer -> single-file HTML dashboard.

Reads ~/.claude/projects/**/*.jsonl transcripts, computes per-turn cost from
the `usage` block of every assistant record, and cross-checks the result
against the `cost-state` records Claude Code writes per session.

Usage:
    ./ccusage.py                                   # local machine only
    ./ccusage.py --source robot201=/tmp/r201/projects --source local=~/.claude/projects
    ./ccusage.py -o /tmp/report.html --open
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# --- Pricing (USD per 1M tokens), Anthropic first-party API rates ------------
# cache write 5m = 1.25x input, 1h = 2x input (standard multipliers).
PRICING = {
    "claude-opus-5-5":   {"in": 4.00, "out": 20.00, "cw5": 5.00,  "cw1h": 8.00,  "cr": 0.20},
    "claude-opus-5":     {"in": 5.00, "out": 25.00, "cw5": 6.25,  "cw1h": 10.00, "cr": 0.50},
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00, "cw5": 6.25,  "cw1h": 10.00, "cr": 0.50},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cw5": 2.50,  "cw1h": 4.00,  "cr": 0.10},
    "claude-sonnet-5":   {"in": 2.00, "out": 10.00, "cw5": 2.50,  "cw1h": 4.00,  "cr": 0.20},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50,  "cw5": 0.125, "cw1h": 0.20,  "cr": 0.01},
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00,  "cw5": 1.25,  "cw1h": 2.00,  "cr": 0.10},
    "claude-fable-5-2":  {"in": 8.00, "out": 40.00, "cw5": 10.00, "cw1h": 16.00, "cr": 0.20},
}
DEFAULT_MODEL = "claude-opus-5-5"


def price(model: str) -> dict:
    return PRICING.get(model or "", PRICING[DEFAULT_MODEL])


def cost_of(model: str, inp: int, out: int, cw5: int, cw1h: int, cr: int) -> float:
    p = price(model)
    return (inp * p["in"] + out * p["out"] + cw5 * p["cw5"]
            + cw1h * p["cw1h"] + cr * p["cr"]) / 1_000_000


def project_name(cwd: str, fallback: str) -> str:
    if not cwd:
        return fallback
    return Path(cwd).name or cwd


def parse_ts(s: str):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def blob_len(v) -> int:
    if v is None:
        return 0
    if isinstance(v, str):
        return len(v)
    if isinstance(v, (list, dict)):
        return len(json.dumps(v, ensure_ascii=False))
    return len(str(v))


class Agg:
    def __init__(self):
        self.days = defaultdict(lambda: dict(cost=0.0, inp=0, out=0, think=0, cr=0, cw=0, turns=0))
        self.sessions = {}
        self.tools = defaultdict(lambda: dict(calls=0, chars=0, errors=0))
        self.heat = defaultdict(float)          # (machine, dow, hour) -> cost
        self.models = defaultdict(lambda: dict(cost=0.0, turns=0, out=0))
        self.efforts = defaultdict(lambda: dict(cost=0.0, turns=0))
        self.branches = defaultdict(lambda: dict(cost=0.0, turns=0))
        self.declared = {}                       # sid -> cost-state totalCostUSD
        self.declared_meta = {}
        self.seen_msgs = set()                   # message.id dedup across all files
        self.titles = {}                         # sid -> {"ai": ..., "custom": ...}
        self.prompts = {}                        # (machine, sid, promptId) -> one request
        self.agent_calls = {}                    # tool_use_id -> {type, desc, session}
        self.agent_logs = defaultdict(lambda: dict(cost=0.0, turns=0, cr=0, cw=0, out=0))
        self.agent_link = {}                     # subagent log -> spawning tool_use_id
        self.ttl = defaultdict(lambda: dict(cost=0.0, turns=0, cw=0, cr=0, write_cost=0.0))


def session_id_of(path: Path, root: Path) -> tuple[str, bool]:
    """Return (owning session id, is_subagent).

    Main-thread logs live at <root>/<project>/<sid>.jsonl.
    Subagent logs live at <root>/<project>/<sid>/subagents/[workflows/<wf>/]<agent>.jsonl
    and are billed to the session that spawned them.
    """
    rel = path.relative_to(root).parts
    if len(rel) >= 3 and "subagents" in rel:
        return rel[1], True
    return path.stem, False


def scan_file(path: Path, machine: str, agg: Agg, since, until, root: Path,
              keep_prompts: bool = True):
    sid, is_agent = session_id_of(path, root)
    pending = {}            # tool_use_id -> tool name
    s = None
    prompt_key = None       # the request every following turn is billed to
    agent_key = (machine, path.stem)   # for a subagent log: its own cost bucket
    for raw in path.open(errors="ignore"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rtype = d.get("type")

        if rtype == "cost-state":
            key = (machine, d.get("sessionId") or sid)
            agg.declared[key] = d.get("totalCostUSD", 0.0)
            agg.declared_meta[key] = {
                "apiMs": d.get("totalAPIDuration", 0),
                "toolMs": d.get("totalToolDuration", 0),
                "wallMs": d.get("totalDuration", 0),
                "added": d.get("totalLinesAdded", 0),
                "removed": d.get("totalLinesRemoved", 0),
            }
            continue

        # Claude Code renames a session as it goes, so the last record wins.
        # A title the user typed outranks the generated one.
        if rtype in ("ai-title", "custom-title"):
            t = agg.titles.setdefault((machine, d.get("sessionId") or sid), {})
            if rtype == "ai-title":
                t["ai"] = d.get("aiTitle") or t.get("ai")
            else:
                t["custom"] = d.get("customTitle") or t.get("custom")
            continue

        msg = d.get("message") or {}
        content = msg.get("content")

        if rtype == "user":
            pid = d.get("promptId")
            if pid:
                prompt_key = (machine, sid, pid)
                p = agg.prompts.get(prompt_key)
                if p is None:
                    p = agg.prompts[prompt_key] = dict(
                        machine=machine, sid=sid, pid=pid, text="",
                        project=project_name(d.get("cwd", ""), path.parent.name),
                        start=d.get("timestamp", ""), cost=0.0, turns=0,
                        tools=0, out=0, ctx=0, agent=is_agent)
                # Only a real typed message has string content; tool results are lists.
                if keep_prompts and isinstance(content, str) and not p["text"]:
                    p["text"] = " ".join(content.split())[:160]
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    # The first tool_result in a subagent log answers the Agent
                    # call in the parent that spawned it, so its id is the link
                    # back to that call's subagent_type and description.
                    if is_agent:
                        agg.agent_link.setdefault(agent_key, b.get("tool_use_id"))
                    name = pending.pop(b.get("tool_use_id"), "(unknown)")
                    t = agg.tools[(machine, name)]
                    t["chars"] += blob_len(b.get("content"))
                    if b.get("is_error"):
                        t["errors"] += 1
            continue

        if rtype != "assistant":
            continue

        # One API response is written out as several assistant records, one per
        # content block, each carrying an identical `usage`. Bill it once — but
        # keep reading every record, because the later ones hold content blocks
        # (tool calls among them) that the first record does not.
        mid = msg.get("id")
        billable = True
        if mid:
            billable = mid not in agg.seen_msgs
            agg.seen_msgs.add(mid)

        ts = parse_ts(d.get("timestamp"))
        if ts is None:
            continue
        if since and ts < since:
            continue
        if until and ts > until:
            continue

        tool_calls = 0
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name") or "(unnamed)"
                if b.get("id") in pending:
                    continue          # the same block written out twice
                pending[b.get("id")] = name
                agg.tools[(machine, name)]["calls"] += 1
                tool_calls += 1
                if name == "Agent":
                    i = b.get("input") or {}
                    agg.agent_calls[(machine, b.get("id"))] = dict(
                        type=i.get("subagent_type") or "(default)",
                        desc=" ".join(str(i.get("description") or "").split())[:60],
                        session=sid)
                    # A subagent log replays the Agent call that spawned it as
                    # its own first tool_use, so the first one names this log.
                    if is_agent and agent_key not in agg.agent_link:
                        agg.agent_link[agent_key] = b.get("id")

        # Tool calls are counted from every record, so attribute them before
        # the billing records are filtered down to one per response.
        if tool_calls and prompt_key is not None:
            agg.prompts[prompt_key]["tools"] += tool_calls

        if not billable:
            continue

        u = msg.get("usage") or {}
        model = d.get("requestedModel") or msg.get("model") or DEFAULT_MODEL
        inp = u.get("input_tokens", 0) or 0
        out = u.get("output_tokens", 0) or 0
        think = (u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
        cr = u.get("cache_read_input_tokens", 0) or 0
        cc = u.get("cache_creation") or {}
        cw5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        cw1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if not (cw5 or cw1h):
            cw5 = u.get("cache_creation_input_tokens", 0) or 0
        c = cost_of(model, inp, out, cw5, cw1h, cr)

        proj = project_name(d.get("cwd", ""), path.parent.name)
        branch = d.get("gitBranch") or "(no branch)"
        day = ts.strftime("%Y-%m-%d")

        rec = agg.days[(machine, proj, day)]
        rec["cost"] += c
        rec["inp"] += inp
        rec["out"] += out
        rec["think"] += think
        rec["cr"] += cr
        rec["cw"] += cw5 + cw1h
        rec["turns"] += 1

        if prompt_key is not None:
            p = agg.prompts[prompt_key]
            p["cost"] += c
            p["turns"] += 1
            p["out"] += out
            p["ctx"] += inp + cr + cw5 + cw1h
        if is_agent:
            al = agg.agent_logs[agent_key]
            al["session"] = sid
            al["project"] = proj
            al["start"] = al.get("start") or ts.isoformat()
            al["cost"] += c
            al["turns"] += 1
            al["cr"] += cr
            al["cw"] += cw5 + cw1h
            al["out"] += out
        # Which cache TTL the turn asked for, and what that choice cost.
        tk = agg.ttl[(machine, d.get("promptCacheRequested") or "(none)")]
        tk["cost"] += c
        tk["turns"] += 1
        tk["cw"] += cw5 + cw1h
        tk["cr"] += cr
        pr = price(model)
        tk["write_cost"] += (cw5 * pr["cw5"] + cw1h * pr["cw1h"]) / 1_000_000

        agg.heat[(machine, ts.weekday(), ts.hour)] += c
        m = agg.models[(machine, model)]
        m["cost"] += c
        m["turns"] += 1
        m["out"] += out
        e = agg.efforts[(machine, d.get("effort") or "(none)")]
        e["cost"] += c
        e["turns"] += 1
        b = agg.branches[(machine, proj, branch)]
        b["cost"] += c
        b["turns"] += 1

        key = (machine, sid)
        s = agg.sessions.get(key)
        if s is None:
            s = agg.sessions[key] = dict(
                machine=machine, sid=sid, project=proj, branch=branch,
                start=ts.isoformat(), end=ts.isoformat(), cost=0.0, turns=0,
                inp=0, out=0, think=0, cr=0, cw=0, ctx_sum=0, version=d.get("version", ""),
                side=0,
            )
        s["project"] = proj
        s["end"] = ts.isoformat()
        s["cost"] += c
        s["turns"] += 1
        s["inp"] += inp
        s["out"] += out
        s["think"] += think
        s["cr"] += cr
        s["cw"] += cw5 + cw1h
        s["ctx_sum"] += inp + cr + cw5 + cw1h
        if d.get("isSidechain"):
            s["side"] += 1


def build(sources, since, until, keep_prompts=True):
    agg = Agg()
    files = 0
    for machine, root in sources:
        root = Path(os.path.expanduser(root))
        if not root.exists():
            print(f"[warn] source not found: {machine}={root}", file=sys.stderr)
            continue
        for f in sorted(root.rglob("*.jsonl")):
            scan_file(f, machine, agg, since, until, root, keep_prompts)
            files += 1
    return agg, files


def to_payload(agg: Agg, sources, files):
    days = [dict(machine=m, project=p, day=d, **v)
            for (m, p, d), v in sorted(agg.days.items())]
    sessions = []
    for key, s in agg.sessions.items():
        s = dict(s)
        s["declared"] = agg.declared.get(key)
        meta = agg.declared_meta.get(key) or {}
        s.update(apiMs=meta.get("apiMs", 0), toolMs=meta.get("toolMs", 0),
                 wallMs=meta.get("wallMs", 0), added=meta.get("added", 0),
                 removed=meta.get("removed", 0))
        s["ctx_avg"] = round(s["ctx_sum"] / s["turns"]) if s["turns"] else 0
        del s["ctx_sum"]
        t = agg.titles.get(key) or {}
        s["title"] = t.get("custom") or t.get("ai") or ""
        s["titled_by"] = "custom" if t.get("custom") else ("ai" if t.get("ai") else "")
        sessions.append(s)
    sessions.sort(key=lambda x: -x["cost"])

    tools = [dict(machine=m, tool=t, **v) for (m, t), v in agg.tools.items() if v["calls"]]
    tools.sort(key=lambda x: -x["chars"])
    heat = [dict(machine=m, dow=w, hour=h, cost=c) for (m, w, h), c in agg.heat.items()]
    models = [dict(machine=m, model=k, **v) for (m, k), v in agg.models.items()]
    efforts = [dict(machine=m, effort=k, **v) for (m, k), v in agg.efforts.items()]
    branches = [dict(machine=m, project=p, branch=b, **v)
                for (m, p, b), v in agg.branches.items()]
    branches.sort(key=lambda x: -x["cost"])

    # --- requests: one user prompt and everything it set in motion -----------
    prompts = [p for p in agg.prompts.values() if p["turns"]]
    prompts.sort(key=lambda p: -p["cost"])
    costs = sorted((p["cost"] for p in prompts), reverse=True)
    n = len(costs)
    pstat = {
        "count": n,
        "total": round(sum(costs), 4),
        "mean": round(sum(costs) / n, 5) if n else 0,
        "median": round(costs[n // 2], 5) if n else 0,
        "p90": round(costs[int(n * 0.1)], 5) if n else 0,
        "top10_share": round(sum(costs[:max(n // 10, 1)]) / sum(costs) * 100, 1) if n else 0,
    }

    # --- subagents: join each fork's log to the Agent call that spawned it ----
    agents = []
    for (machine, stem), v in agg.agent_logs.items():
        call = agg.agent_calls.get((machine, agg.agent_link.get((machine, stem))))
        agents.append(dict(
            machine=machine, log=stem.replace("agent-", "")[:12],
            type=(call or {}).get("type", ""),
            desc=(call or {}).get("desc", ""),
            session=v.get("session", ""), project=v.get("project", ""),
            start=v.get("start", ""),
            cost=v["cost"], turns=v["turns"], cr=v["cr"], cw=v["cw"], out=v["out"]))
    agents.sort(key=lambda a: -a["cost"])
    # Fold each fork's cost back into the session that spawned it, so a session
    # row shows how much of its bill was spent by its subagents.
    per_session = defaultdict(float)
    for a in agents:
        per_session[(a["machine"], a["session"])] += a["cost"]
    for s in sessions:
        s["agent_cost"] = round(per_session.get((s["machine"], s["sid"]), 0.0), 4)

    ttl = [dict(machine=m, ttl=k, **v) for (m, k), v in agg.ttl.items()]

    calc = sum(s["cost"] for s in sessions if s["declared"] is not None)
    decl = sum(s["declared"] for s in sessions if s["declared"] is not None)
    # Per-session ratio is the honest accuracy measure: cost-state is a snapshot
    # taken while the session ran, so a few long sessions under-report badly and
    # wreck the aggregate comparison. The median ratio shows whether the
    # per-turn arithmetic itself is right.
    ratios = sorted(s["cost"] / s["declared"] for s in sessions
                    if s["declared"] and s["declared"] > 0.01)
    median = ratios[len(ratios) // 2] if ratios else None
    return {
        "meta": {
            "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "sources": [{"machine": m, "path": str(p)} for m, p in sources],
            "files": files,
            "calibration": {
                "calculated": round(calc, 4),
                "declared": round(decl, 4),
                "covered_sessions": sum(1 for s in sessions if s["declared"] is not None),
                "total_sessions": len(sessions),
                "error_pct": round((calc - decl) / decl * 100, 2) if decl else None,
                "median_ratio": round(median, 4) if median else None,
                "compared": len(ratios),
            },
            "pricing": PRICING,
        },
        "days": days, "sessions": sessions, "tools": tools,
        "heat": heat, "models": models, "efforts": efforts, "branches": branches,
        "prompts": prompts[:250], "prompt_stats": pstat,
        "agents": agents, "ttl": ttl,
    }


def demo_payload():
    """Synthetic data for screenshots and for trying the dashboard without logs."""
    import random
    rnd = random.Random(7)
    machines = ["laptop", "workstation", "robot-201"]
    projects = ["api-gateway", "web-client", "etl-pipeline", "mobile-app",
                "infra-terraform", "docs-site", "ml-training", "scratch"]
    branches = ["main", "feat/checkout", "fix/flaky-tests", "release/2.4"]
    models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    tools = [("Bash", 260), ("Read", 820), ("Grep", 310), ("Edit", 90),
             ("Write", 60), ("WebSearch", 40), ("Agent", 25), ("Glob", 180)]
    today = dt.date.today()
    days, heat, sessions = [], defaultdict(float), []
    for i in range(45):
        day = today - dt.timedelta(days=44 - i)
        weight = 0.3 if day.weekday() >= 5 else 1.0
        for m in machines:
            mw = {"laptop": 1.0, "workstation": 0.7, "robot-201": 0.2}[m]
            for p in rnd.sample(projects, rnd.randint(1, 4)):
                turns = int(rnd.triangular(5, 120, 30) * weight * mw)
                if turns <= 0:
                    continue
                cr = turns * rnd.randint(30_000, 180_000)
                cw = int(cr * rnd.uniform(0.01, 0.06))
                out = turns * rnd.randint(300, 2_000)
                inp = turns * rnd.randint(1, 40)
                th = int(out * rnd.uniform(0.1, 0.4))
                c = cost_of("claude-opus-5-5", inp, out, 0, cw, cr)
                days.append(dict(machine=m, project=p, day=day.isoformat(),
                                 cost=c, inp=inp, out=out, think=th, cr=cr,
                                 cw=cw, turns=turns))
                for _ in range(max(turns // 12, 1)):
                    hour = int(rnd.triangular(8, 23, 15))
                    heat[(m, day.weekday(), hour)] += c / max(turns // 12, 1)
    topics = ["checkout flow refactor", "flaky test triage", "schema migration",
              "CI pipeline cleanup", "auth bug", "dashboard polish", "log ingestion",
              "release notes", "dependency bump", "perf profiling", "", ""]
    for i in range(70):
        m = rnd.choice(machines)
        p = rnd.choice(projects)
        turns = rnd.randint(8, 900)
        cr = turns * rnd.randint(30_000, 200_000)
        cw = int(cr * rnd.uniform(0.01, 0.06))
        out = turns * rnd.randint(300, 2_000)
        start = dt.datetime.combine(today - dt.timedelta(days=rnd.randint(0, 44)),
                                    dt.time(rnd.randint(9, 22), rnd.randint(0, 59)))
        cost = cost_of("claude-opus-5-5", turns * 8, out, 0, cw, cr)
        sessions.append(dict(
            machine=m, sid=f"{rnd.getrandbits(32):08x}-demo", project=p,
            branch=rnd.choice(branches), start=start.isoformat(),
            end=(start + dt.timedelta(minutes=turns)).isoformat(),
            cost=cost, turns=turns, inp=turns * 8, out=out,
            think=int(out * 0.3), cr=cr, cw=cw, version="2.1.322",
            side=rnd.choice([0, 0, turns // 3]), declared=cost * rnd.uniform(0.9, 1.1),
            apiMs=turns * rnd.randint(2_000, 9_000), toolMs=turns * rnd.randint(500, 4_000),
            wallMs=turns * rnd.randint(10_000, 40_000),
            added=rnd.randint(0, 4_000), removed=rnd.randint(0, 900),
            ctx_avg=(cr + cw) // max(turns, 1),
            title=rnd.choice(topics),
            titled_by=rnd.choice(["ai", "ai", "ai", "custom"])))
    sessions.sort(key=lambda s: -s["cost"])

    asks = ["이 테스트 왜 깨지는지 봐줘", "리팩터링해줘", "배포 스크립트 정리",
            "에러 로그 분석해줘", "문서 업데이트", "의존성 올려줘",
            "성능 프로파일링 돌려줘", "리뷰 코멘트 반영", "마이그레이션 작성"]
    prompts = []
    for s in sessions[:40]:
        for _ in range(rnd.randint(1, 6)):
            t = rnd.randint(1, 40)
            prompts.append(dict(
                machine=s["machine"], sid=s["sid"], pid=f"{rnd.getrandbits(32):08x}",
                project=s["project"], start=s["start"], turns=t,
                tools=rnd.randint(0, t * 3), out=t * rnd.randint(300, 2000),
                ctx=t * rnd.randint(30_000, 200_000),
                cost=cost_of("claude-opus-5-5", t * 8, t * 900, 0,
                             t * 4_000, t * 90_000),
                text=rnd.choice(asks), agent=False))
    prompts.sort(key=lambda p: -p["cost"])
    pcosts = [p["cost"] for p in prompts]
    agents = []
    for s in sessions[:25]:
        for _ in range(rnd.randint(0, 4)):
            t = rnd.randint(20, 400)
            agents.append(dict(
                machine=s["machine"], log=f"{rnd.getrandbits(24):06x}",
                type=rnd.choice(["fork", "general-purpose", "Explore", ""]),
                desc=rnd.choice(asks + [""]), session=s["sid"],
                project=s["project"], start=s["start"],
                cost=cost_of("claude-opus-5-5", t * 8, t * 900, 0, t * 4_000, t * 90_000),
                turns=t, cr=t * 90_000, cw=t * 4_000, out=t * 900))
    agents.sort(key=lambda a: -a["cost"])
    per_session = defaultdict(float)
    for a in agents:
        per_session[(a["machine"], a["session"])] += a["cost"]
    for s in sessions:
        s["agent_cost"] = round(per_session.get((s["machine"], s["sid"]), 0.0), 4)
    ttl = [dict(machine=m, ttl=k, turns=rnd.randint(200, 3000),
                cw=rnd.randint(2_000_000, 20_000_000),
                cr=rnd.randint(200_000_000, 900_000_000),
                cost=rnd.uniform(20, 300), write_cost=rnd.uniform(5, 90))
           for m in machines for k in ("1h", "5m", "(none)")]

    return {
        "meta": {"generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "sources": [{"machine": m, "path": "demo"} for m in machines],
                 "files": 0, "pricing": PRICING,
                 "calibration": {"calculated": 0, "declared": 0, "covered_sessions": 0,
                                 "total_sessions": len(sessions), "error_pct": 0,
                                 "median_ratio": 0.994, "compared": len(sessions)}},
        "days": days, "sessions": sessions,
        "tools": [dict(machine=rnd.choice(machines), tool=t,
                       calls=n * rnd.randint(1, 3),
                       chars=n * rnd.randint(400, 9_000), errors=rnd.randint(0, 20))
                  for t, n in tools for _ in range(2)],
        "heat": [dict(machine=m, dow=w, hour=h, cost=c) for (m, w, h), c in heat.items()],
        "models": [dict(machine=m, model=k, cost=rnd.uniform(5, 400),
                        turns=rnd.randint(50, 4_000), out=rnd.randint(100_000, 3_000_000))
                   for m in machines for k in models],
        "efforts": [dict(machine=m, effort=e, cost=rnd.uniform(5, 300),
                         turns=rnd.randint(50, 3_000))
                    for m in machines for e in ("low", "medium", "high", "xhigh")],
        "branches": sorted(
            [dict(machine=m, project=p, branch=b, cost=rnd.uniform(1, 300),
                  turns=rnd.randint(10, 2_000))
             for m in machines for p in projects for b in branches],
            key=lambda x: -x["cost"]),
        "prompts": prompts[:250], "agents": agents, "ttl": ttl,
        "prompt_stats": {
            "count": len(prompts), "total": round(sum(pcosts), 4),
            "mean": round(sum(pcosts) / len(pcosts), 5),
            "median": round(sorted(pcosts)[len(pcosts) // 2], 5),
            "p90": round(sorted(pcosts, reverse=True)[len(pcosts) // 10], 5),
            "top10_share": round(sum(sorted(pcosts, reverse=True)[:len(pcosts) // 10])
                                 / sum(pcosts) * 100, 1),
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Claude Code usage dashboard generator")
    ap.add_argument("--source", action="append", default=[],
                    metavar="NAME=PATH",
                    help="machine-tagged log root; repeatable. default: local=~/.claude/projects")
    ap.add_argument("-o", "--out", default="tokenwatch-report.html")
    ap.add_argument("--since", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--until", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--json", help="also dump the aggregated JSON here")
    ap.add_argument("--open", action="store_true", help="open the report in a browser")
    ap.add_argument("--demo", action="store_true",
                    help="render synthetic data instead of reading real logs")
    ap.add_argument("--no-prompts", action="store_true",
                    help="leave the text of your prompts out of the report")
    a = ap.parse_args()

    if a.demo:
        payload = demo_payload()
        tpl = Path(__file__).with_name("template.html").read_text(encoding="utf-8")
        out = Path(os.path.expanduser(a.out)).resolve()
        out.write_text(tpl.replace("/*__DATA__*/null",
                                   json.dumps(payload, ensure_ascii=False,
                                              separators=(",", ":"))),
                       encoding="utf-8")
        print(f"demo report: {out}")
        if a.open:
            import webbrowser
            webbrowser.open(out.as_uri())
        return

    sources = []
    for s in a.source or ["local=~/.claude/projects"]:
        name, _, path = s.partition("=")
        sources.append((name, path or name))

    tz = dt.datetime.now().astimezone().tzinfo
    since = dt.datetime.fromisoformat(a.since).replace(tzinfo=tz) if a.since else None
    until = (dt.datetime.fromisoformat(a.until).replace(hour=23, minute=59, second=59, tzinfo=tz)
             if a.until else None)

    agg, files = build(sources, since, until, keep_prompts=not a.no_prompts)
    payload = to_payload(agg, sources, files)

    tpl = Path(__file__).with_name("template.html").read_text(encoding="utf-8")
    html = tpl.replace("/*__DATA__*/null",
                       json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    out = Path(os.path.expanduser(a.out)).resolve()
    out.write_text(html, encoding="utf-8")

    if a.json:
        Path(os.path.expanduser(a.json)).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    cal = payload["meta"]["calibration"]
    print(f"files={files}  sessions={len(payload['sessions'])}  "
          f"calculated=${cal['calculated']:.2f}  declared=${cal['declared']:.2f}")
    if cal["median_ratio"]:
        print(f"accuracy: median per-session ratio {cal['median_ratio']:.3f} "
              f"over {cal['compared']} sessions "
              f"(aggregate gap {cal['error_pct']}% comes from sessions whose "
              f"cost-state snapshot stopped early)")
    print(f"report: {out}")
    if a.open:
        import webbrowser
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
