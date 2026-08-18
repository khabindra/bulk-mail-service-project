import json
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse, Http404
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.html import escape
from mailings.services.preview_service import render_preview_html, get_preview_validation_warnings, PreviewError
from mailings.services.context_builder import build_preview_context


@staff_member_required
def preview_mailing_html(request, object_id):
    """Preview endpoint - supports both GET (browser) and POST (AJAX with overrides)."""
    from mailings.models import Mailing
    
    try:
        mailing = Mailing.objects.select_related(
            'email_template__mail_type', 'sender_email'
        ).get(pk=object_id)
    except Mailing.DoesNotExist:
        raise Http404("Mailing not found")
    
    # FIXED: Use is_template_valid instead of broken None check
    if not mailing.email_template.is_template_valid:
        raise Http404("Template contains syntax errors. Fix the template before previewing.")
    
    if not mailing.email_template.template_content:
        raise Http404("Template has no content.")
    
    context = build_preview_context(
        sender_data={
            'sender_name': mailing.sender_email.name,
            'sender_email': mailing.sender_email.email
        }
    )
    
    dynamic_vars = mailing.context_variables or {}
    
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            override_ctx = json.loads(request.body)
            dynamic_vars = {**dynamic_vars, **override_ctx}
        except (json.JSONDecodeError, TypeError):
            pass
    
    warnings = get_preview_validation_warnings(
        email_template=mailing.email_template,
        context_data=context,
        dynamic_vars=dynamic_vars
    )
    
    try:
        html = render_preview_html(
            email_template=mailing.email_template,
            context_data=context,
            dynamic_vars=dynamic_vars,
            validate=False
        )
    except PreviewError as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return HttpResponseBadRequest(json.dumps({
                'error': str(e),
                'code': e.code,
                'warnings': warnings
            }), content_type='application/json')
        raise Http404(str(e))
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'html': html,
            'warnings': warnings
        })
    
    if warnings:
        escaped_warnings = '<br>'.join(f'• {escape(w)}' for w in warnings)
        warning_html = f'''
        <div style="background: #fff3cd; padding: 10px; margin: 10px 0; border-radius: 4px; color: #856404;">
            <strong>Warnings:</strong><br>
            {escaped_warnings}
        </div>
        '''
        html = warning_html + html
    
    return HttpResponse(html, content_type='text/html')