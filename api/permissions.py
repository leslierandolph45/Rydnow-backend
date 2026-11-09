
# ─────────────────────────── Permissions ───────────────────────────
class IsStaffAndAuthorOrSuperuser(BasePermission):
    """Staff can read/create notes; only the author or a superuser can edit/delete."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        return request.user.is_superuser or obj.author_id == request.user.id


class CoordinatesSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()

    def validate_latitude(self, value):
        if not -90 <= value <= 90:
            raise serializers.ValidationError("Latitude must be between -90 and 90")
        return value
        
    def validate_longitude(self, value):
        if not -180 <= value <= 90:
            raise serializers.ValidationError("Longitude must be between -180 and 180")
        return value




# —————————————————————————
# PERMISSIONS
# —————————————————————————
class RiderIncidentPermission(BasePermission):
    """Admins: everything. Riders: report new incidents and read their own."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_staff:
            return True
        return hasattr(user, "rider_profile") and view.action in ("list", "retrieve", "create")

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True
        return request.method in SAFE_METHODS and obj.rider_profile.user_id == request.user.id
