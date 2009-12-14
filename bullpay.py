"""
To use bullpay (production Paypal URLs given; suggest testing with sandbox URLs):

import bullpay

paypal_client = bullpay.PaypalClient("https://www.paypal.com/cgi-bin/webscr", "https://api-3t.paypal.com/nvp", "my_paypal_api_username", "my_paypal_api_password", "my_paypal_signature")

On your order page:

# start the checkout process
checkout = paypal_client.setExpressCheckout("11.27", "http://my.site.com/order_confirm", "http://my.site.com/order_cancel", {"DESC":"My order description!"})
checkout.put() # the client does not do "puts" -- store this checkout order if you want to retrieve it later
# redirect to paypal to get express checkout details (shipping address, etc.)
self.redirect(checkout.url)

On your order confirmation page (e.g. "/order_confirm"):

# get the Paypal token and payer_id from the request information
token = self.request.get("token")
payer_id = self.request.get("PayerID")

# get the checkout details
details = paypal_client.getExpressCheckoutDetails(token)
details.put() # again the client does not do "puts" -- store these details if you want to retrieve them later

# send some HTML including a link or button to call the submit code
self.response.out.write(template.render("my_template_path.html", {"token":token, "payer_id":payer_id}))

In your order submit code:

# get the token, payer_id, and amount (e.g. from passing along form values; storing in the DB; session data; etc.)
token = self.request.get("token")
payer_id = self.request.get("payer_id")
amt = lookup_amount_for_order(token, payer_id) # your code here

# call for payment (this charges the user's Paypal account for the amt)
payment = paypal_client.doExpressCheckoutPayment(token, payer_id, amt)
payment.put() # again the client does not do "puts" -- store this payment if you want to retrieve it later

# send some HTML including a "thanks!" and so on
self.response.out.write(template.render("my_template_path.html", {}))

"""

from google.appengine.api import urlfetch
from google.appengine.ext import db

from cgi import parse_qs
from django.utils import simplejson as json
from hashlib import sha1
from hmac import new as hmac
from random import getrandbits
from time import time
from urllib import urlencode
from urllib import quote as urlquote
from urllib import unquote as urlunquote

import logging

# TOKEN=EC%2d5T6750760H465573R&TIMESTAMP=2009%2d12%2d12T05%3a00%3a39Z&CORRELATIONID=6620813c42c5d&ACK=Success&VERSION=51%2e0&BUILD=1105502
class ExpressCheckout(db.Expando):
  url = db.StringProperty(required=True)
  created = db.DateTimeProperty(auto_now_add=True)
  TOKEN = db.StringProperty(required=True)
  TIMESTAMP = db.StringProperty(required=True)
  CORRELATIONID = db.StringProperty(required=True)
  ACK = db.StringProperty(required=True)
  VERSION = db.StringProperty(required=True)
  BUILD = db.StringProperty(required=True)
  PAYERID = db.StringProperty() # updated after callback

# TOKEN=EC%2d9MT2559869938661C&TIMESTAMP=2009%2d12%2d12T19%3a52%3a08Z&CORRELATIONID=3d3308c953c49&ACK=Success&VERSION=51%2e0&BUILD=1105502&EMAIL=name%40domain%2ecom&PAYERID=FOO12345678&PAYERSTATUS=verified&FIRSTNAME=Firstname&LASTNAME=Lastname&COUNTRYCODE=US&SHIPTONAME=Firstname%20Lastname&SHIPTOSTREET=Number%20Roadname%20Road&SHIPTOCITY=Durham&SHIPTOSTATE=NC&SHIPTOZIP=27705&SHIPTOCOUNTRYCODE=US&SHIPTOCOUNTRYNAME=United%20States&ADDRESSSTATUS=Confirmed
class ExpressCheckoutDetails(db.Expando):
  created = db.DateTimeProperty(auto_now_add=True)
  TOKEN = db.StringProperty(required=True)
  TIMESTAMP = db.StringProperty(required=True)
  CORRELATIONID = db.StringProperty(required=True)
  ACK = db.StringProperty(required=True)
  EMAIL = db.EmailProperty(required=True)
  PAYERID = db.StringProperty(required=True)
  PAYERSTATUS = db.StringProperty(required=True)
  SHIPTONAME = db.StringProperty(required=True)
  SHIPTOSTREET = db.StringProperty(required=True)
  SHIPTOCITY = db.StringProperty(required=True)
  SHIPTOCOUNTRYNAME = db.StringProperty(required=True)
  SHIPTOCOUNTRYCODE = db.StringProperty(required=True)
  SHIPTOSTATE = db.StringProperty(required=True)
  SHIPTOZIP = db.StringProperty(required=True)
  ADDRESSSTATUS = db.StringProperty(required=True)
  VERSION = db.StringProperty(required=True)
  BUILD = db.StringProperty(required=True)

