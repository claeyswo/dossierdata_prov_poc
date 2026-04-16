"""
POC authentication middleware.

Simulates auth by looking up X-POC-User header against config.
In production, replace with JWT/OAuth middleware.
"""

from __future__ import annotations

from dataclasses import dataclass
from fastapi import Request, HTTPException


@dataclass
class User:
    id: str
    type: str
    name: str
    roles: list[str]
    properties: dict[str, str]
    uri: str | None = None  # canonical external IRI for this agent


class POCAuthMiddleware:
    """Simulates auth by looking up X-POC-User header against config."""

    def __init__(self, users_config: list[dict]):
        self._users: dict[str, User] = {}
        for u in users_config:
            self._users[u["username"]] = User(
                id=str(u["id"]),
                type=u["type"],
                name=u["name"],
                roles=u.get("roles", []),
                properties=u.get("properties", {}),
                uri=u.get("uri"),
            )

    async def __call__(self, request: Request) -> User:
        username = request.headers.get("X-POC-User")
        if not username:
            raise HTTPException(401, detail="X-POC-User header required")
        user = self._users.get(username)
        if not user:
            raise HTTPException(401, detail=f"Unknown POC user: {username}")
        return user
