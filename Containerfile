FROM rockylinux/rockylinux:10-minimal

LABEL maintainer="StackOps"
LABEL description="OpenStack Automatic Backup - Automated backup solution for OpenStack instances and volumes"
LABEL org.opencontainers.image.source="https://git.stackops.ch/stackops/os-backup-scheduler"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Rocky 10 ships Python 3.12; stackops-cloud requires 3.14. uv installs a
# standalone 3.14 under /opt/python and builds the venv from it, so the base
# image stays the fleet's EL10 standard. git is build-time only, for the
# git+https dependency in requirements.txt.
RUN microdnf install -y git ca-certificates \
    && microdnf clean all

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

RUN uv python install 3.14 && uv venv --python 3.14 /opt/venv

COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/requirements.txt

# Create app directory
WORKDIR /app

# Copy scripts
COPY openstack-backup.py /app/openstack-backup.py
COPY openstack-verify.py /app/openstack-verify.py

# Make scripts executable
RUN chmod +x /app/openstack-backup.py /app/openstack-verify.py

# Set entrypoint
ENTRYPOINT ["/app/openstack-backup.py"]
