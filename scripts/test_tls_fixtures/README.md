# Test TLS fixtures

Throwaway certificates for `scripts/test_registry_tls.py`, which tests the registry listener's TLS
support with real handshakes on localhost. They exist so the offline suite needs neither OpenSSL nor
the `cryptography` package at run time: neither is a project dependency, and a test that cannot run on
a fresh machine is not a test.

**These are not secrets and no deployment uses them.** They are pasted here in full so the suite is
self-contained. The CA private keys are deliberately *not* in the repository, so nothing here can mint
another certificate this suite would trust.

| File | Purpose |
|---|---|
| `ca.crt` | The listener's client CA. A device presenting a certificate it signed is trusted. |
| `server.crt`, `server.key` | The listener's own certificate, `CN=localhost` with `DNS:localhost, IP:127.0.0.1`. |
| `listed-client.crt`, `.key` | The one device the credential file lists, mapped by fingerprint to `tls-kitchen-1`. |
| `unlisted-client.crt`, `.key` | Signed by `ca.crt`, so the handshake succeeds, but absent from the credential file. |
| `other-ca.crt` | A second CA the listener does not trust. |
| `other-client.crt`, `.key` | Signed by `other-ca.crt`: its handshake must fail. |

## Regenerating the set

The set is replaced as a whole rather than extended, because the CA keys are not kept here. Any change
must keep `server.crt`'s subject alternative names, the client certificates' `clientAuth` usage, and the
`listed-client` fingerprint in `test_registry_tls.py` (which is computed from the file, so it follows
automatically).

```sh
cd scripts/test_tls_fixtures
DAYS=36500
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days $DAYS -subj "/CN=bridge-test-ca"
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=localhost"
printf "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days $DAYS -extfile server.ext
printf "basicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\n" > client.ext
for name in listed-client unlisted-client; do
  openssl req -newkey rsa:2048 -nodes -keyout $name.key -out $name.csr -subj "/CN=$name"
  openssl x509 -req -in $name.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out $name.crt -days $DAYS -extfile client.ext
done
openssl req -x509 -newkey rsa:2048 -nodes -keyout other-ca.key -out other-ca.crt -days $DAYS -subj "/CN=other-test-ca"
openssl req -newkey rsa:2048 -nodes -keyout other-client.key -out other-client.csr -subj "/CN=other-client"
openssl x509 -req -in other-client.csr -CA other-ca.crt -CAkey other-ca.key -CAcreateserial -out other-client.crt -days $DAYS -extfile client.ext
rm -f *.csr *.ext *.srl ca.key other-ca.key
```
