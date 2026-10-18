from ninja.security import SessionAuth


class StaffSessionAuth(SessionAuth):
    """Session auth (with CSRF) restricted to active staff, like contrib.admin."""

    def authenticate(self, request, key):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_active and user.is_staff:
            return user
        return None


staff_auth = StaffSessionAuth()
