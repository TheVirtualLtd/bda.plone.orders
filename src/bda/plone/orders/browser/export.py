# -*- coding: utf-8 -*-
from AccessControl import Unauthorized
from Acquisition import aq_parent
from bda.plone.cart import get_item_stock
from bda.plone.cart import get_object_by_uid
from bda.plone.orders import message_factory as _
from bda.plone.orders import permissions
from bda.plone.orders import safe_encode
from bda.plone.orders import safe_filename
from bda.plone.orders.browser.views import customers_form_vocab
from bda.plone.orders.browser.views import vendors_form_vocab
from bda.plone.orders.common import DT_FORMAT
from bda.plone.orders.common import get_bookings_soup
from bda.plone.orders.common import get_order
from bda.plone.orders.common import get_orders_soup
from bda.plone.orders.common import get_vendor_uids_for
from bda.plone.orders.common import get_vendors_for
from bda.plone.orders.common import OrderData
from bda.plone.orders.interfaces import IBuyable
from decimal import Decimal
from odict import odict
from plone.uuid.interfaces import IUUID
from Products.CMFPlone.utils import getToolByName
from Products.CMFPlone.utils import safe_unicode
from Products.Five import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from repoze.catalog.query import Any
from repoze.catalog.query import Eq
from repoze.catalog.query import Ge
from repoze.catalog.query import Le
from StringIO import StringIO
from yafowil.base import ExtractionError
from yafowil.controller import Controller
from yafowil.plone.form import YAMLForm

import csv
import datetime
import plone.api
import uuid
import yafowil.loader  # noqa


class DialectExcelWithColons(csv.excel):
    delimiter = ';'


csv.register_dialect('excel-colon', DialectExcelWithColons)


ORDER_EXPORT_ATTRS = [
    'uid',
    'created',
    'ordernumber',
    'cart_discount_net',
    'cart_discount_vat',
    'surcharge_net',
    'surcharge_vat',
    'personal_data.company',
    'personal_data.email',
    'personal_data.gender',
    'personal_data.firstname',
    'personal_data.lastname',
    'personal_data.phone',
    'billing_address.city',
    'billing_address.country',
    'billing_address.street',
    'billing_address.zip',
    'delivery_address.alternative_delivery',
    'delivery_address.firstname',
    'delivery_address.lastname',
    'delivery_address.company',
    'delivery_address.street',
    'delivery_address.city',
    'delivery_address.zip',
    'delivery_address.country',
    'order_comment.comment',
    'payment_selection.payment',
]
COMPUTED_ORDER_EXPORT_ATTRS = odict()
BOOKING_EXPORT_ATTRS = [
    'title',
    'buyable_comment',
    'buyable_count',
    'quantity_unit',
    'net',
    'discount_net',
    'vat',
    'currency',
    'state',
    'salaried',
    'exported',
]
COMPUTED_BOOKING_EXPORT_ATTRS = odict()


def buyable_available(context, booking):
    obj = get_object_by_uid(context, booking.attrs['buyable_uid'])
    if obj:
        item_stock = get_item_stock(obj)
        return item_stock.available
    return None


def buyable_overbook(context, booking):
    obj = get_object_by_uid(context, booking.attrs['buyable_uid'])
    if obj:
        item_stock = get_item_stock(obj)
        return item_stock.overbook
    return None


def buyable_url(context, booking):
    obj = get_object_by_uid(context, booking.attrs['buyable_uid'])
    if obj:
        return obj.absolute_url()
    return None


COMPUTED_BOOKING_EXPORT_ATTRS['buyable_available'] = buyable_available
COMPUTED_BOOKING_EXPORT_ATTRS['buyable_overbook'] = buyable_overbook
COMPUTED_BOOKING_EXPORT_ATTRS['buyable_url'] = buyable_url


def cleanup_for_csv(value):
    """Cleanup a value for CSV export.
    """
    if isinstance(value, datetime.datetime):
        value = value.strftime(DT_FORMAT)
    if value == '-':
        value = ''
    if isinstance(value, float) or \
       isinstance(value, Decimal):
        value = str(value).replace('.', ',')
    return safe_encode(value)


