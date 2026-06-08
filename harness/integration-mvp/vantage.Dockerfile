# RU probe vantage — mirrors the TimeWeb Moscow VPS from quickstart Part 5.
# Just a host the operator (controller) reaches to run probe commands.
FROM ubuntu:24.04
RUN apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        openssh-server age curl openssl ncat jq ca-certificates iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 700 /root/.ssh
# The controller injects its bootstrap key at run time (run.sh writes
# /root/.ssh/authorized_keys via docker cp before starting sshd).
CMD ["/usr/sbin/sshd", "-D", "-e"]
