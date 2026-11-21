"""Self-signed certificate and the legacy TLS context offered to the box."""
import os, ssl, sys, warnings
from datetime import datetime, timedelta, timezone
from .config import CERT_DIR, CERT_FILE, KEY_FILE, SERVER_CN, TLS_CIPHERS
from .logs import log


# ---------- self-signed cert ----------

def gen_cert():
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"generating self-signed cert for {SERVER_CN}...")
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "FR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FakeAzure"),
        x509.NameAttribute(NameOID.COMMON_NAME, SERVER_CN),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 10))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(SERVER_CN),
                x509.DNSName("*.azure-devices.net"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    log(f"cert written: {CERT_FILE}")



def build_ssl_context():
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    # HF-LPB100 firmware speaks legacy TLS (small set of older ciphers).
    # Relax everything so we can negotiate.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # intentional: legacy box
            c.minimum_version = ssl.TLSVersion.TLSv1
    except (AttributeError, ValueError):
        pass
    try:
        c.set_ciphers(TLS_CIPHERS)
    except ssl.SSLError as e:
        sys.exit(f"FATAL: TLS_CIPHERS {TLS_CIPHERS!r} rejected by OpenSSL: {e}")
    return c