class ExportOrdersForm(YAMLForm):
    browser_template = ViewPageTemplateFile('export.pt')
    form_template = 'bda.plone.orders.browser:forms/orders_export.yaml'
    message_factory = _
    action_resource = 'exportorders'

    def __call__(self):
        # check if authenticated user is vendor
        if not get_vendors_for():
            raise Unauthorized
        self.prepare()
        controller = Controller(self.form, self.request)
        if not controller.next:
            self.rendered_form = controller.rendered
            return self.browser_template(self)
        return controller.next

    def vendor_vocabulary(self):
        return vendors_form_vocab()

    def vendor_mode(self):
        return len(vendors_form_vocab()) > 2 and 'edit' or 'skip'

    def customer_vocabulary(self):
        return customers_form_vocab()

    def customer_mode(self):
        return len(customers_form_vocab()) > 2 and 'edit' or 'skip'

    def from_before_to(self, widget, data):
        from_date = data.fetch('exportorders.from').extracted
        to_date = data.fetch('exportorders.to').extracted
        if to_date <= from_date:
            raise ExtractionError(_('from_date_before_to_date',
                                    default=u'From-date after to-date'))
        return data.extracted

    def export(self, widget, data):
        self.vendor = self.request.form.get('exportorders.vendor')
        self.customer = self.request.form.get('exportorders.customer')
        self.from_date = data.fetch('exportorders.from').extracted
        self.to_date = data.fetch('exportorders.to').extracted

    def export_val(self, record, attr_name):
        """Get attribute from record and cleanup.
        Since the record object is available, you can return aggregated values.
        """
        val = record.attrs.get(attr_name)
        return cleanup_for_csv(val)

    def csv(self, request):
        # get orders soup
        orders_soup = get_orders_soup(self.context)
        # get bookings soup
        bookings_soup = get_bookings_soup(self.context)
        # fetch user vendor uids
        vendor_uids = get_vendor_uids_for()
        # base query for time range
        query = Ge('created', self.from_date) & Le('created', self.to_date)
        # filter by given vendor uid or user vendor uids
        vendor_uid = self.vendor
        if vendor_uid:
            vendor_uid = uuid.UUID(vendor_uid)
            # raise if given vendor uid not in user vendor uids
            if vendor_uid not in vendor_uids:
                raise Unauthorized
            query = query & Any('vendor_uids', [vendor_uid])
        else:
            query = query & Any('vendor_uids', vendor_uids)
        # filter by customer if given
        customer = self.customer
        if customer:
            query = query & Eq('creator', customer)
        # prepare csv writer
        sio = StringIO()
        ex = csv.writer(sio, dialect='excel-colon', quoting=csv.QUOTE_MINIMAL)
        # exported column keys as first line
        ex.writerow(ORDER_EXPORT_ATTRS +
                    COMPUTED_ORDER_EXPORT_ATTRS.keys() +
                    BOOKING_EXPORT_ATTRS +
                    COMPUTED_BOOKING_EXPORT_ATTRS.keys())
        # query orders
        for order in orders_soup.query(query):
            # restrict order bookings for current vendor_uids
            order_data = OrderData(self.context,
                                   order=order,
                                   vendor_uids=vendor_uids)
            order_attrs = list()
            # order export attrs
            for attr_name in ORDER_EXPORT_ATTRS:
                val = self.export_val(order, attr_name)
                order_attrs.append(val)
            # computed order export attrs
            for attr_name in COMPUTED_ORDER_EXPORT_ATTRS:
                cb = COMPUTED_ORDER_EXPORT_ATTRS[attr_name]
                val = cb(self.context, order_data)
                val = cleanup_for_csv(val)
                order_attrs.append(val)
            for booking in order_data.bookings:
                booking_attrs = list()
                # booking export attrs
                for attr_name in BOOKING_EXPORT_ATTRS:
                    val = self.export_val(booking, attr_name)
                    booking_attrs.append(val)
                # computed booking export attrs
                for attr_name in COMPUTED_BOOKING_EXPORT_ATTRS:
                    cb = COMPUTED_BOOKING_EXPORT_ATTRS[attr_name]
                    val = cb(self.context, booking)
                    val = cleanup_for_csv(val)
                    booking_attrs.append(val)
                ex.writerow(order_attrs + booking_attrs)
                booking.attrs['exported'] = True
                bookings_soup.reindex(booking)
        # create and return response
        s_start = self.from_date.strftime('%G-%m-%d_%H-%M-%S')
        s_end = self.to_date.strftime('%G-%m-%d_%H-%M-%S')
        filename = 'orders-export-%s-%s.csv' % (s_start, s_end)
        self.request.response.setHeader('Content-Type', 'text/csv')
        self.request.response.setHeader('Content-Disposition',
                                        'attachment; filename=%s' % filename)
        ret = sio.getvalue()
        sio.close()
        return ret


