from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings


class User(AbstractUser):
    class RoleChoices(models.TextChoices):
        ADMIN = 'ADMIN', 'Admin'
        CLIENT = 'CLIENT', 'Client'

    role = models.CharField(
        max_length=10,
        choices=RoleChoices.choices,
        default=RoleChoices.CLIENT
    )


class UserProfile(models.Model):
    """
    Extended user profile for audience targeting.
    
    FIX #9: This model is designed for future recipient filtering.
    When scaling, implement a recipient selection service that:
    1. Builds dynamic querysets based on filter criteria
    2. Supports combinations (e.g., country=US AND subscription_type=PREMIUM)
    3. Caches filtered ID lists for large mailings
    4. Uses Mailing.recipients.set() with pre-filtered IDs
    
    Example usage pattern:
        recipient_ids = RecipientFilterService.filter(
            country='US',
            subscription_type='PREMIUM',
            tags__contains=['vip']
        )
        mailing.recipients.set(recipient_ids)
    """

    class CountryChoices(models.TextChoices):
        US = 'US', 'United States'
        UK = 'UK', 'United Kingdom'
        CA = 'CA', 'Canada'
        AU = 'AU', 'Australia'
        IN = 'IN', 'India'
        DE = 'DE', 'Germany'
        FR = 'FR', 'France'
        NP = 'NP', 'Nepal'

    class SubscriptionChoices(models.TextChoices):
        FREE = 'FREE', 'Free'
        BASIC = 'BASIC', 'Basic'
        PREMIUM = 'PREMIUM', 'Premium'
        ENTERPRISE = 'ENTERPRISE', 'Enterprise'

    class GenderChoices(models.TextChoices):
        MALE = 'M', 'Male'
        FEMALE = 'F', 'Female'
        OTHER = 'O', 'Other'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile'
    )

    # Demographic filters
    country = models.CharField(
        max_length=2,
        choices=CountryChoices.choices,
        default=CountryChoices.US,
        db_index=True  # Add index for filtering
    )
    city = models.CharField(max_length=100, blank=True)
    age = models.IntegerField(null=True, blank=True)
    gender = models.CharField(
        max_length=10,
        choices=GenderChoices.choices,
        blank=True
    )

    # Subscription filters
    subscription_type = models.CharField(
        max_length=20,
        choices=SubscriptionChoices.choices,
        default=SubscriptionChoices.FREE,
        db_index=True  # Add index for filtering
    )
    is_subscriber = models.BooleanField(default=True)
    subscription_start_date = models.DateField(null=True, blank=True)

    # Behavioral filters
    last_login_date = models.DateField(null=True, blank=True)
    total_purchases = models.IntegerField(default=0)
    total_spent = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Preferences
    accepts_marketing = models.BooleanField(default=True, db_index=True)
    preferred_language = models.CharField(max_length=10, default='en')

    # Tags for flexible filtering
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text='Custom tags like ["vip", "early_adopter", "beta_tester"]'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['country', 'subscription_type']),
            models.Index(fields=['is_subscriber', 'accepts_marketing']),
        ]

    def __str__(self):
        return f"{self.user.username}'s profile"