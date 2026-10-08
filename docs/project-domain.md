# EDEN project domain (`eden-fidelis.eu`)

Plan for giving EDEN services hostnames under the project's own domain, such
as `ffis.services.eden-fidelis.eu`, instead of the generic
`eden-ffis.vm.fedcloud.eu`.

**Status (2026-10-08): open, blocked.** We do not yet know who administers
DNS for `eden-fidelis.eu`. Nothing in this document can start until that is
settled. Services launch on `vm.fedcloud.eu` names in the meantime, and the
switch can happen later without touching any service VM.

## Why

- Consistent, identifiable names that say which project a service belongs to.
- Names that stay meaningful if the services move away from EGI Dynamic DNS
  or away from `vm.fedcloud.eu` hosting.
- It is common practice: https://nsupdate.fedcloud.eu/overview/ lists several
  EOSC and EOSC-service project domains.

## How it works

The `eden-fidelis.eu` zone stays where it is. One subdomain,
`services.eden-fidelis.eu`, is delegated to the EGI name servers, and EGI
hosts it on nsupdate.fedcloud.eu as a project domain. From then on, hosts in
it are registered and updated exactly like `vm.fedcloud.eu` hosts today: in
the nsupdate portal, each with its own secret.

```
eden-fidelis.eu            managed by the domain administrator (unchanged)
 +-- services              NS records -> EGI name servers   (one-off change)
      +-- ffis             A record -> entry VM floating IP (nsupdate, FFIS team)
      +-- harvester        A record -> entry VM floating IP (nsupdate, harvester team)
```

All names point at the shared entry VM ([entry-vm.md](entry-vm.md)), which
terminates TLS. The service VMs never see the hostname change.

## Roles

| Role                         | Who                   | Does                                                  |
| ---------------------------- | --------------------- | ----------------------------------------------------- |
| `eden-fidelis.eu` DNS admin  | **TBD**               | Adds the `NS` records for `services` (once); checks CAA |
| Requester                    | Entry VM owner (Mattias Levlin, CSC) | Opens the EGI Helpdesk ticket, coordinates the switch |
| EGI Dynamic DNS support      | EGI Helpdesk          | Creates the project domain, provides name servers     |
| Service teams                | Per service           | Register their host under the new domain              |
| Entry VM owner               | Mattias Levlin (CSC)  | Adds the new names to nginx and the certificates      |

## Open questions

1. **Who administers DNS for `eden-fidelis.eu`?** Registrar account or DNS
   hosting provider, and a person who can make changes there. This is the
   blocker.
2. **How long will the domain live?** If `eden-fidelis.eu` lapses when the
   project ends, every service name under it breaks. Find out who pays for
   renewal and until when, before publishing these names anywhere permanent
   (papers, deliverables, registries).
3. **Subdomain name.** `services.eden-fidelis.eu` is the working assumption.
   Shorter alternatives (`svc.`) are possible; decide before the ticket, as
   the name is fixed once delegated.
4. **Who can manage the domain in nsupdate.** It should be more than one EGI
   Check-in account, so it is not tied to a single person.

## Steps

### 1. Find the domain administrator

Settle open questions 1 and 2. Share this document with them; their part is
step 3 only.

### 2. EGI Helpdesk ticket

At https://helpdesk.egi.eu/, support unit **Dynamic DNS**. Draft:

> Subject: Project domain services.eden-fidelis.eu on nsupdate.fedcloud.eu
>
> Hello,
>
> The EDEN project runs services in the EOSC EU Node (PSNC) and currently
> uses hostnames under vm.fedcloud.eu. We would like a project domain,
> services.eden-fidelis.eu, on nsupdate.fedcloud.eu, as other EOSC projects
> have.
>
> We control eden-fidelis.eu and will delegate services.eden-fidelis.eu to
> the EGI name servers. Could you tell us which name servers to delegate to,
> and set the domain up so that it can be managed by the following EGI
> Check-in accounts: <list>?
>
> Thank you,
> <name>, on behalf of the EDEN project

### 3. Delegation (domain administrator)

In the `eden-fidelis.eu` zone, add one `NS` record per name server EGI
provided in step 2:

```
services.eden-fidelis.eu.   3600  IN  NS  <egi-nameserver-1>.
services.eden-fidelis.eu.   3600  IN  NS  <egi-nameserver-2>.
```

Most DNS hosting panels have an "NS record" type for this; the name field is
`services`. Two related checks:

- **CAA:** if `eden-fidelis.eu` has CAA records, they apply to everything
  below it unless overridden. They must allow `letsencrypt.org`, or
  certificate issuance for the new names fails.
- **DNSSEC:** if the zone is signed, no `DS` record is needed for the
  delegation unless EGI provides one. Without it, `services` is simply an
  unsigned subzone.

### 4. Verify the delegation

```bash
dig NS services.eden-fidelis.eu +short     # the EGI name servers
dig +trace services.eden-fidelis.eu        # delegation path ends at EGI
```

Also check that the domain shows up under your account in the nsupdate
portal.

### 5. Register the service hosts

Each service team registers its host in the nsupdate portal under the new
domain and points it at the entry VM, as in
[deployment.md, B2](deployment.md#b2-point-the-name-at-the-entry-vm):

```bash
curl "https://ffis.services.eden-fidelis.eu:<secret>@nsupdate.fedcloud.eu/nic/update?myip=<entry-floating-IP>"
dig +short ffis.services.eden-fidelis.eu   # the entry floating IP
```

Naming convention: `<service>.services.eden-fidelis.eu`, without the `eden-`
prefix the `vm.fedcloud.eu` names need.

| Service   | Current name                     | New name                             |
| --------- | -------------------------------- | ------------------------------------ |
| FFIS      | `eden-ffis.vm.fedcloud.eu`       | `ffis.services.eden-fidelis.eu`      |
| Harvester | (harvester team)                 | `harvester.services.eden-fidelis.eu` |

### 6. Entry VM: serve both names

Per service, on the entry VM: add the new name to `server_name` in the site
file, then reissue the certificate covering both names:

```bash
sudo sed -i 's/server_name eden-ffis\.vm\.fedcloud\.eu;/server_name eden-ffis.vm.fedcloud.eu ffis.services.eden-fidelis.eu;/' \
  /etc/nginx/sites-available/ffis
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx --cert-name eden-ffis.vm.fedcloud.eu \
  -d eden-ffis.vm.fedcloud.eu -d ffis.services.eden-fidelis.eu

curl -s https://ffis.services.eden-fidelis.eu/health
```

### 7. Cut over

Once both names work:

1. Announce the new names and update links in documentation and
   configuration that refers to the services.
2. After a transition period, redirect the old name to the new one in the
   site file (`return 301 https://ffis.services.eden-fidelis.eu$request_uri;`
   in a server block for the old name).
3. Eventually remove the old name from the certificate and delete the old
   host in nsupdate.

Until step 3, rolling back is just using the old names again. Nothing on the
service VMs changes at any point.