# TOKEN=EC%2d5T6750760H465573R&TIMESTAMP=2009%2d12%2d12T05%3a28%3a15Z&CORRELATIONID=8af611871dbdb&ACK=Success&VERSION=51%2e0&BUILD=1105502&TRANSACTIONID=1R867831CS482083Y&TRANSACTIONTYPE=expresscheckout&PAYMENTTYPE=instant&ORDERTIME=2009%2d12%2d12T05%3a28%3a14Z&AMT=5%2e55&FEEAMT=0%2e33&TAXAMT=0%2e00&CURRENCYCODE=USD&PAYMENTSTATUS=Completed&PENDINGREASON=None&REASONCODE=None
class ExpressCheckoutPayment(db.Expando):
  created = db.DateTimeProperty(auto_now_add=True)
  TOKEN = db.StringProperty(required=True)
  TIMESTAMP = db.StringProperty(required=True)
  CORRELATIONID = db.StringProperty(required=True)
  ACK = db.StringProperty(required=True)
  ORDERTIME = db.StringProperty(required=True)
  AMT = db.StringProperty(required=True)
  FEEAMT = db.StringProperty(required=True)
  TAXAMT = db.StringProperty(required=True)
  PAYMENTSTATUS = db.StringProperty(required=True)
  PENDINGREASON = db.StringProperty(required=True)
  REASONCODE = db.StringProperty(required=True)
  TRANSACTIONID = db.StringProperty(required=True)
  TRANSACTIONTYPE = db.StringProperty(required=True)
  PAYMENTTYPE = db.StringProperty(required=True)
  VERSION = db.StringProperty(required=True)
  BUILD = db.StringProperty(required=True)

def parse_content(content):
  content_dict = {}
  content_pairs = content.split("&")
  for content_pair in content_pairs:
    k,v = content_pair.split("=")
    content_dict[k] = urlunquote(v)
  return content_dict

class PaypalClient:
  def __init__(self, url, api_url, api_username, api_password, signature):
    self.url = url
    self.api_url = api_url
    self.api_username = api_username
    self.api_password = api_password
    self.signature = signature

  # make a remote API call
  def call(self, params, additional_params={}):
    params.update(additional_params)
    encoded_params = urlencode(params)
    logging.info("encoded_params:%s",encoded_params)
    r = urlfetch.fetch(self.api_url, payload=encoded_params, method=urlfetch.POST, deadline=10) # TODO: retry if slowness
    logging.info("response.status_code:%s", r.status_code)
    logging.info("response.headers:%s", r.headers)
    logging.info("response.content:%s", r.content)
    content_dict = parse_content(r.content)
    logging.info("content_dict:%s", content_dict)
    return content_dict

  # step 1: setup the express checkout
  # amount is a string 11.27
  # additional_params like DESC, CUSTOM, INVNUM, NOTETEXT, ALLOWNOTE=1, MAXAMT=100.00
  def setExpressCheckout(self, amount, return_url, cancel_url, currency_code="USD", payment_action="Sale", additional_params={}):
    params = {"USER":self.api_username, "PWD":self.api_password, "SIGNATURE":self.signature, "VERSION":"51.0", "METHOD":"SetExpressCheckout", "AMT":amount, "CURRENCYCODE":currency_code, "RETURNURL":return_url, "CANCELURL":cancel_url, "PAYMENTACTION":payment_action}
    content_dict = self.call(params, additional_params)

    # TODO: check ACK, status_code
    # TIMESTAMP=2009%2d12%2d12T04%3a19%3a26Z&CORRELATIONID=c3d02d8fd50d6&ACK=Failure&VERSION=51%2e0&BUILD=1077585&L_ERRORCODE0=10002&L_SHORTMESSAGE0=Security%20error&L_LONGMESSAGE0=Security%20header%20is%20not%20valid&L_SEVERITYCODE0=Error
    # TOKEN=EC%2d2HF62213290796006&TIMESTAMP=2009%2d12%2d12T04%3a24%3a11Z&CORRELATIONID=65bdbe3887390&ACK=Success&VERSION=51%2e0&BUILD=1105502

    nparams = {"cmd":"_express-checkout", "token":content_dict["TOKEN"]} # , "AMT":amount, "CURRENCYCODE":currency_code, "RETURNURL":return_url, "CANCELURL":cancel_url}
    logging.info("nparams:", nparams)

    rurl = self.url + "?" + urlencode(nparams)
    logging.info("rurl:%s", rurl)

    checkout_params = {}
    checkout_params.update(content_dict)
    checkout_params.update(additional_params)

    logging.info("checkout_params:%s", checkout_params)
    checkout = ExpressCheckout(url=rurl, **checkout_params) # token=content_dict["TOKEN"])
    # TODO: check error in above model create (implies insufficient data from API return)
    return checkout

  def getExpressCheckoutDetails(self, token, additional_params={}):
    params = {"USER":self.api_username, "PWD":self.api_password, "SIGNATURE":self.signature, "VERSION":"51.0", "METHOD":"GetExpressCheckoutDetails", "TOKEN":token}
    content_dict = self.call(params, additional_params)
    # TODO: check ACK, status_code
    details = ExpressCheckoutDetails(**content_dict)
    # TODO: handle missing element error above (implies error getting details)
    return details

  def doExpressCheckoutPayment(self, token, payer_id, amount, currency_code="USD", payment_action="Sale", additional_params={}):
    params = {"USER":self.api_username, "PWD":self.api_password, "SIGNATURE":self.signature, "VERSION":"51.0", "METHOD":"DoExpressCheckoutPayment", "TOKEN":token, "PAYERID":payer_id, "AMT":amount, "CURRENCYCODE":currency_code, "PAYMENTACTION":payment_action}
    content_dict = self.call(params, additional_params)
    # TODO: check ACK, status_code
    payment = ExpressCheckoutPayment(**content_dict)
    # TODO: handle missing element error above (implies error getting details)
    return payment

def get_checkout_by_token(token):
  return ExpressCheckout.all().filter("TOKEN",token).get()

