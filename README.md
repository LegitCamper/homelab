# Installation
- install fedora core and then rebase to `ucore:stable` or `ucore:stable-nvidia`
- ensure tailscaled is running: `sudo systemctl start tailescaled` (ansible will ensure its enabled)
- sign into tailscale: `tailscale up` 
- `ansible-playbook -i server.tailscale.hostname, homeserver.yml` (you will need `--ask-become-pass` if there is a sudo password)
