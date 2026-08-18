from django.contrib import admin
from .models import Client

@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        'company_name',
        'contact_email',
        'user',          # shows linked user
        'is_active'
    )

    search_fields = (
        'company_name',
        'contact_email',
        'user__username',
        'user__email'
    )

    list_filter = ('is_active',)



