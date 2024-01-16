import base64
import datetime
from io import BytesIO
import os
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse, unquote

from flask import abort, flash, make_response, redirect, render_template, \
    request, Response, session, url_for, get_flashed_messages, jsonify
from flask_login import current_user, login_user, logout_user
from flask_jwt_extended import create_access_token, create_refresh_token, \
    set_access_cookies, unset_jwt_cookies, get_jwt
from flask_mail import Message
import pyotp
import qrcode
import i18n
from werkzeug.security import check_password_hash

from qwc_services_core.auth import get_username
from qwc_services_core.database import DatabaseEngine
from qwc_services_core.config_models import ConfigModels
from qwc_services_core.runtime_config import RuntimeConfig

from forms import LoginForm, NewPasswordForm, EditPasswordForm, VerifyForm


POST_PARAM_LOGIN = os.environ.get("POST_PARAM_LOGIN", default="False")
if POST_PARAM_LOGIN.lower() in ("f", "false"):
    POST_PARAM_LOGIN = False

# max number of failed login attempts before sign in is blocked
MAX_LOGIN_ATTEMPTS = int(os.environ.get('MAX_LOGIN_ATTEMPTS', 20))

# enable two factor authentication using TOTP
TOTP_ENABLED = os.environ.get('TOTP_ENABLED', 'False').lower() == 'true'

# issuer name for QR code URI
TOTP_ISSUER_NAME = os.environ.get('TOTP_ISSUER_NAME', 'QWC Services')


