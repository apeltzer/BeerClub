"RequestHandler subclass."

import base64
import json
import logging
import urllib
from collections import OrderedDict as OD

import couchdb
import tornado.web

from . import constants
from . import settings
from . import utils
from .saver import Saver


class SnapshotSaver(Saver):
    doctype = constants.SNAPSHOT


class RequestHandler(tornado.web.RequestHandler):
    "Base request handler."

    def prepare(self):
        "Get the database connection."
        try:
            self.db = utils.get_dbserver()[settings['DATABASE_NAME']]
        except couchdb.http.ResourceNotFound:
            raise KeyError("CouchDB database '%s' does not exist." % 
                           settings['DATABASE_NAME'])

    def get_template_namespace(self):
        "Set the variables accessible within the template."
        result = super().get_template_namespace()
        result['constants'] = constants
        result['settings'] = settings
        result['is_admin'] = self.is_admin()
        result['error'] = self.get_cookie('error', '').replace('_', ' ')
        self.clear_cookie('error')
        result['message'] = self.get_cookie('message', '').replace('_', ' ')
        self.clear_cookie('message')
        return result

    def see_other(self, name, *args, **kwargs):
        """Redirect to the absolute URL given by name
        using HTTP status 303 See Other."""
        query = kwargs.copy()
        try:
            self.set_error_flash(query.pop('error'))
        except KeyError:
            pass
        try:
            self.set_message_flash(query.pop('message'))
        except KeyError:
            pass
        url = self.absolute_reverse_url(name, *args, **query)
        self.redirect(url, status=303)

    def absolute_reverse_url(self, name, *args, **query):
        "Get the absolute URL given the handler name, arguments and query."
        if name is None:
            path = ''
        else:
            path = self.reverse_url(name, *args, **query)
        return settings['BASE_URL'].rstrip('/') + path

    def reverse_url(self, name, *args, **query):
        "Allow adding query arguments to the URL."
        url = super().reverse_url(name, *args)
        url = url.rstrip('?')   # tornado bug? sometimes left-over '?'
        if query:
            url += '?' + urllib.parse.urlencode(query)
        return url

    def set_message_flash(self, message):
        "Set message flash cookie."
        self.set_flash('message', message)

    def set_error_flash(self, message):
        "Set error flash cookie message."
        self.set_flash('error', message)

    def set_flash(self, name, message):
        message = message.replace(' ', '_')
        message = message.replace(';', '_')
        message = message.replace(',', '_')
        self.set_cookie(name, message)

    def get_doc(self, key, viewname=None):
        """Get the document with the given id, or from the given view.
        Raise KeyError if not found.
        """
        return utils.get_doc(self.db, key, viewname=viewname)

    def get_docs(self, viewname, key=None, last=None, **kwargs):
        """Get the list of documents using the named view
        and the given key or interval.
        """
        return utils.get_docs(self.db, viewname, key=key, last=last, **kwargs)

    def get_member(self, email, check=False):
        """Get the member identified by the email address.
        If Swish is enabled, then also check if 'email' is
        a Swish number, which must match exactly.
        Raise KeyError if no such member or if not allowed to view it.
        """
        try:
            member = utils.get_member(self.db, email)
        except KeyError:
            raise KeyError('No such member account.')
        if check:
            if not (self.is_admin() or 
                    member['email'] == self.current_user['email']):
                raise KeyError('You may not view the member account.')
        return member

    def get_balance(self, member=None):
        "Get the current balance for the member, or the sum of all members."
        if member is None:
            result = list(self.db.view('event/credit', group=False))
        else:
            result = list(self.db.view('event/credit',
                                       key=member['email'],
                                       group_level=1))
        if result:
            return result[0].value
        else:
            return 0

    def get_beerclub_balance(self):
        "Get the current balance for the Beer Club account (i.e. payments)."
        result = list(self.db.view('event/payment', group=False))
        if result:
            return result[0].value
        else:
            return 0

    def get_count(self, member, date=None):
        "Get the number of beverages purchased on the given date."
        if date is None:
            date = utils.today()
        result = list(self.db.view('event/beverage',
                                   key=[member['email'], date],
                                   group_level=2))
        if result:
            return result[0].value
        else:
            return 0

    def get_current_user(self):
        """Get the currently logged-in user member, or None.
        This overrides a tornado function, otherwise it should have
        been called 'get_current_member', since the term 'member'
        is used in this code rather than 'user'."""
        try:
            user = self.get_current_user_session()
        except ValueError:
            try:
                user = self.get_current_user_basic()
            except ValueError:
                try:
                    user = self.get_current_user_api_key()
                except ValueError:
                    return None
        self.create_snapshot(user)
        return user

    def get_current_user_session(self):
        """Get the current user from a secure login session cookie.
        Raise ValueError if no or erroneous authentication.
        """
        email = self.get_secure_cookie(
            constants.USER_COOKIE,
            max_age_days=settings['LOGIN_SESSION_DAYS'])
        if not email: raise ValueError
        email = email.decode('utf-8')
        try:
            member = self.get_member(email)
        except KeyError:
            raise ValueError
        # Disabled; must not be allowed to login.
        if member.get('disabled'):
            logging.info("Session auth: DISABLED %s", member['email'])
            raise ValueError
        else:
            # Check if valid login session.
            if member.get('login') is None: raise ValueError
            # All fine.
            logging.info("Session auth: %s", member['email'])
            return member

    def get_current_user_basic(self):
        """Get the current user by HTTP Basic authentication.
        This should be used only if the site is using TLS (SSL, https).
        Raise ValueError if no or erroneous authentication.
        """
        try:
            auth = self.request.headers['Authorization']
        except KeyError:
            raise ValueError
        try:
            auth = auth.split()
            if auth[0].lower() != 'basic': raise ValueError
            auth = base64.b64decode(auth[1])
            email, password = auth.split(':', 1)
            member = self.get_member(email)
            print('given', utils.hashed_password(password))
            print('stored', member.get('password'))
            if utils.hashed_password(password) != member.get('password'):
                raise ValueError
        except (IndexError, ValueError, TypeError):
            raise ValueError
        if member.get('disabled'):
            logging.info("Basic auth login: DISABLED %s", member['email'])
            raise ValueError
        else:
            logging.info("Basic auth login: %s", member['email'])
            return member

    def get_current_user_api_key(self):
        """Get the current user by API key authentication.
        Raise ValueError if no or erroneous authentication.
        """
        try:
            api_key = self.request.headers[constants.API_KEY_HEADER]
        except KeyError:
            raise ValueError
        else:
            try:
                member = self.get_doc(api_key, 'member/api_key')
            except KeyError:
                raise ValueError
            if member.get('status') != constants.ENABLED:
                logging.info("API key login: NOT ENABLED %s", member['email'])
                raise ValueError
            else:
                logging.info("API key login: %s", member['email'])
                return member

    def is_admin(self):
        "Is the current user an administrator?"
        if not self.current_user: return False
        return self.current_user['role'] == constants.ADMIN

    def check_admin(self):
        "Check that the current user is an administrator."
        if not self.is_admin():
            raise tornado.web.HTTPError(403, reason="Role 'admin' is required")

    def create_snapshot(self, user):
        "Create snapshot if not done today."
        # The snapshot is of the state of things the day before.
        date = utils.today(-1)
        try:
            self.get_doc(date, 'snapshot/date')
        except KeyError:
            # Explicit member is required to avoid infinite recursion.
            with SnapshotSaver(rqh=self, member=user) as saver:
                saver['date'] = date
                saver['beerclub_balance'] = self.get_beerclub_balance()
                saver['members_balance'] = self.get_balance()
                counts = dict([(s, 0) for s in constants.STATUSES])
                for row in self.db.view('member/status'):
                    counts[row.key] += 1
                saver['member_counts'] = counts


class ApiMixin(object):
    "Mixin for API and JSON handling."

    def get_json_body(self):
        "Return the body of the request interpreted as JSON."
        content_type = self.request.headers.get('Content-Type', '')
        if content_type.startswith(constants.JSON_MIME):
            return json.loads(self.request.body)
        else:
            return {}

    def check_xsrf_cookie(self):
        "Do not check for XSRF cookie when API."
        pass
