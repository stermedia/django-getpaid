import logging
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect, \
    HttpResponseForbidden
from django.views.generic.base import View
from django.views.generic.detail import DetailView
from getpaid.backends.przelewy24 import PaymentProcessor
from getpaid.models import Payment

logger = logging.getLogger('getpaid.backends.przelewy24')


class OnlineView(View):
    """
    This View answers on Przelewy24 online request that is acknowledge of
    payment status change.
    """

    def post(self, request, *args, **kwargs):
        request_ip = request.META.get('HTTP_X_FORWARDED_FOR')
        if not request_ip:
            request_ip = request.META.get('REMOTE_ADDR')
        allowed_ips = PaymentProcessor.get_backend_setting(
            'allowed_ips', '') or PaymentProcessor.ALLOWED_IP_LIST
        if request_ip not in allowed_ips:
            return HttpResponseForbidden()
        try:
            logger.debug(u"POST data: %s" % request.POST)
            p24_session_id = request.POST['p24_session_id']
            p24_order_id = request.POST['p24_order_id']
            p24_amount = request.POST['p24_amount']
            p24_currency = request.POST['p24_currency']
            p24_sign = request.POST['p24_sign']
        except KeyError:
            logger.warning(
                'Got malformed POST request: %s' % str(request.POST))
            return HttpResponse('MALFORMED', status=500)

        if PaymentProcessor.on_payment_status_change(
                p24_session_id, p24_order_id, p24_amount, p24_currency,
                p24_sign):
            return HttpResponse('OK')
        else:
            return HttpResponse('CRC ERR')


class ReturnView(DetailView):
    """
    This view just redirects to standard backend success link after it schedule
    payment status checking.
    """
    model = Payment

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return HttpResponseRedirect(
            reverse('getpaid-inprogress-fallback', kwargs={'pk': self.object.pk}))

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)
