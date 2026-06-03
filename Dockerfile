# Start from the Miniconda base image to use Conda
FROM continuumio/miniconda3:25.3.1-1

# Set the working directory in the container
WORKDIR /auto-importer

# Create a Conda environment
RUN conda create -n auto-import-env python=3.12 -y

# Install omero-py, bftools, and psycopg2 using Conda
RUN conda install -n auto-import-env -c conda-forge omero-py -y && \
    conda install -n auto-import-env -c bioconda bftools -y && \
    conda install -n auto-import-env -c conda-forge psycopg2 libffi==3.4.4 -y && \
    conda install -n auto-import-env -c conda-forge intel-openmp=2019.4 -y

# Activate the environment by setting the path to environment's bin directory
ENV PATH /opt/conda/envs/auto-import-env/bin:$PATH

# Install git and system prerequisites for building PostgreSQL drivers
# And for podman in podman
RUN apt-get update && apt-get install -y \
    git \
    python3-dev \
    libpq-dev \
    build-essential \
    fuse-overlayfs \
    podman \
    unzip

# Create a group and user with specified GID and UID
RUN groupadd -g 1000 autoimportgroup && \
    useradd -m -r -u 1000 -g autoimportgroup autoimportuser

# ----------------- Setting up Podman in Podman ---------------- #
# Taking inspiration from RHEL/Podman's own blogs:
#   - https://www.redhat.com/en/blog/podman-inside-container
#   - https://github.com/containers/image_build/blob/main/podman/Containerfile
#
# Below setup still requires specifically that the user (autoimportuser here)
# is id 1000:1000 and that we use these tags when running:
#
# --privileged 
# --device /dev/fuse 
# --security-opt label=disable 
#
# It is also quite specific to Podman in Podman, so that's locked in.
# -------------------------------------------------------------- # 

# Pre-create necessary directories in the user's home directory
RUN mkdir -p /home/autoimportuser/.local/share/containers/storage /home/autoimportuser/.config/containers

# Add mappings to /etc/subuid and /etc/subgid
RUN echo -e "autoimportuser:1:999\nautoimportuser:1001:64535" > /etc/subuid && \
    echo -e "autoimportuser:1:999\nautoimportuser:1001:64535" > /etc/subgid

# Ensure proper permissions for all relevant directories in the user's home directory
RUN chown -R autoimportuser:autoimportgroup /home/autoimportuser/.local /home/autoimportuser/.config /auto-importer

# Add container configuration files
ADD /containers.conf /etc/containers/containers.conf
ADD /storage.conf /etc/containers/storage.conf
ADD /podman-containers.conf /home/autoimportuser/.config/containers/containers.conf

# Copy & modify the defaults to provide reference if runtime changes needed
RUN sed -e 's|^#mount_program|mount_program|g' \
           -e '/additionalimage.*/a "/var/lib/shared",' \
           -e 's|^mountopt[[:space:]]*=.*$|mountopt = "nodev,fsync=0"|g' \
           /etc/containers/storage.conf \
           > /etc/containers/storage.conf

# Set up internal Podman to pass subscriptions down from host to internal container
RUN printf '/run/secrets/etc-pki-entitlement:/run/secrets/etc-pki-entitlement\n/run/secrets/rhsm:/run/secrets/rhsm\n' > /etc/containers/mounts.conf

# Note VOLUME options must always happen after the chown call above
# RUN commands can not modify existing volumes
VOLUME /var/lib/containers
VOLUME /home/autoimportuser/.local/share/containers

# Create necessary directories for shared storage and lock files
RUN mkdir -p /var/lib/shared/overlay-images \
             /var/lib/shared/overlay-layers \
             /var/lib/shared/vfs-images \
             /var/lib/shared/vfs-layers && \
    touch /var/lib/shared/overlay-images/images.lock && \
    touch /var/lib/shared/overlay-layers/layers.lock && \
    touch /var/lib/shared/vfs-images/images.lock && \
    touch /var/lib/shared/vfs-layers/layers.lock

# Set permissions for Podman tools
RUN chmod 4755 /usr/bin/newgidmap && \
    chmod 4755 /usr/bin/newuidmap

# Set environment variable to allow custom Podman configurations
ENV _CONTAINERS_USERNS_CONFIGURED="" \
    BUILDAH_ISOLATION=chroot

# Copy the application code (when building from the repository context)
COPY . /auto-importer

# Install the package - use git version if available, otherwise use fallback version
RUN if [ -d "/auto-importer/.git" ]; then \
        git config --global --add safe.directory /auto-importer && \
        pip install /auto-importer; \
    else \
        SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install /auto-importer; \
    fi

# Make the logs directory
RUN mkdir -p /auto-importer/logs

# Ensure proper permissions for all relevant directories in the user's auto-importer directory
RUN chown -R autoimportuser:autoimportgroup /auto-importer/logs

# Ensure your application's startup script is executable (already in GIT)
RUN chmod +x /auto-importer/biomero_importer/main.py

# Download and install OMERO Java client libraries
RUN mkdir -p /opt/omero/server && \
    cd /tmp && \
    wget -q https://downloads.openmicroscopy.org/omero/5.6.16/artifacts/OMERO.server-5.6.16-ice36.zip && \
    unzip -q OMERO.server-5.6.16-ice36.zip && \
    mv OMERO.server-5.6.16-ice36 /opt/omero/server/OMERO.server && \
    rm OMERO.server-5.6.16-ice36.zip && \
    chown -R autoimportuser:autoimportgroup /opt/omero

# Set OMERODIR to point to the server installation (runtime environment)
ENV OMERODIR /opt/omero/server/OMERO.server

# Switch to the new user for all subsequent commands
USER autoimportuser

# Set the default command or entrypoint to the main script
ENTRYPOINT ["/opt/conda/bin/conda", "run", "-n", "auto-import-env", "python", "-m", "biomero_importer.main"]
