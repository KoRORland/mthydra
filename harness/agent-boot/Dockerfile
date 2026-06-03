# amd64 mirror of a TimeWeb RU box, for the agent-boot harness.
FROM ubuntu:24.04
RUN apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-cryptography iptables curl ca-certificates iproute2 procps \
    && rm -rf /var/lib/apt/lists/*
# sing-box: the RU-side Reality client, installed exactly as the cloud-init does.
RUN curl -fsSL https://sing-box.app/install.sh | sh
# Harness shims (volatile-journald + power-off detector) ahead of system bins.
COPY shims/journalctl shims/shutdown /usr/local/bin/
RUN chmod +x /usr/local/bin/journalctl /usr/local/bin/shutdown
# Agent source closure (ru_agent + descriptor + mthydra/__init__), staged by run.sh.
COPY _agentsrc/ /opt/agent/
# Real amd64 mtg ELF + the generated seed, staged by run.sh.
COPY mtg /opt/harness/mtg
COPY seed.json /opt/harness/seed.json
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/harness/mtg
ENV PYTHONPATH=/opt/agent
ENTRYPOINT ["/entrypoint.sh"]
