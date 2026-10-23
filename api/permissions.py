from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    message = "Only an admin can do that."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        profile = getattr(user, "profile", None)
        return bool(profile and profile.role.slug == "admin")
