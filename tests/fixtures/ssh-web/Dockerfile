FROM python:3.13.16-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && useradd --create-home --shell /bin/sh devhard \
    && usermod --password '*' devhard \
    && mkdir -p /run/sshd /home/devhard/.ssh \
    && chmod 700 /home/devhard/.ssh

COPY sshd_config /etc/ssh/sshd_config
COPY server.py /opt/devhard-fixture/server.py

EXPOSE 22
HEALTHCHECK --interval=2s --timeout=2s --start-period=3s --retries=15 \
    CMD ["python", "/opt/devhard-fixture/server.py", "--healthcheck"]
CMD ["python", "/opt/devhard-fixture/server.py"]
