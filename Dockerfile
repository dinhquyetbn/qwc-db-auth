FROM sourcepole/qwc-uwsgi-base:alpine-v2023.10.26

ADD requirements.txt /srv/qwc_service/requirements.txt
# ADD qwc_services_core-1.3.28-py3-none-any.whl /srv/qwc_service/qwc_services_core-1.3.28-py3-none-any.whl
# git: Required for pip with git repos
# postgresql-dev g++ python3-dev: Required for psycopg2
RUN \
    apk add --no-cache --update --virtual runtime-deps postgresql-libs && \
    apk add --no-cache --update --virtual build-deps git postgresql-dev g++ python3-dev && \
    # pip3 install --no-cache-dir /srv/qwc_service/qwc_services_core-1.3.28-py3-none-any.whl && \
    pip3 install --no-cache-dir -r /srv/qwc_service/requirements.txt && \
    apk del build-deps

ADD src /srv/qwc_service/

ENV SERVICE_MOUNTPOINT=/auth
