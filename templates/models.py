from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.text import slugify
from cloudinary.models import CloudinaryField
from django.template import Template, Context


class MailType(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Mail Type"
        verbose_name_plural = "Mail Types"


class EmailTemplate(models.Model):
    """
    Email template with versioning support.
    Multiple active templates per MailType are allowed for A/B testing.
    """
    mail_type = models.ForeignKey(
        MailType, on_delete=models.CASCADE, related_name="templates"
    )

    subject = models.CharField(max_length=255, blank=True)
    template_name = models.CharField(
        max_length=100, unique=True,
        help_text="Unique identifier (e.g., welcome-email)."
    )
    description = models.TextField(blank=True)
    template_content = models.TextField(help_text="HTML Content for the email.")
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    default_context = models.JSONField(
        default=dict, blank=True,
        help_text="Default values for variables (JSON format)."
    )
    available_variables = models.CharField(max_length=255, default="", blank=True)

    class Meta:
        ordering = ['mail_type__name', '-version']
        indexes = [
            models.Index(fields=['mail_type', 'is_active']),
        ]

    def __str__(self):
        return f"{self.template_name} (v{self.version})"

    def save(self, *args, **kwargs):
        if self.template_name:
            self.template_name = slugify(self.template_name)
        # REMOVED: Dead code - @property has no __dict__ entry to clear
        super().save(*args, **kwargs)

    def clean(self):
        if not self.template_content:
            raise ValidationError({"template_content": "Template Content is required."})

    @property
    def is_template_valid(self) -> bool:
        """
        Check if template_content is valid Django template syntax.
        
        This is the correct guard for "is this template ready to use?"
        Unlike accessing compiled_template directly (which raises on bad syntax),
        this returns False instead of propagating the exception.
        
        IMPORTANT: With @property for compiled_template, the old pattern of
        `getattr(template, 'compiled_template', None) is None` no longer works
        because @property always returns a Template object or raises an exception
        (which getattr catches, but doesn't convert to None).
        
        Use this property instead for all validation guards.
        """
        if not self.template_content:
            return False
        try:
            Template(self.template_content)
            return True
        except Exception:
            return False

    @property
    def compiled_template(self):
        """
        Compile template on demand. Safe across Celery workers.
        
        NOTE: This will raise TemplateSyntaxError if content is invalid.
        Call is_template_valid first if you need a graceful check.
        """
        return Template(self.template_content)

    def render_template(self, context_data):
        """Renders the template with context."""
        return self.compiled_template.render(Context(context_data))

    @property
    def variables_list(self):
        if not self.available_variables:
            return []
        return [v.strip() for v in self.available_variables.split(',') if v.strip()]


class InlineImage(models.Model):
    mail_type = models.ForeignKey(
        MailType, on_delete=models.CASCADE, related_name='inline_images'
    )
    content_id = models.CharField(
        max_length=100, help_text="CID referenced in the HTML template"
    )
    image = CloudinaryField(
        'image', folder='email_inline_images', resource_type='image',
        blank=True, null=True
    )
    public_id = models.CharField(
        max_length=255, unique=True, editable=False, null=True, blank=True
    )
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=255, blank=True)
    alt_text = models.CharField(max_length=255, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['mail_type', 'display_order', 'content_id']
        indexes = [
            models.Index(fields=['mail_type', 'content_id', 'is_active']),
        ]

    def __str__(self):
        return f"{self.content_id} v{self.version}"