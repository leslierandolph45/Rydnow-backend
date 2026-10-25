from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils import timezone
from .models import (
    Rider, Booking, CustomerProfile, RiderProfile,
    RiderWallet, Transaction, WithdrawalRequest, RiderComplaint
)


# ══════════════════════════════════════════════════════════
# 👥 CUSTOMER PROFILE
# ══════════════════════════════════════════════════════════
@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'full_name', 'phone', 'created_at')
    search_fields = ('user__username', 'user__email', 'full_name', 'phone')
    readonly_fields = ('created_at', 'updated_at')


# ══════════════════════════════════════════════════════════
# 👥 USER ADMIN — SIMPLIFIED (no inline to avoid conflict)
# ══════════════════════════════════════════════════════════
admin.site.unregister(User)
admin.site.register(User, BaseUserAdmin)


# ══════════════════════════════════════════════════════════
# 👥 RIDER MANAGEMENT — SECTION 1
# ══════════════════════════════════════════════════════════
@admin.register(Rider)
class RiderAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'email', 'verification_status',
                   'is_online', 'is_restricted', 'created_at')
    list_filter = ('verification_status', 'is_online', 'is_restricted', 'created_at')
    search_fields = ('name', 'phone', 'email')
    list_per_page = 30
    
    fieldsets = (
        ('Personal Info', {'fields': ('name', 'email', 'phone', 'location', 'user')}),
        ('Status', {'fields': ('verification_status', 'is_online')}),
        ('Submitted Documents', {'fields': ('id_card', 'driver_license', 'vehicle_registration', 'insurance')}),
        ('Restriction Control', {'fields': ('is_restricted', 'restriction_reason', 'restriction_details', 'restricted_until')}),
        ('Admin Notes', {'fields': ('admin_notes',)}),
    )
    
    actions = ['approve_rider', 'reject_rider', 'mark_online', 'mark_offline',
               'restrict_riders', 'remove_restriction']
    
    def approve_rider(self, request, queryset):
        queryset.update(verification_status='approved')
    approve_rider.short_description = "✅ Mark selected as APPROVED"
    
    def reject_rider(self, request, queryset):
        queryset.update(verification_status='rejected')
    reject_rider.short_description = "❌ Mark selected as REJECTED"
    
    def mark_online(self, request, queryset):
        queryset.update(is_online=True)
    mark_online.short_description = "🟢 Mark as Online"
    
    def mark_offline(self, request, queryset):
        queryset.update(is_online=False)
    mark_offline.short_description = "⚪ Mark as Offline"
    
    def restrict_riders(self, request, queryset):
        queryset.update(is_restricted=True, restricted_at=timezone.now())
    restrict_riders.short_description = "🚫 Restrict selected riders"
    
    def remove_restriction(self, request, queryset):
        queryset.update(is_restricted=False, restriction_reason='',
                        restriction_details='', restricted_until=None)
    remove_restriction.short_description = "✅ Remove Restriction"


@admin.register(RiderProfile)
class RiderProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'phone', 'status', 'is_online', 'created_at')
    list_filter = ('status', 'is_online', 'created_at')
    search_fields = ('full_name', 'phone', 'email')
    readonly_fields = ('created_at',)


# ══════════════════════════════════════════════════════════
# 📋 BOOKING MANAGEMENT — SECTION 2
# ══════════════════════════════════════════════════════════
@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('service_type', 'customer_name', 'pickup', 'destination',
                   'status', 'rider', 'total_amount', 'payment_status', 'created_at')
    list_filter = ('service_type', 'status', 'payment_status', 'created_at')
    search_fields = ('customer_name', 'customer_phone', 'pickup', 'destination')
    readonly_fields = ('created_at', 'updated_at', 'total_amount',
                      'platform_commission', 'rider_amount', 'paid_at')
    
    fieldsets = (
        ('Trip Info', {'fields': ('service_type', 'pickup', 'destination',
                                 'pickup_lat', 'pickup_lng', 'destination_lat', 'destination_lng',
                                 'distance_km', 'price')}),
        ('Customer', {'fields': ('customer', 'customer_name', 'customer_phone', 'customer_email')}),
        ('Rider', {'fields': ('rider', 'status')}),
        ('Payment Details', {'fields': ('total_amount', 'platform_commission',
                                           'rider_amount', 'payment_status',
                                           'payment_method', 'transaction_reference', 'paid_at')}),
        ('Parcel/Waste Details', {'fields': ('parcel_details',)}),
    )
    
    actions = ['mark_completed', 'mark_cancelled']
    
    def mark_completed(self, request, queryset):
        queryset.update(status='completed')
    mark_completed.short_description = "✅ Mark as Completed"
    
    def mark_cancelled(self, request, queryset):
        queryset.update(status='cancelled')
    mark_cancelled.short_description = "❌ Mark as Cancelled"


