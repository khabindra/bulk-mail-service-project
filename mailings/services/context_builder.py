import logging
import re
from typing import Optional
from django.utils import timezone as django_timezone

logger = logging.getLogger(__name__)

# Pattern to find template variables: {{ variable_name }}
# NOTE: This regex catches simple variable access like {{ foo }} and {{ foo.bar }}
# LIMITATIONS (Fix #10): This does NOT catch:
#   - Filter expressions: {{ foo|default:"value" }}
#   - Block tags: {% if foo %}, {% for item in items %}
#   - Complex expressions: {{ foo|date:"Y-m-d" }}
# For production with complex templates, use Django's Template.nodelist parser instead.
TEMPLATE_VAR_PATTERN = re.compile(r'\{\{\s*([\w.]+)(?:\|[^}]*)?\s*\}\}')


def extract_template_variables(html_content: str) -> set[str]:
    """
    Extract all variable names from template HTML.
    
    Returns only the base variable name (before any filters).
    For example, {{ company_name|default:"N/A" }} returns "company_name".
    
    WARNING: Does not extract variables from {% block %} tags.
    See TEMPLATE_VAR_PATTERN docstring for limitations.
    """
    if not html_content:
        return set()
    return set(TEMPLATE_VAR_PATTERN.findall(html_content))


def validate_context_variables(
    template_html: str, 
    context: dict, 
    ignore_vars: Optional[set[str]] = None
) -> tuple[bool, list[str]]:
    """
    Validate that all template variables are provided in context.
    
    Args:
        template_html: The template HTML content
        context: The context dictionary
        ignore_vars: Set of variable names to ignore (e.g., built-in vars)
    
    Returns:
        (is_valid, missing_variables)
    """
    if not template_html:
        return True, []
    
    required_vars = extract_template_variables(template_html)
    
    built_in_vars = {
        'current_year', 'message', 'client_name', 'company_name',
        'contact_email', 'sender_name', 'sender_email'
    }
    
    ignore = built_in_vars | (ignore_vars or set())
    vars_to_check = required_vars - ignore
    
    missing = []
    for var in vars_to_check:
        if not _var_exists_in_context(var, context):
            missing.append(var)
    
    return len(missing) == 0, missing


def _var_exists_in_context(var_name: str, context: dict) -> bool:
    """Check if a variable (possibly dot-notation) exists in context."""
    parts = var_name.split('.')
    current = context
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False
            current = current[part]
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return False
    return True


def build_email_context(
    client, 
    sender, 
    message: str = '', 
    request_data: dict | None = None
) -> dict:
    """Build context dictionary for email rendering."""
    request_data = request_data or {}
    
    context = {
        "current_year": django_timezone.now().year,
        "message": message,
        "client_name": _get_client_name(client),
        "company_name": getattr(client, 'company_name', '') or "",
        "contact_email": getattr(client, 'contact_email', '') or "",
        "sender_name": getattr(sender, 'name', 'Team') if sender else 'Team',
        "sender_email": getattr(sender, 'email', '') if sender else '',
    }
    
    context.update(request_data)
    return context


def _get_client_name(client) -> str:
    """Extract display name from client."""
    if hasattr(client, 'user') and client.user:
        return getattr(client.user, 'username', '') or getattr(client, 'company_name', '') or ""
    return getattr(client, 'company_name', '') or ""


def build_preview_context(
    client_data: dict | None = None, 
    sender_data: dict | None = None, 
    template_defaults: dict | None = None,
    dynamic_vars: dict | None = None
) -> dict:
    """Build context for preview rendering with sample data."""
    context = {
        "current_year": django_timezone.now().year,
        "message": "",
        "client_name": "John Doe",
        "company_name": "Acme Inc.",
        "contact_email": "john@acme.com",
        "sender_name": "Sender Name",
        "sender_email": "sender@example.com"
    }
    
    if sender_data:
        context.update({k: sender_data.get(k, context[k]) for k in ('sender_name', 'sender_email')})
    if client_data:
        context.update({k: client_data.get(k, context[k]) for k in ('client_name', 'company_name', 'contact_email')})
    if template_defaults:
        context.update(template_defaults)
    if dynamic_vars:
        context.update(dynamic_vars)
    
    return context