"""Debug-only: VAPID Key Format inspizieren ohne Secret preiszugeben."""
import os, re, base64

k = os.getenv("VAPID_PRIVATE_KEY", "").strip().strip('"').strip("'")
print(f"Laenge nach strip: {len(k)}")
print(f"Newlines:          {k.count(chr(10))}")
print(f"Erste 10 Zeichen:  {k[:10]!r}")
print(f"Letzte 15 Zeichen: {k[-15:]!r}")

# Zeige alle Zeilen mit Länge
lines = k.split("\n")
for i, line in enumerate(lines):
    is_header = line.startswith("-----")
    print(f"  Zeile {i}: len={len(line)} header={is_header} first_char={line[:1]!r}")

# Versuche PEM zu parsen
if "-----BEGIN" in k:
    # Extrahiere Body zwischen Header und Footer
    m = re.search(r"-----BEGIN[^-]+-----(.+?)-----END", k, re.DOTALL)
    if m:
        body = "".join(m.group(1).split())
        print(f"Base64-Body Laenge: {len(body)}")
        print(f"Base64-Body Erste 4 Zeichen: {body[:4]!r}")
        print(f"Body Byte 0 ASCII: {ord(body[0]) if body else 'leer'}")
        try:
            decoded = base64.b64decode(body + "==")
            print(f"DER-Laenge: {len(decoded)} Bytes")
            print(f"DER Byte 0 (soll 0x30=48): {decoded[0]}")
        except Exception as e:
            print(f"Base64-Decode Fehler: {e}")
