# Author: Krzysztof Dorosz <cypreess@gmail.com>
#
# Disclaimer:
# Writing and open sourcing this backend was kindly funded by Issue Stand
# http://issuestand.com/
#

import datetime
from decimal import Decimal
import hashlib
import logging
import time
import urllib
import urllib2
import urlparse

from django.contrib.sites.models import Site
from django.core.exceptions import ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _
from pytz import utc

from getpaid import signals
from getpaid.backends import PaymentProcessorBase
from getpaid.backends.przelewy24.tasks import get_payment_status_task

logger = logging.getLogger('getpaid.backends.przelewy24')


class PaymentProcessor(PaymentProcessorBase):
    BACKEND = 'getpaid.backends.przelewy24'
    BACKEND_NAME = _('Przelewy24')
    BACKEND_ACCEPTED_CURRENCY = ('PLN', 'EUR', 'GBP', 'CZK')
    BACKEND_LOGO_URL = 'getpaid/backends/przelewy24/przelewy24_logo.png'
    ALLOWED_IP_LIST = ['91.216.191.181', '91.216.191.182', '91.216.191.183',
                       '91.216.191.184', '91.216.191.185']

    _API_VERSION = '3.2'

    _GATEWAY_URL_TRANSACTION_REGISTER = \
        'https://secure.przelewy24.pl/trnRegister'
    _SANDBOX_GATEWAY_URL_TRANSACTION_REGISTER = \
        'https://sandbox.przelewy24.pl/trnRegister'
    _GATEWAY_URL = 'https://secure.przelewy24.pl/trnRequest/'
    _SANDBOX_GATEWAY_URL = 'https://sandbox.przelewy24.pl/trnRequest/'

    _GATEWAY_CONFIRM_URL = 'https://secure.przelewy24.pl/trnVerify'
    _SANDBOX_GATEWAY_CONFIRM_URL = 'https://sandbox.przelewy24.pl/trnVerify'

    _ACCEPTED_LANGS = ('pl', 'en', 'es', 'de', 'it')
    _REQUEST_SIG_FIELDS = (
        'p24_session_id', 'p24_merchant_id', 'p24_amount', 'p24_currency',
        'crc')
    _STATUS_SIG_FIELDS = (
        'p24_session_id', 'p24_order_id', 'p24_amount', 'p24_currency', 'crc')

    @staticmethod
    def compute_sig(params, fields, crc):
        params = params.copy()
        params.update({'crc': crc})
        text = "|".join(
            map(lambda field: unicode(params.get(field, '')).encode('utf-8'),
                fields))
        return hashlib.md5(text).hexdigest()

    @staticmethod
    def on_payment_status_change(p24_session_id, p24_order_id, p24_amount,
                                 p24_currency, p24_sign):
        params = {
            'p24_session_id': p24_session_id,
            'p24_order_id': p24_order_id,
            'p24_amount': p24_amount,
            'p24_currency': p24_currency,
            'p24_sign': p24_sign,
        }
        crc = PaymentProcessor.get_backend_setting('crc')
        if p24_sign != PaymentProcessor.compute_sig(
            params, PaymentProcessor._STATUS_SIG_FIELDS, crc):
            logger.warning('Status view call has wrong crc %s' % str(params))
            return False

        payment_id = p24_session_id.split(':')[0]
        get_payment_status_task.delay(
            payment_id, p24_session_id, p24_amount, p24_currency, p24_order_id)
        return True

    def get_payment_status(self, p24_session_id,
                           p24_amount, p24_currency, p24_order_id):
        merchant_id = PaymentProcessor.get_backend_setting('id')
        params = {
            'p24_merchant_id': merchant_id,
            'p24_pos_id': PaymentProcessor.get_backend_setting(
                'pos_id', default=merchant_id),
            'p24_session_id': p24_session_id,
            'p24_amount': p24_amount,
            'p24_currency': p24_currency,
            'p24_order_id': p24_order_id,
        }
        crc = PaymentProcessor.get_backend_setting('crc')
        params['p24_sign'] = PaymentProcessor.compute_sig(
            params, self._STATUS_SIG_FIELDS, crc)

        for key in params.keys():
            params[key] = unicode(params[key]).encode('utf-8')

        data = urllib.urlencode(params)
        url = self._SANDBOX_GATEWAY_CONFIRM_URL if \
            PaymentProcessor.get_backend_setting('sandbox', False) else \
            self._GATEWAY_CONFIRM_URL

        self.payment.external_id = p24_order_id

        request = urllib2.Request(url, data)
        try:
            response = urllib2.urlopen(request).read()
        except Exception:
            logger.exception(
                'Error while getting payment status change %s data=%s' % (
                    url, str(params)))
            return

        response_params = urlparse.parse_qs(response)

        if response_params.get('error') == ['0']:
            logger.info('Payment accepted %s' % str(params))
            self.payment.amount_paid = Decimal(p24_amount) / Decimal('100')
            self.payment.paid_on = datetime.datetime.utcnow().replace(
                tzinfo=utc)
            if self.payment.amount_paid >= self.payment.amount:
                self.payment.change_status(
                    self.payment.PAYMENT_STATUS_PAID)
            else:
                self.payment.change_status(
                    self.payment.PAYMENT_STATUS_PARTIALLY_PAID)
        else:
            logger.warning(
                'Payment rejected for data=%s: "%s"' % (str(params), response))
            self.payment.change_status(self.payment.PAYMENT_STATUS_FAILED)

    def get_gateway_url(self, request):
        """
        Routes a payment to Gateway, should return URL for redirection.

        """
        merchant_id = PaymentProcessor.get_backend_setting('id')
        params = {
            'p24_merchant_id': PaymentProcessor.get_backend_setting('id'),
            'p24_pos_id': PaymentProcessor.get_backend_setting(
                'pos_id', default=merchant_id),
            'p24_description': self.get_order_description(
                self.payment, self.payment.order),
            'p24_session_id': "%s:%s:%s" % (
                self.payment.pk, self.BACKEND, time.time()),
            'p24_amount': int(self.payment.amount * 100),
            'p24_currency': self.payment.currency.upper(),
            'p24_email': None,

        }

        user_data = {
            'email': None,
            'lang': None,
            'p24_client': None,
            'p24_address': None,
            'p24_zip': None,
            'p24_city': None,
            'p24_country': None,
        }
        signals.user_data_query.send(
            sender=None, order=self.payment.order, user_data=user_data)

        for key in ('p24_client', 'p24_address', 'p24_zip', 'p24_city',
                    'p24_country'):
            if user_data[key] is not None:
                params[key] = user_data[key]

        if user_data['email']:
            params['p24_email'] = user_data['email']

        if user_data['lang'] and user_data['lang'].lower() in \
                PaymentProcessor._ACCEPTED_LANGS:
            params['p24_language'] = user_data['lang'].lower()
        elif PaymentProcessor.get_backend_setting('lang', False) and \
                PaymentProcessor.get_backend_setting(
                    'lang').lower() in PaymentProcessor._ACCEPTED_LANGS:
            params['p24_language'] = \
                PaymentProcessor.get_backend_setting('lang').lower()

        params['p24_sign'] = self.compute_sig(
            params, self._REQUEST_SIG_FIELDS,
            PaymentProcessor.get_backend_setting('crc'))
        params['p24_api_version'] = PaymentProcessor.get_backend_setting(
            'api_version', default=self._API_VERSION)

        current_site = Site.objects.get_current()
        use_ssl = PaymentProcessor.get_backend_setting('ssl_return', False)

        params['p24_url_return'] = ('https://' if use_ssl else 'http://') + \
            current_site.domain + reverse('getpaid-przelewy24-return',
                                          kwargs={'pk': self.payment.pk})
        params['p24_url_status'] = ('https://' if use_ssl else 'http://') + \
            current_site.domain + reverse('getpaid-przelewy24-online')
        if params['p24_email'] is None:
            raise ImproperlyConfigured(
                '%s requires filling `email` field for payment (you need to handle `user_data_query` signal)' % self.BACKEND)

        for key in params.keys():
            params[key] = unicode(params[key]).encode('utf-8')

        data = urllib.urlencode(params)
        url = self._SANDBOX_GATEWAY_URL_TRANSACTION_REGISTER if \
            PaymentProcessor.get_backend_setting('sandbox', False) else \
            self._GATEWAY_URL_TRANSACTION_REGISTER
        request = urllib2.Request(url, data)
        try:
            response = urllib2.urlopen(request).read()
        except Exception:
            logger.exception('Error while payment register %s data=%s' % (
                url, str(params)))
            return
        response_params = urlparse.parse_qs(response)
        if response_params.get('error') == ['1']:
            logger.warning(
                'Payment rejected for data=%s: "%s"' % (str(params), response))
            self.payment.change_status(self.payment.PAYMENT_STATUS_FAILED)
            return ('https://' if use_ssl else 'http://') + \
                current_site.domain + reverse('getpaid-failure-fallback',
                                              kwargs={'pk': self.payment.pk}), \
                'GET'
        return '%s%s' % (self._SANDBOX_GATEWAY_URL if \
            PaymentProcessor.get_backend_setting('sandbox', False) else
            self._GATEWAY_URL, response_params['token'][0]), 'POST', params
