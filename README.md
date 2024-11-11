# Installation
- install fedora core and then rebase to `ucore:stable` or `ucore:stable-nvidia`
- sign into tailscale: `tailscale up` 
- `ansible-playbook -i server.tailscale.hostname, homeserver.yml`
