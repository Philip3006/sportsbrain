"""Debug-only: VAPID Key Format inspizieren ohne Secret preiszugeben."""
import os, re

k = os.getenv("VAPID_PRIVATE_KEY", "")
literal_backslash_n = "\\n" in k
real_newlines = k.count("\n")
starts_pem = k.startswith("-----BEGIN")
header_match = re.match(r"(-+BEGIN[^-]+-+)", k)

print(f"Laenge:            {len(k)}")
print(f"Startet mit BEGIN: {starts_pem}")
print(f"Literal backsl-n:  {literal_backslash_n}")
print(f"Echte Newlines:    {real_newlines}")
print(f"Header:            {header_match.group(1) if header_match else 'KEIN HEADER'}")
print(f"Erste 10 Zeichen:  {k[:10]!r}")
print(f"Letzte 10 Zeichen: {k[-10:]!r}")
# Byte-Werte der ersten 5 Zeichen
print("Erste 5 Bytes:     " + " ".join(str(ord(c)) for c in k[:5]))
