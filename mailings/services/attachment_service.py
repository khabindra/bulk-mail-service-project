import logging
from typing import Optional, TYPE_CHECKING
from django.core.files.uploadedfile import UploadedFile
from django.conf import settings

if TYPE_CHECKING:
    from mailings.models import MailingAttachment
    from test_mailing.models import TestMailingAttachment

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_SIZE = getattr(settings, 'MAILING_SETTINGS', {}).get(
    'MAX_ATTACHMENT_SIZE', 10 * 1024 * 1024
)
ALLOWED_CONTENT_TYPES = getattr(settings, 'MAILING_SETTINGS', {}).get(
    'ALLOWED_ATTACHMENT_CONTENT_TYPES', None
)
BLOCKED_EXTENSIONS = getattr(settings, 'MAILING_SETTINGS', {}).get(
    'BLOCKED_EXTENSIONS', ['.exe', '.bat', '.cmd', '.scr', '.pif', '.com']
)


class AttachmentService:
    """Service for validating and preparing attachments."""

    def __init__(
        self, 
        max_size: int = MAX_ATTACHMENT_SIZE, 
        allowed_types: Optional[list] = ALLOWED_CONTENT_TYPES,
        blocked_extensions: list = BLOCKED_EXTENSIONS
    ):
        self.max_size = max_size
        self.allowed_types = allowed_types
        self.blocked_extensions = [ext.lower() for ext in blocked_extensions]

    def validate_file(self, file: UploadedFile) -> tuple[bool, str]:
        """Validate a single uploaded file."""
        if hasattr(file, 'size') and file.size > self.max_size:
            max_mb = self.max_size // (1024 * 1024)
            return False, f"'{file.name}' exceeds {max_mb}MB limit."
        
        filename = (file.name or '').lower()
        for ext in self.blocked_extensions:
            if filename.endswith(ext):
                return False, f"'{file.name}' has blocked extension '{ext}'."
        
        if self.allowed_types:
            content_type = getattr(file, 'content_type', 'application/octet-stream')
            if content_type not in self.allowed_types:
                return False, f"'{file.name}' has unsupported content type '{content_type}'."
        
        return True, ""

    def validate_files(self, files: list) -> tuple[bool, str]:
        """Validate multiple files."""
        for f in files:
            is_valid, error = self.validate_file(f)
            if not is_valid:
                return False, error
        return True, ""

    def get_file_metadata(self, file: UploadedFile) -> dict:
        """Extract metadata from uploaded file."""
        return {
            'filename': file.name,
            'content_type': getattr(file, 'content_type', 'application/octet-stream'),
            'size': getattr(file, 'size', 0),
        }

    @staticmethod
    def get_cache_prefix_for_model(model_class) -> str:
        """
        Get cache prefix for a given attachment model.
        FIX #8: Uses explicit model identity instead of string check.
        """
        # Import here to avoid circular imports
        from mailings.models import MailingAttachment
        from test_mailing.models import TestMailingAttachment
        
        if model_class is MailingAttachment:
            return 'mailing_attachments'
        elif model_class is TestMailingAttachment:
            return 'test_mailing_attachments'
        else:
            # Fallback for unknown models
            return f"{model_class._meta.app_label}_{model_class._meta.model_name}"

    @staticmethod
    def prepare_attachments_for_executor(attachment_qs) -> list:
        """Convert attachment queryset to list of dicts for BulkMailExecutor."""
        from mailings.utils.attachment_utils import get_cached_attachments_for_model
        
        attachment_ids = list(attachment_qs.values_list('id', flat=True))
        if not attachment_ids:
            return []
        
        model_class = attachment_qs.model
        cache_prefix = AttachmentService.get_cache_prefix_for_model(model_class)
        
        return get_cached_attachments_for_model(
            model_class, 
            attachment_ids, 
            cache_prefix=cache_prefix
        )


# Singleton instance
attachment_service = AttachmentService()