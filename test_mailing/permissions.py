from rest_framework.permissions import BasePermission, SAFE_METHODS

class IsAdminOrReadOnly(BasePermission):
    """Fix: Replaced string check with imported SAFE_METHODS tuple."""
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and getattr(request.user, 'role', None) == 'ADMIN')

class IsOwnerOrAdmin(BasePermission):
    def has_object_permission(self, request, view, obj):
        return bool(
            request.user and (
                obj.created_by == request.user or getattr(request.user, 'role', None) == 'ADMIN'
            )
        )