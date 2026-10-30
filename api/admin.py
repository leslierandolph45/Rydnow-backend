from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils import timezone
from .models import (
    Rider, Booking, CustomerProfile, RiderProfile,
    RiderWallet, WithdrawalRequest, RiderComplaint
)




# ══════════════════════════════════════════════════════════
# 👥 CUSTOMER PROFILE
# ══════════════════════════════════════════════════════════
@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'phone', 'created_at')
    search_fields = ('user__username', 'user__email', 'full_name', 'phone')
    readonly_fields = ('created_at',)




# ══════════════════════════════════════════════════════════
# 👥 USER ADMIN
# ══════════════════════════════════════════════════════════
admin.site.unregister(User)
admin.site.register(User, BaseUserAdmin)




# ══════════════════════════════════════════════════════════
# 👥 RIDER MANAGEMENT
# ══════════════════════════════════════════════════════════
@admin.register(Rider)
class RiderAdmin(admin.ModelAdmin):
    list_display = ('id', 'phone', 'verification_status', 'created_at')
    list_filter = ('verification_status','created_at',)
    search_fields = ('user__username', 'phone', 'user__email')
    list_per_page = 30


    fieldsets = (
        ('Personal Info', {'fields': ('user', 'phone', 'location')}),
        ('Status', {'fields': ('verification_status', 'is_online')}),
    )


    actions = ['approve_rider', 'reject_rider', 'mark_online', 'mark_offline']


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




@admin.register(RiderProfile)
class RiderProfileAdmin(admin.ModelAdmin):
    list_display= ('phone', 'created_at')
    search_fields= ('user__username', 'phone', 'user__email')
    readonly_fields= ('created_at',)




# ══════════════════════════════════════════════════════════
# 📋 BOOKING MANAGEMENT
# ══════════════════════════════════════════════════════════
@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display= ('service_type', 'pickup', 'destination',
                   'status', 'created_at')
    list_filter= ('service_type', 'status', 'created_at')
    search_fields = ('pickup', 'destination')
    readonly_fields = ('created_at', )


    actions = ['mark_completed', 'mark_cancelled']


    def mark_completed(self, request, queryset):
        queryset.update(status='completed')
    mark_completed.short_description = "✅ Mark as Completed"


    def mark_cancelled(self, request, queryset):
        queryset.update(status='cancelled')
    mark_cancelled.short_description = "❌ Mark as Cancelled"




# ══════════════════════════════════════════════════════════
# 💰 FINANCE & WALLET
# ══════════════════════════════════════════════════════════
@admin.register(RiderWallet)
class RiderWalletAdmin(admin.ModelAdmin):
    list_display= ('rider', 'balance',)
    search_fields = ('rider__user__username', 'rider__user__email')
    readonly_fields = ('balance',)


from .models import WalletTransaction
@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display= ('id', 'transaction_type', 'amount', 'status', 'created_at')
    list_filter = ('transaction_type', 'status', 'created_at')
    search_fields = ('description',)
    readonly_fields = ('amount', 'created_at')




@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display= ('id', 'amount', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    readonly_fields = ('amount', 'created_at')


    actions = ['mark_completed', 'mark_failed']


    def mark_completed(self, request, queryset):
        queryset.update(status='completed')
    mark_completed.short_description = "✅ Mark as Completed"


    def mark_failed(self, request, queryset):
        queryset.update(status='failed')
    mark_failed.short_description = "❌ Mark as Failed"




# ══════════════════════════════════════════════════════════
# 🛡️ COMPLAINTS & REVIEWS
# ══════════════════════════════════════════════════════════
@admin.register(RiderComplaint)
class RiderComplaintAdmin(admin.ModelAdmin):
    list_display= ('subject', 'complaint_type', 'status', 'created_at')
    list_filter = ('complaint_type', 'status', 'created_at')
    search_fields = ('description',)
    readonly_fields = ('created_at',)


    def subject(self, obj):
        return str(obj.id)
    subject.short_description = 'Complaint'
