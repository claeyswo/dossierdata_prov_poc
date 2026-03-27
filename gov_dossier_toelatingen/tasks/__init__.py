"""
Task handlers for toelatingen.
In POC these are stubs. In production they'd send emails, call external systems, etc.
"""

from __future__ import annotations


async def duid_verantwoordelijke_org_tasks(**kwargs):
    """Tasks after determining responsible organization."""
    print(f"[TASK] duid_verantwoordelijke_org_tasks: {kwargs}")


async def neem_beslissing_tasks(**kwargs):
    """Tasks after taking the decision."""
    print(f"[TASK] neem_beslissing_tasks: {kwargs}")


TASK_HANDLERS = {
    "duid_verantwoordelijke_org_tasks": duid_verantwoordelijke_org_tasks,
    "neem_beslissing_tasks": neem_beslissing_tasks,
}
