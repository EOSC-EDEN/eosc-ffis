# Deploying FFIS on an EOSC EU Node VM

End-to-end runbook for running this service as a public HTTPS endpoint on a VM
in the EOSC EU Node (PSNC OpenStack), with a hostname from EGI Dynamic DNS and
a Let's Encrypt certificate.

Worked example throughout:

| Placeholder        | Example value                 |
| ------------------ | ----------------------------- |
| project            | `eden`                        |
| service            | `ffis`                        |
| hostname           | `eden-ffis.vm.fedcloud.eu`    |
| VM login user      | `ubuntu`                      |
| floating IP        | `62.3.175.x` (assigned in A7) |

Substitute your own values. Parts A2 to A4 (network, subnet, router) are done
once per project; if a colleague already created `eden-net` and `eden-router`,
reuse them and start at A5.

---

## Part A - The VM (OpenStack dashboard)

### A1. SSH key pair

Check for an existing key, and only generate one if you have none:

```bash
cat ~/.ssh/id_ed25519.pub            # macOS / Linux
# Get-Content ~\.ssh\id_ed25519.pub  # Windows PowerShell

ssh-keygen -t ed25519 -C "you@example.org"   # only if the above printed nothing
```

Import the public key under **Compute -> Key Pairs -> Import Public Key**, type
`SSH Key`. Upload the `.pub` file rather than pasting, to avoid stray line
breaks.

### A2. Network and subnet

**Network -> Networks -> Create Network**

- Network Name: `eden-net`
- Subnet tab: Name `eden-subnet`, Network Address `192.168.0.0/24`
- Subnet Details: defaults

### A3. Router

**Network -> Routers -> Create Router**

- Router Name: `eden-router`
- External Network: `PSNC-EXT-PUB1-EDU` (the IPv4 network that provides
  floating IPs; `PSNC-EXT-IPV6-PUB2-EDU` is IPv6 only and will not give you a
  floating IPv4)

### A4. Attach the subnet to the router

Open `eden-router` -> **Interfaces -> Add Interface** -> select `eden-subnet`
-> Submit.

### A5. Security group

**Network -> Security Groups -> Create Security Group**, name `ffis-web`.
Three ingress rules:

| Port        | Remote CIDR    | Why                                                        |
| ----------- | -------------- | ---------------------------------------------------------- |
| 22 (SSH)    | `<your-IP>/32` | Administration. Never open 22 to `0.0.0.0/0` on a public VM |
| 80 (HTTP)   | `0.0.0.0/0`    | Let's Encrypt HTTP-01 challenge and the 80 -> 443 redirect  |
| 443 (HTTPS) | `0.0.0.0/0`    | The service itself                                          |

Do **not** open 8000. The container publishes on loopback only and nginx
proxies to it.

Find your current IPv4 address:

```bash
curl -4 -s https://ifconfig.me       # macOS / Linux
# curl.exe -4 -s https://ifconfig.me # Windows PowerShell
```

When you move between networks (home, office, VPN) your address changes and
SSH stops working. Add another rule: **your group -> Manage Rules -> Add
Rule**, SSH, CIDR `<new-IP>/32`, and put the location in the description.

### A6. Launch the instance

**Compute -> Instances -> Launch Instance**

- Details: name `ffis`
- Source: Images -> `Ubuntu 24.04 LTS`, Create New Volume: **Yes**, 40 GB.
  Default login user is `ubuntu`.
- Flavour: **at least 2 vCPU and 4 GB RAM**. FFIS is not a toy workload: the
  image build compiles nothing but does pull `onnxruntime` for Magika, the
  running container holds the Magika model in memory, and uvicorn runs 2
  workers. 1 vCPU / 2 GB will build slowly and swap under concurrent uploads.
- Networks: `eden-net`
- Security Groups: `ffis-web`
- Key Pair: the one from A1

Disk sizing: the built image is roughly 2 to 3 GB (Python 3.11 slim, Magika
and its ONNX runtime, the Siegfried binary and PRONOM signatures). With build
cache, logs and the result cache, 40 GB is comfortable and 20 GB is the floor.

### A7. Floating IP

**Network -> Floating IPs -> Allocate IP To Project**, pool
`PSNC-EXT-PUB1-EDU`. Then on the instance row: dropdown -> **Associate
Floating IP** -> pick the instance port -> Associate.

Verify and do first-boot housekeeping:

