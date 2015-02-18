from django.conf.urls import patterns, url
from django.views.decorators.csrf import csrf_exempt
from getpaid.backends.przelewy24.views import OnlineView, ReturnView

urlpatterns = patterns('',
    url(r'^online/$', csrf_exempt(OnlineView.as_view()), name='getpaid-przelewy24-online'),
    url(r'^return/(?P<pk>\d+)/', csrf_exempt(ReturnView.as_view()), name='getpaid-przelewy24-return'),

)
