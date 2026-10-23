from rest_framework.exceptions import PermissionDenied

from .capabilities import user_can


def require(user, *codes: str) -> None:
    if any(user_can(user, code) for code in codes):
        return
    raise PermissionDenied("You do not have access to this. Ask an admin to assign it.")
