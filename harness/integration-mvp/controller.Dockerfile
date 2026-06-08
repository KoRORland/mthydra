# EU controller — mirrors the AWS EC2 Ubuntu host from quickstart Part 1/3.
# Build context = repo root (so we can pip install the real source).
FROM ubuntu:24.04
RUN apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-venv python3-pip python3-cryptography \
        git age sqlite3 curl ca-certificates openssh-client jq iproute2 procps \
    && rm -rf /var/lib/apt/lists/*

# Install the real mthydra source as the installer would (/opt/mthydra/src + venv).
COPY pyproject.toml /opt/mthydra/src/pyproject.toml
COPY src /opt/mthydra/src/src
RUN python3 -m venv /opt/mthydra/venv \
    && /opt/mthydra/venv/bin/pip install --upgrade pip \
    && /opt/mthydra/venv/bin/pip install -e /opt/mthydra/src
ENV PATH="/opt/mthydra/venv/bin:${PATH}"
# Controller commands resolve mthydra-controller next to sys.executable.
ENV MTHYDRA_CONTROLLER=/opt/mthydra/venv/bin/mthydra-controller

COPY harness/integration-mvp/controller/quickstart.sh /opt/quickstart.sh
COPY harness/integration-mvp/controller/stage_image.py /opt/stage_image.py
COPY harness/integration-mvp/controller/add_eu_exit.py /opt/add_eu_exit.py
COPY harness/integration-mvp/controller/extract_seed.py /opt/extract_seed.py
RUN chmod +x /opt/quickstart.sh
CMD ["sleep", "infinity"]
