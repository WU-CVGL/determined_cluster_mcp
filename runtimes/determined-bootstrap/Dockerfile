# syntax=docker/dockerfile:1.7
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SDK_VERSION=0.42.1
ARG SCIENCE_PYTHON=/opt/venv/bin/python
ARG SCIENCE_PROFILE
ARG SCIENCE_REVISION
ENV DET_PYTHON_EXECUTABLE=/usr/local/bin/bm-bootstrap-python
COPY bootstrap-python /usr/local/bin/bm-bootstrap-python
COPY bootstrap.txt admit.py /opt/determined-bootstrap/
RUN test "$SDK_VERSION" = "0.42.1" && test -n "$SCIENCE_PROFILE" && test -n "$SCIENCE_REVISION" && \
    chmod 755 /usr/local/bin/bm-bootstrap-python && \
    "$SCIENCE_PYTHON" /opt/determined-bootstrap/admit.py snapshot /opt/determined-bootstrap/science-before.json && \
    /usr/local/bin/python -c 'import site,sys; assert site.ENABLE_USER_SITE is True; assert sys.prefix != "/opt/venv"' && \
    /usr/local/bin/python -m pip install --break-system-packages -r /opt/determined-bootstrap/bootstrap.txt && \
    /usr/local/bin/python -m pip check && \
    "$SCIENCE_PYTHON" /opt/determined-bootstrap/admit.py compare /opt/determined-bootstrap/science-before.json
LABEL org.determined.bootstrap.sdk="${SDK_VERSION}" \
      org.determined.bootstrap.pip="26.2.1" \
      org.determined.bootstrap.science-profile="${SCIENCE_PROFILE}" \
      org.determined.bootstrap.science-revision="${SCIENCE_REVISION}"