class DBAuth:
    """DBAuth class

    Provide user login and password reset with local user database.
    """

    # name of default admin user
    DEFAULT_ADMIN_USER = 'admin'

    # authentication form fields
    USERNAME = 'username'
    PASSWORD = 'password'

    # reasons requiring password change
    PASSWORD_CHANGE_REASON_FIRST_LOGIN = 'first_login'
    PASSWORD_CHANGE_REASON_EXPIRED = 'expired'

    def __init__(self, tenant, mail, app):
        """Constructor

        :param str tenant: Tenant ID
        :param flask_mail.Mail mail: Application mailer
        :param App app: Flask application
        """
        self.tenant = tenant
        self.mail = mail
        self.app = app
        self.logger = app.logger

        config_handler = RuntimeConfig("dbAuth", self.logger)
        config = config_handler.tenant_config(tenant)

        self.login_logo = config.get('logo_image_url')
        self.login_background = config.get('background_image_url')
        self.customstylesheet = config.get('customstylesheet')
        if not self.customstylesheet.startswith('/') and not self.customstylesheet.startswith('http'):
            self.customstylesheet = url_for('static', filename=self.customstylesheet)
        self.terms_url = config.get('terms_url')
        self.login_hint = config.get('login_hint')
        if isinstance(self.login_hint, dict):
            self.login_hint = self.login_hint.get(
                i18n.get('locale'),
                self.login_hint.get('en', '')
        )
        self.favicon = config.get('favicon')
        db_url = config.get('db_url')

        # get password constraints from config
        self.password_constraints = {
            'min_length': config.get('password_min_length', 8),
            'max_length': config.get('password_max_length', -1),
            'constraints': config.get('password_constraints', []),
            'min_constraints': config.get('password_min_constraints', 0),
            'constraints_message': config.get(
                'password_constraints_message',
                "Password does not match constraints"
            ),
            'expiry': config.get('password_expiry', -1),
            'expiry_notice': config.get('password_expiry_notice', -1),
            'update_interval': config.get('password_update_interval', -1),
            'allow_reuse': config.get('password_allow_reuse', True)
        }

        db_engine = DatabaseEngine()
        self.config_models = ConfigModels(
            db_engine, db_url, ['password_histories']
        )
        self.User = self.config_models.model('users')
        self.PasswordHistory = self.config_models.model('password_histories')

        # toggle password history according to config settings
        self.password_history_active = (
            self.password_constraints['expiry'] != -1 or
            self.password_constraints['update_interval'] != -1 or
            not self.password_constraints['allow_reuse']
        )
        if self.password_history_active and self.PasswordHistory is None:
            self.logger.warning(
                "Could not activate password history. "
                "Table 'qwc_config.password_histories' is missing."
            )
            self.password_history_active = False

        # get user info fields from config
        self.user_info_fields = config.get('user_info_fields', [])

    def tenant_base(self):
        """base path for tentant"""
        # Updates config['JWT_ACCESS_COOKIE_PATH'] as side effect
        prefix = self.app.session_interface.get_cookie_path(self.app)
        return prefix.rstrip('/') + '/'

    def csrf_token(self):
        """ Inject CSRF token """
        token = (get_jwt() or {}).get("csrf")
        if token:
            return token
        else:
            return ""

    def login(self):
        """Authorize user and sign in."""
        target_url = url_path(request.args.get('url') or self.tenant_base())
        retry_target_url = url_path(request.args.get('url') or None)
        self.logger.debug("Login with target_url `%s`" % target_url)

        if POST_PARAM_LOGIN:
            # Pass additional parameter specified
            req = request.form
            queryvals = {}
            for key, val in req.items():
                if key not in (self.USERNAME, self.PASSWORD):
                    queryvals[key] = val
            parts = urlparse(target_url)
            target_query = dict(parse_qsl(parts.query))
            target_query.update(queryvals)
            parts = parts._replace(query=urlencode(target_query))
            target_url = urlunparse(parts) or None

        self.clear_verify_session()

        # create session for ConfigDB
        db_session = self.db_session()

        if POST_PARAM_LOGIN:
            username = req.get(self.USERNAME)
            password = req.get(self.PASSWORD)
            if username:
                user = self.find_user(db_session, name=username)
                if self.__user_is_authorized(user, password, db_session):
                    return self.response(
                        self.__login_response(user, target_url), db_session
                    )
                else:
                    self.logger.info(
                        "POST_PARAM_LOGIN: Invalid username or password")
                    return self.response(
                        redirect(url_for('login', url=retry_target_url)),
                        db_session
                    )

        form = LoginForm(meta=wft_locales())
        form.logo = self.login_logo
        form.background = self.login_background
        form.customstylesheet = self.customstylesheet
        form.terms_url = self.terms_url
        form.favicon = self.favicon
        if form.validate_on_submit():
            user = self.find_user(db_session, name=form.username.data)

            # force password change on first sign in of default admin user
            # NOTE: user.last_sign_in_at will be set after successful auth
            force_password_change = (
                user and user.name == self.DEFAULT_ADMIN_USER
                and user.last_sign_in_at is None
            )

            # check if password has expired
            password_has_expired = self.password_has_expired(db_session, user)
            if password_has_expired:
                force_password_change = True

            if self.__user_is_authorized(user, form.password.data,
                                         db_session):
                if not force_password_change:
                    if self.password_history_active:
                        # check if any password history is present
                        pw_history = self.find_latest_password_history(
                            db_session, user=user
                        )
                        if pw_history is None:
                            # add initial password history entry if missing
                            self.create_password_history(db_session, user)

                    if TOTP_ENABLED:
                        session['login_uid'] = user.id
                        session['target_url'] = target_url
                        if user.totp_secret:
                            # show form for verification token
                            return self.response(
                                 self.__verify(db_session, False),
                                 db_session
                            )
                        else:
                            # show form for TOTP setup on first sign in
                            return self.response(
                                self.__setup_totp(db_session, False),
                                db_session
                            )
                    else:
                        # login successful
                        return self.response(
                            self.__login_response(user, target_url),
                            db_session
                        )
                else:
                    if password_has_expired:
                        self.logger.info(
                            "Force password change on expired password"
                        )
                        reason = self.PASSWORD_CHANGE_REASON_EXPIRED
                    else:
                        self.logger.info(
                            "Force password change on first login"
                        )
                        reason = self.PASSWORD_CHANGE_REASON_FIRST_LOGIN

                    return self.response(
                        self.require_password_change(
                            user, reason, target_url, db_session
                        ),
                        db_session
                    )
            else:
                form.username.errors.append(i18n.t('auth.auth_failed'))
                form.password.errors.append(i18n.t('auth.auth_failed'))
                # Maybe different message when
                # user.failed_sign_in_count >= MAX_LOGIN_ATTEMPTS

        return self.response(
            render_template('login.html', form=form, i18n=i18n,
                            title=i18n.t("auth.login_page_title"),
                            login_hint=self.login_hint,
                            csrf_token=self.csrf_token()),
            db_session
        )

    def verify_login(self):
        """Verify user login (e.g. from basic auth header)."""
        req = request.form
        username = req.get(self.USERNAME)
        password = req.get(self.PASSWORD)
        if username:
            db_session = self.db_session()
            user = self.find_user(db_session, name=username)
            if self.__user_is_authorized(user, password, db_session):
                # access_token = create_access_token(identity=username)
                return jsonify({"identity": username})
            else:
                self.logger.info(
                    "verify_login: Invalid username or password")
                abort(401)
        abort(401)

    def verify(self):
        """Handle submit of form for TOTP verification token."""
        # create session for ConfigDB
        db_session = self.db_session()

        return self.response(self.__verify(db_session), db_session)

    def __verify(self, db_session, submit=True):
        """Show form for TOTP verification token.

        :param Session db_session: DB session
        :param bool submit: Whether form was submitted
                            (False if shown after login form)
        """
        if not TOTP_ENABLED or 'login_uid' not in session:
            self.logger.warning("TOTP not enabled or not in login process")
            return redirect(url_for('login'))

        user = self.find_user(db_session, id=session.get('login_uid', None))
        if user is None:
            self.logger.warning("user not found")
            return redirect(url_for('login'))

        form = VerifyForm(meta=wft_locales())
        form.logo = self.login_logo
        form.background = self.login_background
        form.customstylesheet = self.customstylesheet
        form.favicon = self.favicon
        if submit and form.validate_on_submit():
            if self.user_totp_is_valid(user, form.token.data, db_session):
                # TOTP verified
                target_url = session.pop('target_url', self.tenant_base())
                self.clear_verify_session()
                return self.__login_response(user, target_url)
            else:
                flash(i18n.t('auth.verfication_invalid'))
                form.token.errors.append(i18n.t('auth.verfication_invalid'))
                form.token.data = None

            if user.failed_sign_in_count >= MAX_LOGIN_ATTEMPTS:
                self.logger.info("redirect to login after too many login attempts")
                return redirect(url_for('login'))

        return render_template('verify.html', form=form, i18n=i18n,
                               title=i18n.t("auth.verify_page_title"),
                               csrf_token=self.csrf_token())

    def logout(self, identity):
        """Sign out."""
        target_url = url_path(request.args.get('url', self.tenant_base()))
        self.clear_verify_session()
        resp = make_response(redirect(target_url))
        if identity:
            unset_jwt_cookies(resp)
            logout_user()
        return resp

    def setup_totp(self):
        """Handle submit of form with TOTP QR Code and token confirmation."""
        # create session for ConfigDB
        db_session = self.db_session()

        return self.response(self.__setup_totp(db_session), db_session)

    def __setup_totp(self, db_session, submit=True):
        """Show form with TOTP QR Code and token confirmation.

        :param Session db_session: DB session
        :param bool submit: Whether form was submitted
                            (False if shown after login form)
        """
        if not TOTP_ENABLED or 'login_uid' not in session:
            self.logger.warning("TOTP not enabled or not in login process")
            return redirect(url_for('login'))

        user = self.find_user(db_session, id=session.get('login_uid', None))
        if user is None:
            # user not found
            return redirect(url_for('login'))

        totp_secret = session.get('totp_secret', None)
        if totp_secret is None:
            # generate new secret
            totp_secret = pyotp.random_base32()
            # store temp secret in session
            session['totp_secret'] = totp_secret

        form = VerifyForm(meta=wft_locales())
        form.logo = self.login_logo
        form.background = self.login_background
        form.customstylesheet = self.customstylesheet
        form.favicon = self.favicon
        if submit and form.validate_on_submit():
            if pyotp.totp.TOTP(totp_secret).verify(
                form.token.data, valid_window=1
            ):
                # TOTP confirmed

                # save TOTP secret
                user.totp_secret = totp_secret
                # update last sign in timestamp and reset failed attempts
                # counter
                user.last_sign_in_at = datetime.datetime.now(datetime.UTC)
                user.failed_sign_in_count = 0
                db_session.commit()

                target_url = session.pop('target_url', self.tenant_base())
                self.clear_verify_session()
                return self.__login_response(user, target_url)
            else:
                flash(i18n.t('auth.verfication_invalid'))
                form.token.errors.append(i18n.t('auth.verfication_invalid'))
                form.token.data = None

        # enable one-time loading of QR code image
        session['show_qrcode'] = True

        # show form
        resp = make_response(render_template(
            'qrcode.html', form=form, i18n=i18n,
            title=i18n.t("auth.qrcode_page_title"),
            totp_secret=totp_secret,
            csrf_token=self.csrf_token()
        ))
        # do not cache in browser
        resp.headers.set(
            'Cache-Control', 'no-cache, no-store, must-revalidate'
        )
        resp.headers.set('Pragma', 'no-cache')
        resp.headers.set('Expires', '0')

        return resp

    def qrcode(self):
        """Return TOTP QR code."""
        if not TOTP_ENABLED or 'login_uid' not in session:
            self.logger.warning("TOTP not enabled or not in login process")
            abort(404)

        # check presence of show_qrcode
        # to allow one-time loading from TOTP setup form
        if 'show_qrcode' not in session:
            # not in TOTP setup form
            abort(404)
        # remove show_qrcode from session
        session.pop('show_qrcode', None)

        totp_secret = session.get('totp_secret', None)
        if totp_secret is None:
            # temp secret not set
            abort(404)

        # create session for ConfigDB
        db_session = self.db_session()
        # find user by ID
        user = self.find_user(db_session, id=session.get('login_uid', None))
        # close session
        db_session.close()

        if user is None:
            # user not found
            abort(404)

        # generate TOTP URI
        email = user.email or user.name
        uri = pyotp.totp.TOTP(totp_secret).provisioning_uri(
            email, issuer_name=TOTP_ISSUER_NAME
        )

        # generate QR code
        img = qrcode.make(uri, box_size=6, border=1)
        stream = BytesIO()
        img.save(stream, 'PNG')

        return Response(
                stream.getvalue(),
                content_type='image/png',
                headers={
                    # do not cache in browser
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache',
                    'Expires': '0'
                },
                status=200
            )

    def new_password(self):
        """Show form and send reset password instructions."""
        form = NewPasswordForm(meta=wft_locales())
        form.logo = self.login_logo
        form.background = self.login_background
        form.customstylesheet = self.customstylesheet
        form.favicon = self.favicon
        if form.validate_on_submit():
            # create session for ConfigDB
            db_session = self.db_session()

            user = self.find_user(db_session, email=form.email.data)
            if user:
                # generate and save reset token
                user.reset_password_token = self.generate_token()
                db_session.commit()

                # send password reset instructions
                try:
                    self.send_reset_passwort_instructions(user)
                except Exception as e:
                    self.logger.error(
                        "Could not send reset password instructions to "
                        "user '%s':\n%s" % (user.email, e)
                    )
                    flash(i18n.t("auth.reset_mail_failed"))
                    return self.response(
                        render_template(
                            'new_password.html', form=form, i18n=i18n,
                            title=i18n.t("auth.new_password_page_title"),
                            csrf_token=self.csrf_token()
                        ),
                        db_session
                    )
            else:
                self.logger.info("User lookup failed")

            # NOTE: show message anyway even if email not found
            flash(i18n.t("auth.reset_message"))
            return self.response(
                redirect(url_for('login')),
                db_session
            )

        return render_template(
            'new_password.html', form=form, i18n=i18n,
            title=i18n.t("auth.new_password_page_title"),
            csrf_token=self.csrf_token()
        )

    def edit_password(self, token, identity=None):
        """Show form and reset password.

        :param token str: Password reset token
        :param identity obj: JWT identity
        """
        form = self.edit_password_form()
        form.logo = self.login_logo
        form.background = self.login_background
        form.customstylesheet = self.customstylesheet
        form.favicon = self.favicon
        if form.validate_on_submit():
            # create session for ConfigDB
            db_session = self.db_session()

            if identity:
                user = self.find_user(db_session, name=get_username(identity))
            else:
                user = self.find_user(
                    db_session, reset_password_token=form.reset_password_token.data
                )
            if user:
                if not self.can_change_password(db_session, user):
                    # time since last password update was too short
                    flash(i18n.t("auth.edit_password_rate_limited"))
                    if not identity:
                        target_url = unquote(form.url.data) or None
                        return self.response(
                            redirect(url_for('login', url=target_url)),
                            db_session
                        )
                    else:
                        return self.response(
                            render_template(
                                'edit_password.html', form=form, i18n=i18n,
                                title=i18n.t("auth.edit_password_page_title"),
                                csrf_token=self.csrf_token()
                            ),
                            db_session
                        )

                if not self.password_accepted(
                    db_session, user, form.password.data
                ):
                    # password may not be reused

                    # show message in form
                    form.password.errors.append(
                        i18n.t('auth.edit_password_cannot_reuse')
                    )

                    if token:
                        # set hidden field
                        form.reset_password_token.data = token

                    return self.response(
                        render_template(
                            'edit_password.html', form=form, i18n=i18n,
                            title=i18n.t("auth.edit_password_page_title"),
                            csrf_token=self.csrf_token()
                        ),
                        db_session
                    )

                # save new password
                user.set_password(form.password.data)
                # clear token
                user.reset_password_token = None
                if user.last_sign_in_at is None:
                    # set last sign in timestamp after required password change
                    # to mark as password changed
                    user.last_sign_in_at = datetime.datetime.now(datetime.UTC)
                db_session.commit()

                if self.password_history_active:
                    # add new entry to password history
                    self.create_password_history(db_session, user)

                flash(i18n.t("auth.edit_password_successful"))
                target_url = unquote(form.url.data) or None
                if not identity:
                    return self.response(
                        redirect(url_for('login', url=target_url)),
                        db_session
                    )
                else:
                    return self.response(
                        render_template(
                            'edit_password.html', form=form, i18n=i18n,
                            title=i18n.t("auth.edit_password_page_title"),
                            csrf_token=self.csrf_token()
                        ),
                        db_session
                    )
            else:
                # invalid reset token
                flash(i18n.t("auth.edit_password_invalid_token"))
                return self.response(
                    render_template(
                        'edit_password.html', form=form, i18n=i18n,
                        title=i18n.t("auth.edit_password_page_title"),
                        csrf_token=self.csrf_token()
                    ),
                    db_session
                )

        if token:
            # set hidden field
            form.reset_password_token.data = token

        return render_template(
            'edit_password.html', form=form, i18n=i18n,
            title=i18n.t("auth.edit_password_page_title"),
            csrf_token=self.csrf_token()
        )

    def require_password_change(self, user, reason, target_url, db_session):
        """Show form for required password change.

        :param User user: User instance
        :param str reason: Reason for this required password change
        :param str target_url: URL for redirect
        :param Session db_session: DB session
        """
        # clear last sign in timestamp and generate reset token
        # to mark as requiring password change
        user.last_sign_in_at = None
        user.reset_password_token = self.generate_token()
        db_session.commit()

        # show password reset form
        form = self.edit_password_form()
        form.logo = self.login_logo
        form.background = self.login_background
        form.customstylesheet = self.customstylesheet
        form.favicon = self.favicon
        # set hidden fields
        form.reset_password_token.data = user.reset_password_token
        form.url.data = target_url

        if reason == self.PASSWORD_CHANGE_REASON_FIRST_LOGIN:
            flash(i18n.t('auth.edit_password_reason_first_login'))
        elif reason == self.PASSWORD_CHANGE_REASON_EXPIRED:
            flash(i18n.t('auth.edit_password_reason_expired'))

        flash(i18n.t('auth.edit_password_message'))
        return render_template(
            'edit_password.html', form=form, i18n=i18n,
            title=i18n.t("auth.edit_password_page_title"),
            csrf_token=self.csrf_token()
        )

    def edit_password_form(self):
        """Return password reset form with constraints from config."""
        return EditPasswordForm(
            self.password_constraints['min_length'],
            self.password_constraints['max_length'],
            self.password_constraints['constraints'],
            self.password_constraints['min_constraints'],
            self.password_constraints['constraints_message'],
            meta=wft_locales()
        )

    def db_session(self):
        """Return new session for ConfigDB."""
        return self.config_models.session()

    def response(self, response, db_session):
        """Helper for closing DB session before returning response.

        :param obj response: Response
        :param Session db_session: DB session
        """
        # close session
        db_session.close()

        return response

    def find_user(self, db_session, **kwargs):
        """Find user by filter.

        :param Session db_session: DB session
        :param **kwargs: keyword arguments for filter (e.g. name=username)
        """
        return db_session.query(self.User).filter_by(**kwargs).first()

    def load_user(self, id):
        """Load user by id.

        :param int id: User ID
        """
        # create session for ConfigDB
        db_session = self.db_session()
        # find user by ID
        user = self.find_user(db_session, id=id)
        # close session
        db_session.close()

        return user

    def token_exists(self, token):
        """Check if password reset token exists.

        :param str: Password reset token
        """
        # create session for ConfigDB
        db_session = self.db_session()
        # find user by password reset token
        user = self.find_user(db_session, reset_password_token=token)
        # close session
        db_session.close()

        return user is not None

    def __user_is_authorized(self, user, password, db_session):
        """Check credentials, update user sign in fields and
        return whether user is authorized.

        :param User user: User instance
        :param str password: Password
        :param Session db_session: DB session
        """
        if user is None or user.password_hash is None:
            # invalid username or no password set
            return False
        elif user.check_password(password):
            # valid credentials
            if user.failed_sign_in_count < MAX_LOGIN_ATTEMPTS:
                if not TOTP_ENABLED:
                    # update last sign in timestamp and reset failed attempts
                    # counter
                    user.last_sign_in_at = datetime.datetime.now(datetime.UTC)
                    user.failed_sign_in_count = 0
                    db_session.commit()

                return True
            else:
                # block sign in due to too many login attempts
                return False
        else:
            # invalid password

            # increase failed login attempts counter
            user.failed_sign_in_count += 1
            db_session.commit()

            return False

    def user_totp_is_valid(self, user, token, db_session):
        """Check TOTP token, update user sign in fields and
        return whether user is authorized.

        :param User user: User instance
        :param str token: TOTP token
        :param Session db_session: DB session
        """
        if user is None or not user.totp_secret:
            # invalid user ID or blank TOTP secret
            return False
        elif pyotp.totp.TOTP(user.totp_secret).verify(token, valid_window=1):
            # valid token
            # update last sign in timestamp and reset failed attempts counter
            user.last_sign_in_at = datetime.datetime.now(datetime.UTC)
            user.failed_sign_in_count = 0
            db_session.commit()

            return True
        else:
            # invalid token

            # increase failed login attempts counter
            user.failed_sign_in_count += 1
            db_session.commit()

            return False

    def clear_verify_session(self):
        """Clear session values for TOTP verification."""
        session.pop('login_uid', None)
        session.pop('target_url', None)
        session.pop('totp_secret', None)
        session.pop('show_qrcode', None)

    def __login_response(self, user, target_url):
        self.logger.info("Logging in as user '%s'" % user.name)
        # flask_login stores user in session
        login_user(user)

        # Create the tokens we will be sending back to the user
        identity = {
            'username': user.name
        }
        # collect custom user info fields
        user_info = user.user_info
        # Always store user_id
        identity["user_id"] = user.id
        for field in self.user_info_fields:
            if hasattr(user_info, field):
                identity[field] = getattr(user_info, field)
            else:
                self.logger.warning(
                    "User info field '%s' does not exist" % field
                )

        access_token = create_access_token(identity=identity)
        # refresh_token = create_refresh_token(identity=identity)

        # check if password will soon expire
        days = self.days_for_password_expiry_notice(user)
        if days == -1:
            # redirect to target URL
            resp = make_response(redirect(target_url))
        else:
            # dummy form for expiry notice with customization
            form = LoginForm()
            form.logo = self.login_logo
            form.background = self.login_background
            form.customstylesheet = self.customstylesheet
            form.favicon = self.favicon
            # show expiry notice
            page = render_template(
                'notification.html', form=form, i18n=i18n,
                title=i18n.t("auth.notification_page_title"),
                message=i18n.t("auth.notification_expiry_notice", days=days),
                target_url=target_url,
                csrf_token=self.csrf_token()
            )
            resp = make_response(page)

        # Set the JWTs and the CSRF double submit protection cookies
        # in this response
        set_access_cookies(resp, access_token)

        return resp

    def generate_token(self):
        """Generate new token."""
        token = None
        while token is None:
            # generate token
            token = base64.urlsafe_b64encode(os.urandom(15)). \
                rstrip(b'=').decode('ascii')

            # check uniqueness of token
            if self.token_exists(token):
                # token already present
                token = None

        return token

    def send_reset_passwort_instructions(self, user):
        """Send mail with reset password instructions to user.

        :param User user: User instance
        """
        # generate full reset password URL
        reset_url = url_for(
            'edit_password', reset_password_token=user.reset_password_token,
            _external=True
        )

        msg = Message(
            i18n.t('auth.reset_mail_subject'),
            recipients=[user.email]
        )
        # set message body from template
        msg.body = render_template(
            'reset_password_instructions.%s.txt' % i18n.get('locale'),
            user=user, reset_url=reset_url,
            csrf_token=self.csrf_token()
        )

        # send message
        self.logger.debug(msg)
        self.mail.send(msg)

    def find_latest_password_history(self, db_session, **kwargs):
        """Find latest password history entry by filter.

        :param Session db_session: DB session
        :param **kwargs: keyword arguments for filter (e.g. user=user)
        """
        return db_session.query(self.PasswordHistory). \
            order_by(self.PasswordHistory.created_at.desc()). \
            filter_by(**kwargs).first()

    def create_password_history(self, db_session, user):
        """Create a new password history entry for a user and return it.

        :param Session db_session: DB session
        :param User user: User instance
        """
        pw_history = self.PasswordHistory(
            user=user,
            password_hash=user.password_hash,
            created_at=datetime.datetime.now(datetime.UTC)
        )
        db_session.add(pw_history)
        db_session.commit()

        return pw_history

    def days_for_password_expiry_notice(self, user):
        """Return days until password expires within notice period, or -1
        (if password expiry and notice is enabled).

        :param User user: User instance
        """
        days_notice = -1

        expiry_notice = self.password_constraints['expiry_notice']
        if self.password_history_active and expiry_notice != -1:
            # password expiry notice is enabled
            # get days until expiry
            db_session = self.db_session()
            days_remaining = self.days_until_password_expiry(db_session, user)
            db_session.close()

            if days_remaining != -1 and days_remaining <= expiry_notice:
                # remaining days within notice period
                days_notice = days_remaining

        return days_notice

    def days_until_password_expiry(self, db_session, user):
        """Return days until password expires, or -1
        (if password expiry is enabled).

        :param Session db_session: DB session
        :param User user: User instance
        """
        days_remaining = -1

        expiry = self.password_constraints['expiry']
        if self.password_history_active and expiry != -1:
            # password expiry is enabled
            pw_history = self.find_latest_password_history(
                db_session, user=user
            )
            if pw_history:
                # calculate remaining days
                expires_at = pw_history.created_at + datetime.timedelta(days=expiry)
                if datetime.datetime.now(datetime.UTC) < expires_at:
                    delta = expires_at - datetime.datetime.now(datetime.UTC)
                    days_remaining = delta.days
                    if delta.seconds > 0:
                        # round up partial days
                        days_remaining += 1

        return days_remaining

    def password_has_expired(self, db_session, user):
        """Return whether a user's password has expired
        (if password expiry is enabled).

        :param Session db_session: DB session
        :param User user: User instance
        """
        expired = False

        expiry = self.password_constraints['expiry']
        if self.password_history_active and expiry != -1:
            # password expiry is enabled
            pw_history = self.find_latest_password_history(
                db_session, user=user
            )
            if (
                pw_history and
                datetime.datetime.now(datetime.UTC) >
                    pw_history.created_at + datetime.timedelta(days=expiry)
            ):
                # password has expired
                expired = True

        return expired

    def can_change_password(self, db_session, user):
        """Return whether a user may change the password again
        (if password update interval is set).

        :param Session db_session: DB session
        :param User user: User instance
        """
        allow_change = True

        update_interval = self.password_constraints['update_interval']
        if self.password_history_active and update_interval != -1:
            # password update interval is set
            pw_history = self.find_latest_password_history(
                db_session, user=user
            )
            # check time since last password update
            if (
                pw_history and
                datetime.datetime.now(datetime.UTC) <
                    pw_history.created_at + datetime.timedelta(seconds=update_interval)
            ):
                # time since last update was too short
                allow_change = False

        return allow_change

    def password_accepted(self, db_session, user, new_password):
        """Return whether a user's new password is accepted
        (if password reuse is not allowed).

        :param Session db_session: DB session
        :param User user: User instance
        :param str password: Password
        """
        accepted = True

        allow_reuse = self.password_constraints['allow_reuse']
        if self.password_history_active and not allow_reuse:
            # password reuse is not allowed
            # check password history of user
            pw_histories = db_session.query(self.PasswordHistory). \
                order_by('created_at').filter_by(user=user).all()
            for pw_history in pw_histories:
                # check new password against hash in history
                if check_password_hash(pw_history.password_hash, new_password):
                    # password has already been used
                    accepted = False
                    break

        return accepted


def wft_locales():
    return {'locales': [i18n.get('locale')]}


def url_path(url):
    """ Extract path and query parameters from URL """
    o = urlparse(url)
    parts = list(filter(None, [o.path, o.query]))
    return '?'.join(parts)
