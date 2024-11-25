[![](https://github.com/qwc-services/qwc-db-auth/workflows/build/badge.svg)](https://github.com/qwc-services/qwc-db-auth/actions)
[![docker](https://img.shields.io/docker/v/sourcepole/qwc-db-auth?label=Docker%20image&sort=semver)](https://hub.docker.com/r/sourcepole/qwc-db-auth)

Authentication with User DB
===========================

Authentication service with local user database.


Configuration
-------------

The static config files are stored as JSON files in `$CONFIG_PATH` with subdirectories for each tenant,
e.g. `$CONFIG_PATH/default/*.json`. The default tenant name is `default`.

### DB Auth Service config

* [JSON schema](schemas/qwc-db-auth.json)
* File location: `$CONFIG_PATH/<tenant>/dbAuthConfig.json`

Example:
```json
{
  "$schema": "https://raw.githubusercontent.com/qwc-services/qwc-db-auth/master/schemas/qwc-db-auth.json",
  "service": "db-auth",
  "config": {
    "db_url": "postgresql:///?service=qwc_configdb"
  }
}
```

Set the `MAX_LOGIN_ATTEMPTS` environment variable to set the maximum number of
failed login attempts before sign in is blocked (default: `20`).

A minimum password length of `8` with no other constraints is set by default. Optional password complexity constraints can be set using the following `config` options:
```json
"config": {
  "password_min_length": 8,
  "password_max_length": 128,
  "password_constraints": [
      "[A-Z]",
      "[a-z]",
      "\\d",
      "[ !\"#$%&'()*+,\\-./\\\\:;<=>?@\\[\\]^_`{|}~]"
  ],
  "password_min_constraints": 3,
  "password_constraints_message": "Password must contain at least three of these character types: uppercase letters, lowercase letters, numbers, special characters"
}
```

`password_min_length` and `password_max_length` can be set independently. `password_constraints` is a list of regular expression of which at least `password_min_constraints` have to match for the password to be valid, otherwise the `password_constraints_message` is shown. Note that the regular expression have to be JSON escaped and allow only patterns supported by Python's `re` module.

If the `qwc_config.password_histories` table is present, additional optional password constraints may be set:
```json
"config": {
  "password_expiry": 365,
  "password_expiry_notice": 10,
  "password_update_interval": 600,
  "password_allow_reuse": false
}
```

* `password_expiry` (default: `-1`): Number of days until a password expires, or `-1` to disable. Forces a password change once expired.
* `password_expiry_notice` (default: `-1`): Show an expiry notice within this number of days before a password expires, or `-1` to disable
* `password_update_interval` (default: `-1`): Min number of seconds before a password may be changed again, or `-1` to disable
* `password_allow_reuse` (default: `true`): Set whether a user may reuse previous passwords or not

Besides the form based DB login, an (insecure) plain POST login is supported. This method can be
activated by setting `POST_PARAM_LOGIN=True`. User and password are passed as POST parameters
`username` and `password`.
Usage example: `curl -d 'username=demo&password=demo' http://localhost:5017/login`.

Additional user info fields from `qwc_config.user_infos` may be added to the JWT identity by setting `user_info_fields`:
```json
"config": {
  "user_info_fields": ["surname", "first_name"]
}
```

[Flask-Mail](https://pythonhosted.org/Flask-Mail/) is used for sending mails like password resets. These are the available options:
* `MAIL_SERVER`: default ‘localhost’
* `MAIL_PORT`: default 25
* `MAIL_USE_TLS`: default False
* `MAIL_USE_SSL`: default False
* `MAIL_DEBUG`: default app.debug
* `MAIL_USERNAME`: default None
* `MAIL_PASSWORD`: default None
* `MAIL_DEFAULT_SENDER`: default None
* `MAIL_MAX_EMAILS`: default None
* `MAIL_SUPPRESS_SEND`: default app.testing
* `MAIL_ASCII_ATTACHMENTS`: default False

In addition the standard Flask `TESTING` configuration option is used by Flask-Mail in unit tests.

### Two factor authentication

Two factor authentication using TOTP can be enabled by setting the environment variable `TOTP_ENABLED=True`.
This will require an additional verification token after sign in, based on the user's TOTP secret.

A personal QR code for setting up the two factor authentication is shown to the user on first sign in (or if the TOTP secret is empty).
The TOTP issuer name for your application can be set using the environment variable `TOTP_ISSUER_NAME="QWC Services"`.

An user's TOTP secret can be reset by clearing it in the Admin GUI user form.


### Customization

You can add a custom logo and a custom background image by setting the following `config` options:

```json
"config": {
  "background_image_url": "<url>",
  "logo_image_url": "<url>"
}
```

The specified URLs can be absolute or relative. For relative URLs, you can write i.e.

```json
"config": {
  "background_image_url": "/auth/static/background.jpg",
  "logo_image_url": "/auth/static/logo.jpg"
}
```

where `/auth` is the service mountpoint and place your custom images inside the `static` subfolder of the auth-service, or, if using docker and docker-compose, mount them accordingly:

    qwc-auth-service:
      [...]
      volumes:
        - ./volumes/assets/Background.jpg:/srv/qwc_service/static/background.jpg
        - ./volumes/assets/logo.png:/srv/qwc_service/static/logo.jpg

If you want to override some styles, you can set the `customstylesheet` `config` option to the name of a file below the `static` subfolder of the auth-service, and it will get included into the base template.

Usage
-----

Run standalone application:

    python src/server.py

Endpoints:

    http://localhost:5017/login

    http://localhost:5017/logout


Development
-----------

Create a virtual environment:

    python3 -m venv .venv

Activate virtual environment:

    source .venv/bin/activate

Install requirements:

    pip install -r requirements.txt

Set the `CONFIG_PATH` environment variable to the path containing the service config and permission files when starting this service (default: `config`).

    export CONFIG_PATH=../qwc-docker/volumes/config

Configure environment:

    echo FLASK_ENV=development >.flaskenv
    export MAIL_SUPPRESS_SEND=True
    export MAIL_DEFAULT_SENDER=from@example.com

Start local service:

     python src/server.py
