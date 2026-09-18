from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status


class RBACMiddleware:
    _ALLOWED_ROLES: dict[str, list[str]] = {
        "admin": ["admin", "agent", "viewer"],
        "agent": ["agent", "viewer"],
        "viewer": ["viewer"],
    }

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
