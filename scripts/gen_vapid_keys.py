"""Generiert ein VAPID-Keypair für Web Push.

Einmal-Setup. Nutzung:
    python3 scripts/gen_vapid_keys.py

Liefert:
- VAPID_PUBLIC_KEY  (URL-safe base64, ohne padding)  → in docs/index.html eintragen
- VAPID_PRIVATE_KEY (PEM, mehrzeilig)                → als GitHub Secret + lokal in .env

Speichert NICHT die Keys auf Disk — du musst die Outputs manuell kopieren.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> None:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode("ascii")

    print("=" * 72)
    print("VAPID Keypair generiert. Bitte sicher speichern.")
    print("=" * 72)
    print()
    print("### VAPID_PUBLIC_KEY (in docs/index.html ersetzen) ###")
    print(pub_b64)
    print()
    print("### VAPID_PRIVATE_KEY (PEM, GitHub Secret + lokale .env) ###")
    print(priv_pem.rstrip())
    print()
    print("Setup-Schritte:")
    print("  1. In docs/index.html den Platzhalter 'REPLACE_ME_WITH_VAPID_PUBLIC_KEY'")
    print("     durch den Public-Key oben ersetzen und committen.")
    print("  2. GitHub Secret 'VAPID_PRIVATE_KEY' anlegen (komplette PEM-Block).")
    print("  3. GitHub Secret 'VAPID_SUB' anlegen ('mailto:deine@email').")
    print("  4. Lokale .env ergänzen:")
    print("       VAPID_PRIVATE_KEY=\"-----BEGIN PRIVATE KEY-----...-----END PRIVATE KEY-----\"")
    print("       VAPID_SUB=mailto:deine@email")
    print()


if __name__ == "__main__":
    main()
