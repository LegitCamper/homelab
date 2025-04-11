# Overview
## Hardware
- Gateway: A server with a public ip running on a vps like Digital Ocean
- Homeserver: A server behind a nat
## Networking
- The gateway server forwards all packets from 80 & 443 to to homeserver
## Playbooks
- `gateway.yaml` configures the gateway to forward traffic and protect it with crowdsec
- `homeserver.yml` configures homeserver with the compose files

# Install Ucore on homeserver
- create a simple [butane configuration](https://github.com/ublue-os/ucore/blob/main/examples/ucore-autorebase.butane)
- transpile it to an [ingition file](https://coreos.github.io/butane/getting-started/#container-image)
- validate the ignition file `podman run --pull=always --rm -i quay.io/coreos/ignition-validate:release - < ucore.ign`
> This example uses podman, but docker can be used too
- start an http server to host the ingnition file and prepare for install `sudo podman run --rm --privileged -p 8080:5000 --security-opt label=disable -v $PWD/ucore.ign:/html/ucore.ign  ghcr.io/patrickdappollonio/docker-http-server:v2`
- boot up the fedora core installer and install with your ingition file: `sudo coreos-installer install /dev/sda --ignition-url https://192.168.0.100:8080/ucore.ign` and then reboot

# Final Setup
- ensure tailscaled is running: `sudo systemctl start tailescaled` (ansible will ensure its enabled) and then sign in `tailscale up` 
- `ansible-playbook -i server.tailscale.hostname, homeserver.yml` (you will need `--ask-become-pass` if there is a sudo password)
- The server should be ready to go 🎉