# ══════════════════════════════════════════════════════════
# 💰 FINANCE & WALLET — SECTION 3
# ══════════════════════════════════════════════════════════
@admin.register(RiderWallet)
class RiderWalletAdmin(admin.ModelAdmin):
    list_display = ('rider_name', 'balance', 'total_earned', 'total_withdrawn', 'updated_at')
    search_fields = ('rider__name',)
    readonly_fields = ('balance', 'total_earned', 'total_withdrawn', 'updated_at')
    
    def rider_name(self, obj):
        return obj.rider.name
    rider_name.short_description = 'Rider'


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('rider_name', 'transaction_type', 'amount', 'commission',
                   'rider_amount', 'status', 'created_at')
    list_filter = ('transaction_type', 'status', 'created_at')
    search_fields = ('rider__name', 'description')
    readonly_fields = ('amount', 'commission', 'rider_amount', 'created_at')
    
    def rider_name(self, obj):
        return obj.rider.name
    rider_name.short_description = 'Rider'


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ('rider_name', 'amount', 'mobile_money_number',
                   'status', 'created_at', 'processed_at')
    list_filter = ('status', 'created_at')
    search_fields = ('rider__name', 'mobile_money_number', 'network')
    readonly_fields = ('amount', 'created_at', 'processed_at')
    
    actions = ['mark_completed', 'mark_failed']
    
    def rider_name(self, obj):
        return obj.rider.name
    rider_name.short_description = 'Rider'
    
    def mark_completed(self, request, queryset):
        queryset.update(status='completed', processed_at=timezone.now())
    mark_completed.short_description = "✅ Mark as Sent / Completed"
    
    def mark_failed(self, request, queryset):
        queryset.update(status='failed', processed_at=timezone.now())
    mark_failed.short_description = "❌ Mark as Failed"


# ══════════════════════════════════════════════════════════
# 🛡️ COMPLAINTS & REVIEWS — SECTION 4
# ══════════════════════════════════════════════════════════
@admin.register(RiderComplaint)
class RiderComplaintAdmin(admin.ModelAdmin):
    list_display = ('subject_rider', 'complaint_type', 'status',
                   'reporter_name', 'created_at')
    list_filter = ('complaint_type', 'status', 'created_at')
    search_fields = ('rider__name', 'reporter_name', 'description')
    readonly_fields = ('created_at', 'resolved_at')
    
    fieldsets = (
        ('Complaint Info', {'fields': ('rider', 'booking', 'complaint_type', 'description')}),
        ('Reporter', {'fields': ('reporter_name', 'reporter_phone')}),
        ('Status & Action', {'fields': ('status', 'admin_notes', 'action_taken', 'resolved_at')}),
    )
    
    actions = ['mark_resolved', 'issue_warning', 'restrict_rider_complaint']
    
    def subject_rider(self, obj):
        if obj.rider:
            return obj.rider.name
        return "Unknown Rider"
    subject_rider.short_description = 'Rider'
    
    def mark_resolved(self, request, queryset):
        queryset.update(status='resolved', resolved_at=timezone.now())
    mark_resolved.short_description = "✅ Mark as Resolved"
    
    def issue_warning(self, request, queryset):
        queryset.update(status='warning_issued')
    issue_warning.short_description = "⚠️ Issue Warning"
    
    def restrict_rider_complaint(self, request, queryset):
        queryset.update(status='restricted')
    restrict_rider_complaint.short_description = "🚫 Mark as Restricted"