```bash
ssh ubuntu@<floating-IP>

sudo apt update && sudo apt -y upgrade
sudo apt -y install unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

---

## Part B - Hostname (EGI Dynamic DNS)

Reference: https://docs.egi.eu/users/compute/cloud-compute/dynamic-dns/

### B1. Register the hostname

Log in at https://nsupdate.fedcloud.eu/ via EGI Check-in (your institutional
account works). **Overview -> Add host**: hostname `eden-ffis`, domain
`vm.fedcloud.eu`. The portal shows a secret exactly once - note it down.

Full name: `eden-ffis.vm.fedcloud.eu`

### B2. Point the name at the floating IP

From the VM (so the update sees the VM's public source address):

```bash
curl "https://eden-ffis.vm.fedcloud.eu:<secret>@nsupdate.fedcloud.eu/nic/update"
```

Expected response: `good <floating-IP>` (or `nochg <floating-IP>`).

### B3. Verify it resolves

From your local machine:

```bash
dig +short eden-ffis.vm.fedcloud.eu          # macOS / Linux
# Resolve-DnsName eden-ffis.vm.fedcloud.eu   # Windows PowerShell
```

The answer must be your floating IP. Do not continue to Part C until it is -
certbot will fail the HTTP-01 challenge otherwise, and repeated failures count
against Let's Encrypt rate limits.

### B4. Keep the record fresh (optional but recommended)

The floating IP is static, so the record will not drift on its own. A weekly
refresh protects against the record being aged out or reset. Store the secret
root-only, never in the repository:

```bash
sudo install -m 600 /dev/null /etc/ffis-ddns.secret
sudo tee /etc/ffis-ddns.secret >/dev/null <<'SECRET'
https://eden-ffis.vm.fedcloud.eu:<secret>@nsupdate.fedcloud.eu/nic/update
SECRET

sudo tee /etc/cron.weekly/ffis-ddns >/dev/null <<'CRON'
#!/bin/sh
curl -fsS "$(cat /etc/ffis-ddns.secret)" >/dev/null
CRON
sudo chmod 755 /etc/cron.weekly/ffis-ddns
```

---

## Part C - nginx and HTTPS

### C1. Install nginx

```bash
systemctl is-active nginx || sudo apt -y install nginx
sudo systemctl enable --now nginx
```

### C2. Install the FFIS site config

certbot's nginx plugin edits an existing server block; it does not invent one.
Install the config from this repository (see `deploy/nginx-ffis.conf`) and
disable the stock default site:

```bash
sudo cp deploy/nginx-ffis.conf /etc/nginx/sites-available/ffis
sudo sed -i 's/eden-ffis\.vm\.fedcloud\.eu/<your-hostname>/' /etc/nginx/sites-available/ffis
sudo ln -sf /etc/nginx/sites-available/ffis /etc/nginx/sites-enabled/ffis
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

That config is not just a `proxy_pass`. Two settings matter:

- `client_max_body_size 100m` - nginx defaults to **1 MB**. Without this, every
  upload over 1 MB is rejected with a 413 by nginx before FFIS sees it, while
  the app itself happily accepts 100 MB. Keep this number and
  `FFIS_MAX_UPLOAD_BYTES` in step D3 in sync.
- `proxy_read_timeout 180s` on `/identify` - identification of a large file
  through Siegfried and Magika can exceed nginx's 60 second default, which
  would surface as a 504 mid-request.

It also rate limits `/identify` to 2 req/s per client IP with a burst of 10.
A public identification endpoint is free CPU for anyone who finds it; drop or
raise the limit deliberately, not by accident.

At this point nginx answers on port 80 but has nothing behind it yet - a 502 is
the expected response until Part D. If you want to confirm the path end to end
before deploying, `curl -I http://<your-hostname>/` from your laptop should
return a 502 from nginx rather than a timeout. A timeout means the security
group or DNS is wrong.

### C3. Certificate

```bash
sudo snap install core; sudo snap refresh core
sudo snap install --classic certbot
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot

# Dry run first - cheap, and does not burn rate limits
sudo certbot certonly --nginx -d eden-ffis.vm.fedcloud.eu --dry-run

# For real
sudo certbot --nginx -d eden-ffis.vm.fedcloud.eu \
  --agree-tos --no-eff-email -m you@example.org --redirect
```

`--redirect` makes certbot add the 80 -> 443 redirect to the site config.
`--no-eff-email` opts out of the EFF mailing list.

Verify:

