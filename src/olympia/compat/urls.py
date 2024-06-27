from django.conf.urls import url

from . import views


urlpatterns = (
    url(r'^incoming/?$', views.incoming, name='compat.incoming'),
    url(r'^reporter/?$', views.reporter, name='compat.reporter'),
    url(r'^reporter/(.+)$', views.reporter_detail,
        name='compat.reporter_detail'),
)
