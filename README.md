# Install Ucore
- create a simple [butane configuration](https://github.com/ublue-os/ucore/blob/main/examples/ucore-autorebase.butane)
- transpile it to an [ingition file](https://coreos.github.io/butane/getting-started/#container-image)
- validate the ignition file `podman run --pull=always --rm -i quay.io/coreos/ignition-validate:release - < ucore.ign`
> This example uses podman, but docker can be used too
- start an http server to host the ingnition file and prepare for install `podman run --rm --security-opt label=disable -v $PWD/ucore.ign:/html/ucore.ign -p 8080:5000 ghcr.io/patrickdappollonio/docker-http-server:v2`
- boot up the fedora core installer and install with your ingition file: `sudo coreos-installer install /dev/sda --ignition-url https://example.com/example.ign` and then reboot

# Setup
- ensure tailscaled is running: `sudo systemctl start tailescaled` (ansible will ensure its enabled) and then sign in `tailscale up` 
- `ansible-playbook -i server.tailscale.hostname, homeserver.yml` (you will need `--ask-become-pass` if there is a sudo password)
- The server should be ready to go 
