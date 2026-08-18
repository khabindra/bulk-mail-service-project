from rest_framework.permissions import BasePermission

class IsStaffOrReadOnly(BasePermission):
    """
    Allows read-only access to any authenticated user,
    but restricts write operations (create, update, delete) to staff/admins.
    """
    def has_permission(self, request, view):
        if request.method in ['GET', 'HEAD', 'OPTIONS']:
            return request.user and request.user.is_authenticated
        
        return bool(request.user and request.user.is_staff)