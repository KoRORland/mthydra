# RU box — mirrors the TimeWeb Moscow VM from quickstart Part 7. amd64.
# Runtime deps are exactly what the real cloud-init runcmd apt-installs; the
# agent CODE arrives at boot as the S3-published tarball (not baked in).
FROM ubuntu:24.04
RUN apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-cryptography iptables curl ca-certificates \
        iproute2 procps openssl \
    && rm -rf /var/lib/apt/lists/*
# sing-box: the RU-side Reality client, installed exactly as cloud-init does.
RUN curl -fsSL https://sing-box.app/install.sh | sh
# Shims: volatile-journald, power-off detector, no-op systemctl (no systemd in
# a container; the agent's SIGHUP-on-refresh would otherwise raise).
COPY harness/agent-boot/shims/journalctl harness/agent-boot/shims/shutdown \
     harness/integration-mvp/rubox/systemctl /usr/local/bin/
COPY harness/integration-mvp/rubox/boot.sh /boot.sh
RUN chmod +x /usr/local/bin/journalctl /usr/local/bin/shutdown \
        /usr/local/bin/systemctl /boot.sh
ENTRYPOINT ["/boot.sh"]
