"""Persistent self-signed certificate for the signaling server.

The desktop page always loads via https://127.0.0.1 — a persistent cert with
stable SANs, installed once into the Windows Root store, means WebView2 (and
any browser on the PC) never shows a certificate warning. This also replaces
werkzeug's per-run adhoc cert, which changes every launch.

Files live next to settings.json (portable). If the LAN IP changes the cert
is regenerated so a future phone-side trust flow stays valid.
"""

import datetime
import ipaddress
import os
import socket
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_SUBJECT = "AkiMic Local"
VALIDITY_DAYS = 825


def ensure_cert(base_dir: str, lan_ip: str) -> tuple[str, str] | None:
    """Return (cert_path, key_path), generating when missing or stale."""
    cert_path = os.path.join(base_dir, "cert.pem")
    key_path = os.path.join(base_dir, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path) and _san_ok(cert_path, lan_ip):
        return cert_path, key_path
    try:
        _generate(cert_path, key_path, lan_ip)
        return cert_path, key_path
    except Exception as e:
        print(f"[AkiMic] cert generation failed: {e}")
        return None


def _san_ok(cert_path: str, lan_ip: str) -> bool:
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = {str(i) for i in ext.get_values_for_type(x509.IPAddress)}
        return "127.0.0.1" in ips and str(lan_ip) in ips
    except Exception:
        return False


def _generate(cert_path: str, key_path: str, lan_ip: str) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_SUBJECT)])
    now = datetime.datetime.now(datetime.timezone.utc)

    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(lan_ip)))
    except ValueError:
        pass
    hostname = socket.gethostname()
    if hostname:
        san.append(x509.DNSName(hostname))

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def ensure_trusted(cert_path: str) -> bool:
    """Install the cert into the Windows Root store so local browsers trust it.

    Needs admin (the packaged exe runs elevated). Idempotent.
    """
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            ["certutil", "-addstore", "-f", "Root", cert_path],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