```bash
curl -I http://eden-ffis.vm.fedcloud.eu/     # expect 301 to https
curl -I https://eden-ffis.vm.fedcloud.eu/    # expect 502 for now, 200 after Part D
```

Renewal is automatic via the snap timer. Confirm:

```bash
systemctl list-timers | grep -i certbot
sudo certbot renew --dry-run
```

---

## Part D - Deploy FFIS

### D1. Install Docker

```bash
sudo apt -y install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu   # log out and back in for this to take effect
```

### D2. Get the code

```bash
sudo mkdir -p /opt/ffis && sudo chown ubuntu:ubuntu /opt/ffis
git clone https://github.com/<org>/eosc-ffis.git /opt/ffis
cd /opt/ffis
```

### D3. Start the service

```bash
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

The first build takes several minutes: it downloads the Siegfried binary,
runs `sf -update` to fetch the PRONOM signature file, and installs Magika and
onnxruntime.

`deploy/docker-compose.prod.yml` differs from the development
`docker-compose.yml` in one respect that matters here: it publishes on
`127.0.0.1:8000` instead of `0.0.0.0:8000`. With the dev file, Docker inserts
its own iptables rules ahead of the host firewall and the app would be exposed
on port 8000 in the clear. The OpenStack security group still blocks it, but
relying on a single layer for that is a bad habit.

Check it:

```bash
docker compose -f deploy/docker-compose.prod.yml ps
curl -s localhost:8000/health
curl -s localhost:8000/tools | head
```

### D4. Verify the public endpoint

From your laptop:

```bash
curl -I https://eden-ffis.vm.fedcloud.eu/                      # 200, frontend
curl -s https://eden-ffis.vm.fedcloud.eu/health                # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' \
  https://eden-ffis.vm.fedcloud.eu/docs                        # 200, OpenAPI docs

curl -s -X POST https://eden-ffis.vm.fedcloud.eu/identify \
  -F "file=@some-document.pdf" | head -40
```

---

## Part E - Operations

### Updating the service

```bash
cd /opt/ffis
git pull
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

The PRONOM signature file is baked in at build time, so a periodic rebuild is
also how signatures get refreshed. Rebuilding monthly is reasonable for a
preservation service.

### Logs

```bash
docker compose -f deploy/docker-compose.prod.yml logs -f ffis
sudo tail -f /var/log/nginx/ffis.access.log
```

Container logs are capped at 3 x 10 MB by the compose file so they cannot fill
the disk.

### Reboots

`restart: unless-stopped` plus an enabled `docker` service brings the container
back automatically. Confirm after the first reboot rather than assuming it.

### Security notes for a public deployment

- `/identify/path` (by-reference mode) is denied outright unless
  `FFIS_ALLOWED_PATH_PREFIXES` is set. Leave it unset on a public instance
  unless you have mounted storage and intend to expose it. It is a
  read-a-file-from-the-server endpoint; the prefix allowlist is the only thing
  standing between it and arbitrary local file reads.
- CORS is `allow_origins=["*"]` (`src/ffis/main.py`). Fine for an anonymous
  read-only API, worth narrowing if authentication is ever added.
- The service has no authentication. Anyone who finds the hostname can spend
  its CPU. The nginx rate limit is the mitigation; for a non-public pilot, add
  an IP allowlist or HTTP basic auth to the `location ~ ^/identify` block
  instead.
- Uploads are held in memory during identification (`await file.read()` in
  `src/ffis/api/routes.py`). With 2 workers and a 100 MB limit, the worst case
  is around 200 MB of request buffers on top of the Magika model. This is
  another reason for the 4 GB floor, and a reason to lower
  `FFIS_MAX_UPLOAD_BYTES` if your files are small.

### Troubleshooting

| Symptom                            | Likely cause                                                       |
| ---------------------------------- | ------------------------------------------------------------------ |
| SSH times out                      | Your IP changed; add a new port 22 rule to `ffis-web`               |
| certbot HTTP-01 challenge fails    | DNS not yet pointing at the floating IP, or port 80 not open        |
| 413 on upload                      | `client_max_body_size` missing or below `FFIS_MAX_UPLOAD_BYTES`     |
| 504 on a large file                | `proxy_read_timeout` too low on `/identify`                         |
| 502 from nginx                     | Container not running: check `docker compose ps` and the logs       |
| 429 on `/identify`                 | nginx rate limit; raise it in the site config if legitimate traffic |
| Build fails on `sf -update`        | Transient PRONOM fetch failure; rerun the build                     |
