from django.urls import path, include
from django.http import HttpResponse
import os

def serve_frontend(request):
    frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates', 'index.html')
    with open(frontend_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return HttpResponse(content, content_type='text/html; charset=utf-8')

urlpatterns = [
    path('api/', include('route_planner.urls')),
    path('', serve_frontend, name='frontend'),
]
