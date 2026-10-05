from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status


class RBACMiddleware:
    # Hierarchy: dev > superuser > user. Legacy admin/agent/viewer map onto it.
    _ALLOWED_ROLES: dict[str, list[str]] = {
        "dev": ["dev", "superuser", "user", "admin", "agent", "viewer"],
        "system": ["dev", "superuser", "user", "admin", "agent", "viewer"],
        "admin": ["admin", "superuser", "agent", "viewer", "user"],
        "superuser": ["superuser", "user", "agent", "viewer"],
        "agent": ["agent", "viewer", "user"],
        "user": ["user", "viewer"],
        "viewer": ["viewer"],
    }

    @classmethod
    def can_access_company(cls, user: object, company_id: str | None) -> bool:
        """Devs access all companies; superusers access any company (KPIs);
        regular users only their own company_id."""
        role = str(getattr(user, "role", "") or "").lower()
        # StrEnum value may be like "UserRole.DEV" in odd cases; normalize.
        role = role.split(".")[-1]
        if role in {"dev", "system", "admin"}:
            return True
        if role == "superuser":
            return True
        if company_id is None:
            return False
        return getattr(user, "company_id", None) == company_id

    @classmethod
    def require_company_access(cls, company_id_param: str = "company_id") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                request: Request | None = kwargs.get("request")
                if request is None:
                    for arg in args:
                        if isinstance(arg, Request):
                            request = arg
                            break
                if request is None:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
                user = getattr(request.state, "user", None)
                if user is None:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
                target = kwargs.get(company_id_param)
                if target is None and hasattr(request, "query_params"):
                    target = request.query_params.get(company_id_param)
                if not cls.can_access_company(user, str(target) if target else None):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
                return await func(*args, **kwargs)

            return wrapper

        return decorator

    @classmethod
    def can_access(cls, user_role: str, required_role: str) -> bool:
        allowed = cls._ALLOWED_ROLES.get(user_role, [])
        return required_role in allowed

    @classmethod
    def require_role(cls, required_role: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                request: Request | None = kwargs.get("request")
                if request is None:
                    for arg in args:
                        if isinstance(arg, Request):
                            request = arg
                            break
                if request is None:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

                user = getattr(request.state, "user", None)
                if user is None:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

                if not cls.can_access(user.role.value, required_role):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

                return await func(*args, **kwargs)

            return wrapper

        return decorator
