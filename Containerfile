FROM rockylinux/rockylinux:10-minimal

LABEL maintainer="Net Architect"
LABEL description="OpenStack Automatic Backup - Automated backup solution for OpenStack instances and volumes"
LABEL org.opencontainers.image.source="https://github.com/net-architect-cloud/os-backup-scheduler"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Install system dependencies
RUN microdnf install -y \
    python3 \
    python3-pip \
    && microdnf clean all

# Install OpenStack SDK and CLI
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Create app directory
WORKDIR /app

# Copy scripts
COPY openstack-backup.py /app/openstack-backup.py
COPY openstack-verify.py /app/openstack-verify.py

# Make scripts executable
RUN chmod +x /app/openstack-backup.py /app/openstack-verify.py

# Set entrypoint
ENTRYPOINT ["/app/openstack-backup.py"]
