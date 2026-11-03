# Docker on a machine without systemd (apt blocked)

Read when a task needs Docker (e.g. Postgres-backed tests) and `docker info` fails.

- If apt cannot reach the Debian mirrors (proxy), install the official static binaries instead (pick the current version):
  `curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-<version>.tgz | tar xz; sudo install docker/* /usr/local/bin/; sudo groupadd -f docker; sudo usermod -aG docker "$USER"`.
- If PID 1 is not systemd (e.g. tini in a container), start the daemon by hand:
  `sudo sh -c 'nohup dockerd --iptables=false --ip6tables=false --group docker >/tmp/dockerd.log 2>&1 &'` (overlayfs works; vfs is not needed).
- Shells opened before the group change do not have it: `sudo setfacl -m "u:$USER:rw" /var/run/docker.sock`, again after every dockerd restart.
- Check: `docker run --rm hello-world`, then the service image the tests need (e.g. `postgres:16-alpine`).
