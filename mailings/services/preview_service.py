import logging
from typing import Optional
from django.template import Context

from mailings.services.inline_image_service import get_inline_image_urls
from mailings.services.context_builder import (
    build_preview_context, 
    validate_context_variables
)

logger = logging.getLogger(__name__)


class PreviewError(Exception):
    """Raised when preview rendering fails."""
    def __init__(self, message: str, code: str = "preview_error"):
        self.code = code
        super().__init__(message)


def render_preview_html(
    email_template, 
    context_data: dict,
    dynamic_vars: Optional[dict] = None,
    validate: bool = True
) -> str:
    """
    Render HTML for browser preview with public URLs.
    """
    # FIXED: Use is_template_valid instead of broken None check
    if not email_template.is_template_valid:
        raise PreviewError(
            "Template contains syntax errors and cannot be rendered.",
            code="template_invalid"
        )
    
    if not email_template.template_content:
        raise PreviewError(
            "Template has no content.",
            code="template_empty"
        )
    
    if dynamic_vars:
        context_data = {**context_data, **dynamic_vars}
    
    if validate:
        template_html = email_template.template_content
        
        mail_type = email_template.mail_type
        from templates.models import InlineImage
        image_cids = set(
            InlineImage.objects.filter(mail_type=mail_type, is_active=True)
            .values_list('content_id', flat=True)
        )
        
        is_valid, missing = validate_context_variables(
            template_html, 
            context_data, 
            ignore_vars=image_cids
        )
        if not is_valid:
            logger.warning(
                "Preview has missing variables: %s",
                missing,
                extra={"template_id": email_template.id}
            )
    
    image_urls = get_inline_image_urls(email_template.mail_type)
    for cid, url in image_urls.items():
        context_data[cid] = url
    
    try:
        return email_template.compiled_template.render(Context(context_data))
    except Exception as e:
        raise PreviewError(f"Render failed: {e}", code="render_failed")


def get_preview_validation_warnings(
    email_template, 
    context_data: dict,
    dynamic_vars: Optional[dict] = None
) -> list[str]:
    """Get list of validation warnings for preview."""
    warnings = []
    
    # FIXED: Use is_template_valid instead of broken None check
    if not email_template.is_template_valid:
        warnings.append("Template contains syntax errors and cannot be rendered.")
        return warnings
    
    if not email_template.template_content:
        warnings.append("Template has no HTML content.")
        return warnings
    
    check_context = {**context_data}
    if dynamic_vars:
        check_context.update(dynamic_vars)
    
    template_html = email_template.template_content
    
    mail_type = email_template.mail_type
    from templates.models import InlineImage
    image_cids = set(
        InlineImage.objects.filter(mail_type=mail_type, is_active=True)
        .values_list('content_id', flat=True)
    )
    
    is_valid, missing = validate_context_variables(
        template_html, 
        check_context, 
        ignore_vars=image_cids
    )
    
    if not is_valid:
        warnings.append(f"Missing variables: {', '.join(missing)}")
    
    image_urls = get_inline_image_urls(mail_type)
    for cid in image_cids:
        if cid not in check_context and cid not in image_urls:
            warnings.append(f"Inline image '{cid}' is not available.")
    
    return warnings