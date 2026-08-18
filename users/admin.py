from django.contrib import admin
from .models import UserProfile

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'country', 'city', 'age', 'gender', 'subscription_type',
        'is_subscriber', 'subscription_start_date', 'last_login_date',
        'total_purchases', 'total_spent', 'accepts_marketing', 'preferred_language'
    )
    list_filter = (
        'country', 'gender', 'subscription_type', 'is_subscriber', 'accepts_marketing',
        'last_login_date'
    )
    search_fields = ('user__username', 'user__email', 'city')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('user', 'tags',)
        }),
        ('Demographics', {
            'fields': ('country', 'city', 'age', 'gender')
        }),
        ('Subscription Details', {
            'fields': ('subscription_type', 'is_subscriber', 'subscription_start_date')
        }),
        ('Behavioral', {
            'fields': ('last_login_date', 'total_purchases', 'total_spent')
        }),
        ('Preferences', {
            'fields': ('accepts_marketing', 'preferred_language')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

from django.contrib import admin
from .models import User

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'role', 'is_active')
    list_filter = ('role', 'is_active')
    search_fields = ('username', 'email')
