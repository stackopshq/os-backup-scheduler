FROM rockylinux/rockylinux:10-ubi-init

LABEL maintainer="Net Architect"
LABEL description="OpenStack Automatic Backup - Automated backup solution for OpenStack instances and volumes"
LABEL org.opencontainers.image.source="https://github.com/net-architect-cloud/os-backup-scheduler"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Install system dependencies
RUN dnf install -y \
    python3 \
    python3-pip \
    jq \
    findutils \
    && dnf clean all

# Install OpenStack CLI
RUN pip3 install --no-cache-dir python-openstackclient

# Create app directory
WORKDIR /app

# Copy shared library first (changes less often, better layer caching)
COPY lib/common.sh /app/lib/common.sh

# Copy scripts
COPY openstack-backup.sh /app/openstack-backup.sh
COPY verify-backups.sh /app/verify-backups.sh

# Make scripts executable
RUN chmod +x /app/openstack-backup.sh /app/verify-backups.sh

# Set entrypoint
ENTRYPOINT ["/app/openstack-backup.sh"]