class ExportOrdersContextual(BrowserView):

    def __call__(self):
        user = plone.api.user.get_current()
        # check if authenticated user is vendor
        if not user.checkPermission(permissions.ModifyOrders, self.context):
            raise Unauthorized

        # Special case for constructed objects like IEventOccurrence from
        # plone.app.event
        title = self.context.title or aq_parent(self.context).title

        filename = u'{0}_{1}.csv'.format(
            safe_unicode(title),
            safe_unicode(datetime.datetime.now().strftime('%Y-%m-%d_%H-%M'))
        )
        filename = safe_filename(filename)
        resp = self.request.response
        resp.setHeader('content-type', 'text/csv; charset=utf-8')
        resp.setHeader(
            'content-disposition',
            'attachment;filename={}'.format(filename)
        )
        return self.get_csv()

    def export_val(self, record, attr_name):
        """Get attribute from record and cleanup.
        Since the record object is available, you can return aggregated values.
        """
        val = record.attrs.get(attr_name)
        return cleanup_for_csv(val)

    def get_csv(self):
        context = self.context

        # prepare csv writer
        sio = StringIO()
        ex = csv.writer(sio, dialect='excel-colon', quoting=csv.QUOTE_MINIMAL)
        # exported column keys as first line
        ex.writerow(ORDER_EXPORT_ATTRS +
                    COMPUTED_ORDER_EXPORT_ATTRS.keys() +
                    BOOKING_EXPORT_ATTRS +
                    COMPUTED_BOOKING_EXPORT_ATTRS.keys())

        bookings_soup = get_bookings_soup(context)

        # First, filter by allowed vendor areas
        vendor_uids = get_vendor_uids_for()
        query_b = Any('vendor_uid', vendor_uids)

        # Second, query for the buyable
        query_cat = {}
        query_cat['object_provides'] = IBuyable.__identifier__
        query_cat['path'] = '/'.join(context.getPhysicalPath())
        cat = getToolByName(context, 'portal_catalog')
        res = cat(**query_cat)
        buyable_uids = [IUUID(it.getObject()) for it in res]

        query_b = query_b & Any('buyable_uid', buyable_uids)

        all_orders = {}
        for booking in bookings_soup.query(query_b):
            booking_attrs = []
            # booking export attrs
            for attr_name in BOOKING_EXPORT_ATTRS:
                val = self.export_val(booking, attr_name)
                booking_attrs.append(val)
            # computed booking export attrs
            for attr_name in COMPUTED_BOOKING_EXPORT_ATTRS:
                cb = COMPUTED_BOOKING_EXPORT_ATTRS[attr_name]
                val = cb(context, booking)
                val = cleanup_for_csv(val)
                booking_attrs.append(val)

            # create order_attrs, if it doesn't exist
            order_uid = booking.attrs.get('order_uid')
            if order_uid not in all_orders:
                order = get_order(context, order_uid)
                order_data = OrderData(context,
                                       order=order,
                                       vendor_uids=vendor_uids)
                order_attrs = []
                # order export attrs
                for attr_name in ORDER_EXPORT_ATTRS:
                    val = self.export_val(order, attr_name)
                    order_attrs.append(val)
                # computed order export attrs
                for attr_name in COMPUTED_ORDER_EXPORT_ATTRS:
                    cb = COMPUTED_ORDER_EXPORT_ATTRS[attr_name]
                    val = cb(self.context, order_data)
                    val = cleanup_for_csv(val)
                    order_attrs.append(val)
                all_orders[order_uid] = order_attrs

            ex.writerow(all_orders[order_uid] + booking_attrs)

            # TODO: also set for contextual exports? i'd say no.
            # booking.attrs['exported'] = True
            # bookings_soup.reindex(booking)

        ret = sio.getvalue()
        sio.close()
        return ret
