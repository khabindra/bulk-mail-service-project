import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)

def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        customized_response = {
            "success": False, 
            "message": "A validation error occurred." if response.status_code == 400 else "An error occurred.", 
            "errors": response.data
        }
        response.data = customized_response
    else:
        request = context.get('request')
        user_id = 'Unknown'
        if request:
            try: 
                user_id = request.user.id if request.user.is_authenticated else 'Anonymous'
            except AttributeError: 
                user_id = 'Unknown'
        logger.error(
            "Unhandled API Exception: %s | Path: %s | User: %s", 
            str(exc), 
            request.path if request else 'Unknown', 
            user_id, 
            exc_info=True
        )
        response = Response(
            {"success": False, "message": "An unexpected internal server error occurred.", "errors": []}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    return response