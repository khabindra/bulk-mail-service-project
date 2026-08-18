from rest_framework.pagination import CursorPagination

class StandardCursorPagination(CursorPagination):
    page_size = 20
    ordering = '-scheduled_time'
    page_size_query_param = 'page_size'
    max_page_size = 100