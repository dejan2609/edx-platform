"""
URLs for the certificates app.
"""

from django.conf import settings
from django.conf.urls import patterns, url

from certificates import views

urlpatterns = patterns(
    '',

    # Certificates HTML view end point to render web certs by user and course
    url(
        r'^user/(?P<user_id>[^/]*)/course/{course_id}'.format(course_id=settings.COURSE_ID_PATTERN),
        views.render_html_view,
        name='html_view'
    ),

    # Certificates HTML view end point to render web certs by certificate_uuid
    url(
        r'^(?P<certificate_uuid>[0-9a-f]{32})$',
        views.render_cert_by_uuid,
        name='render_cert_by_uuid'
    ),

    # End-points used by student support
    # The views in the lms/djangoapps/support use these end-points
    # to retrieve certificate information and regenerate certificates.
    url(r'search', views.search_certificates, name="search"),
    url(r'regenerate', views.regenerate_certificate_for_user, name="regenerate_certificate_for_user"),
    url(r'generate', views.generate_certificate_for_user, name="generate_certificate_for_user"),
)
